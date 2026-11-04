"""Dialect fixes for SQL that SQLGlot's generic translation does not cover (ADR 0011).

The planner builds one dialect-neutral SQLGlot tree; ``adapt`` rewrites the few constructs a
source cannot run before the tree is printed in that source's dialect:

- **Time truncation:** MySQL/MariaDB have no ``DATE_TRUNC`` (SQLGlot's ``DATE_ADD`` from year
  0 is not Monday-based for weeks); Oracle uses ``TRUNC`` with format models (``'IW'`` is the
  ISO week, starting on Monday). Every grain returns a timestamp, as on the other sources.
- **Oracle:** no ``IS [NOT] DISTINCT FROM`` (``DECODE`` treats two NULLs as equal), no
  ``DATE_ADD`` (interval functions instead) and no ``GROUP BY`` by position unless a session
  parameter is set (the positions are expanded instead).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

_TRUNC_TEMPLATES = {
    "mysql": {
        "HOUR": "CAST(DATE_ADD(DATE(x), INTERVAL HOUR(x) HOUR) AS DATETIME)",
        "DAY": "CAST(DATE(x) AS DATETIME)",
        "WEEK": "CAST(DATE_SUB(DATE(x), INTERVAL WEEKDAY(x) DAY) AS DATETIME)",
        "MONTH": "CAST(DATE_SUB(DATE(x), INTERVAL DAYOFMONTH(x) - 1 DAY) AS DATETIME)",
        "QUARTER": (
            "CAST(DATE_ADD(MAKEDATE(YEAR(x), 1), INTERVAL (QUARTER(x) - 1) * 3 MONTH) AS DATETIME)"
        ),
        "YEAR": "CAST(MAKEDATE(YEAR(x), 1) AS DATETIME)",
    },
    "oracle": {
        "HOUR": "CAST(TRUNC(x, 'HH24') AS TIMESTAMP)",
        "DAY": "CAST(TRUNC(x, 'DD') AS TIMESTAMP)",
        "WEEK": "CAST(TRUNC(x, 'IW') AS TIMESTAMP)",
        "MONTH": "CAST(TRUNC(x, 'MM') AS TIMESTAMP)",
        "QUARTER": "CAST(TRUNC(x, 'Q') AS TIMESTAMP)",
        "YEAR": "CAST(TRUNC(x, 'YYYY') AS TIMESTAMP)",
    },
}

# Oracle interval functions per unit: (function, unit name, multiplier).
_ORACLE_INTERVALS = {
    "SECOND": ("NUMTODSINTERVAL", "SECOND", 1),
    "MINUTE": ("NUMTODSINTERVAL", "MINUTE", 1),
    "HOUR": ("NUMTODSINTERVAL", "HOUR", 1),
    "DAY": ("NUMTODSINTERVAL", "DAY", 1),
    "WEEK": ("NUMTODSINTERVAL", "DAY", 7),
    "MONTH": ("NUMTOYMINTERVAL", "MONTH", 1),
    "QUARTER": ("NUMTOYMINTERVAL", "MONTH", 3),
    "YEAR": ("NUMTOYMINTERVAL", "YEAR", 1),
}


def trunc(value: exp.Expression, unit: str, dialect: str) -> exp.Expression:
    """Truncates ``value`` to ``unit`` (HOUR, DAY, WEEK, MONTH, QUARTER, YEAR) in ``dialect``."""
    unit = unit.upper()
    if dialect in _TRUNC_TEMPLATES:
        template = sqlglot.parse_one(_TRUNC_TEMPLATES[dialect][unit], read=dialect)
        return template.transform(
            lambda n: value.copy() if isinstance(n, exp.Column) and n.name == "x" else n
        )
    if unit == "WEEK" and dialect == "tsql":
        unit = "ISO_WEEK"  # SQL Server weeks depend on DATEFIRST; ISO weeks start on Monday
    return exp.TimestampTrunc(this=value, unit=exp.Var(this=unit))


def adapt(tree: exp.Expression, dialect: str) -> exp.Expression:
    """Rewrites what ``dialect`` cannot run; a no-op for the other dialects."""
    if dialect not in _TRUNC_TEMPLATES:
        return tree
    if dialect == "oracle":
        from cosmos_semantic.expressions import portable

        tree = portable(tree)  # date ± INTERVAL -> DateAdd, rewritten below

    def sub(node: exp.Expression) -> exp.Expression:
        # ``transform`` does not descend into a replacement; adapt its parts explicitly.
        return adapt(node.copy(), dialect)

    def fix(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, exp.TimestampTrunc | exp.DateTrunc)
            and node.text("unit").upper() in _TRUNC_TEMPLATES[dialect]
        ):
            return trunc(sub(node.this), node.text("unit"), dialect)
        if dialect != "oracle":
            return node
        if isinstance(node, exp.CurrentDate):
            # Oracle's CURRENT_DATE is a DATE with the time of day; the others have no time.
            return exp.Anonymous(this="TRUNC", expressions=[node.copy()])
        if isinstance(node, exp.NullSafeEQ | exp.NullSafeNEQ):
            same = exp.Anonymous(
                this="DECODE",
                expressions=[
                    sub(node.this),
                    sub(node.expression),
                    exp.Literal.number(1),
                    exp.Literal.number(0),
                ],
            )
            flag = 1 if isinstance(node, exp.NullSafeEQ) else 0
            return exp.EQ(this=same, expression=exp.Literal.number(flag))
        if isinstance(node, exp.DateAdd | exp.TsOrDsAdd) and node.text("unit"):
            function, unit, factor = _ORACLE_INTERVALS[node.text("unit").upper()]
            amount = sub(node.expression)
            if factor != 1:
                amount = exp.Mul(this=exp.Paren(this=amount), expression=exp.Literal.number(factor))
            interval = exp.Anonymous(this=function, expressions=[amount, exp.Literal.string(unit)])
            return exp.Add(this=sub(node.this), expression=interval)
        if isinstance(node, exp.Select) and node.args.get("group"):
            _expand_group_positions(node)
        return node

    return tree.transform(fix)


def _expand_group_positions(select: exp.Select) -> None:
    """``GROUP BY 1, 2`` -> the select expressions themselves (Oracle needs a session parameter
    for positions, which older versions and Autonomous Database may not allow)."""
    group = select.args["group"]
    outputs = select.expressions
    expanded = []
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int and 0 < int(item.name) <= len(outputs):
            chosen = outputs[int(item.name) - 1]
            expanded.append((chosen.this if isinstance(chosen, exp.Alias) else chosen).copy())
        else:
            expanded.append(item)
    group.set("expressions", expanded)
