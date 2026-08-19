"""Test bootstrap.

The integration lives under ``custom_components/ninja_woodfire/``, whose
platform modules (``select.py``, ``button.py``, ``number.py``) shadow stdlib
modules — so that directory must never land on ``sys.path``. The top-level
package ``__init__.py`` also imports Home Assistant, which we don't want in a
unit test.

Both problems go away by registering a synthetic package that maps directly at
the ``_lib`` directory. Relative imports inside ``_lib`` (``from ..const
import ...`` in ``api/ayla.py``) then resolve against it normally.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "custom_components" / "ninja_woodfire" / "_lib"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

_PKG = "nwf_lib"


def _install_lib_package() -> None:
    if _PKG in sys.modules:
        return
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(LIB)]
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg


_install_lib_package()


def load_fixture(name: str) -> dict:
    """Load a captured cloud snapshot from ``tests/fixtures``."""
    with open(FIXTURES / f"{name}.json") as fh:
        return json.load(fh)


def hydrate(name: str, *, age_seconds: float = 0.0, online: bool = True):
    """Build a ``CombinedState`` from a fixture with controlled liveness.

    ``age_seconds`` rebases ``last_updated_at`` relative to now, so staleness
    behaviour is deterministic instead of depending on when the fixture was
    captured. Use :func:`hydrate_as_captured` when the fixture's own absolute
    timestamp is the point of the test.
    """
    import datetime as _dt

    from nwf_lib.models import CombinedState, CookState, GrillState, ProbeState

    props = load_fixture(name)["properties"]
    return CombinedState(
        dsn="AC000W000000000",
        grill=GrillState.from_property_value(props["GET_GrillState"]["value"]),
        cook=CookState.from_property_value(props["GET_CookState"]["value"]),
        probes=ProbeState.from_property_value(props["GET_ProbeState"]["value"]),
        online=online,
        connection_status="Online" if online else "Offline",
        last_updated_at=(
            _dt.datetime.now(tz=_dt.timezone.utc)
            - _dt.timedelta(seconds=age_seconds)
        ),
    )


def hydrate_as_captured(name: str):
    """Build a ``CombinedState`` exactly as the cloud served it.

    Keeps the fixture's real ``data_updated_at`` and ``connection_status`` —
    which is how the original bug is reproduced.
    """
    import datetime as _dt

    from nwf_lib.models import CombinedState, CookState, GrillState, ProbeState

    fx = load_fixture(name)
    props = fx["properties"]
    stamps = []
    for entry in props.values():
        raw = entry.get("data_updated_at")
        if isinstance(raw, str) and raw not in ("", "null"):
            stamps.append(_dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
    status = fx.get("device", {}).get("connection_status")
    return CombinedState(
        dsn="AC000W000000000",
        grill=GrillState.from_property_value(props["GET_GrillState"]["value"]),
        cook=CookState.from_property_value(props["GET_CookState"]["value"]),
        probes=ProbeState.from_property_value(props["GET_ProbeState"]["value"]),
        online=str(status).strip().casefold() == "online",
        connection_status=status,
        last_updated_at=max(stamps) if stamps else None,
    )


def fixture_names(transport: str = "ayla") -> list[str]:
    """Fixture stems for one transport.

    Fixtures declare their wire format in `_transport`; the Ayla captures
    predate the field and default to it. The two envelopes differ (Ayla nests
    under `properties`, AWS under `telemetry`), so tests must not mix them.
    """
    names = []
    for path in sorted(FIXTURES.glob("*.json")):
        with open(path) as fh:
            data = json.load(fh)
        if data.get("_transport", "ayla") == transport:
            names.append(path.stem)
    return names


def aws_fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("aws_*.json"))


def ayla_fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.startswith("aws_"))


@pytest.fixture
def fixture_loader():
    return load_fixture
