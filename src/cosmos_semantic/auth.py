"""Identity (ADR 0007): Cosmos validates OIDC tokens from an external provider and never
issues tokens.

- Production: the provider's JWT (Entra ID first; Keycloak, Okta and Auth0 work the same way),
  validated against the JWKS published in the issuer's ``/.well-known/openid-configuration``.
- Development: API keys bound to a fixed user context, declared in a file pointed to by
  ``COSMOS_DEV_API_KEYS``. Never enable in production.
"""

from __future__ import annotations

import hmac
import os
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cosmos_semantic.errors import CosmosError
from cosmos_semantic.planner import Principal
from cosmos_semantic.spec import UserContextField


class AuthError(CosmosError):
    """Missing or invalid credential (HTTP 401)."""


class ForbiddenError(CosmosError):
    """Valid credential without the required claims (HTTP 403)."""


class DevKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    user: dict[str, Any] = {}


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    algorithms: list[str] = ["RS256", "ES256"]
    dev_keys: dict[str, DevKey] = {}

    @classmethod
    def from_env(cls) -> AuthSettings:
        dev_keys: dict[str, DevKey] = {}
        path = os.environ.get("COSMOS_DEV_API_KEYS")
        if path:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            dev_keys = {k: DevKey.model_validate(v) for k, v in (raw.get("keys") or {}).items()}
        return cls(
            issuer=os.environ.get("COSMOS_OIDC_ISSUER"),
            audience=os.environ.get("COSMOS_OIDC_AUDIENCE"),
            jwks_url=os.environ.get("COSMOS_OIDC_JWKS_URL"),
            dev_keys=dev_keys,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.issuer or self.dev_keys)


def principal_from_claims(
    claims: dict[str, Any],
    user_context: dict[str, UserContextField],
    channel: Literal["api", "mcp", "cli"],
) -> Principal:
    user: dict[str, Any] = {}
    missing = []
    for name, field in user_context.items():
        value = _claim(claims, field.claim)
        if value is None:
            if field.required:
                missing.append(f"{name} (claim `{field.claim}`)")
            continue
        user[name] = value
    if missing:
        raise ForbiddenError("the token lacks the required claims: " + ", ".join(missing))
    subject = claims.get("preferred_username") or claims.get("upn") or claims.get("sub")
    return Principal(user=user, channel=channel, subject=subject)


def _claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class Authenticator:
    def __init__(self, settings: AuthSettings, user_context: dict[str, UserContextField]) -> None:
        self.settings = settings
        self.user_context = user_context

    @cached_property
    def _jwks(self):
        import jwt

        url = self.settings.jwks_url
        if not url:
            import httpx

            issuer = (self.settings.issuer or "").rstrip("/")
            discovery = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
            discovery.raise_for_status()
            url = discovery.json()["jwks_uri"]
        return jwt.PyJWKClient(url, cache_keys=True)

    def claims(self, token: str) -> dict[str, Any]:
        import jwt

        if not self.settings.issuer:
            raise AuthError("OIDC tokens are not configured (COSMOS_OIDC_ISSUER)")
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                key.key,
                algorithms=self.settings.algorithms,
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iss"], "verify_aud": bool(self.settings.audience)},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc

    def authenticate(
        self,
        *,
        bearer: str | None = None,
        api_key: str | None = None,
        channel: Literal["api", "mcp", "cli"] = "api",
    ) -> Principal:
        if api_key is None and bearer and self.settings.dev_keys and bearer.count(".") != 2:
            api_key = bearer
        if api_key is not None:
            for key, dev in self.settings.dev_keys.items():
                if hmac.compare_digest(key.encode(), api_key.encode()):
                    return Principal(user=dict(dev.user), channel=channel, subject=dev.subject)
            raise AuthError("invalid API key")
        if not bearer:
            raise AuthError("send `Authorization: Bearer <token>`")
        return principal_from_claims(self.claims(bearer), self.user_context, channel)
