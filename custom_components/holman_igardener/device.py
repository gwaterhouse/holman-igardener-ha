"""BLE connection handling for the Holman iGardener RGB (Vibrance_RGB) controller.

The controller accepts a single connection and only advertises briefly after
power-on or a disconnect, so the link is held open persistently:
  connect -> subscribe notify -> identify (wait for reply) -> set time
  -> session frame -> init slots 0..3 -> control frames as needed.
A 0x24 time frame is written every KEEPALIVE_S seconds to keep the link
alive and detect dead connections; a new advertisement triggers a reconnect.
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_ble_device_from_address,
    async_last_service_info,
    async_process_advertisements,
    async_scanner_devices_by_address,
    async_register_callback,
    BluetoothCallbackMatcher,
    BluetoothChange,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    NOTIFY_UUID,
    WRITE_UUID,
    control_frame,
    identify_frame,
    is_identify_response,
    off_frame,
    reconnect_init_frames,
    session_frame,
    set_time_frame,
)

_LOGGER = logging.getLogger(__name__)

ADV_MAX_AGE_S = 10.0
ADV_WAIT_S = 30.0  # controller advertises sparsely; wait for a fresh one
FAIL_COOLDOWN_S = 20.0
CONNECT_ATTEMPTS = 2  # one retry; first attempts commonly fail with GATT 133
LOCAL_ADAPTER_PREFIX = "hci"  # some onboard adapters cannot connect; prefer proxies
IDENTIFY_NOTIFY_TIMEOUT_S = 5.0
# Persistent link: the controller stops advertising a while after a
# disconnect, so the connection is held open.
PERSISTENT = True
KEEPALIVE_S = 240.0  # 0024 time frame; harmless, also detects dead links


class HolmanDevice:
    def __init__(self, hass: HomeAssistant, address: str, full_init: bool = True):
        self.hass = hass
        self.address = address
        self.full_init = full_init
        self.rgb = (255, 0, 0)
        self.level = 255
        self.on = False
        self._lock = asyncio.Lock()
        self._fail_until = 0.0  # monotonic
        self._identify_event = asyncio.Event()
        self._client = None

    def _notify(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        _LOGGER.debug("Holman notify %s: %s", self.address, raw.hex())
        if is_identify_response(raw):
            self._identify_event.set()

    async def _ensure_can_attempt(self) -> None:
        now = time.monotonic()
        if now < self._fail_until:
            remaining = self._fail_until - now
            raise RuntimeError(
                f"{self.address}: connect cooldown "
                f"({remaining:.0f}s left after previous failure)"
            )
        info = async_last_service_info(
            self.hass, self.address, connectable=True
        )
        if info is not None and now - info.time <= ADV_MAX_AGE_S:
            return
        # Stale/no advert: wait for a genuinely NEW one (the callback fires
        # immediately with the cached info, so filter on its timestamp).
        start = now
        _LOGGER.debug("Holman %s: waiting up to %ss for a fresh advertisement",
                      self.address, ADV_WAIT_S)
        try:
            await async_process_advertisements(
                self.hass,
                lambda si: si.time > start,
                {"address": self.address, "connectable": True},
                BluetoothScanningMode.ACTIVE,
                ADV_WAIT_S,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"{self.address}: no advertisement within {ADV_WAIT_S:.0f}s "
                "(out of range of a connectable proxy?)"
            ) from exc

    def _best_device(self):
        """Prefer a remote (ESPHome proxy) path; some local hciN adapters
        fail to connect to this controller."""
        remote = [
            d for d in async_scanner_devices_by_address(
                self.hass, self.address, connectable=True)
            if not d.scanner.source.lower().startswith(LOCAL_ADAPTER_PREFIX)
            and not getattr(d.scanner, "adapter", "").startswith(LOCAL_ADAPTER_PREFIX)
        ]
        if remote:
            best = max(remote, key=lambda d: d.advertisement.rssi)
            _LOGGER.debug("Holman %s: using scanner %s (rssi %s)", self.address,
                          best.scanner.name, best.advertisement.rssi)
            return best.ble_device
        return async_ble_device_from_address(
            self.hass, self.address, connectable=True)

    def _on_disconnect(self, _client) -> None:
        _LOGGER.debug("Holman %s: link dropped", self.address)
        self._client = None

    async def _open(self):
        """Connect + reconnect-init handshake; return a ready client."""
        await self._ensure_can_attempt()
        device = self._best_device()
        if device is None:
            self._fail_until = time.monotonic() + FAIL_COOLDOWN_S
            raise RuntimeError(
                f"{self.address} not seen by HA bluetooth (check range/proxy)"
            )
        t0 = time.monotonic()
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=CONNECT_ATTEMPTS,
            )
        except Exception:
            self._fail_until = time.monotonic() + FAIL_COOLDOWN_S
            _LOGGER.warning("Holman %s: connect failed; cooling down %ss",
                            self.address, FAIL_COOLDOWN_S)
            raise
        try:
            try:
                await client.start_notify(NOTIFY_UUID, self._notify)
            except Exception as exc:
                _LOGGER.warning("Holman %s: notify subscribe failed: %s",
                                self.address, exc)
            self._identify_event.clear()
            await client.write_gatt_char(WRITE_UUID, identify_frame(), response=True)
            try:
                await asyncio.wait_for(self._identify_event.wait(),
                                       timeout=IDENTIFY_NOTIFY_TIMEOUT_S)
            except asyncio.TimeoutError:
                _LOGGER.warning("Holman %s: identify notify timed out", self.address)
            when = dt_util.now()
            frames = (reconnect_init_frames(when, self.rgb, self.level)
                      if self.full_init else [set_time_frame(when), session_frame()])
            for frame in frames:
                _LOGGER.debug("Holman write %s -> %s", WRITE_UUID, frame.hex())
                await client.write_gatt_char(WRITE_UUID, frame, response=True)
        except Exception:
            self._fail_until = time.monotonic() + FAIL_COOLDOWN_S
            await self._safe_disconnect(client)
            raise
        self._fail_until = 0.0
        _LOGGER.debug("Holman %s: link up + init in %.1fs", self.address,
                      time.monotonic() - t0)
        return client

    async def _safe_disconnect(self, client) -> None:
        try:
            await client.disconnect()
        except Exception as exc:
            _LOGGER.debug("Holman %s: disconnect error: %s", self.address, exc)

    async def _send(self, command_frames: list[bytes]) -> None:
        client = self._client if (self._client and self._client.is_connected) else None
        if client is None:
            client = await self._open()
            if PERSISTENT:
                self._client = client
        try:
            for frame in command_frames:
                _LOGGER.debug("Holman write %s -> %s", WRITE_UUID, frame.hex())
                await client.write_gatt_char(WRITE_UUID, frame, response=True)
        except Exception:
            # stale link: drop it; next command (or advert) reconnects
            self._client = None
            await self._safe_disconnect(client)
            raise
        finally:
            if not PERSISTENT:
                await self._safe_disconnect(client)

    # ---- persistent-link maintenance ----
    def async_start(self) -> None:
        """Called from async_setup_entry when PERSISTENT."""
        if not PERSISTENT:
            return
        self._unsub_adv = async_register_callback(
            self.hass, self._on_advert,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )
        self._keepalive_task = self.hass.async_create_background_task(
            self._keepalive_loop(), f"holman_keepalive_{self.address}")

    def _on_advert(self, _si, _change: BluetoothChange) -> None:
        # Controller is advertising = no link. Grab it while it's awake.
        if self._client is None and not self._lock.locked() \
                and time.monotonic() >= self._fail_until:
            self.hass.async_create_task(self._reconnect())

    async def _reconnect(self) -> None:
        async with self._lock:
            if self._client is not None and self._client.is_connected:
                return
            try:
                self._client = await self._open()
            except Exception as exc:
                _LOGGER.debug("Holman %s: background reconnect failed: %s",
                              self.address, exc)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            async with self._lock:
                c = self._client
                if c is None or not c.is_connected:
                    continue
                try:
                    await c.write_gatt_char(WRITE_UUID, set_time_frame(dt_util.now()),
                                            response=True)
                except Exception as exc:
                    _LOGGER.debug("Holman %s: keepalive failed: %s", self.address, exc)
                    self._client = None
                    await self._safe_disconnect(c)

    async def async_stop(self) -> None:
        if getattr(self, "_unsub_adv", None):
            self._unsub_adv()
        if getattr(self, "_keepalive_task", None):
            self._keepalive_task.cancel()
        if self._client is not None:
            await self._safe_disconnect(self._client)
            self._client = None

    async def turn_on(self, rgb=None, level=None):
        if rgb is not None:
            self.rgb = tuple(rgb)
        if level is not None:
            self.level = int(level)
        async with self._lock:
            await self._send([control_frame(self.rgb, self.level)])
        self.on = True

    async def turn_off(self):
        async with self._lock:
            await self._send([off_frame()])
        self.on = False
