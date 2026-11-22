"""Structured filters (DEC-50): `{dimension, op, value}` objects next to expression strings.

The engine builds the filter as a tree and SQLGlot writes the value as a literal of each
dialect, so no caller quotes a value. An equivalent string and structured filter give the same
SQL and `sql_hash`; values with quotes and backslashes stay values on every dialect. Reference
case A52 checks the numbers against hand-written SQL on every database."""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlglot
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from sqlglot import exp

from cosmos_semantic.api import create_app
from cosmos_semantic.auth import Authenticator, AuthSettings, DevKey
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import QueryError
from cosmos_semantic.evidence import sql_hash
from cosmos_semantic.expressions import describe_filter, filter_refs
from cosmos_semantic.mcp_server import build_mcp
from cosmos_semantic.planner import Filter, Planner, Principal, QueryRequest
from cosmos_semantic.service import SemanticService

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
MIDWEST = Principal(user={"region": "Midwest"}, channel="api")
# A quote, and a backslash before a quote: MySQL, MariaDB, Databricks and BigQuery read a
# backslash in a string literal as an escape, the other dialects do not.
TRICKY = ["O'Brien", "a\\'b", "x' OR '1'='1", "back\\", "\\\\''"]

# (metric, expression string, the same filter as an object)
EQUIVALENT = [
    # `{lit}` is the value as a literal of the dialect, as a careful client would write it.
    ("customer_count", "{customers.segment} = {lit}", ("customers.segment", "=", "O'Brien")),
    ("customer_count", "{customers.segment} <> 'SMB'", ("customers.segment", "!=", "SMB")),
    ("customer_count", "{customers.segment} <> 'SMB'", ("customers.segment", "<>", "SMB")),
    ("payment_count", "{payments.installments} >= 3", ("payments.installments", ">=", 3)),
    ("payment_count", "{payments.installments} < 2.5", ("payments.installments", "<", 2.5)),
    ("payment_count", "{payments.installments} > -2", ("payments.installments", ">", -2)),
    ("payment_count", "{payments.installments} <= 0", ("payments.installments", "<=", 0)),
    (
        "order_count",
        "{orders.status} IN ('completed', 'pending')",
        ("orders.status", "in", ["completed", "pending"]),
    ),
    (
        "order_count",
        "{orders.status} NOT IN ('canceled')",
        ("orders.status", "not in", ["canceled"]),
    ),
    ("customer_count", "{customers.segment} IS NULL", ("customers.segment", "is null", None)),
    (
        "customer_count",
        "{customers.segment} IS NOT NULL",
        ("customers.segment", "is not null", None),
    ),
    ("order_count", "{orders.has_discount} = TRUE", ("orders.has_discount", "=", True)),
    (
        "order_count",
        "{orders.ordered_at} >= '2026-01-01'",
        ("orders.ordered_at", ">=", "2026-01-01"),
    ),
    (
        "order_count",
        "{orders.ordered_at__month} = '2026-01-01'",
        ("orders.ordered_at__month", "=", "2026-01-01"),
    ),
    ("customer_count", "{rule.active_customer} = TRUE", ("rule.active_customer", "=", True)),
]


def _structured(dimension, op, value) -> dict:
    item = {"dimension": dimension, "op": op}
    if value is not None:
        item["value"] = value
    return item


def _string_literals(sql: str, dialect: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read=dialect)
    return [lit.this for lit in tree.find_all(exp.Literal) if lit.is_string]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("metric", "text", "item"), EQUIVALENT, ids=[c[1] + f" [{c[2][1]}]" for c in EQUIVALENT]
)
def test_string_and_structured_filters_give_the_same_sql(graph, dialect, metric, text, item):
    if "{lit}" in text:
        text = text.replace("{lit}", exp.Literal.string(item[2]).sql(dialect=dialect))
    planner = Planner(graph, dialect)
    as_text = planner.plan(QueryRequest(metrics=[metric], filters=[text]), MIDWEST)
    as_object = planner.plan(QueryRequest(metrics=[metric], filters=[_structured(*item)]), MIDWEST)
    assert as_object.sql == as_text.sql
    assert sql_hash(as_object.sql) == sql_hash(as_text.sql)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_mixed_filters_are_anded_with_the_row_filters(graph, dialect):
    texts = ["{orders.status} = 'completed'", "{customers.segment} IS NOT NULL"]
    mixed = [texts[0], {"dimension": "customers.segment", "op": "is not null"}]
    planner = Planner(graph, dialect)
    by_text = planner.plan(QueryRequest(metrics=["revenue"], filters=texts), MIDWEST)
    by_mix = planner.plan(QueryRequest(metrics=["revenue"], filters=mixed), MIDWEST)
    assert by_mix.sql == by_text.sql
    where = sqlglot.parse_one(by_mix.sql, read=dialect).find(exp.Where)
    assert where is not None and isinstance(where.this, exp.And)
    assert "Midwest" in _string_literals(by_mix.sql, dialect)
    assert by_mix.trace.row_filters[0]["user_fields"] == {"region": "Midwest"}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_values_with_quotes_and_backslashes_stay_values(graph, dialect):
    item = {"dimension": "customers.segment", "op": "in", "value": TRICKY}
    compiled = Planner(graph, dialect).plan(
        QueryRequest(metrics=["customer_count"], filters=[item]), MIDWEST
    )
    literals = _string_literals(compiled.sql, dialect)
    assert all(value in literals for value in TRICKY)
    # One IN list, one row filter: nothing leaked out of a literal into the condition.
    tree = sqlglot.parse_one(compiled.sql, read=dialect)
    assert len(list(tree.find_all(exp.In))) == 1
    assert not list(tree.find_all(exp.Or))


@pytest.mark.parametrize(
    ("dialect", "written"),
    [
        # MySQL and MariaDB: the backslash is doubled, the quote doubled.
        ("mysql", r"'a\\''b'"),
        # Databricks: the backslash is doubled, the quote escaped with a backslash.
        ("databricks", r"'a\\\'b'"),
        ("bigquery", r"'a\\\'b'"),
        # The standard dialects: a backslash is a character; the quote is doubled.
        ("duckdb", r"'a\''b'"),
        ("postgres", r"'a\''b'"),
    ],
)
def test_the_literal_is_written_for_the_dialect(graph, dialect, written):
    item = {"dimension": "customers.segment", "op": "=", "value": "a\\'b"}
    compiled = Planner(graph, dialect).plan(
        QueryRequest(metrics=["customer_count"], filters=[item]), MIDWEST
    )
    assert written in compiled.sql
    assert "a\\'b" in _string_literals(compiled.sql, dialect)


def test_tricky_values_run_on_duckdb(graph, duckdb_connector):
    """A value with quotes or backslashes matches nothing it should not: `in` with the real
    `SMB` and the tricky values counts the SMB customers, and the tricky values alone count
    none."""
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    service = SemanticService(engine, audit_path=None)

    def count(filters) -> int:
        result = service.query(QueryRequest(metrics=["customer_count"], filters=filters), MIDWEST)
        return result["rows"][0]["customer_count"]

    smb = count(["{customers.segment} = 'SMB'"])
    assert smb > 0
    seg = "customers.segment"
    assert count([{"dimension": seg, "op": "in", "value": ["SMB", *TRICKY]}]) == smb
    assert count([{"dimension": seg, "op": "in", "value": TRICKY}]) == 0
    for value in TRICKY:
        assert count([{"dimension": seg, "op": "=", "value": value}]) == 0
    everyone = count([])
    named = count([{"dimension": seg, "op": "is not null"}])
    assert count([{"dimension": seg, "op": "not in", "value": TRICKY}]) == named < everyone


@pytest.mark.parametrize(
    "item",
    [
        {"dimension": "customers.segment", "op": "like", "value": "S%"},
        {"dimension": "customers.segment", "op": "= 1 OR 1", "value": "x"},
        {"dimension": "customers.segment", "op": "=", "value": None},
        {"dimension": "customers.segment", "op": "=", "value": ["a"]},
        {"dimension": "customers.segment", "op": "=", "value": {"a": 1}},
        {"dimension": "customers.segment", "op": "in", "value": "SMB"},
        {"dimension": "customers.segment", "op": "in", "value": []},
        {"dimension": "customers.segment", "op": "in", "value": [["SMB"]]},
        {"dimension": "customers.segment", "op": "in", "value": [None]},
        {"dimension": "customers.segment", "op": "is null", "value": "x"},
        {"dimension": "customers.segment", "op": "=", "value": "x", "sql": "1=1"},
        {"dimension": "payments.installments", "op": ">", "value": float("nan")},
        {"dimension": "customers.segment", "op": "in", "value": ["x"] * 1001},
    ],
)
def test_malformed_structured_filters_are_refused(item):
    with pytest.raises(ValidationError):
        QueryRequest(metrics=["customer_count"], filters=[item])


def test_operators_are_case_and_space_insensitive():
    item = Filter.model_validate({"dimension": "orders.status", "op": " NOT   In ", "value": ["a"]})
    assert item.op == "not in"


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"dimension": "customers.planet", "op": "=", "value": "x"}, "does not exist"),
        ({"dimension": "rule.nope", "op": "=", "value": True}, "does not exist"),
        ({"dimension": "customers.segment} OR {x", "op": "=", "value": "x"}, "invalid dimension"),
        ({"dimension": "1=1", "op": "=", "value": "x"}, "invalid dimension"),
        ({"dimension": "payments.installments", "op": ">", "value": "3"}, "number dimension"),
        ({"dimension": "payments.installments", "op": "=", "value": True}, "number dimension"),
        ({"dimension": "orders.has_discount", "op": "=", "value": "true"}, "boolean dimension"),
        ({"dimension": "orders.ordered_at", "op": ">=", "value": 2026}, "time dimension"),
        ({"dimension": "orders.ordered_at", "op": ">=", "value": "yesterday"}, "time dimension"),
        ({"dimension": "orders.status", "op": "in", "value": ["a", False]}, "categorical"),
    ],
)
def test_unknown_dimensions_and_wrong_value_types_are_query_errors(graph, item, message):
    request = QueryRequest(metrics=["order_count"], filters=[item])
    with pytest.raises(QueryError, match=message):
        Planner(graph, "duckdb").plan(request, MIDWEST)


def test_agents_cannot_filter_on_personal_data_with_an_object(graph):
    from cosmos_semantic.errors import PolicyError

    item = {"dimension": "customers.email", "op": "=", "value": "a@example.com"}
    request = QueryRequest(metrics=["customer_count"], filters=[item])
    with pytest.raises(PolicyError):
        Planner(graph, "duckdb").plan(request, Principal(user={"region": "Midwest"}, channel="mcp"))


def test_filter_text_and_refs_for_the_audit_views():
    item = {"dimension": "customers.segment", "op": "not in", "value": ["O'Brien", "a\\'b"]}
    assert describe_filter(item) == "{customers.segment} NOT IN ('O''Brien', 'a\\''b')"
    assert describe_filter({"dimension": "orders.status", "op": "is null"}) == (
        "{orders.status} IS NULL"
    )
    assert describe_filter(Filter.model_validate({**item, "op": "!=", "value": 3})) == (
        "{customers.segment} != 3"
    )
    assert describe_filter("{rule.active_customer}") == "{rule.active_customer}"
    assert filter_refs(item) == ["customers.segment"]
    assert filter_refs("{orders.status} = 'x' AND {rule.active_customer}") == [
        "orders.status",
        "rule.active_customer",
    ]
    # A malformed record in the audit log is shown as it is, never an error.
    assert describe_filter({"op": "="}) == '{"op": "="}'


# -- REST and MCP ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service(graph, duckdb_connector, tmp_path_factory):
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = duckdb_connector
    audit = tmp_path_factory.mktemp("structured") / "audit.jsonl"
    return SemanticService(engine, audit_path=audit)


@pytest.fixture(scope="module")
def authenticator(graph):
    settings = AuthSettings(
        dev_keys={"dev-sul": DevKey(subject="dev@local", user={"region": "Midwest"})}
    )
    return Authenticator(settings, graph.project.user_context)


@pytest.fixture(scope="module")
def client(service, authenticator):
    return TestClient(create_app(service, authenticator))


HEADERS = {"X-API-Key": "dev-sul"}
STATUS_IN = {"dimension": "orders.status", "op": "in", "value": ["completed", "pending"]}
STATUS_TEXT = "{orders.status} IN ('completed', 'pending')"


def test_rest_query_and_sql_take_structured_filters(client, service):
    base = {"metrics": ["revenue"], "dimensions": ["customers.segment"], "order_by": ["-revenue"]}
    by_text = client.post("/v1/query", headers=HEADERS, json={**base, "filters": [STATUS_TEXT]})
    by_object = client.post("/v1/query", headers=HEADERS, json={**base, "filters": [STATUS_IN]})
    assert by_object.status_code == 200, by_object.text
    assert by_object.json()["rows"] == by_text.json()["rows"]
    evidence, text_evidence = by_object.json()["evidence"], by_text.json()["evidence"]
    assert evidence["sql"]["hash"] == text_evidence["sql"]["hash"]
    sql = client.post("/v1/sql", headers=HEADERS, json={**base, "filters": [STATUS_IN]}).json()
    assert (
        sql["sql"]
        == client.post("/v1/sql", headers=HEADERS, json={**base, "filters": [STATUS_TEXT]}).json()[
            "sql"
        ]
    )

    # The audit keeps the object; replay validates it again and gets the same SQL.
    query_id = evidence["query_id"]
    record = service.find_audit(query_id)
    assert record["request"]["filters"] == [STATUS_IN]
    replay = client.post(f"/v1/replay/{query_id}", headers=HEADERS).json()
    assert replay["verdict"] == "identical"
    # The console shows it as text.
    answer = client.get("/v1/console/answers", headers=HEADERS).json()
    shown = next(a for a in answer["answers"] if a["query_id"] == query_id)
    assert shown["filters"] == [STATUS_TEXT]


def test_rest_refuses_malformed_and_ill_typed_filters(client):
    base = {"metrics": ["order_count"]}
    bad_op = {"dimension": "orders.status", "op": "like", "value": "c%"}
    response = client.post("/v1/query", headers=HEADERS, json={**base, "filters": [bad_op]})
    assert response.status_code == 422
    ill_typed = {"dimension": "payments.installments", "op": ">", "value": "3"}
    response = client.post("/v1/query", headers=HEADERS, json={**base, "filters": [ill_typed]})
    assert response.status_code == 400 and "number dimension" in response.json()["detail"]
    unknown = {"dimension": "orders.planet", "op": "=", "value": "x"}
    response = client.post("/v1/sql", headers=HEADERS, json={**base, "filters": [unknown]})
    assert response.status_code == 400 and "does not exist" in response.json()["detail"]


def test_rest_dimension_values_take_filters(client, service):
    path = "/v1/dimensions/orders.status/values"
    everything = client.get(path, headers=HEADERS).json()
    body = {"filters": [STATUS_IN]}
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    values = response.json()
    expected = [v for v in everything["values"] if v["value"] in ("completed", "pending")]
    assert values["values"] == expected
    assert values["evidence"]["filters"] == [STATUS_TEXT]
    # The count of each value is the rows of its own model under the filter, as a query says.
    segment = {"dimension": "customers.segment", "op": "=", "value": "SMB"}
    filtered = client.post(path, headers=HEADERS, json={"filters": [segment]}).json()
    grouped = client.post(
        "/v1/query",
        headers=HEADERS,
        json={"metrics": ["order_count"], "dimensions": ["orders.status"], "filters": [segment]},
    ).json()
    want = {r["orders__status"]: r["order_count"] for r in grouped["rows"]}
    assert {v["value"]: v["count"] for v in filtered["values"]} == want
    # Row filters of the joined model still apply, ANDed with the request filter.
    assert filtered["evidence"]["row_filters"][0]["user_fields"] == {"region": "Midwest"}
    # A time dimension's range under a filter.
    ranged = client.post(
        "/v1/dimensions/orders.ordered_at/values", headers=HEADERS, json={"filters": [segment]}
    ).json()
    assert ranged["type"] == "time" and ranged["distinct_days"] > 0
    assert ranged["evidence"]["filters"] == ["{customers.segment} = 'SMB'"]
    limited = client.post(path, headers=HEADERS, json={"limit": 1, **body}).json()
    assert len(limited["values"]) == 1
    bad = client.post(path, headers=HEADERS, json={"filters": [{"dimension": "x", "op": "~"}]})
    assert bad.status_code == 422
    # The audit records the filters of the call.
    audit = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    last = [r for r in audit if r["event"] == "dimension_values"][-1]
    assert last["request"]["filters"] == [STATUS_IN] and last["outcome"] == "ok"


@pytest.fixture(scope="module")
def mcp_server(service, authenticator):
    return build_mcp(service, authenticator)


def _call(server, tool: str, **arguments):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run())


def _content(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_mcp_tools_take_structured_filters(mcp_server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "dev-sul")
    tricky = {"dimension": "customers.segment", "op": "not in", "value": TRICKY}
    by_object = _content(
        _call(mcp_server, "query", metrics=["order_count"], filters=[STATUS_IN, tricky])
    )
    by_text = _content(
        _call(
            mcp_server,
            "query",
            metrics=["order_count"],
            filters=[STATUS_TEXT, "{customers.segment} IS NOT NULL"],
        )
    )
    assert by_object["rows"] == by_text["rows"] and by_object["rows"][0]["order_count"] > 0
    sql = _content(_call(mcp_server, "show_sql", metrics=["order_count"], filters=[STATUS_IN]))
    text_sql = _content(
        _call(mcp_server, "show_sql", metrics=["order_count"], filters=[STATUS_TEXT])
    )
    assert sql["sql"] == text_sql["sql"]
    values = _content(
        _call(mcp_server, "dimension_values", name="orders.status", filters=[STATUS_IN])
    )
    assert {v["value"] for v in values["values"]} <= {"completed", "pending"}


def test_mcp_refuses_malformed_and_ill_typed_filters(mcp_server, monkeypatch):
    monkeypatch.setenv("COSMOS_API_KEY", "dev-sul")
    bad_op = {"dimension": "orders.status", "op": "like", "value": "c%"}
    result = _call(mcp_server, "query", metrics=["order_count"], filters=[bad_op])
    assert result.is_error
    ill_typed = {"dimension": "orders.has_discount", "op": "=", "value": "yes"}
    result = _content(_call(mcp_server, "query", metrics=["order_count"], filters=[ill_typed]))
    assert result["error"] == "QueryError" and "boolean dimension" in result["detail"]


def test_mcp_schema_describes_the_filter_object(mcp_server):
    async def run():
        async with Client(mcp_server) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}

    schemas = asyncio.run(run())
    for tool in ("query", "show_sql", "dimension_values"):
        text = json.dumps(schemas[tool])
        assert '"not in"' in text and '"dimension"' in text, tool
