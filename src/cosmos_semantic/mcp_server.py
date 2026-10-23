"""MCP server for AI agents.

Every tool call validates the token of its own message: the agent queries on behalf of the user
who asked (on-behalf-of flow), never with a service identity. On the stdio transport (local use)
the context comes from ``COSMOS_API_KEY`` (a development key).
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from cosmos_semantic.auth import Authenticator, AuthError
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import SemanticService

INSTRUCTIONS = """\
Cosmos semantic layer. Before querying:
1. Call `search_concepts` with the terms of the question: synonyms (in any language the
   glossary covers) lead to the concept and to the member (metric, dimension or rule) that
   defines it, with a usage example.
2. Call `list_members` to see the available metrics, dimensions and rules.
3. Call `query` with exact names. Dimensions are `model.dimension`; time accepts `__day`,
   `__week`, `__month`, `__quarter`, `__year`. Predicate business rules are used as the
   filter `{rule.name}`; filters compare `{model.dimension}` references with values.
Personal data (LGPD) never reaches agents: grouping or filtering by it is denied.
Always answer with the number the tool returned and explain the definition used (`explain`)
when the question involves a business term."""

MAX_ROWS_FOR_AGENTS = 200


def build_mcp(service: SemanticService, authenticator: Authenticator | None) -> MCPServer:
    mcp = MCPServer(name="cosmos-semantic", instructions=INSTRUCTIONS, version="0.1.0")

    def principal(ctx: Context) -> Principal:
        headers = ctx.headers
        if headers is None:
            key = os.environ.get("COSMOS_API_KEY")
            if authenticator and key:
                return authenticator.authenticate(api_key=key, channel="mcp")
            raise AuthError("on stdio, set COSMOS_API_KEY to a development key")
        if authenticator is None or not authenticator.settings.enabled:
            raise AuthError("authentication is not configured on the server")
        lower = {k.lower(): v for k, v in headers.items()}
        auth = lower.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return authenticator.authenticate(
            bearer=bearer, api_key=lower.get("x-api-key"), channel="mcp"
        )

    def guarded(fn):
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except CosmosError as exc:
                return {"error": type(exc).__name__, "detail": str(exc)}

        return run

    @mcp.tool(description="Search glossary concepts by term or synonym, and tell how to use them.")
    def search_concepts(text: str, ctx: Context, limit: int = 5) -> dict[str, Any]:
        return guarded(
            lambda: {
                "concepts": _check(principal(ctx), service.catalog.search_concepts(text, limit))
            }
        )()

    @mcp.tool(
        description="Explain a metric, business rule, dimension or concept: definition, owner and logic."
    )
    def explain(name: str, ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.catalog.explain(name)))()

    @mcp.tool(description="List the available metrics, dimensions and business rules.")
    def list_members(ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.catalog.members()))()

    @mcp.tool(
        description=(
            "Run a metrics request with the user's policies applied. "
            f"Returns at most {MAX_ROWS_FOR_AGENTS} rows."
        )
    )
    def query(
        metrics: list[str],
        ctx: Context,
        dimensions: list[str] | None = None,
        filters: list[str] | None = None,
        order_by: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        def run():
            request = QueryRequest(
                metrics=metrics,
                dimensions=dimensions or [],
                filters=filters or [],
                order_by=order_by or [],
                limit=limit,
            )
            return service.query(request, principal(ctx), max_rows=MAX_ROWS_FOR_AGENTS)

        return guarded(run)()

    @mcp.tool(description="Show the SQL the engine generates for a request, without running it.")
    def show_sql(
        metrics: list[str],
        ctx: Context,
        dimensions: list[str] | None = None,
        filters: list[str] | None = None,
    ) -> dict[str, Any]:
        def run():
            request = QueryRequest(
                metrics=metrics, dimensions=dimensions or [], filters=filters or []
            )
            compiled = service.compile(request, principal(ctx))
            return {"sql": compiled.sql, "dialect": compiled.dialect}

        return guarded(run)()

    return mcp


def _check(_principal: Principal, value: Any) -> Any:
    """Metadata requires a valid identity, but does not depend on it."""
    return value
