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

## Cook lifecycle, as observed

Captured live through an Air Crisp cook (150 °C, 3 min). Fixtures for each
phase are in `tests/fixtures/aws_*.json`.

`GrillState.state` reads `"cooking"` for the whole cook including preheat — the
real sub-phase is in `CookState.state.state`, which moves
`preheat` (with `progress`) → `heat` → `none`.

User-facing prompts arrive in `GrillState.message`, paired with an
`eventmask` bitfield:

| `message` | `eventmask` | meaning |
|-----------|-------------|---------|
| `"1:addfood"` | `0x00` | add food (cook running) |
| `"6:done"` | `0x40` | cook complete |
| `"7:getfood"` | `0xC0` | remove food |

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
