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
    return Template(text=text, tree=portable(tree), refs=refs, user_fields=users)


def portable(tree: exp.Expression) -> exp.Expression:
    """``data ± INTERVAL 'n' UNIDADE`` vira soma de datas, que o SQLGlot traduz para todos os
    dialetos (o SQL Server não tem INTERVAL)."""

    def fix(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Sub | exp.Add) and isinstance(node.expression, exp.Interval):
            interval = node.expression
            amount_text = interval.this.name if interval.this else ""
            if interval.unit is None or not amount_text.lstrip("-").isdigit():
                return node
            amount: exp.Expression = exp.Literal.number(int(amount_text))
            if isinstance(node, exp.Sub):
                amount = exp.Neg(this=amount)
            return exp.DateAdd(this=node.this, expression=amount, unit=interval.unit)
        return node

    return tree.transform(fix)


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
    *,
    resolve_condition: Callable[[str], exp.Expression] | None = None,
) -> exp.Expression:
    """Troca os marcadores pelas expressões resolvidas.

    ``resolve_condition`` resolve referências usadas como condição (sozinhas, ou sob AND, OR e
    NOT). No SQL Server um booleano não é valor: como condição ele é um predicado, como valor
    vira CASE. Nos outros dialetos as duas formas coincidem.
    """
    tree = template.tree.copy()
    users = [resolve_user(u) for u in template.user_fields] if resolve_user else []

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            name = node.name
            if name.startswith(_REF_PREFIX):
                ref = template.refs[int(name[len(_REF_PREFIX) :])]
                if resolve_condition and _in_condition(node):
                    return resolve_condition(ref)
                return resolve_ref(ref)
            if name.startswith(_USER_PREFIX):
                return users[int(name[len(_USER_PREFIX) :])].copy()
        return node

    return tree.transform(swap, copy=False)


def _in_condition(node: exp.Expression) -> bool:
    parent = node.parent
    while isinstance(parent, exp.Paren):
        node, parent = parent, parent.parent
    return parent is None or isinstance(parent, exp.And | exp.Or | exp.Not | exp.Where | exp.If)


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

    return portable(tree).transform(add_table)


def table_ref(name: str, dialect: str, schema_map: dict[str, str] | None = None) -> exp.Table:
    """Tabela do modelo na origem: aplica o ``schema_map`` da origem e, no SQL Server, põe
    colchetes em cada parte (``public`` é palavra reservada no T-SQL)."""
    table = exp.to_table(name)
    if schema_map and table.db and table.db in schema_map:
        table.set("db", exp.to_identifier(schema_map[table.db]))
    if dialect == "tsql":
        for key in ("catalog", "db", "this"):
            part = table.args.get(key)
            if isinstance(part, exp.Identifier):
                part.set("quoted", True)
    return table


def remap_sql(sql: str, dialect: str, schema_map: dict[str, str] | None) -> str:
    """Aplica ``table_ref`` às tabelas físicas de um SQL (sem tocar nas CTEs)."""
    if not schema_map and dialect != "tsql":
        return sql
    tree = sqlglot.parse_one(sql, read=dialect)
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}

    def fix(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and (node.db or node.name not in ctes):
            alias = node.args.get("alias")
            mapped = table_ref(".".join(p.name for p in node.parts), dialect, schema_map)
            if alias:
                mapped.set("alias", alias.copy())
            return mapped
        return node

    return tree.transform(fix).sql(dialect=dialect)


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
