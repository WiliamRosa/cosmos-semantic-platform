"""Expressões SQL do modelo: referências ``{membro}``, ``$user.<campo>`` e qualificação de colunas."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from cosmos_semantic.errors import ModelError, QueryError

REF_RE = re.compile(r"\{\s*([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)\s*\}")
USER_RE = re.compile(r"\$user\.([a-z_][a-z0-9_]*)")
_REF_PREFIX = "__cosmos_ref_"
_USER_PREFIX = "__cosmos_user_"

_FORBIDDEN_IN_REQUESTS = (exp.Query, exp.Subquery, exp.Table, exp.Star, exp.Command)


@dataclass
class Template:
    """Expressão com referências substituídas por marcadores, já analisada pelo SQLGlot."""

    text: str
    tree: exp.Expression
    refs: list[str]
    user_fields: list[str]


def parse_template(text: str, dialect: str, *, error: type[Exception] = ModelError) -> Template:
    refs: list[str] = []
    users: list[str] = []

    def ref_sub(match: re.Match[str]) -> str:
        refs.append(match.group(1))
        return f"{_REF_PREFIX}{len(refs) - 1}"

    def user_sub(match: re.Match[str]) -> str:
        users.append(match.group(1))
        return f"{_USER_PREFIX}{len(users) - 1}"

    masked = USER_RE.sub(user_sub, REF_RE.sub(ref_sub, text))
    try:
        tree = sqlglot.parse_one(masked, read=dialect)
    except ParseError as exc:
        raise error(f"expressão inválida `{text}`: {exc}") from exc
    if isinstance(tree, exp.Query):
        raise error(f"expressão inválida `{text}`: consultas não são permitidas")
    return Template(text=text, tree=tree, refs=refs, user_fields=users)


def check_request_expression(template: Template) -> None:
    """Filtros vindos de quem consulta só podem acessar dados por referências ``{membro}``.

    Sem essa regra, uma subconsulta ou coluna solta no filtro poderia ler dados que os
    filtros por linha escondem.
    """
    for node in template.tree.walk():
        if isinstance(node, _FORBIDDEN_IN_REQUESTS):
            raise QueryError(
                f"filtro não permitido `{template.text}`: use apenas referências {{membro}}"
            )
        if isinstance(node, exp.Column) and not node.name.startswith(_REF_PREFIX):
            raise QueryError(
                f"filtro não permitido `{template.text}`: a coluna `{node.sql()}` precisa ser "
                "uma referência {membro}"
            )
    if template.user_fields:
        raise QueryError(f"filtro não permitido `{template.text}`: `$user` só vale no modelo")


def substitute(
    template: Template,
    resolve_ref: Callable[[str], exp.Expression],
    resolve_user: Callable[[str], exp.Expression] | None = None,
) -> exp.Expression:
    refs = [resolve_ref(r) for r in template.refs]
    users = [resolve_user(u) for u in template.user_fields] if resolve_user else []

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            name = node.name
            if name.startswith(_REF_PREFIX):
                return refs[int(name[len(_REF_PREFIX) :])].copy()
            if name.startswith(_USER_PREFIX):
                return users[int(name[len(_USER_PREFIX) :])].copy()
        return node

    return template.tree.copy().transform(swap)


def qualify(text: str, alias: str, dialect: str) -> exp.Expression:
    """Analisa uma expressão de coluna do modelo e prefixa colunas soltas com o alias do modelo."""
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except ParseError as exc:
        raise ModelError(f"expressão inválida `{text}`: {exc}") from exc
    if isinstance(tree, exp.Query):
        raise ModelError(f"expressão inválida `{text}`: consultas não são permitidas")

    def add_table(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            return exp.column(node.this.copy(), table=exp.to_identifier(alias))
        return node

    return tree.transform(add_table)


def literal(value: object) -> exp.Expression:
    if value is None:
        return exp.null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    if isinstance(value, list | tuple):
        raise QueryError("valores em lista não são suportados em $user")
    return exp.Literal.string(str(value))
