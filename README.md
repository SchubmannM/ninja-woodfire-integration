# Ninja Woodfire — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Hobby project — published as-is, no support, no warranty.**
> See [Status & support](#status--support) and [Disclaimer](#disclaimer)
> below before installing.

> **Note:** This is primarily a Claude-assisted project.

> **This is a maintained fork.** The original by
> [@coxtor](https://github.com/coxtor/ninja-woodfire-integration) is no longer
> being developed. This fork adds support for SharkNinja's AWS backend, which
> grills are being migrated onto — without it, a migrated grill reports a
> stale snapshot indefinitely and appears permanently idle. Install from this
> repository, and open issues here rather than upstream.

Home Assistant integration for the Ninja Woodfire Connect Pro XL
outdoor grill. Connects via your Ninja Kitchen account; no local
network access to the grill is required.

For a custom Lovelace card, see [ninja-woodfire-card](https://github.com/coxtor/ninja-woodfire-card).

## Features

- Live sensors: grill / air / smoke / probe temperatures, cook progress,
  preheat progress, end-time, active mode, current setpoint, lid state,
  per-probe status & target.
- The grill's own prompts — *add the food*, *turn the food*, *take the food
  out* — as `Prompt`, so you can be told what the appliance is asking for
  rather than inferring it from the timer.
- Cook controls: start, stop, skip-preheat. Mode-aware temperature
  number (heat-level for grill, °C for everything else). Duration,
  smoke, per-probe targets.
- Adaptive polling — fast while cooking, relaxed while idle.
- Auto-reconnect on token expiry.
- Push notifications, via two [ready-made blueprints](#notifications) — no
  YAML to write.
- HA event-bus events for automations:
  - `ninja_woodfire_cook_started`
  - `ninja_woodfire_preheat_complete`
  - `ninja_woodfire_cook_halftime`
  - `ninja_woodfire_cook_done`
  - `ninja_woodfire_probe_target_reached`
  - `ninja_woodfire_probe_halfway`
- Connectivity diagnostics: `Backend`, `Cloud connected` and `Last cloud report`,
  so a grill that isn't reporting is visible as such rather than
  silently rendering as an idle grill (see *Known limitation* below).

## Two backends: Ayla and AWS

SharkNinja is migrating Ninja grills off Ayla onto their own AWS-backed
service. The integration handles both and picks automatically at setup:
if your grill is present on the AWS backend it reads state from there,
otherwise it uses Ayla. The `Backend` diagnostic sensor shows which is
in play. **Commands always go through Ayla**, which accepts them on
either kind of grill.

You do not need to configure anything for this — the AWS API accepts the
same Ninja account login, so sign-in is unchanged. Full protocol notes
are in [docs/AWS_API.md](docs/AWS_API.md).

### Why this matters

Once the phone app migrates a grill (it writes `Cloud_Mode = 1` to the
grill's AWS device shadow), **that grill stops publishing telemetry to
Ayla completely** — while Ayla carries on serving the last datapoint it
ever received, forever, with no error and no staleness marker. An
Ayla-only integration therefore shows a plausible, tidy, and entirely
fictional idle grill.

Measured on a migrated `OG900-EU`: **2367 consecutive Ayla polls across
24 hours and two real cooks, zero changes.** Ayla reported
`connection_status: Offline` and a `GET_GrillState` of `idle` whose
`data_updated_at` was 24 hours old, while the AWS backend reported the
same grill's true state updated seconds earlier. Ayla's entire 30-day
datapoint history held five state datapoints, all written at
module-connect time.

**Commands follow the reads onto AWS.** A migrated grill does not
receive Ayla datapoints either: the cloud accepts them, the grill never
acknowledges them, and the write eventually times out. So on an affected
grill both reads and commands go to the AWS backend, and Ayla is unused.

One consequence worth knowing:

- **Skip preheat is unavailable** when state cannot be read. The firmware
  has no dedicated skip command — it is the whole cook payload re-issued
  with a flag — so without being able to read the running cook it would
  silently replace your mode, temperature and duration with whatever is
  staged.
  Beyond that, **a command sent while the grill is unplugged is delivered
  when it next powers on**, because the shadow holds the pending value.
  That is vendor behaviour and the official app does the same, but it is
  worth knowing before you press Start on a grill switched off at the
  socket.

### If neither backend has live state

Some grills report to neither — the module publishes a snapshot when it
connects and then goes quiet, serving live state only to LAN/BLE clients
(see [docs/LOCAL_PROTOCOL.md](docs/LOCAL_PROTOCOL.md)). The integration
detects that rather than inventing state:

- entities that mirror grill state go **unavailable** instead of
  presenting a stale snapshot as current,
- `Cloud connected` reports the grill's cloud session, and
  `Last cloud report` shows exactly how far behind the cloud copy is,
- a warning is logged once per transition explaining which of the two
  failure modes you are in (grill offline, or connected-but-frozen).

If your grill *does* keep its cloud copy fresh, nothing changes for
you — everything behaves as before.

## Tested models

Tested only on the Woodfire Connect Pro XL EU (`OG900-EU`).
Other OG9-series models fall back to the same capability set and
*may* work — unverified. NA region endpoints are bundled but were
never tested by the author.

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → *Custom repositories*
2. Add this repository, category *Integration*
3. Install *Ninja Woodfire*
4. Restart Home Assistant

### Manual

Copy `custom_components/ninja_woodfire/` into your Home Assistant
`config/custom_components/` directory, then restart.

## Setup

1. *Settings → Devices & services → Add Integration → Ninja Woodfire*
2. Sign in with your Ninja Kitchen account
3. Pick your grill

The four "advanced" fields in the form are optional and used only if
the bundled defaults stop working — see [Fallback: regenerating
credentials](#fallback-regenerating-credentials) below.

## Region

EU and NA region defaults are bundled. Only EU is exercised by the
author.

## Fallback: regenerating credentials

> The override fields do not appear on the sign-in form until an attempt
> actually fails — which is the only time they are useful. If sign-in works,
> you never see them; if it stops working because SharkNinja rotated the app
> identifiers, they are there on the retry.


The integration ships with the per-region cloud identifiers used by
the official Ninja Kitchen mobile app. If those identifiers ever
rotate (vendor change, new app version, regional split), login will
start failing — at which point you can extract a fresh set from
your own phone:

```bash
git clone https://github.com/SchubmannM/ninja-woodfire-integration
cd ninja-woodfire-integration
python3 scripts/extract_credentials.py --region EU   # or NA
```

Requirements: Android phone with the Ninja Kitchen app installed,
USB debugging enabled, `adb` on your PATH. The script captures
logcat for ~30 seconds while you open the app and writes the four
values to `ninja_woodfire_credentials.txt`. Paste them into the
integration's *advanced* config-flow fields, then delete the file.

`scripts/extract_credentials.py --help` for options including
parsing an existing logfile.

## Development

The toolchain is pinned with [mise](https://mise.jdx.dev), to the Python
version Home Assistant itself ships:

```bash
mise install       # pinned Python + .venv
mise run install   # test dependencies and git hooks
mise run test      # the test suite — no Home Assistant needed
mise run check     # everything the pre-commit hooks check
```

`mise run <task>` needs no shell setup. For bare `python` / `pytest` to
resolve inside the repo, hook mise into your shell so the virtualenv
activates on `cd`:

```bash
echo 'eval "$(mise activate zsh)"' >> ~/.zshrc && exec zsh
```

## Deploying a change

Home Assistant cannot hot-reload a custom integration. HACS downloads new
files fine, but Python has already imported the old modules, so reloading
the config entry re-runs setup against the *old* code — only a restart
picks changes up. `scripts/deploy.py` doesn't remove the restart, it
removes the clicking:

```bash
# once: HA profile -> Security -> Long-lived access tokens
cat > .env.deploy <<'EOF'
HA_URL=https://your-ha-host
HA_TOKEN=<token>
EOF

python3 scripts/deploy.py              # tag + release + HACS update + restart
python3 scripts/deploy.py --restart-only   # really does restart HA
python3 scripts/deploy.py --dry-run        # changes nothing
```

It tags the version in `manifest.json`, publishes a GitHub release (which
is what makes HACS offer a versioned update rather than tracking the
branch), tells HACS to install it, restarts HA, and waits for it to come
back. `.env.deploy` is gitignored.

## Status & support

A personal project published as-is. There is **no warranty and no
guarantee of fitness for any purpose**.

- The integration depends on a third-party cloud that may change or
  break it at any time without warning — and demonstrably does: grills
  are being migrated from Ayla to an AWS backend mid-life, which is why
  this fork exists.
- Things that work today may not work tomorrow. Things that don't
  work may never work.
- The maintainer makes no commitment to keep the project alive,
  compatible with future Home Assistant versions, or working at all.
- Live state depends entirely on the grill choosing to report to the
  cloud. See *Known limitation* above — for some units it never does,
  and on those you get control but no monitoring.

If any of that is a problem for you, do not install this.

## Notifications

"Your food is ready" is the thing the phone app is actually for, and the
integration cannot send it itself — a notification needs an automation. Two
blueprints ship with the repo so you do not have to write one.

HACS installs integrations, not blueprints, so these are imported separately.
Click the button, confirm the URL, and fill in the form:

[![Import the cook-notifications blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FSchubmannM%2Fninja-woodfire-integration%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fninja_woodfire%2Fcook_notifications.yaml)

**Cook notifications** — preheat finished, time to turn the food, food ready,
meat probe at temperature. Pick your grill, tick what you want to hear about,
done. The message text is written for you; the notification action defaults to
`notify.notify` (every device signed in to Home Assistant) and can be swapped
for one phone, a speaker announcement, or anything else.

*Turn the food* is the grill's own prompt rather than a guess from the timer —
it is raised on the tick cook progress crosses 50%. It needs the `Prompt`
sensor picked in the blueprint's optional field, as do *add the food* and
*take the food out*.

[![Import the grill-alerts blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FSchubmannM%2Fninja-woodfire-integration%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fninja_woodfire%2Fgrill_alerts.yaml)

**Grill alerts** — the lid left open mid-cook, a firmware error code, or the
grill going quiet while it is still cooking. Built on the integration's
entities rather than on cook events, so each alert can be left unset. Worth
pointing at a louder notification channel than the cook ones.

The quiet-grill alert watches the *state* sensor rather than *Cloud
connected*, which is the only thing that catches a migrated grill: those stay
connected while their data freezes (see [Why this
matters](#why-this-matters)), so connectivity never drops at all.

Both live in [`blueprints/automation/ninja_woodfire/`](blueprints/automation/ninja_woodfire/);
you can also copy them into `<config>/blueprints/automation/` by hand.

### Rolling your own

The events carry enough to identify the grill and say something useful about
it. Every one of them includes:

| key | |
|---|---|
| `dsn` | the grill's serial |
| `device_id` | its Home Assistant device — what a blueprint's device picker returns |
| `device_name` | what it is called in the UI, following a rename |

`cook_started`, `preheat_complete`, `cook_halftime` and `cook_done` add `mode`
and `setpoint`; `cook_started` adds `seconds_set` and `smoke`; `cook_halftime`
adds `seconds_left` and `seconds_set`; `cook_done` adds `reason` (`done` or
`stopped`). The two probe events carry `probe_index` (0-based), `target` and
`current` in °C.

```yaml
automation:
  - alias: "Steaks ready"
    triggers:
      - trigger: event
        event_type: ninja_woodfire_probe_target_reached
        event_data:
          probe_index: 0
    actions:
      - action: notify.notify
        data:
          message: >
            {{ trigger.event.data.device_name }}: probe 1 reached
            {{ trigger.event.data.target }}°C.
```

Events are suppressed while the snapshot is not live, deliberately: a grill
that drops off the cloud mid-cook and reconnects reading "idle" would
otherwise announce that food still raw is ready.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This is an unofficial, independent project. It is **not affiliated
with, endorsed by, sponsored by, or supported by SharkNinja
Operating LLC** or any of its subsidiaries.

"Ninja", "Woodfire", "Connect Pro XL", and any related product
names are trademarks of their respective owners. They are used
here only descriptively to identify which physical device this
integration interoperates with — no claim of trademark ownership
or affiliation is made or implied.

The integration interacts with a third-party cloud service the
author does not own or control. The author makes no representation
that such interaction is permitted by the operators of that
service; users are solely responsible for ensuring their use
complies with all applicable terms of service and laws in their
jurisdiction. Use at your own risk.
