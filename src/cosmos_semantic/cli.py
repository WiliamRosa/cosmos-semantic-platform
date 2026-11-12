"""Command line: ``cosmos validate``, ``query``, ``sql``, ``schema``, ``test``, ``import``,
``demo-data``, ``reference``, ``serve`` and ``mcp``."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from cosmos_semantic import errors
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import DataSourceConfig, connect, load_connections
from cosmos_semantic.engine import Engine
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import Principal, QueryRequest

app = typer.Typer(
    help="Cosmos Semantic: a semantic layer with its own engine.", no_args_is_help=True
)

ModelOpt = Annotated[Path, typer.Option("--model", "-m", help="Semantic model file or folder.")]
ConnOpt = Annotated[Path, typer.Option("--connections", "-c", help="connections.yml file.")]
SourceOpt = Annotated[
    str | None, typer.Option("--source", "-s", help="Data source in connections.yml.")
]
UserOpt = Annotated[
    list[str] | None, typer.Option("--user", "-u", help="User context, such as region=Midwest.")
]
DEFAULT_CASES = [Path("reference/queries.yml"), Path("reference/queries_advanced.yml")]


@app.callback()
def _setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _principal(user: list[str] | None, channel: str = "cli") -> Principal:
    values = {}
    for item in user or []:
        key, sep, value = item.partition("=")
        if not sep:
            _fail(ValueError(f"--user expects key=value, got `{item}`"))
        values[key] = value
    return Principal(user=values, channel=channel)  # type: ignore[arg-type]


def _request(
    metrics: list[str], dimensions, filters, order_by, limit, as_of, rules_as_of=None
) -> QueryRequest:
    return QueryRequest(
        metrics=metrics,
        dimensions=dimensions or [],
        filters=filters or [],
        order_by=order_by or [],
        limit=limit,
        as_of=dt.date.fromisoformat(as_of) if as_of else None,
        rules_as_of=dt.date.fromisoformat(rules_as_of) if rules_as_of else None,
    )


DimOpt = Annotated[list[str] | None, typer.Option("--by", "-b", help="Dimensions to group by.")]
WhereOpt = Annotated[list[str] | None, typer.Option("--where", "-w", help="Filter expressions.")]
OrderOpt = Annotated[
    list[str] | None, typer.Option("--order", help="Order by; `-` for descending.")
]
LimitOpt = Annotated[int | None, typer.Option("--limit", "-l")]
AsOfOpt = Annotated[str | None, typer.Option("--as-of", help="Reference date YYYY-MM-DD.")]
RulesAsOfOpt = Annotated[
    str | None,
    typer.Option(
        "--rules-as-of", help="Use the business rule versions in effect on this date (YYYY-MM-DD)."
    ),
]
AgentOpt = Annotated[bool, typer.Option(help="Query as an AI agent (MCP channel).")]


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the project.")] = Path("."),
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Creates a starter project with the e-commerce example model and a local DuckDB source."""
    from cosmos_semantic.starter import write_starter

    try:
        written = write_starter(directory, force=force)
    except errors.CosmosError as exc:
        _fail(exc)
    for path in written:
        typer.echo(f"  created {path}")
    where = "" if directory == Path(".") else f"cd {directory} && "
    typer.secho("next steps:", bold=True)
    typer.echo(f"  {where}cosmos demo-data            # synthetic data into data/ecommerce.duckdb")
    typer.echo("  cosmos query revenue --by orders.status --evidence")
    typer.echo("  cosmos explain rule:active_customer")


@app.command()
def explain(
    name: Annotated[
        str,
        typer.Argument(
            help="metric:x, rule:x, concept:x, team:x, system:x, process:x or a dimension."
        ),
    ],
    model: ModelOpt = Path("semantic"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Explains a member of the model: definition, owner, source systems, processes and logic."""
    from cosmos_semantic.catalog import Catalog

    try:
        out = Catalog(compile_project(load_project(model))).explain(name)
    except errors.CosmosError as exc:
        _fail(exc)
    if as_json:
        typer.echo(json.dumps(out, default=str, ensure_ascii=False, indent=2))
        return
    typer.secho(f"{out.get('kind')} {out.get('name')}", bold=True)
    for key, value in out.items():
        if key in ("kind", "name") or value in (None, [], {}, ""):
            continue
        if isinstance(value, dict | list):
            value = json.dumps(value, default=str, ensure_ascii=False)
        typer.echo(f"  {key:15} {value}")


@app.command()
def validate(model: ModelOpt = Path("semantic")) -> None:
    """Validates the model: references, entities, join paths and row filters."""
    try:
        graph = compile_project(load_project(model), strict=False)
    except errors.CosmosError as exc:
        _fail(exc)
    for issue in graph.issues:
        color = typer.colors.RED if issue.severity == "error" else typer.colors.YELLOW
        typer.secho(str(issue), fg=color)
    if any(i.severity == "error" for i in graph.issues):
        raise typer.Exit(1)
    typer.secho(
        f"ok: {len(graph.models)} models, {len(graph.measure_owner)} measures, "
        f"{len(graph.metrics)} metrics",
        fg=typer.colors.GREEN,
    )


@app.command()
def schema(
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Generates the JSON Schema of the v1 authoring format."""
    from cosmos_semantic.spec import json_schema

    text = json.dumps(json_schema(), indent=2, ensure_ascii=False) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
        typer.echo(f"schema written to {output}")
    else:
        sys.stdout.write(text)


@app.command()
def sql(
    metrics: Annotated[list[str], typer.Argument(help="Metrics or measures.")],
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    dimension: DimOpt = None,
    filter: WhereOpt = None,
    order_by: OrderOpt = None,
    limit: LimitOpt = None,
    as_of: AsOfOpt = None,
    rules_as_of: RulesAsOfOpt = None,
    user: UserOpt = None,
    agent: AgentOpt = False,
) -> None:
    """Shows the SQL the engine generates, without running it."""
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        compiled = engine.compile(
            _request(metrics, dimension, filter, order_by, limit, as_of, rules_as_of),
            _principal(user, "mcp" if agent else "cli"),
        )
    except errors.CosmosError as exc:
        _fail(exc)
    typer.echo(compiled.sql)


@app.command()
def query(
    metrics: Annotated[list[str], typer.Argument(help="Metrics or measures.")],
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    dimension: DimOpt = None,
    filter: WhereOpt = None,
    order_by: OrderOpt = None,
    limit: LimitOpt = None,
    as_of: AsOfOpt = None,
    rules_as_of: RulesAsOfOpt = None,
    user: UserOpt = None,
    agent: AgentOpt = False,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    evidence: Annotated[
        bool, typer.Option("--evidence", help="Print the evidence envelope after the result.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print rows and evidence as JSON.")
    ] = False,
    as_csv: Annotated[
        bool, typer.Option("--csv", help="Print rows as CSV (header + one row per result row).")
    ] = False,
) -> None:
    """Runs a metrics request on the data source.

    Set COSMOS_AUDIT_LOG to a .jsonl file to record the query for `cosmos replay`.

    Example:

        cosmos query revenue average_order_value --by orders.status --csv
    """
    from cosmos_semantic.service import SemanticService

    if as_csv and as_json:
        _fail(ValueError("use either --csv or --json"))
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        result = SemanticService(engine).query(
            _request(metrics, dimension, filter, order_by, limit, as_of, rules_as_of),
            _principal(user, "mcp" if agent else "cli"),
        )
    except errors.CosmosError as exc:
        _fail(exc)
    if as_json:
        typer.echo(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        return
    if as_csv:
        sys.stdout.write(_csv_text(result))
        typer.secho(
            f"{len(result['rows'])} rows  query_id={result['evidence']['query_id']}",
            fg=typer.colors.BRIGHT_BLACK,
            err=True,
        )
        if evidence:
            _print_evidence(result["evidence"], out=lambda s: typer.echo(s, err=True))
        return
    if show_sql:
        typer.secho(result["evidence"]["sql"]["text"], fg=typer.colors.BRIGHT_BLACK, err=True)
    rows = result["rows"]
    names = [c["name"] for c in result["columns"]]
    widths = [max([len(n)] + [len(_fmt(r[n])) for r in rows]) for n in names]
    typer.echo("  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo("  ".join(_fmt(row[n]).ljust(w) for n, w in zip(names, widths, strict=True)))
    typer.secho(
        f"{len(rows)} rows  query_id={result['evidence']['query_id']}",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )
    if evidence:
        _print_evidence(result["evidence"])


def _csv_text(result: dict) -> str:
    """Render a query result as CSV: column names as header, then one row
    per result row, with values exactly as the engine returns them (no
    thousands separators or fixed decimals); None becomes an empty field."""
    names = [c["name"] for c in result["columns"]]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(names)
    for row in result["rows"]:
        writer.writerow(["" if row[n] is None else row[n] for n in names])
    return buf.getvalue()


def _print_evidence(ev: dict, out=typer.echo) -> None:
    out("")
    out(f"evidence  {ev['query_id']}")
    out(f"  model   {ev['model_hash'][:16]}  engine {ev['engine_version']}")
    out(
        f"  as of   {ev['as_of']} ({ev['timezone']})  source {ev['data_source']['name']} "
        f"({ev['data_source']['dialect']})"
    )
    out(
        f"  who     {ev['principal']['subject'] or '-'} via {ev['principal']['channel']} "
        f"{ev['principal']['user_context'] or ''}"
    )
    for m in ev["metrics"]:
        out(f"  metric  {m['name']} = {m['formula']}")
    for r in ev["rules"]:
        out(
            f"  rule    {r['name']} ({r['kind']}, {', '.join(r['used_as'])}) owner={r['owner']} "
            f"since={r['effective_from']}{'' if r['current_version'] else ' (earlier version)'} "
            f"tests={r['tests']['status']} {r['tests']['passed']}/{r['tests']['total']}"
        )
    for f in ev["policies"]["row_filters"]:
        out(f"  policy  {f['model']}.{f['name']}: {f['filter']} {f['user_fields']}")
    out(f"  lineage {', '.join(ev['lineage']['models'])}")
    out(
        f"  sql     {ev['sql']['hash'][:16]}  result {ev['result']['hash'][:16]} "
        f"({ev['result']['row_count']} rows)"
    )
    for c in ev["caveats"]:
        out(f"  note    {c}")


@app.command()
def replay(
    query_id: Annotated[str, typer.Argument(help="query_id from the audit log.")],
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    audit: Annotated[
        Path | None, typer.Option("--audit", help="Audit log (.jsonl); default: COSMOS_AUDIT_LOG.")
    ] = None,
) -> None:
    """Re-runs an audited query and tells whether the model, the SQL or the data changed."""
    from cosmos_semantic.service import SemanticService

    try:
        engine = Engine.from_paths(model, load_connections(connections))
        report = SemanticService(engine, audit_path=audit).replay(query_id)
    except errors.CosmosError as exc:
        _fail(exc)
    color = typer.colors.GREEN if report["verdict"] == "identical" else typer.colors.YELLOW
    typer.secho(f"{report['verdict']}: {report['explanation']}", fg=color)
    for key, same in report["matches"].items():
        typer.echo(
            f"  {key:12} {'same' if same else 'differs'}  "
            f"{str(report['original'][key])[:16]} -> {str(report['replayed'][key])[:16]}"
        )


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, dt.datetime) and value.time() == dt.time():
        return value.date().isoformat()
    return "" if value is None else str(value)


@app.command("demo-data")
def demo_data_cmd(
    target: Annotated[
        str, typer.Option(help="Data source in connections.yml, or `parquet:<folder>`.")
    ] = "local",
    connections: ConnOpt = Path("connections.yml"),
    customers: Annotated[int, typer.Option(help="Number of customers; orders = 3x.")] = 2_000,
    seed: int = 42,
    anchor: Annotated[
        str | None, typer.Option(help="Anchor date YYYY-MM-DD; default: today.")
    ] = None,
    locale: Annotated[
        str, typer.Option(help="en_US (default) or pt_BR (Brazilian data with CPF).")
    ] = "en_US",
) -> None:
    """Generates the synthetic e-commerce data and loads it into a data source."""
    from cosmos_semantic import demo_data

    tables = demo_data.generate(
        customers,
        seed=seed,
        anchor=dt.date.fromisoformat(anchor) if anchor else None,
        locale=locale,
    )
    if target.startswith("parquet:"):
        for path in demo_data.write_parquet(tables, target.split(":", 1)[1]):
            typer.echo(f"wrote {path}")
        return
    try:
        config = load_connections(connections).data_sources[target]
    except KeyError:
        _fail(errors.ModelError(f"data source `{target}` is not in {connections}"))
    if config.path:
        Path(config.path).parent.mkdir(parents=True, exist_ok=True)
    connector = connect(config)
    try:
        demo_data.load(connector, tables)
    finally:
        connector.close()
    counts = ", ".join(f"{n}={t.num_rows}" for n, t in tables.items())
    typer.secho(f"data loaded into `{target}`: {counts}", fg=typer.colors.GREEN)


@app.command()
def reference(
    model: ModelOpt = Path("examples/ecommerce/semantic"),
    cases: Annotated[
        list[Path] | None, typer.Option(help="Case files; default: basic and advanced.")
    ] = None,
    connections: ConnOpt = Path("examples/ecommerce/connections.yml"),
    source: SourceOpt = None,
    phase: Annotated[int, typer.Option(help="Run cases up to this phase.")] = 2,
) -> None:
    """Runs the reference queries and compares the engine with hand-written SQL."""
    from cosmos_semantic.reference import load_cases, run_case

    graph = compile_project(load_project(model))
    conns = load_connections(connections)
    config: DataSourceConfig = conns.data_sources[source or conns.default or "local"]
    connector = connect(config)
    resolver = _glossary_resolver(graph)
    colors = {"pass": typer.colors.GREEN, "fail": typer.colors.RED, "skip": typer.colors.YELLOW}
    failed = 0
    try:
        for file in cases or DEFAULT_CASES:
            for case in load_cases(file):
                if case.phase > phase:
                    continue
                result = run_case(case, graph, connector, resolver=resolver)
                typer.secho(
                    f"{case.id} {result.status:4}  {case.title}  {result.detail}",
                    fg=colors[result.status],
                )
                failed += result.status == "fail"
    finally:
        connector.close()
    if failed:
        raise typer.Exit(1)


@app.command()
def reconcile(
    metric: Annotated[str | None, typer.Argument(help="Metric to reconcile.")] = None,
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    source: SourceOpt = None,
    dimension: DimOpt = None,
    filter: WhereOpt = None,
    as_of: AsOfOpt = None,
    user: UserOpt = None,
    sql: Annotated[
        Path | None, typer.Option("--sql", help="File with the legacy SQL to compare with.")
    ] = None,
    other_model: Annotated[
        Path | None, typer.Option("--other-model", help="Another semantic model to compare with.")
    ] = None,
    cases: Annotated[
        Path | None, typer.Option("--cases", help="Run a divergence zoo file instead.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compares a governed metric with legacy SQL or another model and explains divergences.

    Legacy SQL can read anything the database user can, so this is an operator tool: it is
    not exposed through the REST API or MCP.
    """
    from cosmos_semantic.reconcile import (
        check_zoo_case,
        load_zoo,
        reconcile_models,
        reconcile_sql,
    )

    try:
        graph = compile_project(load_project(model))
        conns = load_connections(connections)
        connector = connect(conns.data_sources[source or conns.default or "local"])
    except (errors.CosmosError, KeyError) as exc:
        _fail(exc)
    try:
        if cases:
            failed = 0
            for case in load_zoo(cases):
                ok, report = check_zoo_case(case, graph, connector)
                failed += not ok
                causes = ", ".join(c["code"] for c in report["causes"]) or "-"
                typer.secho(
                    f"{case.id} {'ok  ' if ok else 'FAIL'} {report['status']:9} {causes:40} "
                    f"{case.title}",
                    fg=typer.colors.GREEN if ok else typer.colors.RED,
                )
            if failed:
                raise typer.Exit(1)
            return
        if not metric or (sql is None) == (other_model is None):
            _fail(ValueError("give a metric and exactly one of --sql or --other-model, or --cases"))
        request = _request([metric], dimension, filter, None, None, as_of)
        principal = _principal(user)
        if sql is not None:
            report = reconcile_sql(
                graph, connector, request, sql.read_text(encoding="utf-8"), principal
            )
        else:
            other = compile_project(load_project(other_model))
            report = reconcile_models(graph, other, connector, request, principal)
    except errors.CosmosError as exc:
        _fail(exc)
    finally:
        connector.close()
    if as_json:
        typer.echo(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        return
    _print_reconciliation(report)


@app.command()
def check(
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    source: SourceOpt = None,
    cases: Annotated[
        Path | None,
        typer.Option(
            "--cases",
            help="Reconciliation cases (legacy SQL per metric); without it only rule tests run.",
        ),
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option("--log", help="Runs log to append to; default: COSMOS_RUNS_LOG."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Runs the rule tests and the reconciliation cases and records the run, so the console
    shows health over time. Exits with 1 when a test or a reconciliation fails (for CI)."""
    import os

    from cosmos_semantic.checks import append_run, run_checks, run_ok
    from cosmos_semantic.evidence import EvidenceBuilder
    from cosmos_semantic.reconcile import load_zoo

    connector = None
    try:
        graph = compile_project(load_project(model))
        loaded = load_zoo(cases) if cases else []
        name = None
        if loaded:
            conns = load_connections(connections)
            name = source or conns.default or next(iter(conns.data_sources))
            connector = connect(conns.data_sources[name])
        run = run_checks(
            graph,
            model_hash=EvidenceBuilder(graph).model_hash,
            connector=connector,
            source=name,
            cases=loaded,
        )
    except (errors.CosmosError, KeyError) as exc:
        _fail(exc)
    finally:
        if connector is not None:
            connector.close()
    path = log or os.environ.get("COSMOS_RUNS_LOG")
    if path:
        append_run(path, run)
    if as_json:
        typer.echo(json.dumps(run, default=str, ensure_ascii=False, indent=2))
    else:
        tests = run["rule_tests"]
        typer.echo(f"rule tests       {tests['passed']}/{tests['total']} pass")
        for failure in tests["failures"]:
            typer.secho(
                f"  FAIL {failure['rule']} #{failure['test']}: {failure['detail']}", fg="red"
            )
        for case in run["reconciliations"]:
            causes = ", ".join(c["code"] for c in case.get("causes", [])) or "-"
            ok = case["outcome"] == "pass"
            typer.secho(
                f"{case['id']:6} {case['outcome']:7} {case.get('status', ''):9} {causes:30} "
                f"{case['title']}",
                fg=typer.colors.GREEN if ok else typer.colors.RED,
            )
        typer.echo(f"recorded in {path}" if path else "not recorded (set COSMOS_RUNS_LOG or --log)")
    if not run_ok(run):
        raise typer.Exit(1)


def _print_reconciliation(report: dict) -> None:
    color = typer.colors.GREEN if report["status"] == "match" else typer.colors.YELLOW
    typer.secho(f"{report['status']}: {report['metric']}", fg=color, bold=True)
    rows, totals = report["rows"], report["totals"]
    typer.echo(
        f"  rows     {rows['matched']} matched, {rows['differing']} differing, "
        f"{rows['only_in_cosmos']} only governed, {rows['only_in_legacy']} only legacy"
    )
    pct = totals["difference_pct"]
    typer.echo(
        f"  totals   governed {totals['cosmos']:,.2f}  legacy {totals['legacy']:,.2f}  "
        f"difference {totals['difference']:,.2f}" + (f" ({pct:+.2f}%)" if pct is not None else "")
    )
    typer.echo(f"  governed {report['cosmos']['formula']}  (as of {report['cosmos']['as_of']})")
    if report["legacy"].get("formula"):
        typer.echo(f"  other    {report['legacy']['formula']}")
    for example in report["examples"][:5]:
        typer.echo(
            f"  example  {example['key']}: governed {example['cosmos']} / legacy {example['legacy']}"
        )
    for cause in report["causes"]:
        typer.secho(f"  cause    [{cause['code']}] {cause['summary']}", fg=typer.colors.CYAN)


@app.command()
def impact(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="kind:name, such as rule:active_customer or dimension:orders.status."),
    ] = None,
    model: ModelOpt = Path("semantic"),
    changes_from: Annotated[
        Path | None,
        typer.Option("--changes-from", help="Previous model version: analyze what changed."),
    ] = None,
    audit: Annotated[
        Path | None, typer.Option("--audit", help="Audit log (.jsonl); default: COSMOS_AUDIT_LOG.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Shows what a change to a rule, metric, measure, dimension or model affects, and who
    queried it."""
    import os

    from cosmos_semantic.impact import analyze, changed_members

    if audit is None and os.environ.get("COSMOS_AUDIT_LOG"):
        audit = Path(os.environ["COSMOS_AUDIT_LOG"])
    try:
        graph = compile_project(load_project(model))
        targets = list(targets or [])
        removed: list[str] = []
        if changes_from is not None:
            old = compile_project(load_project(changes_from))
            targets += changed_members(old, graph)
        if not targets:
            if changes_from is not None:
                typer.secho("no changes between the two model versions", fg=typer.colors.GREEN)
                return
            _fail(ValueError("give at least one target or --changes-from"))
        report = analyze(graph, targets, audit_path=audit, removed=removed)
    except errors.CosmosError as exc:
        _fail(exc)
    if as_json:
        typer.echo(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        return
    typer.secho(f"impact of {', '.join(report['targets']) or '-'}", bold=True)
    for name in report.get("removed", []):
        typer.secho(f"  removed     {name}", fg=typer.colors.RED)
    for kind, items in report["affected"].items():
        if items:
            typer.echo(f"  {kind:11} {', '.join(items)}")
    if report["source_systems"]:
        typer.echo(f"  from        {', '.join(report['source_systems'])}")
    for owner, members in report["owners"].items():
        color = typer.colors.YELLOW if owner == "unowned" else None
        typer.secho(f"  owner       {owner}: {', '.join(members)}", fg=color)
    if audit:
        if not report["consumers"]:
            typer.echo("  consumers   none in the audit log")
        for c in report["consumers"]:
            typer.echo(
                f"  consumer    {c['subject'] or '-'} via {c['channel']}: {c['queries']} queries, "
                f"last {c['last_at']}, using {', '.join(c['members'])}"
            )


@app.command()
def graph(
    model: ModelOpt = Path("semantic"),
    focus: Annotated[
        str | None,
        typer.Option(help="kind:name; only what it depends on and what depends on it."),
    ] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="json, mermaid or dot.")] = "mermaid",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Exports the business knowledge graph: teams, systems, processes, models, metrics,
    rules and concepts, with ownership and dependencies."""
    from cosmos_semantic.knowledge import build_graph

    try:
        compiled = compile_project(load_project(model))
        kg = build_graph(compiled)
        if focus:
            from cosmos_semantic.impact import DependencyGraph

            target = (
                focus if focus.startswith("team:") else DependencyGraph(compiled).normalize(focus)
            )
            kg = kg.focus(target)
        rendered = kg.render(fmt)
    except errors.CosmosError as exc:
        _fail(exc)
    text = (
        json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"
        if isinstance(rendered, dict)
        else rendered
    )
    if output:
        output.write_text(text, encoding="utf-8")
        typer.secho(
            f"{len(kg.nodes)} nodes, {len(kg.edges)} edges written to {output}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo(text, nl=False)


@app.command()
def usage(
    audit: Annotated[
        Path | None, typer.Option("--audit", help="Audit log (.jsonl); default: COSMOS_AUDIT_LOG.")
    ] = None,
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    since: Annotated[str | None, typer.Option(help="First day YYYY-MM-DD (UTC).")] = None,
    until: Annotated[str | None, typer.Option(help="Last day YYYY-MM-DD (UTC).")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Usage and estimated cost per metric, rule, user, channel and data source, from the
    audit log. Rates come from `cost` in connections.yml."""
    import os

    from cosmos_semantic.usage import usage_report

    if audit is None and os.environ.get("COSMOS_AUDIT_LOG"):
        audit = Path(os.environ["COSMOS_AUDIT_LOG"])
    if audit is None:
        _fail(ValueError("give --audit or set COSMOS_AUDIT_LOG"))
    try:
        graph = compile_project(load_project(model)) if model.exists() else None
        conns = load_connections(connections) if connections.exists() else None
        report = usage_report(
            audit,
            conns,
            graph,
            since=dt.date.fromisoformat(since) if since else None,
            until=dt.date.fromisoformat(until) if until else None,
        )
    except errors.CosmosError as exc:
        _fail(exc)
    if as_json:
        typer.echo(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        return
    currency = report["currency"] or ""
    t = report["total"]
    typer.secho(
        f"{t['queries']} queries  {t['errors']} errors  {t['denied']} denied  "
        f"{t['rows']} rows  {t['duration_ms'] / 1000:.1f}s  cost {t['cost']:.4f} {currency}",
        bold=True,
    )
    sections = [
        ("metric", "by_metric", ("metric",)),
        ("user", "by_principal", ("subject", "channel")),
        ("channel", "by_channel", ("channel",)),
        ("source", "by_data_source", ("data_source",)),
    ]
    for title, key, names in sections:
        for item in report[key]:
            label = " via ".join(str(item[n] or "-") for n in names)
            extra = f"  agents {item['agent_queries']}" if "agent_queries" in item else ""
            typer.echo(
                f"  {title:8} {label:32} {item['queries']:>5} q  avg {item['avg_ms']:>7.1f} ms  "
                f"cost {item['cost']:.4f}{extra}"
            )
    for item in report["by_rule"]:
        typer.echo(f"  rule     {item['rule']:32} {item['queries']:>5} q")
    if report.get("unused_metrics"):
        typer.secho(f"  unused   {', '.join(report['unused_metrics'])}", fg=typer.colors.YELLOW)
    typer.secho(report["note"], fg=typer.colors.BRIGHT_BLACK, err=True)


@app.command()
def suggest(
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    output: Annotated[
        Path, typer.Option("--output", "-o", help="YAML with the accepted suggestions.")
    ] = Path("suggestions.yml"),
    source: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Data source to validate suggestions by running them; repeat it for every "
                "source the model serves (samples come from the first). Default: none."
            )
        ),
    ] = None,
    samples: Annotated[
        bool,
        typer.Option(
            "--samples",
            help="Send up to 10 distinct values of non-sensitive categorical columns to the model.",
        ),
    ] = False,
    llm: Annotated[str, typer.Option("--llm", help="Claude model id.")] = "claude-opus-5-5",
) -> None:
    """Asks Claude for glossary concepts, rules, metrics and descriptions, validates each one
    with the engine and writes the accepted ones for review. Nothing is applied to the model.
    Needs `pip install "cosmos-semantic[ai]"` (or `uv sync --extra ai`) and ANTHROPIC_API_KEY (or an `ant auth login` profile)."""
    from cosmos_semantic.discovery import ClaudeSuggestionModel, discover
    from cosmos_semantic.interchange import dump_project

    if samples and not source:
        _fail(ValueError("--samples needs --source"))
    try:
        compiled = compile_project(load_project(model))
        connectors = []
        if source:
            engine = Engine(compiled, load_connections(connections))
            connectors = [engine.connector(name) for name in source]
        report = discover(compiled, ClaudeSuggestionModel(llm), connectors, samples=samples)
    except errors.CosmosError as exc:
        _fail(exc)
    header = (
        f"# Suggested by {report.model} on {dt.date.today()}, validated by the engine.\n"
        "# Review, set owners and add tests before moving this file into the model directory.\n"
    )
    output.write_text(header + dump_project(report.project()), encoding="utf-8")
    report_path = output.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    accepted = report.as_dict()["accepted"]
    typer.secho(
        f"accepted: {len(accepted['concepts'])} concepts, {len(accepted['rules'])} rules, "
        f"{len(accepted['metrics'])} metrics, {accepted['descriptions']} descriptions "
        f"-> {output}",
        fg=typer.colors.GREEN,
    )
    for item in report.rejected:
        typer.secho(
            f"  rejected {item['kind']} {item['name']}: {item['reason']}", fg=typer.colors.YELLOW
        )
    typer.echo(f"report (descriptions to apply by hand, rejections) -> {report_path}")


pack_app = typer.Typer(help="Industry semantic packs mapped onto your database.")
app.add_typer(pack_app, name="pack")


@pack_app.command("list")
def pack_list() -> None:
    """Lists the available packs."""
    from cosmos_semantic.packs import available

    for pack in available():
        typer.secho(f"{pack['name']} (v{pack['version']})", bold=True)
        typer.echo(f"  {pack['description']}")


@pack_app.command("apply")
def pack_apply(
    name: Annotated[str, typer.Argument(help="Pack name, such as retail.")],
    connections: ConnOpt = Path("connections.yml"),
    source: Annotated[
        str | None, typer.Option(help="Data source; default: the default one.")
    ] = None,
    schema: Annotated[str, typer.Option(help="Schema with your tables.")] = "public",
    mapping: Annotated[
        list[str] | None,
        typer.Option(
            "--map", help="Override: table=physical_table or table.column=physical_column."
        ),
    ] = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="Draft model (.yml).")] = Path(
        "semantic/pack.yml"
    ),
) -> None:
    """Maps a pack onto your tables by name, drops what your database lacks, runs every
    metric, and writes a draft model plus a report for review."""
    from cosmos_semantic.connectors import connect
    from cosmos_semantic.interchange import dump_project
    from cosmos_semantic.packs import apply_pack

    overrides = {}
    for item in mapping or []:
        key, sep, value = item.partition("=")
        if not sep:
            _fail(ValueError(f"--map expects name=value, got `{item}`"))
        overrides[key] = value
    try:
        conns = load_connections(connections)
        name_ = source or conns.default or next(iter(conns.data_sources))
        connector = connect(conns.data_sources[name_])
        result = apply_pack(name, connector, schema=schema, overrides=overrides)
    except errors.CosmosError as exc:
        _fail(exc)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = result.report
    header = (
        f"# Draft from the `{name}` pack v{report['pack']['version']} on {dt.date.today()}; "
        "review it, then set owners, teams and row filters for your organization.\n"
    )
    output.write_text(header + dump_project(result.project), encoding="utf-8")
    report_path = output.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for logical, physical in report["tables"].items():
        renamed = report["columns_renamed"].get(logical, {})
        detail = ", ".join(f"{k}->{v}" for k, v in renamed.items())
        typer.echo(f"  table    {logical} -> {physical}" + (f"  ({detail})" if detail else ""))
    for logical in report["missing_tables"]:
        typer.secho(f"  missing  table {logical}", fg=typer.colors.YELLOW)
    for logical, columns in report["missing_columns"].items():
        typer.secho(f"  missing  {logical}: {', '.join(columns)}", fg=typer.colors.YELLOW)
    for item in report["ambiguous"]:
        typer.secho(f"  ambiguous {item} (use --map)", fg=typer.colors.YELLOW)
    for item in report["removed"]:
        typer.secho(f"  removed  {item['member']}: {item['reason']}", fg=typer.colors.BRIGHT_BLACK)
    tests = report["rule_tests"]
    typer.secho(
        f"{report['metrics_checked']} metrics ran on the data source, rule tests "
        f"{tests['passed']}/{tests['total']} -> {output}",
        fg=typer.colors.GREEN,
    )


export_app = typer.Typer(help="Export the model to other formats.")
app.add_typer(export_app, name="export")


@export_app.command("skos")
def export_skos(
    model: ModelOpt = Path("semantic"),
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="turtle, jsonld or ntriples.")
    ] = "turtle",
    base: Annotated[
        str, typer.Option(help="Base IRI for concepts, such as https://data.example.com/glossary/.")
    ] = "urn:cosmos-semantic:glossary:",
    title: Annotated[str, typer.Option(help="Title of the concept scheme.")] = "Business glossary",
    definition_lang: Annotated[str, typer.Option(help="Language tag of the definitions.")] = "en",
) -> None:
    """Exports the business glossary as SKOS/RDF for data catalogs (Turtle, JSON-LD or
    N-Triples), with links to the metrics and rules that define each concept."""
    from cosmos_semantic.interchange.skos import export_glossary

    try:
        text = export_glossary(
            compile_project(load_project(model)),
            fmt,
            base=base,
            title=title,
            definition_lang=definition_lang,
        )
    except (errors.CosmosError, ValueError) as exc:
        _fail(exc)
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8")
    typer.secho(f"glossary written to {output} ({fmt})", fg=typer.colors.GREEN)


@export_app.command("ossie")
def export_ossie_cmd(
    model: ModelOpt = Path("semantic"),
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="yaml or json.")] = "yaml",
    name: Annotated[
        str | None, typer.Option(help="Name of the Ossie semantic model (default: the folder).")
    ] = None,
    allow_policy_loss: Annotated[
        bool,
        typer.Option(
            "--allow-policy-loss",
            help="Export even though a row filter that reads a business rule is not exported.",
        ),
    ] = False,
) -> None:
    """Exports models, entities, dimensions, measures, metrics, row filters, sensitivity and the
    glossary as an Apache Ossie document. What is not exported yet is listed as warnings on
    standard error. A row filter that reads a business rule cannot be exported, so a model with
    one is refused unless --allow-policy-loss is given."""
    from cosmos_semantic.interchange.ossie import export_ossie

    if name is None:
        folder = model.resolve()
        name = (folder.parent.name if folder.name == "semantic" else folder.stem) or "semantic"
    try:
        result = export_ossie(
            load_project(model), name=name, fmt=fmt, allow_policy_loss=allow_policy_loss
        )
    except (errors.CosmosError, ValueError) as exc:
        _fail(exc)
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if output is None:
        typer.echo(result.text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.text, encoding="utf-8", newline="\n")
    typer.secho(
        f"Ossie model written to {output} ({fmt}, {len(result.warnings)} warnings)",
        fg=typer.colors.GREEN,
    )


@export_app.command("cube")
def export_cube_cmd(
    model: ModelOpt = Path("semantic"),
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    allow_policy_loss: Annotated[
        bool,
        typer.Option(
            "--allow-policy-loss",
            help="Export even though row filters and sensitivity are not exported.",
        ),
    ] = False,
    allow_metric_loss: Annotated[
        bool,
        typer.Option(
            "--allow-metric-loss",
            help="Export even though metrics Cube cannot carry (cumulative ones) are dropped.",
        ),
    ] = False,
) -> None:
    """Exports models, joins, dimensions, measures and metrics as Cube static YAML, which
    `cosmos import cube` reads back with the same numbers. What is not exported is listed as
    warnings on standard error. A model with row filters or sensitivity is refused unless
    --allow-policy-loss is given, and one with metrics Cube cannot carry unless
    --allow-metric-loss is given."""
    from cosmos_semantic.interchange.cube import export_cube

    try:
        result = export_cube(
            load_project(model),
            allow_policy_loss=allow_policy_loss,
            allow_metric_loss=allow_metric_loss,
        )
    except (errors.CosmosError, ValueError) as exc:
        _fail(exc)
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if output is None:
        typer.echo(result.text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.text, encoding="utf-8", newline="\n")
    typer.secho(
        f"Cube model written to {output} ({len(result.warnings)} warnings)",
        fg=typer.colors.GREEN,
    )


@app.command("bootstrap")
def bootstrap_cmd(
    output: Annotated[Path, typer.Option("--output", "-o", help="Draft model file (.yml).")],
    connections: ConnOpt = Path("connections.yml"),
    source: SourceOpt = None,
    schema_name: Annotated[
        str, typer.Option("--schema", help="Logical schema to read.")
    ] = "public",
    tables: Annotated[
        list[str] | None, typer.Option("--table", "-t", help="Only these tables.")
    ] = None,
    report_path: Annotated[
        Path | None, typer.Option("--report", help="Write the inference report as JSON.")
    ] = None,
) -> None:
    """Drafts a semantic model from the database catalog (keys, joins, dimensions, measures
    and suggested LGPD classification), without AI. Review the draft before using it."""
    from cosmos_semantic.bootstrap import bootstrap
    from cosmos_semantic.interchange import dump_project

    try:
        conns = load_connections(connections)
        connector = connect(conns.data_sources[source or conns.default or "local"])
    except (errors.CosmosError, KeyError) as exc:
        _fail(exc)
    try:
        project, report = bootstrap(connector, schema_name, tables)
    except errors.CosmosError as exc:
        _fail(exc)
    finally:
        connector.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    header = "# Draft generated by `cosmos bootstrap`; review every inference before publishing.\n"
    output.write_text(header + dump_project(project), encoding="utf-8", newline="\n")
    if report_path:
        report_path.write_text(
            json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    typer.secho(
        f"draft written to {output}: {len(report['tables'])} tables, {len(report['keys'])} keys, "
        f"{len(report['relationships'])} relationships in {report['seconds']}s",
        fg=typer.colors.GREEN,
    )
    for key in report["keys"]:
        typer.echo(
            f"  key        {key['table']}.{key['column']} -> {key['entity']} ({key['source']})"
        )
    for rel in report["relationships"]:
        typer.echo(
            f"  join       {rel['from']} -> {rel['to']} "
            f"({rel['non_null']} rows, {rel['orphans']} without a match)"
        )
    for item in report["sensitivity"]:
        typer.secho(f"  lgpd       {item['column']}: {item['suggested']}", fg=typer.colors.YELLOW)
    for warning in report["warnings"]:
        typer.secho(f"  review     {warning}", fg=typer.colors.YELLOW)
    for issue in report["issues"]:
        typer.secho(f"  issue      {issue}", fg=typer.colors.RED)


def _glossary_resolver(graph):
    from cosmos_semantic.ontology import Glossary

    glossary = Glossary(graph.project)

    def resolve(text: str) -> str | None:
        hits = glossary.search(text, limit=1)
        return hits[0].maps_to if hits else None

    return resolve


@app.command("test")
def test_rules(model: ModelOpt = Path("semantic")) -> None:
    """Runs the business rules' examples on DuckDB, without touching the data sources."""
    from cosmos_semantic.ontology import run_rule_tests

    try:
        project = load_project(model)
        compile_project(project)
    except errors.CosmosError as exc:
        _fail(exc)
    results = run_rule_tests(project)
    for r in results:
        color = typer.colors.GREEN if r.passed else typer.colors.RED
        typer.secho(f"{r.rule}[{r.index}] {'ok' if r.passed else 'failed'} {r.detail}", fg=color)
    if any(not r.passed for r in results):
        raise typer.Exit(1)
    typer.secho(f"{len(results)} rule examples ok", fg=typer.colors.GREEN)


@app.command("import")
def import_cmd(
    kind: Annotated[str, typer.Argument(help="dbt, ossie, cube or powerbi (.bim or TMDL folder).")],
    source: Annotated[Path, typer.Argument(help="Source file or folder.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output .yml file.")],
    schema_name: Annotated[
        str | None, typer.Option("--schema", help="Table schema (dbt, Cube, Power BI fallback).")
    ] = None,
) -> None:
    """Imports a model from dbt (latest and legacy), Apache Ossie, Cube (static YAML) or Power BI (.bim or TMDL)."""
    from cosmos_semantic.interchange import dump_project, import_model

    options = {"schema": schema_name} if kind in ("dbt", "cube", "powerbi") and schema_name else {}
    try:
        result = import_model(kind, source, **options)
        graph = compile_project(result.project, strict=False)
    except (errors.CosmosError, ValueError) as exc:
        _fail(exc)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dump_project(result.project), encoding="utf-8", newline="\n")
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    for issue in graph.issues:
        color = typer.colors.RED if issue.severity == "error" else typer.colors.YELLOW
        typer.secho(str(issue), fg=color)
    p = result.project
    typer.secho(
        f"imported into {output}: {len(p.semantic_models)} models, {len(p.metrics)} metrics, "
        f"{len(p.business_rules)} rules, {len(result.warnings)} warnings",
        fg=typer.colors.GREEN,
    )
    if any(i.severity == "error" for i in graph.issues):
        raise typer.Exit(1)


def _service(model: Path, connections: Path):
    import logging

    from cosmos_semantic.auth import Authenticator, AuthSettings
    from cosmos_semantic.service import SemanticService

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = Engine.from_paths(model, load_connections(connections))
    settings = AuthSettings.from_env()
    return SemanticService(engine), Authenticator(settings, engine.graph.project.user_context)


@app.command()
def serve(
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    host: str = "127.0.0.1",
    port: int = 8080,
    allow_anonymous: Annotated[
        bool, typer.Option(help="Accept calls without credentials (development only).")
    ] = False,
) -> None:
    """Starts the REST API (/v1) and the MCP server (/mcp) in the same process.

    Identity: COSMOS_OIDC_ISSUER and COSMOS_OIDC_AUDIENCE (provider tokens), or
    COSMOS_DEV_API_KEYS with development keys. COSMOS_PUBLIC_URL is the address clients use
    (for the OAuth protected resource metadata behind a proxy).
    """
    import os

    import uvicorn

    from cosmos_semantic.api import create_app
    from cosmos_semantic.mcp_server import build_mcp

    try:
        service, authenticator = _service(model, connections)
    except errors.CosmosError as exc:
        _fail(exc)
    if not authenticator.settings.enabled and not allow_anonymous:
        _fail(
            errors.ModelError(
                "set COSMOS_OIDC_ISSUER or COSMOS_DEV_API_KEYS, or use --allow-anonymous"
            )
        )
    mcp_app = build_mcp(service, authenticator).streamable_http_app(
        streamable_http_path="/", host=host
    )
    api = create_app(
        service,
        authenticator,
        allow_anonymous=allow_anonymous,
        mcp_app=mcp_app,
        public_url=os.environ.get("COSMOS_PUBLIC_URL"),
    )
    uvicorn.run(api, host=host, port=port)


@app.command()
def mcp(
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
) -> None:
    """MCP server over stdio, for local clients. The user context comes from COSMOS_API_KEY."""
    from cosmos_semantic.mcp_server import build_mcp

    try:
        service, authenticator = _service(model, connections)
    except errors.CosmosError as exc:
        _fail(exc)
    build_mcp(service, authenticator).run("stdio")


if __name__ == "__main__":
    app()
