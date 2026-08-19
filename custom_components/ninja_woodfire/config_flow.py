"""Config flow: collect Ninja-account credentials, list grills, pick one."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._lib.api.ayla import AuthError, AylaCloudClient, TransportError
from ._lib.const import REGION_DEFAULTS, make_region

from .const import (
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_DSN,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

def _user_schema(show_advanced: bool) -> vol.Schema:
    """The sign-in form.

    Everyone gets account details and a region. The credential overrides are
    an escape hatch for one situation — SharkNinja rotating the app
    identifiers this integration bundles, which would otherwise break sign-in
    for everybody until a release goes out. Users can then extract fresh
    values with `scripts/extract_credentials.py` and paste them here.

    That is rare enough, and obscure enough, that it belongs behind Home
    Assistant's Advanced Mode rather than on the form every user sees when
    setting up a barbecue. Leaving them off the standard form also means an
    ordinary setup is never asked for something that looks like a secret.

    Omitted fields simply fall back to the bundled defaults — see `_opt`.
    """
    schema: dict[Any, Any] = {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): vol.In(
            list(REGION_DEFAULTS.keys())
        ),
    }
    if show_advanced:
        schema.update(
            {
                vol.Optional(CONF_AUTH0_AUDIENCE, default=""): str,
                vol.Optional(CONF_AUTH0_CLIENT_ID, default=""): str,
                vol.Optional(CONF_AYLA_APP_ID, default=""): str,
                vol.Optional(CONF_AYLA_APP_SECRET, default=""): str,
            }
        )
    return vol.Schema(schema)


def _opt(value: str | None) -> str | None:
    """Treat empty strings as 'not provided' so make_region() falls back."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class NinjaWoodfireConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] | None = None
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            region = make_region(
                user_input.get(CONF_REGION, DEFAULT_REGION),
                auth0_audience=_opt(user_input.get(CONF_AUTH0_AUDIENCE)),
                auth0_client_id=_opt(user_input.get(CONF_AUTH0_CLIENT_ID)),
                ayla_app_id=_opt(user_input.get(CONF_AYLA_APP_ID)),
                ayla_app_secret=_opt(user_input.get(CONF_AYLA_APP_SECRET)),
            )
            client = AylaCloudClient(
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                region=region,
                session=async_get_clientsession(self.hass),
            )
            try:
                await client.login()
                self._devices = await client.get_devices()
            except AuthError:
                errors["base"] = "invalid_auth"
            except TransportError as err:
                _LOGGER.warning("transport error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected during login/get_devices")
                errors["base"] = "unknown"
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    self._user_input = user_input
                    return await self.async_step_pick_device()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(self.show_advanced_options),
            errors=errors,
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            dsn = user_input[CONF_DSN]
            await self.async_set_unique_id(dsn)
            self._abort_if_unique_id_configured()

            device = next((d for d in self._devices if d.get("dsn") == dsn), {})
            title = device.get("product_name") or f"Ninja Woodfire {dsn}"
            assert self._user_input is not None
            data = {
                CONF_EMAIL: self._user_input[CONF_EMAIL],
                CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                CONF_REGION: self._user_input.get(CONF_REGION, DEFAULT_REGION),
                CONF_DSN: dsn,
            }
            # Persist credential overrides only when the user actually
            # supplied non-empty values. Otherwise we keep the entry
            # clean and the integration tracks vendor defaults.
            for key in (
                CONF_AUTH0_AUDIENCE,
                CONF_AUTH0_CLIENT_ID,
                CONF_AYLA_APP_ID,
                CONF_AYLA_APP_SECRET,
            ):
                v = _opt(self._user_input.get(key))
                if v:
                    data[key] = v
            return self.async_create_entry(title=title, data=data)

        choices = {
            d.get("dsn", ""): f"{d.get('product_name') or d.get('oem_model') or 'Grill'} ({d.get('dsn')})"
            for d in self._devices
        }
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(CONF_DSN): vol.In(choices)}),
        )
