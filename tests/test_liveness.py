"""Liveness tests — the actual root cause of the original bug report.

The Ayla cloud never stops answering. It keeps serving the last datapoint the
grill ever pushed, with no indication of its age, so a successful poll proves
only that the *cloud* is up. On an OG900-EU the Wi-Fi module pushes a snapshot
when it connects and then goes quiet (live state goes to LAN/BLE clients
only), which means the cloud copy can be a day stale while the grill is
mid-cook.

These tests pin down the resulting contract: a snapshot only counts as
describing the grill when the grill is connected *and* its last report is
recent.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from nwf_lib.models import ACTIVE_COOK_STATES, CombinedState

from .conftest import hydrate, hydrate_as_captured


# ------------------------------------------------- the reported bug, verbatim

def test_bug_report_reproduces_from_the_captured_snapshot() -> None:
    """The exact snapshot HA was showing: 'idle', from an offline grill, 22h old.

    Before the fix nothing here was detectable — `online` defaulted to True and
    nothing looked at the timestamp, so HA rendered a tidy idle grill while the
    real one was running an Air Fry cook.
    """
    state = hydrate_as_captured("idle_offline")

    assert state.grill.state == "idle"           # what HA displayed
    assert state.connection_status == "Offline"   # what the cloud actually said
    assert state.online is False

    age = state.state_age_seconds()
    assert age is not None and age > 20 * 3600    # ~22.7h at capture time

    # The contract that fixes the bug: this snapshot is not live.
    assert state.is_live() is False
    assert state.is_stale() is True


def test_offline_snapshot_suppresses_every_temperature() -> None:
    """An offline grill's cached chamber readings must not surface as live."""
    state = hydrate_as_captured("idle_offline")
    for name, value in (
        ("grill", state.grill.temps.grill),
        ("air", state.grill.temps.air),
        ("smoke", state.grill.temps.smoke),
    ):
        assert value > 0, f"{name}: fixture should carry a leftover reading"
        assert state.temp_is_plausible(value, name) is False, name


# ------------------------------------------------------------ is_live contract

def test_is_live_requires_both_connected_and_fresh() -> None:
    """Neither signal is sufficient alone.

    'Connected' says nothing about freshness on this grill, and a recent
    timestamp says nothing when the module has since dropped off.
    """
    assert hydrate("idle_offline", online=True, age_seconds=0).is_live() is True
    # connected but frozen — the grill pushed once on connect, then went quiet
    assert hydrate("idle_offline", online=True, age_seconds=3600).is_live() is False
    # fresh timestamp but the grill is gone
    assert hydrate("idle_offline", online=False, age_seconds=0).is_live() is False
    assert hydrate("idle_offline", online=False, age_seconds=3600).is_live() is False


def test_is_live_window_is_honoured() -> None:
    state = hydrate("idle_offline", online=True, age_seconds=120)
    assert state.is_live(300) is True
    assert state.is_live(60) is False


def test_unknown_timestamp_is_not_treated_as_stale() -> None:
    """No timestamp means 'can't tell' — stay lenient rather than blanking out."""
    state = hydrate("idle_offline", online=True)
    state.last_updated_at = None
    assert state.state_age_seconds() is None
    assert state.is_stale() is False
    assert state.is_live() is True


def test_online_defaults_optimistic_but_status_defaults_unknown() -> None:
    """A snapshot built without a device record can't claim to know.

    `get_combined_state` leaves `online` at its optimistic default when no
    device record was supplied; `connection_status` staying None is what tells
    a caller that connectivity was never actually established.
    """
    bare = CombinedState(dsn="AC000W000000000")
    assert bare.online is True
    assert bare.connection_status is None


# ------------------------------------------------------- stale-temperature cap

def test_powered_off_is_covered_by_the_idle_temperature_cap() -> None:
    """Regression: the cap used to test `state == "idle"` only.

    The firmware also reports "powered OFF" (plugged in, control panel off),
    and a real capture in that state carried a leftover 82.4 °C chamber
    reading — which sailed straight through to HA as a live temperature.
    """
    state = hydrate("powered_off", online=True, age_seconds=0)
    assert state.grill.state == "powered OFF"
    assert state.grill.temps.grill > 50
    assert state.temp_is_plausible(state.grill.temps.grill, "grill") is False


def test_idle_cap_does_not_hide_a_genuinely_hot_finished_cook() -> None:
    """"done" is not an idle state — the chamber really is still hot."""
    state = hydrate("idle_offline", online=True, age_seconds=0)
    state.grill.state = "done"
    assert state.temp_is_plausible(220.0, "grill") is True


def test_active_cook_temperature_passes_the_cap() -> None:
    state = hydrate("idle_offline", online=True, age_seconds=0)
    state.grill.state = "cooking"
    state.grill.mode = "air crisp"
    assert state.temp_is_plausible(198.0, "grill") is True
    assert state.temp_is_plausible(201.0, "air") is True


def test_chamber_gating_still_applies_during_a_cook() -> None:
    """Mode-irrelevant chambers stay hidden even when the data is live."""
    state = hydrate("idle_offline", online=True, age_seconds=0)
    state.grill.state = "cooking"
    state.grill.mode = "air crisp"
    state.grill.smoke = False
    # smoke chamber is meaningless with the woodfire box off
    assert state.temp_is_plausible(227.1, "smoke") is False
    state.grill.smoke = True
    assert state.temp_is_plausible(227.1, "smoke") is True
    # air chamber is meaningless in a mode that doesn't use it
    state.grill.mode = "grill"
    assert state.temp_is_plausible(180.0, "air") is False


# ------------------------------------------- transport populates connectivity

def _fake_props(grill_value: str, updated_at: str) -> list[dict]:
    return [
        {"name": "GET_GrillState", "value": grill_value, "data_updated_at": updated_at},
        {"name": "GET_CookState", "value": '{"state":{"state":"none"}}',
         "data_updated_at": updated_at},
        {"name": "GET_ProbeState", "value": '{"probes":[]}',
         "data_updated_at": updated_at},
    ]


def _client():
    from nwf_lib.api.ayla import AylaCloudClient
    return AylaCloudClient("someone@example.invalid", "unused")


@pytest.mark.parametrize(
    ("status", "expect_online"),
    [("Online", True), ("Offline", False), ("online", True), (None, False)],
)
def test_get_combined_state_maps_connection_status(status, expect_online) -> None:
    client = _client()
    now = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def fake_get_properties(dsn, names=None):
        return _fake_props('{"state":"idle"}', now)

    client.get_properties = fake_get_properties
    state = asyncio.run(
        client.get_combined_state("AC000W000000000", device={"connection_status": status})
    )
    assert state.online is expect_online
    assert state.connection_status == status


def test_get_combined_state_without_device_record_leaves_status_unknown() -> None:
    client = _client()
    now = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def fake_get_properties(dsn, names=None):
        return _fake_props('{"state":"idle"}', now)

    client.get_properties = fake_get_properties
    state = asyncio.run(client.get_combined_state("AC000W000000000"))
    assert state.connection_status is None


def test_literal_null_timestamp_is_not_parsed_as_a_date() -> None:
    """Ayla sends the string "null" for never-reported properties.

    33 of this device's 48 properties are in that state, so the parse has to
    treat it as absent rather than as something to try `fromisoformat` on.
    """
    client = _client()

    async def fake_get_properties(dsn, names=None):
        return _fake_props('{"state":"idle"}', "null")

    client.get_properties = fake_get_properties
    state = asyncio.run(client.get_combined_state("AC000W000000000", device={}))
    assert state.last_updated_at is None


# -------------------------------------------------- single active-state source

def test_active_state_vocabulary_has_one_definition() -> None:
    """The HA layer must re-export the canonical set, not keep a second copy.

    They had already drifted: the HA copy was missing "heat", so a cook in the
    firmware's `heat` phase did not trigger fast polling.
    """
    for phase in ("preheat", "heat", "cooking", "rest", "flip", "lid open"):
        assert phase in ACTIVE_COOK_STATES, phase
    for phase in ("idle", "none", "done", "powered OFF"):
        assert phase not in ACTIVE_COOK_STATES, phase


# --------------------------------------------- reads and writes fail apart

def test_command_availability_is_independent_of_read_liveness() -> None:
    """Confirmed on real hardware: commands land while state never arrives.

    An OG900-EU accepted cook commands sent through the cloud from mobile data
    alone, while reporting `connection_status: Offline` and publishing no state
    for 1306 consecutive polls. So an unreadable grill is not an
    uncommandable one, and the platform tables must reflect that.

    Checked against the entity descriptions rather than by importing the HA
    platform modules, which need Home Assistant present.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    button_src = (root / "custom_components" / "ninja_woodfire" / "button.py").read_text()

    # Commands must not be gated on the read path at the class level.
    assert "_requires_live_state = False" in button_src, (
        "cook commands must stay available when state is unreadable — "
        "the write path works independently of the read path"
    )

    # ...but skip-preheat must be, because it re-issues the whole cook payload
    # and needs the running cook to preserve it.
    assert "needs_live_cook=True" in button_src
    skip = button_src[button_src.index('key="skip_preheat"'):]
    assert "needs_live_cook=True" in skip[:600], (
        "skip_preheat re-issues the entire cook payload; without a readable "
        "cook it would overwrite mode/temp/duration with staged values"
    )
    # Start and stop are well-defined without live state and must not be gated.
    for key in ('key="start_cook"', 'key="stop_cook"'):
        block = button_src[button_src.index(key):]
        block = block[:block.index("),")]
        assert "needs_live_cook" not in block, f"{key} should not be gated"


def test_staged_settings_survive_an_unreadable_grill() -> None:
    """Staging a cook must work with no read path at all.

    It is the only way to drive the grill from HA when state never arrives,
    so the staged values live in the coordinator and depend on nothing
    the grill reports.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for platform in ("number.py", "select.py", "switch.py"):
        src = (root / "custom_components" / "ninja_woodfire" / platform).read_text()
        assert "_requires_live_state = False" in src, (
            f"{platform}: staged cook settings must not require live state"
        )
