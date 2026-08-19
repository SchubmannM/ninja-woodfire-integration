# The grill's local network interfaces

Findings from a reverse-engineering session against a real **OG900-EU**
(Ninja Woodfire Pro Connect XL, appliance serial `SND…`, module firmware
`M1.1.0.179_X3.4.034`, Ayla agent `ADA 1.5-beta ameba`).

This exists because on that unit the Ayla cloud **never receives live state**
(see README § *Known limitation*), so the cloud transport this integration is
built on cannot work. Any future local transport starts here.

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

### What is still needed

The transport framing is understood; the payload is not. Because the keystream
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

## Why the cloud path fails on this unit

For completeness, since it motivates all of the above. Measured during a real
Air Fry cook, with the app force-quit and after a clean power-cycle:

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

Ruled out by direct measurement: DNS filtering (no matching rule; the grill
never queried the resolver; disabling it changed nothing), the app's
"Upload my Cook Data" setting (ON, mid-cook, cloud still frozen — and it is an
app-only feature), the app holding a LAN session (force-quit, no change),
property names, endpoint, `names[]` filtering, parsing, and device identity
(`GET_WiFiModuleSerialNumber` concatenates the Ayla DSN and the appliance
serial the app displays, confirming both refer to the same device).
