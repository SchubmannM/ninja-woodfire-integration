#!/usr/bin/env python3
"""Fail if the translation files have drifted apart.

Every entity key must exist in every language file. They have drifted before —
`de.json` carried a `preheat_progress` key that `en.json` lacked — and the
symptom is a raw translation key rendered in the UI for whoever is on the other
locale, which nobody notices until a user reports it.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRANSLATIONS = ROOT / "custom_components" / "ninja_woodfire" / "translations"


def entity_keys(doc: dict) -> set[str]:
    return {
        f"{platform}.{key}"
        for platform, keys in (doc.get("entity") or {}).items()
        for key in keys
    }


def main() -> int:
    files = sorted(TRANSLATIONS.glob("*.json"))
    if len(files) < 2:
        return 0

    loaded = {}
    for path in files:
        try:
            loaded[path.name] = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            print(f"{path}: invalid JSON: {err}")
            return 1

    keysets = {name: entity_keys(doc) for name, doc in loaded.items()}
    reference = set().union(*keysets.values())

    failed = False
    for name, keys in sorted(keysets.items()):
        missing = reference - keys
        if missing:
            failed = True
            print(f"{name} is missing {len(missing)} entity key(s):")
            for key in sorted(missing):
                print(f"    {key}")

    if failed:
        print("\nEvery language file must define the same entity keys.")
        return 1
    print(f"translations in sync: {len(reference)} entity keys across {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
