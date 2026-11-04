"""`cosmos init`: a starter project that works on its own, from the installed package."""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from cosmos_semantic.cli import app
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.loader import load_project
from cosmos_semantic.starter import write_starter


def test_starter_project_validates_and_answers(tmp_path, monkeypatch):
    written = write_starter(tmp_path)
    assert {p.relative_to(tmp_path).as_posix() for p in written} == {
        "semantic/sales.yml",
        "semantic/master_data.yml",
        "semantic/ontology.yml",
        "connections.yml",
        "dev-keys.yml",
    }
    graph = compile_project(load_project(tmp_path / "semantic"))
    assert "net_revenue" in graph.metrics and "active_customer" in graph.rules
    first = (tmp_path / "semantic" / "sales.yml").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# yaml-language-server: $schema=https://raw.githubusercontent.com/")
    keys = yaml.safe_load((tmp_path / "dev-keys.yml").read_text(encoding="utf-8"))
    assert keys["keys"]["dev-midwest"]["user"] == {"region": "Midwest"}

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    loaded = runner.invoke(app, ["demo-data", "--customers", "50"])
    assert loaded.exit_code == 0, loaded.output
    result = runner.invoke(app, ["query", "revenue", "--by", "orders.status", "--json"])
    assert result.exit_code == 0, result.output
    assert '"orders__status"' in result.output
    explained = runner.invoke(app, ["explain", "rule:active_customer"])
    assert explained.exit_code == 0 and "sales-leadership" in explained.output


def test_starter_never_overwrites_without_force(tmp_path):
    write_starter(tmp_path)
    (tmp_path / "connections.yml").write_text("# mine\n", encoding="utf-8")
    with pytest.raises(CosmosError, match="refusing to overwrite"):
        write_starter(tmp_path)
    assert (tmp_path / "connections.yml").read_text(encoding="utf-8") == "# mine\n"
    write_starter(tmp_path, force=True)
    assert "data_sources" in (tmp_path / "connections.yml").read_text(encoding="utf-8")
