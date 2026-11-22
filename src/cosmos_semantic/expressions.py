"""Model SQL expressions: ``{member}`` references, ``$user.<field>`` and column qualification."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
        if dialect != "bigquery":
            raise error(f"invalid expression `{text}`: {exc}") from exc
        # Model expressions are standard SQL, where a quote inside a string is doubled
        # ('O''Brien'); BigQuery escapes it with a backslash and reads '' as two literals.
        try:
            tree = sqlglot.parse_one(masked, read="postgres")
        except ParseError:
            raise error(f"invalid expression `{text}`: {exc}") from exc
    if isinstance(tree, exp.Query):
        raise error(f"invalid expression `{text}`: queries are not allowed")
    return Template(text=text, tree=portable(tree), refs=refs, user_fields=users)


def ref_index(node: exp.Expression) -> int | None:
    """The index in ``Template.refs`` of a ``{member}`` placeholder, None for any other node."""
    if isinstance(node, exp.Column) and not node.table and node.name.startswith(_REF_PREFIX):
        return int(node.name[len(_REF_PREFIX) :])
    return None


def ref_placeholder(index: int) -> exp.Column:
    """The placeholder of ``Template.refs[index]``, for a tree built from a template."""
    return exp.column(f"{_REF_PREFIX}{index}")


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
                    return _grouped(resolve_condition(ref), node)
                return _grouped(resolve_ref(ref), node)
            if name.startswith(_USER_PREFIX):
                return _grouped(users[int(name[len(_USER_PREFIX) :])].copy(), node)
        return node

    return tree.transform(swap, copy=False)


# Operators: their operands are the only place where precedence decides the result. (In SQLGlot
# AND and OR are also functions, so the check is on these classes, not on ``exp.Func``.)
_OPERATORS = (exp.Binary, exp.Unary, exp.Predicate)


def _is_operator(node: exp.Expression) -> bool:
    return isinstance(node, _OPERATORS) and not isinstance(node, exp.Paren)


def _grouped(value: exp.Expression, placeholder: exp.Expression) -> exp.Expression:
    """Parenthesizes a resolved operator expression that is an operand of another operator.

    SQLGlot prints the tree as it is and does not add parentheses for precedence, so a dimension
    ``discount > 0`` used in ``{orders.has_discount} = true`` would print as
    ``discount > 0 = TRUE``, ``{a} * 2`` with ``a = x + y`` as ``x + y * 2``, and
    ``NOT {a}`` with ``a = p OR q`` as ``NOT p OR q``.
    """
    parent = placeholder.parent
    if parent is not None and _is_operator(parent) and _is_operator(value):
        return exp.Paren(this=value)
    return value


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


# -- structured filters (DEC-50) ----------------------------------------------------------

COMPARISONS: dict[str, type[exp.Binary]] = {
    "=": exp.EQ,
    "!=": exp.NEQ,
    "<>": exp.NEQ,
    "<": exp.LT,
    "<=": exp.LTE,
    ">": exp.GT,
    ">=": exp.GTE,
}
LIST_OPS = ("in", "not in")
NULL_OPS = ("is null", "is not null")
FILTER_OPS = (*COMPARISONS, *LIST_OPS, *NULL_OPS)


def value_literal(value: object) -> exp.Expression:
    """A request value as a SQLGlot literal. The generator of each dialect writes it with that
    dialect's quoting and escaping, so no caller quotes a value itself."""
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    if isinstance(value, str):
        return exp.Literal.string(value)
    raise QueryError(f"unsupported filter value of type {type(value).__name__}")


_VALUE_PREFIX = "__cosmos_value_"


def _filter_tree(op: str, value: object, dialect: str) -> exp.Expression:
    """The operator is parsed by the dialect from a fixed skeleton (``ref op value``), so the
    tree has the shape the dialect's parser gives an expression string (PostgreSQL reads
    ``IS NOT NULL`` as one node, DuckDB as ``NOT ... IS NULL``); the values are then put in
    as literals and never parsed."""
    if op in COMPARISONS:
        values = [value]
        skeleton = f"{_REF_PREFIX}0 {'<>' if op == '!=' else op} {_VALUE_PREFIX}0"
    elif op in LIST_OPS:
        if not isinstance(value, list | tuple) or not value:
            raise QueryError(f"`{op}` takes a non-empty list of values")
        values = list(value)
        items = ", ".join(f"{_VALUE_PREFIX}{i}" for i in range(len(values)))
        skeleton = f"{_REF_PREFIX}0 {op.upper()} ({items})"
    elif op in NULL_OPS:
        values = []
        skeleton = f"{_REF_PREFIX}0 {op.upper()}"
    else:
        raise QueryError(f"unknown filter operator `{op}`; use one of {', '.join(FILTER_OPS)}")
    literals = [value_literal(v) for v in values]
    tree = sqlglot.parse_one(skeleton, read=dialect)

    def put(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.name.startswith(_VALUE_PREFIX):
            return literals[int(node.name[len(_VALUE_PREFIX) :])].copy()
        return node

    return portable(tree.transform(put))


def filter_text(dimension: str, op: str, value: object = None) -> str:
    """A structured filter written as the expression a caller would send, in standard SQL
    (quotes doubled): for the evidence, the audit views and error messages, never for the SQL."""
    ref = f"{{{dimension}}}"
    if op in NULL_OPS:
        return f"{ref} {op.upper()}"
    if op in LIST_OPS and isinstance(value, list | tuple):
        items = ", ".join(value_literal(v).sql() for v in value)
        return f"{ref} {op.upper()} ({items})"
    return f"{ref} {op} {value_literal(value).sql()}"


def describe_filter(item: Any) -> str:
    """A request filter as text: an expression string as it is; a structured filter, as a
    ``Filter`` or as the dict the audit log keeps, through ``filter_text``."""
    if isinstance(item, str):
        return item
    fields = item if isinstance(item, dict) else vars(item)
    try:
        return filter_text(str(fields["dimension"]), str(fields["op"]), fields.get("value"))
    except (KeyError, QueryError):
        return json.dumps(item, default=str) if isinstance(item, dict) else str(item)


def filter_refs(item: Any) -> list[str]:
    """The members a request filter references (an expression string or a structured one)."""
    if isinstance(item, str):
        return REF_RE.findall(item)
    if isinstance(item, dict):
        dimension = item.get("dimension")
        return [dimension] if isinstance(dimension, str) else []
    return [item.dimension]


def structured_template(dimension: str, op: str, value: object, dialect: str) -> Template:
    """The template of a structured filter ``{dimension, op, value}`` (DEC-50).

    The value never goes through a parser: SQLGlot writes it as a literal of the dialect. The
    tree is the one ``parse_template`` builds from the equivalent expression string, hence the
    same SQL and the same ``sql_hash``."""
    if not REF_RE.fullmatch(f"{{{dimension}}}"):
        raise QueryError(f"invalid dimension `{dimension}` in a filter; use `model.dimension`")
    tree = _filter_tree(op, value, dialect)
    return Template(
        text=filter_text(dimension, op, value), tree=tree, refs=[dimension], user_fields=[]
    )
