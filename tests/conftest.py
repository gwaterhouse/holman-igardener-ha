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
const.Platform = type("Platform", (), {"LIGHT": "light"})
const.CONF_ADDRESS = "address"
const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"

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
