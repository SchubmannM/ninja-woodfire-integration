#!/usr/bin/env python3
"""Explore the grill's Bluetooth LE interface.

The Ninja app talks to the grill over BLE whenever it is in range — that is
where cook commands go, and it is the only transport that carries both reads
and writes for a grill that has been migrated off Ayla.

This connects, enumerates the GATT tree, subscribes to everything that can
notify, and records every frame with a timestamp so the payloads can be
decoded against known state.

Run it from a terminal that has macOS Bluetooth permission (System Settings ->
Privacy & Security -> Bluetooth). Output goes to ble_probe_output.json next to
the repo, plus a readable summary on stdout.

    .venv/bin/python scripts/ble_probe.py [seconds]

Force-quit the Ninja app first: the grill may only accept one connection, and
the app will hold it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("needs bleak:  uv pip install --python .venv/bin/python bleak")

SERVICE_HINT = "fcbb"          # SharkNinja's advertised 16-bit service, 0xFCBB
MANUFACTURER_ID = 3151         # seen in the grill's advertisement
LISTEN_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
OUT = pathlib.Path(__file__).resolve().parents[1] / "ble_probe_output.json"

frames: list[dict] = []


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


async def find_grill():
    print(f"scanning for the grill (service 0x{SERVICE_HINT.upper()})…")
    match = {}

    def cb(dev, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if any(SERVICE_HINT in u for u in uuids) or MANUFACTURER_ID in (adv.manufacturer_data or {}):
            match.setdefault(dev.address, (dev, adv))

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    for _ in range(20):
        await asyncio.sleep(1)
        if match:
            break
    await scanner.stop()
    if not match:
        print("\nNot found. Check that the grill is powered on and in range,")
        print("and that the Ninja app is force-quit (it may hold the connection).")
        return None
    dev, adv = next(iter(match.values()))
    print(f"found {dev.address}  name={dev.name!r}  rssi={adv.rssi}")
    print(f"  service_uuids = {adv.service_uuids}")
    print(f"  manufacturer  = { {k: v.hex() for k, v in (adv.manufacturer_data or {}).items()} }\n")
    return dev


async def main() -> int:
    dev = await find_grill()
    if dev is None:
        return 1

    print("connecting…")
    async with BleakClient(dev, timeout=30.0) as client:
        print(f"connected: {client.is_connected}\n")
        tree = []
        print("=== GATT services ===")
        for service in client.services:
            print(f"  service {service.uuid}  ({service.description})")
            chars = []
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                print(f"    char {ch.uuid}  [{props}]")
                value_hex = None
                if "read" in ch.properties:
                    try:
                        raw = await client.read_gatt_char(ch)
                        value_hex = raw.hex()
                        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
                        print(f"      read: {value_hex[:80]}  |{printable[:40]}|")
                    except Exception as err:
                        print(f"      read failed: {type(err).__name__}")
                chars.append({"uuid": str(ch.uuid), "properties": list(ch.properties),
                              "descriptors": [str(d.uuid) for d in ch.descriptors],
                              "read_hex": value_hex})
            tree.append({"uuid": str(service.uuid), "characteristics": chars})

        print("\n=== subscribing to notifications ===")
        subscribed = []
        for service in client.services:
            for ch in service.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    def make_cb(uuid):
                        def handler(_h, data: bytearray):
                            entry = {"ts": stamp(), "char": str(uuid),
                                     "len": len(data), "hex": bytes(data).hex()}
                            frames.append(entry)
                            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                            print(f"  [{entry['ts'][11:23]}] {uuid} {len(data):4}B "
                                  f"{entry['hex'][:64]}{'…' if len(data) > 32 else ''} |{printable[:32]}|")
                        return handler
                    try:
                        await client.start_notify(ch, make_cb(ch.uuid))
                        subscribed.append(str(ch.uuid))
                        print(f"  subscribed {ch.uuid}")
                    except Exception as err:
                        print(f"  subscribe failed {ch.uuid}: {type(err).__name__}")

        print(f"\nlistening {LISTEN_SECONDS}s — drive the grill now "
              f"(start a cook from its panel, change temp, stop it)\n")
        await asyncio.sleep(LISTEN_SECONDS)

        for uuid in subscribed:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

    OUT.write_text(json.dumps(
        {"captured_at": stamp(), "address": str(dev.address), "name": dev.name,
         "services": tree, "subscribed": subscribed, "frames": frames}, indent=2))
    print(f"\n{len(frames)} frames captured -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
