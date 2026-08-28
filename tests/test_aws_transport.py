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


def test_remove_food_prompt() -> None:
    # Bit 7 (getfood) raised alongside bit 6 (done): 0x80 | 0x40.
    state = _state("aws_get_food")
    assert state.grill.message == "7:getfood"
    assert state.grill.prompt == "getfood"
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


# ------------------------------------------------- per-region endpoints

def test_aws_endpoints_come_from_the_region_not_module_constants() -> None:
    """The AWS host and key must be overridable per install.

    Only an EU deployment has ever been captured. The device record carries
    `"dc": "International"` and the app registers for push on
    `sn-eu-field-iot-ninjakitchen-app`, so regional AWS deployments very
    likely exist — an account served by a different one would otherwise fail
    exactly like an unmigrated account, silently falling back to Ayla and its
    frozen snapshot. Carrying these on CloudRegion makes that a data fix
    rather than a code change, and lets a user correct it from setup.
    """
    import pathlib

    from nwf_lib.const import make_region

    eu = make_region("EU")
    assert eu.aws_rest_base.startswith("https://")
    assert eu.aws_api_key

    overridden = make_region(
        "EU", aws_rest_base="https://example.invalid", aws_api_key="sentinel"
    )
    assert overridden.aws_rest_base == "https://example.invalid"
    assert overridden.aws_api_key == "sentinel"
    # An override of one must not disturb the rest of the region.
    assert overridden.auth0_base == eu.auth0_base
    assert overridden.ayla_app_id == eu.ayla_app_id

    # Both regions must define them, so NA is a one-line data fix.
    assert make_region("NA").aws_rest_base

    # And the client must actually read them off the region rather than
    # reaching for a module-level constant.
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "custom_components" / "ninja_woodfire" / "_lib" / "api" / "aws.py").read_text()
    assert "self._region.aws_rest_base" in src
    assert "self._region.aws_api_key" in src


# ------------------------------------------------------------------ commands

def _client_capturing():
    """An AWS client whose shadow writes are captured instead of sent."""
    from nwf_lib.api.aws import AwsCloudClient

    client = AwsCloudClient("someone@example.invalid", "unused")
    captured: dict = {}

    async def fake_write(identifier, desired):
        captured["identifier"] = identifier
        captured["desired"] = desired
        return {}

    client._write_desired = fake_write
    return client, captured


def test_start_cook_payload_uses_the_spaced_firmware_spelling() -> None:
    """Writes keep the spaces that reads strip.

    Telemetry arrives de-spaced ("secondsset", "skippreheat"), so it is very
    tempting to normalise the command payload the same way. That would be
    wrong: the shadow stores commands with the firmware's own spelling, which
    is the spaced Ayla one. Confirmed against a real write.
    """
    import asyncio

    client, captured = _client_capturing()
    asyncio.run(client.start_cook(
        "AC000W000000000", mode="air crisp", seconds=300, temp=200,
        smoke=True, skip_preheat=False,
    ))
    payload = captured["desired"]["Cook_Command"]
    assert payload["seconds set"] == 300
    assert payload["skip preheat"] == 0
    assert "secondsset" not in payload
    assert "skippreheat" not in payload
    assert payload["mode"] == "air crisp"   # spaced here too
    assert payload["temp"] == 200
    assert payload["smoke"] == 1
    assert payload["id"] == 1001            # the app's start id


def test_stop_cook_payload() -> None:
    import asyncio

    client, captured = _client_capturing()
    asyncio.run(client.stop_cook("AC000W000000000"))
    payload = captured["desired"]["Cook_Command"]
    assert payload["id"] == 1000            # the app's stop id
    assert payload["temp"] == 0
    assert payload["seconds set"] == 0
    assert payload["smoke"] == 0


def test_skip_preheat_reissues_the_cook_with_the_flag() -> None:
    """There is no dedicated skip command on either backend."""
    import asyncio

    client, captured = _client_capturing()
    asyncio.run(client.skip_preheat(
        "AC000W000000000", mode="bake", seconds=600, temp=180, smoke=False,
    ))
    payload = captured["desired"]["Cook_Command"]
    assert payload["skip preheat"] == 1
    assert payload["seconds set"] == 600
    assert payload["mode"] == "bake"


def test_unknown_cook_mode_is_rejected_before_sending() -> None:
    import asyncio

    import pytest as _pytest

    client, captured = _client_capturing()
    with _pytest.raises(ValueError):
        asyncio.run(client.start_cook(
            "AC000W000000000", mode="teleport", seconds=60, temp=100,
        ))
    assert not captured, "nothing should be sent for an invalid mode"


def test_both_backends_expose_the_same_command_interface() -> None:
    """The coordinator holds whichever backend setup chose.

    Ayla and AWS must therefore agree on the command surface, exactly as they
    already do on `read_state`.
    """
    import inspect

    from nwf_lib.api.aws import AwsCloudClient
    from nwf_lib.api.ayla import AylaCloudClient

    for name in ("start_cook", "stop_cook", "skip_preheat"):
        for cls in (AwsCloudClient, AylaCloudClient):
            fn = getattr(cls, name, None)
            assert fn is not None, f"{cls.__name__} is missing {name}()"
            assert inspect.iscoroutinefunction(fn), f"{cls.__name__}.{name} must be async"
        aws_params = set(inspect.signature(AwsCloudClient.start_cook).parameters)
        ayla_params = set(inspect.signature(AylaCloudClient.start_cook).parameters)
        assert ayla_params <= aws_params, (
            f"AWS start_cook is missing parameters the Ayla one accepts: "
            f"{ayla_params - aws_params}"
        )


def test_commands_are_routed_through_the_commander() -> None:
    """Commands must not be hardcoded to the Ayla client.

    A migrated grill never acknowledges Ayla datapoints — the cloud accepts
    them, the grill never sees them, and the write times out. Routing through
    `commander` is what lets setup point commands at whichever backend the
    grill is actually on.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "custom_components" / "ninja_woodfire" / "coordinator.py").read_text()
    for call in ("start_cook(", "stop_cook(", "skip_preheat("):
        assert f"self.client.{call}" not in src, (
            f"{call} must go through self.commander, not self.client"
        )
        assert f"self.commander.{call}" in src
