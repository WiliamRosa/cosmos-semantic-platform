# ADR 0005: The Cosmos Semantic name and open core licensing

- **Status:** accepted
- **Date:** 2026-10-16

## Context

The product needed a name and a licensing model. For the license, the options were Apache 2.0 for everything, a source-available license (BSL or Elastic License), or open core. The stated goal is to give the community as much access as possible to use and change the core, while leaving room for a paid product.

## Decision

**Name:** Cosmos Semantic. The repository is `cosmos-semantic-platform`.

**License:** open core.

| Part | License | Contents |
| --- | --- | --- |
| Core | Apache 2.0 | Authoring format, compiler, planner, SQL generator, connectors, REST API, MCP server, LGPD (personal data) rules, Ossie, Cube and dbt import and export |
| Commercial features | Proprietary | SSO and SCIM, enterprise auditing, role-based access in the control plane, managed multi-tenancy, hosting and support |

Principles for deciding where each feature belongs:

1. **Everything that defines or computes a number stays in the core.** If the community cannot audit and change how a metric is computed, the product loses trust.
2. **Commercial features are organizational, not analytical:** enterprise identity, auditing, managed operations, support.
3. **Nothing leaves the core once it is in.** Moving an open feature to the paid part breaks the community's trust.

## Contributions

- External contributions to the core use the Developer Certificate of Origin (DCO), signed off on each commit, with no copyright assignment.
- Commercial code lives in a separate repository that depends on the core like any other user.

## Consequences

- The free self-hosted version is the core itself, with no artificial limits on data sources or usage.
- Usage limits only exist in the free hosted version, which runs on the product's infrastructure.
- Any company can offer the core as a service; this risk is accepted in exchange for adoption. The defense is the quality of the hosted product and the commercial features.
- Before registering a domain and logo, check for a trademark conflict with Microsoft's Azure Cosmos DB. (2026-10-22: the name was kept.)
