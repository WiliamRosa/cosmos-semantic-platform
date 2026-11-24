# Sunday 2026-11-22: BigQuery support

BigQuery became a fully supported platform in #100 and #110 (DEC-40, ADR 0016). This post announces it.

## Post

BigQuery is now a fully supported platform in Cosmos Semantic.

Define a metric once, and AI agents get the same governed number on BigQuery as on Snowflake, Databricks and the databases you already run, with the evidence to prove it: the formula, the rules applied and the reference date.

Open source, Apache 2.0:
https://github.com/WiliamRosa/cosmos-semantic-platform

#BigQuery #GoogleCloud #SemanticLayer #AIAgents #DataEngineering

## Sources

- BigQuery support and its live validation: [ADR 0016](../../../../docs/decisions/0016-bigquery-as-a-data-source.md) and the CHANGELOG under [Unreleased].
- The evidence (formula, rules, reference date): [README](https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/README.md).

## Notes

- BigQuery is a data platform, not a database, like Databricks and Snowflake. Say "platform" in posts.
- At the maintainer's request, the post does not cite the test count.
- There is no `pip install "cosmos-semantic[bigquery]"`: PyPI still serves 0.4.0. Add the install command to a later post once 0.5.0 is released.
- The BigQuery mark is the Simple Icons glyph (`simple-icons@15.0.0`, `googlebigquery`) in BigQuery blue `#669DF6`, on a white tile.

## Publish

- **Published:** Sunday 2026-11-22, by the maintainer, as Cosmos Semantic.
- **Attached:** [`banner.png`](banner.png), rendered from [`banner.html`](banner.html) at 1200×627 and 2× scale.
