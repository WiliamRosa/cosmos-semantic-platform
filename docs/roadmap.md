# Roadmap

**Status on 2026-10-23:** phases 0, 1 and 2 are implemented; phase 3 is in progress. The 18 basic and 24 advanced reference queries return the same result as hand-written SQL on DuckDB, Postgres and SQL Server; 80 random requests return identical numbers across the three databases; and the 2 synonym-only questions resolve through the glossary. Databricks validation (gates 1 and 2) is deferred by decision ([ADR 0008](decisions/0008-verifiable-numbers-as-phase-2-differentiator.md)); development continues on DuckDB, Postgres and SQL Server, and the gates stay formally open. Cube import moved into phase 1, with an equivalence test: the imported model returns the same numbers as the native one.

Durations are estimates for one person and must be recalibrated at the end of each phase. Project rule: one differentiator per phase, beyond the core.

## Phase 0: foundation (3–4 weeks) — done

- v1 authoring format specification (`version: 1`), with a JSON Schema generated from the Pydantic models and the full [semantic model](semantic-model.md) example.
- A synthetic data generator (an e-commerce with customers, orders, items, payments and sellers, including a tax id, e-mail and a sensitive field; `en_US` by default, `pt_BR` with CPF for LGPD demos) and a set of 20 hand-written SQL reference queries.
- An Arrow connector interface, with ADBC by default and SQLAlchemy as the fallback.
- A planner prototype for simple single-model metrics, generating SQL for Postgres and Databricks.

**Exit criterion:** the prototype reproduces the single-model reference queries on Postgres and Databricks.

## Phase 1: MVP (14–16 weeks) — implemented; Databricks validation pending

- Full planner: multi-hop inferred joins, the 4 metric types, per-grain aggregation to avoid row multiplication.
- Data sources: Postgres, Databricks and SQL Server.
- REST API and MCP server (`search_concepts`, `explain`, `list_members`, `query`, `show_sql`).
- Identity through an external OIDC provider (Entra ID first), with `user_context` mapping token claims; row filters and LGPD classification (`sensitivity`) as basic security.
- Import of dbt (latest and legacy specs), Ossie and Cube (static YAML) ([ADR 0007](decisions/0007-connectors-sources-identity-and-import.md)).
- **Phase differentiator: ontology.** SKOS glossary (concepts, synonyms, hierarchy, Portuguese and English labels) and business rules (`predicate`, `classification` with a decision table, `derived`) with an owner, an effective date and CI tests ([ADR 0006](decisions/0006-ontology-in-the-mvp.md)).
- CLI with `validate`, `test`, `query`, `sql`, `import`, `serve` and `mcp`, and CI validation.

**Exit criterion:** the 20 reference queries, including the ones that span models and use business rules, return the same result through REST, through MCP and through hand-written SQL, on the three data sources; and an agent correctly answers questions asked only with glossary synonyms.

### Reference queries

| Category | Queries |
| --- | --- |
| Fan-out: two measures at different grains (order, item, payment) | 4 |
| Simple metric and time grain | 3 |
| Multi-table join inferred from entities | 3 |
| Ratio and derived metrics | 3 |
| Dimension and business rule filters (`active_customer`) | 3 |
| Row filter from the token and masking of `personal` and `sensitive` data | 2 |
| Question asked only with glossary synonyms, through an agent | 2 |

An advanced suite of 24 more cases covers where databases disagree (integer division, integer averages, ISO weeks, null ordering, booleans, empty results), and a randomized test compares 80 requests across databases. TPC-H at a larger scale will cover performance. The Olist dataset is excluded because its license is non-commercial.

## Phase 2: verifiable numbers (6–8 weeks) — done

Redefined by [ADR 0008](decisions/0008-verifiable-numbers-as-phase-2-differentiator.md). Every item is validated on DuckDB, Postgres and SQL Server.

- **Phase differentiator: verifiable numbers.**
    1. **Evidence envelope** (done) on every REST, MCP and CLI answer: expanded metric definitions, rules with owner, effective date and test status, applied policies, data source, `as_of`, model and SQL hashes; `cosmos replay` re-runs an audited query.
    2. **Reconciliation v1** (done) (`cosmos reconcile`): a Cosmos metric against existing SQL, and model against model, with a diagnosis of the likely cause, proven against a "divergence zoo" of legacy SQL with classic mistakes.
    3. **Impact analysis** (done) (`cosmos impact`): what a change to a rule or metric affects, and who queried it according to the audit log.
    4. **Model bootstrap** (done) (`cosmos bootstrap`): a draft model from the database catalog, deterministic and without AI.
- Enablers: publish the open source core (Apache 2.0, contributions with DCO) — LICENSE, NOTICE, CONTRIBUTING and SECURITY ready, the repository is still private; remote MCP server with OAuth (protected resource metadata) — done; one-to-many dimensions (for example, order revenue by product category) — done, through a distinct bridge.

**Exit criterion:** reconciliation finds and explains every planted divergence on the three databases; replaying an audited query returns the same result and hashes; first external users run the open core with their own models.

## Phase 3: business context (8–10 weeks) — in progress

Redefined by [ADR 0009](decisions/0009-business-context-as-phase-3-differentiator.md): the connected BI and managed product phases are paused. Every item is validated on DuckDB, Postgres and SQL Server.

- **Phase differentiator: business context.**
    1. **Rule versions** (done): `history` on a rule; a query uses the version in effect on `rules_as_of` (default `as_of`); the evidence envelope names the version and its tests.
    2. **Usage and cost report** (`cosmos usage`): per metric, principal and channel, from the audit log, with configurable rates per data source.
    3. **Expanded knowledge graph:** teams, systems and processes linked to metrics, rules and models; impact grouped by owner; graph export.
    4. **Power BI import** (`.bim` and TMDL): simple DAX measures become metrics; the rest becomes warnings.
    5. **AI-assisted discovery:** suggestions validated by the engine and written as a reviewable YAML change.
    6. **Retail pack:** the e-commerce example as an industry pack, mapped onto a customer's tables.

**Exit criterion:** a past number can be recomputed under the rule that was in effect and under today's rule; every metric can be traced to an owner, a system and its usage cost; a Power BI model imports with an equivalence test.

## Phase 4: connected BI and managed product — paused

- Ossie export with a round-trip test; Cube and dbt export, with a round-trip test for Cube.
- Result cache.
- A Postgres-compatible SQL endpoint, for Power BI, Tableau and Excel.
- Control plane: editor, playground, connections, users.
- Managed hosting on Azure, SSO with Entra ID, auditing.

## On the radar

- Exporting the glossary as SKOS/RDF for data catalogs.
- More industry packs, only with validated demand.

Not planned: governance of agent actions (it changes the product from reading to acting).

## Decisions

- [x] Product name: Cosmos Semantic ([ADR 0005](decisions/0005-open-core.md)).
- [x] License: open core, with the core under Apache 2.0 ([ADR 0005](decisions/0005-open-core.md)).
- [x] Trademark conflict with Azure Cosmos DB: name kept; repository `cosmos-semantic-platform`.
- [x] Connectors, data source order, identity and authoring format ([ADR 0007](decisions/0007-connectors-sources-identity-and-import.md)).
- [x] Phase 2 differentiator: verifiable numbers ([ADR 0008](decisions/0008-verifiable-numbers-as-phase-2-differentiator.md)).
- [x] Phase 3 differentiator: business context; connected BI and managed product paused ([ADR 0009](decisions/0009-business-context-as-phase-3-differentiator.md)).
- [ ] Databricks validation (gates 1 and 2): deferred, formally open.
- [ ] Free hosted edition limits and commercial pricing (to revisit later).
- [ ] Result cache technology (phase 4).
- [ ] Build the SQL endpoint, or keep export to Cube as the official BI path.
