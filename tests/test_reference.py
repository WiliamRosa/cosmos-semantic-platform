"""Roadmap gates: the engine returns the same result as hand-written SQL."""

from __future__ import annotations

import pytest

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import ModelError
from cosmos_semantic.evidence import EvidenceBuilder, sql_hash
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import Planner, QueryRequest
from cosmos_semantic.reference import ReferenceCase, load_cases, run_case
from cosmos_semantic.spec import Project

from .conftest import CASES, EXAMPLE

ALL = load_cases(CASES)
ADVANCED = load_cases(CASES.with_name("queries_advanced.yml"))
SQL_CASES = [c for c in ALL + ADVANCED if c.question is None]


def _check(case, graph, connector):
    result = run_case(case, graph, connector)
    if result.status == "skip":
        pytest.skip(result.detail)
    assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_duckdb(case, graph, duckdb_connector):
    _check(case, graph, duckdb_connector)


@pytest.mark.postgres
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_postgres(case, graph, postgres_connector):
    _check(case, graph, postgres_connector)


@pytest.mark.sqlserver
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_sqlserver(case, graph, sqlserver_connector):
    _check(case, graph, sqlserver_connector)


@pytest.mark.databricks
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_databricks(case, graph, databricks_connector):
    _check(case, graph, databricks_connector)


@pytest.mark.snowflake
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_snowflake(case, graph, snowflake_connector):
    _check(case, graph, snowflake_connector)


@pytest.mark.mysql
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_mysql(case, graph, mysql_connector):
    _check(case, graph, mysql_connector)


@pytest.mark.mariadb
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_mariadb(case, graph, mariadb_connector):
    _check(case, graph, mariadb_connector)


@pytest.mark.oracle
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_oracle(case, graph, oracle_connector):
    _check(case, graph, oracle_connector)


@pytest.mark.bigquery
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_bigquery(case, graph, bigquery_connector):
    _check(case, graph, bigquery_connector)


@pytest.mark.trino
@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.id)
def test_reference_trino(case, graph, trino_connector):
    _check(case, graph, trino_connector)


def test_year_to_date_distinct_count_is_refused(duckdb_connector):
    """A year-to-date over a `count_distinct` measure cannot be in a valid model (validation
    refuses it, so it is not in the example and has no case in the YAML files); a model loaded
    despite the error still gets no number from the planner (DEC-21)."""
    data = load_project(EXAMPLE).model_dump(mode="json")
    data["metrics"].append(
        {
            "name": "buyers_year_to_date",
            "type": "cumulative",
            "measure": "buyers",
            "grain_to_date": "year",
        }
    )
    with pytest.raises(ModelError, match="year-to-date over a `count_distinct` measure"):
        compile_project(Project.model_validate(data))
    loaded = compile_project(Project.model_validate(data), strict=False)
    case = ReferenceCase(
        id="DEC21-COUNT-DISTINCT",
        category="tempo",
        phase=3,
        title="Distinct buyers year to date is refused (a buyer would count once per day)",
        request=QueryRequest(
            metrics=["buyers_year_to_date"], dimensions=["orders.ordered_at__month"]
        ),
        expect_error="QueryError",
    )
    result = run_case(case, loaded, duckdb_connector)
    assert result.status == "pass", result.detail
    assert "cannot be accumulated" in result.detail


def test_there_are_twenty_cases():
    assert len(ALL) == 20
    assert len({c.id for c in ALL}) == 20


def test_offset_cases_cross_a_year_change_and_an_empty_month(graph, duckdb_connector):
    """A35 and A36 prove what they claim on any day the suite runs (the data follows the
    clock): A35 has a January that reads the December before, and A36 has a month without
    orders whose next month gets NULL (DEC-18)."""
    by_id = {c.id: c for c in ADVANCED}
    month = "orders__ordered_at__month"

    def rows(case_id):
        compiled = Planner(graph, "duckdb").plan(by_id[case_id].request)
        return duckdb_connector.execute(compiled.sql).to_pylist()

    def index(value):
        return value.year * 12 + value.month

    a35 = {index(r[month]): r for r in rows("A35")}
    januaries = [k for k in a35 if k % 12 == 1 and k - 1 in a35]
    assert januaries
    for k in januaries:
        assert a35[k]["revenue_prev_month"] == pytest.approx(a35[k - 1]["revenue"])

    completed = {index(r[month]): r for r in rows("A36") if r["orders__status"] == "completed"}
    months = sorted(completed)
    gaps = [k for k, before in zip(months[1:], months, strict=False) if k - before > 1]
    assert gaps, "the filter must leave a month without orders"
    for k in gaps:
        assert completed[k]["revenue_prev_month"] is None
        assert completed[k]["revenue_mom_growth"] is None


# -- Offsets in weeks and years (DEC-46) ---------------------------------------------------
# Not in the example (each export test counts its metrics), so these cases run here against
# the example plus two metrics, on every database available. The hand-written SQL pairs
# periods by a number (year * 12 + month, the year, or days since 2000-01-01), never by date
# arithmetic, so the check is independent of the engine's interval SQL.
WEEKS_YEARS_METRICS = [
    {
        "name": "revenue_prev_year",
        "type": "derived",
        "expr": "{revenue}",
        "inputs": [{"metric": "revenue", "offset": "1 year"}],
    },
    {
        "name": "order_count_2_weeks_earlier",
        "type": "derived",
        "expr": "{order_count}",
        "inputs": [{"metric": "order_count", "offset": "2 weeks"}],
    },
]
SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
    "bigquery_connector",
]
DIALECTS = ("duckdb", "postgres", "tsql", "mysql", "oracle", "databricks", "snowflake", "bigquery")


def _two_weeks(sql: str) -> str:
    """A37's hand-written SQL (orders of the day 7 days before), 14 days back instead."""
    assert sql.count("cur.k - 7") == 1 and sql.count("order_count_7_days_earlier") == 1
    return sql.replace("cur.k - 7", "cur.k - 14").replace(
        "order_count_7_days_earlier", "order_count_2_weeks_earlier"
    )


A37 = next(c for c in ADVANCED if c.id == "A37")
WEEKS_YEARS_CASES = [
    ReferenceCase(
        id="DEC46-1-YEAR-BY-MONTH",
        category="tempo",
        phase=3,
        title="Revenue and revenue of the same month one year before, by month",
        request=QueryRequest(
            metrics=["revenue", "revenue_prev_year"], dimensions=["orders.ordered_at__month"]
        ),
        sql="""
WITH monthly AS (
  SELECT DATE_TRUNC('month', ordered_at) AS mon,
         EXTRACT(YEAR FROM ordered_at) * 12 + EXTRACT(MONTH FROM ordered_at) AS k,
         SUM(amount) AS revenue
  FROM public.orders
  GROUP BY DATE_TRUNC('month', ordered_at),
           EXTRACT(YEAR FROM ordered_at) * 12 + EXTRACT(MONTH FROM ordered_at)
)
SELECT cur.mon AS orders__ordered_at__month, cur.revenue AS revenue,
       prev.revenue AS revenue_prev_year
FROM monthly cur
LEFT JOIN monthly prev ON prev.k = cur.k - 12
""",
        sql_by_dialect={
            "tsql": """
WITH monthly AS (
  SELECT DATETRUNC(month, ordered_at) AS mon,
         YEAR(ordered_at) * 12 + MONTH(ordered_at) AS k,
         SUM(amount) AS revenue
  FROM public.orders
  GROUP BY DATETRUNC(month, ordered_at), YEAR(ordered_at) * 12 + MONTH(ordered_at)
)
SELECT cur.mon AS orders__ordered_at__month, cur.revenue AS revenue,
       prev.revenue AS revenue_prev_year
FROM monthly cur
LEFT JOIN monthly prev ON prev.k = cur.k - 12
"""
        },
    ),
    ReferenceCase(
        id="DEC46-1-YEAR-BY-YEAR",
        category="tempo",
        phase=3,
        title="Revenue of the year before, by status and year",
        request=QueryRequest(
            metrics=["revenue_prev_year"], dimensions=["orders.status", "orders.ordered_at__year"]
        ),
        sql="""
WITH yearly AS (
  SELECT status, DATE_TRUNC('year', ordered_at) AS yr, EXTRACT(YEAR FROM ordered_at) AS k,
         SUM(amount) AS revenue
  FROM public.orders
  GROUP BY status, DATE_TRUNC('year', ordered_at), EXTRACT(YEAR FROM ordered_at)
)
SELECT cur.status AS orders__status, cur.yr AS orders__ordered_at__year,
       prev.revenue AS revenue_prev_year
FROM yearly cur
LEFT JOIN yearly prev
  ON (prev.status = cur.status OR (prev.status IS NULL AND cur.status IS NULL))
  AND prev.k = cur.k - 1
""",
        sql_by_dialect={
            "tsql": """
WITH yearly AS (
  SELECT status, DATETRUNC(year, ordered_at) AS yr, YEAR(ordered_at) AS k,
         SUM(amount) AS revenue
  FROM public.orders
  GROUP BY status, DATETRUNC(year, ordered_at), YEAR(ordered_at)
)
SELECT cur.status AS orders__status, cur.yr AS orders__ordered_at__year,
       prev.revenue AS revenue_prev_year
FROM yearly cur
LEFT JOIN yearly prev
  ON (prev.status = cur.status OR (prev.status IS NULL AND cur.status IS NULL))
  AND prev.k = cur.k - 1
"""
        },
    ),
    ReferenceCase(
        id="DEC46-2-WEEKS-BY-DAY",
        category="tempo",
        phase=3,
        title="Orders of the same day two weeks before, by day",
        request=QueryRequest(
            metrics=["order_count_2_weeks_earlier"], dimensions=["orders.ordered_at__day"]
        ),
        sql=_two_weeks(A37.sql),
        sql_by_dialect={d: _two_weeks(s) for d, s in A37.sql_by_dialect.items()},
    ),
    ReferenceCase(
        id="DEC46-1-YEAR-BY-WEEK",
        category="tempo",
        phase=3,
        title="An offset of 1 year by week is refused (only by month, quarter or year)",
        request=QueryRequest(metrics=["revenue_prev_year"], dimensions=["orders.ordered_at__week"]),
        expect_error="QueryError",
    ),
]


@pytest.fixture(scope="module")
def weeks_years_graph():
    data = load_project(EXAMPLE).model_dump(mode="json")
    data["metrics"].extend(WEEKS_YEARS_METRICS)
    return compile_project(Project.model_validate(data))


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("case", WEEKS_YEARS_CASES, ids=lambda c: c.id)
def test_reference_offsets_in_weeks_and_years(case, source, weeks_years_graph, request):
    _check(case, weeks_years_graph, request.getfixturevalue(source))


def test_offsets_in_weeks_and_years_compile_to_days_and_months(weeks_years_graph):
    """`1 year` gives the SQL (and so the `sql_hash`) of `12 months`, and `2 weeks` that of
    `14 days`, on every dialect: the planner reads only the normalized offset (DEC-46). The
    evidence keeps the offset as written."""
    data = weeks_years_graph.project.model_dump(mode="json")
    normal = {"1 year": "12 months", "2 weeks": "14 days"}
    for metric in data["metrics"]:
        for item in metric.get("inputs") or []:
            item["offset"] = normal.get(item["offset"], item["offset"])
    normalized = compile_project(Project.model_validate(data))
    cases = [c for c in WEEKS_YEARS_CASES if not c.expect_error]
    for case in cases:
        for dialect in DIALECTS:
            written = Planner(weeks_years_graph, dialect).plan(case.request).sql
            same = Planner(normalized, dialect).plan(case.request).sql
            assert written == same, (case.id, dialect)
            assert sql_hash(written) == sql_hash(same)
    builder = EvidenceBuilder(weeks_years_graph)
    assert builder.time("revenue_prev_year", cases[0].request)["shifts"] == [
        {"metric": "revenue_prev_year", "input": "revenue", "offset": "1 year"}
    ]
    assert builder.time("order_count_2_weeks_earlier", cases[2].request)["shifts"] == [
        {"metric": "order_count_2_weeks_earlier", "input": "order_count", "offset": "2 weeks"}
    ]
    assert "1 year earlier" in builder.formula("revenue_prev_year")


# -- Rolling windows in weeks (DEC-27) -----------------------------------------------------
# As the offsets above, not in the example (each export test counts its metrics), so A52 to
# A54 run here against the example plus two metrics, on every database available. A week
# window is read as 7 days: the hand-written SQL is A29's and A30's, the 7-day window as is
# for `1 week` and widened to 14 days (`INTERVAL '13' DAY`) for `2 weeks`.
WEEK_WINDOW_METRICS = [
    {
        "name": "revenue_rolling_1_week",
        "type": "cumulative",
        "measure": "revenue",
        "window": "1 week",
    },
    {
        "name": "revenue_rolling_2_weeks",
        "type": "cumulative",
        "measure": "revenue",
        "window": "2 weeks",
    },
]
A29 = next(c for c in ADVANCED if c.id == "A29")
A30 = next(c for c in ADVANCED if c.id == "A30")


def _one_week(sql: str) -> str:
    """A29's hand-written SQL (7 days), named for the 1-week window."""
    assert sql.count("revenue_rolling_7_days") == 1
    return sql.replace("revenue_rolling_7_days", "revenue_rolling_1_week")


def _fourteen_days(sql: str) -> str:
    """A30's hand-written SQL (the 6 days before each day), with the 13 days before it."""
    assert sql.count("revenue_rolling_7_days") == 1
    six = ("INTERVAL '6' DAY", "DATEADD(day, -6, d.dd)")
    assert sum(sql.count(s) for s in six) == 1
    return (
        sql.replace("revenue_rolling_7_days", "revenue_rolling_2_weeks")
        .replace(six[0], "INTERVAL '13' DAY")
        .replace(six[1], "DATEADD(day, -13, d.dd)")
    )


WEEK_WINDOW_CASES = [
    ReferenceCase(
        id="A52",
        category="tempo",
        phase=3,
        title="Rolling 1-week revenue by day is the rolling 7-day revenue",
        request=QueryRequest(
            metrics=["revenue_rolling_1_week"], dimensions=["orders.ordered_at__day"]
        ),
        sql=_one_week(A29.sql),
        sql_by_dialect={d: _one_week(s) for d, s in A29.sql_by_dialect.items()},
    ),
    ReferenceCase(
        id="A53",
        category="tempo",
        phase=3,
        title="Rolling 2-week revenue by status and day over the last 45 days, with full windows",
        request=QueryRequest(
            metrics=["revenue_rolling_2_weeks", "revenue"],
            dimensions=["orders.status", "orders.ordered_at__day"],
            filters=["{orders.ordered_at} >= CURRENT_DATE - INTERVAL '45' DAY"],
        ),
        sql=_fourteen_days(A30.sql),
        sql_by_dialect={d: _fourteen_days(s) for d, s in A30.sql_by_dialect.items()},
    ),
    ReferenceCase(
        id="A54",
        category="tempo",
        phase=3,
        title="A rolling window in weeks by week is refused (only at day grain)",
        request=QueryRequest(
            metrics=["revenue_rolling_2_weeks"], dimensions=["orders.ordered_at__week"]
        ),
        expect_error="QueryError",
    ),
]


@pytest.fixture(scope="module")
def week_window_graph():
    data = load_project(EXAMPLE).model_dump(mode="json")
    data["metrics"].extend(WEEK_WINDOW_METRICS)
    return compile_project(Project.model_validate(data))


def test_week_window_case_ids_are_free():
    """A52 to A54 live here; `reference/queries_advanced.yml` must not reuse their ids."""
    taken = {c.id for c in ALL + ADVANCED}
    assert not taken & {c.id for c in WEEK_WINDOW_CASES}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("case", WEEK_WINDOW_CASES, ids=lambda c: c.id)
def test_reference_windows_in_weeks(case, source, week_window_graph, request):
    _check(case, week_window_graph, request.getfixturevalue(source))


def test_windows_in_weeks_compile_to_days(week_window_graph):
    """`1 week` gives the SQL (and so the `sql_hash`) of `7 days`, and `2 weeks` that of
    `14 days`, on every dialect: the planner reads only the window's days (DEC-27). The
    evidence keeps the window as written."""
    data = week_window_graph.project.model_dump(mode="json")
    normal = {"1 week": "7 days", "2 weeks": "14 days"}
    for metric in data["metrics"]:
        if metric.get("window"):
            metric["window"] = normal.get(metric["window"], metric["window"])
    normalized = compile_project(Project.model_validate(data))
    assert normalized.metrics["revenue_rolling_2_weeks"].window == "14 days"
    cases = [c for c in WEEK_WINDOW_CASES if not c.expect_error]
    for case in cases:
        for dialect in DIALECTS:
            written = Planner(week_window_graph, dialect).plan(case.request).sql
            same = Planner(normalized, dialect).plan(case.request).sql
            assert written == same, (case.id, dialect)
            assert sql_hash(written) == sql_hash(same)
    builder = EvidenceBuilder(week_window_graph)
    time = builder.time("revenue_rolling_2_weeks", cases[1].request)
    assert (time["kind"], time["window"], time["days"]) == ("window", "2 weeks", 14)
    assert builder.time("revenue_rolling_1_week", cases[0].request)["window"] == "1 week"
    assert "over the 2 weeks ending on each day" in builder.formula("revenue_rolling_2_weeks")
