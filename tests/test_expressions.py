from __future__ import annotations

import pytest
import sqlglot

from cosmos_semantic.expressions import parse_template, substitute

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
