"""What unit each temperature field is in, and where the boundary runs.

The integration reported roughly double: a Bake at 160 °C showed the air
chamber at 321 °C. The firmware publishes **two blocks in two different
units** and the parser took both for Celsius.

    GrillState.inputs.temps       raw sensors, Fahrenheit
    GrillState.setpoint           user-facing, Celsius
    ProbeState.probes[].temp      user-facing, Celsius

Neither cloud says so anywhere — Ayla's 24-key property schema has no unit
field, the per-property detail endpoint adds nothing to the list endpoint,
and the AWS device record and shadow have no unit property either. So the
boundary is protocol knowledge, pinned here.

The evidence is in the fixtures rather than in reasoning about magnitudes,
because a magnitude argument would be exactly the unsafe heuristic this
change exists to avoid.
"""
from __future__ import annotations

import json

import pytest

from nwf_lib.models import (
    FAHRENHEIT_TEMP_FIELDS,
    GrillState,
    GrillTemps,
    ProbeState,
    fahrenheit_to_celsius,
)

from .conftest import (
    aws_fixture_names,
    ayla_fixture_names,
    hydrate,
    hydrate_aws,
    load_fixture,
)


# --------------------------------------------------- the self-validating one

def test_one_snapshot_carries_the_same_measurement_in_both_units() -> None:
    """The whole case, from a single payload and no outside assumption.

    A probe was plugged in with its tip in open room air during a Bake. The
    firmware then published that one measurement twice: the raw elements in
    `GrillState.inputs.temps` and its own Celsius conversion in `ProbeState`.

    Nothing here depends on knowing the room temperature, the setpoint, or
    what a grill can physically reach. The firmware is checking our arithmetic
    against its own.
    """
    fx = load_fixture("aws_bake_probe_ambient")
    grill_raw = json.loads(fx["telemetry"]["GrillState"])
    probe_raw = json.loads(fx["telemetry"]["ProbeState"])

    wire_elements = grill_raw["inputs"]["temps"]["probe1_a"]
    firmware_celsius = probe_raw["probes"][0]["temp"]

    assert wire_elements == 80          # what inputs.temps said
    assert firmware_celsius == 26.6     # what ProbeState said, same instant
    # 80 °F is 26.67 °C. The two blocks agree once, and only once, the raw
    # one is read as Fahrenheit.
    assert fahrenheit_to_celsius(wire_elements) == pytest.approx(26.6, abs=0.1)
    assert firmware_celsius != wire_elements

    # And that is what the parser now produces on each side of the boundary.
    state = hydrate_aws("aws_bake_probe_ambient", online=True, age_seconds=0)
    assert state.grill.temps.probe1_a == pytest.approx(26.7, abs=0.1)
    assert state.probes.probes[0].temp == pytest.approx(26.6)


def test_probe_state_celsius_is_left_alone() -> None:
    """The obvious wrong fix, guarded.

    Converting the whole payload would take this probe — sitting in a warm
    kitchen — down to −3 °C. `ProbeState` is already Celsius; only
    `inputs.temps` is not.
    """
    state = hydrate_aws("aws_bake_probe_ambient", online=True, age_seconds=0)
    probe = state.probes.probes[0]
    assert probe.plugged_in is True
    assert probe.temp == pytest.approx(26.6)
    assert fahrenheit_to_celsius(probe.temp) < 0    # what the wrong fix gives


# ------------------------------------------------------- the chamber sensors

def test_bake_160_chamber_reads_near_its_setpoint() -> None:
    """Live acceptance case: Bake, 160 °C setpoint, mid-cook.

    Read as Celsius the air chamber is 302.7 °C, which this appliance cannot
    reach and which is nearly twice its own target. Read as Fahrenheit it is
    150.4 °C — an oven a little under setpoint between burner cycles.
    """
    state = hydrate_aws("aws_bake_probe_ambient", online=True, age_seconds=0)
    assert state.grill.mode == "bake"
    assert state.grill.setpoint == 160
    assert state.grill.temps.air == pytest.approx(150.4, abs=0.1)
    assert state.grill.temps.grill == pytest.approx(136.8, abs=0.1)
    # Within a sane band of the target rather than double it.
    assert abs(state.grill.temps.air - state.grill.setpoint) < 30


# Readings taken off a real OG901EU during two cooks, as the integration
# displayed them before the fix. These are the numbers that started the
# investigation; they are here so the conversion is pinned against
# observations from outside the capture set.
@pytest.mark.parametrize(
    "mode,setpoint,wire_air,wire_grill,want_air,want_grill",
    [
        # Bake at 160 °C, early in the heating cycle.
        ("bake", 160, 321.2, 216.3, 160.7, 102.4),
        # Air Crisp at 200 °C, near the end of the cook.
        ("air crisp", 200, 402.8, 313.5, 206.0, 156.4),
        # Air Crisp at 200 °C, earlier in the same cook.
        ("air crisp", 200, 384.2, 282.5, 195.7, 139.2),
    ],
)
def test_observed_cooks_normalise_to_their_setpoint(
    mode: str, setpoint: int, wire_air: float,
    wire_grill: float, want_air: float, want_grill: float,
) -> None:
    temps = GrillTemps.from_wire({"air": wire_air, "grill": wire_grill})
    assert temps.air == pytest.approx(want_air, abs=0.1)
    assert temps.grill == pytest.approx(want_grill, abs=0.1)
    # The air chamber is the one that tracks the target; the grill plate lags
    # in these modes because it is not the driven element.
    assert abs(temps.air - setpoint) < 15, mode


def test_lid_open_transition_keeps_its_physical_meaning() -> None:
    """Conversion is monotonic, so it cannot invent or erase a trend.

    Observed during an Air Crisp cook: with the lid not quite shut the two
    chambers sat close together, and closing it let the air chamber run away
    from the grill plate again. That ordering is the physical claim, and it
    has to survive the conversion — an affine map with a positive scale
    cannot reorder anything, which is the point.
    """
    ajar = GrillTemps.from_wire({"grill": 252.6, "air": 244.5})
    shut = GrillTemps.from_wire({"grill": 244.7, "air": 366.9})

    assert ajar.air < ajar.grill            # lid ajar: air cannot hold heat
    assert shut.air > shut.grill            # lid shut: air chamber climbs
    assert shut.air - ajar.air == pytest.approx(68.0, abs=0.1)
    # Same trend on the wire, in °F — the conversion changed the scale, not
    # the story.
    assert 244.5 < 252.6 and 366.9 > 244.7


def test_idle_grill_reads_room_temperature() -> None:
    """The other end of the scale, and the one that gives the game away.

    This grill had been off and offline for 23 hours. As Celsius the payload
    claims 79.5 °C, which nothing explains. As Fahrenheit it is 26.4 °C: a
    cold appliance in a room.
    """
    state = hydrate("idle_offline", online=True, age_seconds=0)
    assert state.grill.temps.grill == pytest.approx(26.4)
    assert state.grill.temps.air == pytest.approx(24.8)
    assert 15 < state.grill.temps.grill < 35
    assert 15 < state.grill.temps.air < 35


# ------------------------------------------------------------ the boundary

def test_setpoint_is_never_converted() -> None:
    """`setpoint` is Celsius on the wire, in every capture that has one.

    Cross-checked against the capability table, which declares Air Crisp as
    120-240 °C: a 150 that meant Fahrenheit would be 65 °C and out of range
    for the mode the grill says it is in.
    """
    from nwf_lib.capabilities import for_oem_model

    caps = for_oem_model("OG900-EU")
    for name in aws_fixture_names():
        state = hydrate_aws(name, online=True, age_seconds=0)
        if state.grill.setpoint is None or state.grill.mode is None:
            continue
        mode = caps.get_mode(state.grill.mode)
        if mode is None or mode.temp_unit != "celsius":
            continue
        assert mode.temp_min <= state.grill.setpoint <= mode.temp_max, (
            f"{name}: setpoint {state.grill.setpoint} outside "
            f"{state.grill.mode} range {mode.temp_min}-{mode.temp_max} °C"
        )


def test_pcb_channels_are_not_temperatures_and_are_untouched() -> None:
    """`main` / `ui` are ADC counts wearing temperature names.

    Ayla calls them "Main PCB Temperature" and "UI PCB Temperature", and they
    read 6542.4 / 6513.6 in every capture ever taken. Converting them would
    produce a confident 3617 °C.
    """
    for name in aws_fixture_names() + ayla_fixture_names():
        state = (hydrate_aws if name.startswith("aws_") else hydrate)(
            name, online=True, age_seconds=0
        )
        assert state.grill.temps.main == pytest.approx(6542.4)
        assert state.grill.temps.ui == pytest.approx(6513.6)
    assert "main" not in FAHRENHEIT_TEMP_FIELDS
    assert "ui" not in FAHRENHEIT_TEMP_FIELDS


def test_the_fahrenheit_list_names_real_fields_and_nothing_else() -> None:
    """Invariant: the list is a field list, not a free-text set.

    A typo here would silently leave a sensor unconverted, which looks exactly
    like the bug this fixes.
    """
    fields = set(vars(GrillTemps()))
    assert FAHRENHEIT_TEMP_FIELDS <= fields
    assert fields - FAHRENHEIT_TEMP_FIELDS == {"main", "ui"}


def test_conversion_does_not_depend_on_the_value() -> None:
    """No magnitude heuristic anywhere in the path.

    "Assume Fahrenheit above 250" would be unsafe in both directions and
    would change a reading's meaning as the grill heats. The same wire number
    must convert the same way whatever it is.
    """
    for wire in (0.0, 32.0, 76.6, 200.0, 250.0, 251.0, 402.8, 6542.4):
        assert GrillTemps.from_wire({"air": wire}).air == pytest.approx(
            round(fahrenheit_to_celsius(wire), 1)
        )
    # Zero is a real wire value (an unplugged probe) and converts like any
    # other — it is the plausibility layer's job to hide it, not the
    # parser's.
    assert GrillTemps.from_wire({"probe0_a": 0}).probe0_a == pytest.approx(-17.8)


def test_both_transports_normalise_identically() -> None:
    """One conversion, in the model layer, so neither backend can drift.

    AWS strips the spaces out of the keys but the numbers are the same
    firmware struct, so the same wire values must land on the same °C.
    """
    wire = {"grill": 212.5, "air": 288.1, "smoke": 258}
    ayla = GrillState.from_property_value(
        json.dumps({"state": "cooking", "inputs": {"temps": wire, "io": {"lid open": 0}}})
    )
    aws = GrillState.from_property_value(
        json.dumps({"state": "cooking", "inputs": {"temps": wire, "io": {"lidopen": 0}}})
    )
    assert ayla.temps == aws.temps
    assert ayla.temps.air == pytest.approx(142.3, abs=0.1)


# ------------------------------------------------------------------ sanity

@pytest.mark.parametrize("name", aws_fixture_names() + ayla_fixture_names())
def test_every_fixture_converts_into_a_physically_possible_range(name: str) -> None:
    """No capture may normalise to something an appliance cannot be.

    The pre-fix values failed this: 288.1 "°C" in an Air Crisp capture whose
    own setpoint was 150.
    """
    state = (hydrate_aws if name.startswith("aws_") else hydrate)(
        name, online=True, age_seconds=0
    )
    for sensor in ("grill", "air"):
        value = getattr(state.grill.temps, sensor)
        assert -20 <= value <= 260, f"{name}: {sensor} = {value} °C"
    for probe in state.probes.probes:
        assert -20 <= probe.temp <= 120, f"{name}: probe {probe.name} = {probe.temp} °C"
