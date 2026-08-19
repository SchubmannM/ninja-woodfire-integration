"""Common base class for Ninja Woodfire entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator


class NinjaWoodfireEntity(CoordinatorEntity[NinjaWoodfireCoordinator]):
    _attr_has_entity_name = True

    # Whether this entity describes what the grill is doing right now.
    #
    # The cloud never stops answering: it keeps serving the last datapoint the
    # grill pushed, with no hint of its age. A successful poll therefore says
    # nothing about whether the grill is reachable, so entities that mirror
    # grill state must also require a *live* snapshot — otherwise a grill that
    # dropped off the cloud yesterday still renders as a tidy idle grill.
    #
    # Set False for entities that remain meaningful without one: locally
    # staged cook settings, static device info, the connectivity diagnostics
    # whose whole job is to report that the grill is away, and — importantly —
    # anything that *commands* the grill.
    #
    # Reads and writes fail independently on this hardware. A grill can be
    # unreachable for state (never publishes to the cloud) while still acting
    # on commands (the cloud delivers datapoint writes to it). So this flag
    # governs "can I trust what I am displaying", never "can I send this".
    _requires_live_state: bool = True

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.device_info_extra
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.dsn)},
            name=info.get("product_name") or coordinator.capabilities.display_name,
            manufacturer="SharkNinja",
            model=info.get("oem_model") or coordinator.capabilities.display_name,
            sw_version=info.get("sw_version") or None,
            serial_number=coordinator.dsn,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._requires_live_state:
            return self.coordinator.state_is_live
        return True
