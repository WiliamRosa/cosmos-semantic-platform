## What and why

<!-- What this changes and the problem it solves. Link the issue: "Closes #123". -->

## How it was tested

<!-- Commands you ran and on which databases. Without COSMOS_PG_URI / COSMOS_MSSQL_URI only DuckDB runs; CI runs Postgres and SQL Server too. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`), as [CONTRIBUTING.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/CONTRIBUTING.md#developer-certificate-of-origin) explains.
- [ ] `uv run ruff format src tests` and `uv run ruff check src tests` pass.
- [ ] Tests cover the change; a change that computes numbers keeps the reference queries, the divergence zoo and the cross-database test passing.
- [ ] Docs updated where behavior changed; `schema/` regenerated if `spec.py` changed.
- [ ] A line under `## [Unreleased]` in `CHANGELOG.md` for user-visible changes.
