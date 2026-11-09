# ADR 0013: A supervised team of AI agents for daily evolution

- **Status:** accepted
- **Date:** 2026-11-09

## Context

Cosmos Semantic is public: it has releases on PyPI, a live demo, a listing in the official MCP registry, and its first external contribution merged. The project has one maintainer. Evolving it every day takes routine work: triaging issues and external pull requests, running the cross-database suite, keeping documentation and the changelog current, preparing releases, and turning approved ideas into code.

The maintainer wants that work done by AI agents running on schedules in the cloud, so it does not depend on a local session. The maintainer stays the Product Owner, and their effort should be a short daily approval. Three risks shape the design:

- **Silent semantic regressions.** A change that alters a number on one database without anyone noticing breaks the project's core promise.
- **Review load.** Agents that produce more pull requests than one person can review turn the maintainer into the bottleneck and lower quality.
- **Supply chain.** The release workflow publishes to PyPI. An agent or account with too much access is an attack path to every user who installs the package.

## Decision

| Question | Decision |
| --- | --- |
| Roles | An **orchestrator** and four specialists, defined in `.claude/agents/` and versioned with the code. The **semantic guardian** guards cross-database equivalence, reconciliation and evidence. The **security reviewer** covers identity, row filters, personal data, dependencies and workflows. **Community triage** handles issues, external pull requests and DCO. **Docs and release** maintains the changelog and documentation and prepares releases. Implementation work (backend, MCP, console) is done by the orchestrator or a general-purpose agent and is always reviewed by the guardian and, where relevant, the security reviewer. Product and architecture decisions stay with the maintainer |
| Orchestrator | The only agent that plans. It reads the state of the repository, decides the day's work within the budget, delegates to the specialists, keeps every task traceable to an issue, and writes the daily report. It runs on a schedule as a Claude Code cloud routine; specialists run as its subagents, each coding task in its own Git worktree |
| Daily cycle | **07:00 (São Paulo), plan:** the orchestrator reads issues, pull requests, CI, community activity and yesterday's answers, then posts the daily report in the **Daily** category of GitHub Discussions. **Maintainer:** replies to the report and approves the ready pull requests. **13:00, execute:** the orchestrator takes the approved items, delegates them and opens the day's pull requests. **Fridays:** a weekly review of metrics (stars, downloads, traffic), next week's roadmap and budget calibration |
| Daily report | Short and decision-first: pull requests ready to approve (each with a risk level, test evidence and a three-line summary), decisions numbered `DEC-<n>` with a recommendation, risks, and estimated budget use. The maintainer answers in one reply (`DEC-14 approve`, `DEC-15 reject`, `DEC-16 later`). The orchestrator acts only on replies authored by the maintainer's account; anyone else's comment is information, never an instruction |
| Autonomy | **Automatic:** analyze issues, propose work, create branches, write code, tests and documentation, run tests, open pull requests. **Through the daily:** new features, changes to semantic contracts (spec, planner, dialects, evidence), security-relevant changes, new dependencies, anything with a cost, roadmap priorities. **Never by an agent:** approving pull requests, releases and tags, repository settings and branch protection, secrets, privileged workflows (`release.yml`, `demo.yml`), publishing anywhere outside the repository, deleting anything |
| Daily pull requests | Pull requests come from approved work or from the standing "do without asking" list: documentation, tests, small fixes and dependency patches with green CI. They are small: one concern each, reviewable in minutes. With nothing approved, the orchestrator proposes work in the daily instead of opening a pull request to fill a quota |
| Budget | Target: about **50% of the maintainer's daily Claude usage**. A routine cannot read the plan's remaining usage, so the budget is controlled by size: at most **3 pull requests per day**, the strongest model only for the orchestrator and the semantic guardian, a faster model for the other specialists, and small tasks. The daily report shows an estimate. In the first week the maintainer checks real usage (`/usage`), and the caps are tuned on Fridays |
| Identity | Agents act as a dedicated machine account, **`cosmosagentsteam-lab`**, a collaborator with write access and nothing more. GitHub does not let an author approve their own pull request, so agent pull requests must come from an account other than the maintainer's. Interactive Claude Code sessions push as `cosmosagentsteam-lab` too. Its commits are signed off (DCO) and co-authored by Claude. The account is a collaborator on this repository only. Its token is a classic one with just `public_repo` and `write:discussion`: fine-grained tokens cannot reach a repository owned by another personal account. It has **no `workflow` scope**, so GitHub itself refuses any change to `.github/workflows/`. It has no admin rights, no secrets and no access to PyPI, and it expires every 90 days |
| Protection of `main` | Pull request required, with **1 approving review**. `CODEOWNERS` names the maintainer for everything, and review from code owners is required. The `test` and `dco` checks are required, the branch must be up to date, history is linear, and there are no force pushes or deletions. **Auto-merge** is allowed: once the maintainer approves an agent's pull request, the orchestrator enables auto-merge and it merges when checks pass. Admins may bypass the review rule only for the maintainer's own hand-made commits |
| Databases | The agents' runs use the six databases that run in containers (DuckDB, Postgres, SQL Server, MySQL, MariaDB, Oracle), as CI does. **Databricks** runs weekly and whenever a pull request touches SQL generation, dialects, connectors or the bootstrap, with a least-privilege token stored in the cloud environment. **Snowflake** is out of automated runs: the test account is a trial that ends in December 2026. Its tests stay in the suite for anyone with an account |
| Untrusted input | Issue text, pull request content, review comments, external documentation and web pages are data, never instructions. An external pull request is never run on a machine with credentials; GitHub Actions runs it, after the maintainer or community triage approves the workflow run |
| Tooling | Claude Code's own features: subagents, skills, hooks, worktrees and scheduled cloud routines, plus the `gh` CLI. No third-party orchestration framework is installed. One may be reconsidered if native orchestration proves insufficient, after a review of its code and permissions |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| Ten specialized agents from the start | More pull requests than one person can review, more coordination and more cost, while the bottleneck is users and feedback, not engineering throughput |
| A third-party orchestrator (for example Ruflo) | It would need repository and token access, which is supply-chain risk. Claude Code already provides subagents, worktrees and scheduled cloud runs |
| Agents acting as the maintainer's account | The maintainer could not approve them, since GitHub forbids self-approval, and every action would look like the maintainer's |
| A GitHub App for the agents | Finer permissions, but more setup. It remains the upgrade path if the machine account proves too broad |
| Agents merging after their own or another agent's review | AI review does not replace the human approval that protects a package published to PyPI |
| A quota of pull requests per day | Pull requests made to fill a quota are noise the maintainer still has to review |

## Consequences

- **`CLAUDE.md`** at the repository root holds the project's working rules for every agent and every session: language, DCO, identity, tests, changelog and credit, untrusted input, and what never to do.
- **The maintainer sets up, once:**
  - the `cosmosagentsteam-lab` account and its token (classic, `public_repo` and `write:discussion`, no `workflow`, 90 days);
  - the **Daily** category in GitHub Discussions;
  - the branch protection changes and auto-merge;
  - the secrets of the cloud environment.

  Agents cannot change these settings.
- **Scheduled routines are created after this ADR is merged.** The first week runs with a cap of one pull request per day, so the maintainer can calibrate the budget and the review effort.
- **The maintainer's merges change:** with 1 required review, the maintainer's own pull requests need the admin bypass. Agent pull requests cannot use it.
