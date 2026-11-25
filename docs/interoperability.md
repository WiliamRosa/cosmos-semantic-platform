# Interoperability: Ossie, dbt, Cube and Databricks

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
| dbt | `semantic_models` and `metrics`, in the latest and legacy specs | Reads the YAML without running the dbt project; `Dimension('entity__dim')` filters become `{model.dim}`; a derived metric's input with `offset_window` becomes an offset input (`N days`, `N weeks` as `7N days`, `N months`, `N quarters` as `3N months`, `N years` as `12N months`), and an input read both shifted and unshifted (with an `alias`) becomes a helper metric `<metric>__<alias>`, each with a warning on where MetricFlow's numbers differ (see [Offsets](#offsets) under "Exporting dbt", DEC-36); `percentile`, conversion metrics, `offset_to_grain`, an `offset_window` in another unit (`hour`, a custom granularity) or of zero, and an offset over a cumulative metric or over a metric with offsets of its own become warnings, the last ones skipping the metric with those that use it; a cumulative metric over an `avg` or `count_distinct` measure (to-date, running total or window), which Cosmos cannot accumulate from per-period values, is skipped with a warning, with the ratio and derived metrics that use it (DEC-32); a window over a `count_distinct` measure, which Cosmos recomputes from the facts of each window (DEC-49), is skipped the same way until MetricFlow's numbers are checked against the engine's |
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
| `rolling_window` and multi-stage measures | Cumulative metric: `to_date` as `grain_to_date`, `trailing: N day` with `offset: end` as `window: N days` | Warning and `custom_extensions`; a trailing window in days with `offset: start` or `leading` (it does not end on the row's day), or over an `avg` measure (Cosmos cannot combine it from daily values) or a `count_distinct` measure (Cosmos recomputes it from the facts of each window, DEC-49, but the import does not carry it until Cube's numbers are checked against the engine's), is skipped with its measure, with a warning; so is a `to_date` window (or any other) over an `avg` or `count_distinct` measure (DEC-32). The `number` measures that use a skipped measure are skipped too, each with a warning naming what it depends on |
| `pre_aggregations` and `views` | No MVP equivalent | Kept in `custom_extensions` |
| `access_policy` with one policy for `role: "*"`, `row_level` filters (`equals`, `gt`, `gte`, `lt`, `lte`, `and`/`or`) and `member_level` excludes | `row_filters` (`{ securityContext.x }` becomes `$user.x`, declared in `user_context`) and `sensitivity` (from the dimension's `meta.sensitivity`) | Direct for the shape `cosmos export cube` writes; any other policy fails closed (below) |
| Access control in `cube.js` | `row_filters` and `sensitivity` | Manual: it is JavaScript code; the report lists the rules to recreate |

**Access policies fail closed on import.** Importing part of an `access_policy`, or none of it, would grant more access in Cosmos than Cube did, so `cosmos import cube` refuses, exit code 1, any cube whose policy Cosmos cannot express, naming the cube and the reason: a policy for a specific role, more than one policy, `allow_all`, an operator other than `equals`, `gt`, `gte`, `lt` and `lte` (`notEquals` and `notIn` keep null rows), `gt`/`gte`/`lt`/`lte` on a non-number member, a member of another cube (a member may be plain or prefixed with the cube's own name, `CUBE`, `{CUBE}` or `${CUBE}`), a template other than `{ securityContext.<attribute> }`, a number that is not plain (`1e1`), or a `member_level` exclude of a dimension without `meta.sensitivity` (Cosmos cannot hide a dimension from everyone). The message is `the access_policy of 1 cube (t) would NOT be imported in full (cubes.t: ...); the Cosmos model would grant more access than Cube did. Pass --allow-policy-loss to import anyway`. With `--allow-policy-loss`, each loss is a warning that says the Cosmos model grants more access than Cube did: a policy whose shape or row filters cannot be read is dropped for that cube, while a `member_level` exclude Cosmos cannot express is dropped on its own and the cube's row filters are still imported. Two security context attributes that would become the same `$user` field (`region` and `Region`, in one cube or across cubes) are an error even with the flag.

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
| `AVERAGEX`, `MINX`, `MAXX` over the measure's own table, with the same row expressions as `SUMX` | `avg`, `min`, `max` measure with that row expression. The expression is NULL exactly on the rows where DAX gives BLANK, and those rows are skipped, as DAX skips them: a row with `BLANK + 5` counts as 5, a row with `BLANK * 5` is skipped. `avg` averages as a decimal on every database, as DAX does, even over integer columns |
| `DIVIDE(a, b)` | Ratio metric. With an alternate result other than `BLANK()` (`DIVIDE(a, b, 0)`), it is imported with a warning: DAX returns the alternate result where `b` is zero or BLANK, while Cosmos returns NULL |
| `CALCULATE(m, T[c] = "x", T[d] IN {...}, KEEPFILTERS(...))` | Metric with `filter` |
| `TOTALYTD`, `TOTALQTD`, `TOTALMTD` over a `sum`, `count`, `min` or `max` measure | Cumulative metric with `grain_to_date` (a running minimum or maximum for `min` and `max`) |
| `CALCULATE(m, DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -N, DAY))` over a `sum`, `count`, `min` or `max` measure | Cumulative metric with `window: N days`. Query it only with that date at day grain (or the fact's own date column related to it); the planner accepts any time dimension at day grain, and other date dimensions give different numbers. DAX takes the N dates ending at the last date in context, which at day grain is the row's day: the same days D − (N − 1) to D. A warning per measure names the time dimension to query it with and the differences: grouping or filtering by other columns of the date table (month, weekday) gives different numbers, because the DAX filter on the date column removes the filters on those columns (with a marked date table or a relationship on a date column), while Cosmos keeps one window per value; Power BI also shows days without facts (Cosmos outputs only days with facts, with the same numbers) and coarser periods (where DAX's window ends at the period's last date; Cosmos refuses them) |
| `CALCULATE(m, SAMEPERIODLASTYEAR('Date'[Date]))`, `DATEADD('Date'[Date], -N, DAY \| MONTH \| QUARTER \| YEAR)`, `PARALLELPERIOD('Date'[Date], -N, MONTH)` | Derived metric `{m}` with `inputs: [{metric: m, offset: ...}]`: `N days`, `N months`, `3N months` or `12N months` (`SAMEPERIODLASTYEAR` is `12 months`). `m` is a measure, or a simple, ratio or derived metric over measures; `'Date'` is an imported date table the measure's table reaches through one imported relationship. Query it by that date at a grain the offset fits (month, quarter in multiples of 3 months, year in multiples of 12; day, week in multiples of 7 days), or by the fact's own date column related to it. On whole periods of a date table that holds every day, DAX reads the whole earlier period too: it moves each date in context, and a date on a month's last day takes the rest of the shifted month with it, so April a month back is all of March and February 2025 a year back includes 29 February 2024; `PARALLELPERIOD` by month returns whole months. See the differences below |
| Arithmetic over measures, and `DIVIDE` over arithmetic (`DIVIDE([m] - [m PY], [m PY])`) | Derived metric (division is safe against zero; `[A] / DIVIDE([B], [C])` keeps its grouping, a / (b / c)) |
| Hierarchies | `custom_extensions` |

Reported as warnings, never dropped silently: other iterators (`COUNTX`, `PRODUCTX` and similar) and `SUMX`, `AVERAGEX`, `MINX` or `MAXX` over another table or with a division, a function or `RELATED` (DAX division returns infinity or BLANK where SQL fails, returns NULL or truncates integers), `TOTALYTD`, `TOTALQTD` or `TOTALMTD` over an `avg` or `count_distinct` measure (DAX recomputes it over all the dates to date, which per-period values cannot rebuild; imported, it would fail validation), `DATESINPERIOD` in another unit (`MONTH`, `YEAR`), forward (a positive count), from another anchor (`LASTDATE`, `TODAY()`, another column), with other `CALCULATE` filters, over a date table the measure's table has no imported relationship to, or over an `avg` or `count_distinct` measure (a `count_distinct` window runs in Cosmos, recomputed from the facts, DEC-49, but is not imported until Power BI's numbers are checked against the engine's), `DATEADD` and `PARALLELPERIOD` forward (a positive count) or by zero, in another unit (`WEEK`; `DAY` for `PARALLELPERIOD`), with other `CALCULATE` filters, over a date column of the measure's own table (DAX moves only the dates the column holds, so the earlier period keeps only the days with facts in the current one), over a date table the measure's table has no imported relationship to, over a running total or a measure that already reads an earlier period (offsets do not combine yet), `PARALLELPERIOD` by `QUARTER` or `YEAR` (it returns the whole earlier quarter or year, so by month Power BI shows the whole period where an offset would read one month; by quarter or year it is `DATEADD`, which is imported), and `DATESBETWEEN`, `FILTER`, `ALL`, variables, `LASTDATE` and other functions with no equivalent (with the measures that depend on them), calculated columns and tables, calculation groups, inactive (`USERELATIONSHIP`), bidirectional and many-to-many relationships, and row-level security roles, which must be recreated as `row_filters` with `$user` fields. Three semantic differences are flagged per measure: a plain `CALCULATE` filter replaces the filter on that column in DAX, while Cosmos intersects it, as `KEEPFILTERS` does (numbers differ only when a query also groups or filters by that column); `DISTINCTCOUNT` counts BLANK as a value in DAX, while `count_distinct` ignores nulls (use `DISTINCTCOUNTNOBLANK` in Power BI to match); and an imported `DATESINPERIOD` window gives different numbers when a query groups or filters by other columns of the date table, or uses another date dimension (see the table above).

**Period shifts differ from DAX outside whole periods.** DAX moves the set of dates in the filter context; Cosmos moves the start of the query's period (see [offsets](semantic-model.md)). Each imported period shift gets one warning that names the time dimensions to query it by and these differences:

- **Filters that cut a period.** A date filter or slicer that starts or ends inside a period gives DAX a partial earlier period: with dates from 15 March 2025, `DATEADD(..., -1, MONTH)` for March reads 15 to 28 February. `PARALLELPERIOD` still reads the whole earlier month. Cosmos applies the request's filters to the facts of the earlier period too, so the earlier value covers only the filtered dates (here none, so NULL). This holds for filters of whole periods too: with the year 2025 selected, DAX's January 2025 a month back reads December 2024, and a year back reads January 2024, while in Cosmos both are NULL, because the filter removes the facts of 2024. By month, the month offset agrees again from February on (January is inside the filter), but the year offset is NULL for every month of 2025. A date table that ends inside a period (a calendar up to today) also gives DAX a partial earlier period, while Cosmos reads the whole one.
- **Month ends by day.** By day, DAX moves each date and clamps it to the month's end (29, 30 and 31 March a month back are all 29 February 2024). The planner refuses a month offset by day, and a day offset by month, instead of returning another number.
- **Other columns of the date table.** Grouping or filtering by another column of the date table (a weekday) gives different numbers: DAX moves the dates, which can fall on another weekday, while Cosmos reads the earlier period with the same value of that column.
- **Rows.** Power BI shows a period without facts whose earlier period has some (the month after the last month of data); Cosmos outputs only the periods with facts, and a period whose earlier period has none gets NULL.
- **BLANK in arithmetic.** In DAX a BLANK side of `+` or `-` counts as zero, while in Cosmos a NULL side makes the result NULL: where the earlier period has no facts, `[m] - [m PY]` is `[m]` in Power BI and NULL in Cosmos. A growth that divides by the earlier period, `DIVIDE([m] - [m PY], [m PY])`, is BLANK and NULL alike. Each measure with a shifted measure (or a ratio or copy of one) inside an operand of `+` or `-` gets a warning; one that only divides by it, `DIVIDE([m] - [n], [m PY])`, does not.

An equivalence test imports a Power BI version of the e-commerce example, in both formats, and checks that it returns the same numbers as the native model on DuckDB, Postgres and SQL Server. Imported `SUMX`, `AVERAGEX`, `MINX` and `MAXX` measures are also checked against hand-written SQL on data with NULLs in every numeric column. Imported `DATESINPERIOD` windows (`sum`, `max` and `COUNTROWS` over 1, 3 and 7 days, through a date table) are checked against hand-written SQL by day, and by day and a column of the fact table (which partitions the window in DAX too), on data with days without facts inside the windows. A fact without a date gives a row with a null day and a null value in Cosmos; the blank row Power BI shows for it is not checked. Imported period shifts (`SAMEPERIODLASTYEAR`, `DATEADD` by 1 and 7 days, 1 month and 1 quarter, `PARALLELPERIOD` by 1 month, over `SUM` and `COUNTROWS`, through a date table) are checked by day, week, month, quarter and year, and by month and a column of the fact table, against hand-written SQL over period numbers and against a model of DAX's date shifting written from its documentation (Power BI itself is not run), on data with month ends of months of different lengths, 29 February, a year change and months without facts. The same test checks the documented differences: a date filter that starts inside March, the missing row of a month without facts, and the refusals by day and by month; a growth measure is checked by year.

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

**Policies round trip.** Row filters, the user context fields that row filters and metrics read, and `sensitivity` are in the root extension, so `cosmos import ossie` restores the same filters, the same claims and the same personal-data marking; the e-commerce example exports without any flag. They are also written next to their dataset and field, so other tools see them where the data is, and `cosmos import ossie` reads them from there when a tool has dropped the root extension. The root extension is authoritative: when a tool strips it, the import falls back to these copies and warns that they must be verified and that what lives only in the root extension (some metrics, the user context only they read, glossary concepts, business rules no row filter reads, teams, systems and processes) is missing; a `user_context` field or a business rule the copies declare in conflicting ways is refused. A tool that also strips the field copies loses the sensitivity marking. The business `timezone` is copied next to every dataset, with row filters or not, so the fallback import keeps "today" (`CURRENT_DATE`, month-to-date and the rule version in effect) for every model; copies that declare different time zones are refused.

**The glossary is visible to agents.** Glossary labels and definitions go into `ai_context`, which agents and other tools read, so they must not contain personal data.

**Business rules and business context round trip.** Rules keep every field: kind, owner, inputs, `logic` or `decision_table`, `applies_to`, tests, `effective_from`, `change_note` and each earlier version in `history`, so a query replayed as of a past date uses the same rule version, and gives the same numbers, after the round trip. Teams, systems and processes come back exactly too, and so do glossary concepts that map to a rule. A row filter that reads a rule (`{rule.active_customer}`) is exported with it, so no model needs `--allow-policy-loss` any more: `cosmos export ossie` still accepts the flag, for compatibility, and it has no effect. The rules a dataset's row filters read, together with the rules those rules bind an input to (in the order of `business_rules`), are also copied next to that dataset with the business `timezone`, so a Cosmos import of a document whose root extension a tool dropped still has the row-level security, and "today" (which decides the rule version and window when a query has no `as_of`) is the same; copies that declare a rule or the time zone in conflicting ways are refused.

**The business context leaves with the file.** The export now carries teams, systems and processes (including their `contact` and `owner` fields) and the values in rule tests, so any personal names or e-mail addresses in them leave with the document.

**What is not exported.** User context fields that no row filter or metric reads. Each becomes a warning on standard error, and the extension records the count (`omitted`, never values); `cosmos import ossie` turns that record back into warnings, so whoever imports the file later learns what is missing. Documents written by earlier slices may record omitted row filters or other members; the import still reports them.

Tests export the e-commerce example and re-import it. The authoring model is the same, member by member. The row filters, user context, sensitivity, business rules with every version and their tests, teams, systems and processes are compared after the round trip, field by field. The re-imported model passes all 76 reference cases, business rule and glossary ones included, with the user's row filter and the sensitivity policy applied (R17 and R18 among them), with the numbers of the hand-written SQL. A row filter that reads a rule filters the same rows after the round trip, and rule queries replayed as of a date before a rule change (with `as_of`, and with `rules_as_of` on either side of the change) give the same numbers on the original and the re-imported model. Without the root extension, a row filter over a rule bound to another rule filters the same rows, and a query without `as_of` on a frozen clock (evening in New York, the next day in UTC) uses the same rule version as the original; on a model without row filters, a query that reads `CURRENT_DATE` at 22:30 in New York gives the same date and number as the original. A Cosmos import of the native part alone, without the root extension, returns the same numbers for every metric it expresses, and still restores the policies, so R17 and R18 pass on it too. This says nothing about other Ossie readers, whose semantics the standard does not yet pin down. Other tests pin the exact native expressions. The number checks run on DuckDB and on every database configured in CI.

## Exporting Cube

`cosmos export cube -m semantic -o model/cubes/model.yml` writes the model as Cube static YAML (without `-o`, standard output carries only the YAML). `cosmos import cube` reads it back with the same numbers. It covers models, joins, dimensions, measures, the simple, ratio and derived metrics, since the second slice (DEC-16) running totals since the start of a period, since the third (DEC-20) row filters and sensitivity as a Cube `access_policy`, and since the fourth (DEC-30) rolling windows in days, and in weeks (DEC-27):

| Authoring format | In Cube |
| --- | --- |
| Model | A cube with `sql_table`, `description`, `data_source`, and the owner in `meta` |
| `primary` entity | The dimension with the key's expression marked `primary_key`, or a hidden one (`public: false`) added for it |
| `foreign` entity | A `many_to_one` join on the many side, one per join the engine infers: `{CUBE}.customer_id = {customers}.customer_id` |
| Dimensions | Dimensions: `categorical` becomes `string`; `time`, `number` and `boolean` keep their type |
| Measures | Measures of the same type; `avg` is written as `AVG(1e0 * x)`, so Cube averages real numbers, as the planner does (an integer AVG truncates in SQL Server) |
| Simple metric | A measure of its measure's type, with the metric `filter` in `filters`; a dimension reference becomes its expression on the cube |
| Ratio and derived metrics | `number` measures over the measures they use; division is written `1e0 * a / NULLIF(b, 0)`, real and null on a zero denominator |
| Cumulative metric with `grain_to_date`, over a `sum` or `count` measure | A measure of its measure's type with `rolling_window: {type: to_date, granularity: <grain_to_date>}`; `cosmos import cube` reads it back as the same cumulative metric over a `<name>__base` measure |
| Cumulative metric with `window: N days` (or `N weeks`), over a `sum`, `count`, `min` or `max` measure | A measure of its measure's type with `rolling_window: {trailing: N day, offset: end}` (`trailing: 7N day` for a window of N weeks, DEC-27); `cosmos import cube` reads it back as the same window over a `<name>__base` measure, a window in weeks as its days (`2 weeks` comes back as `14 days`, with the same numbers) |
| Metric `label`, `description` and owner | `title`, `description` and `meta` |
| Row filters on the model's own `categorical` or `number` dimensions | One `access_policy` entry for `role: "*"` with all the filters in `row_level.filters`; the row filter names in the cube's `meta.row_filters` |
| `$user.<field>` | `"{ securityContext.<claim> }"`, the field's `claim` in `user_context` |
| `sensitivity: personal` or `sensitive` | The dimension in `member_level.excludes` (with `includes: "*"`), and the level in the dimension's `meta.sensitivity` |

The `1e0 *` factor is a floating-point literal in DuckDB, SQL Server, MySQL, MariaDB, Databricks and Snowflake, and a high-precision numeric in Postgres and Oracle, so it avoids integer division and MySQL's four extra digits for decimal division. The planner writes `CAST(x AS DOUBLE)` instead: the results agree to floating-point precision, but the SQL is not the same.

Column names in expressions get a `{CUBE}.` prefix, so Cube does not confuse columns of joined cubes; the rest of the expression is written as in the model, and `cosmos import cube` removes the prefix. A ratio or derived metric goes on the cube of its first measure, and refers to members of other cubes as `{payments.paid_value}`.

**Metrics that span cubes are exported with a warning** (Rule 3). `collection_gap` and `items_per_order` in the example combine measures of two cubes; the engine aggregates each measure at its own grain, and Cube must do the same to return the same numbers, so check them in Cube.

**Row filters and sensitivity.** The example's `customers` model (`{region} = $user.region`, three personal or sensitive dimensions) is written as

```yaml
- name: customers
  meta:
    owner: digital-commerce
    row_filters:
    - by_region
  # dimensions: email, tax_id and health_plan carry meta: {sensitivity: personal|sensitive}
  access_policy:
  - role: '*'
    row_level:
      filters:
      - member: region
        operator: equals
        values:
        - '{ securityContext.region }'
    member_level:
      includes: '*'
      excludes:
      - email
      - tax_id
      - health_plan
```

Cube grants the rows of every policy that matches the user's role, so the export writes a single policy for every role (`role: "*"`): a second policy would OR its filters with the first, and a role without a matching policy would be outside it. All row filters of a model go in its one `row_level.filters` list, which Cube ANDs, as the planner does. A row filter is written only when Cube's filter has the same meaning:

- `{dim} = x` and `{dim} IN (x, y)` become `equals`; `<`, `<=`, `>`, `>=` become `lt`, `lte`, `gt`, `gte` on `number` dimensions only; `AND` and `OR` become `and` and `or` groups. Each side may be written either way round (`$user.region = {region}`).
- `x` is a string for a `categorical` dimension, a number for a `number` dimension, or `$user.<field>`. These operators drop the rows where the dimension is null, in SQL and in Cube.
- Everything else is refused, with its reason: a business rule (`{rule.active_customer}`), `<>` and `NOT IN` (Cube's `notEquals` and `notIn` keep the null rows that SQL drops, so they would show more rows), `IS NULL`, `NOT`, functions (`LOWER({region})`), time and boolean dimensions, time grains, other models' dimensions, comparing two dimensions, a value of the wrong type, a string with braces (Cube would read it as a template), a dimension marked with `sensitivity` (hidden by `member_level`), and a `$user` field whose claim is not a plain name (`org.region`).

**A user without the attribute.** In Cosmos, a query that needs a row filter's `$user` field is denied when the user lacks it (`PolicyError`), and a missing `required` claim is already a 403. Your Cube deployment must do the same: its `checkAuth` must reject a security context without each attribute the policies read, and must reject list values (Cosmos refuses a list in `$user`). The export warns about each field it reads, `user_context: the row filters read $user.region as securityContext.region ...`.

**Not verified against Cube's runtime.** The tests check the export through `cosmos import cube` and the Cosmos planner, not through Cube. These points follow Cube's documented syntax but have not been run in Cube, so check them in your deployment before relying on the policy:

- whether `role: "*"` also matches users who have no roles at all;
- whether a cube's policy is enforced when the cube is reached through a view or a join from another cube (define a policy on any view over these cubes, and test both paths);
- what Cube does with `{ securityContext.<attribute> }` when the attribute is missing or is a list (hence the `checkAuth` requirement above).

**Personal data is hidden from everyone in Cube.** `member_level` excludes hide the `personal` and `sensitive` dimensions from every Cube user, people included. Cosmos only refuses them to AI agents (and never lists their values); people query them through the API and CLI. The Cube model is stricter, on purpose, since Cube has no notion of an agent channel.

**What cannot be exported fails closed.** A row filter Cube cannot express is refused, exit code 1, naming it and why: `1 row filter (customers.active_only) would NOT be exported (customers.active_only: it reads the business rule `rule.active_customer`); the Cube model would show those rows to every user. Pass --allow-policy-loss to export anyway`. With the flag, each one dropped is a warning, and the model's other row filters are still exported. Metrics Cube cannot carry here are refused the same way, until `--allow-metric-loss` is given: metrics that read an input at an earlier period (`inputs` with an `offset`); running totals over `avg`, `count_distinct`, `min` or `max` measures, and running totals without `grain_to_date`; rolling windows over a `count_distinct` measure, which the engine recomputes from the facts of each window (DEC-49), until Cube's numbers are checked against the engine's; ratio and derived metrics over a cumulative metric (the planner does not compute a cumulative metric inside another one, so there is no number for Cube to match); simple metrics whose filter uses another cube, a time grain, a business rule or `$user`; ratio metrics with a filter (a `number` measure cannot filter the measures it uses); derived metrics over bare columns; and metrics that use a metric left out. With the flags, each loss is a warning on standard error. Business rules, the glossary, teams, systems, processes, `custom_extensions` and the time zone are not exported yet, and each one present is a warning too; the user context is not exported as declarations (its fields come back from `securityContext`, with the claim as their name and `required: true`). Cube's `meta` (other than `row_filters` and `sensitivity`), `title` and `data_source` are written, but `cosmos import cube` does not read them back yet.

**Running totals.** `revenue_month_to_date` in the example is written as

```yaml
- name: revenue_month_to_date
  type: sum
  sql: '{CUBE}.amount'
  rolling_window:
    type: to_date
    granularity: month
  description: Month-to-date revenue
  meta:
    owner: controllership
```

For each time bucket of the query, Cube's `to_date` window aggregates the rows from the start of the period that holds the bucket to the end of the bucket. For a `sum` or `count` measure this is the planner's running total: the per-bucket values summed from the start of each period, separately for each value of the other dimensions; when each bucket holds whole periods (month-to-date by month or by year), each bucket is its own total, on both sides. Weeks start on Monday in both, but do not nest in months, quarters or years (below). Running totals of other aggregations are not exported (a running average or distinct count is not a sum of per-bucket values); rolling windows in days are below. To get the same numbers in Cube:

- Query the measure with exactly one time dimension and a granularity, the time dimension of the measure's cube, as the planner requires; without a time dimension Cube has no buckets to accumulate over.
- Cube returns every bucket of the query's date range, including buckets without rows (they carry the total so far), and may require a `dateRange` for rolling windows; Cosmos returns only buckets with rows. Buckets returned by both have the same numbers.
- Cube's window reads rows before the start of the `dateRange`, back to the start of the period, and so does Cosmos since DEC-48 for a filter on the time dimension written as a range, an equality or an `IN` list (A09, the last 45 days): the filter chooses the buckets, and each bucket reads its period from the start. A shape Cosmos cannot split, such as `NOT`, still filters the rows first (A62).
- Rows with a null time: Cosmos returns them as a null bucket with its own total; Cube's time series leaves them out.
- Weeks against months, quarters and years: Cube starts the window at the start of the period that holds the bucket's first day, so month-to-date by week also counts, in Cube, the days of the month before its first Monday in that month's week buckets, and week-to-date by month also counts the days of the previous month back to the Monday. Query week-to-date at day or week grain, and month, quarter or year-to-date at day, month, quarter or year grain, for the same numbers; the planner refuses the other combinations (a grain coarser than the period, DEC-21, and a week under a month, quarter or year, DEC-28), while Cube answers them.

**Rolling windows in days.** `revenue_rolling_7_days` in the example is written as

```yaml
- name: revenue_rolling_7_days
  type: sum
  sql: '{CUBE}.amount'
  rolling_window:
    trailing: 7 day
    offset: end
  description: Revenue of the 7 days ending on each day
  meta:
    owner: controllership
```

With `offset: end` (Cube's default, written out), Cube's window for each time bucket takes the rows whose time is after the end of the bucket minus N days and up to the end of the bucket. A day bucket ends at its last instant, so the window is the N days D - (N - 1) to D, the row's day included: the planner's `window: N days`. Cube aggregates the rows of the window with the measure's type, and the planner combines the daily values: the same for `sum`, `count` (a sum of daily counts), `min` and `max`, separately for each value of the other dimensions. `offset: start` would end the window before the bucket starts (the N days before D, D excluded), which is why the export writes `offset: end` and the importer skips the other. Windows over `avg` measures cannot be defined in Cosmos, so there is nothing to export. A window over a `count_distinct` measure is recomputed from the facts of each window in Cosmos (DEC-49); the export refuses it as metric loss (dropped with a warning under `--allow-metric-loss`) until Cube's numbers are checked against the engine's. `cosmos import cube` skips a Cube trailing window over either, with its measure and a warning, instead of importing a model that would not validate. Where Cube's trailing window differs:

- **Days with no facts.** Cube returns every day of the query's date range, and a day without rows carries the window of the days before it; Cosmos returns only the days with rows. Days returned by both have the same numbers.
- **Coarser grains.** The planner answers a window only at day grain and refuses week, month and coarser grains. Cube accepts them, with a window of N days back from the end of each bucket (for a month bucket, the last N days of the month), which is neither the bucket's total nor a Cosmos number. Query the measure at day grain.
- **Date filters.** Cube's window reads the N - 1 days before the start of the `dateRange`, and so does Cosmos for a filter on the window's time dimension written as a range, an equality or an `IN` list (DEC-34; A30 by status over the last 45 days). A filter shape Cosmos cannot split, such as `NOT`, still restricts the rows first and cuts the first windows of the range ([semantic model](semantic-model.md#rolling-windows)).
- **The time dimension.** Query it by the day of a time dimension of the measure's own cube. By a joined cube's day (`customers_rolling_30_days` by an order day), Cube and the planner may aggregate the fanned-out rows differently (Rule 3).
- **Rows with a null time.** Cosmos returns them as a null day with a null value; Cube's time series leaves them out.
- A window that is not whole days (`36 hour`), or in months, is not a Cosmos window; `cosmos import cube` keeps `trailing: 1 month` as a window queries refuse, with a warning. A window in weeks (`trailing: 2 week`) is imported as `window: 2 weeks`, 14 days (DEC-27). The export never writes weeks: Cube does not render a `week` interval the same way on every database (its Oracle query drops it, and its Presto and Trino queries write an interval that may not parse), so a window of N weeks is exported as `trailing: 7N day`.

Ratio and derived metrics over a rolling window stay refused, as over a running total: the planner does not compute a cumulative metric inside another one. The window boundaries above were checked against Cube's query-builder source (`BaseQuery.ts`, whose `rollingWindowDateJoinCondition` writes `time > date_to - trailing AND time <= date_to` for `offset: end`, and `CubeValidator.ts`, which accepts `N day` and `N days` and defaults `offset` to `end`); Cube's runtime was not run. One edge case: a day bucket's end is D 23:59:59 followed by as many 9s as Cube's timestamp precision (D 23:59:59.999 at milliseconds), so a `trailing: N day` window starts just after that instant on D-N, and a timestamp finer than Cube's precision that falls in that last instant of D-N is in Cube's window but not in Cosmos's, which starts at D-(N-1) 00:00.

**The time zone.** In Cosmos, `timezone` only defines "today" (`CURRENT_DATE` and relative dates); model timestamps are already local time and are never converted. Cube's query `timezone` converts timestamps from UTC, so setting it to the model's zone would shift day, week and month buckets. Keep Cube's query time zone at UTC: buckets then match, but relative dates such as "today" follow UTC in Cube, not the model's zone. The export warns about this gap.

**Known limits.**

- A `boolean` dimension whose expression is a predicate (`discount > 0`) is written as is; SQL Server and Oracle do not accept a bare predicate as a selected value, so Cube may fail there.
- `sql_table` is the model's logical table name: a data source's `schema_map` (for example `public` to `cosmos` in SQL Server) is not applied, so map the schema in Cube.
- The `{CUBE}.` prefix finds columns with SQLGlot's generic tokenizer: a quoted column name, or one that is also a keyword in the target database, may not be prefixed correctly; check those expressions.
- Expressions are written as in the model, not translated to the target database's dialect.

Tests export the e-commerce example with `--allow-metric-loss` and import it back with `cosmos import cube`, which reports nothing. They check the numbers through the Cosmos importer and planner, not Cube's own runtime: they show that the export carries the model without loss, not how Cube executes it. Models, tables, dimensions and measures come back the same; filtered metrics come back as a `<name>__base` measure plus a simple metric, ratio metrics as derived ones. The re-imported model answers the 37 reference queries that need no business rule, running maximum, offset or user policy with the numbers of the hand-written SQL, year-to-date by month (A08), month-to-date by status and day (A09), the date-filtered running totals A59, A61 and A62 and the rolling windows A29, A30 and A34 among them; with the row filter, user context and sensitivity it read back from the `access_policy`, it answers all 61 that need no business rule, running maximum or offset, R17, R18, the refused month-to-date by quarter (A32) the 30-day window of customers by segment (A33) and the refused year-to-date by week (A39) among them. A model with every exported filter shape (`IN`, `AND` inside `OR`, `$user` on either side, `<=` against `$user` and `>` on a number dimension) returns the same rows as the original for four principals, and both deny a user without the field; each shape refused on export, and each policy refused on import, is tested with its error and, with `--allow-policy-loss`, its warning. Every measure and metric of an extended example (`max` and `min` measures, filtered `count`, `count_distinct`, `avg` and `max`, a filter on a dimension with an expression, nested division) returns the native numbers. Running totals of `sum` and `count` measures of two cubes for every period (day to year) also come back with the native numbers when the re-imported model is queried through Cosmos at every grain the planner accepts (both models refuse a grain coarser than the period and a week under a longer period), with another dimension, and with a time filter that starts inside a period; this shows the definition survives the round trip, not that Cube's runtime agrees at those grains (see the conditions under "Running totals" above). Windows of 1, 7 and 30 days over `sum`, `count`, `min` and `max` measures of two cubes come back with the native numbers by day, by day and another dimension, and with a time filter that starts the range (both read the days before it), and both models refuse them by week and by month; a trailing window with `offset: start` or `leading`, or over an `avg` or `count_distinct` measure, is skipped on import, with no `__base` measure left behind. The number checks run on DuckDB and on every database configured in CI.

## Exporting dbt

`cosmos export dbt -m semantic -o models/semantic_models.yml` writes the model as dbt Semantic Layer (MetricFlow) YAML, in the spec with top-level `semantic_models` and `metrics` (without `-o`, standard output carries only the YAML). `cosmos import dbt --schema <schema>` reads it back with the same numbers. It covers models, entities, dimensions, measures, the simple, ratio and derived metrics, rolling windows in days (DEC-25) and weeks (DEC-27), running totals since the start of a period (DEC-31), and derived metrics that read an input at an earlier period (DEC-36):

| Authoring format | In dbt |
| --- | --- |
| Model | A semantic model with `model: ref('<table>')`, `description`, the owner in `config.meta`, and `defaults.agg_time_dimension` set to its first time dimension |
| Entities | Entities of the same type, with `expr` |
| Dimensions | `categorical`, and `time` with `time_granularity: day` (the model does not say how fine a column is, so dbt groups it by day at the finest, never by hour as the engine can); `number` and `boolean` become `categorical`, with the type kept in `config.meta.cosmos_type`, which `cosmos import dbt` reads back |
| Measures | Measures with `create_metric: true`, so derived and ratio metrics can use them as dbt requires; `avg` becomes `average` over `1e0 * x`, and a `count` without `expr` counts `1` |
| Simple metric | `type: simple` over its measure; the filter's `{status}` becomes `{{ Dimension('order__status') }}`, named by the primary entity of the dimension's model |
| Ratio metric | `type: ratio` with `numerator`, `denominator` and the filter |
| Derived metric | `type: derived` with `expr` and `metrics`; division is written `1e0 * a / NULLIF(b, 0)`, as in the Cube export |
| Cumulative metric with `window: N days` (or `N weeks`), over a `sum`, `count`, `min` or `max` measure | `type: cumulative` with `type_params.measure` and `type_params.cumulative_type_params.window: N days` (`1 day` for one; `N weeks` for a window in weeks, which MetricFlow subtracts as 7N days), which `cosmos import dbt` reads back as the same metric; see "Rolling windows" below |
| Cumulative metric with `grain_to_date`, over a `sum` or `count` measure | `type: cumulative` with `type_params.measure` and `type_params.cumulative_type_params` set to `grain_to_date: <period>` and `period_agg: last`, which `cosmos import dbt` reads back as the same metric; see "Running totals" below |
| Derived metric with `inputs: [{metric, offset}]` | `type: derived` whose shifted input carries `offset_window`, in the largest unit that holds the offset a whole number of times: `1 day`, `7 days` as `1 week`, `14 days` as `2 weeks`, `1 month`, `3 months` as `1 quarter`, `12 months` as `1 year`; an input's `alias` (DEC-44) is MetricFlow's input `alias`, and the expression reads it by that name; `cosmos import dbt` reads it back as the same offset; see "Offsets" below |
| Metric `label`, `description` and owner | `label` (the metric's name when it has none, since dbt requires one), `description` and `config.meta.owner` |

**What cannot be exported fails closed**, as in the Cube export. A model with row filters or `sensitivity` is refused, exit code 1, naming them, until `--allow-policy-loss` is given. Metrics the export cannot carry are refused until `--allow-metric-loss` is given: running totals over `min` or `max` measures, running totals without `grain_to_date` or a window, and running totals over a model without a time dimension; rolling windows in another unit than days or weeks (`1 month`, which the engine does not compute) or over a model without a time dimension; rolling windows over a `count_distinct` measure, which the engine recomputes from the facts of each window (DEC-49), until MetricFlow's numbers are checked against the engine's; ratio and derived metrics over a cumulative metric, rolling windows included (the planner does not compute a cumulative metric inside another one, so there is no number for MetricFlow to match); metrics whose filter uses a time dimension (MetricFlow compares it truncated to its `time_granularity`), a business rule or `$user`, or a dimension whose model has no primary entity; derived metrics over bare columns; ratio and derived metrics over a filtered count, and ratios whose own filter turns a count into one (see below); offsets over a measure whose model has no time dimension for MetricFlow to shift; and metrics that use a metric left out, such as a ratio over a running maximum. With the flags, each loss is a warning on standard error. Business rules, the glossary, teams, systems, processes, the user context, `data_source`, `custom_extensions` and the time zone are not exported yet, and each one present is a warning too. The export also warns that `ref()` names the table without its schema, so import with `--schema` (tables in several schemas cannot all come back), and that a model with measures and no time dimension needs an `agg_time_dimension` in dbt.

**Filtered metrics and empty groups.** The engine filters a metric inside its own aggregation, so a group with no matching rows is kept: 0 for a count, NULL for a sum. MetricFlow applies the filter as a `WHERE` in each metric's own subquery and joins the subqueries, so a group with no matching rows has no row when the metric is queried alone, and NULL alongside other metrics. For a sum both mean no value; for a count the engine returns 0 where MetricFlow returns no row or NULL. That is why a ratio or derived metric over a filtered `count` or `count_distinct` metric (such as `{order_count} - {completed_orders}`) is refused as metric loss: MetricFlow would return NULL where the engine returns a number. A ratio's own `filter` is applied inside the aggregation of every measure it reaches, nested ones included, so it turns a count into a filtered count too: a ratio with a `filter` is refused when any measure it reaches is a `count` or `count_distinct` and either its measures span more than one model or one of its sides is a ratio or derived metric (`payment_count / order_count` filtered by order status is refused; `revenue / order_count` filtered by status, two plain sides on one model, is exported). Filtered sums are exported.

**Rolling windows.** The engine's rolling windows follow MetricFlow's convention on purpose: the value on day D aggregates the facts dated from D − (N − 1) to D, both included (MetricFlow joins on `metric_time <= D` and `metric_time > D - N days`), so `7 days` on 10 March covers 4 to 10 March in both, and `sum`, `count`, `min` and `max` give the same value over the same rows. MetricFlow windows over `metric_time`, which for the measure is its model's `agg_time_dimension`, the model's first time dimension in the export. MetricFlow needs a time spine model in the dbt project to compile cumulative metrics; the export does not write one. Each exported window is a warning that says where the numbers match and where they differ:

- **Where they match:** queried by that time dimension at day grain (`metric_time__day`, or `orders.ordered_at__day` in Cosmos), alone or with other dimensions, every day the engine returns has the same value in MetricFlow.
- **Days without facts:** MetricFlow joins a time spine, so it also returns the days without facts whose window has some (up to N − 1 days after the last fact, and the gaps between facts), with the value of the facts still in the window; the engine returns only the days with facts. The days both return have the same numbers.
- **Other grains:** the engine refuses a window by week, month or a coarser grain; MetricFlow returns one value per period (by default the window's value on the first day of the period, `period_agg: first`).
- **Time filters:** MetricFlow's time constraint (`--start-time`/`--end-time`) does not cut its window: it still reads the facts before the range's start. Neither does the engine's filter on the window's time dimension written as a range, an equality or an `IN` list (DEC-34; A30 over the last 45 days): the filter chooses the days, and each day's window reads the N − 1 days before it. A shape the engine cannot split, such as `NOT`, still filters the facts first. A `where` filter on `metric_time` takes another path in MetricFlow and has not been checked.
- **Another time dimension:** the engine windows over whichever time dimension the query names (`customers_rolling_30_days` by `customers.last_order_at`, A33), while MetricFlow always windows over `metric_time`; the warning names the model's other time dimensions, which give other numbers.
- **Facts without a date:** the engine returns a row with a null day and a null value; MetricFlow drops them with the time spine join.

**Running totals.** `revenue_month_to_date` in the example is written as

```yaml
- name: revenue_month_to_date
  label: revenue_month_to_date
  description: Month-to-date revenue
  type: cumulative
  type_params:
    measure: revenue
    cumulative_type_params:
      grain_to_date: month
      period_agg: last
  config:
    meta:
      owner: controllership
```

MetricFlow joins each day of its time spine to the facts dated from the start of that day's period (`date_trunc` to the `grain_to_date`) up to the day, both included, so by day its value is the sum of the daily values from the start of the period: the planner's running total, for `sum` and `count` (a sum of daily counts) alike, separately for each value of the other dimensions. Queried at a coarser grain, MetricFlow computes the same daily values and keeps one per bucket with `period_agg`: `last` takes the bucket's last day, which is the total from the start of the period to the end of the bucket, the planner's running sum of the per-bucket values (by month, month-to-date is each month's total and year-to-date the total through the month's last day). MetricFlow's default, `first`, would take the bucket's first day instead, so the export always writes `last`. Running totals of `min` and `max` measures are not exported yet, and those of `avg` and `count_distinct` measures cannot be defined in Cosmos. MetricFlow accumulates over `metric_time`, its model's first time dimension in the export, and needs a time spine model in the dbt project, as for rolling windows. Each exported running total is a warning that says where the numbers match and where they differ:

- **Where they match:** queried by that time dimension (`metric_time`, or `orders.ordered_at` in Cosmos) at day grain or at a grain the engine accepts for the period: day or week for week-to-date, and day, month, quarter or year up to the period for month, quarter and year-to-date (by month for year-to-date, A08; by day and status for month-to-date, A09), alone or with other dimensions; every bucket the engine returns has the same value in MetricFlow. The exported time dimensions are grouped by day at the finest in dbt, so the engine's hour grain has no counterpart.
- **Periods without facts:** MetricFlow's spine join keeps every day (and bucket) after the first fact of its period, with the total so far, and drops the days before it; the engine returns only the buckets with facts. The buckets both return have the same numbers. MetricFlow's last day of a bucket is the last day of its time spine inside the bucket, so the spine must cover the dates queried.
- **Grains the engine refuses:** a grain coarser than the period (month-to-date by quarter, A32: each quarter would only give its total, DEC-21) and a week under a month, quarter or year (year-to-date by week, A39: a week can cross into a new period, DEC-28) are refused by the engine. MetricFlow answers them with the value on the bucket's last day: month-to-date by year is December's total, and year-to-date by week is the total of the year that holds the week's Sunday, through that Sunday.
- **Time filters:** MetricFlow's time constraint (`--start-time`/`--end-time`) moves the facts' start back by one period, so its totals still include the facts between the start of the period and the start of the range. Since DEC-48 so does the engine's filter on the running total's time dimension written as a range, an equality or an `IN` list (A09, the last 45 days): the filter chooses the periods, and each reads its period from the start. A shape the engine cannot split, such as `NOT`, still filters the facts first (A62). A `where` filter on `metric_time` takes another path in MetricFlow and has not been checked.
- **No time dimension:** the engine refuses a running total queried without a time dimension; MetricFlow returns the measure over all time.
- **Another time dimension:** the engine accumulates over whichever time dimension the query names, while MetricFlow always accumulates over `metric_time`; the warning names the model's other time dimensions, which give other numbers.
- **Facts without a date:** the engine returns a row with a null period; MetricFlow drops them with the time spine join.

`cosmos import dbt` reads `grain_to_date` and does not read `period_agg`: a dbt running total with `period_agg: first` (MetricFlow's default) or `average` is imported as the engine's running total, which matches MetricFlow by day but not at coarser grains. The behaviour above was checked against MetricFlow's source (0.214: the `grain_to_date` join `metric_time >= date_trunc(<period>, day) AND metric_time <= day`, the `period_agg` re-aggregation with `LAST_VALUE` over the whole bucket, and the time constraint moved back by one period); MetricFlow itself was not run.

<a id="offsets"></a>**Offsets.** A derived metric's input read at an earlier period ([offsets](semantic-model.md)) is MetricFlow's `offset_window` on that input (DEC-36). MetricFlow reads `offset_window` as `<count> <granularity>`, singular or plural, in `day`, `week`, `month`, `quarter`, `year` or a finer or custom granularity, and shifts `metric_time`, which for each measure is its model's `agg_time_dimension` (the model's first time dimension in the export), joining a time spine with an inner join. When the query's grain is not coarser than the window's unit, MetricFlow aggregates first and then joins each period of the spine to the period the window earlier, which is the engine's rule: the input's value for the period that starts the offset earlier. When the grain is coarser, MetricFlow shifts each day of the spine before aggregating, and a month shift clamps month ends (31 May a month back is 30 April, so by quarter the earlier quarter loses its 31st days and counts some last days of February several times). The export therefore writes each offset in the largest unit that holds it a whole number of times (`3 months` as `1 quarter`, `12 months` as `1 year`, `7 days` as `1 week`), so that every grain the engine accepts for it (month, quarter in multiples of 3 months and year in multiples of 12; day, and week in multiples of 7 days) is one where MetricFlow shifts whole periods. This describes MetricFlow 0.210 and later; earlier versions shift each day of the time spine before aggregating at every grain (0.209 shifts whole periods only when the grain equals the window's unit), so month offsets differ at month ends there even by month. Each exported offset is a warning that names `metric_time` and these conditions:

- **Where they match:** queried by that time dimension at a grain the engine accepts, alone or with other dimensions, every period both return has the same value; a growth over a shifted metric (`revenue_mom_growth`) too.
- **Periods without facts:** the engine returns a row only for a period with facts, for any requested metric, so the month after the last month of data does not appear. MetricFlow joins a time spine and combines a derived metric's inputs with a full outer join, so it also returns periods without facts whose earlier period has some (with NULL for the unshifted inputs). A period whose earlier period has no facts is NULL in both.
- **Other grains:** the engine refuses a grain the offset does not fit (`1 month` by day or by week, `7 days` by month); MetricFlow returns a value, shifting each day (a month offset by day clamps month ends).
- **Time filters:** the engine filters the facts before the shift, so with a filter from 1 March, March's `revenue_prev_month` is NULL. MetricFlow applies a filter that names only `metric_time`, and its time constraint (`--start-time`/`--end-time`), to the time spine after the shift, so March still reads February. Filters on other dimensions cut the facts first in both.
- **Another time dimension:** the engine shifts by whichever time dimension the query names; MetricFlow needs `metric_time` (or the measure's `agg_time_dimension`) in an offset query.
- **Facts without a date:** the engine returns a row with a null period and NULL for the shifted input; MetricFlow drops them with the time spine join.

`cosmos import dbt` maps `offset_window` back: `N days` and `N months` as written, `N weeks` as `7N days`, `N quarters` as `3N months` and `N years` as `12N months`. Each imported offset is a warning with the grains to query it by and the differences above; an `offset_window` in months or quarters that the engine accepts at a coarser grain (`3 months` by quarter, `4 quarters` by year) adds that MetricFlow shifts each day there, so the numbers at that grain differ. MetricFlow writes a growth as one derived metric that reads an input both unshifted and, with an `alias`, shifted (`revenue - revenue_7d`); the import still turns the shifted read into a helper derived metric named `<metric>__<alias>` that the expression reads, with the same numbers (reading it as a Cosmos input `alias`, DEC-44, is a later slice). Skipped with a warning: `offset_to_grain` (the earlier grain's period to date, which the engine does not compute), an `offset_window` in another unit (`hour`, a custom granularity such as a fiscal quarter) or of zero, and an offset over a cumulative metric or over a metric with offsets of its own (the engine does not shift those), together with the metrics that use a skipped one.

Tests export the e-commerce example with both flags and import it back with `cosmos import dbt --schema public`, which reports only the rolling windows' and the offsets' conditions. Models, tables, owners, entities, dimensions, measures and metrics other than the running maximum come back the same, running totals and offsets included, except the `1e0 *` factors and filters that name their model. The re-imported model answers the 43 reference queries that need no business rule, running maximum or user policy with the numbers of the hand-written SQL, the running totals A08 and A09, the date-filtered running totals A59, A61 and A62, the rolling windows A29, A30 and A34 and the offsets A35, A36 and A37 and the aliased offsets A49 to A51 among them; with the original row filters, user context and sensitivity put back, it answers all 68 that need no business rule or running maximum, A33 and the refused A32, A38, A39 and A42 among them. Running totals of `sum` and `count` measures of two models for every period (day to year) come back with the native numbers at every grain the planner accepts, with another dimension, and with a time filter that starts inside a period, and both models refuse the same grains; this shows the definition survives the round trip, not that MetricFlow's runtime agrees at those grains (see the conditions above). Offsets of 1, 7 and 14 days and 1, 3 and 12 months over `sum` and `count` measures of two models, a growth over a shifted metric and a shifted ratio come back as the same offsets with the native numbers at every grain the engine accepts, by another dimension too and with a time filter, and both models refuse the other grains with the same error. A dbt model with `offset_window` in every unit (`1 week`, `2 Months`, `3 months`, `1 quarter`, `1 year`, `1 day`) and an aliased growth imports with the numbers of the same offsets written in Cosmos, and each refused shape (`offset_to_grain`, `1 hour`, `0 days`, a custom granularity, an offset over a shifted or a cumulative metric, and a metric that uses one) is a warning. Rolling windows of 1, 3, 7 and 30 days over `sum`, `count`, `min` and `max` measures of two models come back with the native numbers by day, by day and another dimension, and with a time filter, and both models refuse them by week and month. Windows of 1 and 2 weeks come back with the native numbers by day and with a time filter (the 1-week window with those of the 7-day one), in both the Cube round trip (exported as `trailing: 7 day` and `14 day`, back as `7 days` and `14 days`) and the dbt one (exported and back as `1 week` and `2 weeks`). Every measure and metric of an extended example (filtered metrics of every aggregation, a filter on another model's dimension, a ratio with a filter, nested division) returns the native numbers. As with Cube, the checks run through the Cosmos importer and planner on DuckDB and every database configured in CI, not through MetricFlow.

## Exporting Databricks metric views

`cosmos export databricks -m semantic --catalog main -o metric_views.yml` writes the model as [Unity Catalog metric view](https://docs.databricks.com/aws/en/metric-views/) YAML, `version: 0.1` (DEC-42, first slice). There is no import back yet. The layout is **one metric view per model that has measures**, sourced on the model's table: the measures of a model can only be aggregated over its own rows, and the joins hang off that table. The documents are separated by `---`, each after a comment naming its model; create each one with

```sql
CREATE VIEW main.analytics.orders_metrics WITH METRICS LANGUAGE YAML AS $$
version: 0.1
source: main.public.orders
...
$$
```

and query it with `SELECT status, MEASURE(revenue) FROM main.analytics.orders_metrics GROUP BY ALL`.

| Authoring format | In the metric view |
| --- | --- |
| Model with measures | A metric view with `source` = the model's `table`; `--catalog` is written before a `schema.table` name, otherwise Databricks resolves the name where the view is created (a warning says so) |
| Many-to-one join one step away (inferred from entities) | A `joins` entry named after the target model, with `source` = its table and `on: source.<key> = <model>.<key>`; Databricks joins it LEFT OUTER, as the engine does |
| Dimensions | `dimensions` with `name` and `expr`; a joined model's dimension is `<model>_<dimension>` with `expr: <model>.<column>`. Time dimensions are written raw: truncate them in the query (`DATE_TRUNC('MONTH', ordered_at)`) for the engine's `__month` grains |
| Measures | `measures` with the aggregate expression the planner builds: `SUM(source.amount)`, `COUNT(*)` for a `count` without `expr`, `COUNT(DISTINCT source.x)`, and `AVG(CAST(source.x AS DOUBLE))`. Each bare column of the model is written `source.<column>` (in its dimensions too), so it cannot be confused with a joined model's column of the same name, such as `customer_id` in `orders` and `customers` |
| Simple metric with a filter | The filter inside the aggregation, as the engine applies it: `SUM(CASE WHEN source.status = 'completed' THEN source.amount END)` (a group without matching rows is kept, with NULL for a sum and 0 for a count) |
| Ratio metric | `CAST(num AS DOUBLE) / NULLIF(den, 0)`, with the ratio's filter applied inside both sides |
| Derived metric | Its expression over the inputs' aggregate expressions, each division real and null on a zero denominator |

Metrics are written **inline**, as one aggregate expression each, rather than with `MEASURE()` composition, so a measure does not depend on how a metric view version resolves references between measures.

**What cannot be exported fails closed**, as in the Cube and dbt exports. A model with row filters or `sensitivity` is refused, exit code 1, naming them, until `--allow-policy-loss` is given: **governance stays in Cosmos**, so the metric views have neither the row-level security nor the personal-data marking (and a personal dimension is exported as any other), and each one dropped is a warning. Metrics a metric view cannot carry here are refused until `--allow-metric-loss` is given, each with its reason as a warning: running totals and rolling windows (cumulative metrics), derived metrics with offsets, metrics spanning models (the engine aggregates each model at its own grain; a metric view aggregates one source), metrics filtered by another model's dimension, a time grain, a business rule (under any spelling: `{rule.order_size_band}`, `{order_size_band}` or `{orders.order_size_band}`, since the planner reads the version in effect on `rules_as_of` with the clock at `as_of`) or `$user`, derived metrics over bare columns, and metrics that use one of these. A filter that reads the clock (`CURRENT_DATE`, `NOW()`) is exported with a warning: in the metric view it follows the warehouse's clock, not the query's `as_of`. Warnings also name a joined model's dimension whose name clashes with one of the model's own (it is skipped), the dimensions reached through more than one join (no nested joins in this slice), a joined model reached by two entities, descriptions, labels and owners (not written yet), `user_context`, the time zone, business rules, the glossary, teams, systems, processes and `custom_extensions`. Metric filters (`metric_filters`) belong to a query, not to the model, so there is nothing to export.

Tests check the YAML's structure and refusals, and evaluate every exported measure as a metric view does (the source LEFT JOINed to its joins, grouped by a dimension) on DuckDB: 57 measure-and-grouping pairs (in total, by the model's own dimension and by a joined one) return the planner's numbers. With Databricks credentials (`DATABRICKS_HTTP_PATH` with `DATABRICKS_TOKEN` or `DATABRICKS_CONFIG_PROFILE`), the same test creates the views on the warehouse, queries them with `MEASURE()` and drops them; it is skipped otherwise.

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
