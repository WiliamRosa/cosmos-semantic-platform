# Marketplace listing content (draft)

Draft copy for the Partner Center offer's "Offer listing" page. Everything here describes only what is implemented and verified in this repository (see the [README](../../../README.md), the [roadmap](../../../docs/roadmap.md) and [ADR 0015](../../../docs/decisions/0015-azure-marketplace-managed-application.md)); nothing is aspirational. Fill in the bracketed placeholders, then paste each field into Partner Center.

## Offer name

Cosmos Semantic

## Search results summary (short description, max 100 characters)

Semantic layer with a verifiable ontology: agents get the same number everywhere, with proof.

(99 characters)

## Description (long)

AI agents that write SQL over raw tables return numbers that look right and that nobody can explain: "revenue" has three definitions, "active customer" means 60 days to one team and 90 to another.

Cosmos Semantic defines metrics, business rules and concepts once, and serves them to AI agents over MCP instead of letting them guess SQL. Every answer carries the expanded formula, the rules applied, the row filters, the reference date and hashes, so a number can be reproduced, not just trusted.

**What this offer deploys**

This is a Bring-Your-Own-Azure-subscription offer. It deploys the open-source Cosmos Semantic server (REST API, MCP server, and a read-only console) as an Azure Container App inside your own subscription, alongside a Key Vault and a managed identity for secrets. You own, run and pay for the deployed resources directly; nothing is sent to the publisher.

**Capabilities**

- Multi-database: the same model runs unmodified on Databricks, Snowflake, Oracle, SQL Server, Postgres, MySQL, MariaDB and DuckDB.
- A bilingual (English/Portuguese) business glossary in SKOS, versioned business rules with owners and tests, and a knowledge graph linking metrics to the teams, systems and processes that depend on them.
- A native MCP server: agents find a concept, ask for a governed metric, and get deterministic SQL back, never raw table access.
- Every answer is auditable: formula, rule versions, applied policies, data source, `as_of` date, model and SQL hashes. `cosmos replay` reproduces a past answer exactly.
- Reconciliation against legacy SQL a team already relies on, with a diagnosed likely cause for each divergence.
- A read-only console (also in this deployment) for the people who decide rather than build: metric health, business processes as a graph, the full catalog in business words, every audited answer, and usage/cost per agent.
- Open, no lock-in: the core is Apache 2.0, with import from dbt, Power BI, Cube and Apache Ossie.

**Identity**

Authenticates every API and MCP call against your own Microsoft Entra ID tenant. No separate user database, no credentials held by the publisher.

**Getting started**

After deployment, the app's console is available at `https://<your-app-url>/console`. See the [quickstart](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/quickstart.md) for defining your own semantic model, and the [documentation](https://github.com/WiliamRosa/cosmos-semantic-platform/tree/main/docs) for connecting a real data source and Microsoft Entra ID.

## Search keywords (max 3)

- semantic layer
- MCP
- metrics governance

## Categories

- AI + Machine Learning
- Analytics

## Industries

- (none specific; horizontal data/AI tooling)

## Useful links

- Documentation: https://github.com/WiliamRosa/cosmos-semantic-platform/tree/main/docs
- Live demo of the console: https://wiliamrosa.github.io/cosmos-semantic-platform/
- Source code (Apache 2.0): https://github.com/WiliamRosa/cosmos-semantic-platform
- Quickstart: https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/quickstart.md

## Support contact

- **[ FILL IN: support email, e.g. support@cosmosdata.ai ]**
- Issues are also tracked publicly: https://github.com/WiliamRosa/cosmos-semantic-platform/issues

## Engineering contact (internal, Partner Center only, not shown publicly)

- **[ FILL IN ]**

## Privacy policy URL

- **[ FILL IN once privacy-policy.md (this folder) is published at a stable URL ]**

## Terms of use URL

- **[ FILL IN once terms-of-use.md (this folder) is published at a stable URL ]**

## Screenshots

Five PNGs at 1280x720 in `screenshots/`, captured from the public console demo on 2026-11-21:

1. `01-overview.png` — Overview: metric health, governed metrics, business rules and glossary at a glance.
2. `02-processes.png` — Business processes as a graph, colored by health, with owners and dependent metrics.
3. `03-catalog.png` — The catalog: every metric, measure, rule and concept in business words, searchable.
4. `04-answers.png` — Audited answers: every number an agent received, with the formula, rules and reproduction fingerprint.
5. `05-usage.png` — Usage and estimated cost per metric and per agent, from the audit log.

## Still needed (not drafted here)

- **Logo(s)**: Partner Center requires a small (48x48 or 216x216) and a large (216x216) PNG logo. Needs an actual design asset, not generated here.
- **Video** (optional): not created.
- A final decision on the publisher display name shown to customers (the CosmosData business entity, per the Partner Center enrollment in progress).
