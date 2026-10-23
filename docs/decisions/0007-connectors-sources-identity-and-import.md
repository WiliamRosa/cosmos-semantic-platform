# ADR 0007: Connectors, data sources, identity, and importing dbt, Ossie and Cube

- **Status:** accepted
- **Date:** 2026-10-22

## Context

Before starting the code, four questions were open: the connector library, the order of data sources and the test data, who issues the token that feeds row filters, and how closely the authoring format follows dbt. The criteria were competitiveness, differentiation, modernization and flexibility.

## Decision

| Question | Decision |
| --- | --- |
| Connectors | Our own `Connector` interface that always returns Arrow results. ADBC is the default implementation; SQLAlchemy is the fallback for sources without a mature ADBC driver |
| Data sources | Postgres for development (runs in `docker compose`, free CI tests); Databricks is the first validated source, at gate 1; SQL Server joins in phase 1 and completes gate 2 |
| Test data | A synthetic generator in the repository, with Faker in pt_BR (an e-commerce with CPF, e-mail and a sensitive field); TPC-H at a larger scale for performance. The Olist dataset is excluded because its license is non-commercial |
| Identity | Cosmos is an OIDC resource server: it validates tokens from an external provider through JWKS (Entra ID first; Keycloak, Okta and Auth0 work the same way) and never issues tokens. API keys bound to a fixed context exist only in development mode |
| User context | The model declares in `user_context` each `$user` field and the token claim it comes from; if a required claim is missing, the query is denied. Agents query with the user's token (on-behalf-of flow) |
| Authoring format | Our own YAML with the dbt vocabulary, versioned (`version: 1`), with a JSON Schema generated from the Pydantic models |
| Import in phase 1 | dbt (latest and legacy specs), Ossie and Cube (static YAML only) |

The 20 reference queries cover the engine's traps: fan-out with measures at different grains (4), simple metrics and time grains (3), inferred joins (3), ratios and derived metrics (3), dimension and business-rule filters (3), row filters and LGPD masking (2), and questions using only glossary synonyms (2).

## Importing Cube

| In Cube | In Cosmos | Conversion |
| --- | --- | --- |
| `cubes` with `sql_table` or `sql` | Semantic model | Direct |
| Dimension with `primary_key: true` | Primary entity | Direct |
| `joins` with simple equality | Foreign entity | Direct; complex conditions produce a warning |
| Measures `count`, `count_distinct`, `sum`, `avg`, `min`, `max` | Measure of the same type | Direct |
| `number` measure referencing other measures | Derived metric | Requires reading the SQL expression |
| `segments` | Business rule | Owner and effective date are left pending; validation warns |
| `rolling_window` and multi-stage measures | Cumulative metric (not in the MVP) | Warning and `custom_extensions` |
| `pre_aggregations` and `views` | No MVP equivalent | `custom_extensions` |
| Access control in `cube.js` | `row_filters` and `sensitivity` | Manual; the import report lists the rules to recreate |

JavaScript models and YAML with Jinja or Python templates produce a warning and are not imported, because they would require running user code.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| SQLAlchemy only | Converts results row by row in Python; the market is moving to Arrow (Databricks already uses ADBC by default in Power BI) |
| Issue our own tokens | Duplicates the customer's identity provider and goes against the MCP authorization spec, which treats the server as an OAuth resource server |
| Read dbt YAML unchanged as the official format | dbt reworked its format in 2026 and keeps the previous one as legacy; Cosmos would be hostage to those changes and have no place for `sensitivity`, `row_filters`, the glossary and rules |
| Import Cube only in phase 2 | Initial recommendation; import was brought forward to phase 1 to provide a migration path from the MVP on |

## Consequences

- Phase 1 grows from 11–13 to 14–16 weeks (estimate to validate at gate 1).
- Phase 0 validates the prototype on Postgres and Databricks, no longer on Postgres and SQL Server.
- The JWT secret in Azure Key Vault is replaced by the development-mode keys; Entra ID joins the deployment as the default provider.
- Importing a Cube model and exporting it back must produce the same static YAML, except for what was reported as a warning; this becomes an automated test, as with Ossie.
- Extends [ADR 0002](0002-dbt-inspired-authoring-format.md), which now covers import as well as export.
