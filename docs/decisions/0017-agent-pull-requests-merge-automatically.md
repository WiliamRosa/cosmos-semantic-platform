# ADR 0017: Agent pull requests merge automatically

- **Status:** accepted
- **Date:** 2026-11-25
- **Amends:** [ADR 0013](0013-supervised-agent-team.md) (protection of `main`, autonomy, daily cycle)

## Context

Under ADR 0013, every agent pull request waited for the maintainer's approving review before it could merge. In practice the maintainer approved almost every pull request the 18:00 run opened, and the bottleneck moved from writing changes to merging them: approvals, branch updates after each merge, changelog conflicts between pull requests of the same day. The maintainer's approval already happens earlier, in the daily, when they answer `DEC-<n> approve`.

GitHub does not let a pull request's author approve it, and an approval by GitHub Actions does not count as a code owner's review, so the bot cannot satisfy the review rule by itself.

## Decision

The approval of a decision in the daily is the approval of its pull requests. The 18:00 execute run opens the day's pull requests and **merges the eligible ones itself** once their checks pass.

| Area | Decision |
| --- | --- |
| Protection of `main` | No required review. The `test` and `dco` checks stay required, the branch must be up to date, history stays linear, and force pushes and deletions stay blocked. `CODEOWNERS` keeps naming the maintainer, for review requests only |
| Eligible | A pull request authored by `cosmosagentsteam-lab` that implements a decision the maintainer approved in a daily, or an item of the standing list (documentation, tests, small fixes, dependency patches); with the `semantic-guardian` verdict, and the `security-reviewer` verdict where ADR 0013 asks for it, recorded in its description as a pass; with `test` and `dco` green on a head that is up to date with `main` |
| Never merged by an agent | Pull requests from anyone else (external contributors, the maintainer). `social` pull requests: merging one is the maintainer's approval to publish (ADR 0014). Releases (a version bump). A change to the SQL generated for BigQuery or Databricks, which needs the maintainer's live run (ADR 0016): any change to `dialects.py`, `connectors.py`, `planner.py`, `compiler.py`, `expressions.py`, `bootstrap.py`, `demo_data.py` or the reference cases. Changes to `.github/`, `CODEOWNERS`, `CLAUDE.md`, `.claude/` or `docs/decisions/`. New dependencies. Pull requests labelled `hold`. Any pull request a reviewer did not pass |
| Waiting pull requests | The run labels them `needs-maintainer` and the 20:00 daily lists them with the reason (for example, "Databricks and BigQuery run before merge") |
| Merging | Squash, through the REST API, one pull request at a time: update the branch with `main`, wait for the checks, merge, then the next. A changelog conflict is resolved keeping both entries; any other conflict leaves the pull request for the next run or the maintainer. The bot deletes the branch of a pull request it merged, and nothing else |
| Veto | The maintainer labels a pull request `hold`, or writes `DEC-<n> hold` in the daily, and the run skips it. A merged change is undone with a revert pull request, which the maintainer merges |
| Daily report | "Merged automatically" lists what the run merged; "Waiting for the maintainer" lists the rest with the reason |

Unchanged from ADR 0013: the identity checks, the budget, the decisions in the daily, tags, releases and publishing by the maintainer only, no changes to settings, secrets or privileged workflows by an agent.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| A second machine account that approves the first one's pull requests | Same protection in effect (an agent approving an agent) with one more account, token and secret to manage |
| Keep the review rule and approve every evening | The current state: the maintainer's time goes to approving pull requests they already approved as decisions |
| Merge every agent pull request, live runs included | BigQuery and Databricks are not run in CI; a dialect regression would reach `main` and the next release unseen |

## Consequences

- Approved work reaches `main` the same evening, and the 20:00 daily reports it as done.
- The maintainer's own pull requests no longer need the admin bypass; they still need green checks.
- `main` can receive a change the maintainer has not read. The safeguards are the decision in the daily, the reviewer verdicts, the required checks, the exclusions above, and revert pull requests. Releases stay manual, so nothing reaches PyPI without the maintainer.
- The maintainer applies the protection change (`required_pull_request_reviews` removed); agents never change it.
