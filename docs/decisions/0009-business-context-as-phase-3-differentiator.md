# ADR 0009: Business context as the phase 3 differentiator

- **Status:** accepted
- **Date:** 2026-10-23
- **Changes:** phases 3 and 4 in the [roadmap](../roadmap.md); builds on [ADR 0006](0006-ontology-in-the-mvp.md) and [ADR 0008](0008-verifiable-numbers-as-phase-2-differentiator.md)

## Context

Phase 2 delivered verifiable numbers: an evidence envelope on every answer, replay, reconciliation, impact analysis and a deterministic model bootstrap, all validated on DuckDB, Postgres and SQL Server.

The roadmap then planned a paid product: a Postgres-compatible SQL endpoint for Power BI, Tableau and Excel, a control plane, and managed hosting on Azure. Those items need infrastructure, credits and customers that are not available yet, and they add little to what makes the engine different.

The radar held candidates that build on the same compiled graph and need no infrastructure:

| Candidate | What already exists |
| --- | --- |
| Queries with past versions of business rules | Rules have `effective_from`; queries pin "today" to `as_of` |
| Cost per metric and per agent | The JSONL audit log records every query, principal, channel and result size |
| Expanded knowledge graph (processes, systems, owners) | The dependency graph used by `cosmos impact`; rule owners |
| Importing Power BI models | Importers for dbt, Ossie and Cube with an equivalence test |
| AI-assisted semantic discovery | `cosmos bootstrap` drafts a model without AI; the glossary search |
| Industry packs | The e-commerce example, now in English by default |

## Decision

**Phases 3 (connected BI) and 4 (managed product) are paused. The new phase 3 differentiator is "business context": the model knows how definitions changed over time, who owns them, which systems and processes they serve, and what they cost to answer.**

Phase 3 delivers, in this order, each validated on DuckDB, Postgres and SQL Server:

1. **Rule versions:** a rule keeps earlier versions in `history`; a query uses the version in effect on its `rules_as_of` date (default `as_of`), and the evidence envelope names the version used.
2. **Usage and cost report** (`cosmos usage`): queries, rows and duration per metric, per principal and per channel (agents included), from the audit log, with an optional cost per query or per second for each data source.
3. **Expanded knowledge graph:** teams, systems and processes declared in the model and linked to metrics, rules and models; impact analysis grouped by owner; graph export.
4. **Power BI import:** tables, relationships and measures from a `.bim` (TMSL) or TMDL model; simple DAX aggregations become measures and metrics, everything else becomes a warning.
5. **AI-assisted discovery:** suggested descriptions, synonyms and rules, always validated by the engine and written as a reviewable YAML change, never applied directly. The LLM sits behind an interface, so tests run with a fake model and no API key.
6. **Retail pack:** the e-commerce example as a reusable industry pack, mapped onto a customer's tables by name matching, with the same tests.

The previous phase 3 items (SQL endpoint, result cache, Cube/Ossie export, control plane, managed hosting) move to phase 4, paused, to be revisited with demand.

## Consequences

- No new infrastructure is needed; the open core keeps growing with features a user can run locally.
- Every item reuses the compiled graph and the audit log, so evidence, replay and impact keep working across them.
- The cost report is an estimate based on configured rates, not on billing data; it says so.
- AI discovery never writes to the model without a person reviewing the change, consistent with "suggestions, always approved by a person" from ADR 0008's radar.
- Databricks validation remains deferred and formally open.
