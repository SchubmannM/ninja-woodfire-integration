"""Lid + probe-plugged-in binary sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._lib.models import CombinedState

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[CombinedState], bool | None]


BINARY_SENSORS: tuple[NinjaBinarySensorDescription, ...] = (
    NinjaBinarySensorDescription(
        key="lid_open",
        translation_key="lid_open",
        device_class=BinarySensorDeviceClass.OPENING,
        value_fn=lambda s: s.grill.lid_open,
    ),
    NinjaBinarySensorDescription(
        key="probe0_plugged_in",
        translation_key="probe0_plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda s: s.probes.probes[0].plugged_in if len(s.probes.probes) > 0 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe1_plugged_in",
        translation_key="probe1_plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda s: s.probes.probes[1].plugged_in if len(s.probes.probes) > 1 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe0_active",
        translation_key="probe0_active",
        value_fn=lambda s: s.probes.probes[0].active if len(s.probes.probes) > 0 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe1_active",
        translation_key="probe1_active",
        value_fn=lambda s: s.probes.probes[1].active if len(s.probes.probes) > 1 else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        NinjaBinarySensor(coordinator, desc) for desc in BINARY_SENSORS
    ]
    entities.append(NinjaConnectivitySensor(coordinator))
    async_add_entities(entities)


class NinjaBinarySensor(NinjaWoodfireEntity, BinarySensorEntity):
    entity_description: NinjaBinarySensorDescription

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NinjaConnectivitySensor(NinjaWoodfireEntity, BinarySensorEntity):
    """Whether the grill currently has a session with the cloud.

    Deliberately the one entity that stays available when everything else
    goes away — it is how the user tells "my grill is not reachable" apart
    from "the integration is broken". Every other state entity is
    unavailable in exactly that situation.
    """

    _requires_live_state = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cloud_connected"

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.dsn}_cloud_connected"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.online

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.coordinator.data
        if state is None:
            return None
        age = state.state_age_seconds()
        return {
            "connection_status": state.connection_status,
            "last_report_age_seconds": None if age is None else int(age),
            "state_is_live": self.coordinator.state_is_live,
        }
