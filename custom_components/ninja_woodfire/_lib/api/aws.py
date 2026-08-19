"""Cloud transport for SharkNinja's AWS-backed IoT service.

This is the backend a grill moves to once the mobile app sets `Cloud_Mode = 1`
on its device shadow. Such a grill stops publishing state to Ayla completely,
so for it this transport is the only way to read anything current.

Shape of the exchange, all verified against a real OG900-EU:

    POST {auth0}/oauth/token                    -> id_token (same grant Ayla uses)
    GET  {rest}/householdsEndUser?userId=...    -> {"households": [...]}
    GET  {rest}/devicesEndUserController/{household}/users/{user}
              ?includeRegistry=true&includeConnectivityStatus=true
         -> items[].telemetry / .connectivityStatus / .registry / .updatedAt

Unlike Ayla there is no per-property fetch: one request returns every device
with its current telemetry, so a poll is a single round-trip.

Writes are deliberately absent. The command payload is visible in the shadow's
`desired.Cook_Command`, but the endpoint that sets it was never observed, and
guessing at the write path of a heating appliance is not acceptable. Commands
continue to go through the Ayla transport, which works on migrated grills.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..const import (
    AWS_CALLER,
    AWS_TELEMETRY_COOK_STATE,
    AWS_TELEMETRY_GRILL_STATE,
    AWS_TELEMETRY_PROBE_STATE,
    CloudRegion,
    COOK_MODES,
    make_region,
)
from ..models import CombinedState, CookState, GrillState, ProbeState
from .auth0 import Auth0Error, password_grant

_LOGGER = logging.getLogger(__name__)


class AwsCloudError(Exception):
    """Base exception."""


class AwsAuthError(AwsCloudError):
    """Bad credentials or expired session."""


class AwsTransportError(AwsCloudError):
    """Network / 5xx / unexpected payload."""


# --------------------------------------------------------------- normalisation
#
# The two backends serve the same firmware structures with different spelling:
# AWS strips every space, from object keys and from enum values alike. Rather
# than teach the parsing layer two dialects, the wire format is normalised here
# to the Ayla spelling — the transport's job is to speak its own protocol and
# hand up something canonical.
#
# Keys, as captured:  seconds set/left, probes active, lid open, plugged in.
# Values, as captured: mode "air crisp" -> "aircrisp",
#                      state "powered OFF" -> "poweredOFF".
_KEY_ALIASES = {
    "secondsset": "seconds set",
    "secondsleft": "seconds left",
    "probesactive": "probes active",
    "lidopen": "lid open",
    "pluggedin": "plugged in",
    "presetindex": "preset_index",
}

# Canonical state spellings, keyed by their space-stripped form.
_STATE_ALIASES = {
    "poweredoff": "powered OFF",
    "getfood": "get food",
    "lidopen": "lid open",
    "zcloss": "zc loss",
}

# Canonical cook-mode spellings, derived from the single source of truth so a
# mode added to COOK_MODES is handled here automatically.
_MODE_ALIASES = {m.replace(" ", "").casefold(): m for m in COOK_MODES}


def _canonical_state(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _STATE_ALIASES.get(value.replace(" ", "").casefold(), value)


def _canonical_mode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _MODE_ALIASES.get(value.replace(" ", "").casefold(), value)


def normalise(node: Any, *, key: str | None = None) -> Any:
    """Recursively rewrite an AWS payload into the Ayla spelling.

    `key` is the name the node was reached under, which is what decides
    whether a string value is a cook mode, a state, or plain text.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in node.items():
            canon_key = _KEY_ALIASES.get(raw_key.replace(" ", "").casefold(), raw_key)
            out[canon_key] = normalise(raw_value, key=canon_key)
        return out
    if isinstance(node, list):
        return [normalise(item, key=key) for item in node]
    if isinstance(node, str):
        if key == "mode":
            return _canonical_mode(node)
        if key == "state":
            return _canonical_state(node)
    return node


def normalise_json(raw: Any) -> Any:
    """Normalise a JSON-encoded telemetry string, preserving its string-ness.

    Telemetry arrives as a JSON string exactly as Ayla serves it, so the
    result is re-encoded rather than returned as a dict — that keeps a single
    code path in the parsing layer, which accepts both but is exercised
    against strings everywhere else.
    """
    if isinstance(raw, str):
        try:
            return json.dumps(normalise(json.loads(raw)))
        except (ValueError, json.JSONDecodeError):
            return raw
    if isinstance(raw, dict):
        return normalise(raw)
    return raw


# ------------------------------------------------------------------- client

class AwsCloudClient:
    """Async client for the AWS-backed SharkNinja IoT service."""

    def __init__(
        self,
        email: str,
        password: str,
        region: CloudRegion | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._region = region if region is not None else make_region("EU")
        self._http = session
        self._owns_session = session is None
        self._id_token: str | None = None
        self._user_id: str | None = None
        self._expires_at: float = 0.0
        self._household_id: str | None = None

    async def __aenter__(self) -> "AwsCloudClient":
        if self._owns_session and self._http is None:
            self._http = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_session and self._http is not None:
            await self._http.close()
            self._http = None

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        try:
            creds = await password_grant(
                self._client(),
                auth0_base=self._region.auth0_base,
                audience=self._region.auth0_audience,
                client_id=self._region.auth0_client_id,
                email=self._email,
                password=self._password,
            )
        except Auth0Error as err:
            raise AwsAuthError(str(err)) from err
        self._id_token = creds.id_token
        self._user_id = creds.user_id
        # Prefer the token's own expiry claim; fall back to the grant's
        # expires_in. Refresh a minute early like the Ayla client does.
        exp = creds.claims.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            self._expires_at = float(exp) - 60
        else:
            self._expires_at = time.time() + creds.expires_in - 60
        if not self._user_id:
            raise AwsAuthError("Auth0 token carried no usable `sub` claim")

    async def _ensure_session(self) -> None:
        if self._id_token is None or self._expires_at <= time.time():
            await self.login()

    @property
    def user_id(self) -> str | None:
        return self._user_id

    # ------------------------------------------------------------------ http

    async def _get(self, path: str) -> Any:
        await self._ensure_session()
        url = f"{self._region.aws_rest_base}{path}"
        async with self._client().get(url, headers=self._headers()) as resp:
            if resp.status == 401:
                await self.login()
                async with self._client().get(url, headers=self._headers()) as retry:
                    if retry.status != 200:
                        raise AwsTransportError(
                            f"GET {path} {retry.status}: {await retry.text()}"
                        )
                    return await retry.json()
            if resp.status in (401, 403):
                raise AwsAuthError(f"GET {path} {resp.status}: {await resp.text()}")
            if resp.status != 200:
                raise AwsTransportError(f"GET {path} {resp.status}: {await resp.text()}")
            return await resp.json()

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "authorization": f"Bearer {self._id_token}",
            "content-type": "application/json",
            "x-api-key": self._region.aws_api_key,
            "x-iotn-caller": AWS_CALLER,
            "x-sn-nonce": "12345",
            "x-sn-date": datetime.now(tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z",
        }

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_session = True
        return self._http

    # ------------------------------------------------------------------ data

    async def get_households(self) -> list[str]:
        await self._ensure_session()
        payload = await self._get(f"/householdsEndUser?userId={self._user_id}")
        households = (payload or {}).get("households") or []
        return [str(h) for h in households]

    async def get_household_id(self) -> str:
        """The account's household, cached after the first lookup."""
        if self._household_id:
            return self._household_id
        households = await self.get_households()
        if not households:
            raise AwsCloudError("account has no household on the AWS backend")
        if len(households) > 1:
            _LOGGER.debug(
                "ninja_woodfire: account has %d households, using the first",
                len(households),
            )
        self._household_id = households[0]
        return self._household_id

    async def get_devices(self) -> list[dict[str, Any]]:
        """Every device on the account, with telemetry and connectivity."""
        household = await self.get_household_id()
        payload = await self._get(
            f"/devicesEndUserController/{household}/users/{self._user_id}"
            "?includeRegistry=true&includeConnectivityStatus=true"
        )
        items = (payload or {}).get("items")
        return list(items) if isinstance(items, list) else []

    async def find_device(self, identifier: str) -> dict[str, Any] | None:
        """Locate a device by AWS device id or by its Ayla DSN.

        Existing config entries store the Ayla DSN, which the AWS payload does
        not use as an identifier — but `registry.WiFiModuleSerialNumber` is the
        two joined as "<ayla-dsn>-<appliance-serial>", so an entry created
        against Ayla still resolves after the grill is migrated.
        """
        wanted = (identifier or "").strip()
        if not wanted:
            return None
        for device in await self.get_devices():
            if str(device.get("deviceId", "")) == wanted:
                return device
            registry = device.get("registry") or {}
            if str(registry.get("serialNumber", "")) == wanted:
                return device
            combined = str(registry.get("WiFiModuleSerialNumber", ""))
            if combined and wanted in combined.split("-"):
                return device
        return None

    async def get_combined_state(
        self, identifier: str, *, device: dict[str, Any] | None = None
    ) -> CombinedState:
        """Read the grill's current state in a single round-trip."""
        if device is None:
            device = await self.find_device(identifier)
        if device is None:
            raise AwsCloudError(f"device {identifier!r} not found on the AWS backend")
        return state_from_device(identifier, device)

    async def read_state(self, dsn: str) -> CombinedState:
        """One poll's worth of state — the transport-agnostic read entry point.

        The coordinator calls this on whichever backend it was given, so both
        clients must offer it. Unlike Ayla this needs no second request for
        connectivity: the device listing already carries `connectivityStatus`
        and `updatedAt` alongside the telemetry.
        """
        return await self.get_combined_state(dsn)


def state_from_device(dsn: str, device: dict[str, Any]) -> CombinedState:
    """Hydrate a CombinedState from one AWS device record.

    Kept module-level and free of I/O so it can be exercised directly against
    captured payloads.
    """
    telemetry = device.get("telemetry") or {}
    connectivity = device.get("connectivityStatus") or {}

    state = CombinedState(dsn=dsn)
    state.grill = GrillState.from_property_value(
        normalise_json(telemetry.get(AWS_TELEMETRY_GRILL_STATE))
    )
    state.cook = CookState.from_property_value(
        normalise_json(telemetry.get(AWS_TELEMETRY_COOK_STATE))
    )
    state.probes = ProbeState.from_property_value(
        normalise_json(telemetry.get(AWS_TELEMETRY_PROBE_STATE))
    )

    connected = connectivity.get("connected")
    state.online = bool(connected)
    state.connection_status = (
        "Online" if connected else "Offline" if connected is not None else None
    )
    # `updatedAt` is when the service last recorded a change for this device,
    # which is the AWS equivalent of Ayla's `data_updated_at`.
    state.last_updated_at = _parse_timestamp(device.get("updatedAt"))
    return state


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw or raw == "null":
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
