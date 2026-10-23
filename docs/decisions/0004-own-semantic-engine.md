# ADR 0004: Own semantic engine, with Cube as an export target

- **Status:** accepted
- **Date:** 2026-10-15
- **Supersedes:** [ADR 0001](0001-cube-core-distribution.md)

## Context

ADR 0001 chose to distribute Cube Core and build value on top of it. Detailing the solution revealed two problems:

1. **The product lived in Cube's shadow.** In a semantic layer, the engine that turns a metric request into SQL is the core of the product. Building on Cube meant competing with Cube using Cube's core, and any differentiator on top (such as dbt-style entities and metric types) could be copied by them in a release.
2. **The stack became complex.** Our own format, a compiler to Cube's YAML, the Cube API, the refresh worker, Cube Store and our own layers: too much for one person to operate, and part of it was not ours.

## Decision

Invert the relationship: **the semantic engine is our own, and Cube becomes one of the export targets**, alongside dbt and Apache Ossie.

- The core belongs to the product: semantic model, metric planner with join inference, and per-dialect SQL generation.
- Queries run directly on the data source, with no intermediate infrastructure in the MVP.
- The first interfaces are MCP and REST, aimed at AI agents and applications.
- Whoever needs caching, pre-aggregations or BI tool connectivity can export the model to Cube.

## Open source building blocks

| Component | License | Use |
| --- | --- | --- |
| [SQLGlot](https://github.com/tobymao/sqlglot) | MIT | Build and translate SQL for each dialect (SQL Server, Oracle, Postgres, Databricks, Snowflake and others) |
| [MetricFlow](https://github.com/dbt-labs/metricflow) | Apache 2.0 (since 0.209.0) | Design reference for metric and join planning; not a dependency |
| [Cube](https://github.com/cube-js/cube) | Apache 2.0 | Export target format; not a runtime dependency |

## Consequences

**Positive**

- The core and the intellectual property belong to the product.
- The MVP architecture is smaller: compiler, planner, connectors, REST and MCP.
- The product becomes compatible with Cube instead of dependent on it.

**Negative**

- No caching or pre-aggregations in the MVP: every query goes to the source.
- No Postgres-compatible SQL endpoint in the MVP, so Power BI and Tableau come later or through export to Cube.
- The correctness of the generated SQL becomes the product's responsibility, including hard cases such as row multiplication in joins (fan-out).

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Wrong SQL in joins across many tables | Aggregate each measure at its own grain before joining, as MetricFlow does; reference query suite compared with hand-written SQL |
| Differences between dialects | SQLGlot and tests per supported source |
| Performance on large data without a cache | Push aggregation to the source, cap returned rows, result cache in phase 3 |
| Scope growing again | One differentiator per phase, beyond the core |
