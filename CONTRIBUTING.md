# Contributing to Cosmos Semantic

Thank you for helping. Cosmos Semantic is open core ([ADR 0005](docs/decisions/0005-open-core.md)): everything that defines or computes a number lives in this repository under the Apache 2.0 license.

## Where to start

- Issues labelled [`good first issue`](https://github.com/WiliamRosa/cosmos-semantic-platform/labels/good%20first%20issue) are small and self-contained, and each one says which files to touch and how to test it. Comment on the issue before you start, so two people do not work on the same one.
- Trying Cosmos Semantic on a real model and reporting where the numbers or the import disagree with your current tool is one of the most useful contributions. The [bug report](https://github.com/WiliamRosa/cosmos-semantic-platform/issues/new?template=bug_report.yml) form asks for what is needed to reproduce it.
- A change to the authoring format, the planner or a public interface starts with an issue to agree on the design before the code.

## Developer Certificate of Origin

Contributions use the [Developer Certificate of Origin](https://developercertificate.org/) (DCO), not a copyright assignment. Sign off every commit to certify that you wrote the change or have the right to submit it under the project's license:

```bash
git commit -s -m "feat: describe the change"
```

This adds a `Signed-off-by: Your Name <you@example.com>` line. The `dco` check on every pull request fails when a commit lacks it; `git rebase --signoff main` adds the line to every commit of a branch, then force-push the branch.

## Development setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker (for Postgres and SQL Server).

```bash
uv sync --all-extras
docker compose -p cosmos-semantic -f deploy/docker-compose.yml \
  --profile sqlserver --profile mysql --profile mariadb --profile oracle up -d
# once the Oracle container is healthy:
docker exec -i cosmos-semantic-oracle-1 sqlplus -s system/cosmos@FREEPDB1 < deploy/oracle-setup.sql

export COSMOS_PG_URI=postgresql://cosmos:cosmos@localhost:15432/cosmos
export COSMOS_MSSQL_URI='mssql+pymssql://sa:Cosmos!Passw0rd@localhost:14330/master'
export COSMOS_MYSQL_URI=mysql+pymysql://root:cosmos@127.0.0.1:13306/
export COSMOS_MARIADB_URI=mysql+pymysql://root:cosmos@127.0.0.1:13307/
export COSMOS_ORACLE_DSN=127.0.0.1:11521/FREEPDB1 COSMOS_ORACLE_PASSWORD=cosmos
uv run pytest
```

Without these variables, the database-specific tests are skipped and only DuckDB runs; CI runs Postgres, SQL Server, MySQL, MariaDB and Oracle on every pull request. The Docker credentials are for local tests only.

Databricks is optional and needs a SQL warehouse: set `DATABRICKS_HTTP_PATH`, plus `DATABRICKS_CONFIG_PROFILE` (after `databricks auth login --profile <name>`) or `DATABRICKS_TOKEN` with `DATABRICKS_SERVER_HOSTNAME`. The tests load the example data into the catalog in `DATABRICKS_CATALOG` (default `cosmos_demo`, which must exist). On Git Bash for Windows, also set `MSYS_NO_PATHCONV=1`.

Snowflake is optional too: run [deploy/snowflake-setup.sql](deploy/snowflake-setup.sql) once with your public key, then set `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_FILE`, `SNOWFLAKE_ROLE` and `SNOWFLAKE_WAREHOUSE` (or `SNOWFLAKE_CONNECTION_NAME`). The tests load the example data into `SNOWFLAKE_DATABASE` (default `COSMOS_DEMO`).

## What every change needs

- **Same number everywhere.** Any change to the planner, connectors or SQL generation must keep the reference queries (`reference/`), the divergence zoo and the randomized cross-database test passing on DuckDB, Postgres, SQL Server, MySQL, MariaDB and Oracle. A new feature that computes numbers needs a reference case with hand-written SQL.
- **Formatting and lint:** `uv run ruff format src tests` and `uv run ruff check src tests`.
- **Schema:** after changing `src/cosmos_semantic/spec.py`, regenerate `schema/cosmos-semantic.v1.schema.json` with `uv run cosmos schema -o schema/cosmos-semantic.v1.schema.json`.
- **English** in code, comments, messages, tests and docs. Portuguese stays only where it is data (sample values, `pt` glossary labels).
- **Decisions:** a change to architecture or scope gets an ADR in `docs/decisions/`.

## Reporting security issues

Do not open a public issue for vulnerabilities; see [SECURITY.md](SECURITY.md).
