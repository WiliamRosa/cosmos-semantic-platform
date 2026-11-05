"""Check runs (ADR 0012): rule tests and reconciliations, recorded over time.

``cosmos check`` runs every business rule's tests and, given reconciliation cases, compares
each governed metric with the legacy SQL a team already uses. Each run appends one record to
a JSONL log (``COSMOS_RUNS_LOG``), so the console can show health over time instead of only
the last terminal output. Schedule it in CI or cron; it reads the data source and never
writes to it.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cosmos_semantic.errors import CosmosError
from cosmos_semantic.ontology import run_rule_tests

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph
    from cosmos_semantic.connectors import Connector
    from cosmos_semantic.reconcile import ZooCase


def run_checks(
    graph: SemanticGraph,
    *,
    model_hash: str,
    connector: Connector | None = None,
    source: str | None = None,
    cases: list[ZooCase] | None = None,
) -> dict[str, Any]:
    """One check run. Reconciliations need a connector; rule tests run on DuckDB alone."""
    from cosmos_semantic.reconcile import check_zoo_case

    started = time.perf_counter()
    tests = run_rule_tests(graph.project)
    reconciliations = []
    for case in cases or []:
        item: dict[str, Any] = {
            "id": case.id,
            "title": case.title,
            "metrics": case.request.metrics,
            "dimensions": case.request.dimensions,
        }
        if connector is None:
            item.update(outcome="skipped", detail="no data source")
        else:
            try:
                ok, report = check_zoo_case(case, graph, connector)
                item.update(
                    outcome="pass" if ok else "fail",
                    status=report["status"],
                    # One entry per cause: probes can report the same code more than once.
                    causes=list(
                        {
                            c["code"]: {"code": c["code"], "summary": c["summary"]}
                            for c in reversed(report["causes"])
                        }.values()
                    )[::-1],
                    rows=report.get("rows"),
                    totals=report.get("totals"),
                    expected=case.expect.model_dump() if case.expect else None,
                )
            except CosmosError as exc:
                item.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
        reconciliations.append(item)
    failed = [t for t in tests if not t.passed]
    return {
        "event": "check",
        "run_id": f"run_{uuid.uuid4().hex[:16]}",
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "model_hash": model_hash,
        "data_source": source,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "rule_tests": {
            "total": len(tests),
            "passed": len(tests) - len(failed),
            "failed": len(failed),
            "failures": [
                {
                    "rule": t.rule,
                    "test": t.index,
                    "version": t.version.isoformat() if t.version else None,
                    "detail": t.detail,
                }
                for t in failed
            ],
        },
        "reconciliations": reconciliations,
    }


def run_ok(run: dict[str, Any]) -> bool:
    """A run is healthy when every rule test passes and no reconciliation fails or errors."""
    bad = {"fail", "error"}
    return run["rule_tests"]["failed"] == 0 and not any(
        r["outcome"] in bad for r in run["reconciliations"]
    )


def append_run(path: str | Path, run: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(run, ensure_ascii=False, default=str) + "\n")


def read_runs(path: str | Path | None) -> Iterator[dict[str, Any]]:
    """Check runs in the order they were recorded; nothing when there is no log yet."""
    if path is None or not Path(path).exists():
        return
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                record = json.loads(line)
                if record.get("event") == "check":
                    yield record
