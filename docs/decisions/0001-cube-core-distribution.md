# ADR 0001: Distribute Cube Core, without forking

- **Status:** superseded by [ADR 0004](0004-own-semantic-engine.md) on 2026-10-15
- **Date:** 2026-10-14

> This decision was reversed: the engine became our own, and Cube became an export target. The text below is kept as a historical record.

## Context

The product aims to offer a universal semantic layer comparable to Cube. There were two paths:

| Path | Description |
| --- | --- |
| A. Distribute Cube Core | Use Cube Core as the engine and build our own value on top |
| B. Own engine | Write our own compiler, connectors, cache and SQL endpoint, using open source libraries |

Cube Core's backend is Apache 2.0 and its client is MIT ([repository](https://github.com/cube-js/cube)). It already ships a REST API, GraphQL, a Postgres-compatible SQL endpoint, caching, pre-aggregations and about 30 official data sources ([list](https://docs.cube.dev/admin/connect-to-data/data-sources)).

## Decision

Take path A: Cube Core is a pinned-version dependency, run from the official images (`cubejs/cube` and `cubejs/cubestore`). Cube's code is neither copied nor modified. Our own value lives in:

- the authoring format and compiler ([ADR 0002](0002-dbt-inspired-authoring-format.md));
- Ossie interoperability ([ADR 0003](0003-ossie-as-interchange-format.md));
- the MCP server, control plane and managed hosting.

## Consequences

**Positive**

- A working product in weeks, not years.
- Inherit data sources the dbt Semantic Layer does not support, such as SQL Server, Oracle, MySQL and Microsoft Fabric.
- Upgrade the engine by pulling new versions, without merge conflicts.

**Negative**

- The engine is the same as Cube's, so performance is not a differentiator.
- Features exclusive to Cube Cloud stay out, such as the DAX/XMLA connection to Power BI.
- The Cube project's pace and decisions affect the product.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Cube changes its license in future versions, as Elastic and HashiCorp did | Pinned version; the canonical model is the authoring format and Ossie, not Cube's YAML, so the engine can be replaced |
| Trademark misuse | Do not use "Cube" in the product name; mention Cube Core only as a component, with the notices Apache 2.0 requires |
| A Cube upgrade breaks the generated model | Regression suite before each upgrade |
| A needed feature exists only in Cube Cloud | Build it in our own layer, or accept and document the gap |

## Apache 2.0 license obligations

- Keep Cube's copyright and license notices in any redistribution.
- Include a third-party notices file in the product distribution.
- State any modifications to Cube files; with the decision not to modify them, this should not happen.
