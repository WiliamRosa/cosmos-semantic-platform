# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the authoring
format or the API; such changes are listed under **Changed**.

## [Unreleased]

### Added

- `cosmos export cube` carries running totals (DEC-16): a cumulative metric with
  `grain_to_date` over a `sum` or `count` measure becomes a Cube measure with
  `rolling_window: {type: to_date, granularity: ...}`, and `cosmos import cube` reads it back as
  the same metric, so the example's year-to-date and month-to-date revenue no longer need
  `--allow-metric-loss`. Rolling windows in days, running totals over other aggregations or
  without `grain_to_date`, and ratio or derived metrics over a cumulative metric are still
  refused, each with its reason. The re-imported model answers the cumulative reference queries
  (A08, A09) with the hand-written SQL's numbers; Cube itself must be queried with the time
  dimension, and its date range reads rows back to the start of the period
  ([interoperability](docs/interoperability.md#exporting-cube)).
- `explain` over MCP and `GET /v1/explain/{name}` carry the member's `health` (DEC-15), so an
  agent about to quote a number can tell whether it is checked: `healthy`, `attention`,
  `failing` or `unchecked`, with the reasons (rule tests, last reconciliation). Metrics,
  measures, rules and processes have it, and so does the member a concept maps to. It is the
  console's computation, now shared in `cosmos_semantic.health`; the console is unchanged.
- Dimension values for agents (DEC-17): the MCP tool `dimension_values` and
  `GET /v1/dimensions/{name}/values` return the distinct values of a categorical or boolean
  dimension, each with the number of rows of the dimension's model that carry it (nulls included),
  most frequent first, then by value. An agent wanting "revenue in Brazil" no longer guesses
  `Brazil` against `BR`. The SQL comes from the planner, so the caller's row filters apply as in
  `query`; personal and sensitive dimensions are refused on every channel; time and number
  dimensions are refused; at most 100 values are returned (`limit` asks for fewer). Ties are
  broken by value in the engine, so every database keeps the same values at the cut-off; past
  10,000 distinct values the list is marked `truncated` and ties at that cut-off may differ.
  Each call is written to the audit log as a `dimension_values` event, which `replay` and the
  usage report ignore.

### Fixed

- Cumulative metrics no longer return silently wrong numbers (DEC-21). A `grain_to_date`
  metric (month-to-date, year-to-date) over a `min` or `max` measure now takes the minimum or
  maximum so far (`MIN`/`MAX OVER`); before, it summed the daily values, so "largest order so
  far this year" was a sum of daily maxima. Over an `avg` or `count_distinct` measure it is an
  error in `cosmos validate` and `cosmos check` and is refused by the planner, since an average
  or a distinct count to date cannot be rebuilt from daily values (a customer counted once per
  day); the same holds for a running total without `grain_to_date`. Queried at a grain coarser
  than its period (a month-to-date by quarter or year) it is refused instead of returning the
  quarter's total. `sum` and `count` cumulative metrics generate byte-identical SQL. `cosmos
  import powerbi` imports `TOTALYTD`, `TOTALQTD` and `TOTALMTD` over `min` and `max` measures
  as running minima and maxima, and reports the ones over an average or a distinct count as
  not imported. The example model gains the `max_order_value` measure and the
  `max_order_value_year_to_date` metric, with reference cases A31 (hand-written SQL) and A32
  (month-to-date by quarter, refused). The evidence formula of a running minimum or maximum
  says so (`running maximum of MAX(orders.amount) within each year`).

  **Compatibility:** a model with a cumulative metric over an `avg` or `count_distinct`
  measure passed `cosmos validate` before and now fails it; such a metric returned wrong
  numbers.

- Ossie import without the root `COSMOS_SEMANTIC` extension keeps the business time zone for
  every model (DEC-19). The export now copies `timezone` into every dataset's extension, not
  only those with row filters, and the fallback import reads it, refusing copies that disagree.
  Before, a model without row filters came back in UTC, so "today", month-to-date and the rule
  version in effect moved to the next day in the evening (New York, São Paulo).

## [0.4.0] - 2026-11-13

Import and export with other tools grow: Ossie and Cube export, `cosmos query --csv`, and more
Power BI measures. A TPC-H performance baseline, impact analysis and replay over REST and MCP, and
a supervised agent team are also new.

### Added

- Rolling-window cumulative metrics in days (DEC-14): `window: 7 days` on a `cumulative` metric
  aggregates the measure over the facts dated from D − 6 days to D, for each day D of a request
  at day grain (dbt MetricFlow's convention). Only days with facts appear, as with
  `grain_to_date`. `sum`, `count`, `min` and `max` measures are supported; `avg` and
  `count_distinct`, other units and other grains are refused with an error. The example model
  gains `revenue_rolling_7_days`, with reference cases A29 and A30.

- `cosmos query --csv` prints the result as CSV for spreadsheets and scripts: a header, then one
  line per row, with raw values and nulls as empty fields. Standard output carries only the CSV,
  and `--csv` with `--json` is an error. Contributed by @zheng-guannan577 in #24, the project's
  first external contribution.

- `cosmos import powerbi` imports `DISTINCTCOUNTNOBLANK` as a `count_distinct` measure, and
  flags every `DISTINCTCOUNT` measure in the import report: DAX counts BLANK as a value, while
  `count_distinct` ignores nulls, so the imported measure is one lower whenever the column has
  nulls. Contributed
  by @bfs2021 in #11 (fixes #4).

- `cosmos import powerbi` imports `SUMX` over the measure's own table as a `sum` measure whose
  `expr` is the row expression, when that expression is `+`, `-` and `*` over the table's
  numeric columns and numbers (for example `SUMX('Order Items', [quantity] * [unit_price])`).
  BLANK keeps its DAX meaning: `BLANK * x` is NULL, and `+`/`-` treat one missing side as zero.
  Division, functions, `RELATED`, `FILTER`, other tables and the other iterators stay warnings.

- `cosmos import powerbi` imports `AVERAGEX`, `MINX` and `MAXX` over the measure's own table as
  `avg`, `min` and `max` measures, with the same row expressions as `SUMX` (DEC-12). The row
  expression is NULL exactly where DAX gives BLANK, so the skipped rows match: a row with
  `BLANK + 5` counts as 5, a row with `BLANK * 5` is skipped. `avg` averages as a decimal on
  every database, as DAX does, even over integer columns. Division, functions, `RELATED`,
  `FILTER` and other tables stay warnings.

- A TPC-H model in `examples/tpch` and a performance baseline on DuckDB (DEC-4). Seven metric
  requests in the shapes of TPC-H queries (only one is exactly a TPC-H query, Q14's promotional
  revenue share), among them a ratio across two grains, have hand-written SQL: the default test
  run checks them at scale factor 0.01, and `uv run pytest -m perf tests/perf` times compiling
  each request, running the engine's SQL and running the hand-written SQL at scale factor 1
  (`COSMOS_TPCH_SF` picks another), and checks the results are equal. The data comes from
  DuckDB's `tpch` extension; without network access the tests are skipped, or fail with
  `COSMOS_REQUIRE_TPCH=1`. See "Performance baseline" in `docs/architecture.md`.

- A supervised team of AI agents for daily evolution ([ADR 0013](docs/decisions/0013-supervised-agent-team.md)): an
  orchestrator and four specialists in `.claude/agents/`, the project's working rules in
  `CLAUDE.md`, and a daily report, posted as an issue labelled `daily`, that the maintainer answers.

- `GET /v1/impact/{kind:name}` and the MCP tool `impact` expose `cosmos impact` to applications and
  agents: what a change to a metric, measure, rule, dimension, model, concept, system or process
  affects, grouped by owner, source systems and processes. Who queried the affected members, from
  the audit log, is personal data and is returned only to the subjects in `COSMOS_CONSOLE_ADMINS`
  and to the local anonymous user that `--allow-anonymous` creates (local development); for
  everyone else `consumers` is absent and `consumers_visible` is `false`. An unknown member is a
  `QueryError` (HTTP 400).

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
  each concept's labels and definition as `ai_context` of the member it maps to. Business rules
  and business context are exported too (see the next entry).
  Round-trip and same-numbers tests on every database, including the row-filter and
  sensitivity reference queries ([interoperability](docs/interoperability.md#exporting-ossie)).

- `cosmos export ossie` exports business rules (`predicate`, `classification`, `derived`, with
  owner, effective dates, change notes, tests and every earlier version in `history`) and the
  business context (teams, systems, processes) in the `COSMOS_SEMANTIC` extension, and
  `cosmos import ossie` restores them exactly, so queries replayed as of a past date use the
  same rule version and give the same numbers. Glossary concepts that map to a rule are exported
  too. A row filter that reads a business rule is now exported with the rule (also copied next
  to its dataset, with the rules it depends on and the business time zone, for imports of
  documents whose root extension was dropped), so no model needs
  `--allow-policy-loss`; the flag is still accepted by `cosmos export ossie`, for
  compatibility, and has no effect. The round-trip test
  now runs all 48 reference cases. Teams, systems, processes (with `contact` and `owner`) and
  rule test values now leave with the exported file (DEC-10,
  [interoperability](docs/interoperability.md#exporting-ossie)).

- `cosmos export cube` writes the model as Cube static YAML: one cube per model, joins from the
  entities, dimensions (the primary key as a hidden dimension when none holds it), measures,
  simple metrics as measures with `filters`, and ratio and derived metrics as `number` measures,
  with a `1e0 *` factor so that division and AVG are real (division is null on a zero
  denominator), close to the planner's `CAST(x AS DOUBLE)`. `cosmos import cube` reads the export
  back with the same numbers in Cosmos. Metrics that span
  cubes are exported with a warning (Rule 3). Row filters and sensitivity fail closed unless
  `--allow-policy-loss` is given, and cumulative metrics and filters beyond the measure's own
  cube unless `--allow-metric-loss` is given. Business rules, the glossary, business context,
  user context and the time zone are listed as warnings on standard error; model timestamps are
  local, so Cube's query time zone should stay UTC. Round-trip and same-numbers tests, through
  the Cosmos importer and planner, on every database
  ([interoperability](docs/interoperability.md#exporting-cube)).

- `cosmos export dbt` writes the model as dbt Semantic Layer (MetricFlow) YAML (DEC-11, first
  slice): semantic models with entities, dimensions and measures (each with `create_metric`),
  and simple, ratio and derived metrics. Filters become
  `{{ Dimension('entity__dimension') }}`, and division in derived metrics and AVG get the Cube
  export's `1e0 *` factor. `cosmos import dbt --schema <schema>` reads the export back with the
  same numbers in Cosmos, and now also reads measure descriptions, owners from
  `config.meta.owner`, and `number` and `boolean` dimension types kept in
  `config.meta.cosmos_type`. Row filters and sensitivity fail closed unless
  `--allow-policy-loss` is given, and cumulative metrics, filters on a time dimension, a
  business rule or `$user`, and metrics that use one of these unless `--allow-metric-loss` is
  given; cumulative metrics come in a later slice. Where a metric filter matches no rows in a
  group, the engine returns 0 for a count, and MetricFlow no row (alone) or NULL (alongside
  other metrics), so ratio and derived metrics over a filtered count are refused the same way,
  and so is a filtered ratio that reaches a count unless it is two plain inputs on one model.
  Business rules, the glossary, business
  context, user context and the time zone are listed as warnings on standard error. Round-trip
  and same-numbers tests, through the Cosmos importer and planner, on every database
  ([interoperability](docs/interoperability.md#exporting-dbt)).

- The console pages every long list: the catalog (search, filters, sort and page in the address,
  so a link reproduces the view), audited answers, check runs and the usage tables, each sortable
  by column. `GET /v1/console/answers` and `GET /v1/console/runs` take `offset` next to `limit`
  and return `total`, so every answer in the audit log is reachable, not only the latest 50.
  `Ctrl+K` (`⌘K`) jumps to any metric, measure, rule, concept, process or page, and `/` searches
  the list on the page ([console](docs/console.md#find-your-way)).

### Changed

- Tests render CLI help as plain, unwrapped text everywhere, so a test reading `--help` behaves
  the same on CI, where Typer otherwise forces a coloured terminal (#25).

- The console has a new layout: a collapsible sidebar (a drawer on phones), a light, dark or
  system theme remembered in the browser, the Inter font self-hosted at build time, loading
  skeletons instead of a "Loading" line, a visible focus ring for keyboard users, and "Back"
  links that return to the filtered list. The public demo greets first-time visitors with three
  places to start.

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

[Unreleased]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.3.0
[0.2.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.2.0
[0.1.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.1.0
