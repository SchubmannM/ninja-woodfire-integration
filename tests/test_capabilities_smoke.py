"""Smoke: supported, defaulted, or compulsory — three different things.

The phone app greys the Woodfire toggle out in Smoker and labels it
ACTIVATED rather than offering it as a choice. The capability table only knew
"supported" and "on by default", so the integration would happily compose
Smoker with smoke off — a combination the appliance does not offer, which the
firmware either rejects outright or ignores while the card goes on reporting
that the grill is not smoking.

`smoke_locked` is that third state. These tests pin the three places it has
to be honoured, because each was written before it existed.
"""
from __future__ import annotations

import pytest

from nwf_lib.capabilities import for_oem_model


CAPS = for_oem_model("OG900-EU")


def mode(name: str):
    m = CAPS.get_mode(name)
    assert m is not None, name
    return m


def test_smoker_requires_smoke() -> None:
    assert mode("smoker").smoke_locked is True
    assert mode("smoker").supports_smoke is True


def test_no_other_mode_forces_it() -> None:
    """Only the one the app actually locks. Grill and bake offer smoke as a
    choice and must go on offering it."""
    locked = [m.name for m in CAPS.modes if m.smoke_locked]
    assert locked == ["smoker"]


def test_locked_implies_supported() -> None:
    """A mode that requires smoke but claims not to support it would make the
    switch unavailable *and* have the coordinator strip the flag."""
    for m in CAPS.modes:
        if m.smoke_locked:
            assert m.supports_smoke, m.name


def test_a_locked_mode_must_also_default_to_smoke() -> None:
    """The two flags answer different questions — "do we turn it on for you"
    and "may you turn it off" — but locked without default is incoherent.

    `select.py` snaps the staged value on a mode change using `smoke_default`
    alone. A mode that required smoke without defaulting to it would leave
    the staged setting reading off, the switch unavailable and therefore
    unfixable, and the payload sending on regardless: the card would state the
    opposite of what the grill is doing. Enforced here rather than worked
    around there, so the invariant lives in one place.
    """
    for m in CAPS.modes:
        if m.smoke_locked:
            assert m.smoke_default, m.name


@pytest.mark.parametrize("name", ["dehydrate", "reheat"])
def test_modes_without_smoke_are_untouched(name: str) -> None:
    m = CAPS.get_mode(name)
    if m is None:
        pytest.skip(f"{name} not offered by this model")
    assert m.supports_smoke is False
    assert m.smoke_locked is False


# ------------------------------------------- what actually reaches the grill

import asyncio  # noqa: E402

from .conftest import (  # noqa: E402
    FakeDevice, FakeHass, description, hydrate_aws, load_coordinator, load_platform,
)

coordinator_module = load_coordinator()
DOMAIN = "ninja_woodfire"
DSN = "AC000W000000000"


class RecordingCommander:
    """Stands in for the cloud client, keeping what it was asked to send."""

    def __init__(self) -> None:
        self.cooks: list[dict] = []

    async def start_cook(self, dsn, **kwargs):
        self.cooks.append(kwargs)
        return {}

    async def stop_cook(self, dsn):
        return {}


def make_coordinator() -> tuple:
    hass = FakeHass()
    hass.device_registry.add({(DOMAIN, DSN)}, FakeDevice("dev1", "WoodNinja"))
    commander = RecordingCommander()
    coord = coordinator_module.NinjaWoodfireCoordinator(
        hass, client=object(), dsn=DSN, capabilities=CAPS, device_key=1,
        commander=commander,
    )
    # The burst refresh polls the cloud three times with real sleeps in
    # between; none of that is what these tests are about.
    async def _no_refresh() -> None:
        return None
    coord._burst_refresh = _no_refresh
    return coord, commander


def test_starting_a_smoker_cook_sends_smoke_even_if_it_was_staged_off() -> None:
    """The switch is unavailable in this mode, but the staged value survives
    from whatever mode the user was in before — so the payload builder cannot
    assume it has already been corrected."""
    coord, commander = make_coordinator()
    coord.cook_setting_mode = "smoker"
    coord.cook_setting_smoke = False

    asyncio.run(coord.async_start_cook())

    assert commander.cooks[-1]["mode"] == "smoker"
    assert commander.cooks[-1]["smoke"] is True


def test_a_mode_that_merely_defaults_to_smoke_still_takes_no_for_an_answer() -> None:
    """The lock must not turn every default into a compulsion — grill offers
    smoke as a choice and has to keep offering it."""
    coord, commander = make_coordinator()
    coord.cook_setting_mode = "grill"
    coord.cook_setting_smoke = False

    asyncio.run(coord.async_start_cook())
    assert commander.cooks[-1]["smoke"] is False


def test_a_mode_without_smoke_still_never_gets_it() -> None:
    for name in ("dehydrate", "reheat"):
        if CAPS.get_mode(name) is None:
            continue
        coord, commander = make_coordinator()
        coord.cook_setting_mode = name
        coord.cook_setting_smoke = True
        asyncio.run(coord.async_start_cook())
        assert commander.cooks[-1]["smoke"] is False, name


# ------------------------------------------------ and what the user is shown

SWITCH = load_platform("switch")


class StagedCook:
    """Just the coordinator surface the switch's `available_fn` reads."""

    def __init__(self, mode: str):
        self.capabilities = CAPS
        self.live_or_staged_mode = mode


def smoke_switch_available(mode: str) -> bool:
    return description(SWITCH, "cook_smoke").available_fn(StagedCook(mode))


def test_the_smoke_switch_is_hidden_where_smoke_is_compulsory() -> None:
    """Not a decision the user gets to make, so it should not be offered as
    one — the card dims the row and its label, which is how they see why."""
    assert smoke_switch_available("smoker") is False


def test_it_is_hidden_where_smoke_is_impossible() -> None:
    for name in ("dehydrate", "reheat"):
        if CAPS.get_mode(name) is None:
            continue
        assert smoke_switch_available(name) is False, name


def test_it_is_offered_where_smoke_is_a_choice() -> None:
    assert smoke_switch_available("grill") is True
    assert smoke_switch_available("bake") is True


def test_an_unknown_mode_does_not_raise() -> None:
    """`live_or_staged_mode` comes off the wire; a mode this model's table has
    never heard of must not take the switch down with it."""
    assert smoke_switch_available("sousvide") is False


# ------------------------------- switching into a locked mode, and out again

def test_choosing_smoker_turns_smoke_on_for_you() -> None:
    """The select snaps the staged settings before sending, so the payload is
    never composed out of a combination the mode does not allow. Smoker is now
    the case where "on by default" is not enough — it has to be on, full
    stop."""
    select = load_platform("select")
    coord, commander = make_coordinator()
    coord.cook_setting_mode = "grill"
    coord.cook_setting_smoke = False

    entity = select.CookModeSelect.__new__(select.CookModeSelect)
    entity.coordinator = coord
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_select_option("smoker"))
    assert coord.cook_setting_smoke is True
    assert coord.cook_setting_mode == "smoker"


def test_leaving_smoker_for_a_mode_that_cannot_smoke_turns_it_back_off() -> None:
    select = load_platform("select")
    target = next((m.name for m in CAPS.modes if not m.supports_smoke), None)
    if target is None:
        pytest.skip("this model smokes in every mode")

    coord, _ = make_coordinator()
    coord.cook_setting_mode = "smoker"
    coord.cook_setting_smoke = True

    entity = select.CookModeSelect.__new__(select.CookModeSelect)
    entity.coordinator = coord
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_select_option(target))
    assert coord.cook_setting_smoke is False


def test_modifying_a_live_cook_into_smoker_keeps_smoke_on() -> None:
    """`async_modify_cook` clamps the proposal to the target mode before
    sending. Changing mode mid-cook with smoke staged off would otherwise
    compose the same impossible payload by the other route."""
    coord, commander = make_coordinator()
    coord.cook_setting_mode = "grill"
    coord.cook_setting_smoke = False
    coord.data = hydrate_aws("aws_cooking")

    asyncio.run(coord.async_modify_cook(mode="smoker", smoke=False))

    assert commander.cooks, "a live cook should have been re-issued"
    assert commander.cooks[-1]["mode"] == "smoker"
    assert commander.cooks[-1]["smoke"] is True
