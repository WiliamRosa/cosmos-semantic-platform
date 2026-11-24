"""Health of each member: is this number checked? Shared by the console and by ``explain``.

Computed from the model's rule tests and the latest check run (``COSMOS_RUNS_LOG``). Reasons
are codes with parameters (``{"code": "rule_tests_fail", "failed": 1, "total": 4}``), never
sentences. Health holds no audit data, so any authenticated user may see it.

The vocabulary, the same everywhere:

- ``failing``: a rule test fails, or a reconciliation of the metric fails or errors.
- ``attention``: the member depends on something failing.
- ``healthy``: tests pass and, for a metric, its last reconciliation matched.
- ``unchecked``: nothing verifies it yet (no rule tests, or a metric never reconciled).

Every state also says how fresh it is: ``last_reconciled_at`` (when the latest check run
reconciled the metric or measure) and ``last_tests_run_at`` (when the latest check run ran the
rule tests), ISO timestamps or null. With ``health.max_age`` in the project, a metric whose
last reconciliation matched but is older than that needs ``attention``
(``reconciliation_stale``). Rule tests run again each time health is computed, so they are
never stale.

A run of one case from the API or MCP (DEC-45) is marked ``partial``: it updates the cases it
ran and leaves the others as the latest full run (``cosmos check``) recorded them. Each case
counts with the time of the run that last ran it; a metric's ``last_reconciled_at`` is the
newest of its cases, and staleness is judged on the oldest.

A reconciliation passes or fails within the tolerance its metric declares (DEC-39). When that
tolerance is the metric's own, the ``reconciled`` and ``reconciliation_failed`` reasons carry
it (``tolerance: {abs_tolerance, rel_tolerance, source: "metric"}``); under the default
tolerance they are unchanged.
"""

from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cosmos_semantic.checks import read_runs
from cosmos_semantic.spec import parse_max_age

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph

RANK = {"failing": 3, "attention": 2, "unchecked": 1, "healthy": 0}


def _parse_at(value: Any) -> dt.datetime | None:
    """A run's ``at`` as an aware datetime (naive ones are UTC); None when it cannot be read."""
    try:
        at = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=dt.UTC)


def _declared_tolerance(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """``{"tolerance": ...}`` when a case ran under the metric's own tolerance (DEC-39), else
    nothing, so reasons under the default tolerance keep their shape."""
    for case in cases:
        tolerance = case.get("tolerance")
        if isinstance(tolerance, dict) and tolerance.get("source") == "metric":
            return {"tolerance": tolerance}
    return {}


def _sort_at(value: Any) -> dt.datetime:
    return _parse_at(value) or dt.datetime.min.replace(tzinfo=dt.UTC)


def _latest_cases(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The latest result of each case, each with the ``at`` of its run: the partial runs
    (DEC-45) newer than the latest full run, then that full run. Without partial runs, exactly
    the latest run's reconciliations."""
    seen: dict[str, dict[str, Any]] = {}
    for run in reversed(runs):
        for case in run.get("reconciliations") or []:
            seen.setdefault(case["id"], {**case, "at": run.get("at")})
        if not run.get("partial"):
            break
    return list(seen.values())


class Health:
    def __init__(self, graph: SemanticGraph, *, runs_path: str | Path | None = None) -> None:
        self.graph = graph
        path = runs_path or os.environ.get("COSMOS_RUNS_LOG")
        self.runs_path = Path(path) if path else None

    @cached_property
    def deps(self):
        from cosmos_semantic.impact import DependencyGraph

        return DependencyGraph(self.graph)

    @cached_property
    def rule_tests(self) -> dict[str, dict[str, Any]]:
        from cosmos_semantic.ontology import run_rule_tests

        out: dict[str, dict[str, Any]] = {
            name: {"total": 0, "failed": 0, "failures": []} for name in self.graph.rules
        }
        for result in run_rule_tests(self.graph.project):
            item = out.setdefault(result.rule, {"total": 0, "failed": 0, "failures": []})
            item["total"] += 1
            if not result.passed:
                item["failed"] += 1
                item["failures"].append(result.detail)
        return out

    def runs(self) -> list[dict[str, Any]]:
        return list(read_runs(self.runs_path))

    def states(self) -> dict[str, dict[str, Any]]:
        """State and reasons per knowledge-graph node id (`metric:x`, `rule:y`, ...)."""
        states: dict[str, dict[str, Any]] = {}
        failing_rules = []
        for name, tests in self.rule_tests.items():
            if tests["failed"]:
                state = "failing"
                reason = {
                    "code": "rule_tests_fail",
                    "failed": tests["failed"],
                    "total": tests["total"],
                }
                failing_rules.append(f"rule:{name}")
            elif tests["total"]:
                state, reason = "healthy", {"code": "rule_tests_pass", "total": tests["total"]}
            else:
                state, reason = "unchecked", {"code": "no_tests"}
            states[f"rule:{name}"] = {"state": state, "reasons": [reason]}

        runs = self.runs()
        latest = runs[-1] if runs else None
        tests_at = latest.get("at") if latest and latest.get("rule_tests") else None
        settings = self.graph.project.health
        max_age = parse_max_age(settings.max_age) if settings and settings.max_age else None
        now = dt.datetime.now(dt.UTC)
        reconciled: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for case in _latest_cases(runs):
            for metric in case["metrics"]:
                reconciled[metric].append(case)
        members = [f"metric:{n}" for n in self.graph.metrics]
        members += [f"measure:{n}" for n in self.graph.measure_owner]
        for member in members:
            cases = reconciled.get(member.partition(":")[2], [])
            if not cases:
                states[member] = {"state": "unchecked", "reasons": [{"code": "never_reconciled"}]}
                continue
            ats = [c["at"] for c in cases if c.get("at")]
            at = max(ats, key=_sort_at) if ats else None
            oldest = min(ats, key=_sort_at) if ats else None
            bad = [c for c in cases if c["outcome"] in ("fail", "error")]
            if bad:
                reasons = [
                    {
                        "code": "reconciliation_failed",
                        "case": c["id"],
                        "title": c["title"],
                        "at": c.get("at"),
                    }
                    | _declared_tolerance([c])
                    for c in bad
                ]
                states[member] = {"state": "failing", "reasons": reasons, "last_reconciled_at": at}
            else:
                reasons = [
                    {"code": "reconciled", "count": len(cases), "at": at}
                    | _declared_tolerance(cases)
                ]
                state = "healthy"
                if max_age is not None:
                    when = _parse_at(oldest)
                    if when is None or now - when > max_age:
                        state = "attention"
                        reasons.append(
                            {
                                "code": "reconciliation_stale",
                                "at": oldest,
                                "max_age": settings.max_age,
                            }
                        )
                states[member] = {"state": state, "reasons": reasons, "last_reconciled_at": at}

        # Whatever depends on a failing rule, metric or measure needs attention.
        failing = list(
            dict.fromkeys(failing_rules + [k for k, v in states.items() if v["state"] == "failing"])
        )
        for source in failing:
            impact = self.deps.impact([source])
            affected = (
                [f"metric:{m}" for m in impact.metrics]
                + [f"measure:{m}" for m in impact.measures]
                + [f"process:{p}" for p in impact.processes]
            )
            for node in affected:
                if node == source:
                    continue
                current = states.setdefault(node, {"state": "healthy", "reasons": []})
                if RANK[current["state"]] < RANK["attention"]:
                    current["state"] = "attention"
                current["reasons"].append({"code": "depends_on_failing", "member": source})
        for process in self.graph.project.processes:
            states.setdefault(f"process:{process.name}", {"state": "healthy", "reasons": []})
        for state in states.values():
            state.setdefault("last_reconciled_at", None)
            state["last_tests_run_at"] = tests_at
        return states
