# Monday 2026-11-23: what's new

What merged since the launch week, in user terms.

## Post

What's new in Cosmos Semantic since launch week:

1. Two more data sources, in preview: Google BigQuery and Trino / Starburst. They join Databricks, Snowflake, Oracle, SQL Server, PostgreSQL, MySQL, MariaDB and DuckDB, and the same model is checked against them in CI.

2. Growth from a single metric. A derived metric can read the same input shifted and unshifted, so year-over-year growth is one definition, with no helper metric:
(revenue - revenue_year_before) / revenue_year_before

3. Offsets in weeks and years. "offset: 1 year" or "2 weeks" produces exactly the SQL of 12 months or 14 days, and the evidence keeps the offset as you wrote it.

4. A query explorer in the console. Pick a metric, dimensions, filters and business rules, then see the result, the generated SQL and the evidence. Row filters apply and every run is audited.

Every change keeps the same promise: the same number everywhere, with the proof in the answer.

Live demo: https://wiliamrosa.github.io/cosmos-semantic-platform/
Changelog: https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/CHANGELOG.md

#SemanticLayer #AIAgents #DataEngineering #OpenSource #AnalyticsEngineering

## Sources

- BigQuery in preview (DEC-40, PR #100) and Trino / Starburst in preview (DEC-43, PR #104): [CHANGELOG](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/CHANGELOG.md) `[Unreleased]`, "Added"; [README](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/README.md) "Multi-database" row. The eight databases: same row.
- "Checked against them in CI": CHANGELOG, Trino entry ("CI runs the suites against a Trino container") and BigQuery entry (BigQuery suites).
- Growth in one metric (DEC-44, PR #105): CHANGELOG, "Growth in one metric"; example metric `revenue_yoy_growth` in `examples/ecommerce/semantic/sales.yml`.
- Offsets in weeks and years (DEC-46, PR #102): CHANGELOG, "Offsets in weeks and years" (same SQL and `sql_hash` as `14 days` or `12 months`; evidence keeps the offset as written).
- Query explorer (DEC-41, PR #90): CHANGELOG, "A query explorer in the console" (row filters apply, every run audited).

## Publish

- **Suggested time:** Monday 2026-11-23, 09:00 São Paulo.
- **Attach:** `card.png` (1080×1350). If it is missing, render `card.html` with `python marketing/linkedin/render.py marketing/linkedin/posts/2026-11-23-whats-new`.
- After publishing, change this section to start with **Published:**.
