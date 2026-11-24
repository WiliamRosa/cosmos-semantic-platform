# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the authoring
format or the API; such changes are listed under **Changed**.

## [Unreleased]

### Added

- Reconciliation through MCP and REST (DEC-45): the MCP tool `reconcile` and
  `POST /v1/reconcile/{case}` run a reconciliation case the operator declared in
  `COSMOS_CHECK_CASES` (the file `cosmos check --cases` reads), so an agent can ask whether a
  number is still reconciled. They return the outcome, the row counts and totals, the row diff
  (at most 200 rows), the likely causes, the metric's updated health and the evidence, and
  append a partial check run to `COSMOS_RUNS_LOG`: health updates that case and keeps the
  others from the latest `cosmos check`. `GET /v1/reconcile` (the tool without `case`) lists
  the declared cases by id and title. Only declared cases run, never SQL from the caller,
  as the caller, with the same authentication and audit as `query` (event `reconcile`). Row
  filters cannot be applied to hand-written SQL, so a case is refused (403) when a row filter
  applies to it or when its SQL reads anything but the tables of models without row filters.
  An unknown case is a 404, and a case runs at most once per `COSMOS_RECONCILE_MIN_INTERVAL`
  seconds (60 by default; sooner is a 429). A case that cannot run is recorded with its error
  type only. The evidence carries a `reconcile_id`, which is not replayable. Point
  `COSMOS_CHECK_CASES` at the same file as `cosmos check --cases`, since health matches cases
  by id; every check run records the file's sha256 as `cases_hash`. The console's latest check
  and history use full runs only, and its runs list marks partial runs. `cosmos reconcile
  --json` reports gain `diff`, every row that does not match (up to 1,000), and their totals
  are summed exactly (`math.fsum`), so they no longer depend on row order.

- Offsets in the Cube export (DEC-51): `cosmos export cube` writes a derived metric with offset
  inputs as a Cube multi-stage `number` measure with `time_shift: [{interval, type: prior}]`
  (`revenue_prev_month`: `sql: "{revenue}"`, `interval: 1 month`) instead of refusing it. A
  metric that also reads members unshifted, or reads an input through an `alias`, reads each
  shifted reference through a `<metric>__<alias or input>` measure; metrics over a shifted one
  are `multi_stage` too. Intervals keep the offset's unit (`1 year`, `12 months`), except weeks,
  written as days (Cube's Oracle query drops a `week` interval). Checked against Cube's
  Tesseract source: Cube adds the interval to each row's time before truncating it, so at every
  grain the planner accepts (days by day, multiples of 7 days by week, months by month, quarter
  and year in multiples of 3 and 12) the shift moves whole periods, month-end clamping included,
  and the numbers agree; nothing is refused on month-end grounds. Each exported offset is a
  warning on what differs: Cube answers grains the planner refuses, returns periods without
  facts, applies a date range after the shift, and applies no shift to a query without a time
  member. `cosmos import cube` reads a single `prior`
  `time_shift` back as offsets (`N quarter` as `3N months`), folds the `<metric>__<alias>`
  measures back into the metric's input and alias, and skips with a warning `next` shifts,
  `time_dimension` or named shifts, several shifts, other units, shifts of zero, shifts of a
  shifted or cumulative measure and mixed days and months. The re-imported example answers the
  offset reference queries A35 to A38 and A49 to A51 with the hand-written SQL's numbers.

## [0.5.0] - 2026-11-23

This release adds BigQuery as a data source, checked on a live project, and Trino and Starburst in preview. Metrics gain offsets and rolling windows in weeks, offsets in years, growth in one metric, a reconciliation tolerance, filters on metric values and structured time semantics in the evidence. Requests take structured filters on MCP and REST, and `cosmos reconcile --expected` checks a metric against a report's numbers.

### Added

- Structured filters on MCP and REST (DEC-50): `filters` in `query`, `show_sql`
  (`POST /v1/query`, `POST /v1/sql`) and `dimension_values` take objects
  `{"dimension", "op", "value"}` next to expression strings, which keep working unchanged.
  Operators: `=`, `!=`/`<>`, `<`, `<=`, `>`, `>=`, `in` and `not in` with a list, `is null` and
  `is not null`. The server writes the value as a literal of the data source's dialect through
  SQLGlot, so no client quotes or escapes a value; a quote or a backslash in a value stays a
  value on MySQL, MariaDB, Databricks and BigQuery. A structured filter and the equivalent
  expression string give the same SQL and `sql_hash`. Values are checked against the
  dimension's type. `dimension_values` gains `filters` (the MCP argument, and
  `POST /v1/dimensions/{name}/values` with `{"limit", "filters"}`), so it counts only the rows
  they keep; its evidence lists them. The console's query explorer sends structured filters
  instead of quoting values itself. Reference case A55 checks the numbers on every database.
- Reconciliation against a report's numbers (DEC-38): `cosmos reconcile <metric> --by ...
  --expected report.csv` compares the governed metric with a CSV exported from a report
  (Excel, Power BI, a finance close), with one column per dimension and one for the metric. It
  gives the same report as `--sql`, under the metric's declared tolerance or the default:
  rows that agree, agree within tolerance, differ or are on one side only, totals, examples
  and the likely-cause probes. The report's `legacy` is `{"kind": "csv", "file", "file_hash",
  "rows"}`. Dimension columns are named as the request names them or as `cosmos query --csv`
  writes them; time dimensions take ISO dates or `YYYY-MM`, and a categorical cell matches a
  number the database returns (such as a `customer_id`) only when it is that number's exact
  text. Unknown or missing columns,
  non-numeric values, thousands separators, ragged lines and repeated rows are refused with
  the file, line and column. The SQL the engine generates and the output of `--sql`,
  `--other-model` and `--cases` are unchanged. Reconciliation cases in `cosmos check` do not
  read a CSV yet.
- Trino and Starburst as a data source, in preview (DEC-43): `type: trino` with the `trino`
  extra (the `trino` client). The connection is a `uri` such as
  `trino://user@host:443/catalog?http_scheme=https`, with `options` for the password (basic
  authentication), a JWT (`access_token`), TLS verification, roles and session properties; a
  query stops after the source's `timeout_seconds` (`query_max_run_time`). The model's schemas
  map to schemas of the catalog. Weeks start on Monday (Trino's `DATE_TRUNC` is ISO), interval
  arithmetic becomes `DATE_ADD` (Trino's intervals have no `WEEK` or `QUARTER`), and a date
  that gets hours is read as a `TIMESTAMP` first. `docker-compose.yml` has a `trino` profile
  (Trino 484 with an in-memory catalog, on localhost); the comparative suites run on it when
  `COSMOS_TRINO_URI` is set. Offline tests check that every reference query plans and parses
  for Trino, and CI runs the suites against a Trino container.
- Rolling windows in weeks (DEC-27): a cumulative metric accepts `window: 1 week` or
  `2 weeks`, read as 7 days per week. It runs where the window in days runs (day grain only)
  and gives exactly the SQL, and so the `sql_hash`, of `7 days` or `14 days`; the evidence and
  the catalog keep the window as written. `cosmos export dbt` writes it as `window: N weeks`,
  which MetricFlow reads as the same 7N days, and `cosmos import dbt` reads it back as
  `N weeks`. `cosmos export cube` writes its days, `trailing: 7N day`, because Cube does not
  render a `week` interval on every database (its Oracle query drops it), so a Cube round trip
  turns `2 weeks` into `14 days`, with the same numbers. `cosmos import cube` reads a
  `trailing: N week` as `window: N weeks` (it was imported as a window queries refused). Reference cases with
  hand-written SQL (A52 to A54, in `tests/test_reference.py`) check `1 week` and `2 weeks`
  against the 7 and 14-day windows on every database, and the refusal by week.
- Offsets in weeks and years (DEC-46): a derived metric's `inputs` accept `offset: 1 week`,
  `2 weeks`, `1 year` or `2 years`, read as 7 days and 12 months. They run at the grains of
  their days or months (`2 weeks` by day or week, `1 year` by month, quarter or year) and give
  exactly the SQL, and so the `sql_hash`, of `14 days` or `12 months`; the evidence keeps the
  offset as written. Reference cases with hand-written SQL check `1 year` by month and by year
  and `2 weeks` by day on every database.
- Growth in one metric (DEC-44): an offset input of a derived metric takes an `alias`, so the
  expression reads the same metric both shifted and unshifted, such as
  `expr: ({revenue} - {revenue_year_before}) / {revenue_year_before}` with
  `inputs: [{metric: revenue, offset: 12 months, alias: revenue_year_before}]`; no helper
  metric is needed. Several aliases can read one metric at different offsets. Validation
  refuses an alias that is not used in `expr`, names two inputs or is the name of a measure or
  metric, and a metric listed twice in `inputs` without aliases. The evidence's `time.shifts`
  entry names the `alias`, the catalog and formula expand it, and `cosmos export dbt` writes it
  as MetricFlow's input `alias`. The SQL of every existing request is unchanged (13,688
  request-dialect pairs compared with the previous version), and so is the hash of a model
  without aliases. New example metric `revenue_yoy_growth` and reference cases A49 to A51
  (year-over-year growth by month and by year, and beside month-over-month growth by status
  with a filter).
- BigQuery as a data source (DEC-40, [ADR 0016](docs/decisions/0016-bigquery-as-a-data-source.md)):
  `type: bigquery` with the `bigquery` extra (`google-cloud-bigquery`). It logs in with
  Application Default Credentials or a service account key (`options.credentials_file`),
  `options.maximum_bytes_billed` stops a query that would scan more, and the model's schemas map
  to datasets (`schema_map: {public: cosmos_demo}`). Weeks start on Monday (`ISOWEEK`), times
  are `DATETIME` and date arithmetic on a time uses `DATETIME_ADD`, so the numbers match the
  other databases. `cosmos demo-data` loads with free load jobs (they work in the BigQuery
  sandbox), and `cosmos bootstrap` reads the dataset's `INFORMATION_SCHEMA`. On a BigQuery
  sandbox project ([setup](deploy/bigquery-setup.md)), the BigQuery suites pass: 160 tests,
  among them the 66 reference queries, the divergence zoo, the randomized same-number test
  against DuckDB, bootstrap, packs, the Power BI import and the dbt, Cube and Ossie exports.
  The live run found and fixed what the emulator did not: `DATE - DATE` is an `INTERVAL` in
  BigQuery (now `DATE_DIFF`), a CTE named like its own column reads the whole row, and a model
  expression with a doubled quote (`'O''Brien'`) did not parse in the BigQuery dialect.
- A metric can declare its reconciliation tolerance (DEC-39):
  `reconciliation: {abs_tolerance: 0.01, rel_tolerance: 0.001}`. `cosmos reconcile`, the
  reconciliation cases of `cosmos check` and member health use it in place of the fixed default
  (equal up to float noise), so a legacy report rounded to cents or thousands can agree. The
  reconcile report states the `tolerance` that applied (`source: metric` or `default`), counts
  and lists the rows that agree only within it (`rows.within_tolerance`, `tolerated`), and shows
  the `difference` on each example row; check runs record the tolerance per case, health reasons
  carry it when it is the metric's own, and query evidence lists it under the metric. Each
  tolerance must be a non-negative, finite number, and unknown keys are refused. Without the
  field nothing changes: same results, same model hash, same SQL and evidence.
- `deploy/azure/`: a first slice packaging Cosmos Semantic as an Azure Marketplace Managed
  Application offer (ADR 0015), whose resources deploy into the customer's own subscription
  rather than a publisher-run SaaS. A Dockerfile builds `cosmos serve` from source with the
  e-commerce example and a baked-in DuckDB demo dataset (no external database needed to try
  it), `entrypoint.sh` falls back to `--allow-anonymous` until `COSMOS_OIDC_ISSUER` is set, and
  `main.bicep` provisions a scale-to-zero Container App, a Key Vault, and a user-assigned
  managed identity with "Key Vault Secrets User" on it. `createUiDefinition.json` adds one
  wizard step for Microsoft Entra ID wiring. Validated end-to-end in a real subscription
  (ADR 0015's 2026-11-21 update): image build and push, Bicep deploy, and the actual wizard and
  post-deploy overview/side-menu chrome via a private Service Catalog Managed Application. Fixed
  along the way: the `AcrPull` role assignment now targets the registry's own resource group
  through `acr-pull-role.bicep` (a module; it previously defaulted to the deployment's own
  resource group, granting nothing useful), and a new `.dockerignore` keeps the build context
  from including `.venv`. Not done: a production `connections.yml` wired to Key Vault secrets,
  and the Partner Center listing itself.
- `cosmos export dbt` and `cosmos import dbt` carry offsets as MetricFlow's `offset_window`
  (DEC-36). The export writes a derived metric's `inputs: [{metric, offset}]` as the input's
  `offset_window` in the largest unit that holds the offset (`7 days` as `1 week`, `3 months` as
  `1 quarter`, `12 months` as `1 year`), so at every grain the engine accepts MetricFlow shifts
  whole periods, as the engine does, instead of each day; each exported offset is a warning
  naming `metric_time` and where MetricFlow differs (it joins a time spine, so it also returns
  periods without facts, returns grains the engine refuses, and applies `metric_time` filters
  after the shift, while the engine filters the facts first). The import reads `offset_window`
  in days, weeks, months, quarters and years as offsets in days or months, turns an aliased
  input read both shifted and unshifted into a helper metric `<metric>__<alias>`, and warns
  where the numbers differ, including a month or quarter window at a coarser grain. Still
  skipped with a warning: `offset_to_grain`, other units, a zero window, and offsets over a
  cumulative metric or a metric with offsets of its own. Offsets over a model without a time
  dimension are not exported. The re-imported example answers the offset reference queries
  (A35 to A38) with the hand-written SQL's numbers, and offsets of 1, 7 and 14 days and 1, 3
  and 12 months over two models return the native numbers at every grain, on DuckDB and every
  database configured in CI.
- A query explorer in the console (DEC-41), the read-only first slice of the phase-4 playground:
  pick a metric, dimensions, a time dimension and grain, filters (a dimension, an operator and a
  value; never SQL) and business rules, then run it or only show its SQL. It shows the result
  table, the generated SQL and the evidence envelope: rules, row filters and user context,
  fingerprints and caveats, with a link to the audited answer. It calls `POST /v1/query` and
  `POST /v1/sql` with the console's credential, so the same row filters apply and every run is
  audited like any API query. It is in the sidebar and in `Ctrl+K`, in English and Portuguese;
  the public demo explains that it needs `cosmos serve`.
- A social media agent for the project's LinkedIn page ([ADR 0014](docs/decisions/0014-social-media-agent.md)):
  on weekday eves it opens one pull request labelled `social` with the next business day's post, its
  visual and the source of every claim, under `marketing/linkedin/` (playbook, page copy, brand assets).
  Merging is the maintainer's approval, and the maintainer publishes; no agent posts.
- `cosmos import powerbi` reads period-over-period measures as offset inputs (DEC-35):
  `CALCULATE([m], SAMEPERIODLASTYEAR('Date'[Date]))`, `DATEADD('Date'[Date], -N, DAY | MONTH |
  QUARTER | YEAR)` and `PARALLELPERIOD('Date'[Date], -N, MONTH)` become a derived metric with
  `inputs: [{metric, offset}]` (`N days`, `N months`, `3N months`, `12N months`), and growth
  measures built on them, such as `DIVIDE([m] - [m PY], [m PY])`, come along as derived
  metrics. The date column must belong to an imported date table that the measure's table
  reaches through one imported relationship. Each imported measure gets one warning that names
  the time dimensions to query it by and the differences from DAX, which moves a set of dates
  where Cosmos moves the period start: a date filter that cuts a period, or a date table that
  ends inside one, gives DAX a partial earlier period while Cosmos filters the earlier period's
  facts too; other columns of the date table (a weekday) give different numbers; Power BI also
  shows periods without facts; and by day, where DAX clamps month ends, a month offset is
  refused. Adding or subtracting a shifted measure, or a ratio over one, gets a warning too (DAX
  counts BLANK as zero in `+` and `-`). `DIVIDE` with an alternate result other than `BLANK()`
  (`DIVIDE(a, b, 0)`) is imported with a warning that Cosmos returns NULL where DAX returns the
  alternate result, and `[A] / DIVIDE([B], [C])` now imports as a / (b / c). `PARALLELPERIOD` by quarter or year (the whole earlier quarter or year, even
  by month), forward shifts, other `CALCULATE` filters, the fact's own date column, shifts of a
  running total or of another shifted measure, and other units stay warnings. An equivalence
  test checks them against hand-written SQL and a model of DAX by day, week, month, quarter,
  year and by month and buyer, on DuckDB and every database configured in CI.

- `cosmos export dbt` writes running totals since the start of a period (DEC-31): a cumulative
  metric with `grain_to_date` over a `sum` or `count` measure becomes a MetricFlow `cumulative`
  metric with `cumulative_type_params: {grain_to_date: <period>, period_agg: last}`, which
  `cosmos import dbt` reads back as the same metric, so the example's `revenue_year_to_date`
  and `revenue_month_to_date` no longer need `--allow-metric-loss`. MetricFlow sums the facts
  from the start of each day's period up to the day, and `period_agg: last` keeps each coarser
  bucket's last day (MetricFlow's default, `first`, would keep its first day), so by the
  model's `agg_time_dimension` at day grain, and at every grain the engine accepts for the
  period, the buckets both return have the same numbers. A warning per metric says so and
  lists where they differ: MetricFlow also returns days after the first fact of a period that
  have no facts, answers the grains the engine refuses (coarser than the period, DEC-21, and
  weeks under a month, quarter or year, DEC-28), reads the facts from the start of the period
  when a time constraint starts inside it while the engine filters first, and always
  accumulates over `metric_time`. Running totals over `min` or `max` measures, without a period,
  or over a model without a time dimension stay refused, each with its reason. The re-imported
  example answers 30 reference queries (52 with its policies), A08, A09, A32 and A39 among them,
  and running totals for every period over `sum` and `count` measures of two models return the
  native numbers at every grain the planner accepts, with another dimension and with a time
  filter. The planner and the SQL of every request are unchanged
  ([interoperability](docs/interoperability.md#exporting-dbt)).
- Structured time semantics in the evidence (DEC-37): each entry of `metrics` carries a `time`
  block for cumulative and offset metrics (null for the others), so the time logic is no longer
  only inside the `formula` text. It holds `kind` (`grain_to_date` with its `period`, null for a
  running total over all time; `window` with the `window` as written and its `days`; or
  `offset` with its `shifts`, each the metric that reads an input at an earlier period, the
  input and the offset, through nested metrics too), the request's `time_dimension` and its
  `query_grain`, and `filters_applied`, which is `before` today: the request's filters, the
  row filters and a metric's own filter restrict the rows before the window or shift (a test on
  DuckDB checks the values, so a change to this order must update the field). Fields only; no SQL, number or hash
  changes ([architecture](docs/architecture.md#evidence-envelope)).
- Filters on metric values, first slice (DEC-29): a request (`POST /v1/query`, `POST /v1/sql`,
  the MCP tool `query`, `cosmos query` and `cosmos sql` with the repeatable `--metric-filter`)
  takes `metric_filters` such as `{revenue} > 1000`, which keep only the rows of the result
  whose metric values meet them, after aggregation at the requested grain. The row filters and
  the request's `filters` still apply to the facts first; `order_by` and `limit` apply to the
  rows kept. A condition compares one requested metric with a plain decimal number (`=`, `<>`,
  `<`, `<=`, `>`, `>=`) or tests `IS NULL` / `IS NOT NULL`, joined by `AND`; the text is parsed
  strictly and never reaches the SQL, and anything else (`OR`, parentheses, functions, strings,
  comments, exponents, an unrequested metric) is refused. Simple, ratio and derived metrics are
  allowed; requests with cumulative metrics or with inputs read at an earlier period are refused
  with `metric_filters` for now. The planner computes the result into a CTE and keeps the rows
  with an outer `WHERE` (SQL Server accepts `WITH` only at the top). The evidence lists the
  conditions in `metric_filters`, next to `policies.row_filters`, with a caveat that a sum of
  the rows kept is not the total. Reference cases A40 (a simple metric under a row filter, with
  the limit after the filter) and A41 (a ratio, with a null group) have hand-written SQL with
  `HAVING`; A42 and A43 are refusals; the randomized cross-database test draws 8 requests with
  metric filters from its own generator. The SQL of every request without `metric_filters` is
  unchanged ([semantic model](docs/semantic-model.md#filters-on-metric-values)).
- `cosmos export cube` writes rolling windows in days (DEC-30): a cumulative metric with
  `window: N days` over a `sum`, `count`, `min` or `max` measure becomes a measure of that type
  with `rolling_window: {trailing: N day, offset: end}`, so the example's
  `revenue_rolling_7_days`, `smallest_order_rolling_7_days` and `customers_rolling_30_days` are
  no longer refused. With `offset: end`, Cube's window for a day bucket is the N days ending on
  that day, the row's day included, as in Cosmos. `cosmos import cube` reads it back as the same
  window; a trailing window in days with `offset: start` or `leading` does not end on the row's
  day and is now skipped with a warning instead of being imported as a Cosmos window, and so
  is a trailing window over an `avg` or `count_distinct` measure (it would not validate in
  Cosmos); a skipped window leaves no `<name>__base` measure behind. The
  docs list where Cube's window differs: days without facts, grains coarser than a day, date
  filters (Cube reads the days before the range), a joined cube's day and rows with a null
  time. Ratio and derived metrics over a window and offsets stay refused. Tests: the
  re-imported example answers 30 reference queries (47 with its policies) with the
  hand-written SQL's numbers, A29, A30, A33 and A34 among them, and windows of 1, 7 and 30
  days over measures of two cubes return the native numbers by day, by day and another
  dimension, and with a time filter. The planner and the SQL of every request are unchanged.
- `cosmos export dbt` writes rolling windows in days (DEC-25): a cumulative metric with
  `window: N days` over a `sum`, `count`, `min` or `max` measure becomes a MetricFlow
  `cumulative` metric with `cumulative_type_params.window: N days`, which `cosmos import dbt`
  reads back as the same metric, so the example's `revenue_rolling_7_days`,
  `smallest_order_rolling_7_days` and `customers_rolling_30_days` no longer need
  `--allow-metric-loss`. Both windows cover the days D - (N - 1) to D; a warning per metric
  says where MetricFlow's numbers are the engine's (by the model's `agg_time_dimension` at day
  grain, on the days with facts) and where they differ: MetricFlow also returns days without
  facts and coarser grains, its time constraint does not cut the window while the engine
  filters the facts first, and it always windows over `metric_time`, never over another time
  dimension of the model. Running totals (`grain_to_date` or without a window), windows in other
  units, windows over a model without a time dimension, and ratio or derived metrics over a
  cumulative metric (the planner does not compute one inside another metric) stay refused,
  each with its reason; offset metrics too. The re-imported example answers 28 reference
  queries (44 with policies), A29, A30, A33 and A34 among them; windows of 1, 3, 7 and 30 days
  over four aggregations of two models return the native numbers by day, by day and another
  dimension, and with a time filter
  ([interoperability](docs/interoperability.md#exporting-dbt)).
- Date range of a time dimension for agents (DEC-24): the MCP tool `dimension_values` and
  `GET /v1/dimensions/{name}/values` now accept time dimensions and return
  `{min, max, distinct_days}` instead of a list of values: the first and last day holding rows
  of the dimension's model, as ISO dates, and how many distinct days do, so an agent knows
  which dates hold data before answering "last month" or "since launch". Days are the model's
  local days (its business `timezone`; model timestamps are local and never converted), the
  same days as a `__day` grouping, whose path the planner reuses; rows without a time hold no
  day. The caller's row filters apply, so a principal never learns the range of rows it cannot
  see; with no visible rows the answer is `null`, `null` and 0. Personal and sensitive time
  dimensions are refused, a grain (`ordered_at__month`) is refused, `limit` does not apply,
  and the evidence (with the `timezone`) and the `dimension_values` audit event are the same as
  for values. Tests compare with hand-written SQL on every database for DATE and TIMESTAMP
  columns, with a row filter and with no visible rows. The SQL of every existing request is
  unchanged.
- Freshness in member health (DEC-26, #66): `explain` (MCP and `GET /v1/explain/{name}`) and
  the console's member view now return `last_reconciled_at` and `last_tests_run_at` (ISO
  timestamps of the latest check run, null when never run), and the `reconciled` and
  `reconciliation_failed` reasons carry the run's `at`. A new optional project setting,
  `health: {max_age: 7 days}` (hours, days or weeks), makes a metric whose last reconciliation
  matched but is older than that `attention`, with the reason `reconciliation_stale`. Without
  the setting every state is as before. The console shows both timestamps on a member's page
  ([member health](docs/architecture.md#member-health)).
- Period-over-period metrics, first slice (DEC-18): a derived metric can read an input at an
  earlier period with `inputs: [{metric: revenue, offset: 1 month}]`, so `revenue_prev_month`
  and `revenue_mom_growth` (`({revenue} - {revenue_prev_month}) / {revenue_prev_month}`) are
  now in the example model and the retail pack, with `order_count_7_days_earlier`. The value
  for a period is the input's value for the period that starts the offset earlier, at the
  query's time grain; the rows are the periods with facts, and a period whose earlier period
  has none gets NULL (a count too). Offsets in days run by day (or by week in multiples of 7),
  offsets in months by month (or by quarter or year in multiples of 3 or 12); other units and
  grains, requests without exactly one time dimension, offsets of cumulative metrics and
  offsets over metrics that have offsets are refused, at validation or at query time. The
  planner adds one aggregate per offset and joins it on its period moved forward, so only
  period starts are shifted and no database clamps a month end. Reference cases A35 to A38
  (hand-written SQL that pairs months by number, across a year change and a month without
  orders), exact numbers on a table built for the edge cases, and draws of their own in the
  randomized cross-database test. The SQL of every existing request is unchanged. The Cube and
  dbt exports refuse these metrics for now, and Ossie keeps them only in the Cosmos extension
  ([semantic model](docs/semantic-model.md#period-over-period)).
- `cosmos import powerbi` reads rolling windows from `DATESINPERIOD` (DEC-23):
  `CALCULATE([m], DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -N, DAY))` over a `sum`,
  `count`, `min` or `max` measure becomes a cumulative metric with `window: N days`. At day
  grain, the only grain the engine answers a window at, DAX's window ends at the row's day as the
  engine's does. A warning per measure names the day dimension to query it with (other date
  dimensions give different numbers) and the differences: grouping or filtering by other columns
  of the date table gives different numbers, since the DAX filter on the date column removes
  those filters while Cosmos keeps one window per value; Power BI also shows days without facts
  and coarser periods; and a date slicer does not cut its window. Other units, forward windows,
  other anchors (`LASTDATE`, `TODAY()`), extra filters, an unrelated date table, `avg` and
  `count_distinct` measures, and `DATESBETWEEN` stay warnings. Tests check the numbers against hand-written SQL on every database, by day and by
  day and buyer, with days without facts inside the windows and a fact without a date
  ([interoperability](docs/interoperability.md#importing-power-bi)).
- `cosmos export cube` carries row filters and sensitivity (DEC-20): each cube gets one
  `access_policy` for every role (`role: "*"`) with the model's row filters as `row_level`
  filters, `$user.<field>` as `{ securityContext.<claim> }`, and personal or sensitive
  dimensions as `member_level` excludes; `cosmos import cube` reads them back as the same row
  filters and sensitivity. Only filters with an exact Cube equivalent are written (`=`, `IN`, and
  `<`, `<=`, `>`, `>=` on numbers, joined by AND and OR); anything else, such as a filter that
  reads a business rule, `<>` (Cube's `notEquals` keeps null rows) or a function, is refused
  until `--allow-policy-loss`, which turns each one into a warning. The example no longer needs
  `--allow-policy-loss`. `cosmos import cube` fails closed too: a cube whose `access_policy`
  Cosmos cannot express (a role-specific or second policy, `notEquals`, a member of another
  cube, an exclude without `meta.sensitivity`, ...) is refused, since the import would grant
  more access than Cube did, until `cosmos import cube --allow-policy-loss`, which warns for
  each loss; two attributes that would become the same `$user` field are an error. Your Cube
  `checkAuth` must reject a security context that lacks an attribute the policies read, or holds
  a list (Cosmos denies those queries); the export warns about it. `member_level` hides personal
  and sensitive dimensions from every Cube user, while Cosmos only refuses them to agents. Not
  verified against Cube's runtime: whether `role: "*"` matches users with no roles, whether a
  policy is enforced through views and joins, and how Cube treats a missing or list
  `securityContext` attribute
  ([interoperability](docs/interoperability.md#exporting-cube)).
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
- Offline tests for BigQuery (EXTRA-2), with a fake `google.cloud.bigquery` client and no
  project or credentials: each query job's timeout, `maximum_bytes_billed`, `job_retry=None`
  and row limit; `load_bigquery` (Parquet, an explicit schema with times as `DATETIME`,
  `WRITE_TRUNCATE`, a missing dataset created); `cosmos bootstrap` reading
  `` `<dataset>`.INFORMATION_SCHEMA `` and `INT64`, `FLOAT64` and `BIGNUMERIC` as numbers; the
  `bigquery` block of `cosmos init`'s `connections.yml`; and `credentials_file`, read only from
  `options` and never logged.

### Changed

- `cosmos validate` and `cosmos check` refuse a metric whose offsets mix days (or weeks) with
  months (or years), its own or those of the metrics it uses (DEC-46): an offset in days runs
  by day or week and one in months by month, quarter or year, so no grain can answer it. Such
  a metric used to pass validation and fail on every query; the error now names both offsets
  and sits on the metric where the mix appears.
- Date filters no longer cut rolling windows short (DEC-34). A request filter on a rolling
  window's time dimension now chooses the output days, and each day's window still reads the
  N − 1 days before it, as in Power BI, Cube and MetricFlow; other filters (status, region, row
  filters) still apply to the facts first. **Filtered rolling-window queries change their numbers
  by design:** the first N − 1 days of a filtered range become full-window numbers instead of
  sums of the days kept (reference case A30 now expects them). The planner splits ranges (`>=`,
  `>`, `<`, `<=`, `BETWEEN`), equalities and `IN` lists on the time dimension (the column, or it
  at day grain or coarser), joined by `AND` and `OR`: the window reads its facts from a second
  daily aggregate, `<model>_agg_lookback_<N>_days`, whose lower bounds are moved back with each
  dialect's date addition (N days on the column, N − 1 at a grain), while the request's filters
  still choose the days; on the column itself an upper bound is read one day earlier, so a
  bound inside a day (`<= ... 12:00`) keeps that day's later facts in its window. Other shapes (`NOT`, `<>`, `NOT IN`, functions of the time dimension,
  the hour grain, `OR` with another member) keep today's behaviour and are documented. The SQL
  of every request without a filter on a rolling window's time dimension is unchanged (a test
  pins its `sql_hash` on every dialect). The evidence's `time.filters_applied` is `time_after`
  for rolling windows, or `before` when an unsplit filter cuts them: a new value, which
  unfiltered rolling-window queries now carry too (they had `before`); the Power BI and dbt
  export warnings no longer list date filters among the differences. Reference cases A44 (a
  `BETWEEN` range with a status filter), A45 (a date column, a 30-day window and a row filter),
  A46 (an `IN` list of days, a `min` window), A47 (a `NOT` filter that still cuts) and A48 (an
  upper bound at noon of a day with orders after it) have hand-written SQL; on Oracle,
  `CAST(... AS DATETIME)` in a request filter now becomes `TIMESTAMP`, and the randomized cross-database test draws 8 date-filtered window
  requests. Running totals (`grain_to_date`) and offsets still filter first; they are the next
  slices ([semantic model](docs/semantic-model.md#rolling-windows)).
- Rolling windows close the gaps of the #57 review (DEC-22). `cosmos validate`, `cosmos check`
  and every strict model load now refuse a cumulative metric with `window` over an `avg` or
  `count_distinct` measure, with the reason the query used to give; before, the model was valid
  and only a query of the metric was refused. No number changes and the SQL of existing requests
  is the same. The example model gains `smallest_order` (`min`), `customer_records` (`count`) and
  their rolling metrics `smallest_order_rolling_7_days` and `customers_rolling_30_days`, with
  reference cases A33 (null segments and null days under a row filter) and A34 (a `min` window).
  The randomized cross-database test draws rolling metrics at day grain of its own, and the TPC-H
  model gains `revenue_rolling_7_days` with case T08, so the baseline times the range self-join.

### Fixed

- The Power BI import tests skip their BigQuery cases on the emulator, which panics on them.
- `cosmos import cube` and `cosmos import dbt` skip a running total over an `avg` or
  `count_distinct` measure, with a warning naming it, instead of writing a model that
  `cosmos validate` then rejects (DEC-32). Since DEC-21 such a metric is invalid in Cosmos: an
  average or a distinct count to date cannot be accumulated from per-period values. Cube
  skips the measure with a `to_date` (or any other) `rolling_window`, with no `__base` measure
  left behind; dbt skips the cumulative metric and keeps its measure, using the validator's own
  condition. The metrics that use a skipped one (Cube `number` measures, dbt ratio and derived
  metrics), directly or through another, are skipped too, each with a warning naming what it
  depends on. The Power BI import already did this (#69). No SQL or number changes.

- A month-, quarter- or year-to-date metric queried by week is refused with an error instead of
  returning a wrong number (DEC-28). A week does not nest in a month, quarter or year, and the
  planner put each week in the period it starts in, so the week that crosses into a new period
  was counted in the old one: on the test data (600 customers, seed 7, anchored on 2026-11-16),
  `revenue_year_to_date` for the week of 2025-12-29 was 1,870,457.06, more than all of 2025's
  revenue (1,823,298.63), and any data gives more than the whole year's revenue there. The error says
  why and names the grains that work (day, month and up to the period). It shares one check
  with the refusal of a grain coarser than the period (DEC-21). Week-to-date metrics, running
  totals without `grain_to_date` and every request by day are unchanged; the SQL of every
  request still accepted is byte-identical. Derived metrics and offsets cannot wrap a
  cumulative metric, so no request reaches the number through them. Reference case A39
  (year-to-date by week, refused); the Cube and dbt notes now name the refusal
  ([semantic model](docs/semantic-model.md#period-to-date)).

  **Compatibility:** a request for a month-, quarter- or year-to-date metric with a time
  dimension at `__week` planned before and now fails; its numbers were wrong at period
  boundaries.

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

[Unreleased]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.3.0
[0.2.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.2.0
[0.1.0]: https://github.com/WiliamRosa/cosmos-semantic-platform/releases/tag/v0.1.0
