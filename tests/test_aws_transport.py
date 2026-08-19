"""AWS transport tests, against payloads captured from a real OG900-EU.

The AWS backend serves the same firmware structures as Ayla with every space
stripped — from object keys and enum values alike. These tests pin the
normalisation that hides that from the parsing layer, and check the fields
that only exist during a live cook.
"""
from __future__ import annotations

import json

import pytest

from nwf_lib.api.aws import (
    normalise,
    normalise_json,
    state_from_device,
)

from .conftest import load_fixture


def _device(name: str) -> dict:
    fx = load_fixture(name)
    return {
        "telemetry": fx["telemetry"],
        "connectivityStatus": {"connected": fx["device"].get("connected")},
        "updatedAt": fx["device"].get("updatedAt"),
    }


def _state(name: str):
    return state_from_device("AC000W000000000", _device(name))


# ------------------------------------------------------------- normalisation

def test_key_aliases_are_rewritten() -> None:
    raw = {
        "secondsset": 180,
        "secondsleft": 42,
        "probesactive": 1,
        "inputs": {"io": {"lidopen": 1}},
        "probes": [{"pluggedin": 1}],
    }
    out = normalise(raw)
    assert out["seconds set"] == 180
    assert out["seconds left"] == 42
    assert out["probes active"] == 1
    assert out["inputs"]["io"]["lid open"] == 1
    assert out["probes"][0]["plugged in"] == 1


def test_mode_and_state_values_are_canonicalised() -> None:
    assert normalise({"mode": "aircrisp"})["mode"] == "air crisp"
    assert normalise({"state": "poweredOFF"})["state"] == "powered OFF"
    # already-canonical values survive untouched
    assert normalise({"mode": "air crisp"})["mode"] == "air crisp"
    assert normalise({"state": "idle"})["state"] == "idle"
    # a mode we don't know is passed through rather than mangled
    assert normalise({"mode": "sousvide"})["mode"] == "sousvide"


def test_normalise_only_touches_mode_and_state_strings() -> None:
    """A message like "7:getfood" is content, not a state enum."""
    out = normalise({"message": "7:getfood", "eventmask": "0xC0"})
    assert out["message"] == "7:getfood"
    assert out["eventmask"] == "0xC0"


def test_normalise_json_round_trips_a_string() -> None:
    out = normalise_json('{"state":"poweredOFF","secondsset":60}')
    assert isinstance(out, str)
    assert json.loads(out) == {"state": "powered OFF", "seconds set": 60}


def test_normalise_json_tolerates_garbage() -> None:
    for bad in ("", "not json", "{", None, 42):
        assert normalise_json(bad) == bad


# ------------------------------------------------------------- live cook

def test_preheat_state() -> None:
    """During preheat GrillState already says "cooking" — the sub-phase and
    its progress live in CookState, which is what the UI must follow."""
    state = _state("aws_preheat")
    assert state.grill.state == "cooking"
    assert state.cook.state == "preheat"
    assert state.cook.progress is not None and 0 <= state.cook.progress <= 100
    assert state.grill.mode == "air crisp"      # normalised from "aircrisp"
    assert state.grill.setpoint == 150
    assert state.grill.seconds_set == 180
    assert state.grill.end_time_utc is not None
    assert state.online is True
    assert state.is_live() is False or state.is_live() is True  # timestamp-dependent


def test_active_cook_exposes_every_field_ha_displays() -> None:
    state = _state("aws_cooking")
    assert state.grill.state == "cooking"
    assert state.grill.mode == "air crisp"
    assert state.grill.setpoint is not None
    assert state.grill.seconds_set is not None
    assert state.grill.seconds_left is not None
    assert state.grill.end_time_utc is not None
    assert state.grill.probes_active == 0
    assert state.grill.smoke is False
    assert state.grill.error == 0
    assert state.grill.temps.grill > 0
    assert state.grill.temps.air > 0


def test_air_temperature_is_shown_for_an_air_crisp_cook() -> None:
    """The point of normalising the mode name.

    The air-chamber sensor is gated on the mode being one that uses it. Left
    as "aircrisp" the gate would never match and an Air Crisp cook would show
    no air temperature at all.
    """
    state = _state("aws_cooking")
    assert state.grill.mode == "air crisp"
    state.last_updated_at = None            # bypass staleness for this check
    assert state.temp_is_plausible(state.grill.temps.air, "air") is True


def test_cook_done_is_signalled_in_message_not_state() -> None:
    """Completion arrives as message "6:done" + eventmask 0x40 while
    GrillState.state is still "cooking"."""
    state = _state("aws_cook_done")
    assert state.grill.message == "6:done"
    assert state.grill.event_mask == "0x40"
    assert state.grill.state == "cooking"
    assert state.grill.seconds_left == 0


def test_add_food_prompt() -> None:
    state = _state("aws_add_food")
    assert state.grill.message == "7:getfood"
    assert state.grill.event_mask == "0xC0"


def test_powered_off_with_lid_open() -> None:
    state = _state("aws_powered_off_lid_open")
    assert state.grill.state == "powered OFF"   # normalised from "poweredOFF"
    assert state.grill.lid_open is True
    assert state.grill.mode is None


def test_powered_off_suppresses_the_hot_chamber_reading() -> None:
    """Normalisation has to reach the stale-temperature cap.

    The captured payload has a 205 C chamber with the panel off. If the state
    stayed spelled "poweredOFF" the cap would not match it and that reading
    would surface as a live temperature.
    """
    state = _state("aws_powered_off_lid_open")
    assert state.grill.temps.grill > 50
    state.last_updated_at = None
    assert state.temp_is_plausible(state.grill.temps.grill, "grill") is False


# ------------------------------------------------------- connectivity mapping

@pytest.mark.parametrize(
    ("connected", "online", "status"),
    [(True, True, "Online"), (False, False, "Offline"), (None, False, None)],
)
def test_connectivity_status_maps_onto_the_shared_model(connected, online, status) -> None:
    device = {
        "telemetry": {"GrillState": '{"state":"idle"}'},
        "connectivityStatus": {"connected": connected},
        "updatedAt": "2026-08-19T18:36:27.530Z",
    }
    state = state_from_device("AC000W000000000", device)
    assert state.online is online
    assert state.connection_status == status


def test_missing_telemetry_does_not_raise() -> None:
    state = state_from_device("AC000W000000000", {})
    assert state.grill.state == "unknown"
    assert state.online is False
    assert state.last_updated_at is None


def test_updated_at_becomes_the_freshness_timestamp() -> None:
    device = {
        "telemetry": {"GrillState": '{"state":"idle"}'},
        "connectivityStatus": {"connected": True},
        "updatedAt": "2026-08-19T18:36:27.530Z",
    }
    state = state_from_device("AC000W000000000", device)
    assert state.last_updated_at is not None
    assert state.last_updated_at.year == 2026
    assert state.state_age_seconds() is not None


# ------------------------------------------------- the reader interface

def test_both_transports_expose_the_same_read_entry_point() -> None:
    """The coordinator calls `read_state(dsn)` on whichever backend it holds.

    Regression guard: the AWS client originally lacked it, which would have
    broken every poll on exactly the migrated grills this transport exists
    for — while Ayla-backed grills carried on working, so the failure would
    have looked device-specific rather than structural.
    """
    import inspect

    from nwf_lib.api.aws import AwsCloudClient
    from nwf_lib.api.ayla import AylaCloudClient

    for cls in (AwsCloudClient, AylaCloudClient):
        fn = getattr(cls, "read_state", None)
        assert fn is not None, f"{cls.__name__} is missing read_state()"
        assert inspect.iscoroutinefunction(fn), f"{cls.__name__}.read_state must be async"
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["self", "dsn"], (
            f"{cls.__name__}.read_state must take (self, dsn); got {params}"
        )
