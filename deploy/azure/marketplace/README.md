# Marketplace listing content (drafts)

Prepared while the Partner Center business verification for `administrator@cosmosdata.ai` was in progress (ETA 2026-11-25), so the listing is ready the moment verification clears. Nothing here has been submitted anywhere; these are files in this repository.

- [`listing.md`](listing.md) — offer name, descriptions, keywords, links, screenshots list. A few fields are marked `[ FILL IN ]` (support email, privacy/terms URLs once published) since only you can decide them.
- [`screenshots/`](screenshots) — five 1280x720 PNGs of the public console demo (Overview, Processes, Catalog, Answers, Usage and cost), captured 2026-11-21.
- [`privacy-policy.md`](privacy-policy.md) — draft, grounded in the actual architecture (a Managed Application: the publisher never receives customer data, since everything runs in the customer's own subscription). **Needs legal review before publishing anywhere or linking from Partner Center.**
- [`terms-of-use.md`](terms-of-use.md) — draft, same caveat. **Needs legal review.**
- [`pricing-proposal.md`](pricing-proposal.md) — a recommendation (launch free) with its reasoning, not a decision already made.

## Still missing before you can submit the offer in Partner Center

- A logo (48x48 and 216x216 PNG) — a design asset, not drafted here.
- Legal review and finalization of the privacy policy and terms of use, then publishing them at stable URLs (e.g. as pages on the GitHub Pages blog or a CosmosData site) so `listing.md`'s placeholder links can be filled in.
- A support email decision.
- Waiting for Partner Center business verification to clear (ETA 2026-11-25 as of this writing).
- The actual publicly-pullable container image: `main.bicep`'s `containerImage` default still points at `ghcr.io/wiliamrosa/cosmos-semantic-demo:latest`, which has never actually been pushed (the GitHub token used in this session lacked `write:packages`; the 2026-11-21 end-to-end test used a temporary Azure Container Registry instead, since torn down). Push a real image there (or wherever you decide) before submitting, and re-run `az bicep build` with that confirmed.
