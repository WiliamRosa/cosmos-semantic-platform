# ADR 0014: A social media agent for the LinkedIn page

- **Status:** accepted
- **Date:** 2026-11-20

## Context

Cosmos Semantic now has a LinkedIn Company Page, [linkedin.com/company/cosmos-semantic](https://www.linkedin.com/company/cosmos-semantic/), administered by the maintainer. The product changes every day (ADR 0013), but the people who could use it only hear about it when the maintainer finds time to write a post. The maintainer wants a page that reads like a company's: what is new this week, demos of the product, and posts that sell it, all in English.

Three constraints shape the design:

- **Publishing is outside the repository.** `CLAUDE.md` and ADR 0013 forbid agents from publishing anywhere outside the repository without the maintainer's approval.
- **LinkedIn's API is gated.** Posting as a page needs the Community Management API and the `w_organization_social` scope. LinkedIn grants it only to registered legal organizations, after vetting: a business email, a website, a privacy policy and, for the Standard tier, a screen recording ([Community Management app review](https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review)). Its access tokens last 60 days. The page is registered as self-employed today, so the API is not available yet. The self-serve `w_member_social` scope posts as a person, not as the page.
- **Claims must be true.** The product's promise is the right number, with the proof. A post that overstates a feature, invents a customer or quotes a number nobody measured damages the project more than no post.

## Decision

| Question | Decision |
| --- | --- |
| Role | A sixth specialist, **social media** (`.claude/agents/social-media.md`). It writes the page's posts and keeps the page's copy current. It plans nothing else and changes no product code |
| What it produces | One **content pull request per business day**, labelled `social`, touching only `marketing/linkedin/`. The pull request holds the next business day's post: the text ready to paste, the image or document to attach, the suggested time, and the source of every claim (pull requests, `CHANGELOG.md`, docs, test counts). When the product changes what the page says (a new database, a release), the same pull request updates `marketing/linkedin/page.md` |
| Weekly calendar | Monday: what is new this week. Tuesday: one feature in depth. Wednesday: a demo of the product. Thursday: a problem and how Cosmos solves it, written to sell. Friday: why it matters, with a call to action. No posts on weekends. The playbook in `marketing/linkedin/README.md` has the details |
| Approval | **Merging the pull request is the maintainer's approval** of that post. The daily report lists the open `social` pull request under "Ready to approve". The pull request does not count toward the cap of three agent pull requests a day: it touches no code and costs little to review |
| Publishing | **The maintainer publishes**, from the merged pull request, in about a minute. In an interactive session the maintainer may ask Claude to publish it through the browser, after reviewing the final screen. No agent publishes on its own |
| Claims | Only what is on `main` or in a merged pull request, with its source in the pull request. No customers, testimonials, logos of other companies, user counts or benchmarks that the repository does not hold. No negative comparisons with named products. English, plain, no emoji walls |
| Schedule | A cloud routine on weekdays' eves, Sunday to Thursday at 19:00 São Paulo time, before the 20:00 daily, so the post is in the daily and the maintainer can merge it that evening |
| Identity and budget | Same as ADR 0013: the machine account, the REST API, signed-off commits. The routine uses a faster model than the orchestrator, since the work is writing, not engineering |

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| The agent posts through LinkedIn's API | The API is only for registered organizations. When the project has one, an approved, merged pack can be posted by a GitHub Actions workflow holding the page token as a secret; that workflow is a separate decision, made by the maintainer, because it publishes outside the repository |
| A LinkedIn profile for the product or a bot | LinkedIn's terms forbid fake or headless profiles. A Company Page is the right form |
| Browser automation in the cloud routine | It would need the maintainer's LinkedIn session in the cloud, and it would publish without a human looking at the final screen |
| Posting every day, weekends included | A new page gains more from five good posts a week than from seven average ones, and the audience is at work on weekdays |
| A separate repository for marketing | The posts come from the product's pull requests and changelog; keeping them next to the code keeps the sources one click away |

## Consequences

- `marketing/linkedin/` holds the playbook, the page copy (`page.md`), the brand assets and one folder per post.
- The orchestrator's daily report gains the `social` pull request in "Ready to approve".
- Once the project is a registered organization, the maintainer can apply for the Community Management API and decide, in a new ADR, whether a workflow posts merged packs.
