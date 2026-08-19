"""Start / stop cook buttons."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NinjaWoodfireCoordinator], Awaitable[None]]
    # True for commands that are only well-defined when the running cook can
    # actually be read. Not a connectivity check — a semantics check.
    needs_live_cook: bool = False


BUTTONS: tuple[NinjaButtonDescription, ...] = (
    NinjaButtonDescription(
        key="start_cook",
        translation_key="start_cook",
        icon="mdi:play",
        press_fn=lambda c: c.async_start_cook(),
    ),
    NinjaButtonDescription(
        key="stop_cook",
        translation_key="stop_cook",
        icon="mdi:stop",
        press_fn=lambda c: c.async_stop_cook(),
    ),
    NinjaButtonDescription(
        key="skip_preheat",
        translation_key="skip_preheat",
        icon="mdi:fast-forward",
        # There is no dedicated skip-preheat command in the firmware: this
        # re-issues the *entire* cook payload with "skip preheat" set. Doing
        # that without being able to read the running cook would replace the
        # user's actual cook with whatever happens to be staged, silently
        # changing mode, temperature and duration mid-cook. Better unavailable.
        needs_live_cook=True,
        press_fn=lambda c: c.async_skip_preheat(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinjaButton(coordinator, desc) for desc in BUTTONS)


class NinjaButton(NinjaWoodfireEntity, ButtonEntity):
    """A cook command.

    Stays available even when the read path is dead, because the two
    directions fail independently: the cloud delivers datapoint writes to the
    grill (confirmed on an OG900-EU from mobile data alone, with the grill
    reporting `connection_status: Offline` throughout) while that same grill
    never publishes state back. Gating commands on readable state would
    disable the half of the integration that still works.
    """

    _requires_live_state = False
    entity_description: NinjaButtonDescription

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.entity_description.needs_live_cook:
            return self.coordinator.state_is_live
        return True

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
