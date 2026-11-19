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
from cosmos_semantic.errors import CosmosError, QueryError
from cosmos_semantic.evidence import engine_version
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.service import MAX_DIMENSION_VALUES, VALUES_FETCH_CEILING, SemanticService

INSTRUCTIONS = """\
Cosmos semantic layer. Before querying:
1. Call `search_concepts` with the terms of the question: synonyms (in any language the
   glossary covers) lead to the concept and to the member (metric, dimension or rule) that
   defines it, with a usage example.
2. Call `list_members` to see the available metrics, dimensions and rules.
3. Call `query` with exact names. Dimensions are `model.dimension`; time accepts `__day`,
   `__week`, `__month`, `__quarter`, `__year`. Predicate business rules are used as the
   filter `{rule.name}`; filters compare `{model.dimension}` references with values. Before
   filtering by a value, call `dimension_values` to get the exact values (such as `BR` or
   `Brazil`): a guessed value returns zero rows that look real. For a time dimension it
   returns the first and last day with data, so check it before a relative date range.
Personal data (LGPD) never reaches agents: grouping or filtering by it is denied.
Always answer with the number the tool returned. Every `query` result has an `evidence`
envelope: cite the metric formula, the business rules applied (with owner and test status),
the reference date (`as_of`) and the `query_id`, so the answer can be audited and replayed.
Call `replay` with a `query_id` the user ran to tell whether the model, the SQL or the data
changed since then. Call `impact` with `kind:name` (such as `rule:active_customer`) to tell
what a change to a member affects, by owner, source systems and processes."""

MAX_ROWS_FOR_AGENTS = 200


def build_mcp(service: SemanticService, authenticator: Authenticator | None) -> MCPServer:
    mcp = MCPServer(name="cosmos-semantic", instructions=INSTRUCTIONS, version=engine_version())

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
        description=(
            "Explain a metric, business rule, dimension, concept, team, system or process: "
            "definition, owner, source systems, processes and logic. Metrics, measures, rules "
            "and processes also carry `health`: whether the number is checked (`healthy`, "
            "`attention`, `failing` or `unchecked`), with the reasons (rule tests, last "
            "reconciliation) and `last_reconciled_at`. Mention it when you quote a number that "
            "is not healthy or was last reconciled long ago."
        )
    )
    def explain(name: str, ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.explain(name)))()

    @mcp.tool(description="List the available metrics, dimensions and business rules.")
    def list_members(ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.catalog.members()))()

    @mcp.tool(
        description=(
            "Run a metrics request with the user's policies applied. `metric_filters` keep "
            "only the rows where a requested metric meets a condition, after aggregation, such "
            "as `{revenue} > 1000`: a metric compared with a number (`=`, `<>`, `<`, `<=`, `>`, "
            "`>=`) or `IS NULL` / `IS NOT NULL`, joined by AND; `limit` applies after them. "
            f"Returns at most {MAX_ROWS_FOR_AGENTS} rows."
        )
    )
    def query(
        metrics: list[str],
        ctx: Context,
        dimensions: list[str] | None = None,
        filters: list[str] | None = None,
        metric_filters: list[str] | None = None,
        order_by: list[str] | None = None,
        limit: int | None = None,
        as_of: str | None = None,
        rules_as_of: str | None = None,
    ) -> dict[str, Any]:
        def run():
            request = QueryRequest(
                metrics=metrics,
                dimensions=dimensions or [],
                filters=filters or [],
                metric_filters=metric_filters or [],
                order_by=order_by or [],
                limit=limit,
                as_of=as_of,
                rules_as_of=rules_as_of,
            )
            return service.query(request, principal(ctx), max_rows=MAX_ROWS_FOR_AGENTS)

        return guarded(run)()

    @mcp.tool(
        description=(
            "Distinct values of a categorical or boolean dimension (`model.dimension`), with the "
            "number of rows of its model carrying each one, most frequent first, after the "
            "user's row filters. Use it to filter by an exact value. Personal data is refused. "
            f"Returns at most {MAX_DIMENSION_VALUES} values; ties in count are ordered by value. "
            f"Past {VALUES_FETCH_CEILING} distinct values the list is marked truncated, and ties "
            "at that cut-off may differ between databases. For a time dimension it returns its "
            "date range instead: `min` and `max` (the first and last day holding rows, ISO dates "
            "in the model's time zone) and `distinct_days`; null, null and 0 when no row is "
            "visible. Call it before answering 'last month' or 'since launch'."
        )
    )
    def dimension_values(name: str, ctx: Context, limit: int | None = None) -> dict[str, Any]:
        return guarded(lambda: service.dimension_values(name, principal(ctx), limit=limit))()

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

    @mcp.tool(
        description=(
            "Replay one of the user's audited queries, by `query_id`, with its original request, "
            "user context and as_of, and tell whether the model, the SQL or the data changed."
        )
    )
    def replay(query_id: str, ctx: Context) -> dict[str, Any]:
        def run():
            requester = principal(ctx)
            # Check ownership before anything else, and answer a query of another user exactly
            # like an unknown id, so an agent cannot learn which ids exist or how they ended.
            record = service.find_audit(query_id)
            owner = (record.get("principal") or {}).get("subject")
            if requester.subject is None or owner != requester.subject:
                raise QueryError(f"query `{query_id}` is not in the audit log")
            return service.replay(query_id, requester=requester)

        return guarded(run)()

    @mcp.tool(
        description=(
            "What a change to a metric, measure, rule, dimension, model, concept, system or "
            "process affects, as `kind:name` (for example `rule:active_customer`): affected "
            "members grouped by owner, source systems and processes. Who queried them (from the "
            "audit log) is included only for admins."
        )
    )
    def impact(name: str, ctx: Context) -> dict[str, Any]:
        return guarded(lambda: service.impact(name, requester=principal(ctx)))()

    return mcp


def _check(_principal: Principal, value: Any) -> Any:
    """Metadata requires a valid identity, but does not depend on it."""
    return value
