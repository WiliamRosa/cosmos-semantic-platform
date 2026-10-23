"""Model SQL expressions: ``{member}`` references, ``$user.<field>`` and column qualification."""

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
    """An expression whose references were replaced by placeholders, parsed by SQLGlot."""

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
        raise error(f"invalid expression `{text}`: {exc}") from exc
    if isinstance(tree, exp.Query):
        raise error(f"invalid expression `{text}`: queries are not allowed")
    return Template(text=text, tree=portable(tree), refs=refs, user_fields=users)


def portable(tree: exp.Expression) -> exp.Expression:
    """``date ± INTERVAL 'n' UNIT`` becomes a date addition, which SQLGlot translates to every
    dialect (SQL Server has no INTERVAL)."""

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
    """Filters sent by callers may only reach data through ``{member}`` references.

    Without this rule, a subquery or a bare column in a filter could read data that row
    filters hide.
    """
    for node in template.tree.walk():
        if isinstance(node, _FORBIDDEN_IN_REQUESTS):
            raise QueryError(
                f"filter not allowed `{template.text}`: use only {{member}} references"
            )
        if isinstance(node, exp.Column) and not node.name.startswith(_REF_PREFIX):
            raise QueryError(
                f"filter not allowed `{template.text}`: column `{node.sql()}` must be "
                "a {member} reference"
            )
    if template.user_fields:
        raise QueryError(
            f"filter not allowed `{template.text}`: `$user` is only valid in the model"
        )


def substitute(
    template: Template,
    resolve_ref: Callable[[str], exp.Expression],
    resolve_user: Callable[[str], exp.Expression] | None = None,
    *,
    resolve_condition: Callable[[str], exp.Expression] | None = None,
) -> exp.Expression:
    """Replaces placeholders with the resolved expressions.

    ``resolve_condition`` resolves references used as conditions (alone, or under AND, OR and
    NOT). In SQL Server a boolean is not a value: as a condition it is a predicate, as a value
    it becomes a CASE. In the other dialects both forms are the same.
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
    """Parses a model column expression and prefixes bare columns with the model alias."""
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except ParseError as exc:
        raise ModelError(f"invalid expression `{text}`: {exc}") from exc
    if isinstance(tree, exp.Query):
        raise ModelError(f"invalid expression `{text}`: queries are not allowed")

    def add_table(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            return exp.column(node.this.copy(), table=exp.to_identifier(alias))
        return node

    return portable(tree).transform(add_table)


def table_ref(name: str, dialect: str, schema_map: dict[str, str] | None = None) -> exp.Table:
    """The model's table on the data source: applies the source's ``schema_map`` and, in SQL
    Server, brackets every part (``public`` is a reserved word in T-SQL)."""
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
    """Applies ``table_ref`` to the physical tables of a SQL statement (CTEs are untouched)."""
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
        raise QueryError("list values are not supported in $user")
    return exp.Literal.string(str(value))
