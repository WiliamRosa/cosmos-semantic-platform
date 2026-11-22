#!/bin/sh
# Starts `cosmos serve`. Falls back to --allow-anonymous when neither OIDC
# (COSMOS_OIDC_ISSUER) nor development API keys (COSMOS_DEV_API_KEYS) are
# configured, so the demo image boots with no identity wiring. Production
# deployments set COSMOS_OIDC_ISSUER/COSMOS_OIDC_AUDIENCE (Microsoft Entra ID)
# and drop the anonymous fallback.
set -eu

args="serve --host 0.0.0.0 --port 8080 --model semantic --connections connections.yml"

if [ -z "${COSMOS_OIDC_ISSUER:-}" ] && [ -z "${COSMOS_DEV_API_KEYS:-}" ]; then
    args="$args --allow-anonymous"
fi

exec cosmos $args
