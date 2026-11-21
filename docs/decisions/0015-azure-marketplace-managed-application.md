# ADR 0015: Azure Marketplace, as a Managed Application

- **Status:** accepted
- **Date:** 2026-11-21

## Context

[ADR 0009](0009-business-context-as-phase-3-differentiator.md) paused phase 4 (a Postgres-compatible SQL endpoint, a control plane, managed hosting on Azure) because it "needs infrastructure, credits and customers that are not available yet". That reasoning still holds for a self-run, multi-tenant SaaS: it needs a 24/7 landing page and webhook, a real multi-tenant backend, and ongoing hosting cost that scales with customers before any customer exists to pay for it.

Microsoft Azure Marketplace, however, offers a second listing type with a materially different cost shape: an **Azure Application (Managed Application)** offer deploys an ARM/Bicep template (`mainTemplate.json`) directly into the *customer's own subscription*. The customer's subscription runs and pays for the resources; the publisher holds no infrastructure and no ongoing hosting cost (confirmed on `learn.microsoft.com/en-us/partner-center/marketplace-offers/plan-azure-app-managed-app` and `learn.microsoft.com/en-us/azure/azure-resource-manager/managed-applications/overview`, 2026-11-21). The deployment wizard (from `createUiDefinition.json`) and the post-deploy overview blade with a resource-scoped side menu are both native Azure Portal chrome, generated for every Managed Application; no custom frontend is needed to get that experience.

This also removes the main blocker ADR 0009 named: validating the packaging and the deploy flow costs only what it takes to deploy-and-tear-down a test instance in the publisher's own subscription, which fits a small, fixed monthly Azure credit, not a budget that must scale with customer count.

## Decision

**Package Cosmos Semantic as an Azure Marketplace Managed Application offer, not a transactable SaaS offer.** The resources it deploys run in the customer's subscription, under their own cost and governance.

First slice, in `deploy/azure/`:

| Piece | What it does |
| --- | --- |
| `Dockerfile` | Builds `cosmos serve` from this repository's source (not the published PyPI package, so the engine version always matches the example model's authoring format) with the `examples/ecommerce` model and a baked-in DuckDB demo dataset. No external database is required to try the product |
| `entrypoint.sh` | Starts `cosmos serve`; falls back to `--allow-anonymous` when `COSMOS_OIDC_ISSUER` and `COSMOS_DEV_API_KEYS` are both unset, so the demo image works before any identity is wired, and the same image serves a production deployment once they are set |
| `main.bicep` | A Container App (scale to zero, 0.5 vCPU / 1Gi), a Key Vault with a "Key Vault Secrets User" role assignment to the app's user-assigned managed identity (for a future connections.yml to pull real data-source secrets), and the Log Analytics workspace Container Apps requires. Compiles to `mainTemplate.json` with `az bicep build` |
| `createUiDefinition.json` | One wizard step, Identity: an optional Microsoft Entra ID issuer URL and audience/client ID. Left empty, the deployment is anonymous/test mode. The container image itself is fixed by the publisher and never shown to the customer |

Deferred, revisited only with real listing progress:

- A production `connections.yml` wired to Key Vault secrets, for a customer's own data source instead of the baked-in demo data.
- `viewDefinition.json` (a custom blade inside the deployed Managed Application), since the default Azure Portal resource view already covers the first slice.
- The actual Partner Center offer, listing text, pricing and certification. Publishing anything (an image to a registry, an offer to Partner Center) needs the maintainer's explicit approval each time, per `CLAUDE.md`: "Nothing outside the repository without the maintainer's approval: no posts, no comments on other projects, no registry or directory submissions, no package publishing."
- The paused phase-4 items that a Managed Application does not need either way: the SQL endpoint, result cache, and a self-run control plane. Those stay paused.

## Consequences

- No new infrastructure or ongoing cost for the publisher: every resource this template deploys runs in whoever deploys it, including the maintainer's own subscription while testing.
- The demo image needs no external database, so a first test deploy exercises the whole path (container build, Bicep deploy, portal wizard, overview blade) without provisioning a second paid resource just to have data to query.
- Building from source instead of PyPI means the demo image can drift from the latest *released* engine; re-pin to a tagged PyPI release once one exists that matches the example model in use.
- A Managed Application, unlike SaaS, has no built-in per-seat or metered billing tied to actual usage inside the deployed instance; Marketplace billing here is limited to a flat "Get It Now" subscription or usage-based fee on top of infrastructure cost, set in Partner Center, not in this template.
- This does not commit to Marketplace certification, pricing or publishing; those remain separate, explicit decisions.

## Update, 2026-11-21: end-to-end test in a real subscription

Tested the whole path for real: built the image, pushed it to a temporary Azure Container Registry (used only because the publisher's GitHub token lacked `write:packages`; a published offer should still reference a publicly pullable image, so no registry role is needed in the customer's subscription), compiled and deployed `main.bicep` directly, then wrapped it as a private `Microsoft.Solutions/applicationDefinitions` (a Service Catalog Managed Application, visible only in this tenant, not published anywhere) to see the actual Marketplace wizard and the post-deploy overview/side-menu chrome this ADR described.

Two real bugs surfaced and were fixed in `main.bicep`:

- The `AcrPull` role assignment had no explicit `scope`, so it defaulted to the resource group being deployed into rather than the registry's own resource group, granting a permission that pointed nowhere useful. Fixed by moving it into `acr-pull-role.bicep`, a module invoked with `scope: resourceGroup(...)` targeting the registry's actual resource group, the pattern Bicep requires for any cross-resource-group role assignment.
- The repository had no `.dockerignore`, so building the image sent the whole working tree (including `.venv`, with every optional database connector installed) as build context; a remote `az acr build` stalled uploading it. Added `.dockerignore` restricting the context to what the Dockerfile actually copies.

Both fixes are in this version of `main.bicep`; the CLI command for `az acr build` also needs `--file`/context to stay at the repository root even with the smaller context. A Container App at `minReplicas: 0` takes roughly a minute to cold-start on its first request after a fresh deploy; this is expected, not a failure.
