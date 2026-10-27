"""Holman iGardener RGB (Vibrance_RGB) integration."""
from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform, CONF_ADDRESS
from homeassistant.helpers import issue_registry as ir
from .device import HolmanDevice
from .const import CONF_FULL_INIT, DOMAIN
from .recovery import unreachable_issue_id

PLATFORMS = [Platform.LIGHT, Platform.BINARY_SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device = HolmanDevice(hass, entry.data[CONF_ADDRESS],
                          entry.options.get(CONF_FULL_INIT, entry.data.get(CONF_FULL_INIT, True)))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    device.async_start()
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        device = hass.data[DOMAIN].pop(entry.entry_id, None)
        if device is not None:
            await device.async_stop()
    return ok

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the persistent repair when the config entry is removed.

    Unload also runs on reload, so the issue stays across a reload. Removing
    the entry is the point at which nobody will clear it by reconnecting.
    """
    ir.async_delete_issue(
        hass, DOMAIN, unreachable_issue_id(entry.data[CONF_ADDRESS])
    )
