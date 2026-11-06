"""REST API for applications (FastAPI), and the HTTP front of the MCP server.

The MCP endpoint follows the MCP authorization spec: it is an OAuth protected resource. It
publishes its metadata (RFC 9728) at ``/.well-known/oauth-protected-resource`` and answers
requests without a valid token with HTTP 401 and a ``WWW-Authenticate`` header that points to
that metadata, so clients such as Claude can discover the identity provider and sign the user in.
"""

import json
import os
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from cosmos_semantic.auth import Authenticator, AuthError, ForbiddenError
from cosmos_semantic.errors import (
    ConnectorError,
    CosmosError,
    ModelError,
    PolicyError,
    QueryError,
)
from cosmos_semantic.evidence import engine_version
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


class MCPAuthGate:
    """ASGI wrapper for the MCP endpoint: rejects requests without a valid credential at the
    HTTP level (401 or 403), before any MCP message is processed. Tools still validate the
    token of their own message, as a second layer."""

    def __init__(self, app, authenticator: Authenticator | None, metadata_url, allow_anonymous):
        self.app = app
        self.authenticator = authenticator
        self.metadata_url = metadata_url
        self.allow_anonymous = allow_anonymous

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = self.authenticator
        if auth is None or not auth.settings.enabled:
            if self.allow_anonymous:
                return await self.app(scope, receive, send)
            return await self._deny(scope, send, 401, "authentication is not configured")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
        api_key = headers.get("x-api-key")
        if not bearer and not api_key:
            return await self._deny(scope, send, 401, "send `Authorization: Bearer <token>`")
        try:
            await anyio.to_thread.run_sync(
                lambda: auth.authenticate(bearer=bearer, api_key=api_key, channel="mcp")
            )
        except ForbiddenError as exc:
            return await self._deny(scope, send, 403, str(exc), error="insufficient_scope")
        except AuthError as exc:
            return await self._deny(scope, send, 401, str(exc), error="invalid_token")
        return await self.app(scope, receive, send)

    async def _deny(self, scope, send, status: int, detail: str, error: str | None = None):
        challenge = f'Bearer resource_metadata="{self.metadata_url(scope)}"'
        if error:
            challenge += f', error="{error}"'
        body = json.dumps({"error": "AuthError", "detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", challenge.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _base_url(scope, public_url: str | None) -> str:
    if public_url:
        return public_url.rstrip("/")
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    host = headers.get("host", "localhost")
    return f"{scope.get('scheme', 'http')}://{host}"


def create_app(
    service: SemanticService,
    authenticator: Authenticator | None,
    *,
    allow_anonymous: bool = False,
    mcp_app: Any = None,
    public_url: str | None = None,
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
        version=engine_version(),
        description="Semantic layer with its own engine: metrics, business rules and glossary.",
        lifespan=lifespan,
    )

    def metadata_url(scope) -> str:
        return f"{_base_url(scope, public_url)}/.well-known/oauth-protected-resource"

    if mcp_app is not None:
        app.mount("/mcp", MCPAuthGate(mcp_app, authenticator, metadata_url, allow_anonymous))

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    def protected_resource(request: Request) -> dict[str, Any]:
        """OAuth protected resource metadata (RFC 9728) for the MCP endpoint."""
        issuer = authenticator.settings.issuer if authenticator else None
        return {
            "resource": f"{_base_url(request.scope, public_url)}/mcp",
            "authorization_servers": [issuer] if issuer else [],
            "bearer_methods_supported": ["header"],
            "resource_name": "Cosmos Semantic",
        }

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

    @app.get("/v1/glossary", summary="The business glossary as SKOS/RDF, for data catalogs")
    def glossary(_user: User, format: str = "turtle", base: str | None = None) -> Response:
        from cosmos_semantic.interchange.skos import FORMATS, export_glossary

        if format not in FORMATS:
            raise QueryError(f"unknown format `{format}`; use one of {', '.join(FORMATS)}")
        media = {
            "turtle": "text/turtle",
            "jsonld": "application/ld+json",
            "ntriples": "application/n-triples",
        }
        options = {"base": base} if base else {}
        text = export_glossary(service.engine.graph, format, **options)
        return Response(text, media_type=f"{media[format]}; charset=utf-8")

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

    _add_console(app, service, User)
    return app


CONSOLE_DIST = Path(__file__).parent / "console_dist"


def _console_files(directory: Path = CONSOLE_DIST):
    """Static files for the console. Next.js prefetches route segments as
    ``__next.<route>.<segment>.txt`` while its static export writes them as
    ``__next.<route>/<segment>.txt``; the dotted name is mapped to the file."""
    from fastapi.staticfiles import StaticFiles

    class ConsoleFiles(StaticFiles):
        def lookup_path(self, path: str):
            found = super().lookup_path(path)
            # Starlette hands over an OS path (backslashes on Windows).
            head, _, name = path.replace("\\", "/").rpartition("/")
            if found[1] is None and name.startswith("__next.") and name.endswith(".txt"):
                parts = name[: -len(".txt")].split(".")
                if len(parts) > 2:
                    nested = "/".join([f"{parts[0]}.{parts[1]}", *parts[2:]]) + ".txt"
                    return super().lookup_path(f"{head}/{nested}" if head else nested)
            return found

    return ConsoleFiles(directory=directory, html=True)


def _add_console(app: FastAPI, service: SemanticService, User: Any) -> None:
    """Read-only console views (ADR 0012) under /v1/console, and the built web app at /console."""
    from cosmos_semantic.console import Console

    console = Console(service)

    @app.get("/v1/console/overview", summary="Console: model, governance, health and usage")
    def console_overview(user: User) -> dict[str, Any]:
        return console.overview(user)

    @app.get("/v1/console/processes", summary="Console: business processes and their health")
    def console_processes(_user: User) -> list[dict[str, Any]]:
        return console.processes()

    @app.get("/v1/console/graph", summary="Console: knowledge graph with health per node")
    def console_graph(_user: User, focus: str | None = None) -> dict[str, Any]:
        return console.graph_view(focus)

    @app.get("/v1/console/catalog", summary="Console: metrics, rules and concepts with status")
    def console_catalog(_user: User) -> dict[str, Any]:
        return console.catalog()

    @app.get("/v1/console/members/{member_id:path}", summary="Console: one member in detail")
    def console_member(member_id: str, user: User) -> dict[str, Any]:
        return console.member(member_id, user)

    @app.get("/v1/console/answers", summary="Console: recent audited answers")
    def console_answers(user: User, limit: int = 50, metric: str | None = None) -> dict[str, Any]:
        return console.answers(user, limit=min(limit, 500), metric=metric)

    @app.get("/v1/console/answers/{query_id}", summary="Console: one audited answer")
    def console_answer(query_id: str, user: User) -> dict[str, Any]:
        return console.answer(query_id, user)

    @app.get("/v1/console/usage", summary="Console: usage and estimated cost (admins)")
    def console_usage(user: User) -> dict[str, Any]:
        return console.usage(user)

    @app.get("/v1/console/runs", summary="Console: check runs (rule tests, reconciliations)")
    def console_runs(_user: User, limit: int = 30) -> dict[str, Any]:
        return console.runs(limit=min(limit, 500))

    if (CONSOLE_DIST / "index.html").exists():
        app.mount("/console", _console_files(), name="console")
    else:

        @app.get("/console", include_in_schema=False)
        def console_missing() -> Response:
            return Response(
                "<p>The console is not built in this installation. From a source checkout run "
                "<code>npm ci &amp;&amp; npm run build</code> in <code>console/</code>.</p>",
                media_type="text/html",
            )

    origins = [o.strip() for o in os.environ.get("COSMOS_CORS_ORIGINS", "").split(",") if o]
    if origins:  # for the console's dev server (`npm run dev`) on another port
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "X-API-Key", "Content-Type"],
        )
