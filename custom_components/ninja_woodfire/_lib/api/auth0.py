"""Shared Auth0 password-realm grant.

Both cloud backends authenticate the same way — SharkNinja's Auth0 tenant
issues one `id_token` that Ayla exchanges for its own session and that the
AWS API accepts directly as a bearer token. Keeping the grant here means
there is one implementation of the credential flow rather than one per
transport.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import aiohttp


class Auth0Error(Exception):
    """Auth0 rejected the request."""


class Auth0Credentials:
    """The outcome of a successful grant.

    `user_id` is the bare identifier from the `sub` claim with Auth0's
    connection prefix stripped ("auth0|abc123" -> "abc123"). The AWS API
    wants it in that form: passing the prefixed value is rejected with 403,
    so the prefix is not cosmetic.
    """

    __slots__ = ("id_token", "access_token", "refresh_token", "expires_in", "claims", "user_id")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.id_token: str = payload.get("id_token", "")
        self.access_token: str = payload.get("access_token", "")
        self.refresh_token: str = payload.get("refresh_token", "")
        self.expires_in: float = float(payload.get("expires_in", 0) or 0)
        if not self.id_token:
            raise Auth0Error(f"no id_token in grant response (keys: {sorted(payload)})")
        self.claims: dict[str, Any] = decode_jwt_claims(self.id_token)
        sub = str(self.claims.get("sub", ""))
        self.user_id: str = sub.split("|", 1)[1] if "|" in sub else sub


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's claims without verifying it.

    We only need the `sub` claim to derive the user id; the token's validity
    is the API's business, not ours, and we never mint or trust one locally.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


async def password_grant(
    session: aiohttp.ClientSession,
    *,
    auth0_base: str,
    audience: str,
    client_id: str,
    email: str,
    password: str,
) -> Auth0Credentials:
    """Run the password-realm grant and return the resulting credentials."""
    body = {
        "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
        "username": email,
        "password": password,
        "audience": audience,
        "scope": "openid profile email read:current_user offline_access",
        "client_id": client_id,
        "realm": "Username-Password-Authentication",
    }
    async with session.post(f"{auth0_base}/oauth/token", json=body) as resp:
        if resp.status in (401, 403):
            raise Auth0Error(f"Auth0 rejected credentials: {await resp.text()}")
        if resp.status != 200:
            raise Auth0Error(f"Auth0 {resp.status}: {await resp.text()}")
        return Auth0Credentials(await resp.json())
