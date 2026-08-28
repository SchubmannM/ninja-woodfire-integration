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
