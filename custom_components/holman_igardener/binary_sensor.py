"""Diagnostic connectivity sensor for Holman iGardener RGB.

This reports the same sustained-outage latch as the light's availability.
It stays available and turns off, so an automation can watch for "off"
without the entity itself disappearing. It is on during startup and during
a short proxy blip; those are not yet an outage.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, address_slug, device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback
) -> None:
    add(
        [
            HolmanConnectivity(
                hass.data[DOMAIN][entry.entry_id], entry.data[CONF_ADDRESS]
            )
        ]
    )


class HolmanConnectivity(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_name = "Holman iGardener RGB Connectivity"

    def __init__(self, device, address: str) -> None:
        self._device = device
        self._attr_unique_id = f"{address_slug(address)}_connectivity"
        self._attr_device_info = device_info(address)

    @property
    def available(self) -> bool:
        # The light is what becomes unavailable. This sensor stays so its
        # off state is visible.
        return True

    @property
    def is_on(self) -> bool:
        return self._device.reachable

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))
