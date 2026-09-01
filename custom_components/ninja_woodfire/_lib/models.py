"""Transport-agnostic state models.

The grill firmware exposes its state as JSON-encoded strings in three Ayla
properties (`GET_GrillState`, `GET_CookState`, `GET_ProbeState`). The same
data eventually comes over BLE as bincode-encoded structs. Both transports
hydrate into the dataclasses below — entities don't care which transport
was used.

All fields are observed in real captures; semantics are documented in
docs/API.md § 3.1 (state schemas).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# Canonical cook-phase vocabulary. The firmware reports phases in two
# places with slightly different spellings (`GET_GrillState.state` and
# `GET_CookState.state.state`), so both sets list every observed variant.
#
# This is the single source of truth: the HA layer re-exports ACTIVE_COOK_STATES
# as `const.ACTIVE_STATES` rather than keeping a second copy, which previously
# drifted (the HA copy lacked "heat", this one lacked "flip"/"lid open").
ACTIVE_COOK_STATES = frozenset({
    "start",
    "preheat", "preheating",
    "heat",
    "cook", "cooking",
    "rest", "resting",
    "flip",
    "get food", "get_food",
    "lid open", "lid_open",
})

# States in which the grill is definitely not applying heat and has not been
# for a while, so any chamber reading the cloud still serves is a leftover
# from an earlier cook rather than a live measurement. Compared casefolded.
#
# "done" is deliberately absent: a just-finished cook leaves a genuinely hot
# chamber, and suppressing that reading would be wrong.
IDLE_COOK_STATES = frozenset({
    "idle", "none", "powered off", "unknown", "",
})


def _parse_value(raw: Any) -> dict[str, Any]:
    """Ayla wraps the JSON state in a string. Tolerate both string + dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------- temps
#
# Unit handling. Neither backend states the unit of anything, anywhere.
#
# Ayla's property objects carry a fixed 24-key schema — `base_type`,
# `display_name`, `derived`, `direction`, … — and not one of those keys is a
# unit. `base_type` is a JSON type (`integer`, `string`), `display_name` is
# prose ("Air Temperature"), every temperature property has `derived: false`
# and `generated_from: null`, and the per-property detail endpoint returns
# exactly the same keys as the list endpoint. The device record has no unit
# or locale field either, and on the AWS side neither `metadata`
# (`{"deviceName": …}`) nor the device shadow — whose only properties are
# `Cloud_Mode`, `Cook_Command`, `Cook_Notifications`, `Exec_Command` and
# `user_linked` — carries one. There is no flag to read, so the unit of each
# field is protocol knowledge that has to be recorded here.
#
# And the payload mixes units. The firmware publishes two blocks:
#
#   * `GrillState.inputs.temps` — the **raw sensor block, in Fahrenheit**.
#   * `ProbeState.probes[]` and `GrillState.setpoint` — the **user-facing
#     values, already in Celsius**.
#
# That is not inferred from magnitudes. One captured snapshot
# (`tests/fixtures/aws_bake_probe_ambient.json`) contains the *same physical
# measurement in both blocks at once*: a probe resting in open room air reads
# `inputs.temps.probe1_a = 80` and `ProbeState.probes[0].temp = 26.6`, and
# 80 °F is 26.67 °C. Two further live samples reconciled the same way (82 ↔
# 27.7, 81 ↔ 27.2). The firmware converts for the block a human reads and
# leaves the sensor block in the scale the MCU works in.
#
# Everything else agrees. A Bake at a 160 °C setpoint held `air` between
# 294.6 and 339.4 — 145.9 °C to 170.8 °C, a thermostat cycling either side of
# its target, and unreachable as Celsius. A grill idle for 23 hours reported
# `grill` 79.5 / `air` 76.6, which is 26.4 °C / 24.8 °C: room temperature.
# Both ends of the scale land where physics says they should.
#
# This is why the integration reported roughly double: it took the raw block
# at face value and labelled it CELSIUS.

def fahrenheit_to_celsius(value: float) -> float:
    """Convert one Fahrenheit reading to Celsius."""
    return (value - 32.0) * 5.0 / 9.0


# The keys inside `inputs.temps` that are Fahrenheit, named explicitly.
#
# A list, not a heuristic. "Assume Fahrenheit above 250" would be unsafe in
# both directions — 200 °C is a legitimate Air Crisp target and 200 °F is a
# legitimate warming one — and it would silently change a reading's meaning
# as the grill heats. A field is on this list because it was observed, or
# because it sits in a block whose unit was observed; nothing here depends on
# the value.
#
# `main` and `ui` are deliberately absent. Ayla names them "Main PCB
# Temperature" and "UI PCB Temperature", but they read 6542.4 and 6513.6 in
# every capture ever taken — across both backends, idle, mid-cook and powered
# off — never varying by so much as 0.1. They are PCB analog reads (ADC counts
# × 0.1) that were given temperature-shaped names, and they are neither °F nor
# °C.
FAHRENHEIT_TEMP_FIELDS = frozenset({
    "grill",
    "air",
    "smoke",
    "probe0_a", "probe0_b",
    "probe1_a", "probe1_b",
})


@dataclass
class GrillTemps:
    """Live sensor readings from the grill MCU, **normalised to °C**.

    The wire format is Fahrenheit (see the note above); these fields are the
    converted values, so everything downstream — entities, plausibility
    gating, lifecycle events — works in one unit. `GrillState.raw` still
    holds the untouched payload for diagnostics.

    grill/air are the two chamber sensors. probeN_a/probeN_b are the raw
    dual-element readings of each meat probe; nothing consumes them, because
    the firmware already averages them into `ProbeInfo.temp` in Celsius, and
    that is the value the integration exposes.

    `smoke` is converted with its block, but is not a trustworthy reading.
    It never falls below ~227 °F (108 °C), including on a grill switched off
    at the socket for 23 hours, so it carries a large offset or is an
    unpopulated channel. It has also never been captured with the Woodfire
    box actually lit — every capture has `smoke: 0` — so what it does when it
    matters is unobserved. `temp_is_plausible` hides it unless smoke is on,
    which is the only reason exposing it at all is defensible.

    main/ui are PCB ADC counts, not temperatures, and are passed through
    untouched — see FAHRENHEIT_TEMP_FIELDS.
    """

    grill: float = 0.0
    air: float = 0.0
    smoke: float = 0.0
    probe0_a: float = 0.0
    probe0_b: float = 0.0
    probe1_a: float = 0.0
    probe1_b: float = 0.0
    main: float = 0.0  # PCB ADC, not a temperature in any unit
    ui: float = 0.0    # PCB ADC, not a temperature in any unit

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> "GrillTemps":
        """Parse one `inputs.temps` object, converting °F fields to °C.

        The single place the Fahrenheit-to-Celsius step happens. Both
        transports carry the same firmware structure, so doing it here covers
        Ayla and AWS at once and means no entity ever sees a raw °F value.

        Rounded to one decimal: that is the precision the firmware itself
        uses for the Celsius values it publishes, and it keeps 26.666666666
        out of the UI.
        """
        def read(name: str) -> float:
            value = float(raw.get(name, 0) or 0)
            if name in FAHRENHEIT_TEMP_FIELDS:
                return round(fahrenheit_to_celsius(value), 1)
            return value

        return cls(
            grill=read("grill"),
            air=read("air"),
            smoke=read("smoke"),
            probe0_a=read("probe0_a"),
            probe0_b=read("probe0_b"),
            probe1_a=read("probe1_a"),
            probe1_b=read("probe1_b"),
            main=read("main"),
            ui=read("ui"),
        )


# ---------------------------------------------------------------- grill state

def parse_prompt(message: str) -> str:
    """The name out of a firmware prompt like ``"4:flipfood"``.

    User-facing prompts arrive in ``GrillState.message`` as ``"<bit>:<name>"``,
    paired with ``eventmask`` as a bitfield of everything currently raised —
    the number before the colon is the bit index, so ``4:flipfood`` comes with
    ``0x10`` and ``7:getfood`` with ``0x80``. Only the name means anything to a
    user; the index is recoverable from the mask if it ever matters.

    Captured on an OG900-EU: ``1:addfood`` when preheat ends, ``4:flipfood`` at
    the tick cook progress crosses 50%, ``6:done``, and ``7:getfood``. Parsed
    tolerantly rather than against that list, so a prompt nobody has captured
    yet still arrives under its own name instead of being dropped.

    These are brief — ``flipfood`` was raised for ten seconds — which is fine
    against the one-second poll of an active cook, but means anything watching
    must react to the *transition*, not wait for the state to settle.

    The name is canonicalised the way ``aws.normalise`` treats modes and
    states: spaces and underscores dropped, casefolded. That function does
    **not** touch ``message``, and no Ayla capture has ever carried a prompt —
    every one of them is empty — so the Ayla spelling is unknown. This
    firmware writes these same words both ways elsewhere (``"get food"`` and
    ``"get_food"`` are both in ``ACTIVE_COOK_STATES``), and AWS stripping
    ``"4:flip food"`` down to ``"4:flipfood"`` is exactly what the capture
    looks like. One spelling out of both dialects means consumers match one
    string, instead of a notification quietly never arriving on a grill that
    was never migrated.
    """
    text = str(message or "").strip()
    if not text:
        return ""
    _, sep, name = text.partition(":")
    # Only fall back to the whole string when there was no index to strip;
    # "4:" is a prompt with no name, not a prompt named "4:".
    raw = name if sep else text
    return raw.strip().replace(" ", "").replace("_", "").casefold()


@dataclass
class GrillState:
    """Consolidated grill state — what the grill display shows.

    Populated from `GET_GrillState.value`. Most fields are only present
    while a cook is active; defaults are safe for `state == "idle"`.
    """

    # Always present
    state: str = "unknown"           # idle | preheat | cooking | rest | done | …
    message: str = ""                # raw "<bit>:<name>", e.g. "4:flipfood"
    prompt: str = ""                 # just the name — see parse_prompt
    event_mask: str = ""
    lid_open: bool = False
    sim: int = 0
    temps: GrillTemps = field(default_factory=GrillTemps)
    raw: dict[str, Any] = field(default_factory=dict)

    # Only present when state != idle (None when idle)
    mode: str | None = None          # grill | smoker | bake | roast | broil | …
    # Target value; semantics depend on mode (heat level 1/2/3 in `grill`
    # mode, otherwise °C). Already Celsius on the wire — do NOT convert.
    # A Bake the user set to 160 °C reports `setpoint: 160`, and the
    # capability table's ranges (bake 120-210) are the same scale. This is
    # the user-facing block; only `inputs.temps` is Fahrenheit.
    setpoint: int | None = None
    seconds_set: int | None = None   # original cook duration in seconds
    seconds_left: int | None = None  # remaining time in seconds
    end_time_utc: int | None = None  # UNIX timestamp of cook end
    smoke: bool = False              # True if "Woodfire-Aromatechnologie" active
    error: int = 0                   # firmware error code, 0 = OK
    probes_active: int = 0           # 0/1/2

    @classmethod
    def from_property_value(cls, raw: Any) -> "GrillState":
        d = _parse_value(raw)
        inputs = d.get("inputs", {})
        temps_raw = inputs.get("temps", {})
        io = inputs.get("io", {})
        return cls(
            state=str(d.get("state", "unknown")),
            message=str(d.get("message", "")),
            prompt=parse_prompt(d.get("message", "")),
            event_mask=str(d.get("eventmask", "")),
            lid_open=bool(io.get("lid open", 0)),
            sim=int(d.get("sim", 0)),
            temps=GrillTemps.from_wire(temps_raw),
            mode=d.get("mode") if d.get("mode") else None,
            setpoint=int(d["setpoint"]) if "setpoint" in d else None,
            seconds_set=int(d["seconds set"]) if "seconds set" in d else None,
            seconds_left=int(d["seconds left"]) if "seconds left" in d else None,
            end_time_utc=int(d["endtimeutc"]) if "endtimeutc" in d else None,
            smoke=bool(d.get("smoke", 0)),
            error=int(d.get("error", 0)),
            probes_active=int(d.get("probes active", 0)),
            raw=d,
        )


# ---------------------------------------------------------------- cook state

@dataclass
class CookState:
    """The cook-state-machine's current step.

    Populated from `GET_CookState.value`. The nested `state` field can be
    either a plain string ("none") or an object with `state` + `progress`
    (e.g. {"state":"preheat","progress":75}).
    """

    state: str = "none"           # none | start | preheat | heat | cooking | flip | rest | done | error | lid_open
    progress: int | None = None   # 0-100, only when state has progress (preheat/cook/rest)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_property_value(cls, raw: Any) -> "CookState":
        d = _parse_value(raw)
        nested = d.get("state", {})
        if isinstance(nested, dict):
            state_name = str(nested.get("state", "none"))
            progress = nested.get("progress")
        else:
            state_name = str(nested)
            progress = None
        return cls(
            state=state_name,
            progress=int(progress) if progress is not None else None,
            raw=d,
        )


# ---------------------------------------------------------------- probes

@dataclass
class ProbeMode:
    """Probe target — either manual setpoint or a doneness preset."""

    mode: str = "none"             # none | manual | preset
    # Target temperature in °C (manual mode). Celsius in both directions:
    # the integration writes `{"mode": "manual", "setpoint": N}` in °C, and
    # this lives in `ProbeState`, the block the firmware publishes in °C.
    # No capture has yet read back an *active* probe target, so the read
    # path is consistent-by-block rather than directly observed.
    setpoint: int | None = None
    preset_index: int | None = None
    protein: int | str | None = None     # ProteinKind enum or string: Beef|Poultry|Chicken|…
    cut: int | str | None = None
    doneness: int | str | None = None    # Doneness enum or string: Rare|MedRare|Med|MedWell|Well

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProbeMode":
        def _parse_int_or_str(value: Any) -> int | str | None:
            """Parse field that can be either int enum or string name."""
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    return value
            return None

        return cls(
            mode=str(d.get("mode", "none")) if d else "none",
            setpoint=int(d["setpoint"]) if d and "setpoint" in d else None,
            preset_index=int(d["preset_index"]) if d and "preset_index" in d else None,
            protein=_parse_int_or_str(d.get("protein")) if d else None,
            cut=_parse_int_or_str(d.get("cut")) if d else None,
            doneness=_parse_int_or_str(d.get("doneness")) if d else None,
        )


@dataclass
class ProbeInfo:
    """Per-probe state, read from `GET_ProbeState.probes[i]`."""

    name: str = ""                 # "probe0" or "probe1"
    plugged_in: bool = False
    active: bool = False           # True when a cook is using this probe
    # Current measured probe temperature, **already °C on the wire** — do
    # NOT convert. This is the firmware's own conversion of the raw
    # elements in `GrillState.inputs.temps.probeN_a/b`, which are °F: a
    # probe in open room air reported `probe1_a: 80` here as `temp: 26.6`.
    # Converting this too was the obvious wrong fix; it would have read
    # -3 °C for a probe sitting in a warm kitchen.
    temp: float = 0.0
    progress: int = 0              # 0-100 progress towards setpoint
    target: ProbeMode = field(default_factory=ProbeMode)
    state: str = "none"            # cooking | done | none | get_food | flip_food | …

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProbeInfo":
        mode_obj = d.get("mode", {})
        state_obj = d.get("state", {})
        if isinstance(state_obj, dict):
            state = str(state_obj.get("state", "none"))
        else:
            state = str(state_obj)
        return cls(
            name=str(d.get("name", "")),
            plugged_in=bool(d.get("plugged in", 0)),
            active=bool(d.get("active", 0)),
            temp=float(d.get("temp", 0)),
            progress=int(d.get("progress", 0)),
            target=ProbeMode.from_dict(mode_obj if isinstance(mode_obj, dict) else {}),
            state=state,
        )


@dataclass
class ProbeState:
    probes: list[ProbeInfo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_property_value(cls, raw: Any) -> "ProbeState":
        d = _parse_value(raw)
        probes_raw = d.get("probes", []) or []
        return cls(
            probes=[ProbeInfo.from_dict(p) for p in probes_raw],
            raw=d,
        )


# ---------------------------------------------------------------- combined

@dataclass
class CombinedState:
    """Snapshot of the entire grill — what HA entities consume."""

    dsn: str
    grill: GrillState = field(default_factory=GrillState)
    cook: CookState = field(default_factory=CookState)
    probes: ProbeState = field(default_factory=ProbeState)
    # Whether the grill currently holds a session with the transport it was
    # read through. Set by the transport — never assume it. For the Ayla cloud
    # transport this mirrors the device record's `connection_status`, which is
    # the only way to tell "idle grill" apart from "grill that has not spoken
    # to the cloud since yesterday": the cloud keeps serving the last datapoint
    # it ever received either way.
    online: bool = True
    # Raw transport-reported status string, kept verbatim for diagnostics
    # (Ayla: "Online" / "Offline").
    connection_status: str | None = None
    # ISO-8601 timestamp from `property.data_updated_at` — when the grill
    # last reported any of these values. If older than ~60s the grill is
    # most likely off / in standby and the cached temps are stale.
    last_updated_at: Any = None  # datetime | None at runtime

    def state_age_seconds(self) -> float | None:
        """Seconds since the grill last reported, or None if unknown."""
        if self.last_updated_at is None:
            return None
        from datetime import datetime, timezone
        return (datetime.now(tz=timezone.utc) - self.last_updated_at).total_seconds()

    def is_stale(self, max_age_seconds: int = 60) -> bool:
        """True if the transport's last-reported timestamp is too old to trust."""
        age = self.state_age_seconds()
        if age is None:
            return False  # be lenient if we can't tell
        return age > max_age_seconds

    def is_live(self, max_age_seconds: int = 300) -> bool:
        """Whether this snapshot describes the grill *now*.

        Both halves matter. An offline grill is not live no matter how recent
        its last datapoint looks, and a grill that is nominally connected but
        has not reported for minutes is not live either — the OG900-EU pushes
        a snapshot when its Wi-Fi module connects and then goes quiet, so a
        "connected" flag on its own says nothing about freshness.
        """
        return self.online and not self.is_stale(max_age_seconds)

    # Modes that exercise each chamber sensor. Outside this set the
    # cloud may keep returning a (stale or noisy) value but the
    # chamber isn't being used — better to hide than to mislead.
    _AIR_TEMP_MODES = frozenset({
        "air crisp", "bake", "roast", "reheat", "dehydrate",
    })

    def temp_is_plausible(self, value: float, name: str = "grill") -> bool:
        """Whether a temperature reading should be shown.

        Args:
            value: the reading in °C. `GrillTemps.from_wire` has already
                converted it out of the wire's Fahrenheit, so the 50 °C
                ambient threshold below means what it says — before that
                conversion existed it was silently comparing °F against it,
                and hid every genuine idle reading as if it were stale.
            name: which sensor — "grill", "air", "smoke". Drives
                  per-mode plausibility (e.g. smoke chamber only
                  reports meaningfully when smoke=on; air chamber
                  only matters in air-crisp / bake / dehydrate).
        """
        if value is None:
            return False
        # An offline grill cannot be reporting a live temperature, however
        # plausible the cached number looks.
        if not self.online:
            return False
        if self.is_stale():
            return False
        # Not-cooking grill: stale cache from the last cook session keeps
        # leaking — anything above ambient is implausible. Matched against
        # every non-heating state, not just "idle": the firmware also reports
        # "powered OFF" (control panel off, module still up) and served a
        # leftover 82 °C chamber reading in that state in real captures.
        if self.grill.state.strip().casefold() in IDLE_COOK_STATES and value > 50:
            return False
        # Active cook: gate sensors by which chamber is in play.
        if self.grill.state in ACTIVE_COOK_STATES:
            mode = self.grill.mode
            if name == "smoke" and not self.grill.smoke:
                return False
            if name == "air" and mode and mode not in self._AIR_TEMP_MODES:
                return False
        return True
