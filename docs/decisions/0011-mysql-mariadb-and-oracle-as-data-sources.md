# ADR 0011: MySQL, MariaDB and Oracle as data sources

- **Status:** accepted
- **Date:** 2026-11-04

## Context

Cosmos Semantic runs on DuckDB, Postgres, SQL Server, Databricks and Snowflake ([ADR 0007](0007-connectors-sources-identity-and-import.md), [ADR 0010](0010-snowflake-as-a-data-source.md)). Many operational systems that teams want to reconcile against keep their data in MySQL, MariaDB or Oracle. These three differ from the others in ways that change numbers or stop a query from running:

- MySQL and MariaDB have no `DATE_TRUNC`. The SQLGlot translation counts weeks from year 0, so its weeks do not start on Monday. `||` is a logical OR unless `PIPES_AS_CONCAT` is set.
- Oracle has no `IS [NOT] DISTINCT FROM` and no `DATE_ADD`. It rejects `GROUP BY 1` unless a session parameter is set. Its `CURRENT_DATE` carries the time of day. `DOUBLE PRECISION` is a decimal `NUMBER`. `public` is a reserved word, and a schema is a user. Unquoted names are folded to upper case, as in Snowflake.

## Decision

| Question | Decision |
| --- | --- |
| MySQL and MariaDB library | SQLAlchemy with PyMySQL, behind the `mysql` extra; both types share it. Neither database has a mature ADBC driver, so this is the fallback path of ADR 0007. Each connection sets `PIPES_AS_CONCAT` and the statement timeout from `timeout_seconds` (`max_execution_time` in MySQL, `max_statement_time` in MariaDB) |
| Oracle library | The official `python-oracledb` in thin mode, behind the `oracle` extra: pure Python, no Instant Client to install. Each session sets the NLS date, timestamp and number formats and binary sorting and comparison, so literals and order match the other databases. `call_timeout` comes from `timeout_seconds` |
| SQL dialect | SQLGlot's `mysql` dialect for both MySQL and MariaDB (SQLGlot has no MariaDB dialect), and `oracle` for Oracle. A new module, `dialects.py`, rewrites what the generic translation gets wrong before the SQL is printed. Time grains use native functions (`WEEKDAY` and `MAKEDATE` in MySQL; `TRUNC(x, 'IW' / 'MM' / 'Q' / 'YYYY')` in Oracle), and every grain returns a timestamp. In Oracle, null-safe equality becomes `DECODE(a, b, 1, 0) = 1`, date arithmetic uses `NUMTODSINTERVAL`/`NUMTOYMINTERVAL`, `CURRENT_DATE` becomes `TRUNC(CURRENT_DATE)`, and `GROUP BY` positions are expanded |
| Names | The Oracle connector folds an all-upper-case result column back to lower case, as the Snowflake one does. `cosmos bootstrap` reads Oracle's catalog from `ALL_TAB_COLUMNS` and `ALL_CONSTRAINTS`, and reads `NUMBER(p, 0)` as an integer |
| Schemas | In MySQL and MariaDB a schema is a database, so `public.orders` works as written. In Oracle the source maps `public` to the owning user with `schema_map` (`public: cosmos`) |
| Demo data | `float64` columns are loaded as `BINARY_DOUBLE` in Oracle. `DOUBLE PRECISION` there is decimal, so sums of equal values grouped differently than on the other databases |
| Planner | The internal bridge alias `__bridge_key` becomes `cosmos_bridge_key`, because Oracle rejects unquoted names that start with `_` |
| Validation | The whole suite passes on MySQL 8.4, MariaDB 11.4 and Oracle Database 23ai Free, all three in Docker and in CI. Oracle Autonomous Database is not tested for now. The connector passes wallet options (`config_dir`, `wallet_location`, `wallet_password`) through to `oracledb.connect`, but nothing exercises them |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| `mysqlclient` or MySQL Connector/Python | `mysqlclient` needs a C compiler or prebuilt wheels per platform; PyMySQL is pure Python and works the same on MariaDB |
| Oracle thick mode (Instant Client) | One more native install per machine, with no capability the suite needs |
| Setting Oracle's `group_by_position_enabled` | A 23ai parameter; older versions and Autonomous Database may refuse it, while expanding the positions works everywhere |
| Fixing each construct in SQLGlot upstream | Still worth reporting. But the engine needs the fix now, and the week semantics (Monday start) are a product choice, not a dialect bug |

## Consequences

- Eight databases in the comparative suites: DuckDB, Postgres, SQL Server, MySQL, MariaDB and Oracle run in CI; Databricks and Snowflake run where their credentials are set.
- `deploy/docker-compose.yml` has `mysql`, `mariadb` and `oracle` profiles. `deploy/oracle-setup.sql` creates the second schema (`SHOP`) that the pack tests use, and grants the test user the rights to load it.
- Hand-written SQL that is meant to fail or differ, as in the divergence zoo, needs a MySQL and an Oracle version wherever PostgreSQL syntax does not run there.
