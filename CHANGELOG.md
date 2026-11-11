# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the authoring
format or the API; such changes are listed under **Changed**.

## [Unreleased]

### Added

- `cosmos query --csv` prints the result as CSV for spreadsheets and scripts: a header, then one
  line per row, with raw values and nulls as empty fields. Standard output carries only the CSV,
  and `--csv` with `--json` is an error. Contributed by @zheng-guannan577 in #24, the project's
  first external contribution.

- `cosmos import powerbi` imports `DISTINCTCOUNTNOBLANK` as a `count_distinct` measure, and
  flags every `DISTINCTCOUNT` measure in the import report: DAX counts BLANK as a value, while
  `count_distinct` ignores nulls, so the imported measure is one lower whenever the column has
  nulls. Contributed
  by @bfs2021 in #11 (fixes #4).

- A supervised team of AI agents for daily evolution ([ADR 0013](docs/decisions/0013-supervised-agent-team.md)): an
  orchestrator and four specialists in `.claude/agents/`, the project's working rules in
  `CLAUDE.md`, and a daily report, posted as an issue labelled `daily`, that the maintainer answers.

- The MCP server has a `replay` tool, the equivalent of `POST /v1/replay/{query_id}`: it re-runs
  one of the user's audited queries and tells whether the model, the SQL or the data changed.
  Only the user who ran a query can replay it; over MCP, another user's query gets the same
  error as an unknown `query_id`.

- `cosmos export ossie` writes the model as an Apache Ossie document (YAML, or JSON with
  `--format json`). It exports models, entities, dimensions, measures and the four metric types
  as datasets, keys, relationships, fields and `OSSIE_SQL_2026` metrics on each dataset. Metrics
  that span datasets (a naive reader could fan them out) and cumulative ones stay only in a
  `COSMOS_SEMANTIC` extension, from which `cosmos import ossie` restores the same authoring
  model. Row filters (with the user context they read) and `sensitivity` are exported in that
  extension and next to their dataset and field, and come back exactly, so a governed model
  such as the example exports without `--allow-policy-loss`. The glossary is exported too, with
  each concept's labels and definition as `ai_context` of the member it maps to. A row filter
  that reads a business rule still fails closed unless `--allow-policy-loss` is given. Business
  rules and business context (teams, systems, processes) are not exported yet; they are listed
  as warnings on standard error and recorded in the extension, which the importer reports again.
  Round-trip and same-numbers tests on every database, including the row-filter and
  sensitivity reference queries ([interoperability](docs/interoperability.md#exporting-ossie)).

### Changed

- Tests render CLI help as plain, unwrapped text everywhere, so a test reading `--help` behaves
  the same on CI, where Typer otherwise forces a coloured terminal (#25).

## [0.3.0] - 2026-11-04

A read-only console for people who decide, and a live demo of it that needs no install. AI
suggestions are now validated on every database the model serves.

### Added

- A read-only console at `/console` ([ADR 0012](docs/decisions/0012-read-only-console.md),
  [docs](docs/console.md)), in English and Brazilian Portuguese, for people who decide rather
  than build:
  - metric health and ownership, with each number linking to the list behind it;
  - business processes as graphs colored by health;
  - the catalog with definitions, rule versions, reconciliations and impact;
  - audited answers with replay;
  - check history;
  - usage and cost (admins only, `COSMOS_CONSOLE_ADMINS`).

  Built with Next.js, React and Tailwind CSS as a static export that the wheel ships, and served
  by `cosmos serve`; installing is still one `pip install`.
- A public demo of the console on GitHub Pages (https://wiliamrosa.github.io/cosmos-semantic-platform/),
  rebuilt on every change to `main`. It needs no server: `console/demo/snapshot.py` runs the
  example model on synthetic data and writes each console view as JSON, and the demo build reads
  those files.
- `cosmos check` runs the rule tests and the reconciliation cases, appends the run to
  `COSMOS_RUNS_LOG`, and exits with 1 when something fails, so CI or cron can schedule it.
  `examples/ecommerce/reconciliations.yml` has four realistic cases, two of which diverge on
  purpose.
- `cosmos suggest` takes `--source` more than once and accepts a suggestion only if it runs on
  every source given, so SQL that one database accepts and another rejects is caught.
- `cosmos-semantic` as a second name for the `cosmos` command, so `uvx cosmos-semantic mcp ...`
  works, and a `server.json` for the official MCP registry.

### Changed

- Reconciliation cases no longer need `expect`: without it, a case passes when the legacy numbers
  match the governed metric.

### Fixed

- `cosmos suggest` runs classification and derived rules as dimensions of their model, instead
  of only compiling them; a rule bound to a column the table does not have is now rejected.

## [0.2.0] - 2026-11-04

Four more data sources: the whole test suite now passes on eight databases (DuckDB,
Postgres, SQL Server, MySQL, MariaDB, Oracle, Databricks and Snowflake), and CI runs the six
that fit in a container on every pull request.

### Added

- Snowflake as a data source (`snowflake` extra, [ADR 0010](docs/decisions/0010-snowflake-as-a-data-source.md)):
  key-pair, `connections.toml`, browser or password login, Arrow results, Monday weeks and ISO
  week numbers. The whole suite passes on a Snowflake warehouse; `cosmos bootstrap` reads its
  catalog.
- MySQL, MariaDB and Oracle as data sources (`mysql` and `oracle` extras,
  [ADR 0011](docs/decisions/0011-mysql-mariadb-and-oracle-as-data-sources.md)): MySQL and
  MariaDB through SQLAlchemy and PyMySQL, Oracle through python-oracledb in thin mode (no
  Instant Client). Time grains, null-safe joins and date arithmetic use each database's own
  functions, with weeks starting on Monday. The whole suite passes on MySQL 8.4, MariaDB 11.4
  and Oracle Database 23ai Free, which CI now runs on every pull request; `cosmos bootstrap`
  reads Oracle's catalog.
- Issue and pull request templates, a DCO check on pull requests, and the first issues marked
  `good first issue`.
- `cosmos suggest` keeps each rejected suggestion, as the model sent it, in the report.

### Changed

- The REST API and the MCP server report the installed package version instead of a fixed
  `0.1.0`.
- `cosmos demo-data` writes column types in the target database's dialect (for example
  `VARCHAR(200)` instead of `STRING` on Databricks), and loads `float64` as `BINARY_DOUBLE` on
  Oracle.

### Fixed

- A reference to a member whose expression is an operation is now parenthesized when it is an
  operand of another operator. `{orders.has_discount} = false` (with `has_discount` defined as
  `discount > 0`) printed `discount > 0 = FALSE`, a syntax error on DuckDB and Postgres;
  `NOT {x}` with `x = p OR q` printed `NOT p OR q`, a wrong result. Found by the first run of
  `cosmos suggest` against the Claude API.

## [0.1.0] - 2026-11-04

First public release. The whole test suite passes on DuckDB, Postgres, SQL Server and Databricks,
and on the lowest dependency versions this release allows.

### Engine and authoring format

- Own YAML authoring format with the dbt Semantic Layer vocabulary, `version: 1` and a JSON Schema
  (`cosmos schema`); `cosmos validate` checks references, entities, join paths and row filters.
- Planner with one aggregation per measure-owner model and many-to-one joins inferred from
  entities, so measures at different grains never multiply rows; one-to-many dimensions through a
  distinct bridge.
- Metric types `simple`, `ratio`, `derived` and `cumulative`; NULL-safe division; integer averages
  and divisions computed the same way on every database.
- "Today" pinned to the model's time zone or to the request's `as_of`, identically on every
  database.
- Connectors that return Arrow: DuckDB, Postgres (ADBC), SQL Server (SQLAlchemy) and Databricks
  (SQL warehouse, token or CLI profile).

### Ontology and security

- SKOS-style glossary with labels and synonyms per language, and business rules (`predicate`,
  `classification` with a decision table, `derived`) with owner, effective date and tests
  (`cosmos test`).
- Rule versions: a rule keeps its history, and a query uses the version in effect on its
  `rules_as_of` date.
- Row filters from the user context (`$user`), OIDC identity (Entra ID first) with claims mapped
  to the user context, and personal or sensitive data (`sensitivity`) never reaching AI agents.

### Verifiable numbers

- Evidence envelope on every answer (expanded formulas, rules with owner and test status, policies,
  data source, `as_of`, model, SQL and result hashes) and `cosmos replay` from the audit log.
- `cosmos reconcile`: a governed metric against legacy SQL or another model, with a diagnosis of
  the likely cause, proven against a "divergence zoo" of ten classic mistakes.
- `cosmos impact`: what a change to a rule, metric, measure, dimension, model, system or process
  affects, who owns it and who queried it; `--changes-from` for pull requests.
- `cosmos bootstrap`: a draft model from the database catalog, without AI.

### Business context

- Teams, systems and processes in the model; `cosmos graph` exports the knowledge graph as
  Mermaid, Graphviz DOT or JSON.
- `cosmos usage`: queries, denials, duration and estimated cost per metric, rule, user, agent and
  data source.
- `cosmos suggest` (optional `ai` extra): Claude suggests concepts, rules, metrics and
  descriptions; the engine validates each one and nothing is applied without review.
- `cosmos pack apply retail`: an industry pack mapped onto your tables by name and synonyms.

### Interfaces and interoperability

- REST API and MCP server (`search_concepts`, `explain`, `list_members`, `query`, `show_sql`),
  over stdio or HTTP with OAuth protected resource metadata.
- Import from dbt (latest and legacy specs), Apache Ossie, Cube (static YAML) and Power BI (`.bim`
  and TMDL), each with an equivalence test.
- Glossary export as SKOS/RDF (Turtle, JSON-LD, N-Triples) with `cosmos export skos` and
  `GET /v1/glossary`.
- `cosmos init` creates a starter project with the e-commerce example and a local DuckDB source.

[Unreleased]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.3.0
[0.2.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.2.0
[0.1.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.1.0
