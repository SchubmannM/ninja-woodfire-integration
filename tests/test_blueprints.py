"""The shipped automation blueprints.

Blueprints are the one part of this repo that is pure configuration: nothing
imports them, nothing type-checks them, and a typo only shows up when a user
tries to import one. Two kinds of mistake are worth catching here.

*Drift.* `cook_notifications.yaml` triggers on the six event names in
`const.EVENT_*`. Rename one of those constants and the blueprint keeps
importing cleanly and silently never fires again.

*The message templates.* They are the actual deliverable — the reason a user
installs the blueprint instead of writing three lines of YAML — and they are
the only Jinja in the repo. Rendering each branch is cheaper than discovering
a broken `{% elif %}` from a notification that says nothing.

Home Assistant is not here to validate the schema, so this does not pretend
to: importing a blueprint into a real instance remains the acceptance test.
"""
from __future__ import annotations

import pathlib
import re

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINTS = ROOT / "blueprints" / "automation" / "ninja_woodfire"

EVENT_PREFIX = "ninja_woodfire_"


class Input:
    """Stand-in for Home Assistant's `!input` tag."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"!input {self.name}"


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!input", lambda loader, node: Input(loader.construct_scalar(node)))


def load(name: str) -> dict:
    with open(BLUEPRINTS / f"{name}.yaml") as fh:
        return yaml.load(fh, Loader=Loader)


def walk(node):
    """Every value in the tree, containers included."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def declared_inputs(blueprint: dict) -> set[str]:
    """Input names, flattening the nested sections the UI groups by."""
    names = set()
    for key, spec in blueprint["blueprint"]["input"].items():
        if isinstance(spec, dict) and "input" in spec:
            names.update(spec["input"])  # a section, not an input
        else:
            names.add(key)
    return names


ALL = ["cook_notifications", "grill_alerts"]


# ------------------------------------------------------------------ structure

@pytest.mark.parametrize("name", ALL)
def test_blueprint_is_well_formed(name: str) -> None:
    bp = load(name)["blueprint"]
    assert bp["domain"] == "automation"
    assert bp["name"].startswith("Ninja Woodfire")
    assert bp["description"].strip()


@pytest.mark.parametrize("name", ALL)
def test_source_url_points_at_the_file_itself(name: str) -> None:
    """It is what Home Assistant offers as "re-import"; a stale one re-imports
    the wrong blueprint."""
    url = load(name)["blueprint"]["source_url"]
    assert url.endswith(f"/blueprints/automation/ninja_woodfire/{name}.yaml")


@pytest.mark.parametrize("name", ALL)
def test_every_input_is_declared_and_every_declaration_is_used(name: str) -> None:
    doc = load(name)
    used = {node.name for node in walk(doc) if isinstance(node, Input)}
    declared = declared_inputs(doc)
    assert used - declared == set(), "referenced but not declared"
    assert declared - used == set(), "declared but never referenced"


@pytest.mark.parametrize("name", ALL)
def test_every_trigger_has_an_id(name: str) -> None:
    """The message templates branch on `trigger.id`; an unnamed trigger would
    fall through to whatever the last `else` happens to be."""
    for trigger in load(name)["triggers"]:
        assert trigger.get("id"), trigger


@pytest.mark.parametrize("name", ALL)
def test_the_notification_action_is_the_last_step(name: str) -> None:
    actions = load(name)["actions"]
    assert isinstance(actions[-1]["default"], Input)
    assert actions[-1]["choose"] == []


# ------------------------------------- the event names, against the constants

def integration_event_names() -> set[str]:
    """`const.EVENT_*`, read as text so this needs no Home Assistant."""
    source = (ROOT / "custom_components" / "ninja_woodfire" / "const.py").read_text()
    return set(re.findall(r'^EVENT_\w+ = "([^"]+)"', source, re.MULTILINE))


def test_the_constants_are_the_six_events_the_readme_promises() -> None:
    assert len(integration_event_names()) == 6


def event_triggers(name: str = "cook_notifications") -> list[dict]:
    return [t for t in load(name)["triggers"] if "event_type" in t]


def prompt_triggers() -> list[dict]:
    return [t for t in load("cook_notifications")["triggers"]
            if t.get("trigger") == "state"]


def test_cook_notifications_triggers_on_exactly_the_integration_events() -> None:
    assert {t["event_type"] for t in event_triggers()} == integration_event_names()


def test_trigger_ids_are_the_event_names_without_the_prefix() -> None:
    """Not cosmetic: the ids are also the values of the "notify me when"
    picker, so a mismatch silently disables that tick box."""
    for trigger in event_triggers():
        assert trigger["id"] == trigger["event_type"].removeprefix(EVENT_PREFIX)


# --------------------------------------------- the grill's own prompts

def test_the_prompt_triggers_match_what_the_parser_produces() -> None:
    """The blueprint waits for `to: "flipfood"`; the sensor reports whatever
    `parse_prompt` returns. Those are two files apart, and a mismatch is a
    notification that simply never arrives."""
    from nwf_lib.models import parse_prompt

    observed = {"1:addfood", "4:flipfood", "7:getfood"}
    assert {t["to"] for t in prompt_triggers()} == {parse_prompt(m) for m in observed}


def test_the_prompt_triggers_wait_for_the_prompt_not_for_it_to_clear() -> None:
    """They are raised for as little as ten seconds and then go back to
    `unknown`. `to:` the name; anything watching for a change would fire twice
    and the second one would say nothing."""
    for trigger in prompt_triggers():
        assert trigger["to"] == trigger["id"], trigger
        assert "from" not in trigger
        assert trigger["entity_id"].name == "prompt_sensor"


def test_done_is_not_wired_to_a_prompt() -> None:
    """`6:done` was seen raised mid-cook with `heat` resuming three seconds
    later. The grill-state event is the one that knows a cook is over."""
    assert "done" not in {t["id"] for t in prompt_triggers()}


def test_turning_the_food_is_on_by_default() -> None:
    """The point of the whole exercise: the grill asks, twice a cook, and
    until now nothing passed it on."""
    assert "flipfood" in load("cook_notifications")["blueprint"]["input"]["events"]["default"]


def test_the_device_filter_does_not_drop_the_prompt_triggers() -> None:
    """It reads `trigger.event.data.device_id`, which a state trigger has not
    got. Unguarded, every prompt notification would be silently discarded."""
    condition = load("cook_notifications")["conditions"][0]["value_template"]
    from_event = render(condition, trigger=Trigger("cook_done", {"device_id": "dev1"}),
                        grill="dev1")
    other_grill = render(condition, trigger=Trigger("cook_done", {"device_id": "dev2"}),
                         grill="dev1")
    from_prompt = render(condition, trigger=StateTrigger("flipfood"), grill="dev1")
    assert (from_event, other_grill, from_prompt) == ("True", "False", "True")


def test_the_picker_offers_every_event_and_defaults_to_a_sensible_subset() -> None:
    doc = load("cook_notifications")
    events = doc["blueprint"]["input"]["events"]
    offered = {option["value"] for option in events["selector"]["select"]["options"]}
    ids = {t["id"] for t in doc["triggers"]}

    assert offered == ids
    assert set(events["default"]) <= offered
    # The halfway pair is the noisy one on a short cook; off unless asked for.
    assert "cook_halftime" not in events["default"]
    assert "probe_halfway" not in events["default"]


# ------------------------------------------------------------- the templates

def render_raw(template: str, **context) -> str:
    """Render exactly what Home Assistant would, before it strips the result."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    # Home Assistant globals the blueprints use. Only `grill_alerts` needs
    # them; a plain Jinja environment has neither.
    env.globals["states"] = lambda entity: context.get("_states", {}).get(entity, "unknown")
    env.globals["device_attr"] = lambda _entity, attr: context.get("_device", {}).get(attr)
    return env.from_string(template).render(**context)


def render(template: str, **context) -> str:
    return render_raw(template, **context).strip()


def message_templates(name: str) -> dict[str, str]:
    """The `variables:` step that composes the text."""
    for action in load(name)["actions"]:
        if "variables" in action:
            return action["variables"]
    raise AssertionError("no variables step")


class Trigger:
    def __init__(self, trigger_id: str, event_data: dict | None = None,
                 to_state: str | None = None, from_state: str | None = None):
        self.id = trigger_id
        self.event = type("Event", (), {"data": event_data or {}})()
        self.to_state = type("State", (), {"state": to_state})()
        self.from_state = type("State", (), {"state": from_state})()


class StateTrigger:
    """A state trigger: an entity_id and no `event` attribute whatsoever."""

    def __init__(self, trigger_id: str, entity_id: str = "sensor.woodninja_prompt"):
        self.id = trigger_id
        self.entity_id = entity_id


COOK_EVENT = {
    "dsn": "AC000W000000000",
    "device_id": "dev1",
    "device_name": "WoodNinja",
    "mode": "air crisp",
    "setpoint": 190,
    "seconds_set": 1200,
    "smoke": False,
}


@pytest.mark.parametrize(
    "trigger_id",
    [
        "cook_started",
        "preheat_complete",
        "cook_halftime",
        "cook_done",
        "probe_target_reached",
        "probe_halfway",
    ],
)
def test_every_cook_notification_says_something(trigger_id: str) -> None:
    """Each branch must produce text, name the grill, and be distinct.

    A missing `{% elif %}` renders as an empty string — a push notification
    with a blank body, which is exactly the failure that would otherwise reach
    a user mid-cook.
    """
    variables = message_templates("cook_notifications")
    data = {**COOK_EVENT, "reason": "done", "probe_index": 1}
    context = {
        "trigger": Trigger(trigger_id, data),
        "grill_name": "WoodNinja",
        "cook_mode": "Air Crisp",
        "probe": "Probe 2",
        "minutes": 20,
    }
    title = render(variables["title"], **context)
    message = render(variables["message"], **context)

    assert title and message
    assert "WoodNinja" in title
    assert message.endswith("."), message
    assert "{" not in message and "}" not in message
    # A folded scalar keeps the line break when a continuation line is
    # indented further than the first, which lands verbatim in the push.
    assert "\n" not in message and "  " not in message, repr(message)


@pytest.mark.parametrize("part", ["title", "message"])
def test_the_cook_notification_branches_are_all_different(part: str) -> None:
    """Two events that render the same text are two events one of which is
    pointless — and the titles are what a phone shows on the lock screen."""
    variables = message_templates("cook_notifications")
    seen = {}
    for trigger_id in [t["id"] for t in load("cook_notifications")["triggers"]]:
        data = {**COOK_EVENT, "reason": "done", "probe_index": 0}
        seen[trigger_id] = render(
            variables[part],
            trigger=Trigger(trigger_id, data),
            grill_name="WoodNinja", cook_mode="Air Crisp",
            probe="Probe 1", minutes=20,
        )
    assert len(set(seen.values())) == len(seen), seen


def test_a_stopped_cook_does_not_claim_the_food_is_ready() -> None:
    variables = message_templates("cook_notifications")
    data = {**COOK_EVENT, "reason": "stopped"}
    message = render(
        variables["message"],
        trigger=Trigger("cook_done", data),
        grill_name="WoodNinja", cook_mode="Air Crisp", probe="Probe 1", minutes=20,
    )
    assert "stopped" in message
    assert "ready" not in message


def test_the_grill_names_itself_and_falls_back_when_it_cannot() -> None:
    variables = message_templates("cook_notifications")
    for device_name, expected in [("WoodNinja", "WoodNinja"), (None, "The grill")]:
        data = {**COOK_EVENT, "device_name": device_name}
        assert render(variables["grill_name"], trigger=Trigger("cook_started", data)) == expected


def test_a_prompt_notification_names_the_grill_from_its_entity() -> None:
    """There is no event to read the name off, so it comes from the device
    behind the sensor — and still follows a rename."""
    variables = message_templates("cook_notifications")
    assert render(variables["grill_name"], trigger=StateTrigger("flipfood"),
                  _device={"name": "Ninja Woodfire", "name_by_user": "Patio grill"}) \
        == "Patio grill"
    assert render(variables["grill_name"], trigger=StateTrigger("flipfood"),
                  _device={}) == "The grill"


def test_probe_messages_are_one_indexed_like_the_grills_own_display() -> None:
    variables = message_templates("cook_notifications")
    assert render(variables["probe"], trigger=Trigger("x", {"probe_index": 0})) == "Probe 1"
    assert render(variables["probe"], trigger=Trigger("x", {"probe_index": 1})) == "Probe 2"


def test_probe_messages_quote_no_temperature() -> None:
    """The payload is always °C. A household displaying °F would be told the
    wrong number, so the messages describe the milestone instead."""
    variables = message_templates("cook_notifications")
    for trigger_id in ["probe_target_reached", "probe_halfway"]:
        data = {**COOK_EVENT, "probe_index": 0, "target": 63, "current": 63.5}
        message = render(
            variables["message"],
            trigger=Trigger(trigger_id, data),
            grill_name="WoodNinja", cook_mode="", probe="Probe 1", minutes=0,
        )
        assert "63" not in message
        assert "°" not in message


@pytest.mark.parametrize("trigger_id", ["lid_open", "error", "offline"])
def test_every_grill_alert_says_something(trigger_id: str) -> None:
    variables = message_templates("grill_alerts")
    context = {
        "trigger": Trigger(trigger_id, to_state="3"),
        "grill_name": "WoodNinja",
        "grill_state_entity": "sensor.woodninja_grill_state",
        "_device": {"name": "WoodNinja", "name_by_user": None},
    }
    title = render(variables["title"], **context)
    message = render(variables["message"], **context)
    assert "WoodNinja" in title
    assert message and "{" not in message


def test_the_error_alert_quotes_the_code() -> None:
    variables = message_templates("grill_alerts")
    message = render(
        variables["message"],
        trigger=Trigger("error", to_state="7"), grill_name="WoodNinja",
    )
    assert "7" in message


def test_a_renamed_grill_wins_in_the_alerts_too() -> None:
    variables = message_templates("grill_alerts")
    name = render(
        variables["grill_name"],
        grill_state_entity="sensor.x",
        _device={"name": "Ninja Woodfire", "name_by_user": "Patio grill"},
    )
    assert name == "Patio grill"


# ------------------------------------------------- alerts only fire mid-cook

def alerts_condition():
    return load("grill_alerts")["conditions"][0]["value_template"]


def cooking(state: str, *, trigger_id: str = "lid_open",
            offline_enabled: bool = True) -> bool:
    """Render the real condition scalar and read its verdict."""
    doc = load("grill_alerts")
    return render(
        alerts_condition(),
        trigger=Trigger(trigger_id, from_state=state),
        grill_state_entity="s.x",
        cooking_states=doc["variables"]["cooking_states"],
        offline_enabled=offline_enabled,
        _states={"s.x": state},
    ) == "True"


def test_grill_alerts_only_fire_while_something_is_cooking() -> None:
    """An open lid on a cold grill is a normal Tuesday, not an alert."""
    assert cooking("cooking")
    assert cooking("preheat")
    assert not cooking("idle")
    assert not cooking("powered OFF")
    assert not cooking("unknown")


def test_the_offline_alert_looks_at_where_the_grill_came_from() -> None:
    """The one that has to be different.

    A grill that stops reporting takes its state sensor unavailable with it —
    availability is gated on liveness — so by the time the trigger fires,
    `states()` answers "unavailable" and a naive gate would drop every offline
    alert there is. `from_state` still remembers the cook.
    """
    assert not cooking("unavailable", trigger_id="lid_open")
    assert cooking("cooking", trigger_id="offline")
    assert not cooking("idle", trigger_id="offline")
    # ...and the checkbox that turns it off actually turns it off, without
    # touching the other two alerts.
    assert not cooking("cooking", trigger_id="offline", offline_enabled=False)
    assert cooking("cooking", trigger_id="lid_open", offline_enabled=False)


def test_a_firmware_fault_is_reported_whatever_the_grill_says_it_is_doing() -> None:
    """The gate that killed the offline alert must not be allowed to kill this
    one. `error` is not in ACTIVE_COOK_STATES, so a grill reporting its fault
    as `error` would silence the very alert about it."""
    assert cooking("error", trigger_id="error")
    assert cooking("idle", trigger_id="error")
    assert cooking("cooking", trigger_id="error")
    # ...but the other two stay gated.
    assert not cooking("idle", trigger_id="lid_open")


def test_the_condition_renders_without_stray_whitespace() -> None:
    """It only ever worked because Home Assistant strips the result. A folded
    scalar keeps the break when a continuation line is indented further —
    the same trap the message templates guard against."""
    raw = render_raw(
        alerts_condition(),
        trigger=Trigger("lid_open", from_state="cooking"),
        grill_state_entity="s.x", cooking_states=["cooking"],
        offline_enabled=True, _states={"s.x": "cooking"},
    )
    assert raw == "True", repr(raw)


def test_the_offline_alert_waits_before_crying_wolf() -> None:
    """A grill that blinks out for one poll has not gone anywhere."""
    offline = next(t for t in load("grill_alerts")["triggers"] if t["id"] == "offline")
    assert offline["for"], "needs a settle period"


def test_the_offline_alert_watches_the_state_sensor_not_connectivity() -> None:
    """Connectivity is the wrong signal twice over: it drops in the same pass
    as the state sensor, and a migrated grill freezes while still connected."""
    doc = load("grill_alerts")
    offline = next(t for t in doc["triggers"] if t["id"] == "offline")
    assert offline["entity_id"].name == "grill_state"
    assert offline["to"] == "unavailable"


def test_the_cooking_states_have_not_drifted_from_the_integration() -> None:
    """A third copy of this list, in YAML where nothing imports it.

    The other two had already drifted apart once, which is why
    `const.ACTIVE_STATES` re-exports `models.ACTIVE_COOK_STATES` instead of
    restating it. This one cannot re-export, so it gets a test.
    """
    from nwf_lib.models import ACTIVE_COOK_STATES

    listed = load("grill_alerts")["variables"]["cooking_states"]
    assert sorted(listed) == sorted(ACTIVE_COOK_STATES)
    assert len(listed) == len(set(listed))


def test_grill_alerts_can_be_left_unset() -> None:
    """The two entity-backed alerts default to an empty list, so an
    unconfigured trigger never matches instead of blocking the import."""
    inputs = load("grill_alerts")["blueprint"]["input"]["alerts"]["input"]
    for key in ["lid_sensor", "error_sensor"]:
        assert inputs[key]["default"] == []
        assert inputs[key]["selector"]["entity"]["multiple"] is True
    # The offline alert needs no entity of its own — it watches the state
    # sensor that every other alert already depends on — so it is a checkbox.
    assert inputs["offline"]["default"] is True
    assert "boolean" in inputs["offline"]["selector"]
