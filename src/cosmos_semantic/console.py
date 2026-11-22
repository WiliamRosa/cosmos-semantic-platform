"""Read-only views for the console (ADR 0012): what already exists, for people who decide.

Everything here is computed from the semantic model, the audit log (``COSMOS_AUDIT_LOG``) and
the check runs log (``COSMOS_RUNS_LOG``). Nothing is written and no data source is queried,
except by ``replay``, which goes through the service as before.

Health (state and reasons per member, see ``cosmos_semantic.health``) is shared with
``explain``. Its reasons are codes with parameters, never sentences, so the console can show
them in English or Portuguese.

Who sees what: any authenticated user sees the model, the graph and the health. The audit
records (who asked what) and usage per person are shown only to the subjects listed in
``COSMOS_CONSOLE_ADMINS``; everyone else sees only their own answers.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cosmos_semantic.checks import run_ok
from cosmos_semantic.errors import PolicyError, QueryError
from cosmos_semantic.expressions import describe_filter
from cosmos_semantic.health import RANK, Health

if TYPE_CHECKING:
    from cosmos_semantic.planner import Principal
    from cosmos_semantic.service import SemanticService


def _worst(*states: str) -> str:
    return max(states, key=RANK.__getitem__) if states else "unchecked"


class Console:
    def __init__(
        self,
        service: SemanticService,
        *,
        runs_path: str | Path | None = None,
        admins: set[str] | None = None,
    ) -> None:
        self.service = service
        self.graph = service.engine.graph
        # The service's health (COSMOS_RUNS_LOG), unless another runs log is given here.
        self.health = (
            service.health if runs_path is None else Health(self.graph, runs_path=runs_path)
        )
        self.runs_path = self.health.runs_path
        # The same admins as the service (COSMOS_CONSOLE_ADMINS), unless given here.
        self.admins = service.admins if admins is None else admins

    # -- access ------------------------------------------------------------------

    def is_admin(self, principal: Principal) -> bool:
        # `--allow-anonymous` is for local development; the person at the keyboard sees all.
        if principal.local_anonymous:
            return True
        return principal.subject is not None and principal.subject in self.admins

    def _require_admin(self, principal: Principal) -> None:
        if not self.is_admin(principal):
            raise PolicyError("only console admins (COSMOS_CONSOLE_ADMINS) see this")

    # -- model facts (fixed for the life of the process) -------------------------

    @cached_property
    def _knowledge(self):
        from cosmos_semantic.knowledge import build_graph

        return build_graph(self.graph)

    @property
    def _rule_tests(self) -> dict[str, dict[str, Any]]:
        return self.health.rule_tests

    @cached_property
    def _owners(self) -> dict[str, str]:
        from cosmos_semantic.knowledge import owned_items

        return dict(owned_items(self.graph))

    # -- logs ----------------------------------------------------------------------

    def _audit(self) -> list[dict[str, Any]]:
        from cosmos_semantic.usage import read_audit

        path = self.service.audit_path
        if path is None or not path.exists():
            return []
        return list(read_audit(path))

    def _runs(self) -> list[dict[str, Any]]:
        return self.health.runs()

    # -- health ----------------------------------------------------------------------

    def _health(self) -> dict[str, dict[str, Any]]:
        """State and reasons per knowledge-graph node id (`metric:x`, `rule:y`, ...)."""
        return self.health.states()

    # -- views -------------------------------------------------------------------------

    def overview(self, principal: Principal) -> dict[str, Any]:
        g, project = self.graph, self.graph.project
        health = self._health()
        metric_states = Counter(
            v["state"] for k, v in health.items() if k.startswith("metric:") and k[7:] in g.metrics
        )
        rule_tests = self._rule_tests.values()
        runs = self._runs()
        latest = runs[-1] if runs else None
        audit = self._audit()
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        recent = [r for r in audit if dt.datetime.fromisoformat(r["at"]) >= since]
        by_day: dict[str, Counter] = defaultdict(Counter)
        for record in recent:
            day = record["at"][:10]
            by_day[day]["queries"] += 1
            by_day[day]["agent"] += (record.get("principal") or {}).get("channel") == "mcp"
            by_day[day]["errors"] += record.get("outcome") != "ok"
        top = Counter(
            m
            for r in recent
            if r.get("outcome") == "ok"
            for m in (r.get("request") or {}).get("metrics", [])
        )
        durations = sorted(r["duration_ms"] for r in recent if r.get("duration_ms") is not None)
        return {
            "timezone": project.timezone or "UTC",
            "model_hash": self.service.evidence.model_hash,
            "counts": {
                "models": len(g.models),
                "metrics": len(g.metrics),
                "measures": len(g.measure_owner),
                "rules": len(g.rules),
                "concepts": len(project.glossary),
                "processes": len(project.processes),
                "systems": len(project.systems),
                "teams": len(project.teams),
            },
            "governance": {
                "metrics_with_owner": sum(1 for m in g.metrics if f"metric:{m}" in self._owners),
                "metrics_described": sum(1 for m in g.metrics.values() if m.description),
                "rules_with_owner": sum(1 for r in g.rules if f"rule:{r}" in self._owners),
                "rules_with_tests": sum(1 for t in rule_tests if t["total"]),
                "rule_tests_total": sum(t["total"] for t in rule_tests),
                "rule_tests_failed": sum(t["failed"] for t in rule_tests),
                "metric_health": {s: metric_states.get(s, 0) for s in RANK},
            },
            "latest_check": _run_summary(latest) if latest else None,
            "checks": [_run_summary(r) for r in runs[-30:]],
            "usage": {
                "available": self.service.audit_path is not None,
                "queries_30d": len(recent),
                "agent_queries_30d": sum(c["agent"] for c in by_day.values()),
                "errors_30d": sum(c["errors"] for c in by_day.values()),
                "p50_ms": _percentile(durations, 50),
                "p95_ms": _percentile(durations, 95),
                "by_day": [{"day": d, **by_day[d]} for d in sorted(by_day)],
                "top_metrics": [{"metric": m, "queries": n} for m, n in top.most_common(8)],
            },
            "viewer": {"subject": principal.subject, "admin": self.is_admin(principal)},
        }

    def processes(self) -> list[dict[str, Any]]:
        health = self._health()
        out = []
        for process in self.graph.project.processes:
            node = f"process:{process.name}"
            sub = self._knowledge.focus(node)
            kinds = Counter(n["kind"] for n in sub.nodes.values())
            states = Counter(health.get(n, {}).get("state", "unchecked") for n in sub.nodes)
            out.append(
                {
                    "name": process.name,
                    "description": process.description,
                    "owner": process.owner,
                    "systems": process.systems,
                    "state": health[node]["state"],
                    "reasons": health[node]["reasons"],
                    "metrics": kinds.get("metric", 0),
                    "rules": kinds.get("rule", 0),
                    "failing": states.get("failing", 0),
                }
            )
        return out

    def graph_view(self, focus: str | None = None) -> dict[str, Any]:
        """The knowledge graph (or the part around ``focus``) with health and usage per node."""
        from cosmos_semantic.knowledge import owner_of

        kg = self._knowledge.focus(focus) if focus else self._knowledge
        health = self._health()
        usage = Counter(
            m for r in self._audit() for m in (r.get("request") or {}).get("metrics", [])
        )
        nodes = []
        for node in kg.as_dict()["nodes"]:
            item = dict(node)
            state = health.get(node["id"])
            if state:
                item["state"], item["reasons"] = state["state"], state["reasons"]
            elif node["kind"] in ("metric", "rule"):
                item["state"], item["reasons"] = "unchecked", []
            if node["kind"] in ("metric", "measure"):
                item["queries"] = usage.get(node["name"], 0)
            if "owner" not in item and (owner := owner_of(self.graph, node["id"])):
                item["owner"] = owner  # measures and dimensions inherit their model's owner
            nodes.append(item)
        return {"focus": focus, "nodes": nodes, "edges": kg.edges}

    def catalog(self) -> dict[str, Any]:
        from cosmos_semantic.knowledge import owner_of

        g = self.graph
        health = self._health()
        usage = Counter(
            m for r in self._audit() for m in (r.get("request") or {}).get("metrics", [])
        )
        concepts_for: dict[str, list[str]] = defaultdict(list)
        for concept in g.project.glossary:
            if concept.maps_to:
                concepts_for[concept.maps_to].append(concept.concept)

        def row(member: str, kind: str, name: str, description: str | None, **extra):
            state = health.get(member, {"state": "unchecked", "reasons": []})
            return {
                "id": member,
                "kind": kind,
                "name": name,
                "description": description,
                "owner": owner_of(g, member),
                "state": state["state"],
                "reasons": state["reasons"],
                "concepts": concepts_for.get(member, []),
                **extra,
            }

        items = [
            row(f"metric:{n}", "metric", n, m.description, type=m.type, queries=usage.get(n, 0))
            for n, m in g.metrics.items()
        ]
        items += [
            row(
                f"measure:{n}",
                "measure",
                n,
                g.models[owner].measures[n].description,
                type=g.models[owner].measures[n].agg,
                model=owner,
                queries=usage.get(n, 0),
            )
            for n, owner in g.measure_owner.items()
        ]
        items += [
            row(
                f"rule:{n}",
                "rule",
                n,
                r.description,
                type=r.kind,
                versions=len(r.history) + 1,
                tests=self._rule_tests.get(n, {}).get("total", 0),
            )
            for n, r in g.rules.items()
        ]
        items += [
            {
                "id": f"concept:{c.concept}",
                "kind": "concept",
                "name": c.concept,
                "label": c.pref_label,
                "synonyms": c.alt_labels,
                "description": c.definition,
                "maps_to": c.maps_to,
            }
            for c in g.project.glossary
        ]
        return {"items": items}

    def member(self, member_id: str, principal: Principal) -> dict[str, Any]:
        from cosmos_semantic.impact import analyze

        detail = self.service.catalog.explain(member_id)
        kind = detail["kind"]
        key = f"{kind}:{detail['name']}"
        health = self._health().get(key)
        out: dict[str, Any] = {"detail": detail, "health": health}
        if kind in ("metric", "measure"):
            out["formula"] = self.service.evidence.formula(detail["name"])
        if kind == "rule":
            out["tests"] = self._rule_tests.get(detail["name"])
        if kind in ("metric", "measure", "rule", "model", "system", "dimension"):
            # Consumers (who queried the affected members) come from the audit log: admins only.
            audit = self.service.audit_path if self.is_admin(principal) else None
            if audit is not None and not audit.exists():
                audit = None
            try:
                out["impact"] = analyze(self.graph, [key], audit_path=audit)
            except QueryError:
                out["impact"] = None
        runs = self._runs()
        if kind in ("metric", "measure"):
            # The latest result of each case: the run history lives on the checks page.
            latest: dict[str, dict[str, Any]] = {}
            for run in runs:
                for case in run["reconciliations"]:
                    if detail["name"] in case["metrics"]:
                        latest[case["id"]] = {"at": run["at"], "run_id": run["run_id"], **case}
            out["reconciliations"] = list(latest.values())
        recent = [
            r
            for r in reversed(self._audit())
            if self._mentions(r, kind, detail["name"])
            and (self.is_admin(principal) or _subject(r) == principal.subject)
        ]
        out["recent_answers"] = [_answer_row(r) for r in recent[:10]]
        return out

    @staticmethod
    def _mentions(record: dict[str, Any], kind: str, name: str) -> bool:
        request = record.get("request") or {}
        if kind in ("metric", "measure"):
            return name in request.get("metrics", [])
        if kind == "rule":
            return name in record.get("rules", [])
        return False

    def answers(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        offset: int = 0,
        metric: str | None = None,
    ) -> dict[str, Any]:
        """Audited answers the principal may see, newest first, one page at a time.

        ``total`` counts every visible answer, so the console can page through all of them.
        """
        admin = self.is_admin(principal)
        visible = [
            record
            for record in reversed(self._audit())
            if (admin or _subject(record) == principal.subject)
            and (not metric or metric in (record.get("request") or {}).get("metrics", []))
        ]
        limit, offset = max(limit, 1), max(offset, 0)
        return {
            "scope": "everyone" if admin else "yours",
            "answers": [_answer_row(r) for r in visible[offset : offset + limit]],
            "total": len(visible),
            "offset": offset,
            "limit": limit,
        }

    def answer(self, query_id: str, principal: Principal) -> dict[str, Any]:
        record = self.service.find_audit(query_id)
        if not self.is_admin(principal) and _subject(record) != principal.subject:
            raise PolicyError("this answer belongs to another user")
        request = record.get("request") or {}
        formulas = {}
        for name in request.get("metrics", []):
            try:
                formulas[name] = self.service.evidence.formula(name)
            except (QueryError, KeyError):
                formulas[name] = None
        rules = []
        for name in record.get("rules", []):
            try:
                rules.append(self.service.catalog.explain(f"rule:{name}"))
            except QueryError:
                rules.append({"name": name, "missing": True})
        return {
            **_answer_row(record),
            "request": request,
            "user_context": (record.get("principal") or {}).get("user_context"),
            "sql": record.get("sql"),
            "hashes": {k: record.get(k) for k in ("model_hash", "sql_hash", "result_hash")},
            "model_changed": record.get("model_hash") != self.service.evidence.model_hash,
            "formulas": formulas,
            "rules": rules,
        }

    def usage(self, principal: Principal) -> dict[str, Any]:
        from cosmos_semantic.usage import usage_report

        self._require_admin(principal)
        if self.service.audit_path is None or not self.service.audit_path.exists():
            return {"available": False}
        report = usage_report(self.service.audit_path, self.service.engine.connections, self.graph)
        return {"available": True, **report}

    def runs(self, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Check runs, newest first, one page at a time; ``total`` counts every run."""
        runs = list(reversed(self._runs()))
        limit, offset = max(limit, 1), max(offset, 0)
        return {
            "available": self.runs_path is not None,
            "runs": runs[offset : offset + limit],
            "total": len(runs),
            "offset": offset,
            "limit": limit,
        }


def _subject(record: dict[str, Any]) -> str | None:
    return (record.get("principal") or {}).get("subject")


def _answer_row(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request") or {}
    principal = record.get("principal") or {}
    return {
        "query_id": record.get("query_id"),
        "at": record.get("at"),
        "subject": principal.get("subject"),
        "channel": principal.get("channel"),
        "metrics": request.get("metrics", []),
        "dimensions": request.get("dimensions", []),
        "filters": [describe_filter(f) for f in request.get("filters", [])],
        "as_of": request.get("as_of"),
        "data_source": record.get("data_source"),
        "rules": record.get("rules", []),
        "outcome": record.get("outcome"),
        "error": record.get("error"),
        "duration_ms": record.get("duration_ms"),
        "row_count": record.get("row_count"),
    }


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    outcomes = Counter(r["outcome"] for r in run["reconciliations"])
    return {
        "run_id": run["run_id"],
        "at": run["at"],
        "ok": run_ok(run),
        "data_source": run.get("data_source"),
        "rule_tests": {k: run["rule_tests"][k] for k in ("total", "passed", "failed")},
        "reconciliations": {k: outcomes.get(k, 0) for k in ("pass", "fail", "error", "skipped")},
        "duration_ms": run.get("duration_ms"),
    }


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, round(pct / 100 * (len(values) - 1)))
    return values[index]
