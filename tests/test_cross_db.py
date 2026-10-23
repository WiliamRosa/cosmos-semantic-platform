"""Same request, same number on every data source.

Generates random requests (fixed seed) combining metrics, dimensions, grains, business rules
and users, and requires DuckDB, Postgres and SQL Server to return exactly the same result.
Also checks that a refused request is refused on every data source.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import random

import pytest

from cosmos_semantic.compiler import TIME_GRAINS
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.planner import Planner, Principal, QueryRequest

REGIONS = ["Sul", "Sudeste", "Nordeste", "Norte", "Centro-Oeste"]
FILTERS = [
    "{orders.status} = 'completed'",
    "{orders.has_discount}",
    "NOT {orders.has_discount}",
    "{rule.active_customer}",
    "{orders.order_size_band} IN ('Grande', 'Médio')",
    "{products.category} IS NULL OR {products.category} LIKE 'C%'",
    "{payments.installments} >= 3",
    "{customers.segment} IS NOT NULL",
]
SOURCES = ["duckdb_connector", "postgres_connector", "sqlserver_connector"]


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


REQUESTS_PER_RUN = 80


def test_random_requests_give_the_same_numbers_everywhere(graph, connectors):
    failures = []
    requests = _requests(graph, REQUESTS_PER_RUN, seed=2026)
    assert len(requests) == REQUESTS_PER_RUN
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
            QueryRequest(metrics=["customer_count"], dimensions=["customers.cpf"]),
            Principal(user={"region": "Sul"}, channel="mcp"),
        ),
        (QueryRequest(metrics=["revenue_month_to_date"]), Principal()),
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
