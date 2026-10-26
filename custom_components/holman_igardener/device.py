"""BLE connection handling for the Holman iGardener RGB (Vibrance_RGB) controller.

The controller accepts a single connection and only advertises briefly after
power-on or a disconnect. The protocol has no close command. The link is held
open, and shutdown waits for the Bluetooth client to disconnect so the
controller can advertise again.

One reconnect task owns retries. The first attempt waits out the startup
delay. A dropped link is retried once, shortly after. Failed attempts then
wait 30s, 60s, 120s and 180s. Advertisements do not start extra connects.
If the controller stays unreachable, a repair issue asks for a power-cycle.
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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)

from .const import DOMAIN
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
from .recovery import (
    CONNECT_ATTEMPTS,
    CONNECT_BUDGET_S,
    DISCONNECT_TIMEOUT_S,
    GATT_OP_TIMEOUT_S,
    RECONNECT_SOON_S,
    STALE_ADV_GRACE_S,
    STARTUP_DELAY_S,
    UNREACHABLE_REPAIR_S,
    WATCHDOG_INTERVAL_S,
    AttemptDeferred,
    backoff_seconds,
    ready_in,
    repair_due,
    unreachable_issue_id,
)

_LOGGER = logging.getLogger(__name__)

ADV_MAX_AGE_S = 10.0
ADV_WAIT_S = 30.0  # controller advertises sparsely; wait for a fresh one
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
        self._failure_count = 0
        self._identify_event = asyncio.Event()
        self._client = None
        self._stopping = False
        self._stop_wait: asyncio.Event | None = None
        self._startup_not_before = (
            time.monotonic() + STARTUP_DELAY_S if PERSISTENT else 0.0
        )
        self._wanted_since: float | None = None
        self._link_up_at: float | None = None
        self._repair_open = False
        self._reconnect_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._stale_task: asyncio.Task | None = None
        self._command_task: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        # In-flight client.disconnect() tasks, keyed by id(client). One close
        # per client so a stop snapshot and the owner cannot both call it.
        self._disconnect_tasks: dict[int, asyncio.Task] = {}
        self._disconnecting: set[int] = set()
        self._unsub_adv = None
        self._unsub_stop = None

    def _notify(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        _LOGGER.debug("Holman notify %s: %s", self.address, raw.hex())
        if is_identify_response(raw):
            self._identify_event.set()

    def _live_client(self):
        client = self._client
        if (
            client is not None
            and id(client) not in self._disconnecting
            and getattr(client, "is_connected", False)
        ):
            return client
        return None

    def _ready_in(self) -> float:
        return ready_in(time.monotonic(), self._startup_not_before, self._fail_until)

    def _mark_wanted(self) -> None:
        """Start the unreachable clock once we actually want a link."""
        if self._wanted_since is None:
            self._wanted_since = time.monotonic()

    def _advert_time(self, service_info) -> float:
        stamp = getattr(service_info, "time", None)
        if isinstance(stamp, (int, float)):
            return float(stamp)
        return time.monotonic()

    async def _ensure_can_attempt(self) -> None:
        if self._stopping or self._ready_in() > 0:
            raise AttemptDeferred(
                f"{self.address}: next attempt in {self._ready_in():.0f}s"
            )
        now = time.monotonic()
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
        except TimeoutError as exc:
            raise RuntimeError(
                f"{self.address}: no advertisement within {ADV_WAIT_S:.0f}s "
                "(out of range, or the controller is not advertising)"
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

    def _on_disconnect(self, client) -> None:
        # A close we started keeps self._client until that wait finishes.
        # The caller decides whether to reconnect.
        if id(client) in self._disconnecting:
            return
        if self._client is not client or self._stopping:
            if self._client is client:
                self._client = None
            return
        self._client = None
        self._reconnect_soon("link dropped")

    async def _write(self, client, frame: bytes) -> None:
        _LOGGER.debug("Holman write %s -> %s", WRITE_UUID, frame.hex())
        await asyncio.wait_for(
            client.write_gatt_char(WRITE_UUID, frame, response=True),
            timeout=GATT_OP_TIMEOUT_S,
        )

    async def _open(self):
        """Connect + reconnect-init handshake; return a ready client."""
        await self._ensure_can_attempt()
        device = self._best_device()
        if device is None:
            raise RuntimeError(
                f"{self.address} not seen by HA bluetooth (check range/proxy)"
            )
        t0 = time.monotonic()
        # BlueZ only: drop a stale kernel connection before dialing. This is
        # a no-op for an ESPHome proxy; that slot is released by disconnect()
        # on the client we held, or by the proxy when its API session ends.
        await self._release_proxy_slot()
        try:
            client = await self._dial(device)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._release_proxy_slot()
            raise
        try:
            try:
                await asyncio.wait_for(
                    client.start_notify(NOTIFY_UUID, self._notify),
                    timeout=GATT_OP_TIMEOUT_S,
                )
            except Exception as exc:
                _LOGGER.warning("Holman %s: notify subscribe failed: %s",
                                self.address, exc)
            self._identify_event.clear()
            await self._write(client, identify_frame())
            try:
                await asyncio.wait_for(self._identify_event.wait(),
                                       timeout=IDENTIFY_NOTIFY_TIMEOUT_S)
            except TimeoutError:
                _LOGGER.warning("Holman %s: identify notify timed out", self.address)
            when = dt_util.now()
            frames = (reconnect_init_frames(when, self.rgb, self.level)
                      if self.full_init else [set_time_frame(when), session_frame()])
            for frame in frames:
                await self._write(client, frame)
        except asyncio.CancelledError:
            await self._close_local(client)
            raise
        except Exception:
            await self._abandon(client)
            raise
        if self._stopping:
            await self._abandon(client)
            raise RuntimeError(f"{self.address}: integration is stopping")
        _LOGGER.debug("Holman %s: link up + init in %.1fs", self.address,
                      time.monotonic() - t0)
        return client

    async def _dial(self, device):
        """Run establish_connection without cancelling it.

        bleak-retry-connector builds the client internally and returns it
        only after connect() succeeds. Cancelling that task drops the
        client while is_connected is still true, so the ESPHome proxy slot
        stays taken. max_attempts limits timeouts and connect errors only;
        ESPHome out-of-slot errors keep retrying inside the library.
        A deadline or a stop waits for the call to finish, then disconnects
        any client it returned. The wait after the deadline is shielded too:
        a bare await is what cancels the dial when shutdown lands there.
        The client exists only inside that task until it returns. After it
        returns, the only reference is local until _disconnect_client or the
        caller stores it. The close itself is shielded, and _connect_task is
        cleared only after that wait.
        """
        task = asyncio.create_task(
            establish_connection(
                BleakClientWithServiceCache,
                device,
                self.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=CONNECT_ATTEMPTS,
            ),
            name=f"holman_dial_{self.address}",
        )
        self._connect_task = task
        try:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task), timeout=CONNECT_BUDGET_S
                )
            except TimeoutError as exc:
                await self._disconnect_client(await self._take_connect_result(task))
                raise RuntimeError(f"{self.address}: connect timed out") from exc
            except asyncio.CancelledError:
                await self._disconnect_client(await self._take_connect_result(task))
                raise
        finally:
            # The handlers above have already finished their disconnect wait.
            if self._connect_task is task:
                self._connect_task = None

    async def _take_connect_result(self, task):
        """Wait until the dial finishes without cancelling it.

        Shutdown cancels this task. A bare await would cancel the dial too.
        Shield it, and if that wait is cancelled, uncancel and keep waiting.
        """
        while not task.done():
            current = asyncio.current_task()
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
            except Exception:
                return None
        try:
            return task.result()
        except (asyncio.CancelledError, Exception):
            return None

    def _absorb_cancel(self) -> bool:
        """Clear pending cancellation so the next await can run.

        Returns True when this task was being cancelled. Python 3.11+
        re-raises CancelledError at the next await unless the count is
        brought back to zero.
        """
        current = asyncio.current_task()
        if current is None or not current.cancelling():
            return False
        while current.cancelling():
            current.uncancel()
        return True

    def _track_disconnect(self, client) -> asyncio.Task | None:
        """Start one disconnect() for this client, or join the one in flight."""
        if getattr(client, "_holman_closed", False):
            return None
        key = id(client)
        # Mark before the first await so a cancel cannot land with no task
        # stored and no self._client entry left to close.
        self._disconnecting.add(key)
        existing = self._disconnect_tasks.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._invoke_disconnect(client),
            name=f"holman_disc_{self.address}",
        )
        self._disconnect_tasks[key] = task
        return task

    async def _invoke_disconnect(self, client) -> None:
        key = id(client)
        cancelled = False
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            _LOGGER.debug("Holman %s: disconnect error: %s", self.address, exc)
        finally:
            if not cancelled:
                try:
                    client._holman_closed = True
                except Exception:
                    _LOGGER.debug(
                        "Holman %s: could not mark client closed", self.address
                    )
                # disconnect() has returned. Drop the stored reference before
                # this task yields so a callback from the close cannot observe
                # a client we have already disconnected.
                if self._client is client:
                    self._client = None
            self._disconnecting.discard(key)
            current = self._disconnect_tasks.get(key)
            if current is asyncio.current_task():
                self._disconnect_tasks.pop(key, None)

    async def _await_shielded(self, task: asyncio.Task, timeout: float) -> None:
        """Wait for task. Cancelling this caller does not cancel task.

        At timeout the task is cancelled. That is the bound for a stuck
        close. If this caller was cancelled, CancelledError is raised
        only after the wait.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        cancelled = self._absorb_cancel()
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                if not task.done():
                    _LOGGER.debug(
                        "Holman %s: abandoning a close after %.0fs",
                        self.address, timeout,
                    )
                    task.cancel()
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                if not task.done():
                    _LOGGER.debug(
                        "Holman %s: abandoning a close after %.0fs",
                        self.address, timeout,
                    )
                    task.cancel()
                break
            except asyncio.CancelledError:
                # Our own cancel raises here while the shielded task keeps
                # running. A cancel of the task itself (the timeout abandon)
                # also surfaces as CancelledError; that close is already over.
                if self._absorb_cancel():
                    cancelled = True
                if task.done() or not cancelled:
                    break
                continue
        if cancelled or self._absorb_cancel():
            raise asyncio.CancelledError

    async def _disconnect_client(self, client) -> None:
        """Finish disconnect() even when this task is being cancelled.

        The caller often holds the client only in a local. async_stop
        cancels that task, and CancelledError is not an Exception, so a
        bare await aborts the GATT close and drops the proxy slot.
        """
        if client is None:
            return
        task = self._track_disconnect(client)
        if task is None:
            if self._absorb_cancel():
                raise asyncio.CancelledError
            return
        await self._await_shielded(task, DISCONNECT_TIMEOUT_S)

    async def _close_local(self, client) -> None:
        """Disconnect a client, then drop self._client if it is still that one."""
        if client is None:
            return
        try:
            await self._disconnect_client(client)
        finally:
            if self._client is client:
                self._client = None

    async def _drain_disconnects(self) -> None:
        """Wait out closes still running after their owner was left behind."""
        tasks = [
            task for task in list(self._disconnect_tasks.values()) if not task.done()
        ]
        for task in tasks:
            await self._await_shielded(task, DISCONNECT_TIMEOUT_S)

    async def _release_proxy_slot(self) -> None:
        """Best-effort BlueZ stale-connection cleanup.

        close_stale_connections_by_address returns immediately unless this
        host is Linux and BlueZ still has the device. It does not clear an
        ESPHome Bluetooth proxy connection slot. The slot this integration
        can release is the one held by its own client, via disconnect().
        After a hard kill the proxy frees that slot when the API session
        drops. Do not construct another Bleak client just to disconnect:
        the ESPHome backend needs the proxy API session and a second client
        will not free the slot.
        """
        try:
            await close_stale_connections_by_address(self.address)
        except Exception as exc:
            _LOGGER.debug(
                "Holman %s: stale connection cleanup failed: %s",
                self.address, exc,
            )

    async def _abandon(self, client) -> None:
        """Disconnect this client and forget it. Does not schedule a reconnect."""
        if client is None:
            return
        await self._close_local(client)
        await self._release_proxy_slot()

    def _note_failure(self, exc: BaseException) -> None:
        self._failure_count += 1
        wait = backoff_seconds(self._failure_count)
        self._fail_until = time.monotonic() + wait
        self._mark_wanted()
        _LOGGER.warning(
            "Holman %s: connect failed (%s); next attempt in %.0fs (failure %s)",
            self.address, exc, wait, self._failure_count,
        )
        self._maybe_raise_repair()
        self._schedule_reconnect()

    def _note_success(self) -> None:
        failed = self._failure_count
        had_issue = self._repair_open
        self._failure_count = 0
        self._fail_until = 0.0
        self._wanted_since = None
        self._link_up_at = time.monotonic()
        self._clear_repair()
        if had_issue:
            _LOGGER.info(
                "Holman %s: link is back; cleared the repair issue", self.address
            )
        elif failed:
            _LOGGER.info(
                "Holman %s: link recovered after %s failed attempt(s)",
                self.address, failed,
            )
        else:
            _LOGGER.info("Holman %s: link up", self.address)

    def _reconnect_soon(self, reason: str) -> None:
        if self._stopping:
            return
        _LOGGER.warning(
            "Holman %s: %s; reconnecting in %.0fs",
            self.address, reason, RECONNECT_SOON_S,
        )
        self._fail_until = time.monotonic() + RECONNECT_SOON_S
        self._mark_wanted()
        self._schedule_reconnect()

    def _maybe_raise_repair(self) -> None:
        if self._stopping or self._repair_open:
            return
        if not repair_due(
            time.monotonic(),
            self._wanted_since,
            self._live_client() is not None,
            after=UNREACHABLE_REPAIR_S,
        ):
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            unreachable_issue_id(self.address),
            is_fixable=False,
            is_persistent=True,
            learn_more_url=(
                "https://github.com/gwaterhouse/holman-igardener-ha#troubleshooting"
            ),
            severity=ir.IssueSeverity.ERROR,
            translation_key="unreachable",
            translation_placeholders={"address": self.address},
        )
        self._repair_open = True
        _LOGGER.error(
            "Holman %s: unreachable for %.0f minutes. If it is not advertising, "
            "power-cycle the controller. Raised repair %s.",
            self.address,
            UNREACHABLE_REPAIR_S / 60,
            unreachable_issue_id(self.address),
        )

    def _clear_repair(self) -> None:
        """Drop the repair even if this process did not create it.

        The issue is persistent, so a restart must clear it on the next
        successful link and not only when the in-memory flag is set.
        """
        self._repair_open = False
        ir.async_delete_issue(self.hass, DOMAIN, unreachable_issue_id(self.address))

    async def _send(self, command_frames: list[bytes]) -> None:
        if self._stopping:
            raise RuntimeError(f"{self.address}: integration is stopping")
        client = self._live_client()
        try:
            if client is None:
                held = self._client
                if held is not None and id(held) not in self._disconnecting:
                    await self._abandon(held)
                if self._stopping:
                    raise RuntimeError(f"{self.address}: integration is stopping")
                if self._ready_in() > 0:
                    raise RuntimeError(
                        f"{self.address}: not connected; next attempt in "
                        f"{self._ready_in():.0f}s"
                    )
                self._mark_wanted()
                try:
                    client = await self._open()
                except AttemptDeferred as exc:
                    raise RuntimeError(str(exc)) from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._stopping:
                        self._note_failure(exc)
                    raise
                if self._stopping:
                    await self._abandon(client)
                    raise RuntimeError(f"{self.address}: integration is stopping")
                if PERSISTENT:
                    self._client = client
                    self._note_success()
            dropped = False
            try:
                for frame in command_frames:
                    await self._write(client, frame)
            except Exception as exc:
                dropped = True
                await self._abandon(client)
                client = None
                if PERSISTENT and not self._stopping:
                    self._note_failure(exc)
                raise
            finally:
                if not PERSISTENT and not dropped and client is not None:
                    await self._disconnect_client(client)
        except asyncio.CancelledError:
            # Handshake, the command write, or the non-persistent close can
            # be the await stop cancels. The client is local until stored.
            if (
                client is not None
                and not getattr(client, "_holman_closed", False)
            ):
                await self._close_local(client)
            elif client is not None and self._client is client:
                self._client = None
            raise

    def _spawn(self, coro, suffix: str):
        name = f"holman_{suffix}_{self.address}"
        create = getattr(self.hass, "async_create_background_task", None)
        if create is None:
            return self.hass.async_create_task(coro)
        return create(coro, name=name)

    # ---- persistent-link maintenance ----
    def async_start(self) -> None:
        """Called from async_setup_entry when PERSISTENT."""
        if not PERSISTENT or self._stopping:
            return
        self._unsub_adv = async_register_callback(
            self.hass, self._on_advert,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._on_ha_stop,
        )
        self._keepalive_task = self._spawn(self._keepalive_loop(), "keepalive")
        self._watchdog_task = self._spawn(self._watchdog_loop(), "watchdog")
        self._schedule_reconnect()

    async def _on_ha_stop(self, _event) -> None:
        await self.async_stop()

    def _on_advert(self, service_info, _change: BluetoothChange) -> None:
        # One task owns reconnects. A cached advert at startup must not
        # start a connect, and a stream of adverts must not start more.
        if self._stopping:
            return
        client = self._live_client()
        if client is not None and self._link_up_at is not None:
            adv_time = self._advert_time(service_info)
            if adv_time > self._link_up_at + STALE_ADV_GRACE_S:
                self._schedule_stale_drop()
            return
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._stopping:
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        delay = self._ready_in()
        _LOGGER.info(
            "Holman %s: connection attempt scheduled in %.0fs",
            self.address, delay,
        )
        self._reconnect_task = self._spawn(self._reconnect(), "reconnect")

    def _schedule_stale_drop(self) -> None:
        if self._stopping:
            return
        task = self._stale_task
        if task is not None and not task.done():
            return
        self._stale_task = self._spawn(self._drop_stale(), "stale")

    async def _drop_stale(self) -> None:
        _LOGGER.warning(
            "Holman %s: advertisement while connected; dropping the stale link",
            self.address,
        )
        async with self._lock:
            if self._stopping:
                return
            client = self._client
        # Leave self._client in place until the close finishes. _live_client
        # hides it once the disconnect task is tracked.
        await self._close_local(client)
        await self._release_proxy_slot()
        if self._stopping:
            return
        # The controller is advertising now. Retry promptly; a failure
        # still takes the normal backoff.
        self._fail_until = min(self._fail_until, time.monotonic())
        self._mark_wanted()
        self._schedule_reconnect()

    async def _reconnect(self) -> None:
        pending = None
        try:
            while not self._stopping:
                delay = self._ready_in()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                async with self._lock:
                    if self._stopping:
                        return
                    if self._live_client() is not None:
                        return
                    self._mark_wanted()
                    pending = None
                    try:
                        pending = await self._open()
                    except AttemptDeferred:
                        pending = None
                    except asyncio.CancelledError:
                        # _open disconnects a client it already holds.
                        pending = None
                        raise
                    except Exception as exc:
                        pending = None
                        if self._stopping:
                            return
                        self._note_failure(exc)
                        continue
                    else:
                        if self._stopping:
                            await self._abandon(pending)
                            pending = None
                            return
                        self._client = pending
                        pending = None
                        self._note_success()
                        return
                await asyncio.sleep(RECONNECT_SOON_S)
        except asyncio.CancelledError:
            if pending is not None and not getattr(pending, "_holman_closed", False):
                await self._close_local(pending)
            raise
        except Exception:
            _LOGGER.exception("Holman %s: reconnect loop crashed", self.address)

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(KEEPALIVE_S)
            if self._stopping:
                return
            async with self._lock:
                if self._stopping:
                    return
                client = self._live_client()
                if client is None:
                    continue
                try:
                    await self._write(client, set_time_frame(dt_util.now()))
                except asyncio.CancelledError:
                    # The write is local to this task until it returns. Close
                    # before releasing the lock so stop cannot time out and
                    # leave the link up.
                    await self._close_local(client)
                    raise
                except Exception as exc:
                    await self._abandon(client)
                    if not self._stopping:
                        self._reconnect_soon(f"keepalive failed: {exc}")

    async def _watchdog_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            if self._stopping:
                return
            self._maybe_raise_repair()
            if self._live_client() is None:
                self._schedule_reconnect()

    async def _cancel_task(self, task) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            # A stuck Bluetooth call must not hold up shutdown. The task is
            # cancelled; if it does not finish, it still sees _stopping.
            await asyncio.wait_for(task, timeout=DISCONNECT_TIMEOUT_S)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except (TimeoutError, Exception):
            _LOGGER.debug("Holman %s: background task ended during stop", self.address)

    async def async_stop(self) -> None:
        """Disconnect and cancel retries. Safe to call from unload and HA stop."""
        if self._stop_wait is not None:
            await self._stop_wait.wait()
            return
        self._stopping = True
        self._stop_wait = asyncio.Event()
        try:
            for attr in ("_unsub_stop", "_unsub_adv"):
                unsub = getattr(self, attr, None)
                if unsub is None:
                    continue
                setattr(self, attr, None)
                try:
                    unsub()
                except Exception as exc:
                    _LOGGER.debug("Holman %s: unsubscribe failed: %s", self.address, exc)
            for task in (
                self._keepalive_task,
                self._watchdog_task,
                self._stale_task,
            ):
                await self._cancel_task(task)
            # These two own an in-flight dial. Cancelling them must not
            # cancel establish_connection; they disconnect whatever it returns
            # before they finish. Wait that out. A short timeout here is how
            # a live proxy slot was being dropped.
            await self._await_cancelled(self._reconnect_task)
            await self._await_cancelled(self._command_task)
            connect_task = self._connect_task
            if connect_task is not None:
                try:
                    await self._disconnect_client(
                        await self._take_connect_result(connect_task)
                    )
                finally:
                    if self._connect_task is connect_task:
                        self._connect_task = None
            client = None
            got_lock = False
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT_S):
                    async with self._lock:
                        client = self._client
                        got_lock = True
            except TimeoutError:
                # Another coroutine still holds the lock and the client.
                # Do not clear self._client out from under it. That coroutine
                # sees _stopping and disconnects before it stores the client.
                _LOGGER.warning(
                    "Holman %s: stop timed out waiting for the BLE lock",
                    self.address,
                )
            if got_lock:
                await self._close_local(client)
            # A background close can outlive _cancel_task's wait. Do not
            # return while that disconnect() is still the only reference.
            await self._drain_disconnects()
            _LOGGER.info("Holman %s: disconnected", self.address)
        finally:
            self._stop_wait.set()

    async def _await_cancelled(self, task) -> None:
        """Cancel task and wait until it has finished, including a shielded dial."""
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            _LOGGER.debug("Holman %s: task ended during stop", self.address)

    def _bind_command(self) -> None:
        self._command_task = asyncio.current_task()

    def _unbind_command(self) -> None:
        if self._command_task is asyncio.current_task():
            self._command_task = None

    async def turn_on(self, rgb=None, level=None):
        if rgb is not None:
            self.rgb = tuple(rgb)
        if level is not None:
            self.level = int(level)
        self._bind_command()
        try:
            async with self._lock:
                await self._send([control_frame(self.rgb, self.level)])
            self.on = True
        finally:
            self._unbind_command()

    async def turn_off(self):
        self._bind_command()
        try:
            async with self._lock:
                await self._send([off_frame()])
            self.on = False
        finally:
            self._unbind_command()
