"""Linha de comando: ``cosmos validate``, ``query``, ``sql``, ``schema``, ``demo-data``, ``reference``."""

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

app = typer.Typer(help="Cosmos Semantic: camada semântica com motor próprio.", no_args_is_help=True)

ModelOpt = Annotated[
    Path, typer.Option("--model", "-m", help="Arquivo ou pasta do modelo semântico.")
]
ConnOpt = Annotated[Path, typer.Option("--connections", "-c", help="Arquivo connections.yml.")]
SourceOpt = Annotated[str | None, typer.Option("--source", "-s", help="Origem de connections.yml.")]
UserOpt = Annotated[
    list[str] | None, typer.Option("--user", "-u", help="Contexto do usuário, como region=Sul.")
]


@app.callback()
def _setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")


def _fail(exc: Exception) -> None:
    typer.secho(f"erro: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _principal(user: list[str] | None, channel: str = "cli") -> Principal:
    values = {}
    for item in user or []:
        key, sep, value = item.partition("=")
        if not sep:
            _fail(ValueError(f"--user espera chave=valor, recebeu `{item}`"))
        values[key] = value
    return Principal(user=values, channel=channel)  # type: ignore[arg-type]


def _request(metrics: list[str], dimensions, filters, order_by, limit) -> QueryRequest:
    return QueryRequest(
        metrics=metrics,
        dimensions=dimensions or [],
        filters=filters or [],
        order_by=order_by or [],
        limit=limit,
    )


@app.command()
def validate(model: ModelOpt = Path("semantic")) -> None:
    """Valida o modelo: referências, entidades, caminhos de join e filtros por linha."""
    try:
        graph = compile_project(load_project(model), strict=False)
    except errors.CosmosError as exc:
        _fail(exc)
    for issue in graph.issues:
        color = typer.colors.RED if issue.severity == "error" else typer.colors.YELLOW
        typer.secho(str(issue), fg=color)
    errors_found = [i for i in graph.issues if i.severity == "error"]
    if errors_found:
        raise typer.Exit(1)
    typer.secho(
        f"ok: {len(graph.models)} modelos, {len(graph.measure_owner)} medidas, "
        f"{len(graph.metrics)} métricas",
        fg=typer.colors.GREEN,
    )


@app.command()
def schema(
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Gera o JSON Schema do formato de autoria v1."""
    from cosmos_semantic.spec import json_schema

    text = json.dumps(json_schema(), indent=2, ensure_ascii=False) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
        typer.echo(f"schema escrito em {output}")
    else:
        sys.stdout.write(text)


@app.command()
def sql(
    metrics: Annotated[list[str], typer.Argument(help="Métricas ou medidas.")],
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    dimension: Annotated[list[str] | None, typer.Option("--by", "-b")] = None,
    filter: Annotated[list[str] | None, typer.Option("--where", "-w")] = None,
    order_by: Annotated[list[str] | None, typer.Option("--order")] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-l")] = None,
    user: UserOpt = None,
    agent: Annotated[bool, typer.Option(help="Consultar como agente de IA (canal MCP).")] = False,
) -> None:
    """Mostra o SQL que o motor gera, sem executar."""
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        compiled = engine.compile(
            _request(metrics, dimension, filter, order_by, limit),
            _principal(user, "mcp" if agent else "cli"),
        )
    except errors.CosmosError as exc:
        _fail(exc)
    typer.echo(compiled.sql)


@app.command()
def query(
    metrics: Annotated[list[str], typer.Argument(help="Métricas ou medidas.")],
    model: ModelOpt = Path("semantic"),
    connections: ConnOpt = Path("connections.yml"),
    dimension: Annotated[list[str] | None, typer.Option("--by", "-b")] = None,
    filter: Annotated[list[str] | None, typer.Option("--where", "-w")] = None,
    order_by: Annotated[list[str] | None, typer.Option("--order")] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-l")] = None,
    user: UserOpt = None,
    agent: Annotated[bool, typer.Option(help="Consultar como agente de IA (canal MCP).")] = False,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Executa um pedido de métricas na origem."""
    try:
        engine = Engine.from_paths(model, load_connections(connections))
        result = engine.query(
            _request(metrics, dimension, filter, order_by, limit),
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
    typer.secho(f"{len(rows)} linhas", fg=typer.colors.BRIGHT_BLACK, err=True)


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, dt.datetime) and value.time() == dt.time():
        return value.date().isoformat()
    return "" if value is None else str(value)


@app.command("demo-data")
def demo_data_cmd(
    target: Annotated[
        str, typer.Option(help="Origem de connections.yml, ou `parquet:<pasta>`.")
    ] = "local",
    connections: ConnOpt = Path("connections.yml"),
    customers: Annotated[int, typer.Option(help="Número de clientes; pedidos = 3x.")] = 2_000,
    seed: int = 42,
    anchor: Annotated[
        str | None, typer.Option(help="Data-âncora AAAA-MM-DD; padrão: hoje.")
    ] = None,
) -> None:
    """Gera os dados sintéticos do e-commerce e carrega numa origem."""
    from cosmos_semantic import demo_data

    tables = demo_data.generate(
        customers, seed=seed, anchor=dt.date.fromisoformat(anchor) if anchor else None
    )
    if target.startswith("parquet:"):
        for path in demo_data.write_parquet(tables, target.split(":", 1)[1]):
            typer.echo(f"escrito {path}")
        return
    try:
        config = load_connections(connections).data_sources[target]
    except KeyError:
        _fail(errors.ModelError(f"origem `{target}` não está em {connections}"))
    if config.path:
        Path(config.path).parent.mkdir(parents=True, exist_ok=True)
    connector = connect(config)
    try:
        demo_data.load(connector, tables)
    finally:
        connector.close()
    counts = ", ".join(f"{n}={t.num_rows}" for n, t in tables.items())
    typer.secho(f"dados carregados em `{target}`: {counts}", fg=typer.colors.GREEN)


@app.command()
def reference(
    model: ModelOpt = Path("examples/ecommerce/semantic"),
    cases: Annotated[Path, typer.Option(help="Arquivo de casos.")] = Path("reference/queries.yml"),
    connections: ConnOpt = Path("examples/ecommerce/connections.yml"),
    source: SourceOpt = None,
    phase: Annotated[int, typer.Option(help="Roda os casos até esta fase.")] = 1,
) -> None:
    """Roda as consultas de referência e compara o motor com o SQL escrito à mão."""
    from cosmos_semantic.reference import load_cases, run_case

    graph = compile_project(load_project(model))
    conns = load_connections(connections)
    config: DataSourceConfig = conns.data_sources[source or conns.default or "local"]
    connector = connect(config)
    resolver = _glossary_resolver(graph)
    failed = 0
    try:
        for case in load_cases(cases):
            if case.phase > phase:
                continue
            result = run_case(case, graph, connector, resolver=resolver)
            color = {
                "pass": typer.colors.GREEN,
                "fail": typer.colors.RED,
                "skip": typer.colors.YELLOW,
            }
            typer.secho(
                f"{case.id} {result.status:4}  {case.title}  {result.detail}",
                fg=color[result.status],
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
    """Executa os exemplos das regras de negócio no DuckDB, sem acessar as origens."""
    from cosmos_semantic.ontology import run_rule_tests

    try:
        project = load_project(model)
        compile_project(project)
    except errors.CosmosError as exc:
        _fail(exc)
    results = run_rule_tests(project)
    for r in results:
        color = typer.colors.GREEN if r.passed else typer.colors.RED
        typer.secho(f"{r.rule}[{r.index}] {'ok' if r.passed else 'falhou'} {r.detail}", fg=color)
    if any(not r.passed for r in results):
        raise typer.Exit(1)
    typer.secho(f"{len(results)} exemplos de regras ok", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
