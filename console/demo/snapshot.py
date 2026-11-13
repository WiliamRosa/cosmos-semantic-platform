"""Builds the data of the public console demo (ADR 0012): no server, synthetic data only.

Runs the example model on an in-memory DuckDB with generated data, answers a set of questions
through the engine (as people over the API and as AI agents over MCP), records a check run,
and writes every console endpoint's JSON to a folder. The demo build of the console reads
those files instead of calling the API.

    uv run python console/demo/snapshot.py console/public/demo-data

File names follow ``slug`` below; ``console/lib/api.ts`` computes the same names.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

from cosmos_semantic import demo_data
from cosmos_semantic.checks import append_run, run_checks
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import ConnectionsFile, DataSourceConfig, connect
from cosmos_semantic.console import Console
from cosmos_semantic.engine import Engine
from cosmos_semantic.errors import CosmosError
from cosmos_semantic.evidence import EvidenceBuilder
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import Principal, QueryRequest
from cosmos_semantic.reconcile import load_zoo
from cosmos_semantic.service import SemanticService, jsonable

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "ecommerce"

# Who asks what. Agents go through the MCP channel, as Claude or another assistant would.
ANALYST = Principal(subject="analyst@example.com", channel="api", user={"region": "Midwest"})
FINANCE = Principal(subject="finance@example.com", channel="api", user={"region": "Northeast"})
SALES_AGENT = Principal(subject="sales-assistant", channel="mcp", user={"region": "Midwest"})
CFO_AGENT = Principal(subject="cfo-copilot", channel="mcp", user={"region": "Northeast"})
QUESTIONS = [
    (ANALYST, {"metrics": ["revenue"], "dimensions": ["orders.status"]}),
    (
        ANALYST,
        {
            "metrics": ["completed_revenue", "average_order_value"],
            "dimensions": ["orders.ordered_at__month"],
        },
    ),
    (FINANCE, {"metrics": ["net_revenue", "discount_rate"], "dimensions": ["products.category"]}),
    (
        FINANCE,
        {"metrics": ["paid_value", "collection_gap"], "dimensions": ["orders.ordered_at__month"]},
    ),
    (SALES_AGENT, {"metrics": ["completed_revenue"], "dimensions": ["customers.segment"]}),
    (SALES_AGENT, {"metrics": ["customer_count"], "filters": ["{rule.active_customer}"]}),
    (SALES_AGENT, {"metrics": ["revenue"], "dimensions": ["orders.order_size_band"]}),
    (SALES_AGENT, {"metrics": ["orders_per_buyer"], "dimensions": ["customers.segment"]}),
    (CFO_AGENT, {"metrics": ["net_revenue"], "dimensions": ["orders.ordered_at__month"]}),
    (CFO_AGENT, {"metrics": ["revenue_year_to_date"], "dimensions": ["orders.ordered_at__month"]}),
    (CFO_AGENT, {"metrics": ["collection_gap"]}),
    # Refused on purpose: personal data never reaches agents.
    (CFO_AGENT, {"metrics": ["customer_count"], "dimensions": ["customers.email"]}),
]
# The demo shows everything, as the local user of `cosmos serve --allow-anonymous` sees it. Admin
# rights come from how a principal was created, never from its name (see service.py).
VIEWER = Principal(subject="anonymous", channel="api", local_anonymous=True)


def slug(path: str) -> str:
    """``/v1/console/members/metric:revenue`` -> ``v1_console_members_metric_revenue``."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", path.strip("/"))


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    graph = compile_project(load_project(EXAMPLE / "semantic"))
    connector = connect(DataSourceConfig(type="duckdb"))
    demo_data.load(connector, demo_data.generate(2000, seed=7, anchor=dt.date.today()))
    engine = Engine(graph, ConnectionsFile(data_sources={"local": DataSourceConfig(type="duckdb")}))
    engine._connectors["local"] = connector
    service = SemanticService(engine, audit_path=work / "audit.jsonl")

    for principal, request in QUESTIONS:
        try:
            service.query(QueryRequest.model_validate(request), principal)
        except CosmosError:
            pass  # refusals are recorded in the audit log, which is the point

    run = run_checks(
        graph,
        model_hash=EvidenceBuilder(graph).model_hash,
        connector=connector,
        source="demo (DuckDB)",
        cases=load_zoo(EXAMPLE / "reconciliations.yml"),
    )
    append_run(work / "runs.jsonl", run)

    console = Console(service, runs_path=work / "runs.jsonl", admins=set())
    pages: dict[str, object] = {
        "/v1/console/overview": console.overview(VIEWER),
        "/v1/console/processes": console.processes(),
        "/v1/console/graph": console.graph_view(None),
        "/v1/console/catalog": console.catalog(),
        "/v1/console/answers": console.answers(VIEWER, limit=50),
        "/v1/console/runs": console.runs(),
        "/v1/console/usage": console.usage(VIEWER),
    }
    for process in graph.project.processes:
        focus = f"process:{process.name}"
        pages[f"/v1/console/graph?focus={focus}"] = console.graph_view(focus)
    members = [item["id"] for item in pages["/v1/console/catalog"]["items"]]  # type: ignore[index]
    members += [f"process:{p.name}" for p in graph.project.processes]
    members += [f"system:{s.name}" for s in graph.project.systems]
    for member in members:
        try:
            pages[f"/v1/console/members/{member}"] = console.member(member, VIEWER)
        except CosmosError:
            pass
    for answer in pages["/v1/console/answers"]["answers"]:  # type: ignore[index]
        if answer["query_id"]:
            pages[f"/v1/console/answers/{answer['query_id']}"] = console.answer(
                answer["query_id"], VIEWER
            )

    for path, body in pages.items():
        text = json.dumps(body, default=jsonable, ensure_ascii=False)
        (out / f"{slug(path)}.json").write_text(text, encoding="utf-8")
    print(f"{len(pages)} demo pages in {out}")
    connector.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "console" / "public" / "demo-data")
