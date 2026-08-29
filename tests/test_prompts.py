"""The grill's own prompts — "add the food", "turn it", "take it out".

These arrive in `GrillState.message` as `"<bit>:<name>"`, paired with
`eventmask` as a field of everything currently raised. They are the only
place the appliance tells the user to go and do something, and until now
nothing read them: `sensor.message` passed the raw string through as a
diagnostic and no automation could sensibly trigger on `"4:flipfood"`.

The strings below are the four observed on a real OG900-EU. `parse_prompt`
deliberately does not match against them — a prompt on one of the four bits
nobody has captured yet should arrive under its own name rather than vanish.
"""
from __future__ import annotations

import pytest

from nwf_lib.models import GrillState, parse_prompt

from .conftest import hydrate_aws


# The full observed table. The number before the colon is the bit index, so
# the mask is `1 << index` — 4 -> 0x10 — with every raised bit still set,
# which is why getfood arrives as 0xC0 rather than 0x80.
OBSERVED = [
    ("1:addfood", "addfood", 0x02),
    ("4:flipfood", "flipfood", 0x10),
    ("6:done", "done", 0x40),
    ("7:getfood", "getfood", 0x80),
]


@pytest.mark.parametrize("message,name,bit", OBSERVED)
def test_the_name_comes_out_of_the_message(message: str, name: str, bit: int) -> None:
    assert parse_prompt(message) == name


@pytest.mark.parametrize("message,name,bit", OBSERVED)
def test_the_index_is_the_bit_the_mask_raises(message: str, name: str, bit: int) -> None:
    """Not used by the parser, but it is why the index can be discarded: the
    mask already carries it, so the name is the only part with information."""
    assert 1 << int(message.split(":")[0]) == bit


def test_an_uncaptured_prompt_still_arrives_under_its_own_name() -> None:
    """Four of the eight bits have never been seen. Matching against a table
    of the known four would silently drop whatever turns up on the others."""
    assert parse_prompt("3:restfood") == "restfood"
    assert parse_prompt("0:something") == "something"


@pytest.mark.parametrize("message", ["", None, "   "])
def test_no_prompt_is_the_empty_string(message) -> None:
    assert parse_prompt(message) == ""


def test_a_message_with_no_index_is_taken_whole() -> None:
    """Defensive: every capture has the "<bit>:" prefix, but a bare name is
    the obvious other shape and losing it would be worse than keeping it."""
    assert parse_prompt("flipfood") == "flipfood"


@pytest.mark.parametrize("message", ["4:", ":", "6:  "])
def test_an_index_with_no_name_is_no_prompt(message: str) -> None:
    """Not a prompt named "4:"."""
    assert parse_prompt(message) == ""


@pytest.mark.parametrize(
    "message",
    ["4:flipfood", "4:flip food", "4:flip_food", "4:FlipFood", "4:FLIP FOOD"],
)
def test_both_dialects_and_any_casing_give_one_name(message: str) -> None:
    """`aws.normalise` canonicalises `mode` and `state` — not `message`. And
    no Ayla capture carries a prompt at all, so its spelling is unknown.

    This firmware writes these same words both ways elsewhere ("get food" and
    "get_food" are both in ACTIVE_COOK_STATES), and AWS stripping every space
    is exactly what turns "4:flip food" into the "4:flipfood" we captured. A
    consumer matching one literal string would otherwise get no notification
    at all on a grill that was never migrated — a silent failure, and the
    worst kind to diagnose.
    """
    assert parse_prompt(message) == "flipfood"


def test_only_the_first_colon_splits() -> None:
    assert parse_prompt("4:flip:food") == "flip:food"


# ------------------------------------------------------- against the captures

def test_the_captures_parse() -> None:
    assert hydrate_aws("aws_cooking").grill.prompt == "addfood"
    assert hydrate_aws("aws_get_food").grill.prompt == "getfood"
    assert hydrate_aws("aws_cook_done").grill.prompt == "done"


def test_a_grill_with_nothing_to_say_has_no_prompt() -> None:
    for name in ("aws_preheat", "aws_powered_off_lid_open"):
        assert hydrate_aws(name).grill.prompt == ""


def test_the_raw_message_is_kept_alongside_the_parsed_name() -> None:
    """`sensor.message` stays as it was — a diagnostic showing exactly what
    the firmware said, including the index the prompt sensor drops."""
    state = hydrate_aws("aws_get_food")
    assert state.grill.message == "7:getfood"
    assert state.grill.prompt == "getfood"


def test_parsing_does_not_depend_on_the_envelope() -> None:
    """The parser runs the same wherever the payload came from.

    Note what this does *not* show: no Ayla capture has ever carried a prompt,
    so whether that backend spells it the same way is unknown. That is the
    reason the name is canonicalised rather than taken verbatim.
    """
    state = GrillState.from_property_value('{"state":"cooking","message":"4:flipfood"}')
    assert state.prompt == "flipfood"
    assert state.message == "4:flipfood", "the raw string is kept as-is"


# ----------------------------------------------- how a probe target was set
#
# Reached through the real description table rather than a local copy of the
# logic: `attrs_fn` is where the behaviour lives, and a test that restates it
# goes on passing after the lambda is deleted.

from .conftest import description, load_platform  # noqa: E402

SENSOR = load_platform("sensor")


def probe_target_attrs(state, index: int):
    return description(SENSOR, f"probe{index}_setpoint").attrs_fn(state)


def preset_probe(index: int = 0):
    """A capture with a preset target grafted onto one probe.

    No capture has one — the app's preset flow has never been run against a
    grill while anybody was recording — so this is the shape `ProbeMode`
    already parses, on top of a real cook.
    """
    state = hydrate_aws("aws_cooking")
    probe = state.probes.probes[index]
    probe.active = True
    probe.target.mode = "preset"
    probe.target.setpoint = 47
    probe.target.preset_index = 3
    probe.target.protein = "Beef"
    probe.target.doneness = "MedRare"
    return state


def test_the_setpoint_sensors_carry_the_target_attributes() -> None:
    for index in (0, 1):
        assert description(SENSOR, f"probe{index}_setpoint").attrs_fn is not None


def test_a_preset_target_keeps_every_field_the_firmware_sent() -> None:
    """The point of surfacing these: a preset cook used to resolve to a bare
    temperature and the rest was parsed and dropped, so not even the recorder
    could say what a preset looks like on the wire."""
    assert probe_target_attrs(preset_probe(), 0) == {
        "target_mode": "preset", "preset_index": 3,
        "protein": "Beef", "cut": None, "doneness": "MedRare",
    }


def test_a_manual_target_says_so_rather_than_leaving_it_blank() -> None:
    state = hydrate_aws("aws_cooking")
    probe = state.probes.probes[0]
    probe.active = True
    probe.target.mode = "manual"
    probe.target.setpoint = 63
    assert probe_target_attrs(state, 0) == {
        "target_mode": "manual", "preset_index": None,
        "protein": None, "cut": None, "doneness": None,
    }


def test_the_fields_survive_as_numbers_too() -> None:
    """`_parse_int_or_str` accepts either, because which one this firmware
    sends has never been captured — the phone app shows Beef as "Med Rare 3"
    against 47 °C, and that 3 could be the index or part of a label."""
    from nwf_lib.models import ProbeMode

    t = ProbeMode.from_dict({"mode": "preset", "protein": 2, "doneness": 3})
    assert (t.protein, t.doneness) == (2, 3)

    state = preset_probe()
    state.probes.probes[0].target.protein = 2
    state.probes.probes[0].target.doneness = 3
    attrs = probe_target_attrs(state, 0)
    assert (attrs["protein"], attrs["doneness"]) == (2, 3)


def test_an_inactive_probe_reports_no_target_at_all() -> None:
    """The attributes hang off the setpoint sensor, whose own value is None
    unless the cook is using that probe. They have to agree, or the card shows
    a doneness for a probe that is not in anything."""
    state = hydrate_aws("aws_cooking")
    assert state.probes.probes[0].active is False
    assert probe_target_attrs(state, 0) is None
    assert description(SENSOR, "probe0_setpoint").value_fn(state) is None


def test_a_probe_the_model_does_not_have_is_not_an_error() -> None:
    state = preset_probe()
    state.probes.probes = state.probes.probes[:1]
    assert probe_target_attrs(state, 1) is None


def test_nothing_invents_a_label_for_an_index() -> None:
    """Deliberate: passing the raw values through is what makes the next
    preset cook self-documenting. A guessed mapping would put an assumption
    exactly where the evidence is supposed to go."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "custom_components" / "ninja_woodfire" / "sensor.py").read_text()
    body = source[source.index("def _probe_target_attrs"):source.index("_GRILL_LEVEL_LABELS")]
    # Past the docstring, which cites the app's labels as evidence.
    assert body.count('"""') == 2, "expected exactly one docstring"
    code = body.rsplit('"""', 1)[1]
    for guess in ("Rare", "MedRare", "Med Rare", "Beef", "Chicken", "Well"):
        assert guess not in code, guess
