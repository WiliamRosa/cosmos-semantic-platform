# Interoperability: Ossie, dbt and Cube

[Apache Ossie](https://github.com/open-semantic-interchange/OSI), formerly Open Semantic Interchange (OSI), is the open standard for exchanging semantic models between tools. It was launched in September 2025 by Snowflake, Salesforce and dbt Labs, is incubating at the Apache Software Foundation under the Apache 2.0 license, and has participants such as Cube, AtScale, Collibra and Starburst. See [ADR 0003](decisions/0003-ossie-as-interchange-format.md).

## State of the standard

| Item | Status in October 2026 |
| --- | --- |
| Development version | `0.2.0.dev0`, a draft; the schema may still change |
| Latest published version | `0.1.1` |
| Format | JSON and YAML, with the schema in `core-spec/` |
| Reference converters | dbt, GoodData, Polaris, Salesforce |
| Extensions | `custom_extensions` at every level, with `vendor_name` and JSON `data` |

Source: [Ossie specification](https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md).

## Mapping to the authoring format

| Authoring format | Ossie | Note |
| --- | --- | --- |
| Project | Semantic model (root) | `version`, `name`, `datasets`, `relationships`, `metrics` |
| `semantic_models[]` | `datasets[]` | `table` maps to `source` |
| `primary` entity | The dataset's `primary_key` | |
| `unique` entity | The dataset's `unique_keys` | |
| `foreign` entity | Explicit `relationships[]`, with `from`, `to`, `from_columns` and `to_columns` | On export, inferred joins become explicit relationships; on import, relationships become entities |
| `dimensions[]` | `fields[]` with `dimension` (and `is_time` for time) | |
| `measures[]` | `metrics[]` with an aggregation expression | Ossie does not separate measures from metrics; the separation lives in an extension |
| `metrics[]` | `metrics[]` | The metric type (ratio, cumulative) is kept in `custom_extensions` |
| `description` | `description` | |
| `row_filters` and `sensitivity` | `custom_extensions` | Access and LGPD policies have no native Ossie field |
| Glossary: `alt_labels` and `definition` | `ai_context` (`synonyms`, `instructions`) of the member linked by `maps_to` | A native Ossie field |
| Glossary: concepts, hierarchy and per-language labels | `custom_extensions` at the model root | Ossie has no glossary of its own |
| Business rules | `fields[]` with the compiled expression in each `applies_to` dataset | Owner, effective date, decision table and tests are kept in `custom_extensions` |

## Rules

1. **Expressions:** Ossie stores expressions per dialect (`expression.dialects`). Only the `OSSIE_SQL_2026` dialect has semantics defined by the standard; the others are opaque text. On export, the product writes `OSSIE_SQL_2026` and, when useful, the source's dialect. On import, it prefers `OSSIE_SQL_2026`, then `ANSI_SQL`.
2. **Extensions:** whatever Ossie does not represent (metric type, measure/metric separation, cumulative window) goes into `custom_extensions` under the product's `vendor_name`. A model exported and re-imported must not lose information.
3. **Cross-dataset metrics:** Ossie allows metrics that span datasets, and so does our engine, because it aggregates each measure at its own grain. On import, a compound expression becomes one measure per aggregation plus a derived metric; only export to Cube warns about them.
4. **Round trip:** exporting and re-importing the same model must produce exactly the same authoring model. This is an automated test.

## Import in phase 1

Phase 1 imports three formats and converts everything to the Cosmos format ([ADR 0007](decisions/0007-connectors-sources-identity-and-import.md)), with `cosmos import dbt|ossie|cube <path> -o <file.yml>`. No conversion loses information silently: whatever has no equivalent goes into `custom_extensions` or becomes a warning in the import report.

| Format | What comes in | Limit |
| --- | --- | --- |
| dbt | `semantic_models` and `metrics`, in the latest and legacy specs | Reads the YAML without running the dbt project; `Dimension('entity__dim')` filters become `{model.dim}`; `percentile`, `offset_window` and conversion metrics become warnings |
| Ossie | The whole document, including `custom_extensions`; `ai_context.synonyms` becomes glossary entries | Only the `OSSIE_SQL_2026` dialect has guaranteed semantics; composite keys become warnings |
| Cube | Static YAML: cubes, dimensions, measures, joins and segments | JavaScript models and YAML with Jinja or Python templates produce a warning and are not imported |

Every importer is tested for equivalence: the imported model must return the same numbers as the native example model, on DuckDB, Postgres and SQL Server.

## Importing Cube

Cube is the most adopted open source semantic layer, so importing from it is the shortest migration path for new users. Together with export, it closes the round trip with Cube.

| In Cube | In Cosmos | Conversion |
| --- | --- | --- |
| `cubes` with `sql_table` or `sql` | Semantic model | Direct |
| Dimension with `primary_key: true` | Primary entity | Direct |
| `joins` with simple equality (`{CUBE}.customer_id = {customers}.id`) | Foreign entity | Direct; complex conditions produce a warning |
| Measures `count`, `count_distinct`, `sum`, `avg`, `min`, `max` | Measure of the same type | Direct |
| `number` measure referencing other measures | Derived metric | Requires reading the SQL expression |
| `segments` | Business rule | Comes in with owner and effective date pending; validation warns until they are set |
| `rolling_window` and multi-stage measures | Cumulative metric (rolling windows are not in the MVP) | Warning and `custom_extensions` |
| `pre_aggregations` and `views` | No MVP equivalent | Kept in `custom_extensions` |
| Access control in `cube.js` | `row_filters` and `sensitivity` | Manual: it is JavaScript code; the report lists the rules to recreate |

Importing a Cube model and exporting it back must produce the same static YAML, except for what the report recorded as warnings; this becomes an automated test, as with Ossie.

## Importing Power BI

`cosmos import powerbi <model.bim | TMDL folder> -o semantic/powerbi.yml` reads a Power BI semantic model without running it ([ADR 0009](decisions/0009-business-context-as-phase-3-differentiator.md)). A `.bim` file (TMSL JSON) and a TMDL folder (the `definition/` of a PBIP project) give the same result.

| Power BI | Cosmos |
| --- | --- |
| Table | Semantic model; the physical table comes from the partition: Power Query navigation (`Schema`/`Item`, or `Name`/`Kind` as in Databricks and Snowflake), `Value.NativeQuery` or a legacy query partition with a `SELECT * FROM` |
| Column | Dimension (`sourceColumn` becomes `expr`); hidden columns, usually keys, only when a filter needs them |
| Active relationship | Foreign entity on the many side, primary entity on the one side |
| `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `DISTINCTCOUNT`, `DISTINCTCOUNTNOBLANK`, `COUNTROWS` | Measure |
| `SUMX(T, ...)` over the measure's own table, with `+`, `-` and `*` over its numeric columns and numbers | `sum` measure with that row expression; `+` and `-` keep the DAX rule that one BLANK side counts as zero (`BLANK * x` is BLANK, as NULL is in SQL) |
| `DIVIDE(a, b)` | Ratio metric |
| `CALCULATE(m, T[c] = "x", T[d] IN {...}, KEEPFILTERS(...))` | Metric with `filter` |
| `TOTALYTD`, `TOTALQTD`, `TOTALMTD` | Cumulative metric |
| Arithmetic over measures | Derived metric |
| Hierarchies | `custom_extensions` |

Reported as warnings, never dropped silently: other iterators (`AVERAGEX` and similar) and `SUMX` over another table or with a division, a function or `RELATED` (DAX division returns infinity or BLANK where SQL fails, returns NULL or truncates integers), `FILTER`, `ALL`, variables, `LASTDATE` and other functions with no equivalent (with the measures that depend on them), calculated columns and tables, calculation groups, inactive (`USERELATIONSHIP`), bidirectional and many-to-many relationships, and row-level security roles, which must be recreated as `row_filters` with `$user` fields. Two semantic differences are flagged per measure: a plain `CALCULATE` filter replaces the filter on that column in DAX, while Cosmos intersects it, as `KEEPFILTERS` does (numbers differ only when a query also groups or filters by that column); and `DISTINCTCOUNT` counts BLANK as a value in DAX, while `count_distinct` ignores nulls (use `DISTINCTCOUNTNOBLANK` in Power BI to match).

An equivalence test imports a Power BI version of the e-commerce example, in both formats, and checks that it returns the same numbers as the native model on DuckDB, Postgres and SQL Server. Imported `SUMX` measures are also checked against hand-written SQL on data with NULLs in every numeric column.

## Exporting Ossie

`cosmos export ossie -m semantic -o model.ossie.yaml` writes the model as an Ossie `0.2.0.dev0` document, following the mapping above (`--format json` for JSON, `--name` for the model name; without `-o`, standard output carries only the document). It covers the whole authoring model: models, entities, dimensions, measures, the four metric types, row filters, sensitivity, the glossary, business rules (with every earlier version) and the business context (teams, systems, processes):

| Authoring format | In the document |
| --- | --- |
| Models, dimensions | `datasets` with `source`; `fields` with `dimension` (`is_time` for time dimensions) |
| Entities | `primary_key`, `unique_keys`, a field per key expression, and one explicit relationship per join the engine infers |
| Measures, and metrics over one dataset | `metrics` on that dataset in `OSSIE_SQL_2026`, written as the planner computes them: a metric filter becomes `CASE WHEN` inside the aggregation, `AVG(CAST(x AS DOUBLE))`, and division is `CAST(a AS DOUBLE) / NULLIF(b, 0)` |
| Row filters, with the `user_context` fields they read | A `COSMOS_SEMANTIC` extension on the dataset (`row_filters`, `user_context`), as in the mapping above: Ossie has no native field for access policies |
| `sensitivity` | A `COSMOS_SEMANTIC` extension on the field (`{"sensitivity": "personal"}`) |
| Glossary concepts | `ai_context` of the member each concept maps to (a dimension's field, an entity's key field, or a metric on its dataset): every `pref_label` and `alt_labels` entry in `synonyms`, the `definition` in `instructions`. Concepts, hierarchy and per-language labels are in the root extension |
| Business rules (`predicate`, `classification`, `derived`), with owner, `effective_from`, `change_note`, tests and every earlier version in `history`; teams, systems and processes | The root `COSMOS_SEMANTIC` extension: Ossie has no native field for them. The rules a dataset's row filters read are also copied into that dataset's extension (`business_rules`) |
| Everything Ossie does not represent | A `COSMOS_SEMANTIC` extension at the root holding the exported part of the project, which `cosmos import ossie` restores exactly (Rule 4) |

Some metrics have no native expression and live only in the extension, with a warning that states the reason and that other tools will not see them:

- metrics that span datasets: Ossie does not require a reader to aggregate each dataset at its own grain, so a native expression could fan out (a row-level join of orders and payments inflates order revenue);
- cumulative metrics;
- metrics whose filter uses another dataset, a time grain, a business rule (only Cosmos evaluates rules, version by version) or `$user`, or whose derived expression uses bare columns.

**Policies round trip.** Row filters, the user context fields that row filters and metrics read, and `sensitivity` are in the root extension, so `cosmos import ossie` restores the same filters, the same claims and the same personal-data marking; the e-commerce example exports without any flag. They are also written next to their dataset and field, so other tools see them where the data is, and `cosmos import ossie` reads them from there when a tool has dropped the root extension. The root extension is authoritative: when a tool strips it, the import falls back to these copies and warns that they must be verified and that what lives only in the root extension (some metrics, the user context only they read, glossary concepts, business rules no row filter reads, teams, systems and processes) is missing; a `user_context` field or a business rule the copies declare in conflicting ways is refused. A tool that also strips the field copies loses the sensitivity marking.

**The glossary is visible to agents.** Glossary labels and definitions go into `ai_context`, which agents and other tools read, so they must not contain personal data.

**Business rules and business context round trip.** Rules keep every field: kind, owner, inputs, `logic` or `decision_table`, `applies_to`, tests, `effective_from`, `change_note` and each earlier version in `history`, so a query replayed as of a past date uses the same rule version, and gives the same numbers, after the round trip. Teams, systems and processes come back exactly too, and so do glossary concepts that map to a rule. A row filter that reads a rule (`{rule.active_customer}`) is exported with it, so no model needs `--allow-policy-loss` any more: `cosmos export ossie` still accepts the flag, for compatibility, and it has no effect. The rules a dataset's row filters read, together with the rules those rules bind an input to (in the order of `business_rules`), are also copied next to that dataset with the business `timezone`, so a Cosmos import of a document whose root extension a tool dropped still has the row-level security, and "today" (which decides the rule version and window when a query has no `as_of`) is the same; copies that declare a rule or the time zone in conflicting ways are refused.

**The business context leaves with the file.** The export now carries teams, systems and processes (including their `contact` and `owner` fields) and the values in rule tests, so any personal names or e-mail addresses in them leave with the document.

**What is not exported.** User context fields that no row filter or metric reads. Each becomes a warning on standard error, and the extension records the count (`omitted`, never values); `cosmos import ossie` turns that record back into warnings, so whoever imports the file later learns what is missing. Documents written by earlier slices may record omitted row filters or other members; the import still reports them.

Tests export the e-commerce example and re-import it. The authoring model is the same, member by member. The row filters, user context, sensitivity, business rules with every version and their tests, teams, systems and processes are compared after the round trip, field by field. The re-imported model passes all 48 reference cases, business rule and glossary ones included, with the user's row filter and the sensitivity policy applied (R17 and R18 among them), with the numbers of the hand-written SQL. A row filter that reads a rule filters the same rows after the round trip, and rule queries replayed as of a date before a rule change (with `as_of`, and with `rules_as_of` on either side of the change) give the same numbers on the original and the re-imported model. Without the root extension, a row filter over a rule bound to another rule filters the same rows, and a query without `as_of` on a frozen clock (evening in New York, the next day in UTC) uses the same rule version as the original. A Cosmos import of the native part alone, without the root extension, returns the same numbers for every metric it expresses, and still restores the policies, so R17 and R18 pass on it too. This says nothing about other Ossie readers, whose semantics the standard does not yet pin down. Other tests pin the exact native expressions. The number checks run on DuckDB and on every database configured in CI.

## Exporting Cube

`cosmos export cube -m semantic -o model/cubes/model.yml` writes the model as Cube static YAML (without `-o`, standard output carries only the YAML). `cosmos import cube` reads it back with the same numbers. This first slice covers models, joins, dimensions, measures and the simple, ratio and derived metrics:

| Authoring format | In Cube |
| --- | --- |
| Model | A cube with `sql_table`, `description`, `data_source`, and the owner in `meta` |
| `primary` entity | The dimension with the key's expression marked `primary_key`, or a hidden one (`public: false`) added for it |
| `foreign` entity | A `many_to_one` join on the many side, one per join the engine infers: `{CUBE}.customer_id = {customers}.customer_id` |
| Dimensions | Dimensions: `categorical` becomes `string`; `time`, `number` and `boolean` keep their type |
| Measures | Measures of the same type; `avg` is written as `AVG(1e0 * x)`, so Cube averages real numbers, as the planner does (an integer AVG truncates in SQL Server) |
| Simple metric | A measure of its measure's type, with the metric `filter` in `filters`; a dimension reference becomes its expression on the cube |
| Ratio and derived metrics | `number` measures over the measures they use; division is written `1e0 * a / NULLIF(b, 0)`, real and null on a zero denominator |
| Metric `label`, `description` and owner | `title`, `description` and `meta` |

The `1e0 *` factor is a floating-point literal in DuckDB, SQL Server, MySQL, MariaDB, Databricks and Snowflake, and a high-precision numeric in Postgres and Oracle, so it avoids integer division and MySQL's four extra digits for decimal division. The planner writes `CAST(x AS DOUBLE)` instead: the results agree to floating-point precision, but the SQL is not the same.

Column names in expressions get a `{CUBE}.` prefix, so Cube does not confuse columns of joined cubes; the rest of the expression is written as in the model, and `cosmos import cube` removes the prefix. A ratio or derived metric goes on the cube of its first measure, and refers to members of other cubes as `{payments.paid_value}`.

**Metrics that span cubes are exported with a warning** (Rule 3). `collection_gap` and `items_per_order` in the example combine measures of two cubes; the engine aggregates each measure at its own grain, and Cube must do the same to return the same numbers, so check them in Cube.

**What cannot be exported fails closed.** Cube's access policies are not mapped yet, so a model with row filters or `sensitivity` is refused, exit code 1, naming them: `1 row filter (customers.by_region) and the sensitivity of 3 dimensions (...) would NOT be exported: the Cube export does not carry access or personal-data policies yet; ... Pass --allow-policy-loss to export anyway`. Metrics Cube cannot carry here are refused the same way, until `--allow-metric-loss` is given: cumulative metrics; simple metrics whose filter uses another cube, a time grain, a business rule or `$user`; ratio metrics with a filter (a `number` measure cannot filter the measures it uses); derived metrics over bare columns; and metrics that use a metric left out. With the flags, each loss is a warning on standard error. Business rules, the glossary, teams, systems, processes, the user context, `custom_extensions` and the time zone are not exported yet, and each one present is a warning too. Cube's `meta`, `title` and `data_source` are written, but `cosmos import cube` does not read them back yet.

**The time zone.** In Cosmos, `timezone` only defines "today" (`CURRENT_DATE` and relative dates); model timestamps are already local time and are never converted. Cube's query `timezone` converts timestamps from UTC, so setting it to the model's zone would shift day, week and month buckets. Keep Cube's query time zone at UTC: buckets then match, but relative dates such as "today" follow UTC in Cube, not the model's zone. The export warns about this gap.

**Known limits.**

- A `boolean` dimension whose expression is a predicate (`discount > 0`) is written as is; SQL Server and Oracle do not accept a bare predicate as a selected value, so Cube may fail there.
- `sql_table` is the model's logical table name: a data source's `schema_map` (for example `public` to `cosmos` in SQL Server) is not applied, so map the schema in Cube.
- The `{CUBE}.` prefix finds columns with SQLGlot's generic tokenizer: a quoted column name, or one that is also a keyword in the target database, may not be prefixed correctly; check those expressions.
- Expressions are written as in the model, not translated to the target database's dialect.

Tests export the e-commerce example with both flags and import it back with `cosmos import cube`, which reports nothing. They check the numbers through the Cosmos importer and planner, not Cube's own runtime: they show that the export carries the model without loss, not how Cube executes it. Models, tables, dimensions and measures come back the same; filtered metrics come back as a `<name>__base` measure plus a simple metric, ratio metrics as derived ones. The re-imported model answers the 25 reference queries that need no business rule, cumulative metric or user policy with the numbers of the hand-written SQL; with the original row filters, user context and sensitivity put back, it answers all 40 that need no business rule or cumulative metric, R17 and R18 among them. Every measure and metric of an extended example (`max` and `min` measures, filtered `count`, `count_distinct`, `avg` and `max`, a filter on a dimension with an expression, nested division) returns the native numbers. The number checks run on DuckDB and on every database configured in CI.

## Exporting the glossary as SKOS/RDF

`cosmos export skos -o glossary.ttl` (or `GET /v1/glossary?format=turtle|jsonld|ntriples` on the REST API) publishes the business glossary for data catalogs and thesaurus tools that read [SKOS](https://www.w3.org/TR/skos-reference/):

| Glossary | SKOS/RDF |
| --- | --- |
| The glossary | `skos:ConceptScheme` with `dct:title` and `dct:modified`; concepts without `broader` are its top concepts |
| `concept` | `skos:Concept`, IRI `<base>concept/<name>`, `skos:notation` = the name |
| `pref_label`, `alt_labels` | `skos:prefLabel`, `skos:altLabel`, one language tag per label (`en`, `pt`) |
| `definition` | `skos:definition` (tagged `en` by default, `--definition-lang`) |
| `broader`, `related` | `skos:broader` and `skos:narrower`, `skos:related` both ways, so tools without inference see every link |
| `maps_to` | `rdfs:isDefinedBy` to a resource for the governed member (`<base>metric/…`, `rule/…`, `dimension/…`) with its description, owner and, for rules, `dct:valid` = the effective date; plus a `skos:scopeNote` saying the same in words |

Formats: Turtle, JSON-LD (expanded, valid without a context) and N-Triples, written without extra dependencies. `--base` sets the IRI namespace (default `urn:cosmos-semantic:glossary:`); use a URL the organization controls when the catalog will link back. The tests parse every format with rdflib, check that the three give the same graph, and check the SKOS integrity rules (one `prefLabel` per language, `prefLabel` and `altLabel` disjoint, `broader`/`narrower` and `related` symmetric).

## Risks

- The standard is still a draft. The compiler isolates the mapping in its own layer, with tests per schema version.
- The Ossie repository already has a reference converter for dbt; assess whether it can replace part of the product's dbt exporter.
- dbt reworked its YAML format in 2026 and keeps the previous one as legacy. The dbt importer is isolated and tested per format version.
