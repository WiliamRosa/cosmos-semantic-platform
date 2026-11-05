# ADR 0012: A read-only console for people who decide

- **Status:** accepted
- **Date:** 2026-11-04

## Context

Everything Cosmos Semantic proves about a number already exists:
- the evidence envelope and replay;
- reconciliation against legacy SQL;
- rule versions and their tests;
- impact analysis;
- the knowledge graph of teams, systems and processes;
- the audit log, with usage and cost.

All of it is reached through the CLI, the REST API or JSON files. The people who decide whether to trust the numbers, a director of data and AI or a manager of a business process, do not use those. Pilots also need something to show in a meeting.

The risk is building a second product: an editor, a scheduler, a place to store credentials. Each of those is a project of its own, and the model already has a review process, pull requests in Git.

## Decision

| Question | Decision |
| --- | --- |
| Scope | Read only. The console shows what the engine already knows and changes nothing: no YAML editing, no data source management, no SQL. Changes keep going through pull requests |
| Audience | Non-engineers first: a director (can we trust the numbers, what do agents cost, where are the risks) and a process manager (what does this process depend on, who owns each part, what changed, what breaks if it changes) |
| Views | **Overview:** model size, ownership and tests, metric health, the last check and 30 days of answers. **Processes:** the knowledge graph around a process, each box colored by health. **Catalog:** every metric, measure, rule and concept, with a detail page showing definition, formula, rule versions, reconciliations, impact and recent answers. **Answers:** audited answers, with the formula, rules and user context, plus a button that replays the answer. **Checks:** the history of check runs. **Usage and cost:** admins only |
| Health | One vocabulary everywhere: `failing` (a rule test fails, or a reconciliation fails), `attention` (depends on something failing), `healthy`, `unchecked` (nothing verifies it yet). The reasons travel as codes with parameters, so the console words them in either language |
| History | The one new engine piece. `cosmos check` runs the rule tests and the reconciliation cases and appends a record to `COSMOS_RUNS_LOG` (JSONL, like the audit log). It exits with 1 when something fails, so CI or cron can schedule it. Reconciliation cases no longer need an expectation: outside the divergence zoo, a case passes when the legacy numbers match |
| Access | Any authenticated user sees the model, the graph and health, as `/v1/members` already allows. Audit records (who asked what) and usage per person are shown only to the subjects in `COSMOS_CONSOLE_ADMINS`. Everyone else sees only their own answers, and impact analysis lists consumers only for admins. `--allow-anonymous` (local development) sees everything |
| Languages | English by default, Brazilian Portuguese as an option; the choice is remembered per browser. Glossary labels come from the model's `pt`/`en` labels |
| Stack | Next.js 16 (App Router, static export), React 19, TypeScript, Tailwind CSS 4, React Flow (graph) and Recharts (charts), in `console/`. The export is built into `src/cosmos_semantic/console_dist` and served by `cosmos serve` at `/console`, so installing is still one `pip install`. No new backend: the views are `/v1/console/*` endpoints in the existing FastAPI app |
| License | Apache 2.0, in the open-source core ([ADR 0005](0005-open-core.md)): the console is what makes adoption visible. Editing with approval flows, scheduling and secrets management stay candidates for a commercial edition (phase 4, paused) |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| A Go backend for the console | The engine, the evidence and the policies are Python. A second backend would duplicate them and add a third language to maintain |
| A separate Node server (Next.js server rendering) | One more process to run and secure. A static export behind the API is enough for a read-only console |
| A full workbench (editor, data sources, scheduling) | It would be the largest part of the codebase, would compete with Git as the source of truth, and would delay the pilots that should decide whether it is needed |
| An existing BI tool over the logs | No process graph, no link from a number to its rules, and a dependency for every user |

## Consequences

- CI and the release workflow build the console with Node 22 before packaging; the wheel ships the static files.
- Signing in takes a pasted API key or provider token for now. An OIDC login flow in the browser is the next step once a pilot uses Entra ID end to end.
- `examples/ecommerce/reconciliations.yml` shows realistic cases, legacy reports without expectations, and two of them diverge on purpose.
