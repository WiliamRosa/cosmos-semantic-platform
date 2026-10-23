# Security policy

Cosmos Semantic sits between people, AI agents and business data, so security reports get priority.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button (Security tab of this repository). Do not open a public issue. Include the affected version or commit, the steps to reproduce and the impact you observed.

You will get an acknowledgement within 5 business days. Fixes are released as soon as they are ready, and reporters are credited unless they prefer otherwise.

## Scope

Of particular interest:

- bypassing row filters or `user_context` checks;
- personal or sensitive data (LGPD `sensitivity`) reaching AI agents;
- caller filters reading data outside `{member}` references;
- token validation (OIDC, JWKS) and the MCP authorization flow;
- anything that lets generated SQL write to a data source.

## Design notes

- Cosmos validates tokens from an external OIDC provider and never issues tokens.
- Development API keys (`COSMOS_DEV_API_KEYS`) are for local use only and must not be enabled in production.
- SQL reconciliation runs operator-provided SQL and is only available in the CLI, not through the REST API or MCP.
