"""Same request, same number on every data source.

Generates random requests (fixed seed) combining metrics, dimensions, grains, business rules
and users, and requires DuckDB, Postgres and SQL Server to return exactly the same result.
Rolling-window metrics, valid only at day grain, get draws of their own so every run has them,
also with a filter on their time dimension (DEC-34); so do grain-to-date metrics with a filter
on their time dimension (DEC-48), and metrics that read an input at an earlier period, valid
only at the grains it fits.
Also checks that a refused request is refused on every data source.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import random

import pytest

from cosmos_semantic.compiler import TIME_GRAINS, metrics_under, to_date_grain_problem
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.spec import CumulativeMetric, DerivedMetric, offset_grain_problem

REGIONS = ["Midwest", "Northeast", "South", "West", "Mountain"]
FILTERS = [
    "{orders.status} = 'completed'",
    "{orders.has_discount}",
    "NOT {orders.has_discount}",
    "{rule.active_customer}",
    "{orders.order_size_band} IN ('Large', 'Medium')",
    "{products.category} IS NULL OR {products.category} LIKE 'C%'",
    "{payments.installments} >= 3",
    "{customers.segment} IS NOT NULL",
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
    "trino_connector",
]


@pytest.fixture(scope="module")
def connectors(request):
    found = {}
    for name in SOURCES:
        try:
            found[name.split("_")[0]] = request.getfixturevalue(name)
        except pytest.skip.Exception:
            continue
    if len(found) < 2:
        pytest.skip("needs at least two data sources (set COSMOS_PG_URI or COSMOS_MSSQL_URI)")
    return found


def _norm(value):
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    return value


def _rows(table, columns):
    rows = [tuple(_norm(r[c]) for c in columns) for r in table.to_pylist()]
    return sorted(rows, key=lambda t: tuple((v is None, str(v)) for v in t))


def _equal(a, b) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, int | float) and isinstance(v, int | float):
                if not math.isclose(u, v, rel_tol=1e-9, abs_tol=1e-6):
                    return False
            elif u != v:
                return False
    return True


def _requests(graph, count: int, seed: int):
    rng = random.Random(seed)
    metrics = sorted(set(graph.metrics) | set(graph.measure_owner))
    dims = sorted(
        f"{m}.{d}"
        for m, node in graph.models.items()
        for d, spec in node.dimensions.items()
        if not spec.sensitivity
    )
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        chosen_dims = rng.sample(dims, rng.randint(0, 2))
        chosen_dims = [
            f"{d}__{rng.choice(TIME_GRAINS[1:])}"
            if graph.dimension(graph.resolve_dimension(d)).type == "time"
            else d
            for d in chosen_dims
        ]
        request = QueryRequest(
            metrics=rng.sample(metrics, rng.randint(1, 3)),
            dimensions=chosen_dims,
            filters=rng.sample(FILTERS, rng.choice([0, 0, 1, 2])),
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            Planner(graph, "duckdb").plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


# Every dialect a connector can have: a drawn request must plan on all of them.
DIALECTS = [
    "duckdb",
    "postgres",
    "tsql",
    "databricks",
    "snowflake",
    "mysql",
    "oracle",
    "bigquery",
    "trino",
]


def _rolling_requests(graph, count: int, seed: int):
    """Requests with a rolling-window metric and a time dimension at day grain (the only grain
    a window in days accepts), with up to one other dimension, one other metric and one filter.
    Its own generator, so these draws do not change the random sequence of `_requests` (the
    model's members still decide what that sequence picks)."""
    rng = random.Random(seed)
    rolling = sorted(
        name
        for name, metric in graph.metrics.items()
        if isinstance(metric, CumulativeMetric) and metric.window_days()
    )
    others = sorted(
        name
        for name in set(graph.metrics) | set(graph.measure_owner)
        if not isinstance(graph.metrics.get(name), CumulativeMetric)
    )
    days, plain = [], []
    for m, node in graph.models.items():
        for d, spec in node.dimensions.items():
            if not spec.sensitivity:
                (days if spec.type == "time" else plain).append(f"{m}.{d}")
    days = sorted(f"{d}__day" for d in days)
    plain = sorted(plain)
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        request = QueryRequest(
            metrics=[rng.choice(rolling), *rng.sample(others, rng.randint(0, 1))],
            dimensions=[*rng.sample(plain, rng.randint(0, 1)), rng.choice(days)],
            filters=rng.sample(FILTERS, rng.choice([0, 1])),
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            for dialect in DIALECTS:
                Planner(graph, dialect).plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


# Filters on a rolling window's time dimension (DEC-34), written for the drawn dimension `{t}`:
# shapes the planner splits (ranges, BETWEEN, IN, a coarser grain, OR) and one it cannot (NOT).
DATE_FILTERS = [
    "{{{t}}} >= CURRENT_DATE - INTERVAL '60' DAY",
    "{{{t}}} BETWEEN CURRENT_DATE - INTERVAL '120' DAY AND CURRENT_DATE - INTERVAL '90' DAY",
    "{{{t}__day}} IN (CURRENT_DATE - INTERVAL '10' DAY, CURRENT_DATE - INTERVAL '33' DAY)",
    "{{{t}__month}} >= CURRENT_DATE - INTERVAL '75' DAY",
    "{{{t}}} < CURRENT_DATE - INTERVAL '300' DAY OR {{{t}__day}} >= CURRENT_DATE - INTERVAL '20' DAY",
    "NOT ({{{t}}} < CURRENT_DATE - INTERVAL '30' DAY)",
]


def _rolling_date_filter_requests(graph, count: int, seed: int):
    """Rolling-window requests with a filter on the window's time dimension and up to one
    other filter (DEC-34). Its own generator, as for rolling windows."""
    rng = random.Random(seed)
    rolling = sorted(
        name
        for name, metric in graph.metrics.items()
        if isinstance(metric, CumulativeMetric) and metric.window_days()
    )
    days = sorted(
        f"{m}.{d}"
        for m, node in graph.models.items()
        for d, spec in node.dimensions.items()
        if spec.type == "time" and not spec.sensitivity
    )
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        time = rng.choice(days)
        request = QueryRequest(
            metrics=[rng.choice(rolling)],
            dimensions=[f"{time}__day"],
            filters=[
                rng.choice(DATE_FILTERS).format(t=time),
                *rng.sample(FILTERS, rng.choice([0, 1])),
            ],
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            for dialect in DIALECTS:
                Planner(graph, dialect).plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


def _to_date_filter_requests(graph, count: int, seed: int):
    """Grain-to-date requests with a filter on the time dimension and up to one other filter
    (DEC-48), at the grains each period accepts. Its own generator, as for rolling windows."""
    rng = random.Random(seed)
    to_date = sorted(
        name
        for name, metric in graph.metrics.items()
        if isinstance(metric, CumulativeMetric) and metric.grain_to_date
    )
    times = sorted(
        f"{m}.{d}"
        for m, node in graph.models.items()
        for d, spec in node.dimensions.items()
        if spec.type == "time" and not spec.sensitivity
    )
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        name, time = rng.choice(to_date), rng.choice(times)
        period = graph.metrics[name].grain_to_date
        grain = rng.choice([g for g in ("day", "month") if not to_date_grain_problem(period, g)])
        request = QueryRequest(
            metrics=[name],
            dimensions=[f"{time}__{grain}"],
            filters=[
                rng.choice(DATE_FILTERS).format(t=time),
                *rng.sample(FILTERS, rng.choice([0, 1])),
            ],
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            for dialect in DIALECTS:
                Planner(graph, dialect).plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


def _offset_grains(graph, name: str) -> list[str]:
    """The grains every offset under ``name`` fits (empty when it reads no offset)."""
    offsets = [
        i.offset
        for under in metrics_under(graph, name)
        if isinstance(graph.metrics[under], DerivedMetric)
        for i in graph.metrics[under].inputs
    ]
    if not offsets:
        return []
    return [g for g in TIME_GRAINS if not any(offset_grain_problem(o, g) for o in offsets)]


def _offset_requests(graph, count: int, seed: int):
    """Requests with a metric that reads an input at an earlier period (DEC-18) and one time
    dimension at a grain its offset fits, with up to one other dimension, one other metric and
    one filter. Its own generator, as for rolling windows."""
    rng = random.Random(seed)
    shifted = sorted(n for n in graph.metrics if _offset_grains(graph, n))
    others = sorted(
        name
        for name in set(graph.metrics) | set(graph.measure_owner)
        if not isinstance(graph.metrics.get(name), CumulativeMetric)
        and not _offset_grains(graph, name)
    )
    times, plain = [], []
    for m, node in graph.models.items():
        for d, spec in node.dimensions.items():
            if not spec.sensitivity:
                (times if spec.type == "time" else plain).append(f"{m}.{d}")
    times, plain = sorted(times), sorted(plain)
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        metric = rng.choice(shifted)
        time = f"{rng.choice(times)}__{rng.choice(_offset_grains(graph, metric))}"
        request = QueryRequest(
            metrics=[metric, *rng.sample(others, rng.randint(0, 1))],
            dimensions=[*rng.sample(plain, rng.randint(0, 1)), time],
            filters=rng.sample(FILTERS, rng.choice([0, 1])),
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            for dialect in DIALECTS:
                Planner(graph, dialect).plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


METRIC_CONDITIONS = ["> 0", ">= 1000", "< 50000", "<= 2500.5", "IS NOT NULL", "IS NULL"]


def _metric_filter_requests(graph, count: int, seed: int):
    """Requests with conditions on metric values (DEC-29): up to two metrics without running
    totals or offsets, up to two dimensions, a request filter or none, and one or two
    conditions on the requested metrics. No limit: strings sort by each database's collation,
    so a limit could keep different rows (A40 checks the limit with a numeric order). Its own
    generator, as for rolling windows."""
    rng = random.Random(seed)
    plain = sorted(
        name
        for name in set(graph.metrics) | set(graph.measure_owner)
        if not isinstance(graph.metrics.get(name), CumulativeMetric)
        and not _offset_grains(graph, name)
    )
    dims = sorted(
        f"{m}.{d}"
        for m, node in graph.models.items()
        for d, spec in node.dimensions.items()
        if not spec.sensitivity
    )
    produced, attempts = [], 0
    while len(produced) < count and attempts < count * 50:
        attempts += 1
        metrics = rng.sample(plain, rng.randint(1, 2))
        chosen = [
            f"{d}__{rng.choice(TIME_GRAINS[1:])}"
            if graph.dimension(graph.resolve_dimension(d)).type == "time"
            else d
            for d in rng.sample(dims, rng.randint(1, 2))
        ]
        conditions = [
            f"{{{rng.choice(metrics)}}} {rng.choice(METRIC_CONDITIONS)}"
            for _ in range(rng.randint(1, 2))
        ]
        request = QueryRequest(
            metrics=metrics,
            dimensions=chosen,
            filters=rng.sample(FILTERS, rng.choice([0, 1])),
            metric_filters=[" AND ".join(conditions)],
        )
        principal = Principal(
            user={"region": rng.choice(REGIONS)}, channel=rng.choice(["api", "mcp"])
        )
        try:
            for dialect in DIALECTS:
                Planner(graph, dialect).plan(request, principal)
        except CosmosError:
            continue
        produced.append((request, principal))
    return produced


REQUESTS_PER_RUN = 80
ROLLING_PER_RUN = 12
ROLLING_DATE_FILTER_PER_RUN = 8
TO_DATE_FILTER_PER_RUN = 8
OFFSET_PER_RUN = 12
METRIC_FILTER_PER_RUN = 8


def test_metric_filter_draws_are_deterministic(graph):
    first = _metric_filter_requests(graph, METRIC_FILTER_PER_RUN, seed=2026)
    assert len(first) == METRIC_FILTER_PER_RUN
    assert first == _metric_filter_requests(graph, METRIC_FILTER_PER_RUN, seed=2026)
    assert all(request.metric_filters for request, _ in first)


def test_offset_draws_are_deterministic_and_cover_every_offset_metric(graph):
    first = _offset_requests(graph, OFFSET_PER_RUN, seed=2026)
    assert len(first) == OFFSET_PER_RUN
    assert first == _offset_requests(graph, OFFSET_PER_RUN, seed=2026)
    drawn = {m for request, _ in first for m in request.metrics}
    assert {n for n in graph.metrics if _offset_grains(graph, n)} <= drawn


def test_rolling_draws_are_deterministic_and_cover_every_rolling_metric(graph):
    first = _rolling_requests(graph, ROLLING_PER_RUN, seed=2026)
    assert len(first) == ROLLING_PER_RUN
    assert first == _rolling_requests(graph, ROLLING_PER_RUN, seed=2026)
    drawn = {m for request, _ in first for m in request.metrics}
    rolling = {
        name
        for name, metric in graph.metrics.items()
        if isinstance(metric, CumulativeMetric) and metric.window_days()
    }
    assert rolling <= drawn
    for request, _ in first:
        assert sum(d.endswith("__day") for d in request.dimensions) == 1


def test_rolling_date_filter_draws_are_deterministic_and_split(graph):
    first = _rolling_date_filter_requests(graph, ROLLING_DATE_FILTER_PER_RUN, seed=2026)
    assert len(first) == ROLLING_DATE_FILTER_PER_RUN
    assert first == _rolling_date_filter_requests(graph, ROLLING_DATE_FILTER_PER_RUN, seed=2026)
    lookback = [
        "lookback" in Planner(graph, "duckdb").plan(request, principal).sql
        for request, principal in first
    ]
    assert any(lookback)


def test_to_date_filter_draws_are_deterministic_and_split(graph):
    first = _to_date_filter_requests(graph, TO_DATE_FILTER_PER_RUN, seed=2026)
    assert len(first) == TO_DATE_FILTER_PER_RUN
    assert first == _to_date_filter_requests(graph, TO_DATE_FILTER_PER_RUN, seed=2026)
    start = [
        "_start AS" in Planner(graph, "duckdb").plan(request, principal).sql
        for request, principal in first
    ]
    assert any(start)


def test_random_requests_give_the_same_numbers_everywhere(graph, connectors):
    failures = []
    requests = _requests(graph, REQUESTS_PER_RUN, seed=2026)
    assert len(requests) == REQUESTS_PER_RUN
    rolling = _rolling_requests(graph, ROLLING_PER_RUN, seed=2026)
    assert len(rolling) == ROLLING_PER_RUN
    requests += rolling
    dated = _rolling_date_filter_requests(graph, ROLLING_DATE_FILTER_PER_RUN, seed=2026)
    assert len(dated) == ROLLING_DATE_FILTER_PER_RUN
    requests += dated
    to_date = _to_date_filter_requests(graph, TO_DATE_FILTER_PER_RUN, seed=2026)
    assert len(to_date) == TO_DATE_FILTER_PER_RUN
    requests += to_date
    shifted = _offset_requests(graph, OFFSET_PER_RUN, seed=2026)
    assert len(shifted) == OFFSET_PER_RUN
    requests += shifted
    kept = _metric_filter_requests(graph, METRIC_FILTER_PER_RUN, seed=2026)
    assert len(kept) == METRIC_FILTER_PER_RUN
    requests += kept
    for i, (request, principal) in enumerate(requests):
        results = {}
        for name, connector in connectors.items():
            compiled = Planner(graph, connector.dialect, connector.config.schema_map).plan(
                request, principal
            )
            columns = [c.name for c in compiled.columns]
            try:
                results[name] = _rows(connector.execute(compiled.sql, max_rows=50_000), columns)
            except CosmosError as exc:
                results[name] = f"error: {exc}"
        first_name, first = next(iter(results.items()))
        for name, rows in results.items():
            if isinstance(rows, str) or isinstance(first, str) or not _equal(first, rows):
                failures.append(
                    f"#{i} {request.model_dump(exclude_defaults=True)} {principal.user}: "
                    f"{first_name}={str(first)[:200]} | {name}={str(rows)[:200]}"
                )
                break
    assert not failures, "\n".join(failures[:10])


def test_refusals_are_the_same_everywhere(graph, connectors):
    bad = [
        (QueryRequest(metrics=["paid_value"], dimensions=["products.category"]), Principal()),
        (QueryRequest(metrics=["customer_count"]), Principal()),
        (
            QueryRequest(metrics=["customer_count"], dimensions=["customers.tax_id"]),
            Principal(user={"region": "Midwest"}, channel="mcp"),
        ),
        (QueryRequest(metrics=["revenue_month_to_date"]), Principal()),
        (
            QueryRequest(metrics=["revenue_year_to_date"], dimensions=["orders.ordered_at__week"]),
            Principal(),
        ),
    ]
    for request, principal in bad:
        errors = set()
        for connector in connectors.values():
            try:
                Planner(graph, connector.dialect).plan(request, principal)
                errors.add("accepted")
            except CosmosError as exc:
                errors.add(type(exc).__name__)
        assert len(errors) == 1 and "accepted" not in errors, (request, errors)
