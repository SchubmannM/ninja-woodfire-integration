# SharkNinja's AWS grill API

SharkNinja is migrating Ninja grills off Ayla onto their own AWS-backed IoT
service. This documents that backend, as observed from a real **OG900-EU**
(Ninja Kitchen Android app 1.25.0).

It matters because the migration is silent and one-way from the integration's
point of view: **a migrated grill stops publishing telemetry to Ayla
completely**, while Ayla keeps serving the last datapoint it ever received,
forever, with no error and no staleness marker. See README § *Known
limitation*.

## How a grill gets migrated

The phone app writes `Cloud_Mode = 1` into the grill's AWS device shadow. On
the measured unit that happened at a precise, recorded moment:

```
2026-08-18T17:29:38Z   Ayla   SET_Exec_Command = "setkey:<8 chars>"
2026-08-18T17:29:39Z   Ayla   SET_Exec_Command = "*"   echo=true   (grill acked)
2026-08-18T17:29:41Z   AWS    shadow desired: Cloud_Mode = 1, user_linked = true
                              ── Ayla never heard from the grill again ──
```

Everything after that is consistent with a receive-only Ayla presence: over the
following 24 hours Ayla logged **zero** state datapoints across 2367 polls,
including through two full cooks, while reporting `connection_status: Offline`
and `connection_priority: ["LAN"]`.

**Commands still work over Ayla.** Datapoint writes to `SET_Cook_Command` reach
a migrated grill and are acted on — verified from mobile data only, with the
grill "Offline" and publishing nothing. The module keeps a lightweight ANS
registration to *receive* pushes while never *publishing* telemetry. That is
why this integration reads from AWS but still writes through Ayla.

## Authentication

No new credential flow. The AWS API accepts the **same Auth0 `id_token`** the
Ayla flow already fetches — the shared grant lives in `_lib/api/auth0.py`.

The account's user id is the `sub` claim with the `auth0|` prefix stripped, so
it needs no lookup:

```
sub: "auth0|<userId>"   ->   <userId>
```

## Endpoints

Base: `https://stakra.rannsaka.thor.skegox.com`

**Region:** unlike Ayla, these hosts carry no region marker and the same pair
served an EU account throughout, so the integration does not vary them by
region — the `Region` setting only selects the Auth0 tenant (which the AWS API
authenticates against) and the Ayla endpoints. Regional AWS deployments may
still exist: the device record carries `"dc": "International"` and the app
registers for push on `sn-eu-field-iot-ninjakitchen-app`. **Only EU has been
observed.** If an NA account uses different hosts, these belong in
`CloudRegion` — where they already live, defaulted to the observed EU values
for both regions, so confirming an NA host is a one-line data change. A capture
from an NA grill would settle it.

Until then the failure mode is degraded rather than broken: an unreachable AWS
backend falls back to Ayla, and setup logs why at INFO with a pointer to the
`AWS API base URL` / `AWS API key` overrides in the advanced setup options —
because an account on an unseen deployment fails *identically* to an
unmigrated one, and the difference matters.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/householdsEndUser?userId={userId}` | `{"households": ["<id>"]}` |
| `GET` | `/devicesEndUserController/{householdId}/users/{userId}?includeRegistry=true&includeConnectivityStatus=true` | every device, **with live telemetry** |
| `GET` | `/devicesEndUserController/{householdId}/devices/{deviceId}` | one device |

Headers on every request:

```
authorization:  Bearer <auth0 id_token>
x-api-key:      <static per-app key>        (public in every app install,
                                             same category as the bundled
                                             Auth0/Ayla identifiers)
x-iotn-caller:  ENDUSER_MOBILEAPP
x-sn-nonce:     12345                       (fixed in the app; not validated)
x-sn-date:      <ISO-8601 UTC, ms precision>
content-type:   application/json
```

A single authenticated GET returns live telemetry, so the existing polling
coordinator fits unchanged. The app additionally holds a WebSocket at
`wss://stakra.rannsaka.bifrost.skegox.com` receiving `IOT_SHADOW_UPDATE_EVENT`
pushes (`source: "iot-shadow-update-rule"`), but polling is sufficient and far
simpler in Home Assistant. Not implemented here.

## Device record

```jsonc
{
  "deviceId": "<appliance serial>",     // note: NOT the Ayla DSN
  "registry": {
    "modelNumber": "OG900-EU",
    "OTA_FW_VERSION": "M1.1.0.179_X3.4.034",
    "WiFiModuleSerialNumber": "<ayla dsn>-<appliance serial>",   // ties both ids
    "serialNumber": "<appliance serial>", "macAddress": "…", "brandName": "Ninja"
  },
  "telemetry": {
    "GrillState": "{…}",   // same JSON blobs as Ayla, no GET_ prefix
    "CookState":  "{…}",
    "ProbeState": "{…}",
    "RSSI": -66
  },
  "connectivityStatus": {
    "connected": true,
    "lastConnectedAt": "…", "lastDisconnectedAt": "…"
  },
  "updatedAt": "2026-08-19T18:36:27.530Z"
}
```

Two identifiers are in play: the app and this API key on the **appliance
serial** (`SND…`), Ayla on the **module DSN** (`AC000W…`). `registry
.WiFiModuleSerialNumber` concatenates them, which is what lets `find_device()`
match a grill by either.

`connectivityStatus.connected` plus `updatedAt` give a genuine liveness
signal — unlike Ayla, where a successful poll proves nothing about freshness.

## Payload dialect

The structures are identical to Ayla's, but **every space is stripped**, from
object keys *and* enum values. `_lib/api/aws.py::normalise` canonicalises onto
the Ayla spelling so `models.py` and the capability tables need no changes, and
so cook commands keep using the spelling the firmware expects.

| Ayla | AWS |
|------|-----|
| `"seconds set"` | `"secondsset"` |
| `"seconds left"` | `"secondsleft"` |
| `"probes active"` | `"probesactive"` |
| `"lid open"` | `"lidopen"` |
| `"plugged in"` | `"pluggedin"` |
| `"air crisp"` (mode) | `"aircrisp"` |
| `"powered OFF"` (state) | `"poweredOFF"` |
| `"get food"` (state) | `"getfood"` |

## Temperature units

**The payload mixes units, and neither backend says so anywhere.**

| Block | Unit | Fields |
|---|---|---|
| `GrillState.inputs.temps` | **Fahrenheit** | `grill`, `air`, `smoke`, `probe0_a/b`, `probe1_a/b` |
| `GrillState.setpoint` | **Celsius** | the cook target (heat level 1/2/3 in `grill` mode) |
| `ProbeState.probes[].temp` | **Celsius** | measured probe temperature |
| `ProbeState.probes[].mode.setpoint` | **Celsius** | probe target |
| `GrillState.inputs.temps.main` / `.ui` | *neither* | PCB ADC counts, constant at 6542.4 / 6513.6 |

`_lib/models.py::GrillTemps.from_wire` converts the Fahrenheit block on the
way in, so `CombinedState` is Celsius throughout and entities need no unit
logic. `FAHRENHEIT_TEMP_FIELDS` is the list; nothing keys off the magnitude
of a reading.

### How the boundary was established

One snapshot settles it without any argument from plausible magnitudes.
`tests/fixtures/aws_bake_probe_ambient.json` was taken with a meat probe
plugged in and its tip in open room air, and it carries **the same
measurement twice**:

```json
"inputs": {"temps": {"probe1_a": 80, "probe1_b": 80, ...}}     // Fahrenheit
"probes": [{"name": "probe0", "pluggedin": 1, "temp": 26.6}]   // Celsius
```

80 °F is 26.67 °C. Two further live samples reconciled the same way (82 ↔
27.7, 81 ↔ 27.2). The firmware converts for the block a human reads and
leaves the sensor block in the scale the MCU works in — which is also why
`setpoint`, the number the user dialled in, is Celsius sitting right beside a
Fahrenheit `inputs.temps`.

Both ends of the scale agree. A Bake at a 160 °C setpoint held `air` between
294.6 and 339.4 over a full thermostat cycle — 145.9 °C to 170.8 °C either
side of target, and unreachable as Celsius. A grill idle and offline for 23
hours reported `grill` 79.5 / `air` 76.6, which is 26.4 °C / 24.8 °C: room
temperature.

### There is no unit flag to read

Checked exhaustively, because reading a flag would beat hard-coding a list:

* **Ayla property objects** carry a fixed 24-key schema — `ack_enabled`,
  `app_type`, `base_type`, `data_updated_at`, `denied_roles`, `derived`,
  `device_key`, `direction`, `display_name`, `generated_at`,
  `generated_from`, `host_sw_version`, `key`, `name`, `passthrough`,
  `product_name`, `read_only`, `recipe`, `retention_days`, `scope`,
  `time_series`, `track_only_changes`, `type`, `value`. None is a unit.
  `base_type` is a JSON type; `display_name` is prose ("Air Temperature").
* **The per-property endpoint** `/apiv1/dsns/{dsn}/properties/{name}.json`
  returns exactly the same keys as the list endpoint — no extra metadata.
  `/template.json` and `/catalog.json` are 404 for an end-user token.
* Every temperature property has `derived: false` and `generated_from:
  null`, so there is no server-side conversion recipe to inspect either.
* **The Ayla device record** has no unit or locale field.
* **AWS** `metadata` is `{"deviceName": …}`, and the device shadow's only
  properties are `Cloud_Mode`, `Cook_Command`, `Cook_Notifications`,
  `Exec_Command` and `user_linked`.

### Ayla declares scalar temperature properties it never populates

The full property list (48 entries, against the 3 the integration reads)
includes `GET_Temp_Air`, `GET_Temp_Grill`, `GET_Temp_MainPCB`,
`GET_Temp_UIPCB`, `GET_Probe1_Temp`, `GET_Probe2_Temp` — and also
`GET_CombinedState`, `GET_CookDefaults`, `GET_Lid_Open`, `GET_Error_Code`,
`GET_Estimated_End_Time`. **All of them have `value: null` and
`data_updated_at: "null"`**: declared in the device template, never written
by this firmware. They are not an alternative, already-converted source, and
they carry no unit metadata either. Only 16 properties have ever reported,
and the three state blobs are the only useful ones.

### What is not established

* **Whether an NA grill reports `setpoint` in Fahrenheit.** The sensor block
  is a raw MCU scale and there is no mechanism in the protocol by which it
  could vary per account — there is no unit field for the firmware to key
  off. `setpoint` is different: it is the number shown on the appliance, and
  on a US model that display is Fahrenheit. Only an EU OG900-EU has been
  captured. This is a pre-existing exposure, not one this change introduces —
  `capabilities.py` already declares every range in °C — but it is the thing
  to check first if an NA user reports nonsense setpoints.
* **What `smoke` actually measures.** It never reads below ~227 °F (108 °C),
  including on a grill switched off at the socket for 23 hours, so it carries
  a large offset or is an unpopulated channel. It is converted with its block
  because it sits in that block, but no capture has ever had the Woodfire box
  lit (`smoke: 0` in all of them), so its behaviour when it matters is
  unobserved. `temp_is_plausible` hides it unless smoke is on.
* **Whether `inputs.temps.probeN_*` is off by one against `ProbeState`.** A
  probe reported as `ProbeState.probes[0]` showed its raw elements in
  `probe1_a`/`probe1_b`, with `probe0_a`/`probe0_b` at zero. Only one probe
  has ever been plugged in at a time, so socket-to-index mapping is
  unresolved. It does not currently matter: nothing consumes `probeN_a/b`,
  because `ProbeInfo.temp` is the firmware's own average of them in Celsius.

## Cook lifecycle, as observed

Captured live through an Air Crisp cook (150 °C, 3 min). Fixtures for each
phase are in `tests/fixtures/aws_*.json`.

`GrillState.state` reads `"cooking"` for the whole cook including preheat — the
real sub-phase is in `CookState.state.state`, which moves
`preheat` (with `progress`) → `heat` → `none`.

User-facing prompts arrive in `GrillState.message`, paired with an
`eventmask` bitfield.

The number before the colon is the **bit index** into `eventmask`, which is a
field of everything currently raised — so `4:flipfood` arrives with `0x10`
(`1 << 4`), and `7:getfood` with `0xC0` because bit 6 (`done`) is still up
alongside it.

| `message` | `eventmask` | meaning |
|-----------|-------------|---------|
| `"1:addfood"` | `0x02` | preheat has finished — put the food in |
| `"4:flipfood"` | `0x10` | turn the food |
| `"6:done"` | `0x40` | cook complete |
| `"7:getfood"` | `0x80` (`0xC0` with `done`) | take the food out |

Bits 0, 2, 3 and 5 have not been observed. `models.parse_prompt` reads the
name rather than matching this table, so an unseen prompt still arrives under
its own name.

**The prompts are brief, and not equally so.** Observed on an OG900-EU:
`flipfood` for 10 and 12 seconds, `done` for 9, `getfood` for 91, `addfood`
for several minutes. `message` returns to empty in between, so anything acting
on a prompt has to react to the transition rather than wait for it to settle.
The one-second poll of an active cook catches even the shortest comfortably.

**`eventmask` outlives `message`.** At the end of one cook the message cleared
to empty while the mask still read `0x40`, and only dropped to `0x00` half a
minute later. The mask is what is currently raised; the message is what is
currently being *said*. `sensor.prompt` follows the message, which is the one
that maps to "tell the user something now".

**`flipfood` looks mode-dependent.** Both captures of it are Grill cooks. Two
full Air Crisp cooks watched end to end raised `addfood`, `done` and
`getfood` and never asked for a flip — which is plausible enough for a basket
you do not turn food in, but it means a quiet Air Crisp cook is not evidence
of anything broken.

**`flipfood` lands exactly on the halfway tick** — in two captured cooks it
was raised on the same second that `cook_progress` went 50 → 51.

**`done` is not the end of the cook.** In one capture it was raised at
10:34:07 with `CookState` momentarily `none`, and `heat` resumed three
seconds later; the cook actually ended at 10:39:49. Use the grill state for
that, which is what `EVENT_COOK_DONE` does.

**The cook-phase sensors never carry these.** Across that whole cook
`CookState.state` went `preheat → heat → none → heat → none` while the grill
twice asked for a flip. `GrillState.message` is the only source.

It is not that `CookState.state` reports nothing but phases — a later cook
showed it going to `lid open` and back four times as the lid was worked. It
simply never carries the food prompts.

During preheat `secondsleft` stays pinned at `secondsset` and `endtimeutc`
keeps sliding forward — the countdown only starts once preheat ends, so
derived progress must exclude the preheat phase.

## Commands

Commands are written into the shadow's `desired` section:

```
PATCH /devicesEndUserController/{householdId}/devices/{deviceId}
{"shadow": {"properties": {"desired": {"Cook_Command": {
    "id": 1001, "mode": "air crisp", "temp": 200,
    "seconds set": 300, "smoke": 0, "skip preheat": 0 }}}}}
```

Three things are easy to get wrong here.

**The payload uses the spaced spelling.** Telemetry comes back with every
space stripped, but commands are stored with the firmware's own names —
`"seconds set"`, `"skip preheat"`, `"air crisp"`. Reads and writes use
different dialects, and normalising the write payload the way reads are
normalised produces a command the grill ignores.

**`id` is not the Ayla device key.** The app sends `1001` to start and `1000`
to stop; those are mirrored rather than invented.

**A 200 is not delivery.** It means the shadow accepted the desired state. The
grill acknowledges separately by writing `reported.Cook_Command`, and helpfully
records the transport it arrived over — a command sent from the app over
Bluetooth shows up as `"* (BTLE CMD)"`. An offline grill receives the command
whenever it next connects, so a queued start will fire on power-on.

The request body is validated against a strict whitelist: the endpoint accepts
only `shadow` and `metadata`, and anything else returns
`400 "property X should not exist"`. Malformed shadow bodies surface the
underlying AWS IoT errors verbatim (`Shadow state must contain either
"desired" or "reported"`), which is a convenient way to map the schema.

### Reading the shadow back

**`shadow` is only populated on the single-device endpoint.** The listing
endpoint (`/users/{userId}`) returns `{"desired": {}, "reported": {}}` even
when the shadow is full — use `/devices/{deviceId}` to read it. This is worth
knowing before concluding that a write failed, or that something wiped the
shadow.

## Not implemented

- **The WebSocket.** Polling covers reads, and the socket is receive-only for
  clients anyway: every message sent up it is answered `Forbidden`, including
  the `sendMessage` action that appears in inbound frames.
