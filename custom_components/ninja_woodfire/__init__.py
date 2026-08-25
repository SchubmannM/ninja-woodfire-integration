"""Ninja Woodfire HA integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._lib.api.aws import AwsCloudClient, AwsCloudError
from ._lib.api.ayla import AuthError, AylaCloudClient, TransportError
from ._lib.const import BACKEND_AWS, BACKEND_AYLA, make_region

from .const import (
    CONF_AWS_API_BASE,
    CONF_AWS_API_KEY,
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_DSN,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import NinjaWoodfireCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Each credential field is optional in the entry — when absent, the
    # bundled per-region default is used. This makes existing setups
    # automatically pick up new defaults if vendor identifiers rotate.
    region = make_region(
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        auth0_audience=entry.data.get(CONF_AUTH0_AUDIENCE),
        auth0_client_id=entry.data.get(CONF_AUTH0_CLIENT_ID),
        ayla_app_id=entry.data.get(CONF_AYLA_APP_ID),
        ayla_app_secret=entry.data.get(CONF_AYLA_APP_SECRET),
        aws_rest_base=entry.data.get(CONF_AWS_API_BASE),
        aws_api_key=entry.data.get(CONF_AWS_API_KEY),
    )

    client = AylaCloudClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        region=region,
        session=async_get_clientsession(hass),
    )

    from ._lib.capabilities import for_oem_model

    try:
        await client.login()
        devices = await client.get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TransportError as err:
        raise ConfigEntryNotReady(str(err)) from err

    dsn = entry.data[CONF_DSN]
    device = next((d for d in devices if d.get("dsn") == dsn), None)
    if device is None:
        raise ConfigEntryNotReady(f"device {dsn} not found in account")
    capabilities = for_oem_model(device.get("oem_model"))
    device_key = int(device.get("key", 0))
    if not device_key:
        raise ConfigEntryNotReady(f"device {dsn} has no device key")

    # Pick where state is read from. SharkNinja is migrating grills onto their
    # own AWS-backed service; a migrated grill stops publishing to Ayla
    # entirely, so Ayla would keep serving whatever snapshot it last received
    # — indefinitely, and with no error to notice. Commands are unaffected and
    # continue to go through Ayla either way.
    reader: Any = client
    commander: Any = client
    backend = BACKEND_AYLA
    aws_client = AwsCloudClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        region=region,
        session=async_get_clientsession(hass),
    )
    try:
        await aws_client.login()
        if await aws_client.find_device(dsn) is not None:
            reader = aws_client
            commander = aws_client
            backend = BACKEND_AWS
            _LOGGER.info(
                "ninja_woodfire %s: grill found on SharkNinja's AWS backend; "
                "reading state and sending commands there. Ayla is not used — "
                "a migrated grill never acknowledges Ayla datapoints.",
                dsn,
            )
        else:
            _LOGGER.debug(
                "ninja_woodfire %s: not present on the AWS backend, "
                "reading state from Ayla", dsn,
            )
    except AwsCloudError as err:
        # Not fatal: an account that has never been migrated has no AWS
        # presence at all, and Ayla remains perfectly good for those.
        #
        # It is worth more than a debug line though, because the other way to
        # land here is a deployment we have not seen. Only an EU AWS host has
        # ever been captured, so an account served by a different one fails
        # exactly like an unmigrated account — silently, with Ayla then
        # serving a frozen snapshot if the grill has in fact been migrated.
        # Say so once, and point at the override.
        _LOGGER.info(
            "ninja_woodfire %s: could not reach the AWS backend (%s); reading "
            "state from Ayla. Normal if this grill has not been migrated. If "
            "it has — the app shows live data but Home Assistant does not — "
            "the AWS host may differ for your region; it can be overridden in "
            "the integration's advanced setup options.",
            dsn, err,
        )

    coordinator = NinjaWoodfireCoordinator(
        hass=hass,
        client=client,
        dsn=dsn,
        capabilities=capabilities,
        device_key=device_key,
        reader=reader,
        commander=commander,
        backend=backend,
        device_info_extra={
            "oem_model": str(device.get("oem_model", "")),
            "model": str(device.get("model", "")),
            "sw_version": str(device.get("sw_version", "")),
            "product_name": str(device.get("product_name") or capabilities.display_name),
        },
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
