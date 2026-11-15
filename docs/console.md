# Console

A read-only web console for the people who decide whether to trust the numbers ([ADR 0012](decisions/0012-read-only-console.md)). It shows:
- the model's health and how it is governed;
- the business processes as graphs;
- the catalog in business words;
- every audited answer.

It is available in English and in Brazilian Portuguese.

## Run it

`cosmos serve` serves the console at `/console`, next to the REST API and the MCP server:

```bash
export COSMOS_AUDIT_LOG=audit.jsonl        # answers, usage and cost
export COSMOS_RUNS_LOG=runs.jsonl          # check runs (health over time)
export COSMOS_CONSOLE_ADMINS=lead@example.com   # who sees everyone's answers and usage
cosmos serve -m semantic -c connections.yml
# open http://127.0.0.1:8080/console/
```

Sign in with an API key (`COSMOS_DEV_API_KEYS`) or a token from your identity provider. The credential stays in the browser. With `--allow-anonymous` (local development only) no sign-in is asked and everything is visible.

## Record health over time

The console shows what `cosmos check` recorded. Schedule it in CI or cron:

```bash
cosmos check -m semantic -c connections.yml --cases reconciliations.yml --log runs.jsonl
```

Each run does two things and appends one line to the log:
- it runs every business rule's tests;
- it compares each reconciliation case (a legacy report's SQL) with the governed metric.

It exits with 1 when a test fails or a report diverges.

With a DuckDB file as the data source, run `cosmos check` while `cosmos serve` is stopped. DuckDB lets only one process open a file, and both commands open it. Server databases (Postgres, SQL Server, Databricks, Snowflake and the others) have no such limit. A case without an `expect` passes only when the numbers match. See [examples/ecommerce/reconciliations.yml](../examples/ecommerce/reconciliations.yml).

## What each view answers

| View | Question |
| --- | --- |
| Overview | Can we trust the numbers? Ownership, tests, metric health, the last check, 30 days of answers by agents and people |
| Processes | What does this process depend on, and who owns each part? A graph from source systems to metrics and rules, colored by health |
| Catalog | What does this metric or rule mean? Definition, formula, versions, reconciliations, what depends on it, recent answers |
| Answers | Where did this number come from? Formula, rules, user context, SQL and fingerprints, with a button to reproduce it |
| Checks | What did each check run find? Rule tests and reconciliations with the likely cause of each divergence |
| Usage and cost | Who asks what, and at what estimated cost? Admins only |

## Find your way

| To | Do this |
| --- | --- |
| Jump to any metric, measure, rule, concept, process or page | Press `Ctrl+K` (`⌘K` on a Mac) and type part of its name |
| Search the list on the page | Press `/` |
| Switch between light, dark and the system's theme | The theme button at the top right; the choice is remembered in the browser |
| Get more room | Collapse the sidebar at its bottom; on a phone the menu opens as a drawer |

Long lists are paged and can be sorted by any column. The catalog keeps its search, filters, sort and page in the address, so a link reproduces the same view and "Back" from a metric returns to it. Answers and check runs are paged by the server: `GET /v1/console/answers` and `GET /v1/console/runs` take `limit` (at most 500) and `offset`, and return `total`, so every answer in the audit log is reachable, not only the latest ones.

Health always uses the same four states:

| State | Meaning |
| --- | --- |
| Failing | A rule test fails, or a reconciliation of the metric diverges |
| Needs attention | It depends on something failing |
| Healthy | Its tests pass and, for a metric, its last reconciliation matched |
| Not verified | Nothing verifies it yet |

## Public demo

[wiliamrosa.github.io/cosmos-semantic-platform](https://wiliamrosa.github.io/cosmos-semantic-platform/) is the console on the example model with synthetic data. The `demo` workflow rebuilds it on every change to `main`, and it needs no server:

1. `console/demo/snapshot.py` runs the example model on an in-memory DuckDB, answers a set of questions as people and as agents, records a check run, and writes each console view as JSON.
2. The demo build (`NEXT_PUBLIC_COSMOS_DEMO=1`) reads those files instead of calling the API. With no server to page answers and check runs, it filters and pages them in the browser. A first visit shows three places to start.

Replaying an answer needs a data source, so the demo explains that instead.

```bash
uv run python console/demo/snapshot.py console/public/demo-data
cd console && NEXT_PUBLIC_COSMOS_DEMO=1 NEXT_PUBLIC_BASE_PATH=/cosmos-semantic-platform npx next build
```

On Git Bash for Windows, prefix the build with `MSYS_NO_PATHCONV=1`, or the base path becomes a Windows path.

## Develop it

The sources are in `console/`: Next.js 16 (static export), React 19, TypeScript, Tailwind CSS 4, React Flow and Recharts.

```bash
# API with CORS for the dev server
COSMOS_CORS_ORIGINS=http://localhost:3000 cosmos serve -m examples/ecommerce/semantic \
  -c examples/ecommerce/connections.yml --allow-anonymous --port 8080

cd console
npm ci
NEXT_PUBLIC_COSMOS_API=http://127.0.0.1:8080 npm run dev   # http://localhost:3000/console/
npm run typecheck
npm run build      # static export, copied into src/cosmos_semantic/console_dist
```

Every visible string lives in `console/lib/i18n.tsx`, in English and Portuguese. Add a key to both dictionaries.

Shared pieces live in `console/components/`: `ui.tsx` (cards, badges, skeletons, `BackLink`), `table.tsx` (sortable headers, the pager, the search box) and `Shell.tsx` (sidebar, top bar, theme and `CommandPalette.tsx`). `console/lib/table.ts` sorts and pages lists in the browser. Reuse them so every list behaves the same way. The Inter font is self-hosted at build time by `next/font`, so the console makes no request to Google when it runs.
