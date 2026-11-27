# Friday 2026-11-27: why it matters, "show me how you got that number"

The question every audit and every board meeting eventually asks. Cosmos answers it with the number itself: the formula, the rules, the date and a replay.

## Post

Three months from now, someone asks: "Where did this number in the report come from?"

If an AI agent produced it, the honest answer is often "we can't tell". The SQL was generated on the fly, and nobody kept it.

Cosmos Semantic is built so that answer is never needed.

Every answer can carry its evidence: the formulas, the business rules, the policies applied, the date it was computed for and hashes of the SQL. Every query can be written to an audit log. And any audited query can be replayed.

The replay says what changed since:
- identical: same number
- data_changed: same model and SQL, different result
- model_changed: a definition moved
- engine_changed: the engine did

It does not promise the same number if the source data moved. It tells you so.

That is the difference between a number an agent produced and a number a team can defend: to an auditor, to finance, to the person who made the decision with it.

Personal data never reaches the agents, and every call runs as the user who owns the token.

Open source, Apache 2.0. Try the console in two minutes, or read the quickstart.

Live demo: https://wiliamrosa.github.io/cosmos-semantic-platform/
Code and quickstart: https://github.com/WiliamRosa/cosmos-semantic-platform

#DataGovernance #AIAgents #SemanticLayer #MCP #OpenSource

## Sources

- Evidence envelope (formulas, rules, policies, `as_of`, hashes) and the audit log (`COSMOS_AUDIT_LOG`): [README](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/README.md), the "Evidence envelope ... replay" example.
- Replay and its four verdicts (`identical`, `data_changed`, `model_changed`, `engine_changed`), "only returns the same number if the source data has not changed": [docs/architecture.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/architecture.md), audit and replay paragraph; the `replay` MCP tool row.
- Every call runs as the user who owns the token; personal data never reaches agents: README, MCP section and the "Built for AI agents" row.
- Apache 2.0: README, principle 6 "Open core".
- Live demo and quickstart links: playbook.
- Not claimed: any customer, figure or audit outcome. The opening question is an illustration.

## Publish

- **Suggested time:** Friday 2026-11-27, 09:00 São Paulo.
- **Attach:** `card.png` (1080×1350). If it is missing, render `card.html` with `python marketing/linkedin/render.py marketing/linkedin/posts/2026-11-27-audit-replay`.
- After publishing, change this section to start with **Published:**.
