# Wednesday 2026-11-25: demo, why two reports disagree

A two-minute path through the live demo: from a metric to the check that compares it with a legacy report, and the likely cause when they differ.

## Post

Two reports, two different revenue numbers. Who is right, and why?

You can walk through that question in the Cosmos Semantic live demo in about two minutes. Read-only, synthetic data, nothing to install:

1. Open the demo and press Ctrl+K. Type "revenue" and jump straight to the metric.
2. On the metric's page, read the definition, the formula, the owner and what depends on it.
3. Open Checks. Each check run compares governed metrics with legacy report SQL, such as a finance report that counts every order, canceled ones included.
4. For a report that does not match, the console shows the likely cause, not just a red mark.
5. Open Answers to see an audited answer: the formula, the rules, the SQL and the fingerprints behind a number.

The point: a number is not trusted because a tool shows it. It is trusted because you can see its definition, its health and the reason when it differs.

Try it: https://wiliamrosa.github.io/cosmos-semantic-platform/
Code (Apache 2.0): https://github.com/WiliamRosa/cosmos-semantic-platform

#SemanticLayer #DataGovernance #DataEngineering #AnalyticsEngineering #OpenSource

## Sources

- Public demo on the example model with synthetic data, no server: [docs/console.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/console.md), "Public demo".
- Ctrl+K jump to any metric, rule or page; catalog with definition, formula, versions, what depends on it: docs/console.md, "Find your way" and "What each view answers" (Catalog).
- Checks compare each reconciliation case (a legacy report's SQL) with the governed metric and show the likely cause of a divergence: docs/console.md, "What each view answers" (Checks) and the check description above it; probes: [docs/architecture.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/architecture.md), reconciliation section.
- Example legacy report that counts canceled orders (FIN-01): [examples/ecommerce/reconciliations.yml](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/examples/ecommerce/reconciliations.yml).
- Answers view (formula, rules, SQL, fingerprints): docs/console.md, "What each view answers" (Answers).
- Not claimed: which exact cases pass or fail in the demo today. Check the live demo's Checks page before publishing and adjust step 4 if it differs.

## Publish

- **Suggested time:** Wednesday 2026-11-25, 09:00 São Paulo.
- **Attach:** `card.png` (1080×1350). If it is missing, render `card.html` with `python marketing/linkedin/render.py marketing/linkedin/posts/2026-11-25-demo-checks`.
- Before posting, spend one minute in the demo to confirm the five steps match what it shows.
- After publishing, change this section to start with **Published:**.
