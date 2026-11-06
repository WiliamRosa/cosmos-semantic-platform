"""Tests for `cosmos query --csv` (issue #5)."""

import csv
import io

from typer.testing import CliRunner

from cosmos_semantic import cli as cli_module
from cosmos_semantic.cli import _csv_text, app

runner = CliRunner()

RESULT = {
    "rows": [
        {"status": "shipped", "revenue": 1234.5, "note": None},
        {"status": 'with, "comma"', "revenue": 0.0, "note": "hi"},
    ],
    "columns": [{"name": "status"}, {"name": "revenue"}, {"name": "note"}],
    "evidence": {"query_id": "q1"},
}


def test_csv_text_header_and_raw_values():
    text = _csv_text(RESULT)
    lines = text.splitlines()
    assert lines[0] == "status,revenue,note"
    # Raw values as the engine returns them: no thousands separators,
    # no fixed decimals, and None becomes an empty field.
    assert lines[1] == "shipped,1234.5,"
    # csv quoting keeps a value containing a comma intact as one field
    assert lines[2] == '"with, ""comma""",0.0,hi'


def test_csv_text_round_trip():
    parsed = list(csv.reader(io.StringIO(_csv_text(RESULT))))
    assert parsed[0] == ["status", "revenue", "note"]
    assert parsed[1] == ["shipped", "1234.5", ""]
    assert parsed[1][1] == "1234.5"
    assert parsed[2][0] == 'with, "comma"'
    assert parsed[2] == ['with, "comma"', "0.0", "hi"]


def test_query_help_shows_csv_example():
    result = runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert "--csv" in result.output
    assert "cosmos query revenue average_order_value --by orders.status --csv" in result.output


def test_query_csv_and_json_is_an_error():
    result = runner.invoke(app, ["query", "revenue", "--csv", "--json"])
    assert result.exit_code == 1
    assert "use either --csv or --json" in result.output


def test_query_csv_keeps_stdout_pure_csv(monkeypatch):
    class FakeService:
        def __init__(self, engine):
            pass

        def query(self, request, principal):
            return RESULT

    monkeypatch.setattr(cli_module.Engine, "from_paths", staticmethod(lambda *a, **k: object()))
    monkeypatch.setattr(cli_module, "load_connections", lambda *a, **k: object())
    monkeypatch.setattr("cosmos_semantic.service.SemanticService", FakeService)
    result = runner.invoke(
        app, ["query", "revenue", "--csv", "-m", "semantic", "-c", "connections.yml"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "status,revenue,note"
    assert "shipped" in result.stdout
