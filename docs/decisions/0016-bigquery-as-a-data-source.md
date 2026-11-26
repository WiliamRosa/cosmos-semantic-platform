# ADR 0016: BigQuery as a data source

- **Status:** accepted
- **Date:** 2026-11-21

## Context

Cosmos Semantic runs on DuckDB, Postgres, SQL Server, MySQL, MariaDB, Oracle, Databricks and Snowflake ([ADR 0007](0007-connectors-sources-identity-and-import.md), [0010](0010-snowflake-as-a-data-source.md), [0011](0011-mysql-mariadb-and-oracle-as-data-sources.md)). BigQuery is the third large cloud warehouse, and teams on Google Cloud could not use Cosmos. Its SQL differs from the others in ways that change numbers:

- `WEEK` truncation starts on Sunday.
- A time without a time zone is a `DATETIME`, while `TIMESTAMP` is an instant in UTC.
- `DATE_ADD` takes only a `DATE`.
- A schema is a dataset, and each dataset has its own `INFORMATION_SCHEMA`.

The proposal is DEC-40 in [daily #83](https://github.com/WiliamRosa/cosmos-semantic-platform/issues/83).

## Decision

| Question | Decision |
| --- | --- |
| Library | The official `google-cloud-bigquery` client, behind the `bigquery` extra. Results come back as Arrow (`to_arrow`) without the BigQuery Storage API, which the demo's result sizes do not need. The `max_rows` limit is passed as `max_results`, so no more rows are fetched than the limit allows |
| Authentication | Application Default Credentials (`gcloud auth application-default login` on a laptop, the attached service account on Google Cloud), or a service account key in `options.credentials_file`. There is no password option. `options.project` is required, and `options.location` is optional |
| Cost guard | `options.maximum_bytes_billed` makes a query that would scan more fail instead of running. Every query job has the source's `timeout_seconds` as its job timeout, and a failed job is not retried |
| Names | The model's schemas are datasets: `schema_map: {public: cosmos_demo}`. Table names stay unquoted; the planner writes them in lower case, and BigQuery table names are case-sensitive, as they are in Postgres |
| SQL | The SQLGlot `bigquery` dialect, which writes `TIMESTAMP` as `DATETIME`, with three rewrites in `dialects.py` (and, since DEC-57, `DATETIME(column, 'UTC')` for a time dimension with `column_type: timestamp`). Weeks truncate to `ISOWEEK`, which starts on Monday. Date arithmetic on a time uses `DATETIME_ADD`, because `DATE_ADD` takes only a `DATE` and no hours. A date read as a time uses `DATETIME(date)`. Reference queries written in PostgreSQL are translated, as for Databricks and Snowflake |
| Demo data | A load job from Parquet, with `DATETIME` for times. Load jobs are free and work in the BigQuery sandbox, where DML such as `INSERT` is refused |
| Catalog | `cosmos bootstrap` reads `` `<dataset>`.INFORMATION_SCHEMA.COLUMNS `` and the dataset's primary keys (BigQuery keys are not enforced, so bootstrap still checks uniqueness, as everywhere else) |
| Validation | Two levels. (1) The [BigQuery emulator](https://github.com/goccy/bigquery-emulator) checks locally that the generated SQL passes Google's SQL analyzer (ZetaSQL) and returns the same numbers as DuckDB. (2) A live run on a real project is required before a change to the BigQuery dialect merges, as for Databricks |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| ADBC BigQuery driver | A Go library loaded from a DLL, the path that fails on Windows paths with accents (see the Postgres connector), and newer than the official client |
| SQLAlchemy (`sqlalchemy-bigquery`) | Converts results row by row in Python, the path ADR 0007 keeps only as a fallback |
| `INSERT` statements for the demo data | Refused in the free sandbox, and BigQuery charges DML by bytes scanned. The emulator, which does not run load jobs, gets them instead |
| Store times as `TIMESTAMP` | `TIMESTAMP` is an instant in UTC; the other databases store civil times, and the model's `timezone` already decides what "today" is |
| The emulator in CI | It answers the reference queries, but it runs rolling windows (range joins) for more than ten minutes each, and it gets some casts wrong (`CAST(DATE AS DATETIME)` returns 1970-01-01). The Power BI import queries make it panic (`googlesqlite: panic runtime error: slice bounds out of range`), so `tests/test_powerbi.py` skips its BigQuery cases when `COSMOS_BIGQUERY_ENDPOINT` is set; they pass on a real project. It stays a local check, not a merge gate |

## Consequences

- Nine databases. The BigQuery tests run when `COSMOS_BIGQUERY_PROJECT` is set:
  - against the emulator with `COSMOS_BIGQUERY_ENDPOINT=http://localhost:19050` (`docker compose --profile bigquery`);
  - against a real project with Application Default Credentials or `COSMOS_BIGQUERY_CREDENTIALS_FILE`.

  CI does not run them, as it does not run Databricks and Snowflake.
- On the emulator, 58 reference queries return the same numbers as DuckDB, the A06 Monday weeks and the A48 window up to noon among them. Rolling windows over a range join (A26) and the Power BI import cases, which the emulator skips, need a real project.
- **Live run (2026-11-21):** 160 tests passed on a BigQuery sandbox project, with 1 skipped. They include the 66 reference queries, the divergence zoo, the randomized same-number test against DuckDB, bootstrap, packs, the Power BI import and the dbt, Cube and Ossie exports. The skipped test is the evidence replay, which changes data with `UPDATE`, and the sandbox refuses DML.
- **What the live run found that the emulator did not:**
  - `DATE - DATE` is an `INTERVAL` in BigQuery; the dialect now writes `DATE_DIFF(..., DAY)`.
  - In hand-written reference SQL, a CTE named like one of its own columns resolves to the whole row; the BigQuery translation renames such a CTE.
  - A model expression with a doubled quote (`'O''Brien'`, standard SQL) did not parse in the BigQuery dialect; it is now read as standard SQL and written with BigQuery's escape.
- **`TIMESTAMP` columns (DEC-57, 2026-11-25):** the demo loads times as `DATETIME`, so the live run never read a `TIMESTAMP` column, and comparing one with a `DATE` or `DATETIME` has no matching signature on BigQuery. A time dimension may now say `column_type: timestamp` (optional; `cosmos bootstrap` sets it on BigQuery `TIMESTAMP` columns). The dialect then reads the column as its date and time in UTC, `DATETIME(column, 'UTC')`, written out so the SQL does not depend on a default time zone, before any truncation, comparison or date arithmetic, and in business rules bound to it. From there on it is the `DATETIME` path the live run checked. `DATETIME` and `DATE` columns, and every other database, keep their SQL and `sql_hash`. Two limits: a day is a UTC day, not a day in the model's `timezone`; and a filter on `DATETIME(column, 'UTC')` does not prune a table partitioned on that column, so `maximum_bytes_billed` matters more on large partitioned tables. The checks are offline (`tests/test_bigquery_offline.py`); a live run on a `TIMESTAMP` table is required before merge, as for any dialect change.
- `deploy/bigquery-setup.md` gives the least-privilege setup for a sandbox project: a dataset and a service account with `roles/bigquery.jobUser` on the project and `roles/bigquery.dataEditor` on the dataset only.
