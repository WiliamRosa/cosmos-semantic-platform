# ADR 0008: Verifiable numbers as the phase 2 differentiator

- **Status:** accepted
- **Date:** 2026-10-22
- **Changes:** the phase 2 plan in the [roadmap](../roadmap.md)

## Context

A strategy review (2026-10-22) concluded that ontology, MCP and governance alone do not differentiate Cosmos Semantic: established products already offer parts of them. The differentiator must be an integrated experience in which every number is **verifiable, reconcilable and explainable**. The review proposed six evolutions: AI-assisted semantic discovery, a trust engine (evidence per answer), metric reconciliation, a business knowledge graph, governance of agent actions, and industry packs.

Much of the groundwork already exists in the code:

| Need | What already exists |
| --- | --- |
| Evidence per answer | Generated SQL, reference date (`as_of`), audit log, rule owner and effective date, rule tests |
| Reconciliation | The reference runner compares the engine with hand-written SQL row by row; importers bring dbt and Cube definitions |
| Divergence diagnosis | The cross-database work already catalogued the classic causes: filters, fan-out, integer division, integer averages, nulls, time zone and date cut-offs, orphan rows lost in joins |
| Knowledge graph | The compiler's graph of models, entities, metrics, rules and concepts |

The roadmap previously planned Ossie export as the phase 2 differentiator, with Cube and dbt export, remote MCP with OAuth and the open source release. The project rule is one differentiator per phase.

Databricks validation (gate 1) is deferred by decision: development continues on DuckDB, Postgres and SQL Server.

## Decision

**The phase 2 differentiator is "verifiable numbers": an evidence envelope on every answer, plus metric reconciliation.** They are one story: reconciliation explains divergences using the same evidence.

Phase 2 delivers, in this order, each validated on DuckDB, Postgres and SQL Server:

1. **Evidence envelope** on every REST, MCP and CLI answer: expanded metric definitions, rules with owner, effective date and test status, applied policies, data source, `as_of`, model and SQL hashes. Plus `cosmos replay` to re-run an audited query.
2. **Reconciliation v1** (`cosmos reconcile`): a Cosmos metric against existing SQL, and model against model (for example an imported dbt or Cube model against the Cosmos model), with a diagnosis of the likely cause.
3. **Impact analysis** (`cosmos impact`): metrics, rules and concepts affected by a change, and who queried them according to the audit log.
4. **Model bootstrap from the database** (`cosmos bootstrap`), deterministic and without AI: entities from primary and foreign keys, candidate measures and time dimensions from column types.

Enablers in the same phase, not differentiators: publishing the open source core, remote MCP with OAuth, and closing the one-to-many dimension gap (for example, order revenue by product category).

**Moved to phase 3:** Ossie, Cube and dbt export.

**On the radar for the following phase (not phase 2):**

- AI-assisted semantic discovery: synonym, rule and description suggestions, always with human approval (a pull request with the generated YAML).
- Expanded knowledge graph: processes, systems and owners beyond rules.
- Industry semantic packs, starting from the e-commerce example as a retail seed, only with validated demand.

**Not planned:** governance of agent actions. It changes the product from reading to acting and raises the operational and security scope.

## Consequences

- A "divergence zoo" becomes a test fixture and the commercial demo: legacy SQL with deliberate classic mistakes (canceled orders counted, gross versus net, double counting through item joins, UTC date cut-off, integer average, customers without orders lost in a join). Reconciliation must find each one and name the cause, on all three databases.
- Reproducibility has an honest limit: re-running with the same model and `as_of` only returns the same number if the source data has not changed. The envelope states this and records evidence (row counts, hashes), without promising more.
- Gate 1 remains formally open until Databricks is validated; this is an accepted deviation, not a closed gate.
- Internal maturity scores from the strategy review (such as 7/10 toward 9–10/10) guide prioritization and are not used in public material.
