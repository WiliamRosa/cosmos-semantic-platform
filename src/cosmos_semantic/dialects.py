"""Dialect fixes for SQL that SQLGlot's generic translation does not cover (ADR 0011).

The planner builds one dialect-neutral SQLGlot tree; ``adapt`` rewrites the few constructs a
source cannot run before the tree is printed in that source's dialect:

- **Time truncation:** MySQL/MariaDB have no ``DATE_TRUNC`` (SQLGlot's ``DATE_ADD`` from year
  0 is not Monday-based for weeks); Oracle uses ``TRUNC`` with format models (``'IW'`` is the
  ISO week, starting on Monday). Every grain returns a timestamp, as on the other sources.
- **Oracle:** no ``IS [NOT] DISTINCT FROM`` (``DECODE`` treats two NULLs as equal), no
  ``DATE_ADD`` (interval functions instead) and no ``GROUP BY`` by position unless a session
  parameter is set (the positions are expanded instead).
- **BigQuery:** ``WEEK`` truncates to Sunday, so weeks use ``ISOWEEK`` (Monday), as on the
  other sources. A time without a time zone is a ``DATETIME`` there (SQLGlot writes it for
  ``TIMESTAMP``), and ``TIMESTAMP_TRUNC`` and ``DATE_ADD`` accept it.
- **Trino:** ``DATE_TRUNC('week', ...)`` already starts on Monday and ``/`` on integers
  truncates as in PostgreSQL, so SQLGlot's translation stands. Interval literals have no
  ``WEEK`` or ``QUARTER`` and a ``DATE`` takes no hours, so ``date ± INTERVAL`` becomes
  ``DATE_ADD`` and a date that gets hours, minutes or seconds is read as a ``TIMESTAMP`` first.
- **Date arithmetic:** ``add_days`` and ``shift`` build one ``DATE_ADD`` node that SQLGlot prints
  per dialect; Oracle gets ``+ NUMTODSINTERVAL``/``NUMTOYMINTERVAL`` here.
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
    if unit == "WEEK" and dialect == "bigquery":
        unit = "ISOWEEK"  # BigQuery's WEEK starts on Sunday; ISOWEEK starts on Monday
    if unit in _TIME_OF_DAY_UNITS and dialect == "trino":
        # Trino refuses DATE_TRUNC('hour', date); a date read as a time is midnight.
        value = exp.Cast(this=value, to=exp.DataType.build("TIMESTAMP"))
    return exp.TimestampTrunc(this=value, unit=exp.Var(this=unit))


def add_days(value: exp.Expression, days: int) -> exp.Expression:
    """``value`` plus ``days`` days. SQLGlot prints it in each dialect (``DATEADD``,
    ``DATE_ADD``, ``+ INTERVAL``); ``adapt`` rewrites it for Oracle."""
    return exp.DateAdd(this=value, expression=exp.Literal.number(days), unit=exp.Var(this="DAY"))


def shift(value: exp.Expression, amount: int, unit: str) -> exp.Expression:
    """``value`` plus ``amount`` days or months (DEC-18), printed as ``add_days`` is. The
    planner only shifts period starts (the first day of a month, quarter or year for months),
    so no database has to clamp a day that does not exist in the target month."""
    return exp.DateAdd(
        this=value, expression=exp.Literal.number(amount), unit=exp.Var(this=unit.upper())
    )


def adapt(tree: exp.Expression, dialect: str) -> exp.Expression:
    """Rewrites what ``dialect`` cannot run; a no-op for the other dialects."""
    if dialect == "bigquery":
        from cosmos_semantic.expressions import portable

        # date ± INTERVAL -> DateAdd first: in BigQuery a DATE plus an INTERVAL is a DATETIME.
        return portable(tree).transform(_bigquery)
    if dialect == "trino":
        from cosmos_semantic.expressions import portable

        # date ± INTERVAL -> DateAdd: Trino's interval literals have no WEEK or QUARTER.
        return portable(tree).transform(_trino)
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
        if isinstance(node, exp.Cast) and node.to.is_type(exp.DataType.Type.DATETIME):
            # Oracle has no DATETIME; its TIMESTAMP holds a date and a time of day, as
            # DATETIME does elsewhere (a request filter such as `CAST(CURRENT_DATE AS DATETIME)`).
            return exp.Cast(this=sub(node.this), to=exp.DataType.build("timestamp"))
        if isinstance(node, exp.Select) and node.args.get("group"):
            _expand_group_positions(node)
        return node

    return tree.transform(fix)


def _bigquery(node: exp.Expression) -> exp.Expression:
    """BigQuery rewrites: Monday weeks in hand-written ``DATE_TRUNC('week', ...)`` (reference
    SQL), ``DATETIME_ADD`` for date arithmetic on a time (``DATE_ADD`` takes only a ``DATE``
    and no hours), and ``DATETIME(date)`` for a date read as a time: the same value as the
    cast, which the BigQuery emulator used in tests gets wrong (1970-01-01)."""
    if (
        isinstance(node, exp.Cast)
        and node.to.is_type(*_TIME_TYPES)
        and (_is_date(node.this) or isinstance(node.this, exp.Date))
    ):
        return exp.Anonymous(this="DATETIME", expressions=[node.this.copy()])
    if isinstance(node, exp.TimestampTrunc | exp.DateTrunc) and node.text("unit").upper() == "WEEK":
        node = node.copy()
        node.set("unit", exp.Var(this="ISOWEEK"))
        return node
    if isinstance(node, exp.DateAdd | exp.TsOrDsAdd) and node.text("unit"):
        if _is_date(node.this) and node.text("unit").upper() not in ("HOUR", "MINUTE", "SECOND"):
            return node
        value = node.this.copy()
        if not (isinstance(value, exp.Cast) and value.to.is_type(*_TIME_TYPES)):
            value = exp.Cast(this=value, to=exp.DataType.build("TIMESTAMP"))
        # ``transform`` does not descend into a replacement; adapt its value explicitly.
        value = value.transform(_bigquery)
        return exp.DatetimeAdd(
            this=value, expression=node.expression.copy(), unit=exp.Var(this=node.text("unit"))
        )
    return node


def _trino(node: exp.Expression) -> exp.Expression:
    """Trino rewrites: units are singular (``INTERVAL '3 days'`` arrives as ``DAYS``, which
    ``DATE_ADD`` refuses), and ``DATE_ADD('hour', n, date)`` fails (a ``DATE`` has no time of
    day), so a date that gets hours, minutes or seconds is cast to ``TIMESTAMP`` first, which is
    what PostgreSQL does implicitly for ``date + INTERVAL '3' HOUR``."""
    if not (isinstance(node, exp.DateAdd | exp.TsOrDsAdd) and node.text("unit")):
        return node
    unit = node.text("unit").upper()
    singular = unit[:-1] if unit.endswith("S") and unit[:-1] in _DATE_ADD_UNITS else unit
    to_time = singular in _TIME_OF_DAY_UNITS and _is_date(node.this)
    if singular == unit and not to_time:
        return node
    node = node.copy()
    node.set("unit", exp.Var(this=singular))
    if to_time:
        node.set("this", exp.Cast(this=node.this, to=exp.DataType.build("TIMESTAMP")))
    return node


_TIME_OF_DAY_UNITS = ("HOUR", "MINUTE", "SECOND")
_DATE_ADD_UNITS = ("YEAR", "QUARTER", "MONTH", "WEEK", "DAY", *_TIME_OF_DAY_UNITS)


# SQLGlot writes TIMESTAMP (without a time zone) as BigQuery's DATETIME.
_TIME_TYPES = (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME)


def _is_date(value: exp.Expression) -> bool:
    return isinstance(value, exp.CurrentDate) or (
        isinstance(value, exp.Cast) and value.to.is_type(exp.DataType.Type.DATE)
    )


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
