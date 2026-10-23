"""Usage and cost report from the audit log (ADR 0009).

Who queries which metrics, through which channel (AI agents included), how often, how slowly
and at what estimated cost. Costs come from the rates configured per data source in
``connections.yml`` (``cost: {per_query, per_second, per_1k_rows, currency}``); they are an
estimate, not billing data. A query's cost is split evenly across the metrics it requested.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cosmos_semantic.errors import QueryError

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph
    from cosmos_semantic.connectors import ConnectionsFile, CostRates

ESTIMATE_NOTE = (
    "Costs are estimates from the rates in connections.yml (per query, per second of duration "
    "and per 1,000 rows returned), not billing data. Queries denied or rejected before reaching "
    "a data source cost nothing. A query's cost is split evenly across its metrics."
)


def read_audit(
    path: str | Path, since: dt.date | None = None, until: dt.date | None = None
) -> Iterator[dict[str, Any]]:
    """Query records from a JSONL audit log, optionally limited to [since, until] (UTC days)."""
    path = Path(path)
    if not path.exists():
        raise QueryError(f"audit log `{path}` does not exist")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("event") != "query":
                continue
            day = dt.datetime.fromisoformat(record["at"]).date()
            if (since and day < since) or (until and day > until):
                continue
            yield record


def _cost(record: dict[str, Any], rates: CostRates | None) -> float:
    if rates is None:
        return 0.0
    seconds = (record.get("duration_ms") or 0) / 1000
    rows = record.get("row_count") or 0
    return rates.per_query + rates.per_second * seconds + rates.per_1k_rows * rows / 1000


def _bucket() -> dict[str, Any]:
    return {
        "queries": 0,
        "errors": 0,
        "denied": 0,
        "rows": 0,
        "duration_ms": 0.0,
        "cost": 0.0,
        "last_at": None,
    }


def _add(bucket: dict[str, Any], record: dict[str, Any], cost: float) -> None:
    bucket["queries"] += 1
    if record.get("outcome") != "ok":
        error = record.get("error") or ""
        bucket["denied" if error.startswith("PolicyError") else "errors"] += 1
    bucket["rows"] += record.get("row_count") or 0
    bucket["duration_ms"] += record.get("duration_ms") or 0
    bucket["cost"] += cost
    bucket["last_at"] = max(filter(None, [bucket["last_at"], record["at"]]))


def _finish(buckets: dict[Any, dict[str, Any]], key_names: tuple[str, ...]) -> list[dict]:
    out = []
    for key, bucket in buckets.items():
        keys = key if isinstance(key, tuple) else (key,)
        item = dict(zip(key_names, keys, strict=True))
        item.update(bucket)
        if "principals" in item:
            item["principals"] = len(item["principals"])
        item["duration_ms"] = round(item["duration_ms"], 1)
        item["avg_ms"] = round(item["duration_ms"] / item["queries"], 1) if item["queries"] else 0
        item["cost"] = round(item["cost"], 6)
        out.append(item)
    return sorted(out, key=lambda i: (-i["cost"], -i["queries"], str([i[k] for k in key_names])))


def usage_report(
    audit_path: str | Path,
    connections: ConnectionsFile | None = None,
    graph: SemanticGraph | None = None,
    *,
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> dict[str, Any]:
    sources = connections.data_sources if connections else {}
    currencies = {cfg.cost.currency for cfg in sources.values() if cfg.cost}
    if len(currencies) > 1:
        raise QueryError(f"data sources use different currencies: {', '.join(sorted(currencies))}")

    total = _bucket()
    metrics: dict[str, dict[str, Any]] = {}
    rules: dict[str, dict[str, Any]] = {}
    principals: dict[tuple, dict[str, Any]] = {}
    channels: dict[str, dict[str, Any]] = {}
    data_sources: dict[str, dict[str, Any]] = {}
    slowest: list[dict[str, Any]] = []

    for record in read_audit(audit_path, since, until):
        source = record.get("data_source")
        config = sources.get(source) if source else None
        cost = _cost(record, config.cost if config else None)
        principal = record.get("principal") or {}
        who = (principal.get("subject"), principal.get("channel"))
        requested = (record.get("request") or {}).get("metrics", [])

        _add(total, record, cost)
        _add(principals.setdefault(who, _bucket()), record, cost)
        _add(channels.setdefault(who[1] or "unknown", _bucket()), record, cost)
        _add(data_sources.setdefault(source or "unknown", _bucket()), record, cost)
        for name in requested:
            bucket = metrics.setdefault(
                name, {**_bucket(), "principals": set(), "agent_queries": 0}
            )
            _add(bucket, record, cost / len(requested))
            bucket["principals"].add(who)
            bucket["agent_queries"] += who[1] == "mcp"
        for name in record.get("rules", []):
            _add(rules.setdefault(name, _bucket()), record, 0.0)
        if record.get("outcome") == "ok":
            slowest.append(
                {
                    "query_id": record.get("query_id"),
                    "metrics": requested,
                    "subject": who[0],
                    "channel": who[1],
                    "duration_ms": record.get("duration_ms"),
                    "rows": record.get("row_count"),
                }
            )

    report: dict[str, Any] = {
        "period": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "currency": next(iter(currencies), None),
        "note": ESTIMATE_NOTE,
        "total": _finish({"all": total}, ("scope",))[0],
        "by_metric": _finish(metrics, ("metric",)),
        "by_rule": [{k: v for k, v in r.items() if k != "cost"} for r in _finish(rules, ("rule",))],
        "by_principal": _finish(principals, ("subject", "channel")),
        "by_channel": _finish(channels, ("channel",)),
        "by_data_source": _finish(data_sources, ("data_source",)),
        "slowest": sorted(slowest, key=lambda q: -(q["duration_ms"] or 0))[:5],
    }
    if graph is not None:
        used = set(metrics)
        report["unused_metrics"] = sorted(set(graph.metrics) - used)
        report["unknown_metrics"] = sorted(
            m for m in used if m not in graph.metrics and m not in graph.measure_owner
        )
    return report
