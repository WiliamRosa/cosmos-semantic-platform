# ADR 0003: Apache Ossie as the interchange format

- **Status:** accepted
- **Date:** 2026-10-14

## Context

The product must follow the market's open standard, so users are not locked in and the product can talk to other tools. Open Semantic Interchange (OSI), launched in 2025, became Apache Ossie, incubating at the Apache Software Foundation with dbt Labs, Snowflake, Cube and AtScale among the participants. The specification is a draft (`0.2.0.dev0`) and has an extension mechanism (`custom_extensions`).

## Decision

Ossie is the product's **import and export format**, not its authoring format. Every model can be exported to Ossie and re-imported without loss, using `custom_extensions` for whatever the standard does not cover. Details in [interoperability](../interoperability.md).

## Consequences

- The compiler has an isolated mapping module, tested per Ossie schema version.
- The round-trip test (export, re-import, compare) is part of CI.
- When the standard stabilizes and covers entities and metric types, reassess whether it can also become the authoring format.
