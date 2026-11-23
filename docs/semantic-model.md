# Semantic model

Users write in our own YAML format, with the dbt Semantic Layer vocabulary (entities, dimensions, measures and metric types) and an ontology layer (SKOS glossary and business rules) on top. Our engine reads the model, plans each query and generates the source's SQL. dbt, Ossie and Cube models come in through import, and the same model can be exported to Ossie, Cube and dbt. See [ADR 0002](decisions/0002-dbt-inspired-authoring-format.md), [ADR 0004](decisions/0004-own-semantic-engine.md) and [ADR 0007](decisions/0007-connectors-sources-identity-and-import.md).

## Version and JSON Schema

Every file starts with `version: 1`. The format's JSON Schema lives in `schema/cosmos-semantic.v1.schema.json`, generated from the compiler's Pydantic models (`cosmos schema`). With it, VS Code and other editors validate and autocomplete the YAML without a plugin:

```yaml
# yaml-language-server: $schema=../../schema/cosmos-semantic.v1.schema.json
version: 1
```

The dbt vocabulary is kept so that dbt users can read a model right away, but Cosmos does not depend on dbt's format, which was reworked in 2026 and keeps the previous one as legacy.

## Full example

The complete, runnable model is in [examples/ecommerce/semantic](../examples/ecommerce/semantic). A shorter version:

```yaml
version: 1
timezone: America/New_York

semantic_models:
  - name: orders
    table: public.orders
    description: Sales orders, one row per order
    entities:
      - name: order
        type: primary
        expr: order_id
      - name: customer
        type: foreign
        expr: customer_id
    dimensions:
      - name: ordered_at
        type: time
      - name: status
        type: categorical
      - name: has_discount
        type: boolean
        expr: discount > 0
    measures:
      - name: order_count
        agg: count
      - name: revenue
        agg: sum
        expr: amount
        description: Gross order value
      - name: discounts
        agg: sum
        expr: discount

  - name: customers
    table: public.customers
    entities:
      - name: customer
        type: primary
        expr: customer_id
    dimensions:
      - name: region
        type: categorical
      - name: segment
        type: categorical
      - name: email
        type: categorical
        sensitivity: personal
    measures:
      - name: customer_count
        agg: count_distinct
        expr: customer_id
    row_filters:
      - name: by_region
        filter: "{region} = $user.region"

user_context:
  region:
    claim: region
    required: true

metrics:
  - name: completed_revenue
    type: simple
    measure: revenue
    filter: "{status} = 'completed'"
  - name: average_order_value
    type: ratio
    numerator: revenue
    denominator: order_count
  - name: net_revenue
    type: derived
    expr: "{revenue} - {discounts}"
  - name: revenue_month_to_date
    type: cumulative
    measure: revenue
    grain_to_date: month
```

## Elements

### Project settings

| Field | Required | Description |
| --- | --- | --- |
| `version` | Yes | Format version; currently `1` |
| `timezone` | No | Business time zone (IANA name). Defines "today" for `CURRENT_DATE` in rules and filters, identically on every data source. Default: UTC |
| `health.max_age` | No | How old a metric's last reconciliation may be, in hours, days or weeks (`7 days`), before its health is `attention` with `reconciliation_stale` instead of `healthy` ([member health](architecture.md#member-health)). Default: no limit |

### Semantic model

A table or view in the data source.

| Field | Required | Description |
| --- | --- | --- |
| `name` | Yes | Unique name in the project |
| `table` | Yes | Table or view in the source |
| `description` | No | Text for people and AI agents |
| `data_source` | No | Data source name, when there is more than one |
| `entities` | No | Keys that link models together |
| `dimensions` | No | Attributes to group and filter by |
| `measures` | No | Basic aggregations |
| `row_filters` | No | Filters applied according to the user context |

### Entities

Instead of declaring joins, users declare keys.

| Type | Meaning |
| --- | --- |
| `primary` | Identifies a row of the model; at most one model per entity name |
| `unique` | A unique value in the model that is not the primary key |
| `foreign` | A reference to another model's primary or unique entity |

A `foreign` entity in model A and the same `primary` or `unique` entity in model B form an edge of the join graph. The planner walks this graph, across several hops if needed (order items → orders → customers). An ambiguous path is an error. Dimensions on the "many" side of a relationship (order revenue by product category) are attributed through a bridge of distinct pairs: each order counts once per category it contains, never once per item.

### Dimensions

`type` accepts `categorical`, `time`, `number` and `boolean`. `expr` is optional; without it, the column with the same name is used. `sensitivity` accepts `personal` (personal data) and `sensitive` (sensitive personal data, such as health): these columns never reach an agent as raw values, only aggregated or masked.

Time dimensions accept a grain suffix in requests: `orders.ordered_at__day`, `__week` (ISO, starting on Monday), `__month`, `__quarter`, `__year` and `__hour`.

### Measures

`agg` accepts `count`, `count_distinct`, `sum`, `avg`, `min` and `max`. Measure names are unique in the project, and every measure can also be requested as a metric. `avg` is always a real average, even over integer columns.

### Metrics

| Type | Fields | Behavior |
| --- | --- | --- |
| `simple` | `measure`, optional `filter` | The measure, with an optional filter |
| `ratio` | `numerator`, `denominator` | Real division, safe against zero; each side aggregated at its own grain |
| `derived` | `expr` with `{measure}` or `{metric}` references, optional `inputs` | Expression over already aggregated values; division is real and safe against zero. `inputs` reads references at an earlier period ([period over period](#period-over-period)) |
| `cumulative` | `measure`, and `grain_to_date` or `window` | Running total (or running minimum or maximum) within each period (`grain_to_date`), or over a rolling window (`window`); requires exactly one time dimension in the request |

Because the planner aggregates each measure at its own grain before combining them, ratios and derived metrics can use measures from different models without row multiplication.

Every metric also takes the optional fields `description`, `label`, `owner` and `reconciliation` ([reconciliation tolerance](#reconciliation-tolerance)).

#### Period to date

`grain_to_date` accumulates the measure from the start of each `day`, `week`, `month`, `quarter` or `year` up to each row of the request:

```yaml
metrics:
  - name: revenue_year_to_date
    type: cumulative
    measure: revenue
    grain_to_date: year
  - name: max_order_value_year_to_date
    type: cumulative
    measure: max_order_value   # agg: max
    grain_to_date: year
```

- **Measures.** `sum` and `count` add up from the start of the period; `min` and `max` take the minimum or maximum so far (`MIN`/`MAX OVER`), so "largest order so far this year" is the largest order, never a sum of daily maxima. `avg` and `count_distinct` are refused: an average or a distinct count of the period cannot be rebuilt from per-day values (a distinct customer would count once per day). `cosmos validate` and `cosmos check` report such a metric as an error, and the planner refuses to query it. Without `grain_to_date` or `window`, a running total since the beginning of the data follows the same rules.
- **Grains.** Query it with the time dimension at a grain that nests in the period: a week-to-date by day or week, a month-to-date by day or month, a year-to-date by day, month, quarter or year (`__hour` and a time dimension without a grain work too). Two kinds of grain are refused with an error. A grain coarser than the period: a month-to-date by quarter or by year would only be the quarter's or the year's total. A week under a month-to-date, quarter-to-date or year-to-date: a week does not nest in those periods, so the week that crosses into a new one (Monday 29 December 2025 to Sunday 4 January 2026) would be counted in the old one, and a year-to-date for that week could exceed the whole year's total. The error names the grains that work.
- **Days in the output.** A row appears for each period (and combination of the other requested dimensions) that has facts after the request's filters; there is no time spine yet.
- **Other dimensions.** Each combination of the other requested dimensions accumulates on its own.

#### Rolling windows

A rolling window aggregates the measure over the last N days, today included:

```yaml
metrics:
  - name: revenue_rolling_7_days
    type: cumulative
    measure: revenue
    window: 7 days
```

- **Range.** The value on day D aggregates the facts dated from D − (N − 1) days to D, both included: `7 days` on 10 March covers 4 to 10 March. This is dbt MetricFlow's `window` convention (`metric_time <= D` and `metric_time > D - N days`).
- **Days in the output.** As with `grain_to_date`, a row appears for each day (and combination of the other requested dimensions) that has facts after the request's filters; there is no time spine yet. Days without facts are not output, but they still count as days of the window, so the window is a calendar range, never "the last N rows". On the first days of the data the window simply has fewer days with facts. MetricFlow, which joins a time spine, also outputs days without facts whose window has some.
- **Filters.** A request filter on the window's time dimension chooses the output days, and each day's window still reads the N − 1 days before it, so the first days of the range have full windows (DEC-34): "7-day rolling revenue for the last 2 weeks" sums 7 days on every day, as Power BI, Cube and MetricFlow do. Every other filter (status, region, a row filter, a metric's own `filter`) restricts the facts first, as before. In the SQL, the window reads its facts from a second daily aggregate (`<model>_agg_lookback_<N>_days`) where each lower bound on the time dimension is moved back: `{orders.ordered_at} >= X` reads `ordered_at + N days >= X` (N, not N − 1, because the column may hold a time of day), and `{orders.ordered_at__day} >= X` reads the day of `ordered_at + (N − 1) days`. Upper bounds need no later facts, but the last day's window reads all of that day: on the column itself an upper bound is read one day earlier (`ordered_at - 1 day <= Y`), so a bound inside a day (`<= ... 12:00`) keeps the day and its whole window; at day grain or coarser an upper bound stays as written.
  - **Shapes split.** Comparisons (`>=`, `>`, `<`, `<=`, `=`), `BETWEEN` and `IN` lists between the time dimension (the column, or it at day, week, month, quarter or year grain) and values without references, such as dates or `CURRENT_DATE - INTERVAL '45' DAY`, in either order, combined with `AND` and `OR`. Conditions on other members may sit next to them under `AND`, in the same filter.
  - **Shapes kept as before.** Any other filter on the time dimension still restricts the facts before the window and cuts the first windows of its range: `NOT`, `<>`, `NOT IN`, a function of the time dimension (`EXTRACT`), the `hour` grain, a comparison with another member, and `OR` mixing the time dimension with another member. The evidence says which applied: the metric's `time.filters_applied` is `time_after`, or `before` when such a filter is present.
  - **Not yet.** Running totals with `grain_to_date` and inputs read at an earlier period (offsets) still apply every filter to the facts first; letting them read back to the start of the period, or to the earlier period, are the next slices.
- **Other dimensions.** Each combination of the other requested dimensions has its own window; a null value is a group of its own. A null day (a fact with no date) is a row whose value is null.
- **Measures.** `sum` and `count` add up over the window; `min` and `max` take the minimum or maximum of the window. `avg` and `count_distinct` are refused: they cannot be combined from daily values, and recomputing them over each window is a later slice. The model itself is invalid (`cosmos validate` and `cosmos check` report it), not only a query of the metric.
- **Supported for now.** Only windows in days (`1 day`, `7 days`, `30 days`), queried with the time dimension at day grain (`orders.ordered_at__day`). Other units (`2 weeks`, `1 month`) and other grains are refused with an error.
- **SQL.** The daily aggregate is joined to itself on the date range, with no window frame, so the same SQL shape runs on every data source (SQL Server has no `RANGE` frame with an interval).

#### Period over period

A derived metric can read a reference at an earlier period: list it in `inputs` with an `offset`. The other references of `expr` are read at the same period.

```yaml
metrics:
  - name: revenue_prev_month
    type: derived
    expr: "{revenue}"
    inputs:
      - metric: revenue       # a measure or a metric referenced in expr
        offset: 1 month
  - name: revenue_mom_growth
    type: derived
    expr: "({revenue} - {revenue_prev_month}) / {revenue_prev_month}"
```

To read the same reference both shifted and unshifted in one expression, give the shifted input an `alias` and use the alias in `expr` (DEC-44); `{revenue}` is still read at the same period:

```yaml
metrics:
  - name: revenue_yoy_growth
    type: derived
    expr: "({revenue} - {revenue_year_before}) / {revenue_year_before}"
    inputs:
      - metric: revenue
        offset: 12 months
        alias: revenue_year_before   # how expr reads revenue 12 months earlier
```

The numbers are those of the same growth written over a second metric, as `revenue_mom_growth` above. Several inputs can read one metric at different offsets, each with its own alias (`{metric: revenue, offset: 1 month, alias: revenue_m1}`, `{metric: revenue, offset: 12 months, alias: revenue_m12}`). An alias is a name like a metric's (lowercase letters, digits and `_`, starting with a letter or `_`); it must be used in `expr`, name a single input, and not be the name of a measure or metric of the project, so `{alias}` always reads one thing. A metric listed twice in `inputs` needs an alias on its inputs. Without an alias an input is read by its metric's name, and every `{metric}` in `expr` is shifted, as before.

- **Value.** The value for a period is the input's value for the period that starts the offset earlier, at the query's time grain: by month, `revenue_prev_month` for March is the revenue of February, and for January the revenue of the December before. The input is aggregated at the query's grain first, then its period start is moved, so a month end never has to be clamped (31 March minus a month does not exist), and `1 month` means the previous calendar month whatever its length.
- **Grains.** The offset must be a whole number of periods of the request's time grain: an offset in months runs by month, by quarter in multiples of 3 months and by year in multiples of 12; an offset in days runs by day, and by ISO week in multiples of 7 days. Weeks are read as 7 days and years as 12 months (DEC-46): `2 weeks` runs where `14 days` does, `1 year` where `12 months` does, and gives the same SQL; the evidence (`time.shifts`, the formula) keeps the offset as written. Anything else is refused with an error: `1 month` by day (which day is "a month before" 31 March is where databases disagree), by week or by quarter, `7 days` by month, any offset by hour or on a time dimension without a grain.
- **Rows.** As with cumulative metrics, there is no time spine yet: a row appears for each period (and combination of the other requested dimensions) that has facts after the request's filters, for any of the requested metrics, the offset inputs included before they are shifted. A period whose earlier period has no facts gets NULL, even for a count, and so does a growth computed from it; a period without facts is not output, so the month after the last month of data does not appear. A row whose period is null (a fact without a date) gets NULL too.
- **Filters.** Request filters apply to the facts before the shift (rolling windows read back before a date filter since DEC-34; offsets do not yet): with a filter on the last 90 days, the first month of the range has no month before it. Each combination of the other requested dimensions is shifted on its own.
- **Requests.** A metric that reads an offset, directly (`revenue_prev_month`, `revenue_yoy_growth`) or through another metric (`revenue_mom_growth`), needs exactly one time dimension in the request.
- **Supported for now.** Offsets in days, weeks, months and years, singular or plural (`1 day`, `7 days`, `2 weeks`, `1 month`, `12 months`, `1 year`). Other units (`1 quarter`, `12 hours`), an offset of zero, an input not referenced in `expr` or listed twice, an offset of a cumulative metric, an offset over a metric that has offsets of its own, and a metric whose offsets, its own or those of the metrics it uses, mix days or weeks with months or years (no grain answers both; DEC-46) are errors in `cosmos validate` and `cosmos check`; the planner refuses them too. An expression reads the same reference both shifted and unshifted through an input's `alias` (DEC-44); an alias that is not used in `expr`, names two inputs or is the name of a measure or metric is an error too. The Cube export refuses these metrics, and Ossie keeps them only in the Cosmos extension; `cosmos import powerbi` reads `SAMEPERIODLASTYEAR`, `DATEADD` and `PARALLELPERIOD` by month as offsets ([where they differ from DAX](interoperability.md#importing-power-bi)). `cosmos export dbt` writes them as MetricFlow's `offset_window` and `cosmos import dbt` reads it back (DEC-36); MetricFlow (0.210 and later) shifts whole periods at a grain not coarser than the window's unit and each day of its time spine otherwise, so the export writes each offset in the largest unit that holds it, and it returns periods without facts and applies `metric_time` filters after the shift ([where they differ](interoperability.md#offsets)).
- **SQL.** One aggregate per model and offset, grouped like the others at the query's grain, joined to the spine of dimension values on its period moved forward by the offset (`DATEADD`, `+ INTERVAL`, `NUMTOYMINTERVAL` in Oracle), with plain equality on the period and null-safe equality on the other dimensions.

#### Reconciliation tolerance

`cosmos reconcile` and the reconciliation cases of `cosmos check` compare the governed value of each row with the legacy one. By default they agree only when they are equal up to float noise (an absolute difference of `1e-6` or a relative one of `1e-9`), so a legacy report rounded to cents or to thousands always differs. A metric can declare how close is close enough:

```yaml
metrics:
  - name: net_revenue
    type: derived
    expr: "{revenue} - {discounts}"
    reconciliation:
      abs_tolerance: 0.01     # the finance report rounds to cents
      rel_tolerance: 0.001    # or is within 0.1% of the governed value
```

- **Agreement.** A legacy value agrees when it is within `abs_tolerance` of the governed value, or within `rel_tolerance` times the larger of the two (`math.isclose`). A field left out keeps its default, so `abs_tolerance: 0.01` alone still allows the default relative difference of `1e-9`.
- **Validation.** Each tolerance is a non-negative, finite number; strings, booleans and unknown keys are refused, and `reconciliation` needs at least one of the two fields.
- **Where it applies.** The tolerance of the reconciled metric decides matched and differing rows in `cosmos reconcile` (against SQL, another model or a report's CSV), the pass or fail of each `cosmos check` case, and so the metric's health. The likely-cause probes compare within it too, so a cause is still found when the legacy report is also rounded. A measure reconciled on its own always uses the default.
- **What the output states.** The reconcile report has `tolerance` (`abs_tolerance`, `rel_tolerance` and `source`: `metric` or `default`), `rows.within_tolerance` (matched rows that are not equal), `tolerated` (up to five of them) and a `difference` (legacy minus governed) on each example. Each check run case records its `tolerance`, health's `reconciled` and `reconciliation_failed` reasons carry it when it is the metric's own, and the evidence of a query lists it under the metric as `reconciliation`.
- **Hashes.** A metric without `reconciliation` serializes as before, so the model hash, the generated SQL and the evidence of existing models do not change. Declaring a tolerance changes the model hash (as any change to the model does) but not the SQL.

### Row filters

`row_filters` use `{dimension}` or `{model.dimension}` for model members and `$user.<field>` for the user's token claims. All of a model's filters apply whenever the model participates in a query.

### User context

`user_context` declares each `$user` field and the token claim it comes from, because each identity provider uses different names. If a required claim is missing, the query is denied.

```yaml
user_context:
  region:
    claim: region        # claim name in the provider's token
    required: true       # without it, the query is denied
```

## Glossary (SKOS)

The glossary describes business concepts in the style of [SKOS](https://www.w3.org/TR/skos-reference/), the W3C standard for vocabularies. Each concept points to the model member or rule that defines it. Labels and synonyms can be in several languages, so agents find concepts from questions in Portuguese or English. See [ADR 0006](decisions/0006-ontology-in-the-mvp.md).

```yaml
glossary:
  - concept: customer
    pref_label: { pt: Cliente, en: Customer }
    alt_labels: { pt: [comprador, consumidor], en: [buyer] }
    definition: Registered person or company that can place orders
    maps_to: entity:customer

  - concept: active_customer
    pref_label: { pt: Cliente ativo, en: Active customer }
    alt_labels: { pt: [cliente recorrente, comprador ativo], en: [active buyer] }
    broader: customer
    definition: Customer with a purchase in the last 90 days
    maps_to: rule:active_customer

  - concept: net_revenue
    pref_label: { pt: Receita líquida, en: Net revenue }
    alt_labels: { pt: [faturamento líquido] }
    definition: Gross revenue minus discounts
    maps_to: metric:net_revenue
```

| Field | SKOS equivalent | Use |
| --- | --- | --- |
| `concept` | Concept identifier | Unique name in the project |
| `pref_label` | `skos:prefLabel` | Official name, per language |
| `alt_labels` | `skos:altLabel` | Synonyms the agent recognizes |
| `definition` | `skos:definition` | Natural-language definition |
| `broader` | `skos:broader` | "Is a kind of" hierarchy |
| `related` | `skos:related` | Link between concepts without hierarchy (optional) |
| `maps_to` | Own extension | Links the concept to `entity:`, `dimension:`, `metric:`, `measure:` or `rule:` |

## Business rules

Business rules are logic with an owner, a version and tests, reusable across models. Each rule becomes a virtual dimension on every model in `applies_to`.

```yaml
business_rules:
  - name: active_customer
    kind: predicate
    owner: sales-leadership
    effective_from: 2026-07-01
    change_note: Window widened from 60 to 90 days to match the quarterly sales cycle
    description: Customer with a purchase in the last 90 days
    inputs:
      last_purchase: time
    logic: "{last_purchase} >= CURRENT_DATE - INTERVAL '90' DAY"
    applies_to:
      - model: customers
        bind: { last_purchase: last_order_at }
    tests:
      - given: { last_purchase: 2026-10-01 }
        today: 2026-10-16
        expect: true
      - given: { last_purchase: 2026-05-01 }
        today: 2026-10-16
        expect: false
    history:
      - effective_from: 2026-01-01
        description: Customer with a purchase in the last 60 days
        logic: "{last_purchase} >= CURRENT_DATE - INTERVAL '60' DAY"
        tests:
          - given: { last_purchase: 2026-05-01 }
            today: 2026-06-15
            expect: true

  - name: order_size_band
    kind: classification
    owner: sales-leadership
    effective_from: 2026-01-01
    inputs:
      amount: number
    decision_table:
      - when: "{amount} >= 5000"
        then: Large
      - when: "{amount} >= 1000"
        then: Medium
      - otherwise: Small
    applies_to:
      - model: orders
        bind: { amount: amount }
```

| `kind` | Result | Becomes in the engine | Typical use |
| --- | --- | --- | --- |
| `predicate` | True or false | Boolean dimension and named filter | `filters: ["{rule.active_customer}"]` in a request or metric |
| `classification` | A category | Categorical dimension (`CASE WHEN`) | `dimensions: ["orders.order_size_band"]` |
| `derived` | A value | Numeric dimension | `filters: ["{orders.order_net_value} > 3000"]` |

| Field | Required | Description |
| --- | --- | --- |
| `name`, `kind` | Yes | Unique name and kind |
| `owner` | Recommended (validation warns) | Team or person responsible for the rule |
| `effective_from` | Recommended (validation warns); required with `history` | Since when the current version applies |
| `change_note` | No | What changed in this version |
| `inputs` | Yes | Named, typed inputs |
| `logic` or `decision_table` | Yes | An expression, or a decision table (only for `classification`) |
| `applies_to` | Yes | Models where the rule applies, with the mapping from inputs to columns or dimensions |
| `tests` | Recommended | Input examples and expected outputs, run in CI by `cosmos test` |
| `history` | No | Earlier versions, each with `effective_from`, its own `logic` or `decision_table`, `tests` and an optional `change_note`. Inputs and `applies_to` are shared by all versions |

### Rule versions

A query uses the version in effect on its `rules_as_of` date, which defaults to `as_of` (itself "today" in the model's time zone). So "active customers on 2026-06-30" counts with the 60-day rule that was in effect then, and `rules_as_of: 2026-10-23` recomputes that date with today's 90-day rule; the difference between the two is exactly the effect of the rule change. A date before the first version uses the first version. The evidence envelope names the version used, flags a non-current version as a caveat and reports the test status of that version; `cosmos explain rule:active_customer` lists the history. In the CLI: `cosmos query customer_count -w "{rule.active_customer}" --as-of 2026-06-30 --rules-as-of 2026-10-23`.

A rule inherits the LGPD classification of the columns it reads: a rule over sensitive data is also denied to agents.

**How a question goes through the ontology:** "how many active buyers do we have in the Midwest?" (or, in Portuguese, "quantos compradores ativos temos no Midwest?") → the synonym "active buyer" ("comprador ativo") leads to the concept `active_customer` → defined by the rule `active_customer` → which becomes a filter on `customers`. `CURRENT_DATE` is replaced by the business date, so the result is the same on every database:

```sql
WITH customers_agg AS (
  SELECT COUNT(DISTINCT customers.customer_id) AS customer_count
  FROM public.customers AS customers
  WHERE (customers.last_order_at) >= CAST('2026-10-22' AS DATE) + INTERVAL '-90 DAY'
    AND customers.region = 'Midwest'                       -- the user's row filter
)
SELECT customers_agg.customer_count AS customer_count FROM customers_agg
```

**Limits:** rules only use columns of the row (classifications over aggregates need a summary model); the glossary does no logical inference.

## Business context

Teams, systems and processes put the model in its business setting ([ADR 0009](decisions/0009-business-context-as-phase-3-differentiator.md)). Models and metrics take an optional `owner`, like rules.

```yaml
teams:
  - name: controllership
    description: Revenue recognition, discounts and collections
    contact: controllership@example.com

systems:
  - name: payment_gateway
    kind: api            # application, database, warehouse, pipeline, bi, api or other
    owner: controllership
    feeds: [payments]    # models whose data originates here

processes:
  - name: order_to_cash
    owner: controllership
    systems: [storefront, payment_gateway]
    uses: [metric:net_revenue, metric:collection_gap, rule:order_net_value]
```

| Element | Links | Used by |
| --- | --- | --- |
| `teams` | Named by `owner` anywhere; when teams are declared, an owner outside them is a warning | `explain team:<name>` lists what the team owns |
| `systems` | `feeds` models | Impact of `system:<name>` (an outage or a late feed); `source_systems` in `explain` |
| `processes` | `systems` and `uses` (`kind:name` members) | Impact reports the affected processes; `explain` lists the processes of a metric or rule |

Measures and dimensions inherit the owner of their model. `cosmos impact` groups the affected members by owner (`unowned` when nobody owns them) and lists the source systems, and `cosmos graph` exports everything as Mermaid, Graphviz DOT or JSON, optionally focused on one member (`--focus metric:net_revenue`): what it depends on, what depends on it and who owns each part.

## Structured filters

A request's `filters` (in `POST /v1/query`, `POST /v1/sql`, `POST /v1/dimensions/{name}/values` and the MCP tools `query`, `show_sql` and `dimension_values`) take expression strings with `{model.dimension}` references, or objects with a dimension, an operator and a value (DEC-50). Both kinds can be mixed; every filter is ANDed with the others and with the user's row filters.

```json
{
  "metrics": ["revenue"],
  "dimensions": ["customers.state"],
  "filters": [
    {"dimension": "orders.status", "op": "in", "value": ["completed", "pending"]},
    {"dimension": "customers.segment", "op": "!=", "value": "O'Brien"},
    {"dimension": "orders.ordered_at", "op": ">=", "value": "2026-01-01"},
    "{rule.active_customer}"
  ]
}
```

- **Operators.** `=`, `!=` (or `<>`), `<`, `<=`, `>`, `>=` with one value; `in` and `not in` with a non-empty list of at most 1,000 values; `is null` and `is not null` with no value. Case and extra spaces in the operator do not matter.
- **Values.** Strings, numbers and booleans, sent as JSON values. The server checks them against the dimension's type: numbers for a number dimension, `true` or `false` for a boolean one, ISO dates or date-times as strings for a time dimension, strings or numbers for a categorical one. A dimension may carry a time grain (`orders.ordered_at__month`) or be a business rule (`rule.active_customer`).
- **Literals.** The value never goes through a parser: SQLGlot writes it as a literal of the data source's dialect, with that dialect's quoting and escaping. A client therefore never quotes or escapes a value, and a value such as `O'Brien` or `a\'b` stays that value on MySQL, MariaDB, Databricks and BigQuery, which read a backslash in a string as an escape.
- **Same SQL.** A structured filter and the equivalent expression string (written with the dialect's own quoting) produce the same SQL, and so the same `sql_hash`. The audit log keeps the object as sent, so a replay gets the same SQL; the console's answers and the evidence show it as the equivalent expression.
- **Errors.** An unknown operator, a value of the wrong shape (a list for `=`, a value for `is null`) or an unknown field is refused before planning (HTTP 422, or an MCP tool error); an unknown dimension or a value that does not fit the dimension's type is a `QueryError` (HTTP 400). Personal data stays refused to agents, as with expression strings.

Reference case A55 checks the numbers of a request with structured filters, among them a `not in` list holding a quote and a backslash before a quote, against hand-written SQL on every database.

## Filters on metric values

A request's `filters` restrict the facts before aggregation. Its `metric_filters` keep only the rows of the result whose metric values meet conditions, after aggregation, at the requested grain: "the states with more than 450,000 in revenue", "the segments whose average order value is at least 2,600 with more than 5 orders".

```json
{
  "metrics": ["revenue", "order_count"],
  "dimensions": ["customers.state"],
  "metric_filters": ["{revenue} > 450000", "{order_count} >= 100"],
  "order_by": ["-revenue"],
  "limit": 3
}
```

In the CLI: `cosmos query revenue order_count --by customers.state --metric-filter "{revenue} > 450000" --metric-filter "{order_count} >= 100"`; in MCP, the `metric_filters` argument of `query`.

- **Grammar.** A condition compares one requested metric (or measure), written `{name}`, with a number: `=`, `<>`, `<`, `<=`, `>`, `>=`; or tests it with `IS NULL` or `IS NOT NULL`. Conditions are joined by `AND`, inside one filter or across the list. Numbers are plain decimals (`1000`, `-2.5`, `0.25`), with no exponent and at most 18 digits on each side of the point. Anything else is refused with an error: `OR`, `NOT`, parentheses, functions, strings, columns, dimensions, `$user`, comments, other metrics as the right-hand side, or a metric that is not in `metrics`. The text never reaches the SQL: the engine parses it and writes the comparison from the metric name, the operator and the digits. At most 20 filters of 500 characters each.
- **Order.** The row filters and the request's `filters` apply to the facts first, as always; then the metrics are aggregated (and ratios and derived metrics computed) at the requested grain; then `metric_filters` remove rows; `order_by` and `limit` apply to the rows kept, so `limit: 3` returns three rows that meet the conditions.
- **Nulls.** A comparison with a NULL value is not true, so a row whose metric is NULL (a ratio over zero orders, a metric from another model with no facts for that row) is never kept by `>` or `<>`; ask for `IS NULL` to see those rows.
- **Totals.** Only rows are removed; the values of the rows kept are the same as without the filter. A sum of the rows returned is therefore not the metric's total, and the evidence says so in `caveats`. It lists the conditions applied in `metric_filters`, next to `policies.row_filters`.
- **Metrics.** Simple, ratio and derived metrics and measures can be compared. Requests with cumulative metrics (running totals and rolling windows) or with metrics that read an input at an earlier period are refused when they carry `metric_filters`, in this first slice: removing rows from those would raise the question of whether the window or the earlier period still sees them.
- **SQL.** The result is computed as before into a CTE, `cosmos_result`, and an outer `SELECT ... FROM cosmos_result WHERE ...` keeps the rows, then orders and limits them. SQL Server accepts `WITH` only at the top of a statement, so the result is a CTE, not a subquery. A request without `metric_filters` gets exactly the SQL it got before.

## The SQL the engine generates

The examples below are Postgres output (lightly abridged). The same plan generates SQL for other dialects through SQLGlot; `cosmos sql` shows it for any request.

**Simple request:** revenue and average order value by customer region, by month.

```sql
WITH orders_agg AS (
  SELECT
    DATE_TRUNC('MONTH', orders.ordered_at) AS orders__ordered_at__month,
    customers.region AS customers__region,
    SUM(orders.amount) AS revenue,
    COUNT(*) AS order_count
  FROM public.orders AS orders
  LEFT JOIN public.customers AS customers ON orders.customer_id = customers.customer_id
  WHERE customers.region = 'Midwest'                       -- the user's row filter
  GROUP BY DATE_TRUNC('MONTH', orders.ordered_at), customers.region
)
SELECT
  orders_agg.orders__ordered_at__month,
  orders_agg.customers__region,
  orders_agg.revenue,
  CAST(orders_agg.revenue AS DOUBLE PRECISION) / NULLIF(orders_agg.order_count, 0) AS average_order_value
FROM orders_agg
```

**Measures from different models:** revenue (from `orders`) and paid value (from `payments`) by region. Joining and summing directly would multiply rows, so each measure is aggregated at its own grain, and the results are joined through the "spine" of dimension values with null-safe equality.

```sql
WITH orders_agg AS (
  SELECT customers.region AS customers__region, SUM(orders.amount) AS revenue
  FROM public.orders AS orders
  LEFT JOIN public.customers AS customers ON orders.customer_id = customers.customer_id
  WHERE customers.region = 'Midwest'
  GROUP BY customers.region
), payments_agg AS (
  SELECT customers.region AS customers__region, SUM(payments.value) AS paid_value
  FROM public.payments AS payments
  LEFT JOIN public.orders AS orders ON payments.order_id = orders.order_id
  LEFT JOIN public.customers AS customers ON orders.customer_id = customers.customer_id
  WHERE customers.region = 'Midwest'
  GROUP BY customers.region
), dimensions AS (
  SELECT customers__region FROM orders_agg
  UNION
  SELECT customers__region FROM payments_agg
)
SELECT
  dimensions.customers__region,
  orders_agg.revenue,
  payments_agg.paid_value
FROM dimensions
LEFT JOIN orders_agg ON dimensions.customers__region IS NOT DISTINCT FROM orders_agg.customers__region
LEFT JOIN payments_agg ON dimensions.customers__region IS NOT DISTINCT FROM payments_agg.customers__region
```

## Exporting to Cube (phase 2)

Whoever needs caching, pre-aggregations or BI connectivity will be able to export the model to Cube. The example above produces, among others, this cube:

```yaml
cubes:
  - name: orders
    sql_table: public.orders
    joins:
      - name: customers
        relationship: many_to_one
        sql: "{CUBE}.customer_id = {customers}.customer_id"
    dimensions:
      - name: order
        sql: order_id
        type: number
        primary_key: true
      - name: ordered_at
        sql: ordered_at
        type: time
      - name: status
        sql: status
        type: string
    measures:
      - name: revenue
        type: sum
        sql: amount
      - name: completed_revenue
        type: sum
        sql: amount
        filters:
          - sql: "{CUBE}.status = 'completed'"
      - name: average_order_value
        type: number
        sql: "1.0 * {revenue} / NULLIF({order_count}, 0)"
      - name: revenue_month_to_date
        type: sum
        sql: amount
        rolling_window:
          type: to_date
          granularity: month
```

Whatever Cube does not represent the same way produces a warning on export:

- ratio or derived metrics combining different models, because in Cube a measure belongs to one cube;
- `row_filters` and `sensitivity`, which in Cube are configuration (`query_rewrite`), not model.

## Validations

- Duplicate names within a model, and duplicate measures or metrics in the project.
- A `primary` or `unique` entity repeated in more than one model.
- An ambiguous join path between two models.
- A metric referencing a missing measure or dimension, or missing the fields of its type.
- A derived metric's `inputs` with an unsupported offset, an input not referenced in `expr` or listed twice without an `alias`, an alias not used in `expr`, given to two inputs or equal to the name of a measure or metric, an offset of a cumulative metric or of a metric with offsets of its own, or a metric whose offsets mix days or weeks with months or years.
- An unknown `timezone`; a `health.max_age` that is not a whole number of hours, days or weeks; model files with different `timezone` or `health` settings.
- A concept whose `maps_to` points to a missing member or rule; a cycle in the `broader` hierarchy.
- The same synonym in two different concepts: a warning, because the agent could not tell which to pick.
- A rule with an input not mapped in some `applies_to` model, or mapped to a column of an incompatible type.
- A decision table without `otherwise`.
- A rule test whose result differs from the expected one (`cosmos test`).

## Not in the MVP

- Conversion (funnel) metrics.
- A time spine for series without gaps on days without data.
- Rolling windows in units other than days, or at grains other than the day.
