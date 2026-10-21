"""Light platform for Holman iGardener RGB."""
from __future__ import annotations
from .const import DOMAIN
from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS, ATTR_RGB_COLOR
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import HomeAssistantError

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback):
    add([HolmanLight(hass.data[DOMAIN][entry.entry_id], entry.data[CONF_ADDRESS])])

class HolmanLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_name = "Holman iGardener RGB"
    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, device, address):
        self._device = device
        self._attr_unique_id = address.replace(":", "").lower()
        self._attr_device_info = {"identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": "Holman iGardener RGB", "manufacturer": "Holman",
            "model": "iGardener RGB (Vibrance_RGB)", "connections": {("bluetooth", address)}}

    @property
    def is_on(self): return self._device.on
    @property
    def brightness(self): return self._device.level
    @property
    def rgb_color(self): return self._device.rgb

    async def async_turn_on(self, **kwargs):
        rgb = kwargs.get(ATTR_RGB_COLOR, self._device.rgb)
        level = kwargs.get(ATTR_BRIGHTNESS, self._device.level if self._device.level else 255)
        try:
            await self._device.turn_on(rgb=rgb, level=level)
        except Exception as exc:
            raise HomeAssistantError(f"Holman turn_on failed: {exc}") from exc
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        try:
            await self._device.turn_off()
        except Exception as exc:
            raise HomeAssistantError(f"Holman turn_off failed: {exc}") from exc
        self.async_write_ha_state()
