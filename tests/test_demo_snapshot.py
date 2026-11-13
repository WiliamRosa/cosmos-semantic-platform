"""The public console demo's snapshot (console/demo/snapshot.py) builds every page it promises."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_snapshot():
    spec = importlib.util.spec_from_file_location(
        "cosmos_demo_snapshot", ROOT / "console" / "demo" / "snapshot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_writes_the_admin_pages(tmp_path):
    snapshot = _load_snapshot()
    snapshot.main(tmp_path)

    usage = json.loads((tmp_path / "v1_console_usage.json").read_text(encoding="utf-8"))
    answers = json.loads((tmp_path / "v1_console_answers.json").read_text(encoding="utf-8"))
    assert usage
    assert answers["answers"], "the demo questions should appear as answers"
    assert (tmp_path / "v1_console_overview.json").exists()
    assert list(tmp_path.glob("v1_console_members_*.json"))


def test_demo_viewer_is_admin_by_construction_not_by_name():
    snapshot = _load_snapshot()
    assert snapshot.VIEWER.local_anonymous is True
