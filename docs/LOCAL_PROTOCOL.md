# The grill's local network interfaces

Findings from a reverse-engineering session against a real **OG900-EU**
(Ninja Woodfire Pro Connect XL, appliance serial `SND…`, module firmware
`M1.1.0.179_X3.4.034`, Ayla agent `ADA 1.5-beta ameba`).

> **Superseded — read this first.** This was written while the cloud path
> looked like a dead end. It is not: the grill reports fine, just to
> SharkNinja's **AWS** backend rather than Ayla, and the integration now both
> reads *and sends commands* there. See [AWS_API.md](AWS_API.md). Nothing
> below is needed to make the integration work.
>
> Both local channels — the tcp/85 stream and Bluetooth LE — turned out to be
> encrypted, and blocked on the same key material inside the app's Rust core.
> They are documented here so nobody repeats the investigation.
>
> It is kept because the findings are hard-won and still true: the grill does
> expose an undocumented encrypted stream on the LAN, and if the vendor ever
> shuts the cloud down, or someone wants a genuinely local integration, this
> is the map of what is there and what was already ruled out.

This began as an investigation into why one `OG900-EU` never published live
state to Ayla. That question is now answered elsewhere; what follows is the
local-network reconnaissance done along the way.

Everything below was obtained by observing the device on the LAN. Nothing was
written to the grill beyond a LAN-mode registration attempt.

## Open ports

A full `1-65535` scan finds exactly two open ports; everything else is
filtered.

| Port | Service |
|------|---------|
| `80` | Ayla ADA embedded HTTP server |
| `85` | undocumented binary stream — **the interesting one** |

## Port 80 — Ayla ADA HTTP server

Minimal and mostly unhelpful. It answers exactly one useful path:

```
GET /regtoken.json
  → {"regtoken":null,"registered":1,"registration_type":"","host_symname":""}
```

Notable negatives:

- `GET /status.json`, `/time.json`, `/property.json` → the ADA 404 page.
- Most other paths drop the TCP connection rather than answering. The server
  handles **one connection at a time** — probe it serially, with retries, or
  results are meaningless.
- `POST`/`PUT /local_reg.json` → **HTTP 503**, consistently, including with the
  official app force-quit. So Ayla LAN mode is *provisioned* in the cloud
  (`/apiv1/dsns/<dsn>/lan.json` returns a `lanip_key`, `status: enable`,
  `keep_alive: 30`) but the device refuses to register a LAN client. The likely
  reason is that ADA cannot validate a `lanip_key_id` while it has no cloud
  session — which it never has. Ayla LAN mode therefore appears to be a dead
  end on this unit, and is *not* how the official app reads live state.

## Port 85 — framed binary stream

### Behaviour

- Silent on connect. Sending **any** byte triggers a burst of frames; there is
  no handshake. `\n`, `\x00`, or arbitrary bytes all work equally.
- The server closes the connection after a burst, so a client reconnects and
  pokes it again to keep reading.
- Frame contents are always different, and track a live cook — this is live
  data, not a static blob.

### Frame format

```
offset  size  meaning
------  ----  ---------------------------------------------
     0     2  magic 0x37 0x38  ("78")
     2     2  payload length, uint16 little-endian
     4   len  payload (encrypted)
 4+len     4  trailer (MAC or encrypted checksum)
```

Validated against 4 raw captures totalling ~27 kB: parsing as
`4 + len + 4` yields **370 frames with 18 bytes unparsed**, and every capture
ends on a frame boundary. Parsing as `4 + len + {0,2,8}` desyncs on the second
frame, so the 4-byte trailer is real and the length field covers the payload
only.

### Message types

Four payload sizes were observed. Semantics unknown; the 68-byte type
dominates and is the obvious candidate for periodic state.

| `len` | frame total | count in sample |
|-------|-------------|-----------------|
| `0x44` (68) | 76 | 320 |
| `0x28` (40) | 48 | 17 |
| `0x60` (96) | 104 | 17 |
| `0x0c` (12) | 20 | 16 |

### Cryptanalysis — what was ruled out

Over a 282-frame corpus of the 68-byte type:

- **Per-position structure**: only the 4 header bytes are constant. Payload
  positions average **7.247 bits/byte** of entropy, which is essentially the
  maximum a 282-sample column can show.
- **Keystream reuse**: XORing frame pairs gives **0.28 zero bytes per pair**,
  exactly the random expectation (72/256). Longest zero run: 1 byte. So no two
  frames share a keystream, even across separate connections — despite
  consecutive frames necessarily carrying near-identical plaintext during a
  cook.
- **ECB**: **0** repeated 16-byte blocks out of 1128. No block-level structure.
- **Counters**: no byte position advances monotonically.
- **Trailer**: matches neither CRC32 (LE/BE, over payload / header+payload /
  whole frame) nor Adler-32. It is a keyed MAC, or is itself encrypted.
- **Cipher mode**: payload lengths `{12, 40, 68, 96}` are never a multiple of
  16, under any nonce/IV prefix size (0/4/8/16). Nothing pads to a block
  boundary, so this is a **stream cipher** — CTR, ChaCha, or RC4 — with the
  nonce/counter held implicitly by both sides rather than sent in the frame.
- **Key guesses**: the official app writes `SET_Exec_Command = "setkey:<8
  chars>"` over the cloud immediately before the grill goes permanently
  cloud-silent, which makes that value the obvious key candidate. Every trivial
  derivation from it was tried and **all failed**: raw, zero-padded to 16/32,
  MD5, SHA-1[:16], SHA-256, under AES-ECB / AES-CBC (zero IV and
  frame-embedded IV) / AES-CTR, plus RC4 and repeating-XOR.

### What would still be needed

Only if someone wants a truly local transport — the integration does not need
this. The framing is understood; the payload is not. Because the keystream
never repeats and the nonce is not on the wire, both the **key** and the
**nonce/counter derivation** have to come from the official Android app —
`jadx` on the APK, or a `frida` hook at runtime. Search leads for whoever picks
this up: the `0x3738` magic, the string `setkey`, and the cloud property names
`SET_Exec_Command` / `SET_Enable_RT_Log`.

One caveat worth stating: port 85 being *the app's* channel is strongly
suspected but **not proven**. It carries live encrypted data on the only other
open port while the app demonstrably reads live state locally, which is
compelling — but the app's traffic was never actually captured. `SET_Enable_RT_Log`
("real-time log") hints at an alternative purpose. Confirming it needs a packet
capture of the phone talking to the grill.

## Bluetooth LE — mapped, and closed

The app uses BLE whenever the phone is near the grill: in one capture 529 of
570 state samples came from Bluetooth rather than the cloud, and the grill
acknowledges those commands distinctly, writing `reported.Cook_Command` as
`"* (BTLE CMD)"`. Home Assistant can see the grill advertising too (service
`0xFCBB`, RSSI -72 through a Shelly proxy), so a local transport looked
plausible. It is not, without the app's crypto.

The grill advertises as `NCEU<mac>` and exposes one service:

| characteristic | properties | observed |
|---|---|---|
| `0000b001` | read | 96 B, maximal entropy — challenge or key material |
| `0000b002` | write, write-without-response | the command channel |
| `0000b003` | notify | **never sends anything** |
| `0000b004` | indicate | 96 B frames on connect, maximal entropy |

Every payload is 96 bytes — exactly 6 AES blocks — at 6.26 bits/byte, which is
the ceiling for a 96-byte sample rather than a shortfall from 8. It is
ciphertext.

**The decisive test.** Connect without authenticating, subscribe to everything
that notifies, then drive the grill from its own panel for two minutes:
increase the timer, decrease the temperature, stop the cook. Result: one
handshake frame on `b004` at connect, and then **complete silence**. `b003`,
the state stream, sent nothing at all.

So the grill gates all telemetry behind a handshake it offers on connect. There
is no read-only subset to listen in on, and no partial win — an
unauthenticated client gets a GATT map and nothing else.

Continuing means reversing the BLE crypto in `libgrillcore_android.so`:
locating the handshake, recovering the key derivation, then decoding bincode
structs on top of it. The `setkey:<8 chars>` value the app writes over the
cloud is very likely the shared secret feeding it — every trivial derivation
from it was already tried against the port-85 stream and none worked.

Capturing the app's own BLE traffic would shortcut the handshake, but Android's
HCI snoop log could not be extracted on the test device: OxygenOS `dumpstate`
fails to finalise the zip (`Failed to add text entry to .zip file`), and the
log is unreadable without root.

`scripts/ble_probe.py` performs the enumeration above and records every frame
with a timestamp, for whoever picks this up.

## Why the *Ayla* path fails on this unit

Retained because it motivated the reconnaissance above, and because the
symptom is worth recognising. The cause was later established: the grill had
been migrated to SharkNinja's AWS backend and stopped publishing to Ayla
entirely — the phone app writes `Cloud_Mode = 1` to the AWS device shadow,
and Ayla goes read-dead for that grill from that moment. Details in
[AWS_API.md](AWS_API.md).

Measured during a real Air Fry cook, with the app force-quit and after a clean
power-cycle:

- 248 consecutive cloud polls, **zero** value changes.
- `connection_status: Offline` throughout, while the grill answered ping in
  5 ms and served both local ports.
- `GET_GrillState` stuck on a **24-hour-old** `idle` datapoint.
- The entire 30-day datapoint history holds **five** state datapoints, all
  written at module-connect time, and not one of them a cooking state.
- Device record reads `connection_priority: ["LAN"]`.

**The write direction still works.** Cook commands sent as Ayla datapoints
reach the grill and are acted on — verified from mobile data only, with the
grill reporting `connection_status: Offline` and publishing no state
throughout. The module evidently keeps a lightweight ANS registration so it can
*receive* pushes (`ans_enabled: true`) while never *publishing* telemetry to
the cloud. That asymmetry is why the integration keeps cook commands available
even when every state entity is unavailable, and it means `connection_status`
must never be used to decide whether a command can be sent.

Ruled out by direct measurement at the time — the actual cause, an AWS
migration, was not visible from the Ayla side at all: DNS filtering (no matching rule; the grill
never queried the resolver; disabling it changed nothing), the app's
"Upload my Cook Data" setting (ON, mid-cook, cloud still frozen — and it is an
app-only feature), the app holding a LAN session (force-quit, no change),
property names, endpoint, `names[]` filtering, parsing, and device identity
(`GET_WiFiModuleSerialNumber` concatenates the Ayla DSN and the appliance
serial the app displays, confirming both refer to the same device).
