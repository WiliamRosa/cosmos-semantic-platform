# ADR 0010: Snowflake as a data source

- **Status:** accepted
- **Date:** 2026-11-04

## Context

Cosmos Semantic runs on DuckDB, Postgres, SQL Server and Databricks ([ADR 0007](0007-connectors-sources-identity-and-import.md)). Snowflake is the other cloud warehouse that most data teams evaluating a semantic layer already use. Its SQL differs from the other four in ways that change numbers: unquoted names are folded to upper case, integer division returns a decimal, and the first day of the week and the week number depend on session parameters.

## Decision

| Question | Decision |
| --- | --- |
| Library | The official `snowflake-connector-python`, behind the `snowflake` extra. It fetches results as Arrow batches, as the `Connector` interface requires. The Snowflake ADBC driver was not chosen: it is a Go library loaded from a DLL, like the Postgres driver, and adds no capability the official connector lacks |
| Authentication | Whatever the official connector accepts in `options`: a key pair (`private_key_file`), a named connection from `~/.snowflake/connections.toml` (`connection_name`), an `authenticator` such as `externalbrowser`, or a password. Tests and services use a key pair on a `TYPE = SERVICE` user, which has no password and no MFA prompt |
| Session | Every session sets `WEEK_START = 1` and `WEEK_OF_YEAR_POLICY = 1` (weeks start on Monday, ISO week numbers, as on the other databases) and `STATEMENT_TIMEOUT_IN_SECONDS` from the source's `timeout_seconds`. `options.session_parameters` adds or overrides parameters |
| Names | The planner writes lower-case unquoted names and aliases. Snowflake returns them in upper case, so the connector folds an all-upper-case result column back to lower case; `cosmos bootstrap` does the same with catalog names, and reads `NUMBER(p, 0)` as an integer |
| SQL | The SQLGlot `snowflake` dialect. Reference queries written in PostgreSQL are translated to Snowflake, as they are for Databricks, unless a case spells a Snowflake version |
| Validation | The same gate as Databricks: the whole suite (reference queries, the divergence zoo, the randomized cross-database test, evidence, bootstrap, packs, Power BI import, rule versions) passes on a Snowflake warehouse |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| ADBC Snowflake driver | One more native library to load on Windows from paths with accents (see the Postgres connector), with the same Arrow results |
| SQLAlchemy (`snowflake-sqlalchemy`) | Converts results row by row in Python, the path ADR 0007 keeps only as a fallback |
| Quote every identifier in the generated SQL | Would make lower-case names case-sensitive in Snowflake and break models over tables created without quotes, which is the usual case |

## Consequences

- Five databases in the comparative suites. The Snowflake tests run when `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_USER` (with `SNOWFLAKE_PRIVATE_KEY_FILE`), or `SNOWFLAKE_CONNECTION_NAME`, are set; CI does not run them, as it does not run Databricks.
- `deploy/snowflake-setup.sql` creates a warehouse, database, role and service user for the tests with the least privilege they need.
- A model over tables created with quoted mixed-case names in Snowflake must write those names in the model's case; the folding applies only to names in upper case.
