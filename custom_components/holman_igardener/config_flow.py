"""Config flow for Holman iGardener RGB (Vibrance_RGB)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_ADDRESS

from .const import CONF_FULL_INIT, DOMAIN
from .protocol import DEVICE_NAME, SERVICE_UUID


def _is_supported(info: BluetoothServiceInfoBleak) -> bool:
    return info.name == DEVICE_NAME or SERVICE_UUID in info.service_uuids


class HolmanFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(self, info: BluetoothServiceInfoBleak):
        await self.async_set_unique_id(info.address)
        self._abort_if_unique_id_configured()
        self._address = info.address
        self.context["title_placeholders"] = {"name": info.name or DEVICE_NAME}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title=DEVICE_NAME,
                data={CONF_ADDRESS: self._address, CONF_FULL_INIT: True},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm", description_placeholders={"address": self._address}
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=DEVICE_NAME,
                data={CONF_ADDRESS: address, CONF_FULL_INIT: True},
            )
        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not _is_supported(info):
                continue
            self._discovered[info.address] = f"{info.name or DEVICE_NAME} ({info.address})"
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )
