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


# ---------------------------------------------------------------- coordinator

# `coordinator.py` is the only Home Assistant module worth unit-testing: it
# holds the cook-lifecycle state machine, which is pure logic over two
# consecutive snapshots and has no business needing a running Home Assistant
# to exercise. It imports four things from `homeassistant`, so it is cheaper
# to stand those up than to install the framework.
#
# Registering `nwf_ha` the same way as `nwf_lib` above keeps the integration
# directory off `sys.path` — `select.py`, `button.py` and `number.py` shadow
# stdlib modules, and only ever resolve here as `nwf_ha.select`.

_HA_PKG = "nwf_ha"
INTEGRATION = ROOT / "custom_components" / "ninja_woodfire"


class FakeDevice:
    """A device registry entry, as far as the coordinator is concerned."""

    def __init__(self, device_id: str, name: str, name_by_user: str | None = None):
        self.id = device_id
        self.name = name
        self.name_by_user = name_by_user


class FakeDeviceRegistry:
    def __init__(self, devices: dict[frozenset, FakeDevice] | None = None):
        self._devices = devices or {}

    def add(self, identifiers: set, device: FakeDevice) -> None:
        self._devices[frozenset(identifiers)] = device

    def async_get_device(self, identifiers=None, connections=None):
        return self._devices.get(frozenset(identifiers or ()))


class FakeBus:
    """Records what was fired instead of dispatching it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, dict(data or {})))

    def of_type(self, event_type: str) -> list[dict]:
        return [data for name, data in self.events if name == event_type]

    def types(self) -> list[str]:
        return [name for name, _ in self.events]


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.device_registry = FakeDeviceRegistry()


def _install_ha_stubs() -> None:
    """Minimal stand-ins for the three Home Assistant imports coordinator uses."""
    if _HA_PKG in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = FakeHass

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    # The real `async_get` returns the singleton registry for a hass instance.
    device_registry.async_get = lambda hass: hass.device_registry

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            # Home Assistant's own coordinator starts with no data, and the
            # liveness properties read it before the first poll lands.
            self.data = None
            self.last_update_success = True

        async def async_request_refresh(self) -> None:
            return None

        def __class_getitem__(cls, item):
            return cls

    class _UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
    update_coordinator.UpdateFailed = _UpdateFailed

    for name, module in {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }.items():
        sys.modules.setdefault(name, module)

    pkg = types.ModuleType(_HA_PKG)
    pkg.__path__ = [str(INTEGRATION)]
    pkg.__package__ = _HA_PKG
    sys.modules[_HA_PKG] = pkg


def _install_platform_stubs() -> None:
    """The rest of the Home Assistant surface the entity modules touch.

    Enough to import `sensor.py` / `switch.py` and read their description
    tables — which is the point: those tables are where the behaviour lives,
    as `value_fn` / `attrs_fn` / `available_fn` lambdas. Re-implementing one
    in a test to "check the logic" tests the copy, not the code.

    The entity description bases have to be real dataclasses with defaulted
    fields, because the integration subclasses them with `kw_only=True`.
    """
    import dataclasses
    import enum

    if "homeassistant.components.sensor" in sys.modules:
        return

    # Split the way Home Assistant splits them. A single shared base would
    # accept `state_class=` on a switch row — a TypeError on a real instance,
    # silently green here, which is the opposite of what this layer is for.
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class _EntityDescription:
        key: str
        translation_key: str | None = None
        device_class: object | None = None
        entity_category: object | None = None
        entity_registry_enabled_default: bool = True
        icon: str | None = None
        name: str | None = None

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class _MeasuringEntityDescription(_EntityDescription):
        """Sensor and number: the ones that carry a unit."""

        native_unit_of_measurement: str | None = None
        state_class: object | None = None

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class _NumberEntityDescription(_MeasuringEntityDescription):
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: object | None = None

    class _StrEnum(str, enum.Enum):
        pass

    # Exactly the members the integration uses, spelled as Home Assistant
    # spells them. Deliberately not permissive: reaching for a constant that
    # does not exist upstream should fail here rather than at runtime on
    # somebody's grill.
    class SensorDeviceClass(_StrEnum):
        DURATION = "duration"
        TEMPERATURE = "temperature"
        TIMESTAMP = "timestamp"

    class SensorStateClass(_StrEnum):
        MEASUREMENT = "measurement"

    class BinarySensorDeviceClass(_StrEnum):
        CONNECTIVITY = "connectivity"
        OPENING = "opening"
        PLUG = "plug"

    class EntityCategory(_StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class UnitOfTemperature(_StrEnum):
        CELSIUS = "\u00b0C"
        FAHRENHEIT = "\u00b0F"

    class UnitOfTime(_StrEnum):
        MINUTES = "min"
        SECONDS = "s"

    class NumberMode(_StrEnum):
        BOX = "box"
        SLIDER = "slider"

    modules: dict[str, dict] = {
        "homeassistant.components": {},
        "homeassistant.components.sensor": {
            "SensorEntity": type("SensorEntity", (), {}),
            "SensorEntityDescription": _MeasuringEntityDescription,
            "SensorDeviceClass": SensorDeviceClass,
            "SensorStateClass": SensorStateClass,
        },
        "homeassistant.components.switch": {
            "SwitchEntity": type("SwitchEntity", (), {}),
            "SwitchEntityDescription": _EntityDescription,
        },
        "homeassistant.components.binary_sensor": {
            "BinarySensorEntity": type("BinarySensorEntity", (), {}),
            "BinarySensorEntityDescription": _EntityDescription,
            "BinarySensorDeviceClass": BinarySensorDeviceClass,
        },
        "homeassistant.components.button": {
            "ButtonEntity": type("ButtonEntity", (), {}),
            "ButtonEntityDescription": _EntityDescription,
        },
        "homeassistant.helpers.entity": {"EntityCategory": EntityCategory},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.components.number": {
            "NumberEntity": type("NumberEntity", (), {}),
            "NumberEntityDescription": _NumberEntityDescription,
            "NumberMode": NumberMode,
        },
        "homeassistant.components.select": {
            "SelectEntity": type("SelectEntity", (), {}),
            "SelectEntityDescription": _EntityDescription,
        },
        "homeassistant.const": {
            "PERCENTAGE": "%",
            "UnitOfTemperature": UnitOfTemperature,
            "UnitOfTime": UnitOfTime,
        },
    }
    for name, attrs in modules.items():
        module = types.ModuleType(name)
        if name == "homeassistant.components":
            module.__path__ = []
        for attr, value in attrs.items():
            setattr(module, attr, value)
        sys.modules.setdefault(name, module)

    # entity.py's two, added to the already-installed helper modules.
    dr = sys.modules["homeassistant.helpers.device_registry"]
    if not hasattr(dr, "DeviceInfo"):
        dr.DeviceInfo = dict
    uc = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(uc, "CoordinatorEntity"):
        class _CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

            def __class_getitem__(cls, item):
                return cls

            @property
            def available(self) -> bool:
                # Home Assistant returns the coordinator's last poll result
                # here, and `NinjaWoodfireEntity.available` builds on it — so
                # returning a flat True would give a wrong answer for the
                # failed-poll path the moment anything tests it.
                return getattr(self.coordinator, "last_update_success", True)

        uc.CoordinatorEntity = _CoordinatorEntity


def load_coordinator():
    """Import `coordinator.py` against the stubs above."""
    _install_ha_stubs()
    import importlib

    return importlib.import_module(f"{_HA_PKG}.coordinator")


def load_platform(name: str):
    """Import one of the entity platform modules — `sensor`, `switch`, ...

    So a test can reach the real `value_fn` / `attrs_fn` / `available_fn` out
    of the description tables rather than restating them.
    """
    _install_ha_stubs()
    _install_platform_stubs()
    import importlib

    return importlib.import_module(f"{_HA_PKG}.{name}")


def description(module, key: str):
    """The description row with this `key`, from whichever table holds it.

    Strict about duplicates: silently returning the first of two would let a
    test assert against a row it did not mean.
    """
    found = [
        entry
        for table in vars(module).values()
        if isinstance(table, tuple)
        for entry in table
        if getattr(entry, "key", None) == key
    ]
    if not found:
        raise AssertionError(f"no description with key {key!r} in {module.__name__}")
    if len(found) > 1:
        raise AssertionError(f"{len(found)} descriptions with key {key!r}")
    return found[0]


def hydrate_aws(name: str, *, age_seconds: float = 0.0, online: bool = True):
    """`hydrate` for the AWS envelope.

    The two backends serve different shapes — Ayla nests under `properties`,
    AWS under `telemetry` — so they need separate constructors. As with
    `hydrate`, the timestamp is rebased so liveness is deterministic.
    """
    import datetime as _dt

    from nwf_lib.api.aws import state_from_device

    fx = load_fixture(name)
    state = state_from_device(
        "AC000W000000000",
        {
            "telemetry": fx["telemetry"],
            "connectivityStatus": {"connected": online},
            "updatedAt": fx["device"].get("updatedAt"),
        },
    )
    state.online = online
    state.last_updated_at = (
        _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(seconds=age_seconds)
    )
    return state
