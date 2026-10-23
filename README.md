# Cosmos Semantic

> Repository: `cosmos-semantic-platform`. License: open core, with the core under Apache 2.0 ([ADR 0005](docs/decisions/0005-open-core.md)).

A semantic layer for AI agents and applications, with **its own engine**: its own versioned YAML format with the [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl) vocabulary and a JSON Schema, SQL generation for each data source, and native interoperability with [Apache Ossie](https://github.com/open-semantic-interchange/OSI) (formerly Open Semantic Interchange, OSI). [Cube](https://github.com/cube-js/cube) and dbt are **import sources and export targets**, not dependencies.

**Status:** phases 0 and 1 are implemented; phase 2 (verifiable numbers) is in progress, with the evidence envelope, replay, reconciliation, impact analysis and model bootstrap done. The engine, the ontology (glossary and business rules), the REST API, the MCP server, OIDC identity and dbt, Ossie and Cube import are ready. Reference queries and a randomized cross-database test pass on DuckDB, Postgres and SQL Server; Databricks validation depends on a configured SQL warehouse. See the [roadmap](docs/roadmap.md).

## In one sentence

Define entities, metrics, business concepts and business rules once, and any AI agent or application queries the same number, with SQL generated and run directly on your data source, and the model portable to Ossie, Cube and dbt.

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras

# Generate synthetic e-commerce data into a local DuckDB (data/ecommerce.duckdb)
uv run cosmos demo-data -c examples/ecommerce/connections.yml   # --locale pt_BR for Brazilian data with CPF

# Validate the example model
uv run cosmos validate -m examples/ecommerce/semantic

# Revenue and average order value by status
uv run cosmos query revenue average_order_value --by orders.status \
  -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Metrics from different models, with the user's row filter; --show-sql prints the generated SQL
uv run cosmos query revenue paid_value customer_count --by customers.region -u region=Midwest --show-sql \
  -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Business rules as a filter and as a grouping; --as-of pins "today"
uv run cosmos query customer_count --where "{rule.active_customer}" --by customers.segment \
  -u region=Northeast --as-of 2026-10-22 -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Evidence envelope: formulas, rules, policies, as_of and hashes; then replay the audited query
export COSMOS_AUDIT_LOG=data/audit.jsonl
uv run cosmos query average_order_value --by customers.segment --where "{rule.active_customer}"   -u region=Northeast --evidence -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml
uv run cosmos replay <query_id> -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Reconcile a governed metric with legacy SQL or another model, and explain the divergence
uv run cosmos reconcile revenue --by orders.status --sql legacy_revenue.sql   -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml
uv run cosmos reconcile --cases examples/ecommerce/divergence_zoo.yml   -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# What a change affects, and who queried it (also: --changes-from <previous model>)
uv run cosmos impact rule:active_customer -m examples/ecommerce/semantic --audit data/audit.jsonl

# Draft a model from the database catalog (keys, joins, measures, LGPD suggestions), then review it
uv run cosmos bootstrap -c examples/ecommerce/connections.yml -o semantic/draft.yml

# Business rule examples (on DuckDB, without touching the data sources) and reference queries
uv run cosmos test -m examples/ecommerce/semantic
uv run cosmos reference

# Import models from other tools
uv run cosmos import cube path/to/model -o semantic/imported.yml
uv run cosmos import dbt path/to/dbt/models --schema analytics -o semantic/dbt.yml
uv run cosmos import ossie model.yaml -o semantic/ossie.yml
```

### REST API and MCP

`cosmos serve` starts the REST API at `/v1` and the MCP server at `/mcp` in the same process; interactive docs are at `/docs`.

```bash
# Production: tokens from the OIDC provider (Cosmos only validates, never issues)
export COSMOS_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
export COSMOS_OIDC_AUDIENCE=api://cosmos-semantic
# Development: API keys bound to a fixed context (a file with `keys: {<key>: {subject, user}}`)
export COSMOS_DEV_API_KEYS=dev-keys.yml

uv run cosmos serve -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml
curl -X POST localhost:8080/v1/query -H "X-API-Key: <key>" -H "content-type: application/json" \
  -d '{"metrics": ["revenue"], "dimensions": ["customers.segment"]}'
```

Remote MCP clients use `/mcp` as an OAuth protected resource: requests without a valid token get HTTP 401 with a `WWW-Authenticate` header pointing to `/.well-known/oauth-protected-resource`, which names the identity provider (set `COSMOS_PUBLIC_URL` behind a proxy).

For local MCP clients (Claude Desktop, Claude Code), use the stdio transport: `cosmos mcp -m ... -c ...` with `COSMOS_API_KEY` set. The tools are `search_concepts`, `explain`, `list_members`, `query` and `show_sql`. Every call runs as the user who owns the token; personal data never reaches agents.

### Data sources

Local Postgres: `docker compose -p cosmos-semantic -f deploy/docker-compose.yml up -d`, then `cosmos demo-data --target postgres` and `cosmos reference --source postgres`. To also run the tests on Postgres, set `COSMOS_PG_URI=postgresql://cosmos:cosmos@localhost:15432/cosmos`.

Local SQL Server: `docker compose -p cosmos-semantic -f deploy/docker-compose.yml --profile sqlserver up -d` and `COSMOS_MSSQL_URI='mssql+pymssql://sa:Cosmos!Passw0rd@localhost:14330/master'`. Every data source accepts a `schema_map` in `connections.yml` (for example `public: dbo`) to use the same model with different schemas.

Databricks: set `DATABRICKS_HTTP_PATH` (SQL warehouse) and `DATABRICKS_TOKEN` with `DATABRICKS_SERVER_HOSTNAME`, or `DATABRICKS_CONFIG_PROFILE` with a Databricks CLI profile.

Editors validate and autocomplete model files through the JSON Schema in [schema/](schema/), referenced on the first line of each file (`# yaml-language-server: $schema=...`). After changing `spec.py`, regenerate it with `uv run cosmos schema -o schema/cosmos-semantic.v1.schema.json`.

## Principles

1. **The engine is ours.** The semantic model, metric planner and SQL generation are the core of the product.
2. **Compatible, not dependent.** Cube, dbt and Ossie are import and export formats.
3. **Query the source directly.** No intermediate infrastructure in the MVP; caching comes later.
4. **Same number everywhere.** The same request returns the same result on every supported database.
5. **One differentiator per phase.** Scope only grows after it is validated.
6. **Open core.** Everything that defines or computes a number is Apache 2.0; the paid part covers enterprise identity, auditing, managed operations and support.

## Documentation

| Document | Contents |
| --- | --- |
| [Product vision](docs/product-vision.md) | Problem, audience, positioning, free and paid editions |
| [Architecture](docs/architecture.md) | Components, query planner, cross-database guarantees, security, deployment |
| [Semantic model](docs/semantic-model.md) | The authoring format, the generated SQL and the exports |
| [Interoperability](docs/interoperability.md) | Mapping to Apache Ossie, and dbt and Cube import |
| [Roadmap](docs/roadmap.md) | Phases, MVP scope and exit criteria |
| [Architecture decisions](docs/decisions/) | Decision records (ADRs); 0004 supersedes 0001, 0005 sets the name and license, 0006 puts the ontology in the MVP, 0007 sets connectors, data sources, identity and import, 0008 makes verifiable numbers the phase 2 differentiator |

## Repository layout

```text
src/cosmos_semantic/
  spec.py            v1 authoring format (Pydantic); source of the JSON Schema
  loader.py          reading YAML files
  compiler.py        validation and semantic graph, with joins inferred from entities
  expressions.py     {member} and $user references, and protection of caller filters
  planner.py         query plan and per-dialect SQL (SQLGlot)
  connectors.py      DuckDB, Postgres (ADBC), Databricks and SQL Server (SQLAlchemy), always Arrow
  engine.py          model + connections -> executed queries
  ontology.py        business rules (virtual dimensions and tests) and SKOS glossary
  catalog.py         members, concept search and explanations
  auth.py            OIDC token validation and development keys
  evidence.py        evidence envelope: formulas, rules, policies, hashes
  reconcile.py       reconciliation with legacy SQL or another model, and divergence probes
  impact.py          impact analysis over the model graph and the audit log
  bootstrap.py       draft model from the database catalog
  service.py         querying with evidence, auditing and replay, shared by REST, MCP and CLI
  api.py             REST API (FastAPI)
  mcp_server.py      MCP server
  interchange/       dbt, Ossie and Cube import
  demo_data.py       synthetic data (en_US default, pt_BR optional)
  reference.py       running the reference queries
  cli.py             the `cosmos` command
schema/              JSON Schema of the format
examples/ecommerce/  example model, connections and the divergence zoo
reference/           reference queries (basic and advanced)
tests/fixtures/      example models in dbt, Ossie and Cube
deploy/              local docker compose
docs/                documentation and ADRs
```

## License and contributing

The core is licensed under [Apache 2.0](LICENSE) ([ADR 0005](docs/decisions/0005-open-core.md)). Contributions are welcome under the Developer Certificate of Origin; see [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
