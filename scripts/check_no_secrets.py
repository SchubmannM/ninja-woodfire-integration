#!/usr/bin/env python3
"""Block real device identifiers and credentials from being committed.

This repository is developed against a live grill and a real account, so
captures, fixtures and notes routinely pass through the working tree carrying
DSNs, appliance serials, MACs, household ids and bearer tokens. Fixtures are
supposed to be scrubbed; this catches the times they are not.

Patterns are shape-based rather than value-based, so it protects anyone's
device, not just the original author's.
"""
from __future__ import annotations

import re
import sys

PATTERNS = [
    # Ayla DSN: "AC" + 3 chars + "W" + 9 digits, e.g. AC000W000000000.
    (re.compile(r"\bAC[0-9A-Z]{3}W\d{9}\b"), "Ayla DSN"),
    (re.compile(r"\bSND\d{9,}\b"), "appliance serial"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "MAC address"),
    (re.compile(r"\b01[A-HJ-NP-TV-Z0-9]{24}\b"), "household id (ULID)"),
    (re.compile(r"\bauth0\|[0-9a-f]{16,}\b"), "Auth0 user id"),
]

# Placeholders that deliberately look like the real thing.
ALLOWED = {
    "AC000W000000000",
    "00:00:00:00:00:00",
}


def main(paths: list[str]) -> int:
    failed = False
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for pattern, label in PATTERNS:
                        for hit in pattern.findall(line):
                            if hit in ALLOWED:
                                continue
                            shown = hit[:8] + "…" if len(hit) > 12 else hit
                            print(f"{path}:{lineno}: looks like a {label}: {shown}")
                            failed = True
        except (OSError, UnicodeDecodeError):
            continue
    if failed:
        print("\nScrub these before committing. Fixtures use AC000W000000000.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
