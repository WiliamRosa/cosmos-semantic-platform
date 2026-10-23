# Contributing to Cosmos Semantic

Thank you for helping. Cosmos Semantic is open core ([ADR 0005](docs/decisions/0005-open-core.md)): everything that defines or computes a number lives in this repository under the Apache 2.0 license.

## Developer Certificate of Origin

Contributions use the [Developer Certificate of Origin](https://developercertificate.org/) (DCO), not a copyright assignment. Sign off every commit to certify that you wrote the change or have the right to submit it under the project's license:

```bash
git commit -s -m "feat: describe the change"
```

This adds a `Signed-off-by: Your Name <you@example.com>` line. Pull requests with unsigned commits cannot be merged.

## Development setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker (for Postgres and SQL Server).

```bash
uv sync --all-extras
docker compose -p cosmos-semantic -f deploy/docker-compose.yml --profile sqlserver up -d

export COSMOS_PG_URI=postgresql://cosmos:cosmos@localhost:15432/cosmos
export COSMOS_MSSQL_URI='mssql+pymssql://sa:Cosmos!Passw0rd@localhost:14330/master'
uv run pytest
```

Without the two variables, the database-specific tests are skipped and only DuckDB runs. The Docker credentials are for local tests only.

## What every change needs

- **Same number everywhere.** Any change to the planner, connectors or SQL generation must keep the reference queries (`reference/`), the divergence zoo and the randomized cross-database test passing on DuckDB, Postgres and SQL Server. A new feature that computes numbers needs a reference case with hand-written SQL.
- **Formatting and lint:** `uv run ruff format src tests` and `uv run ruff check src tests`.
- **Schema:** after changing `src/cosmos_semantic/spec.py`, regenerate `schema/cosmos-semantic.v1.schema.json` with `uv run cosmos schema -o schema/cosmos-semantic.v1.schema.json`.
- **English** in code, comments, messages, tests and docs. Portuguese stays only where it is data (sample values, `pt` glossary labels).
- **Decisions:** a change to architecture or scope gets an ADR in `docs/decisions/`.

## Reporting security issues

Do not open a public issue for vulnerabilities; see [SECURITY.md](SECURITY.md).
