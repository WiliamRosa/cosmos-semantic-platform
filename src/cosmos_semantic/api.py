"""REST API for applications (FastAPI)."""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from cosmos_semantic.auth import Authenticator, AuthError, ForbiddenError
from cosmos_semantic.errors import (
    ConnectorError,
    CosmosError,
    ModelError,
    PolicyError,
    QueryError,
)
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService

_STATUS = {
    AuthError: 401,
    ForbiddenError: 403,
    PolicyError: 403,
    QueryError: 400,
    ConnectorError: 502,
    ModelError: 500,
}


def create_app(
    service: SemanticService,
    authenticator: Authenticator | None,
    *,
    allow_anonymous: bool = False,
    mcp_app: Any = None,
) -> FastAPI:
    lifespan = None
    if mcp_app is not None:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

    app = FastAPI(
        title="Cosmos Semantic",
        version="0.1.0",
        description="Semantic layer with its own engine: metrics, business rules and glossary.",
        lifespan=lifespan,
    )
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    @app.exception_handler(CosmosError)
    async def _cosmos_error(_request: Request, exc: CosmosError) -> JSONResponse:
        status = next((code for cls, code in _STATUS.items() if isinstance(exc, cls)), 500)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(
            {"error": type(exc).__name__, "detail": str(exc)}, status_code=status, headers=headers
        )

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Principal:
        if authenticator is None or not authenticator.settings.enabled:
            if allow_anonymous:
                return Principal(channel="api", subject="anonymous")
            raise AuthError("authentication is not configured on the server")
        bearer = None
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not value:
                raise AuthError("use `Authorization: Bearer <token>`")
            bearer = value.strip()
        return authenticator.authenticate(bearer=bearer, api_key=x_api_key, channel="api")

    User = Annotated[Principal, Depends(principal)]

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/members", summary="Available metrics, dimensions and rules")
    def members(_user: User) -> dict[str, Any]:
        return service.catalog.members()

    @app.get("/v1/concepts", summary="Search glossary concepts by term or synonym")
    def concepts(q: str, _user: User, limit: int = 5) -> list[dict[str, Any]]:
        return service.catalog.search_concepts(q, limit)

    @app.get("/v1/explain/{name}", summary="Explain a metric, rule, dimension or concept")
    def explain(name: str, _user: User) -> dict[str, Any]:
        return service.catalog.explain(name)

    @app.post("/v1/query", summary="Run a metrics request on the data source")
    def query(request: QueryRequest, user: User) -> dict[str, Any]:
        return service.query(request, user)

    @app.post(
        "/v1/replay/{query_id}",
        summary="Replay one of your audited queries and tell whether model, SQL or data changed",
    )
    def replay(query_id: str, user: User) -> dict[str, Any]:
        return service.replay(query_id, requester=user)

    @app.post("/v1/sql", summary="Show the SQL the engine generates, without running it")
    def sql(request: QueryRequest, user: User) -> dict[str, Any]:
        compiled = service.compile(request, user)
        return {
            "sql": compiled.sql,
            "dialect": compiled.dialect,
            "data_source": compiled.data_source,
        }

    return app
