# Pricing proposal (draft, for your decision)

This is a recommendation with its reasoning, not a decision already made. Partner Center lets a Managed Application plan be **free** ("Get It Now (Free)") or charge a **flat monthly/annual fee** and/or a **usage-based fee**, on top of whatever the resources themselves cost the customer's own subscription (confirmed in ADR 0015). Nothing here commits you to anything until you set a price in Partner Center.

## Recommendation: launch free

Price the initial plan at **$0 ("Get It Now (Free)")**.

**Why:**

- The product itself is marked `Development Status :: 3 - Alpha` in `pyproject.toml`; charging before the API/authoring format has stabilized (the project's own rule: "Before 1.0, a minor version may change the authoring format or the API") risks charging for something that still changes underneath early customers.
- [ADR 0009](../../../docs/decisions/0009-business-context-as-phase-3-differentiator.md) paused the whole "managed product" phase specifically because it "needs infrastructure, credits and customers that are not available yet." A free offer needs neither: there's no infrastructure to fund (it runs in the customer's subscription) and no customer base to have disappointed with a premature paid commitment.
- A free Managed Application offer is also simpler to get certified and listed: no payout profile mechanics tied to this specific offer's revenue, no pricing tiers to justify in the certification review.
- It matches what the project already is: Apache 2.0, open-core. Charging for the deployment template itself, with no differentiated paid capability behind it yet, would be charging for something freely available in the GitHub repository anyway (`az bicep build` + `az managedapp definition create` works for anyone who reads this `deploy/azure/` folder).

## A later paid tier, once there is real signal

Not now, but worth having in mind so the free launch does not foreclose it:

- A **"Supported"** plan (flat monthly fee) once you are ready to commit to an actual support SLA (response time, a named support contact, update notifications) — the thing a paying customer is actually buying, since the software itself stays Apache 2.0 either way.
- Metering real usage inside the deployed instance for a usage-based fee is **not implemented** anywhere in this codebase today; it would need a new integration with the Marketplace Metering Service API from within `cosmos serve`, a real engineering task, not a Partner Center checkbox. Don't commit to usage-based pricing before that exists.
- `docs/roadmap.md` already lists "Free hosted edition limits and commercial pricing (to revisit later)" as an open decision; revisit it together with this, not independently, so the Managed Application pricing and any future SaaS pricing tell one consistent story.

## What this needs from you regardless of which tier you pick

- A **payout account and tax profile** in Partner Center (confirmed requirement for transactable offers even at $0, per `plan-azure-app-managed-app`); you're already mid-enrollment for this.
- A decision on whether "CosmosData" is the entity actually invoicing, if a paid tier comes later.
