# Friday 2026-11-20: console demo video

A 47-second captioned walkthrough of the live demo console, recorded with [`record.py`](record.py) (Playwright video, converted to H.264 MP4).

## Post

45 seconds inside the Cosmos Semantic console, the live demo you can open right now:

1. Can we trust the numbers? Every governed metric with its health and owner.
2. An audited answer: the rules applied, the row filters, the reference date, the fingerprints and the exact SQL.
3. An agent asks for customer emails, and the request is refused and logged. Personal data never reaches an agent.
4. A business process as a graph, from source system to metric.
5. One definition of revenue, with owner, rules and tests.

Read-only, synthetic data, no install:
https://wiliamrosa.github.io/cosmos-semantic-platform/

Apache 2.0 · pip install cosmos-semantic
https://github.com/WiliamRosa/cosmos-semantic-platform

#SemanticLayer #AIAgents #DataEngineering #MCP #OpenSource

## Sources

- Every screen and caption: the [live demo](https://wiliamrosa.github.io/cosmos-semantic-platform/) and [docs/console.md](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/console.md).

## Publish

- **Published:** Friday 2026-11-20, by the maintainer, as Cosmos Semantic.
- **Attached:** [`cosmos-semantic-demo.mp4`](cosmos-semantic-demo.mp4) as a native video. To refresh it after console changes: `python record.py <out-dir>`, then convert the `.webm` with ffmpeg (`-c:v libx264 -pix_fmt yuv420p -movflags +faststart`).
