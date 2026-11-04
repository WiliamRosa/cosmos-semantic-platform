# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the authoring
format or the API; such changes are listed under **Changed**.

## [Unreleased]

### Added

- Snowflake as a data source (`snowflake` extra, [ADR 0010](docs/decisions/0010-snowflake-as-a-data-source.md)):
  key-pair, `connections.toml`, browser or password login, Arrow results, Monday weeks and ISO
  week numbers. The whole suite passes on a Snowflake warehouse; `cosmos bootstrap` reads its
  catalog.
- Issue and pull request templates, a DCO check on pull requests, and the first issues marked
  `good first issue`.
- `cosmos suggest` keeps each rejected suggestion, as the model sent it, in the report.

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

[Unreleased]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.1.0
