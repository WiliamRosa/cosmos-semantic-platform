from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from cosmos_semantic.errors import ModelError, QueryError
from cosmos_semantic.expressions import parse_template, qualify, structured_template, substitute
from cosmos_semantic.planner import Planner, Principal

RESOLVED = {"a": "x + y", "flag": "discount > 0", "either": "p OR q", "col": "amount"}


def _render(text: str) -> str:
    template = parse_template(text, "duckdb")
    tree = substitute(template, lambda ref: sqlglot.parse_one(RESOLVED[ref], read="duckdb"))
    return tree.sql(dialect="duckdb")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A resolved expression inside an operator keeps its own precedence.
        ("{a} * 2", "(x + y) * 2"),
        ("{flag} = TRUE", "(discount > 0) = TRUE"),
        ("{either} AND r", "(p OR q) AND r"),
        ("NOT {either}", "NOT (p OR q)"),
        # Alone, already in parentheses, or a single column: printed as is.
        ("{flag}", "discount > 0"),
        ("({flag})", "(discount > 0)"),
        ("{col} * 2", "amount * 2"),
        ("SUM({a})", "SUM(x + y)"),
    ],
)
def test_substitute_keeps_precedence(text: str, expected: str) -> None:
    assert _render(text) == expected


# -- Expressions are standard SQL on every dialect (DEC-53) --------------------------------
# Read in the source's dialect, MySQL, MariaDB, Databricks and BigQuery took "status" for a
# string (the condition was always false), Databricks and BigQuery wrote 'O''Brien' as
# CONCAT('O', 'Brien'), and BigQuery dropped the backslash of LIKE 'c\_nceled'. Each check
# reads the generated SQL back with the dialect's own parser: what the database will see.
DIALECTS = [
    "duckdb",
    "postgres",
    "tsql",
    "mysql",
    "databricks",
    "snowflake",
    "oracle",
    "bigquery",
    "trino",
]


def _written(text: str, dialect: str, *, error: type[Exception] = ModelError) -> exp.Expression:
    """The expression as the dialect writes it, read back by the dialect's own parser."""
    tree = parse_template(text, dialect, error=error).tree
    return sqlglot.parse_one(tree.sql(dialect=dialect), read=dialect)


def _strings(tree: exp.Expression) -> list[str]:
    return [lit.this for lit in tree.find_all(exp.Literal) if lit.is_string]


def _columns(tree: exp.Expression) -> list[tuple[str, str]]:
    return [(c.table, c.name) for c in tree.find_all(exp.Column)]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_double_quotes_delimit_an_identifier(dialect):
    back = _written("\"status\" = 'paid'", dialect)
    assert _columns(back) == [("", "status")]
    assert _strings(back) == ["paid"]
    column = qualify("CASE WHEN \"status\" = 'paid' THEN 1 END", "orders", dialect)
    back = sqlglot.parse_one(column.sql(dialect=dialect), read=dialect)
    assert _columns(back) == [("orders", "status")]
    assert _strings(back) == ["paid"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_doubled_quote_is_one_quote_in_one_literal(dialect):
    for text in ("{customers.segment} = 'O''Brien'", "{customers.segment} IN ('O''Brien', 'x')"):
        back = _written(text, dialect, error=QueryError)
        assert "O'Brien" in _strings(back)
        assert not list(back.find_all(exp.Concat, exp.DPipe))
    column = qualify("CASE WHEN x THEN 'O''Brien' END", "c", dialect)
    back = sqlglot.parse_one(column.sql(dialect=dialect), read=dialect)
    assert _strings(back) == ["O'Brien"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_backslash_in_a_like_pattern_is_kept(dialect):
    back = _written("{orders.status} NOT LIKE 'c\\_nceled'", dialect, error=QueryError)
    assert _strings(back) == ["c\\_nceled"]
    back = _written("{orders.status} LIKE 'a\\_b%' OR {orders.status} = 'back\\'", dialect)
    assert _strings(back) == ["a\\_b%", "back\\"]


@pytest.mark.parametrize(
    ("dialect", "text", "written"),
    [
        ("mysql", "\"status\" = 'paid'", "`status` = 'paid'"),
        ("databricks", "\"status\" = 'paid'", "`status` = 'paid'"),
        ("bigquery", "\"status\" = 'paid'", "`status` = 'paid'"),
        ("tsql", "\"status\" = 'paid'", "[status] = 'paid'"),
        ("duckdb", "\"status\" = 'paid'", "\"status\" = 'paid'"),
        ("databricks", "x = 'O''Brien'", "x = 'O\\'Brien'"),
        ("bigquery", "x = 'O''Brien'", "x = 'O\\'Brien'"),
        ("mysql", "x = 'O''Brien'", "x = 'O''Brien'"),
        ("bigquery", "x LIKE 'a\\_b%'", "x LIKE 'a\\\\_b%'"),
        ("databricks", "x LIKE 'a\\_b%'", "x LIKE 'a\\\\_b%'"),
        ("mysql", "x LIKE 'a\\_b%'", "x LIKE 'a\\\\_b%'"),
        ("postgres", "x LIKE 'a\\_b%'", "x LIKE 'a\\_b%'"),
    ],
)
def test_the_expression_is_written_for_the_dialect(dialect, text, written):
    assert parse_template(text, dialect).tree.sql(dialect=dialect) == written


@pytest.mark.parametrize("dialect", DIALECTS)
def test_structured_and_standard_string_filters_give_the_same_tree(dialect):
    """A structured filter (DEC-50) and its expression string, written in standard SQL, are
    the same tree on every dialect, hence the same SQL and `sql_hash`."""
    for op, value, text in [
        ("=", "O'Brien", "{customers.segment} = 'O''Brien'"),
        ("not in", ["a\\'b", "x"], "{customers.segment} NOT IN ('a\\''b', 'x')"),
        ("is not null", None, "{customers.segment} IS NOT NULL"),
    ]:
        structured = structured_template("customers.segment", op, value, dialect)
        string = parse_template(text, dialect, error=QueryError)
        assert structured.tree.sql(dialect=dialect) == string.tree.sql(dialect=dialect)
        assert structured.text == text


@pytest.mark.parametrize("dialect", DIALECTS)
def test_dimension_values_read_string_filters_as_standard_sql(graph, dialect):
    """The string filters `dimension_values` takes (service.py) go through the same parser:
    the values and the date range of a dimension get one literal `O'Brien` and the backslash
    of the LIKE pattern, on every dialect."""
    filters = ["{customers.segment} <> 'O''Brien'", "{customers.state} NOT LIKE 'N\\_'"]
    principal = Principal(user={"region": "West"})
    planner = Planner(graph, dialect)
    for compiled in (
        planner.plan_values("customers.segment", principal, limit=10, filters=filters),
        planner.plan_time_range("customers.created_at", principal, filters=filters),
    ):
        back = sqlglot.parse_one(compiled.sql, read=dialect)
        assert {"O'Brien", "N\\_"} <= set(_strings(back))
        assert not list(back.find_all(exp.Concat, exp.DPipe))


def test_dialect_syntax_that_is_not_standard_sql_is_read_in_the_dialect():
    """A model written in its source's own dialect keeps working: T-SQL brackets (an array in
    PostgreSQL), MySQL backticks, a quote escaped with a backslash."""
    assert _columns(_written("[order status] = 'paid'", "tsql")) == [("", "order status")]
    assert _columns(_written("`status` = 'paid'", "mysql")) == [("", "status")]
    assert _strings(_written("x = 'O\\'Brien'", "databricks")) == ["O'Brien"]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("text", ["{a} = 'unterminated", "{a} = ", "{a} = (1", "SELECT 1"])
def test_invalid_expressions_are_the_callers_error(dialect, text):
    """Any SQLGlot error, the tokenizer's included, is the caller's error type, never an
    unhandled exception (an HTTP 500)."""
    with pytest.raises(QueryError, match="invalid expression"):
        parse_template(text, dialect, error=QueryError)
    with pytest.raises(ModelError, match="invalid expression"):
        qualify(text.replace("{a}", "x"), "m", dialect)
