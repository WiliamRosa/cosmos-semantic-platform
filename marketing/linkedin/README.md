# LinkedIn playbook

The playbook for the [Cosmos Semantic LinkedIn page](https://www.linkedin.com/company/cosmos-semantic/). The social media agent ([ADR 0014](../../docs/decisions/0014-social-media-agent.md), [`.claude/agents/social-media.md`](../../.claude/agents/social-media.md)) follows it, and so does anyone who writes a post by hand.

## How a post gets out

1. On weekday eves (Sunday to Thursday, 19:00 São Paulo), the agent opens a pull request labelled `social` with the next business day's post in `posts/<YYYY-MM-DD>-<slug>/`.
2. The 20:00 daily report lists it under "Ready to approve".
3. **The maintainer merges it: that is the approval.** To change the text, edit the pull request or ask for changes in a review.
4. **The maintainer publishes** the merged `post.md` on the page at the suggested time: Start a post as Cosmos Semantic, paste the text, attach the visual, post. About a minute.

No agent publishes. When the project can use LinkedIn's Community Management API (it needs a registered organization), a workflow that posts merged packs becomes a separate decision.

## Who we write for

- **Data and analytics engineers** who own a warehouse and are asked to "put an AI agent on top of it".
- **Data leaders and governance teams** who need numbers that match across tools and can be audited.
- **Builders of AI agents** who need governed metrics over MCP instead of generated SQL.

## Voice

- Clear and concrete, like good documentation: the reader's problem first, then what Cosmos does, then the proof.
- Confident, never inflated. "Checked by the test suite on every change", not "revolutionary".
- English. Short paragraphs. At most five hashtags, at the end. Emoji are optional, at most two.

## Weekly calendar

| Day | Theme | What goes in it | Visual |
| --- | --- | --- | --- |
| Monday | **What's new** | What merged last week, in user terms: two to four items, each with its pull request. The release, if there was one | A card listing the items, or the changelog section |
| Tuesday | **Feature in depth** | One capability explained with a small example: the YAML, the request, the answer and its evidence | A card with the snippet, or a console screenshot |
| Wednesday | **Demo** | Something the reader can try in the live demo in two minutes, step by step | The console tour GIF or a screenshot of the step |
| Thursday | **Problem → solution** (commercial) | A pain buyers recognize: "revenue" with three definitions, an AI agent quoting a number nobody can explain, a migration that changes a KPI. How Cosmos solves it today | A before/after card |
| Friday | **Why it matters** (commercial) | The business case: audit, governance, the cost of a wrong number, AI agents in production. One call to action: the demo, the quickstart or a star on GitHub | The social preview or a card |

When a release ships, the next post is about it, whatever the day.

## Claims

Every claim in a post has a source in the repository, listed in the pack's `## Sources`:

- features: the merged pull request and the docs section;
- counts (databases, reference cases, tests): the docs or the latest CI run;
- the demo: the live demo page it shows.

Never: customers, users, testimonials, quotes, adoption numbers, benchmarks the repository does not hold, other companies' logos, negative comparisons with named products, or features that are not merged.

## Links

- Live demo: https://wiliamrosa.github.io/cosmos-semantic-platform/
- Repository: https://github.com/WiliamRosa/cosmos-semantic-platform
- Quickstart: https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/quickstart.md
- Install: `pip install cosmos-semantic`

Links inside images and PDF documents are not clickable on LinkedIn; put them in the text.

## Formats

- **Image:** 1080×1350 (portrait) or 1200×627 (landscape), PNG.
- **Document (carousel):** a PDF, one slide per page, 1080×1350. Multiple images in one post show as a grid, not a carousel.
- **GIF:** LinkedIn plays GIFs up to 8 MB inline.

## Folder layout

```text
marketing/linkedin/
  README.md        this playbook
  page.md          the page's copy (tagline, overview, specialties, button), the source of truth
  render.py        renders a pack's card.html to card.png
  brand/           logo, banner and the HTML they are rendered from
  posts/
    2026-11-20-launch/
      post.md      the text to paste, sources, publish notes
      card.html    (optional) the visual's source
      card.png     (optional) the rendered visual

A pack whose `## Publish` section starts with **Published:** is on the page. Published packs are the page's history: read them before writing, so a new post neither repeats an angle nor reuses a visual from the last two weeks.
```

## Hashtags

Pick at most five, relevant to the post: `#SemanticLayer` `#AIAgents` `#MCP` `#DataEngineering` `#AnalyticsEngineering` `#DataGovernance` `#OpenSource` `#Databricks` `#Snowflake` `#dbt` `#PowerBI`.
