"""Servidor MCP para agentes de IA.

Cada chamada de ferramenta valida o token da própria mensagem: o agente consulta em nome do
usuário que perguntou (fluxo on-behalf-of), nunca com uma identidade de serviço. No transporte
stdio (uso local), o contexto vem de ``COSMOS_API_KEY`` (chave de desenvolvimento).
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
Camada semântica Cosmos. Antes de consultar:
1. Use `search_concepts` com os termos da pergunta: sinônimos levam ao conceito e ao membro
   (métrica, dimensão ou regra) que o define, com um exemplo de uso.
2. Use `list_members` para ver métricas, dimensões e regras disponíveis.
3. Use `query` com nomes exatos. Dimensões são `modelo.dimensao`; tempo aceita `__day`,
   `__week`, `__month`, `__quarter`, `__year`. Regras de negócio do tipo predicado entram
   como filtro `{rule.nome}`; filtros comparam referências `{modelo.dimensao}` com valores.
Dados pessoais (LGPD) nunca chegam a agentes: agrupar ou filtrar por eles é negado.
Responda sempre com o número que a ferramenta devolveu e explique a definição usada
(`explain`) quando a pergunta envolver um termo de negócio."""

MAX_ROWS_FOR_AGENTS = 200


def build_mcp(service: SemanticService, authenticator: Authenticator | None) -> MCPServer:
    mcp = MCPServer(name="cosmos-semantic", instructions=INSTRUCTIONS, version="0.1.0")

    def principal(ctx: Context) -> Principal:
        headers = ctx.headers
        if headers is None:
            key = os.environ.get("COSMOS_API_KEY")
            if authenticator and key:
                return authenticator.authenticate(api_key=key, channel="mcp")
            raise AuthError("no stdio, defina COSMOS_API_KEY com uma chave de desenvolvimento")
        if authenticator is None or not authenticator.settings.enabled:
            raise AuthError("autenticação não configurada no servidor")
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

    @mcp.tool(description="Busca conceitos do glossário por termo ou sinônimo e diz como usá-los.")
    def search_concepts(text: str, ctx: Context, limit: int = 5) -> dict[str, Any]:
        return guarded(
            lambda: {
                "concepts": _check(principal(ctx), service.catalog.search_concepts(text, limit))
            }
        )()

    @mcp.tool(
        description="Explica uma métrica, regra de negócio, dimensão ou conceito: definição, dono e lógica."
    )
    def explain(name: str, ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.catalog.explain(name)))()

    @mcp.tool(description="Lista métricas, dimensões e regras de negócio disponíveis.")
    def list_members(ctx: Context) -> dict[str, Any]:
        return guarded(lambda: _check(principal(ctx), service.catalog.members()))()

    @mcp.tool(
        description=(
            "Executa um pedido de métricas com as políticas do usuário aplicadas. "
            f"Devolve no máximo {MAX_ROWS_FOR_AGENTS} linhas."
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

    @mcp.tool(description="Mostra o SQL que o motor gera para um pedido, sem executar.")
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
    """Metadados exigem uma identidade válida, mas não dependem dela."""
    return value
