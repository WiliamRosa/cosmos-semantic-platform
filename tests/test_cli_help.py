"""CLI help is plain text in tests, on every machine and on CI (see conftest.py)."""

from __future__ import annotations

from typer.testing import CliRunner

from cosmos_semantic.cli import app


def test_help_has_no_terminal_codes_and_does_not_wrap_options():
    result = CliRunner().invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert "\x1b[" not in result.output
    assert "--json" in result.output
    assert "Print rows and evidence as JSON." in result.output
