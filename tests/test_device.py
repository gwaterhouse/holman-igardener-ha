"""Device recovery tests. No radio, no Home Assistant process."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import custom_components.holman_igardener.device as device_mod
from custom_components.holman_igardener.device import HolmanDevice

ADDRESS = "C5:6A:D4:A1:4A:88"


class FakeBus:
    def __init__(self):
        self._once = {}

    def async_listen_once(self, event_type, callback):
        self._once.setdefault(event_type, []).append(callback)

        def _unsub(cb=callback, kind=event_type):
            listeners = self._once.get(kind, [])
            if cb in listeners:
                listeners.remove(cb)

        return _unsub

    async def async_fire(self, event_type):
        for callback in list(self._once.get(event_type, [])):
            result = callback(None)
            if asyncio.iscoroutine(result):
                await result


class FakeHass:
    def __init__(self):
        self.bus = FakeBus()
        self.data = {}
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def async_create_background_task(self, coro, name=None):
        return self.async_create_task(coro)


class FakeClient:
    def __init__(self, fail_write=False):
        self.is_connected = True
        self.fail_write = fail_write
        self.disconnects = 0
        self.writes = []
        self._notify = None
        self.disconnected = asyncio.Event()

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        self.disconnected.set()

    async def start_notify(self, _uuid, callback):
        self._notify = callback

    async def write_gatt_char(self, _uuid, data, response=True):
        payload = bytes(data)
        self.writes.append(payload)
        if self.fail_write:
            raise RuntimeError("write failed")
        if self._notify is not None and len(payload) >= 2 and payload[1] == 0x1F:
            self._notify(None, bytearray.fromhex("001f01" + "00" * 17))


def _issues(monkeypatch):
    created, deleted = [], []

    def _create(*args, **kwargs):
        created.append((args, kwargs))

    def _delete(*args, **kwargs):
        deleted.append((args, kwargs))

    monkeypatch.setattr(device_mod.ir, "async_create_issue", _create)
    monkeypatch.setattr(device_mod.ir, "async_delete_issue", _delete)
    return created, deleted


@pytest.fixture(autouse=True)
def _clear_connector_calls():
    device_mod.establish_connection.calls.clear()
    device_mod.close_stale_connections_by_address.calls.clear()
    yield


def _device():
    hass = FakeHass()
    return hass, HolmanDevice(hass, ADDRESS)


def test_adverts_do_not_stack_reconnects():
    async def _body():
        hass, dev = _device()
        try:
            dev.async_start()
            await asyncio.sleep(0)
            started = len(hass.tasks)
            for _ in range(10):
                dev._on_advert(SimpleNamespace(time=time.monotonic()), None)
            await asyncio.sleep(0)
            assert len(hass.tasks) == started
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_startup_does_not_connect():
    async def _body():
        _hass, dev = _device()
        try:
            dev.async_start()
            await asyncio.sleep(0.05)
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()
        assert device_mod.establish_connection.calls == []
        assert dev._client is None

    asyncio.run(_body())


def test_stop_disconnects_a_held_client_once():
    async def _body():
        _hass, dev = _device()
        client = FakeClient()
        dev._client = client
        await asyncio.gather(dev.async_stop(), dev.async_stop())
        assert client.disconnects == 1
        assert dev._client is None

    asyncio.run(_body())


def test_home_assistant_stop_disconnects_and_unload_is_idempotent():
    async def _body():
        hass, dev = _device()
        client = FakeClient()
        dev._client = client
        try:
            dev.async_start()
            await hass.bus.async_fire(device_mod.EVENT_HOMEASSISTANT_STOP)
            assert client.disconnects == 1
            assert dev._stopping is True
            await dev.async_stop()
            assert client.disconnects == 1
            assert device_mod.establish_connection.calls == []
        finally:
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_keepalive_failure_disconnects_and_backs_off(monkeypatch):
    monkeypatch.setattr(device_mod, "KEEPALIVE_S", 0.01)

    async def _body():
        hass, dev = _device()
        client = FakeClient(fail_write=True)
        dev._client = client
        dev._link_up_at = time.monotonic()
        try:
            dev.async_start()
            started = len(hass.tasks)
            await asyncio.wait_for(client.disconnected.wait(), 2)
            assert client.disconnects == 1
            assert dev._client is None
            assert dev._failure_count == 0
            remaining = dev._fail_until - time.monotonic()
            assert 0 < remaining <= device_mod.RECONNECT_SOON_S
            assert device_mod.close_stale_connections_by_address.calls
            assert device_mod.establish_connection.calls == []
            await asyncio.sleep(0.05)
            # The in-flight reconnect task absorbs the retry. No second task.
            assert len(hass.tasks) == started
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_connect_failure_uses_growing_backoff():
    async def _body():
        hass, dev = _device()
        try:
            dev._note_failure(RuntimeError("gatt 133"))
            assert dev._failure_count == 1
            assert 29 <= dev._fail_until - time.monotonic() <= 30
            first_tasks = len(hass.tasks)
            dev._note_failure(RuntimeError("gatt 133"))
            assert dev._failure_count == 2
            assert 59 <= dev._fail_until - time.monotonic() <= 60
            assert len(hass.tasks) == first_tasks == 1
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_command_during_backoff_is_not_another_failure():
    async def _body():
        _hass, dev = _device()
        try:
            dev._startup_not_before = 0
            dev._fail_until = time.monotonic() + 50
            with pytest.raises(RuntimeError, match="not connected"):
                await dev.turn_on()
            assert dev.on is False
            assert dev._failure_count == 0
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_repair_is_raised_once_and_cleared_on_recovery(monkeypatch):
    created, deleted = _issues(monkeypatch)

    async def _body():
        _hass, dev = _device()
        try:
            dev._maybe_raise_repair()
            assert created == []
            dev._wanted_since = time.monotonic() - device_mod.UNREACHABLE_REPAIR_S - 1
            dev._maybe_raise_repair()
            dev._maybe_raise_repair()
            assert len(created) == 1
            _args, kwargs = created[0]
            assert kwargs["translation_key"] == "unreachable"
            assert kwargs["is_fixable"] is False
            assert kwargs["is_persistent"] is True
            assert kwargs["severity"] == "error"
            assert kwargs["translation_placeholders"] == {"address": ADDRESS}
            assert dev._repair_open is True
            dev._note_success()
            assert dev._repair_open is False
            assert dev._wanted_since is None
            assert dev._failure_count == 0
            assert len(deleted) == 1
            # A restart can hold a persisted issue this process did not flag.
            dev._clear_repair()
            assert len(deleted) == 2
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_watchdog_raises_the_repair(monkeypatch):
    created, _deleted = _issues(monkeypatch)
    monkeypatch.setattr(device_mod, "WATCHDOG_INTERVAL_S", 0.02)
    monkeypatch.setattr(device_mod, "UNREACHABLE_REPAIR_S", 0)

    async def _body():
        _hass, dev = _device()
        dev._wanted_since = time.monotonic() - 5
        try:
            dev.async_start()
            for _ in range(50):
                if created:
                    break
                await asyncio.sleep(0.02)
            assert len(created) == 1
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_stale_advert_drops_the_link_once():
    async def _body():
        hass, dev = _device()
        client = FakeClient()
        dev._client = client
        dev._link_up_at = time.monotonic() - 60
        dev._startup_not_before = time.monotonic() + 1000
        try:
            now = time.monotonic()
            dev._on_advert(SimpleNamespace(time=now), None)
            dev._on_advert(SimpleNamespace(time=now), None)
            await asyncio.wait_for(client.disconnected.wait(), 2)
            assert client.disconnects == 1
            assert dev._client is None
            assert device_mod.establish_connection.calls == []
            assert len(hass.tasks) == 2  # the drop, then one reconnect
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_advert_just_after_connect_is_not_a_stale_link():
    async def _body():
        hass, dev = _device()
        client = FakeClient()
        dev._client = client
        dev._link_up_at = time.monotonic()
        try:
            dev._on_advert(SimpleNamespace(time=time.monotonic()), None)
            dev._on_advert(SimpleNamespace(time=dev._link_up_at - 5), None)
            await asyncio.sleep(0.05)
            assert client.disconnects == 0
            assert hass.tasks == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_no_advertisement_counts_as_a_failed_attempt(monkeypatch):
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)

    async def _body():
        _hass, dev = _device()
        try:
            dev.async_start()
            for _ in range(50):
                if dev._failure_count:
                    break
                await asyncio.sleep(0.02)
            assert dev._failure_count == 1
            assert dev._fail_until > time.monotonic()
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_one_connection_after_the_startup_delay(monkeypatch):
    calls = []

    async def _establish(*_args, **_kwargs):
        calls.append(1)
        return FakeClient()

    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0.05)
    monkeypatch.setattr(device_mod, "establish_connection", _establish)
    _created, deleted = _issues(monkeypatch)

    async def _allow(self):
        return None

    async def _body():
        hass, dev = _device()
        dev._ensure_can_attempt = _allow.__get__(dev, HolmanDevice)
        dev._best_device = lambda: object()
        try:
            dev.async_start()
            for _ in range(40):
                if dev._client is not None:
                    break
                await asyncio.sleep(0.05)
            assert dev._client is not None
            assert calls == [1]
            assert deleted  # successful link clears a persisted repair
            for _ in range(5):
                dev._on_advert(SimpleNamespace(time=time.monotonic()), None)
            await asyncio.sleep(0.05)
            assert calls == [1]
            assert len([task for task in hass.tasks if not task.done()]) >= 1
        finally:
            await dev.async_stop()

    asyncio.run(_body())
