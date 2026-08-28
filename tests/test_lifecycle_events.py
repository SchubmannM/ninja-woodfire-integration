"""Cook-lifecycle events — the ones automations and blueprints subscribe to.

`_emit_lifecycle_events` diffs each poll against the last and fires on the
transitions worth telling someone about. Until now nothing covered it, which
is uncomfortable for the one part of the integration whose entire job is to
be the trigger for "your food is ready".

Transitions between real cook phases are driven from the captured fixtures
rather than hand-built states, so the states are the ones the firmware
actually reports — `GrillState.state` reads "cooking" throughout preheat, and
the real sub-phase is in `CookState.state`, which is precisely the kind of
thing a hand-written state would get wrong. Probe readings are set on top of a
real snapshot because no capture has a probe in the meat.
"""
from __future__ import annotations

import pytest

from .conftest import (
    FakeDevice,
    FakeHass,
    hydrate,
    hydrate_aws,
    load_coordinator,
)

coordinator_module = load_coordinator()

from nwf_ha._lib.capabilities import for_oem_model  # noqa: E402
from nwf_ha.const import (  # noqa: E402
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
)

DSN = "AC000W000000000"
DOMAIN = "ninja_woodfire"


def make_coordinator(device: FakeDevice | None = FakeDevice("dev1", "WoodNinja")):
    """A coordinator wired to a recording bus, with no cloud behind it."""
    hass = FakeHass()
    if device is not None:
        hass.device_registry.add({(DOMAIN, DSN)}, device)
    coord = coordinator_module.NinjaWoodfireCoordinator(
        hass,
        client=object(),
        dsn=DSN,
        capabilities=for_oem_model("OG900-EU"),
        device_key=1,
    )
    return coord


def play(coord, *states) -> list[tuple[str, dict]]:
    """Feed snapshots through in order and return what fired."""
    before = len(coord.hass.bus.events)
    for state in states:
        coord._emit_lifecycle_events(state)
    return coord.hass.bus.events[before:]


# The three phases of a real cook, as captured from the grill.
def idle():
    return hydrate("idle_offline", online=True)


def preheat():
    return hydrate_aws("aws_preheat")


def cooking():
    return hydrate_aws("aws_cooking")


# ------------------------------------------------------- who the event is about

def test_every_event_says_which_grill_and_what_it_is_called() -> None:
    """A notification that names a serial number is a notification nobody wants.

    The device id is what lets a blueprint offer a device picker instead of
    asking for a DSN; the name is what the message can actually say.
    """
    coord = make_coordinator()
    fired = play(coord, idle(), preheat(), cooking())

    assert [name for name, _ in fired] == [
        EVENT_COOK_STARTED,
        EVENT_PREHEAT_COMPLETE,
    ]
    for _, data in fired:
        assert data["dsn"] == DSN
        assert data["device_id"] == "dev1"
        assert data["device_name"] == "WoodNinja"


def test_renaming_the_device_renames_it_in_the_event() -> None:
    coord = make_coordinator(FakeDevice("dev1", "WoodNinja", name_by_user="Patio grill"))
    fired = play(coord, idle(), preheat())
    assert fired[0][1]["device_name"] == "Patio grill"


def test_events_still_fire_before_the_device_exists() -> None:
    """The device is created by the first entity, i.e. after the first refresh.

    The identity keys must still be present so a template that reads them
    renders empty rather than erroring.
    """
    coord = make_coordinator(device=None)
    fired = play(coord, idle(), preheat())
    assert fired[0][1]["device_id"] is None
    assert fired[0][1]["device_name"] is None
    assert fired[0][1]["dsn"] == DSN


def test_probe_events_are_identified_the_same_way() -> None:
    """Probe events used to carry only the DSN — the notifications people
    actually want ("meat is at temperature") were the ones hardest to route."""
    coord = make_coordinator()
    play(coord, idle())

    state = cooking()
    probe = state.probes.probes[0]
    probe.active = True
    probe.temp = 62.0
    probe.target.setpoint = 60

    play(coord, state)
    reached = coord.hass.bus.of_type(EVENT_PROBE_TARGET_REACHED)
    assert len(reached) == 1
    assert reached[0]["device_id"] == "dev1"
    assert reached[0]["device_name"] == "WoodNinja"
    assert reached[0]["probe_index"] == 0


# ------------------------------------------------------------- the transitions

def test_cook_started_fires_once_when_the_grill_leaves_idle() -> None:
    coord = make_coordinator()
    fired = play(coord, idle(), preheat(), preheat(), cooking())
    assert [n for n, _ in fired].count(EVENT_COOK_STARTED) == 1

    started = coord.hass.bus.of_type(EVENT_COOK_STARTED)[0]
    assert started["mode"] == "air crisp"
    assert started["setpoint"] is not None
    assert "seconds_set" in started
    assert "smoke" in started


def test_nothing_fires_on_the_very_first_poll() -> None:
    """With no previous snapshot there is no transition — only a starting point.

    Otherwise every Home Assistant restart during a cook would announce a cook
    that started an hour ago.
    """
    coord = make_coordinator()
    assert play(coord, cooking()) == []


def test_preheat_complete_fires_on_the_cook_sub_phase() -> None:
    """`GrillState.state` says "cooking" for the whole cook, preheat included.

    The transition only exists in `CookState.state` (preheat -> heat), which
    is why this is driven from real captures.
    """
    coord = make_coordinator()
    assert preheat().grill.state == cooking().grill.state == "cooking"
    fired = play(coord, idle(), preheat(), cooking())
    assert [n for n, _ in fired].count(EVENT_PREHEAT_COMPLETE) == 1


def test_cook_done_reports_whether_it_finished_or_was_stopped() -> None:
    coord = make_coordinator()
    play(coord, idle(), cooking())

    stopped = cooking()
    stopped.grill.state = "idle"
    play(coord, stopped)
    assert coord.hass.bus.of_type(EVENT_COOK_DONE)[-1]["reason"] == "stopped"

    coord = make_coordinator()
    play(coord, idle(), cooking())
    finished = cooking()
    finished.grill.state = "done"
    play(coord, finished)
    assert coord.hass.bus.of_type(EVENT_COOK_DONE)[-1]["reason"] == "done"


def test_halftime_fires_once_when_the_timer_passes_the_midpoint() -> None:
    coord = make_coordinator()
    play(coord, idle(), cooking())

    def at(seconds_left: int):
        state = cooking()
        state.grill.seconds_set = 600
        state.grill.seconds_left = seconds_left
        return state

    play(coord, at(400), at(299), at(280), at(120))
    assert len(coord.hass.bus.of_type(EVENT_COOK_HALFTIME)) == 1
    assert coord.hass.bus.of_type(EVENT_COOK_HALFTIME)[0]["seconds_set"] == 600


def test_probe_halfway_and_target_fire_once_each_per_probe() -> None:
    coord = make_coordinator()
    play(coord, idle())

    def at(temp: float):
        state = cooking()
        probe = state.probes.probes[0]
        probe.active = True
        probe.temp = temp
        probe.target.setpoint = 60
        return state

    play(coord, at(20.0), at(31.0), at(45.0), at(61.0), at(65.0))
    assert len(coord.hass.bus.of_type(EVENT_PROBE_HALFWAY)) == 1
    assert len(coord.hass.bus.of_type(EVENT_PROBE_TARGET_REACHED)) == 1


def test_a_second_cook_can_fire_the_one_shots_again() -> None:
    """The one-shot flags are reset in two places — cook start and cook done.

    A grill that has finished one cook and started another has to be able to
    announce halftime again.
    """
    coord = make_coordinator()

    def run_a_cook():
        play(coord, idle(), cooking())
        half = cooking()
        half.grill.seconds_set = 600
        half.grill.seconds_left = 200
        play(coord, half)
        stopped = cooking()
        stopped.grill.state = "idle"
        play(coord, stopped)

    run_a_cook()
    run_a_cook()
    assert len(coord.hass.bus.of_type(EVENT_COOK_HALFTIME)) == 2
    assert len(coord.hass.bus.of_type(EVENT_COOK_DONE)) == 2


# ------------------------------------------------------------------- liveness

@pytest.mark.parametrize(
    "kwargs",
    [{"online": False}, {"age_seconds": 60 * 60}],
    ids=["offline", "stale"],
)
def test_a_snapshot_that_is_not_live_fires_nothing(kwargs) -> None:
    coord = make_coordinator()
    play(coord, idle(), cooking())
    before = len(coord.hass.bus.events)
    play(coord, hydrate_aws("aws_cooking", **kwargs))
    assert len(coord.hass.bus.events) == before


def test_a_grill_that_drops_off_mid_cook_does_not_announce_a_cook_done() -> None:
    """The reason the liveness guard clears the previous snapshot.

    Without it, a grill that vanishes mid-cook and comes back reading "idle"
    looks exactly like a cook that finished, and the user gets a "your food is
    ready" for food that is still raw.
    """
    coord = make_coordinator()
    play(coord, idle(), cooking())
    play(coord, hydrate_aws("aws_cooking", online=False))

    reconnected = idle()
    play(coord, reconnected)
    assert coord.hass.bus.of_type(EVENT_COOK_DONE) == []
