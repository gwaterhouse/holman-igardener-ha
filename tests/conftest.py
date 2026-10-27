"""Import stubs so tests can load the integration without Home Assistant."""
from __future__ import annotations

import sys
import types
from datetime import datetime


def _pkg(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    mod.__path__ = []
    mod.__package__ = name
    if "." in name:
        parent_name, _, child = name.rpartition(".")
        setattr(_pkg(parent_name), child, mod)
    return mod


def _mod(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    if "." in name:
        parent_name, _, child = name.rpartition(".")
        mod.__package__ = parent_name
        setattr(_pkg(parent_name), child, mod)
    return mod


_pkg("homeassistant")
_pkg("homeassistant.components")
_pkg("homeassistant.helpers")
_pkg("homeassistant.util")

const = _mod("homeassistant.const")
const.Platform = type(
    "Platform", (), {"LIGHT": "light", "BINARY_SENSOR": "binary_sensor"}
)
const.CONF_ADDRESS = "address"
const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
const.EntityCategory = type("EntityCategory", (), {"DIAGNOSTIC": "diagnostic"})


class _Entity:
    """Enough of homeassistant.helpers.entity.Entity for these tests."""

    def async_on_remove(self, callback):
        self._unsubs = getattr(self, "_unsubs", [])
        self._unsubs.append(callback)

    async def async_added_to_hass(self):
        return None

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1

    @property
    def name(self):
        return getattr(self, "_attr_name", None)

    @property
    def unique_id(self):
        return getattr(self, "_attr_unique_id", None)

    @property
    def device_class(self):
        return getattr(self, "_attr_device_class", None)

    @property
    def entity_category(self):
        return getattr(self, "_attr_entity_category", None)

    @property
    def device_info(self):
        return getattr(self, "_attr_device_info", None)


entity_platform = _mod("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object

exceptions = _mod("homeassistant.exceptions")
exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

light = _mod("homeassistant.components.light")
light.ColorMode = type("ColorMode", (), {"RGB": "rgb"})
light.ATTR_BRIGHTNESS = "brightness"
light.ATTR_RGB_COLOR = "rgb_color"
light.LightEntity = type("LightEntity", (_Entity,), {})

binary_sensor = _mod("homeassistant.components.binary_sensor")
binary_sensor.BinarySensorDeviceClass = type(
    "BinarySensorDeviceClass", (), {"CONNECTIVITY": "connectivity"}
)
binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (_Entity,), {})

core = _mod("homeassistant.core")
core.HomeAssistant = type("HomeAssistant", (), {})

entries = _mod("homeassistant.config_entries")
entries.ConfigEntry = type("ConfigEntry", (), {})

dt = _mod("homeassistant.util.dt")
dt.now = lambda: datetime(2026, 10, 26, 12, 0, 0)

bluetooth = _mod("homeassistant.components.bluetooth")
bluetooth.BluetoothScanningMode = type("BluetoothScanningMode", (), {"ACTIVE": "active"})
bluetooth.BluetoothChange = type("BluetoothChange", (), {"ADVERTISEMENT": "advertisement"})


class _Matcher:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _register(*_args, **_kwargs):
    return lambda: None


def _no_device(*_args, **_kwargs):
    return None


def _no_rows(*_args, **_kwargs):
    return []


async def _no_advert(*_args, **_kwargs):
    raise TimeoutError


bluetooth.BluetoothCallbackMatcher = _Matcher
bluetooth.async_register_callback = _register
bluetooth.async_ble_device_from_address = _no_device
bluetooth.async_last_service_info = _no_device
bluetooth.async_scanner_devices_by_address = _no_rows
bluetooth.async_process_advertisements = _no_advert

issues = _mod("homeassistant.helpers.issue_registry")
issues.IssueSeverity = type(
    "IssueSeverity", (), {"ERROR": "error", "WARNING": "warning"}
)
issues.async_create_issue = lambda *_a, **_k: None
issues.async_delete_issue = lambda *_a, **_k: None

bleak = _mod("bleak_retry_connector")
bleak.BleakClientWithServiceCache = type("BleakClientWithServiceCache", (), {})


async def _establish(*args, **kwargs):
    _establish.calls.append((args, kwargs))
    raise RuntimeError("unexpected establish_connection")


async def _close_stale(*args, **kwargs):
    _close_stale.calls.append((args, kwargs))


_establish.calls = []
_close_stale.calls = []
bleak.establish_connection = _establish
bleak.close_stale_connections_by_address = _close_stale
