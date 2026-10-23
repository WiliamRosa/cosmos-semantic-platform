# ADR 0002: Authoring format inspired by the dbt Semantic Layer

- **Status:** accepted (revised on 2026-10-15 after [ADR 0004](0004-own-semantic-engine.md); extended on 2026-10-22 by [ADR 0007](0007-connectors-sources-identity-and-import.md))
- **Date:** 2026-10-14

## Context

The dbt Semantic Layer, with the MetricFlow engine, uses entities to infer joins and has explicit metric types, which makes modeling simpler and less error-prone than declaring joins and compound metrics by hand, as in Cube.

MetricFlow has been Apache 2.0 since version 0.209.0, but today it works as a library coupled to a dbt project and a dbt adapter ([repository](https://github.com/dbt-labs/metricflow)).

## Decision

Create our own authoring format, with the dbt Semantic Layer concepts (entities, metric types), feeding the product's **own semantic engine**. MetricFlow serves as a **design reference**, not a dependency. The same model can be exported to Cube YAML, dbt YAML and Ossie. Specification in [semantic model](../semantic-model.md).

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| Adopt Cube's YAML as the format | Ties the product to another vendor's model and has no entities or metric types |
| Use MetricFlow as the engine | Requires a dbt project and a dbt adapter to work |
| Use Ossie as the authoring format | The standard is still a draft and has no entities or metric types; it works better as an interchange format |

## Consequences

- The format needs its own versioning, to evolve without breaking existing projects.
- Each export (Cube, dbt, Ossie) needs tests comparing results with our own engine.
- Concepts without a direct equivalent in an export target produce a warning on export, never a silent loss.
