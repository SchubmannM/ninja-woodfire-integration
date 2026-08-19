#!/usr/bin/env python3
"""Cut a release and roll it out to Home Assistant in one command.

Home Assistant cannot hot-reload a custom integration: HACS downloads the new
files happily, but the old Python modules are already imported, so reloading
the config entry re-runs setup against the *old* code. Only a process restart
picks up changes. This does not remove the restart — it removes the clicking.

On Home Assistant OS there is no filesystem access from a dev machine, so
everything here goes through HACS and HA's REST API:

    tag + GitHub release  ->  HACS sees a new version
    update.install        ->  HACS downloads it
    homeassistant.restart ->  the new code is imported
    poll /api/            ->  wait until HA is back

Setup (once):

    1. Home Assistant -> your profile -> Security -> Long-lived access tokens
       -> "Create token". Copy it.
    2. Put it in `.env.deploy` in the repo root (gitignored):

           HA_URL=https://hass.example.com
           HA_TOKEN=<the token>

Usage:

    python3 scripts/deploy.py                # release current version + deploy
    python3 scripts/deploy.py --restart-only # just restart HA
    python3 scripts/deploy.py --dry-run      # print the plan, change nothing
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Deliberately stdlib-only. This is the script you reach for when something
# needs deploying, sometimes from a shell with no virtualenv active — making it
# depend on `requests` means it fails exactly when it is least convenient.

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components" / "ninja_woodfire" / "manifest.json"
ENV_FILE = ROOT / ".env.deploy"
REPO = "SchubmannM/ninja-woodfire-integration"


def load_env() -> tuple[str, str]:
    if not ENV_FILE.exists():
        sys.exit(
            f"missing {ENV_FILE.name}. Create it with:\n"
            "    HA_URL=https://your-ha-host\n"
            "    HA_TOKEN=<long-lived access token>\n"
            "See the docstring at the top of this script."
        )
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    missing = {"HA_URL", "HA_TOKEN"} - env.keys()
    if missing:
        sys.exit(f"{ENV_FILE.name} is missing: {', '.join(sorted(missing))}")
    return env["HA_URL"].rstrip("/"), env["HA_TOKEN"]


def run(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"$ {' '.join(args)}\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


class HA:
    def __init__(self, url: str, token: str, dry_run: bool) -> None:
        self.url = url
        self.dry_run = dry_run
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload=None, timeout: int = 30):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, headers=self._headers, method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
        return json.loads(body) if body.strip() else None

    def get(self, path: str, timeout: int = 20):
        return self._request("GET", path, timeout=timeout)

    def service(self, domain: str, service: str, **data):
        if self.dry_run:
            print(f"    [dry-run] {domain}.{service} {data or ''}")
            return None
        return self._request(
            "POST", f"/api/services/{domain}/{service}", payload=data, timeout=60
        )

    def find_update_entity(self) -> str | None:
        """The HACS update entity for this integration."""
        for state in self.get("/api/states") or []:
            eid = state.get("entity_id", "")
            if not eid.startswith("update."):
                continue
            attrs = state.get("attributes") or {}
            haystack = (
                f"{eid} {attrs.get('friendly_name', '')} {attrs.get('title', '')}"
            ).lower()
            if "ninja" in haystack and "woodfire" in haystack:
                return eid
        return None

    def wait_until_up(self, timeout: int = 240) -> bool:
        deadline = time.time() + timeout
        # Give it a moment to actually go down first, or we would see the old
        # process still answering and declare success immediately.
        time.sleep(5)
        while time.time() < deadline:
            try:
                self.get("/api/", timeout=5)
                return True
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(3)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--restart-only", action="store_true",
                    help="skip the release and update, just restart HA")
    ap.add_argument("--no-release", action="store_true",
                    help="skip tagging/releasing; still update and restart")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen without changing anything")
    args = ap.parse_args()

    url, token = load_env()
    ha = HA(url, token, args.dry_run)
    version = json.loads(MANIFEST.read_text())["version"]
    tag = f"v{version}"

    if not args.restart_only and not args.no_release:
        dirty = run("git", "status", "--porcelain")
        if dirty:
            sys.exit("working tree is not clean — commit first:\n" + dirty)

        print(f"==> releasing {tag}")
        existing = run("git", "tag", "-l", tag)
        if existing:
            print(f"    tag {tag} already exists; not re-tagging")
        elif args.dry_run:
            print(f"    [dry-run] would tag and push {tag}")
        else:
            run("git", "tag", "-a", tag, "-m", f"Release {version}")
            run("git", "push", "origin", tag)
            print(f"    tagged and pushed {tag}")

        released = run("gh", "release", "view", tag, "--repo", REPO,
                       "--json", "tagName", check=False)
        if released:
            print(f"    release {tag} already published")
        elif args.dry_run:
            print(f"    [dry-run] would publish release {tag}")
        else:
            run("gh", "release", "create", tag, "--repo", REPO,
                "--title", tag, "--generate-notes")
            print(f"    published release {tag}")

    if not args.restart_only:
        print("==> asking HACS for the new version")
        entity = ha.find_update_entity()
        if entity is None:
            print("    no HACS update entity found for this integration.")
            print("    Is it installed via HACS as a custom repository?")
            print("    Continuing to restart anyway.")
        else:
            print(f"    {entity}")
            ha.service("homeassistant", "update_entity", entity_id=entity)
            time.sleep(3)
            if not args.dry_run:
                state = ha.get(f"/api/states/{entity}")
                installed = (state.get("attributes") or {}).get("installed_version")
                latest = (state.get("attributes") or {}).get("latest_version")
                print(f"    installed={installed} latest={latest}")
                if state.get("state") == "on":
                    print("    installing…")
                    ha.service("update", "install", entity_id=entity)
                else:
                    print("    HACS reports no update pending")
            else:
                ha.service("update", "install", entity_id=entity)

    print("==> restarting Home Assistant")
    ha.service("homeassistant", "restart")
    if args.dry_run:
        print("==> dry run complete")
        return 0

    print("    waiting for it to come back…")
    if ha.wait_until_up():
        print("==> Home Assistant is up")
        return 0
    print("==> timed out waiting for Home Assistant; check it manually")
    return 1


if __name__ == "__main__":
    sys.exit(main())
