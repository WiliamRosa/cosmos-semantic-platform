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
| `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `DISTINCTCOUNT`, `COUNTROWS` | Measure |
| `DIVIDE(a, b)` | Ratio metric |
| `CALCULATE(m, T[c] = "x", T[d] IN {...}, KEEPFILTERS(...))` | Metric with `filter` |
| `TOTALYTD`, `TOTALQTD`, `TOTALMTD` | Cumulative metric |
| Arithmetic over measures | Derived metric |
| Hierarchies | `custom_extensions` |

Reported as warnings, never dropped silently: iterators (`SUMX` and similar), `FILTER`, `ALL`, variables, `LASTDATE` and other functions with no equivalent (with the measures that depend on them), calculated columns and tables, calculation groups, inactive (`USERELATIONSHIP`), bidirectional and many-to-many relationships, and row-level security roles, which must be recreated as `row_filters` with `$user` fields. One semantic difference is flagged per measure: a plain `CALCULATE` filter replaces the filter on that column in DAX, while Cosmos intersects it, as `KEEPFILTERS` does; numbers differ only when a query also groups or filters by that column.

An equivalence test imports a Power BI version of the e-commerce example, in both formats, and checks that it returns the same numbers as the native model on DuckDB, Postgres and SQL Server.

## Risks

- The standard is still a draft. The compiler isolates the mapping in its own layer, with tests per schema version.
- The Ossie repository already has a reference converter for dbt; assess whether it can replace part of the product's dbt exporter.
- dbt reworked its YAML format in 2026 and keeps the previous one as legacy. The dbt importer is isolated and tested per format version.
