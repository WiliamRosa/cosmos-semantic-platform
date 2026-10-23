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
timezone: America/Sao_Paulo

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
| `derived` | `expr` with `{measure}` or `{metric}` references | Expression over already aggregated values; division is real and safe against zero |
| `cumulative` | `measure` and `grain_to_date` (`window` is not in the MVP) | Running total within each period; requires exactly one time dimension in the request |

Because the planner aggregates each measure at its own grain before combining them, ratios and derived metrics can use measures from different models without row multiplication.

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
    effective_from: 2026-01-01
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

  - name: order_size_band
    kind: classification
    owner: sales-leadership
    effective_from: 2026-01-01
    inputs:
      amount: number
    decision_table:
      - when: "{amount} >= 5000"
        then: Grande
      - when: "{amount} >= 1000"
        then: Médio
      - otherwise: Pequeno
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
| `effective_from` | Recommended (validation warns) | Since when the current version applies |
| `inputs` | Yes | Named, typed inputs |
| `logic` or `decision_table` | Yes | An expression, or a decision table (only for `classification`) |
| `applies_to` | Yes | Models where the rule applies, with the mapping from inputs to columns or dimensions |
| `tests` | Recommended | Input examples and expected outputs, run in CI by `cosmos test` |

A rule inherits the LGPD classification of the columns it reads: a rule over sensitive data is also denied to agents.

**How a question goes through the ontology:** "quantos compradores ativos temos no Sul?" (how many active buyers in the South?) → the synonym "comprador ativo" leads to the concept `active_customer` → defined by the rule `active_customer` → which becomes a filter on `customers`. `CURRENT_DATE` is replaced by the business date, so the result is the same on every database:

```sql
WITH customers_agg AS (
  SELECT COUNT(DISTINCT customers.customer_id) AS customer_count
  FROM public.customers AS customers
  WHERE (customers.last_order_at) >= CAST('2026-10-22' AS DATE) + INTERVAL '-90 DAY'
    AND customers.region = 'Sul'                       -- the user's row filter
)
SELECT customers_agg.customer_count AS customer_count FROM customers_agg
```

**Limits:** rules only use columns of the row (classifications over aggregates need a summary model); queries always use the current version of a rule; the glossary does no logical inference.

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
  WHERE customers.region = 'Sul'                       -- the user's row filter
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
  WHERE customers.region = 'Sul'
  GROUP BY customers.region
), payments_agg AS (
  SELECT customers.region AS customers__region, SUM(payments.value) AS paid_value
  FROM public.payments AS payments
  LEFT JOIN public.orders AS orders ON payments.order_id = orders.order_id
  LEFT JOIN public.customers AS customers ON orders.customer_id = customers.customer_id
  WHERE customers.region = 'Sul'
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
- An unknown `timezone`.
- A concept whose `maps_to` points to a missing member or rule; a cycle in the `broader` hierarchy.
- The same synonym in two different concepts: a warning, because the agent could not tell which to pick.
- A rule with an input not mapped in some `applies_to` model, or mapped to a column of an incompatible type.
- A decision table without `otherwise`.
- A rule test whose result differs from the expected one (`cosmos test`).

## Not in the MVP

- Conversion (funnel) metrics.
- A time spine for series without gaps on days without data.
- Rolling-window cumulative metrics.
