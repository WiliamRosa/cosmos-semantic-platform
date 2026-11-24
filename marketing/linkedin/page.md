# The page's copy

What is on [linkedin.com/company/cosmos-semantic](https://www.linkedin.com/company/cosmos-semantic/) today. When a merged change makes it wrong, the social media agent updates this file in its pull request and says what to change on the page; the maintainer edits the page.

The page has two language versions, the default one and English, and both hold the English text below, so every visitor reads English.

## Header

| Field | Value |
| --- | --- |
| Name | Cosmos Semantic |
| URL | linkedin.com/company/cosmos-semantic |
| Tagline | Open-source semantic layer for AI agents: the same number on every database, with the proof in every answer. |
| Logo | [`brand/logo-400.png`](brand/logo-400.png) |
| Cover | [`brand/banner-1128x191.png`](brand/banner-1128x191.png) (text starts 330 px from the left, clear of the logo) |
| Button | Visit website → https://cosmosdata.ai |

## Details

| Field | Value |
| --- | --- |
| Website | https://cosmosdata.ai |
| Industry | Software Development |
| Company size | 2-10 employees |
| Type | Self-employed |
| Founded | 2026 |
| Specialties | Semantic Layer, AI Agents, Model Context Protocol, Data Governance, Business Ontology, Metrics Layer, Data Engineering, Analytics Engineering, Databricks, Snowflake, BigQuery, Open Source, Data Quality |

Add a specialty when its feature is merged (for example a new database), never before.

## Overview

AI agents that write SQL over raw tables return numbers that look right and that nobody can explain. "Revenue" has three definitions; "active customer" means 60 days to one team and 90 to another.

Cosmos Semantic is an open-source semantic layer with an ontology at its core. You define metrics, business rules and concepts once. AI agents and apps ask for a governed metric over MCP or REST instead of guessing SQL, and every answer carries its evidence: the expanded formula, the rules applied, the row filters, the reference date and hashes.

What you get:
- The same number on Databricks, Snowflake, BigQuery, Oracle, SQL Server, PostgreSQL, MySQL, MariaDB and DuckDB, checked by the test suite on every change.
- A native MCP server: agents find the concept, request the metric and get deterministic SQL. Personal data never reaches them.
- A business glossary (SKOS), versioned business rules with owners and tests, and a knowledge graph of teams, systems and processes.
- Reconciliation against the legacy SQL a team already uses, naming the likely cause of each divergence.
- Replay: tell whether a number changed, and why.
- Import from dbt, Power BI, Cube and Apache Ossie. No lock-in.

Apache 2.0. Install with pip install cosmos-semantic.

Live demo: https://wiliamrosa.github.io/cosmos-semantic-platform/
Code and docs: https://github.com/WiliamRosa/cosmos-semantic-platform
