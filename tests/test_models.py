"""Parser tests over real captured firmware payloads.

Every fixture in ``tests/fixtures`` is a byte-preserved capture from a real
OG900-EU (Ninja Woodfire Pro Connect XL) with DSN/MAC/IP scrubbed. The
firmware emits tab-indented, pretty-printed JSON inside the Ayla property
``value`` string, so the fixtures keep that exact formatting.
"""
from __future__ import annotations

import json

import pytest

from nwf_lib.models import CombinedState, CookState, GrillState, ProbeState

from .conftest import ayla_fixture_names, hydrate, load_fixture


def _hydrate(name: str) -> CombinedState:
    """Fixture parsed with a fresh, online snapshot — parser tests only care
    about the payload, not about liveness gating."""
    return hydrate(name, age_seconds=0.0, online=True)


# --------------------------------------------------------------- smoke tests

@pytest.mark.parametrize("name", ayla_fixture_names())
def test_every_fixture_parses(name: str) -> None:
    """No fixture may fall back to the 'unknown'/empty parse."""
    fx = load_fixture(name)
    state = _hydrate(name)
    assert state.grill.state != "unknown", f"{name}: grill state failed to parse"
    assert state.grill.raw, f"{name}: grill raw payload empty"
    assert len(state.probes.probes) == 2, f"{name}: expected 2 probes"


@pytest.mark.parametrize("name", ayla_fixture_names())
def test_firmware_json_is_tab_indented(name: str) -> None:
    """Regression guard: the firmware ships pretty-printed JSON with tabs.

    Fixtures must keep it verbatim — a fixture that got reformatted no longer
    proves the parser tolerates the real wire format.
    """
    raw = load_fixture(name)["properties"]["GET_GrillState"]["value"]
    assert "\n\t" in raw, f"{name}: fixture was reformatted, recapture it"
    assert json.loads(raw)  # still valid JSON


# ------------------------------------------------------------------ idle

def test_idle_offline_snapshot() -> None:
    """The bug-report snapshot: cloud serving a day-old 'idle' datapoint."""
    state = _hydrate("idle_offline")
    assert state.grill.state == "idle"
    # Idle payloads carry no cook fields at all — this is why HA showed
    # "Unknown" for mode/setpoint/progress/end time.
    assert state.grill.mode is None
    assert state.grill.setpoint is None
    assert state.grill.seconds_set is None
    assert state.grill.seconds_left is None
    assert state.grill.end_time_utc is None
    assert state.cook.state == "none"
    assert state.cook.progress is None
    assert state.grill.lid_open is False
    assert all(not p.plugged_in and not p.active for p in state.probes.probes)
    # Leftover chamber temps from the previous cook are still being served.
    assert state.grill.temps.grill == pytest.approx(79.5)
    assert state.grill.temps.smoke == pytest.approx(227.1)


def test_idle_leftover_temps_are_suppressed() -> None:
    """An idle grill must not report last-cook temperatures as live."""
    state = _hydrate("idle_offline")
    assert state.temp_is_plausible(state.grill.temps.grill, "grill") is False
    assert state.temp_is_plausible(state.grill.temps.smoke, "smoke") is False


# ------------------------------------------------------- powered off / zc loss

def test_powered_off_state_parses() -> None:
    """'powered OFF' — plugged in, control panel off. A real firmware state."""
    state = _hydrate("powered_off")
    assert state.grill.state == "powered OFF"
    assert state.grill.mode is None
    assert state.cook.state == "none"


def test_powered_off_leftover_temps_are_suppressed() -> None:
    """Same leftover-temperature problem as idle, different state string.

    The grill is not cooking, so its stale 82.4 C chamber reading must not
    reach HA as a live temperature.
    """
    state = _hydrate("powered_off")
    assert state.grill.temps.grill > 50  # the stale reading is really there
    assert state.temp_is_plausible(state.grill.temps.grill, "grill") is False


def test_zc_loss_cook_state() -> None:
    """'zc loss' = mains zero-cross lost (switched off at the socket)."""
    state = _hydrate("zc_loss")
    assert state.cook.state == "zc loss"
    assert state.cook.progress is None


# ------------------------------------------------------------ parser tolerance

def test_cook_state_accepts_plain_string_and_object() -> None:
    """GET_CookState.state is a bare string in some firmware builds."""
    assert CookState.from_property_value('{"state":"none"}').state == "none"
    nested = CookState.from_property_value('{"state":{"state":"preheat","progress":75}}')
    assert (nested.state, nested.progress) == ("preheat", 75)


def test_garbage_and_empty_values_do_not_raise() -> None:
    for bad in ("", "not json", "{", None, 42, [], {}):
        grill = GrillState.from_property_value(bad)
        assert grill.state == "unknown"
        assert CookState.from_property_value(bad).state == "none"
        assert ProbeState.from_property_value(bad).probes == []


def test_probe_protein_accepts_int_and_string() -> None:
    """Firmware sends probe enum fields as ints on some builds, names on others."""
    payload = json.dumps({"probes": [
        {"name": "probe0", "plugged in": 1, "active": 1, "temp": 41.5, "progress": 40,
         "mode": {"mode": "preset", "protein": "Beef", "cut": "3", "doneness": "MedRare"},
         "state": {"state": "cooking"}},
        {"name": "probe1", "plugged in": 0, "active": 0, "temp": 0, "progress": 100,
         "mode": {"mode": "manual", "setpoint": 79}},
    ]})
    probes = ProbeState.from_property_value(payload).probes
    assert probes[0].target.protein == "Beef"
    assert probes[0].target.cut == 3          # numeric string is coerced
    assert probes[0].target.doneness == "MedRare"
    assert probes[0].state == "cooking"
    assert probes[0].temp == pytest.approx(41.5)
    assert probes[1].target.setpoint == 79
