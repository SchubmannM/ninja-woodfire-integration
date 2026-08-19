# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

A Home Assistant custom integration (`custom_components/ninja_woodfire/`) for the
Ninja Woodfire Connect Pro XL grill, distributed via HACS.

This is a **maintained fork** of `coxtor/ninja-woodfire-integration`, which is no
longer developed. The fork exists because SharkNinja is migrating grills onto a
new cloud backend that the original does not support — see *Two backends* below.

Only the EU `OG900-EU` model is actually tested; NA endpoints and other
OG9-series models are bundled but unverified.

## Development workflow

`homeassistant` is not expected to be installed locally. Most of the codebase is
still testable without it, because everything under `_lib/` is deliberately free
of Home Assistant imports.

The toolchain is pinned with [mise](https://mise.jdx.dev). `mise.toml` fixes
Python to the version Home Assistant itself ships (3.14 as of HA 2026.3) and
auto-creates `.venv` on `cd`, so the interpreter you develop against is the one
the integration will actually run on:

```bash
mise install       # fetch the pinned Python, create .venv
mise run install   # test deps + git hooks
mise run test      # 53 tests, ~2s, no HA needed
mise run check     # everything the pre-commit hooks check
mise run compile   # syntax check without importing HA
mise run deploy    # release + roll out to HA (see below)
```

`mise run <task>` carries mise's environment itself, so those work with no
shell setup. Bare `python` / `pytest` only resolve inside the repo if mise is
hooked into the shell — otherwise the `[env]` venv activation in `mise.toml`
never fires and you get `command not found: python`:

```bash
echo 'eval "$(mise activate zsh)"' >> ~/.zshrc && exec zsh
```

Tasks address `.venv/bin/...` explicitly rather than relying on PATH. mise
creates the venv with `uv`, which does not put `pip` inside it, so a bare `pip`
falls through to the next interpreter on PATH and installs somewhere else
entirely — leaving the venv empty while everything still appears to work,
against the wrong Python.

Without mise, the equivalent is a `.venv` plus `requirements-test.txt` and
`pre-commit install` — but then nothing pins the interpreter, and it is easy to
develop on a Python newer than the runtime and ship syntax that fails there.
Note the floor is lower than the ceiling: `hacs.json` declares a minimum of HA
2024.12, which ran Python 3.12, so avoid post-3.12 syntax unless that minimum
moves too.

Deploying to a real HA instance is still the only way to exercise the entity
layer. HA cannot hot-reload a custom integration — the old modules stay
imported, so a restart is always required. `scripts/deploy.py` automates the
whole loop (tag → GitHub release → HACS install → restart → wait) against a
Home Assistant OS instance over its REST API; it needs `.env.deploy` with
`HA_URL` and `HA_TOKEN`. For a filesystem-accessible instance:

```bash
cp -r custom_components/ninja_woodfire <ha-config>/custom_components/
```

Do **not** put `custom_components/ninja_woodfire/` on `sys.path` — `select.py`,
`button.py` and `number.py` shadow stdlib modules and break the interpreter in
confusing ways. `tests/conftest.py` works around this by registering a synthetic
`nwf_lib` package that maps straight at the `_lib` directory, so tests import
`nwf_lib.models` rather than going through the HA package.

Releases: bump `version` in `manifest.json` and commit; HACS reads it from there.

## Two backends

**This is the single most important thing to understand about this repo.**

SharkNinja is migrating grills off Ayla onto their own AWS-backed service. When
the phone app writes `Cloud_Mode = 1` to a grill's AWS device shadow, that grill
**stops publishing telemetry to Ayla completely** — while Ayla keeps serving the
last datapoint it ever received, forever, with no error and no staleness marker.

An Ayla-only integration therefore reports a plausible, tidy, entirely fictional
idle grill. Measured on a migrated OG900-EU: 2367 consecutive Ayla polls across
24 hours and two real cooks, zero changes.

| | Ayla | AWS |
|---|---|---|
| module | `_lib/api/ayla.py` | `_lib/api/aws.py` |
| reads | only until the grill is migrated | current |
| writes | **all commands go here**, works on both | not implemented |

`__init__.py` probes AWS at setup and reads from there if the grill is present,
falling back to Ayla. Both clients expose **`async read_state(dsn) -> CombinedState`**
— that is the contract the coordinator depends on, and adding a transport means
implementing it. Commands always go through the Ayla client.

Full protocol notes: `docs/AWS_API.md`, plus `docs/LOCAL_PROTOCOL.md` for the
grill's LAN interfaces (superseded, kept for reference).

### Payload dialects

The two backends serve the same firmware structures, but AWS strips **every
space** — from object keys *and* enum values (`"secondsset"`, `"lidopen"`,
`"aircrisp"`, `"poweredOFF"`). `aws.normalise` canonicalises onto the Ayla
spelling before anything else sees it, so `models.py`, the capability tables and
the cook-command payloads all use one vocabulary. Add a new multi-word mode or
state and it needs an entry there.

## Architecture

### Two layers, deliberately separated

- `_lib/` — transport layer, **no Home Assistant imports**. Cloud clients
  (`api/ayla.py`, `api/aws.py`), the shared Auth0 grant (`api/auth0.py`), state
  dataclasses (`models.py`), per-model capability tables (`capabilities.py`),
  endpoints and property names (`const.py`).
- everything else — the HA layer. `coordinator.py` is the only thing that
  touches `_lib` for reads/writes; platform modules only ever talk to the
  coordinator.

`models.py` and `capabilities.py` have no relative imports and can be exercised
standalone. Keep parsing logic in `models.py` rather than in entities.

### Liveness — reads and writes fail independently

A successful poll proves nothing about freshness: both clouds answer happily
while serving stale data. So `CombinedState` carries `online` (set by the
transport, never assumed) and `last_updated_at`, and `is_live()` requires
**both** connected and recently-reported.

Entities that mirror grill state gate availability on `coordinator.state_is_live`
via `NinjaWoodfireEntity._requires_live_state`. Entities that must stay usable
when state is unreadable set it `False`: staged cook settings, device info,
connectivity diagnostics, and **cook commands** — a grill can be unreadable and
still perfectly commandable, which is exactly the migrated-to-AWS case.

`button.py` additionally has a per-command `needs_live_cook` flag. Only
`skip_preheat` sets it, because the firmware has no skip command — it re-issues
the *entire* cook payload, so without a readable cook it would silently
overwrite the user's mode, temperature and duration.

### Coordinator: staged vs. live cook settings

`NinjaWoodfireCoordinator` holds `cook_setting_*` attributes — the cook the user
is *composing*. They live in memory only (lost on HA restart) and are consumed by
the Start button, exactly like the official app. The `live_or_staged_*`
properties return live grill values during a cook and staged values while idle;
entities read those, never the raw `cook_setting_*`.

`async_modify_cook()` implements single-field edits by re-issuing the entire cook
payload. Three firmware rules are encoded there and must be preserved:

1. **Send remaining time, not the original duration.** Re-issuing with
   `seconds set` = original resets the timer. Remaining is derived from
   `end_time_utc` (immune to poll latency) with `seconds_left` as fallback.
2. **Never re-issue during preheat.** Any cook command the firmware accepts
   mid-preheat either restarts the preheat ramp or jumps straight to cooking.
3. **Clamp to the target mode's capabilities before sending** — out-of-range
   temps or smoke in a non-smoke mode get silently snapped or rejected.

Every write is followed by `_burst_refresh()` (three polls at ~0/1.0/2.5s).
Polling is adaptive: 1s while active, 10s while idle.

### Stale-value gating

Neither cloud ever clears readings. `CombinedState.temp_is_plausible()` and
`sensor._plausible_probe_temp()` suppress readings that are offline, stale
(`last_updated_at` older than 60s), implausible for a non-cooking grill (>50 °C —
matched against `IDLE_COOK_STATES`, which covers `powered OFF` as well as `idle`),
or from a chamber the current mode doesn't use. Route any new temperature sensor
through these rather than exposing raw values.

### Lifecycle events

`coordinator._emit_lifecycle_events()` diffs each poll against the previous
snapshot and fires HA bus events (`EVENT_*` in `const.py`). It returns early on a
non-live snapshot, so a grill dropping off mid-cook cannot fire a bogus
`cook_done` on reconnect. One-shot events use `_*_fired` flags reset in **two**
places — cook start and cook done — so adding an event means adding its reset to
both, and updating the list in `README.md`.

### Capabilities table

`_lib/capabilities.py` maps `oem_model` → `GrillCapabilities` (per-mode temp
ranges, duration caps, smoke/probe support, probe count). Lookup is exact match,
then `OG9*` → `WOODFIRE_PRO_XL`, then a one-mode `GENERIC` fallback. This drives
entity min/max, step, unit and `available` — supporting a new grill means adding
an entry here, not touching entity code.

### Entity pattern

Each platform declares a frozen dataclass description table (`SENSORS`,
`BINARY_SENSORS`, `SWITCHES`, `BUTTONS`) with `value_fn` / `get_fn` / `set_fn` /
`available_fn` / `post_set_fn` lambdas over the coordinator or `CombinedState`,
plus one generic entity class. Adding an entity is normally a new table row plus
translation keys — no new class. `number.py` and `select.py` are hand-written
because their bounds are mode-dependent.

Unique IDs are always `f"{coordinator.dsn}_{key}"`.

## Tests

`tests/` runs without Home Assistant. Fixtures in `tests/fixtures/` are
byte-preserved captures from a real OG900-EU with identifiers scrubbed —
`aws_*.json` from the AWS backend, the rest from Ayla. They keep the firmware's
tab-indented JSON verbatim; a test asserts that, because a reformatted fixture no
longer proves the parser handles the real wire format.

`conftest.py` exposes `ayla_fixture_names()` / `aws_fixture_names()` — the two
sets have different shapes, so never glob all fixtures into one parametrize.

When you learn something new about the hardware, add a fixture. Most of the
subtle bugs found so far were caught by parsing a real payload, not by reasoning.

## Conventions and gotchas

- **Temperature semantics are mode-dependent.** In `grill` mode `temp` is a heat
  level 1/2/3 (Lo/Med/Hi); every other mode uses °C. The firmware reports an
  internal °C target even in grill mode, so `sensor._setpoint_display()`
  translates it back using the coordinator's staged level as a hint, with coarse
  °C bucketing as fallback.
- **`GrillState.state` reads `"cooking"` during preheat.** The real sub-phase is
  in `CookState.state.state` (`preheat` → `heat` → `none`). Use `_cook_phase()`.
- **Translations are the real source of strings.** `translations/en.json` and
  `de.json` hold all 45 entity keys and **must stay in sync** — a pre-commit hook
  enforces that, because they have drifted before. `strings.json` has drifted too
  and only covers ~16; backfill it when convenient.
- **Bundled credentials are intentional.** The per-region Auth0/Ayla identifiers
  and the AWS `x-api-key` in `_lib/const.py` are the public ones shipped in the
  Ninja Kitchen Android app. The config flow exposes four optional override
  fields; empty strings mean "use the bundled default" (`config_flow._opt`), so
  existing entries pick up new defaults automatically.
  `scripts/extract_credentials.py` regenerates them from `adb logcat`.
- **Never commit real identifiers.** DSNs, appliance serials, MACs, household or
  user ids, JWTs, Ayla tokens. `.env` is gitignored; a pre-commit hook blocks the
  obvious shapes. Scrub fixtures.
- `models.py` references a `docs/API.md` that does not exist in this repo.
