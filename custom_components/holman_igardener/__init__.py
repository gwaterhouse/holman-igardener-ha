"""Holman iGardener RGB (Vibrance_RGB) integration."""
from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform, CONF_ADDRESS
from .device import HolmanDevice
from .const import CONF_FULL_INIT, DOMAIN

PLATFORMS = [Platform.LIGHT]

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
