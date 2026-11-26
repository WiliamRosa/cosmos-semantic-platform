# Thursday 2026-11-26: problem and solution, an agent asks "is this number still right?"

An AI agent quotes a revenue figure. Finance's report says something else. Cosmos lets the agent check, with the evidence, instead of guessing.

## Post

Your AI agent quotes a revenue figure. Finance's report shows a different one.

Who is right? Today, someone opens two SQL files and starts reading.

In Cosmos Semantic, the agent can ask the question itself. Through MCP or REST, it runs a reconciliation case your team declared: the governed metric against the legacy report's SQL.

What comes back:
- pass or fail
- the rows that do not match (at most 200)
- the likely cause, for example canceled orders counted in one report and not in the other
- the metric's updated health
- the evidence: hashes of both SQL statements, never the legacy SQL itself

The guardrails matter as much as the feature. Only declared cases run, never SQL from the caller. Cases that would read data under row filters are refused. Every call is audited, and a case runs at most once a minute.

So the number an agent quotes is not just generated. It can be checked against what the business already trusts, and the answer says why when they differ.

Code (Apache 2.0): https://github.com/WiliamRosa/cosmos-semantic-platform
Live demo: https://wiliamrosa.github.io/cosmos-semantic-platform/

#AIAgents #MCP #SemanticLayer #DataGovernance #DataEngineering

## Sources

- The `reconcile` MCP tool, `GET /v1/reconcile` and `POST /v1/reconcile/{case}` (DEC-45, PR #121): [CHANGELOG](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/CHANGELOG.md) `[Unreleased]`, "Reconciliation through MCP and REST".
- Outcome, row counts, row diff (at most 200 rows), likely causes, updated health, evidence with hashes of both SQL statements and never the legacy SQL: [README](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/README.md) (REST/MCP section) and [docs/architecture.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/architecture.md), `reconcile` tool row.
- Only declared cases (`COSMOS_CHECK_CASES`), never SQL from the caller; refused under row filters; audited as event `reconcile`; at most once per `COSMOS_RECONCILE_MIN_INTERVAL` (60 s by default): CHANGELOG entry above and docs/architecture.md.
- A legacy report that counts canceled orders as the example cause (FIN-01): [examples/ecommerce/reconciliations.yml](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/examples/ecommerce/reconciliations.yml).
- Not claimed: any figure or customer story; the opening is an illustration of the problem.

## Publish

- **Suggested time:** Thursday 2026-11-26, 09:00 São Paulo.
- **Attach:** `card.png` (1080×1350). If it is missing, render `card.html` with `python marketing/linkedin/render.py marketing/linkedin/posts/2026-11-26-agent-reconcile`.
- After publishing, change this section to start with **Published:**.
