"""Command line: ``cosmos validate``, ``query``, ``sql``, ``schema``, ``test``, ``import``,
``demo-data``, ``reference``, ``serve`` and ``mcp``."""

from __future__ import annotations

import datetime as dt
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
    list[str] | None, typer.Option("--user", "-u", help="User context, such as region=Sul.")
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


def _request(metrics: list[str], dimensions, filters, order_by, limit, as_of) -> QueryRequest:
    return QueryRequest(
        metrics=metrics,
        dimensions=dimensions or [],
        filters=filters or [],
        order_by=order_by or [],
        limit=limit,
        as_of=dt.date.fromisoformat(as_of) if as_of else None,
    )


DimOpt = Annotated[list[str] | None, typer.Option("--by", "-b", help="Dimensions to group by.")]
WhereOpt = Annotated[list[str] | None, typer.Option("--where", "-w", help="Filter expressions.")]
OrderOpt = Annotated[
    list[str] | None, typer.Option("--order", help="Order by; `-` for descending.")
]
LimitOpt = Annotated[int | None, typer.Option("--limit", "-l")]
AsOfOpt = Annotated[str | None, typer.Option("--as-of", help="Reference date YYYY-MM-DD.")]
AgentOpt = Annotated[bool, typer.Option(help="Query as an AI agent (MCP channel).")]


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
    user: UserOpt = None,
    agent: AgentOpt = False,
) -> None:
    """Shows the SQL the engine generates, without running it."""
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        compiled = engine.compile(
            _request(metrics, dimension, filter, order_by, limit, as_of),
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
    user: UserOpt = None,
    agent: AgentOpt = False,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Runs a metrics request on the data source."""
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        result = engine.query(
            _request(metrics, dimension, filter, order_by, limit, as_of),
            _principal(user, "mcp" if agent else "cli"),
        )
    except errors.CosmosError as exc:
        _fail(exc)
    if show_sql:
        typer.secho(result.compiled.sql, fg=typer.colors.BRIGHT_BLACK, err=True)
    rows = result.to_pylist()
    if as_json:
        typer.echo(json.dumps(rows, default=str, ensure_ascii=False, indent=2))
        return
    names = [c.name for c in result.compiled.columns]
    widths = [max([len(n)] + [len(_fmt(r[n])) for r in rows]) for n in names]
    typer.echo("  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo("  ".join(_fmt(row[n]).ljust(w) for n, w in zip(names, widths, strict=True)))
    typer.secho(f"{len(rows)} rows", fg=typer.colors.BRIGHT_BLACK, err=True)


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
) -> None:
    """Generates the synthetic e-commerce data and loads it into a data source."""
    from cosmos_semantic import demo_data

    tables = demo_data.generate(
        customers, seed=seed, anchor=dt.date.fromisoformat(anchor) if anchor else None
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
    phase: Annotated[int, typer.Option(help="Run cases up to this phase.")] = 1,
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
    kind: Annotated[str, typer.Argument(help="dbt, ossie or cube.")],
    source: Annotated[Path, typer.Argument(help="Source file or folder.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output .yml file.")],
    schema_name: Annotated[
        str | None, typer.Option("--schema", help="Table schema (dbt and Cube).")
    ] = None,
) -> None:
    """Imports a model from dbt (latest and legacy), Apache Ossie or Cube (static YAML)."""
    from cosmos_semantic.interchange import dump_project, import_model

    options = {"schema": schema_name} if kind in ("dbt", "cube") and schema_name else {}
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
    COSMOS_DEV_API_KEYS with development keys.
    """
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
    api = create_app(service, authenticator, allow_anonymous=allow_anonymous, mcp_app=mcp_app)
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
