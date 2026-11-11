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
| `DIVIDE(a, b)` | Ratio metric |
| `CALCULATE(m, T[c] = "x", T[d] IN {...}, KEEPFILTERS(...))` | Metric with `filter` |
| `TOTALYTD`, `TOTALQTD`, `TOTALMTD` | Cumulative metric |
| Arithmetic over measures | Derived metric |
| Hierarchies | `custom_extensions` |

Reported as warnings, never dropped silently: iterators (`SUMX` and similar), `FILTER`, `ALL`, variables, `LASTDATE` and other functions with no equivalent (with the measures that depend on them), calculated columns and tables, calculation groups, inactive (`USERELATIONSHIP`), bidirectional and many-to-many relationships, and row-level security roles, which must be recreated as `row_filters` with `$user` fields. Two semantic differences are flagged per measure: a plain `CALCULATE` filter replaces the filter on that column in DAX, while Cosmos intersects it, as `KEEPFILTERS` does (numbers differ only when a query also groups or filters by that column); and `DISTINCTCOUNT` counts BLANK as a value in DAX, while `count_distinct` ignores nulls (use `DISTINCTCOUNTNOBLANK` in Power BI to match).

An equivalence test imports a Power BI version of the e-commerce example, in both formats, and checks that it returns the same numbers as the native model on DuckDB, Postgres and SQL Server.

## Exporting Ossie

`cosmos export ossie -m semantic -o model.ossie.yaml` writes the model as an Ossie `0.2.0.dev0` document, following the mapping above (`--format json` for JSON, `--name` for the model name; without `-o`, standard output carries only the document). It covers models, entities, dimensions, measures, the four metric types, row filters, sensitivity and the glossary:

| Authoring format | In the document |
| --- | --- |
| Models, dimensions | `datasets` with `source`; `fields` with `dimension` (`is_time` for time dimensions) |
| Entities | `primary_key`, `unique_keys`, a field per key expression, and one explicit relationship per join the engine infers |
| Measures, and metrics over one dataset | `metrics` on that dataset in `OSSIE_SQL_2026`, written as the planner computes them: a metric filter becomes `CASE WHEN` inside the aggregation, `AVG(CAST(x AS DOUBLE))`, and division is `CAST(a AS DOUBLE) / NULLIF(b, 0)` |
| Row filters, with the `user_context` fields they read | A `COSMOS_SEMANTIC` extension on the dataset (`row_filters`, `user_context`), as in the mapping above: Ossie has no native field for access policies |
| `sensitivity` | A `COSMOS_SEMANTIC` extension on the field (`{"sensitivity": "personal"}`) |
| Glossary concepts | `ai_context` of the member each concept maps to (a dimension's field, an entity's key field, or a metric on its dataset): every `pref_label` and `alt_labels` entry in `synonyms`, the `definition` in `instructions`. Concepts, hierarchy and per-language labels are in the root extension |
| Everything Ossie does not represent | A `COSMOS_SEMANTIC` extension at the root holding the exported part of the project, which `cosmos import ossie` restores exactly (Rule 4) |

Some metrics have no native expression and live only in the extension, with a warning that states the reason and that other tools will not see them:

- metrics that span datasets: Ossie does not require a reader to aggregate each dataset at its own grain, so a native expression could fan out (a row-level join of orders and payments inflates order revenue);
- cumulative metrics;
- metrics whose filter uses another dataset, a time grain, a business rule or `$user`, or whose derived expression uses bare columns.

**Policies round trip.** Row filters, the user context fields that row filters and metrics read, and `sensitivity` are in the root extension, so `cosmos import ossie` restores the same filters, the same claims and the same personal-data marking; the e-commerce example exports without any flag. They are also written next to their dataset and field, so other tools see them where the data is, and `cosmos import ossie` reads them from there when a tool has dropped the root extension.

**What cannot be exported fails closed.** Business rules are not exported yet, so a row filter that reads one (`{rule.active_customer}`) cannot be carried: the export is refused, exit code 1, naming it: `1 row filter (customers.active_only) would NOT be exported: they use business rules, which the Ossie export does not carry yet; the document would not have this row-level security. Pass --allow-policy-loss to export anyway`. `--allow-policy-loss` now only accepts losing such row filters; the other row filters and all sensitivity marking are still exported. A model without them never needs the flag, and passing it changes nothing. Glossary concepts that map to a business rule, or name such a concept in `broader` or `related`, are left out with a warning. Business rules, user context fields nothing exported reads, teams, systems and processes are not exported yet. Every omitted member becomes a warning on standard error, and the extension records what was omitted (`omitted`: row filter names, counts for the rest, never values). `cosmos import ossie` turns that record back into warnings, so whoever imports the file later learns what is missing.

Tests export the e-commerce example and re-import it. The authoring model is the same, member by member. The row filters, user context and sensitivity are compared after the round trip, field by field. The re-imported model answers the 42 reference queries that need no business rule, with the user's row filter and the sensitivity policy applied (R17 and R18 among them), with the numbers of the hand-written SQL. A Cosmos import of the native part alone, without the root extension, returns the same numbers for every metric it expresses, and still restores the policies, so R17 and R18 pass on it too. This says nothing about other Ossie readers, whose semantics the standard does not yet pin down. Other tests pin the exact native expressions. The number checks run on DuckDB and on every database configured in CI.

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
