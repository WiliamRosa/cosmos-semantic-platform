# Product vision

## Problem

Companies define the same metric many times: in the Power BI dashboard, in the finance spreadsheet, in the analyst's SQL and now in the AI agent's prompt. Each version gives a slightly different number. When an AI agent reads the raw database schema, it gets worse: the agent guesses what `vl_tot_liq` means and is confidently wrong.

A semantic layer solves this by defining metrics, dimensions and relationships once and serving the same result to every tool. The market has validated the idea: Databricks, Snowflake, dbt, Cube, AtScale, Dremio and Microsoft launched or strengthened AI-oriented semantic layers in 2025 and 2026.

## Proposal

A semantic layer with its own engine that:

- has a **simple authoring format**, inspired by the dbt Semantic Layer, with entities that infer joins and first-class metric types;
- brings a **business ontology from the MVP on**: a SKOS glossary with synonyms and hierarchy, and business rules with an owner, an effective date and tests, readable by business people;
- **generates SQL for each data source** and runs it there, with no intermediate infrastructure;
- serves **AI agents through MCP** and applications through REST;
- speaks **Apache Ossie** natively and **imports and exports Cube and dbt**;
- starts with a **free edition**.

## Audience

| Profile | What they look for |
| --- | --- |
| AI team | Trustworthy context for agents, through MCP |
| Analytics engineer | Model metrics in git, with review and CI validation |
| Application developer | A metrics API for products and embedded analytics |

Initial focus: mid-sized companies that want to put AI agents on top of their own data and do not yet pay for an enterprise semantic product. BI teams come in once there is a SQL endpoint, or through export to Cube.

## Positioning

| Tool | Strength | Where this product differs |
| --- | --- | --- |
| [Cube](https://cube.dev/pricing) | Mature engine, caching, free plan, official Claude connector | Simpler authoring format and native Ossie; Cube is an import source and export target, not a direct runtime competitor |
| [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/sl-faqs) | Versioned metrics in the dbt project; supports Snowflake, BigQuery, Databricks, Redshift, Postgres and Trino | Does not require a dbt project; supports sources dbt does not, such as SQL Server and Oracle |
| [AtScale](https://atlan.com/know/ai-agent/semantic-layer/atscale-vs-context-layer/) | Virtualization and native Power BI and Excel connectivity | Free plan and affordable pricing for mid-sized companies |
| [Dremio](https://www.dremio.com/newsroom/sap-to-acquire-dremio-to-unify-sap-and-non-sap-data-to-power-agentic-ai/) | Federated engine, open source MCP with semantic search | Independent: Dremio was acquired by SAP in 2026 |
| [Microsoft Fabric IQ](https://letsdatascience.com/news/microsoft-expands-fabric-iq-as-a-context-layer-for-ai-agents-e9a90bd8) | Ontology and semantics over OneLake and Power BI | Does not require Fabric; neutral across platforms |

As the market consolidates around the big platforms, **neutrality** becomes an argument again: the product belongs to no data platform.

The adoption differentiator is the **ontology**. Databricks Genie has business expressions tied to a Genie agent, Fabric IQ has an ontology still in preview and inside Fabric, and Palantir offers an ontology with functions only for large enterprises. Cosmos Semantic delivers a versioned, tested and neutral glossary and business rules, in the open core ([ADR 0006](decisions/0006-ontology-in-the-mvp.md)).

## Editions (open core)

Cosmos Semantic is open core ([ADR 0005](decisions/0005-open-core.md)): everything that defines or computes a number is Apache 2.0.

| Feature | Open core (self-hosted) | Free hosted | Commercial |
| --- | --- | --- | --- |
| Authoring format, engine and validation | Yes | Yes | Yes |
| REST API and MCP server | Yes | Yes, with usage limits | Unlimited |
| Ontology: SKOS glossary and business rules | Yes | Yes | Yes |
| LGPD rules and row filters | Yes | Yes | Yes |
| Ossie, Cube and dbt import and export | Yes | Yes | Yes |
| Result cache and SQL endpoint for BI (phase 3) | Yes | Yes | Yes |
| Data sources | Unlimited | Limited | Unlimited |
| Hosting | By the user | Managed | Managed on Azure |
| SSO, SCIM and enterprise auditing | No | No | Yes |
| Role-based access in the control plane | No | No | Yes |
| Support and SLA | Community | Community | Contractual |

The free hosted edition's limits are hypotheses to validate with the first users.

## Non-goals (for now)

- Caching, pre-aggregations and a Postgres-compatible SQL endpoint in the MVP.
- Importing Power BI models (a post-MVP differentiator candidate).
- Ready-made semantic packages for ERPs.
- A data transformation tool (ETL or dbt).
