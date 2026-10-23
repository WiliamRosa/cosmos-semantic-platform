# ADR 0006: Ontology in the MVP, with a SKOS glossary and business rules

- **Status:** accepted
- **Date:** 2026-10-16

## Context

AI agents need business context, not only metrics. A user asks about "active buyers" ("compradores ativos"), and the agent must reach a defined rule and a correct SQL filter. The market is moving in this direction:

| Reference | What it shows |
| --- | --- |
| Genie Ontology and Genie "SQL expressions" (Databricks) | Filters, measures and calculated fields with business meaning, but tied to a Genie agent |
| Microsoft Fabric IQ | An ontology over OneLake and Power BI, with the ontology item still in preview |
| Palantir Foundry Ontology | Objects, links and business functions as part of the ontology, for large enterprises |
| DMN (OMG standard) | Decision tables readable by business people |

No independent semantic layer treats business rules as objects with an owner, a version and tests, linked to a glossary. The ontology was chosen as the differentiator that convinces users to adopt the product, so it is part of the MVP.

## Decision

The MVP adds two elements to the semantic model. Specification in [semantic model](../semantic-model.md#glossary-skos).

1. **A SKOS-style glossary:** concepts with a preferred label, synonyms, a definition and a hierarchy ("is a kind of"), with labels in more than one language. It is not a formal OWL ontology, which would be too heavy for customers to maintain.
2. **Business rules** (`business_rules`) of three kinds: `predicate` (true or false), `classification` (a category, including DMN-style decision tables) and `derived` (a computed value). Each rule has an owner, an effective date and tests, and is bound to models by column mapping.

The MCP server gains tools to find concepts by synonym and to explain a rule in natural language.

## Accepted limits

- **Row-level rules.** A rule only uses columns and dimensions of the row. Classifications that depend on aggregates (such as revenue over the last 12 months) need a precomputed column or summary model.
- **The current definition applies.** `effective_from` records since when the rule applies and history lives in git, but queries always use the current version. Querying with an older version comes later.
- **No logical inference.** The glossary is navigable (synonyms, hierarchy), but the engine does no automatic reasoning over it.

## Consequences

- The ontology is the phase 1 differentiator. LGPD (personal data) classification stays in the MVP as basic security.
- Phase 1 grows from 8–10 to 11–13 weeks.
- Rule tests run in CI without touching the data source: SQLGlot translates the logic to DuckDB, which evaluates the examples locally.
- The glossary and rules stay in the open source core, because they define what a number means ([ADR 0005](0005-open-core.md)).
