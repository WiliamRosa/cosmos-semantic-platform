"""Reconciling a metric against a report's exported numbers (DEC-38): `cosmos reconcile
<metric> --expected report.csv`. The report side is written from hand-written SQL, then edited
the way a real report differs: rounded, off on one row, missing a row or with an extra one."""

from __future__ import annotations

import csv
import hashlib
import json

import pytest
from typer.testing import CliRunner

from cosmos_semantic import cli as cli_module
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import QueryError
from cosmos_semantic.planner import QueryRequest
from cosmos_semantic.reconcile import reconcile_csv, reconcile_sql
from cosmos_semantic.spec import Project

from .conftest import EXAMPLE

# Net revenue by status, written by hand: gross amount minus discount, every status.
NET_BY_STATUS = "SELECT status, SUM(amount - discount) FROM public.orders GROUP BY status"
GROSS_BY_STATUS = "SELECT status, SUM(amount) FROM public.orders GROUP BY status"
NET_BY_MONTH = (
    "SELECT DATE_TRUNC('month', ordered_at), SUM(amount - discount) FROM public.orders GROUP BY 1"
)
BY_STATUS = QueryRequest(metrics=["net_revenue"], dimensions=["orders.status"])


def hand_rows(connector, sql: str) -> dict:
    return {r[0]: r[1] for r in (list(x.values()) for x in connector.execute(sql).to_pylist())}


def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def net(duckdb_connector):
    return hand_rows(duckdb_connector, NET_BY_STATUS)


def with_tolerance(graph, metric: str, tolerance: dict):
    data = graph.project.model_dump(mode="json")
    next(m for m in data["metrics"] if m["name"] == metric)["reconciliation"] = tolerance
    return compile_project(Project.model_validate(data))


def test_report_that_agrees(graph, duckdb_connector, net, tmp_path):
    path = write_csv(
        tmp_path / "report.csv",
        ["orders.status", "net_revenue"],
        [[s, repr(v)] for s, v in net.items()],
    )
    report = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert report["status"] == "match"
    assert report["rows"] == {
        "matched": len(net),
        "differing": 0,
        "only_in_cosmos": 0,
        "only_in_legacy": 0,
        "within_tolerance": 0,
    }
    assert report["legacy"] == {
        "kind": "csv",
        "file": "report.csv",
        "file_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(net),
    }
    assert report["totals"]["legacy"] == pytest.approx(sum(net.values()))
    assert report["causes"] == []
    # The CSV report has the shape of the SQL one, so every consumer reads it the same way.
    sql_report = reconcile_sql(graph, duckdb_connector, BY_STATUS, NET_BY_STATUS)
    assert set(report) == set(sql_report)
    assert {k: v for k, v in report.items() if k != "legacy"} == {
        k: v for k, v in sql_report.items() if k != "legacy"
    }


def test_report_rounded_to_thousands_within_the_declared_tolerance(
    graph, duckdb_connector, net, tmp_path
):
    rows = [[s, f"{round(v, -3):.0f}"] for s, v in net.items()]
    path = write_csv(tmp_path / "close.csv", ["orders__status", "net_revenue"], rows)
    tolerant = with_tolerance(graph, "net_revenue", {"rel_tolerance": 0.01})
    report = reconcile_csv(tolerant, duckdb_connector, BY_STATUS, path)
    assert report["status"] == "match"
    assert report["tolerance"] == {"abs_tolerance": 1e-6, "rel_tolerance": 0.01, "source": "metric"}
    assert report["rows"]["within_tolerance"] == len(net) == len(report["tolerated"])
    for row in report["tolerated"]:
        assert row["difference"] == pytest.approx(
            round(net[row["key"][0]], -3) - net[row["key"][0]]
        )

    strict = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert strict["status"] == "divergent"
    assert strict["tolerance"]["source"] == "default"
    assert strict["rows"]["differing"] == len(net)


def test_report_that_differs_on_one_row(graph, duckdb_connector, net, tmp_path):
    rows = [[s, repr(v + 1000 if s == "pending" else v)] for s, v in net.items()]
    path = write_csv(tmp_path / "report.csv", ["orders.status", "net_revenue"], rows)
    report = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert report["status"] == "divergent"
    assert report["rows"]["matched"] == len(net) - 1 and report["rows"]["differing"] == 1
    [example] = report["examples"]
    assert example["key"] == ["pending"]
    assert example["difference"] == pytest.approx(1000)
    assert report["totals"]["difference"] == pytest.approx(1000)
    assert [c["code"] for c in report["causes"]] == ["unexplained"]


def test_rows_missing_on_either_side(graph, duckdb_connector, net, tmp_path):
    rows = [[s, repr(v)] for s, v in net.items() if s != "pending"] + [["refunded", "12.5"]]
    path = write_csv(
        tmp_path / "report.csv", ["net_revenue", "orders.status"], [r[::-1] for r in rows]
    )
    report = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert report["status"] == "divergent"
    assert report["rows"]["only_in_cosmos"] == 1 and report["rows"]["only_in_legacy"] == 1
    assert report["rows"]["matched"] == len(net) - 1
    keys = {tuple(e["key"]): (e["cosmos"], e["legacy"]) for e in report["examples"]}
    assert keys[("pending",)] == (pytest.approx(net["pending"]), None)
    assert keys[("refunded",)] == (None, 12.5)


def test_diagnosis_runs_on_the_report(graph, duckdb_connector, tmp_path):
    """A report showing gross revenue under the name net revenue: the probes find it."""
    gross = hand_rows(duckdb_connector, GROSS_BY_STATUS)
    path = write_csv(
        tmp_path / "report.csv",
        ["orders.status", "net_revenue"],
        [[s, repr(v)] for s, v in gross.items()],
    )
    report = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert report["status"] == "divergent"
    found = {c["code"]: c["detail"] for c in report["causes"]}
    assert found["different_metric"]["metric"] == "revenue"


def test_time_dimension_from_a_monthly_report(graph, duckdb_connector, tmp_path):
    """A monthly report writes `2026-04` or `2026-04-01`; both line up with the governed month."""
    by_month = hand_rows(duckdb_connector, NET_BY_MONTH)
    rows = [
        [m.strftime("%Y-%m") if i % 2 else m.date().isoformat(), repr(v)]
        for i, (m, v) in enumerate(sorted(by_month.items()))
    ]
    path = write_csv(tmp_path / "monthly.csv", ["orders.ordered_at__month", "net_revenue"], rows)
    request = QueryRequest(metrics=["net_revenue"], dimensions=["orders.ordered_at__month"])
    report = reconcile_csv(graph, duckdb_connector, request, path)
    assert report["status"] == "match", report["examples"]
    assert report["rows"]["matched"] == len(by_month)


def test_categorical_dimension_over_an_integer_column(graph, duckdb_connector, tmp_path):
    """The database returns `customer_id` as an integer and the CSV holds text: `"7"` is the
    customer 7, while `"07"` is not taken for it and stays on the report's side only."""
    data = graph.project.model_dump(mode="json")
    orders = next(m for m in data["semantic_models"] if m["name"] == "orders")
    orders["dimensions"].append(
        {"name": "customer_code", "type": "categorical", "expr": "customer_id"}
    )
    with_code = compile_project(Project.model_validate(data))
    by_customer = hand_rows(
        duckdb_connector,
        "SELECT customer_id, SUM(amount - discount) FROM public.orders GROUP BY customer_id",
    )
    assert all(isinstance(c, int) for c in by_customer)
    request = QueryRequest(metrics=["net_revenue"], dimensions=["orders.customer_code"])
    rows = [[str(c), repr(v)] for c, v in by_customer.items()]
    path = write_csv(tmp_path / "customers.csv", ["orders.customer_code", "net_revenue"], rows)
    report = reconcile_csv(with_code, duckdb_connector, request, path)
    assert report["status"] == "match", report["examples"]
    assert report["rows"]["matched"] == len(by_customer)

    first = min(by_customer)
    rows = [[f"0{c}" if c == first else str(c), repr(v)] for c, v in by_customer.items()]
    path = write_csv(tmp_path / "padded.csv", ["orders.customer_code", "net_revenue"], rows)
    report = reconcile_csv(with_code, duckdb_connector, request, path)
    assert report["rows"]["matched"] == len(by_customer) - 1
    assert report["rows"]["only_in_cosmos"] == 1 and report["rows"]["only_in_legacy"] == 1
    sides = {tuple(e["key"]): e["legacy"] is None for e in report["examples"]}
    assert sides == {(first,): True, (f"0{first}",): False}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "is empty"),
        ("orders.status,net_revenue,region\ncompleted,1,x\n", "unknown column `region`"),
        ("orders.status;net_revenue\ncompleted;1\n", "semicolon-separated"),
        ("orders.status\ncompleted\n", r"missing the metric column \['net_revenue'\]"),
        ("net_revenue\n1\n", r"missing the dimension column \['orders.status'\]"),
        ("orders.status,orders__status,net_revenue\na,a,1\n", "appears twice"),
        ("orders.status,net_revenue\ncompleted,1.820.000\n", "line 2, column `net_revenue`"),
        ('orders.status,net_revenue\ncompleted,"1,820"\n', "is not a number"),
        ("orders.status,net_revenue\ncompleted,abc\n", "'abc' is not a number"),
        ("orders.status,net_revenue\ncompleted,nan\n", "not a finite number"),
        ("orders.status,net_revenue\ncompleted,1\ncompleted,2\n", "line 3 repeats the row"),
        ("orders.status,net_revenue\ncompleted,1,2\n", "line 2 has 3 fields"),
    ],
)
def test_bad_csv_is_refused_clearly(graph, duckdb_connector, tmp_path, content, message):
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(QueryError, match=message):
        reconcile_csv(graph, duckdb_connector, BY_STATUS, path)


def test_bad_time_value_is_refused(graph, duckdb_connector, tmp_path):
    path = write_csv(
        tmp_path / "bad.csv", ["orders.ordered_at__month", "net_revenue"], [["April", "1"]]
    )
    request = QueryRequest(metrics=["net_revenue"], dimensions=["orders.ordered_at__month"])
    with pytest.raises(QueryError, match="'April' is not an ISO date"):
        reconcile_csv(graph, duckdb_connector, request, path)


def test_excel_byte_order_mark_and_empty_metric(graph, duckdb_connector, net, tmp_path):
    path = tmp_path / "excel.csv"
    body = "orders.status,net_revenue\r\n" + "".join(
        f"{s},{'' if s == 'pending' else repr(v)}\r\n" for s, v in net.items()
    )
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    report = reconcile_csv(graph, duckdb_connector, BY_STATUS, path)
    assert report["rows"]["matched"] == len(net) - 1 and report["rows"]["differing"] == 1


class _KeepOpen:
    """The session's DuckDB connector, without the close the CLI calls at the end."""

    def __init__(self, connector):
        self._connector = connector

    def __getattr__(self, name):
        return getattr(self._connector, name)

    def close(self):
        pass


@pytest.fixture
def cli(duckdb_connector, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module, "connect", lambda config: _KeepOpen(duckdb_connector))
    connections = tmp_path / "connections.yml"
    connections.write_text("data_sources:\n  local:\n    type: duckdb\n", encoding="utf-8")
    base = ["reconcile", "net_revenue", "--model", str(EXAMPLE), "--connections", str(connections)]

    def run(*args):
        return CliRunner().invoke(cli_module.app, [*base, *args])

    return run


def test_cli_reconciles_against_a_report(cli, net, tmp_path):
    rows = [[s, repr(v + 1000 if s == "pending" else v)] for s, v in net.items()]
    path = write_csv(tmp_path / "report.csv", ["orders.status", "net_revenue"], rows)
    result = cli("--by", "orders.status", "--expected", str(path))
    assert result.exit_code == 0, result.output
    assert "divergent: net_revenue" in result.output
    assert f"report   report.csv ({len(net)} rows)" in result.output
    assert "['pending']" in result.output and "(difference +1000)" in result.output

    result = cli("--by", "orders.status", "--expected", str(path), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["legacy"]["kind"] == "csv"


def test_cli_refuses_bad_csv_and_two_sources(cli, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("orders.status,revenue\ncompleted,1\n", encoding="utf-8")
    result = cli("--by", "orders.status", "--expected", str(path))
    assert result.exit_code == 1
    assert "unknown column `revenue`" in result.output

    sql = tmp_path / "legacy.sql"
    sql.write_text(NET_BY_STATUS, encoding="utf-8")
    result = cli("--expected", str(path), "--sql", str(sql))
    assert result.exit_code == 1
    assert "exactly one of --sql, --other-model or --expected" in result.output
