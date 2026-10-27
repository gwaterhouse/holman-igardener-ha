"""Device recovery tests. No radio, no Home Assistant process."""
from __future__ import annotations

import asyncio
import logging
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
    """connect, write, and disconnect all yield so a cancel can land."""

    def __init__(self, fail_write=False):
        self.is_connected = True
        self.fail_write = fail_write
        self.disconnects = 0
        self.disconnect_cancelled = 0
        self.connects = 0
        self.connect_cancelled = 0
        self.write_cancelled = 0
        self.writes = []
        self._notify = None
        self.disconnected = asyncio.Event()
        self.disconnect_started = asyncio.Event()
        self.connect_started = asyncio.Event()
        self.write_started = asyncio.Event()
        self.release_disconnect = asyncio.Event()
        self.release_disconnect.set()
        self.release_connect = asyncio.Event()
        self.release_connect.set()
        self.release_write = asyncio.Event()
        self.release_write.set()

    async def connect(self, *_args, **_kwargs):
        self.connect_started.set()
        try:
            await asyncio.sleep(0)
            if not self.release_connect.is_set():
                await self.release_connect.wait()
        except asyncio.CancelledError:
            self.connect_cancelled += 1
            raise
        self.connects += 1
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_started.set()
        try:
            await asyncio.sleep(0)
            if not self.release_disconnect.is_set():
                await self.release_disconnect.wait()
        except asyncio.CancelledError:
            self.disconnect_cancelled += 1
            raise
        self.disconnects += 1
        self.is_connected = False
        self.disconnected.set()

    async def start_notify(self, _uuid, callback):
        self._notify = callback
        await asyncio.sleep(0)

    async def write_gatt_char(self, _uuid, data, response=True):
        self.write_started.set()
        try:
            await asyncio.sleep(0)
            if not self.release_write.is_set():
                await self.release_write.wait()
        except asyncio.CancelledError:
            self.write_cancelled += 1
            raise
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
            await asyncio.sleep(0.01)
            assert client.disconnects == 1
            assert dev._client is None
            assert dev._failure_count == 0
            # One keepalive miss starts the outage clock but is not yet an outage.
            assert dev._wanted_since is not None
            assert dev.reachable is True
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
            assert dev.reachable is True
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
            await asyncio.sleep(0.01)
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


def _allow_dial(dev, monkeypatch, establish):
    monkeypatch.setattr(device_mod, "establish_connection", establish)

    async def _allow(self):
        return None

    dev._ensure_can_attempt = _allow.__get__(dev, HolmanDevice)
    dev._best_device = lambda: object()


def test_connect_budget_disconnects_and_does_not_store(monkeypatch):
    """A dial that outlives the budget must not be cancelled or kept."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(device_mod, "CONNECT_BUDGET_S", 0.05)
    cancelled = []
    held = []

    async def _establish(*_args, **_kwargs):
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        client = FakeClient()
        held.append(client)
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            for _ in range(40):
                if held and held[0].disconnects:
                    break
                await asyncio.sleep(0.02)
            assert cancelled == []
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
            assert dev._failure_count == 1
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_stop_after_budget_does_not_cancel_a_live_dial(monkeypatch):
    """Stop during the post-budget wait must not cancel a connected dial."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(device_mod, "CONNECT_BUDGET_S", 0.05)
    cancelled = []
    held = []
    connected = asyncio.Event()
    release = asyncio.Event()

    async def _establish(*_args, **_kwargs):
        client = FakeClient()
        held.append(client)
        connected.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            await asyncio.wait_for(connected.wait(), 2)
            # The client exists and the dial has not returned. Wait out the
            # budget so stop lands in the post-budget wait, not the first shield.
            await asyncio.sleep(0.12)
            assert dev._connect_task is not None
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert cancelled == []
            assert held[0].disconnects == 0
            assert held[0].is_connected is True
            release.set()
            await asyncio.wait_for(stop, 2)
            assert cancelled == []
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
        finally:
            release.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_dial_disconnects_and_does_not_store(monkeypatch):
    """Cancelling the reconnect task must still disconnect the dial's client."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    cancelled = []
    held = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _establish(*_args, **_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        client = FakeClient()
        held.append(client)
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            await asyncio.wait_for(started.wait(), 2)
            stop = asyncio.create_task(dev.async_stop())
            for _ in range(50):
                if dev._stopping:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(stop, 2)
            assert cancelled == []
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
        finally:
            release.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_turn_on_dial_outliving_stop_is_not_kept(monkeypatch):
    """Stop must not return a stored client when turn_on is still dialing."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(device_mod, "DISCONNECT_TIMEOUT_S", 0.05)
    cancelled = []
    held = []
    started = asyncio.Event()

    async def _establish(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        client = FakeClient()
        held.append(client)
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        turn = asyncio.create_task(dev.turn_on())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(dev.async_stop(), 2)
            if not turn.done():
                try:
                    await asyncio.wait_for(turn, 2)
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    turn.result()
                except asyncio.CancelledError:
                    pass
            assert cancelled == []
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
            assert dev.on is False
        finally:
            if not turn.done():
                turn.cancel()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_remove_entry_deletes_the_persistent_repair(monkeypatch):
    _created, deleted = _issues(monkeypatch)

    async def _body():
        from custom_components.holman_igardener import async_remove_entry

        hass = FakeHass()
        entry = SimpleNamespace(data={"address": ADDRESS})
        await async_remove_entry(hass, entry)
        assert len(deleted) == 1
        args, _kwargs = deleted[0]
        assert args[1] == "holman_igardener"
        assert args[2] == "unreachable_c56ad4a14a88"

    asyncio.run(_body())


def test_stop_during_connect_does_not_cancel_it(monkeypatch):
    """establish_connection's connect() is inside the dial task. Stop must not cancel it."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    held = []

    async def _establish(*_args, **_kwargs):
        client = FakeClient()
        client.release_connect.clear()
        held.append(client)
        await client.connect()
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            for _ in range(100):
                if held:
                    break
                await asyncio.sleep(0.01)
            assert held
            await asyncio.wait_for(held[0].connect_started.wait(), 2)
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert held[0].connect_cancelled == 0
            assert held[0].disconnects == 0
            held[0].release_connect.set()
            await asyncio.wait_for(stop, 2)
            assert held[0].connect_cancelled == 0
            assert held[0].disconnects == 1
            assert held[0].disconnect_cancelled == 0
            assert held[0].is_connected is False
            assert dev._client is None
        finally:
            if held:
                held[0].release_connect.set()
                held[0].release_disconnect.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_post_budget_disconnect_is_not_cancelled(monkeypatch):
    """Stop that starts during the post-budget close must let that close finish."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(device_mod, "CONNECT_BUDGET_S", 0.05)
    dial_cancelled = []
    held = []
    dial_release = asyncio.Event()
    dial_entered = asyncio.Event()

    async def _establish(*_args, **_kwargs):
        client = FakeClient()
        client.release_disconnect.clear()
        held.append(client)
        dial_entered.set()
        try:
            await dial_release.wait()
        except asyncio.CancelledError:
            dial_cancelled.append(1)
            raise
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            await asyncio.wait_for(dial_entered.wait(), 2)
            await asyncio.sleep(0.12)
            assert dev._connect_task is not None
            dial_release.set()
            await asyncio.wait_for(held[0].disconnect_started.wait(), 2)
            assert dev._client is None
            assert dev._connect_task is not None
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert dial_cancelled == []
            assert not stop.done()
            assert held[0].disconnect_cancelled == 0
            assert held[0].disconnects == 0
            assert held[0].is_connected is True
            held[0].release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            assert dial_cancelled == []
            assert held[0].disconnect_cancelled == 0
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
            assert dev._connect_task is None
            assert dev._failure_count == 0
        finally:
            dial_release.set()
            if held:
                held[0].release_disconnect.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_turn_on_post_budget_disconnect_is_not_cancelled(monkeypatch):
    """turn_on in the post-budget close must not be left connected."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(device_mod, "CONNECT_BUDGET_S", 0.05)
    dial_cancelled = []
    held = []
    dial_release = asyncio.Event()
    dial_entered = asyncio.Event()

    async def _establish(*_args, **_kwargs):
        client = FakeClient()
        client.release_disconnect.clear()
        held.append(client)
        dial_entered.set()
        try:
            await dial_release.wait()
        except asyncio.CancelledError:
            dial_cancelled.append(1)
            raise
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        turn = asyncio.create_task(dev.turn_on())
        try:
            await asyncio.wait_for(dial_entered.wait(), 2)
            await asyncio.sleep(0.12)
            dial_release.set()
            await asyncio.wait_for(held[0].disconnect_started.wait(), 2)
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert dial_cancelled == []
            assert not stop.done()
            assert held[0].disconnect_cancelled == 0
            assert held[0].is_connected is True
            held[0].release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            if not turn.done():
                try:
                    await asyncio.wait_for(turn, 2)
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    turn.result()
                except asyncio.CancelledError:
                    pass
            assert dial_cancelled == []
            assert held[0].disconnect_cancelled == 0
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
            assert dev.on is False
            assert dev._failure_count == 0
        finally:
            dial_release.set()
            if held:
                held[0].release_disconnect.set()
            if not turn.done():
                turn.cancel()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_stale_disconnect_is_not_cancelled():
    """Stale-drop clears nothing until its disconnect() has finished."""

    async def _body():
        _hass, dev = _device()
        client = FakeClient()
        client.release_disconnect.clear()
        dev._client = client
        dev._link_up_at = time.monotonic() - 60
        dev._startup_not_before = time.monotonic() + 1000
        try:
            dev._on_advert(SimpleNamespace(time=time.monotonic()), None)
            await asyncio.wait_for(client.disconnect_started.wait(), 2)
            assert dev._client is client
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert dev._client is client
            assert client.disconnect_cancelled == 0
            assert client.disconnects == 0
            assert client.is_connected is True
            client.release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            assert client.disconnect_cancelled == 0
            assert client.disconnects == 1
            assert client.is_connected is False
            assert dev._client is None
        finally:
            client.release_disconnect.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_handshake_disconnect_is_not_cancelled(monkeypatch):
    """A cancel that lands on the handshake still finishes the cleanup close."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 0)
    held = []

    async def _establish(*_args, **_kwargs):
        client = FakeClient()
        client.release_write.clear()
        client.release_disconnect.clear()
        held.append(client)
        return client

    async def _body():
        _hass, dev = _device()
        _allow_dial(dev, monkeypatch, _establish)
        try:
            dev.async_start()
            for _ in range(100):
                if held:
                    break
                await asyncio.sleep(0.01)
            assert held
            await asyncio.wait_for(held[0].write_started.wait(), 2)
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.wait_for(held[0].disconnect_started.wait(), 2)
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert held[0].disconnect_cancelled == 0
            assert held[0].is_connected is True
            assert dev._client is None
            held[0].release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            assert held[0].disconnect_cancelled == 0
            assert held[0].disconnects == 1
            assert held[0].is_connected is False
            assert dev._client is None
            assert dev._failure_count == 0
        finally:
            if held:
                held[0].release_write.set()
                held[0].release_disconnect.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_keepalive_disconnect_is_not_cancelled(monkeypatch):
    """Keepalive's cleanup close is not aborted when stop cancels that task."""
    monkeypatch.setattr(device_mod, "KEEPALIVE_S", 0.01)

    async def _body():
        _hass, dev = _device()
        client = FakeClient(fail_write=True)
        client.release_disconnect.clear()
        dev._client = client
        dev._link_up_at = time.monotonic()
        dev._startup_not_before = time.monotonic() + 1000
        try:
            dev.async_start()
            await asyncio.wait_for(client.disconnect_started.wait(), 2)
            assert dev._client is client
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert dev._client is client
            assert client.disconnect_cancelled == 0
            assert client.is_connected is True
            client.release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            assert client.disconnect_cancelled == 0
            assert client.disconnects == 1
            assert client.is_connected is False
            assert dev._client is None
        finally:
            client.release_disconnect.set()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_stop_during_command_write_disconnect_is_not_cancelled():
    """A command write cancelled by stop still disconnects the stored client."""

    async def _body():
        _hass, dev = _device()
        client = FakeClient()
        client.release_write.clear()
        client.release_disconnect.clear()
        dev._client = client
        dev._link_up_at = time.monotonic()
        turn = asyncio.create_task(dev.turn_on())
        try:
            await asyncio.wait_for(client.write_started.wait(), 2)
            stop = asyncio.create_task(dev.async_stop())
            await asyncio.wait_for(client.disconnect_started.wait(), 2)
            await asyncio.sleep(0.05)
            assert not stop.done()
            assert client.disconnect_cancelled == 0
            assert client.is_connected is True
            assert dev._client is client
            client.release_disconnect.set()
            await asyncio.wait_for(stop, 2)
            assert client.disconnect_cancelled == 0
            assert client.disconnects == 1
            assert client.is_connected is False
            assert dev._client is None
            assert dev.on is False
        finally:
            client.release_write.set()
            client.release_disconnect.set()
            if not turn.done():
                turn.cancel()
            if not dev._stopping:
                await dev.async_stop()

    asyncio.run(_body())


def test_startup_before_the_first_attempt_is_not_unavailable(monkeypatch, caplog):
    """The startup delay must not count, even if the window is zero."""
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 1000)
    monkeypatch.setattr(device_mod, "UNAVAILABLE_AFTER_S", 0)
    monkeypatch.setattr(device_mod, "WATCHDOG_INTERVAL_S", 0.02)
    caplog.set_level(logging.WARNING)

    async def _body():
        _hass, dev = _device()
        try:
            dev.async_start()
            await asyncio.sleep(0.12)
            assert dev._wanted_since is None
            assert dev.reachable is True
            assert device_mod.establish_connection.calls == []
        finally:
            await dev.async_stop()

    asyncio.run(_body())
    assert "marking the light unavailable" not in caplog.text


def test_short_outage_stays_reachable(caplog):
    caplog.set_level(logging.WARNING)

    async def _body():
        _hass, dev = _device()
        try:
            dev._mark_wanted()
            dev._update_availability()
            assert dev.reachable is True
            dev._reconnect_soon("link dropped")
            assert dev.reachable is True
        finally:
            await dev.async_stop()

    asyncio.run(_body())
    assert "marking the light unavailable" not in caplog.text


def test_first_connect_does_not_log_restored(caplog):
    caplog.set_level(logging.INFO)

    async def _body():
        _hass, dev = _device()
        try:
            dev._note_success()
            assert dev.reachable is True
        finally:
            await dev.async_stop()

    asyncio.run(_body())
    assert "link up" in caplog.text
    assert "communication restored" not in caplog.text


def test_sustained_outage_marks_unavailable_and_recovery_clears_it(caplog):
    caplog.set_level(logging.INFO)

    async def _body():
        hass, dev = _device()
        seen = []
        dev.add_listener(lambda: seen.append(dev.reachable))
        try:
            dev._note_failure(RuntimeError("no advertisement"))
            assert dev.reachable is True
            assert seen == []
            dev._wanted_since = time.monotonic() - device_mod.UNAVAILABLE_AFTER_S - 1
            dev._update_availability()
            assert dev.reachable is False
            assert seen == [False]
            # Retries keep running after the light is marked unavailable.
            assert any(not task.done() for task in hass.tasks)
            dev._update_availability()
            assert seen == [False]
            dev._client = FakeClient()
            dev._note_success()
            assert dev.reachable is True
            assert dev._wanted_since is None
            assert seen == [False, True]
        finally:
            await dev.async_stop()

    asyncio.run(_body())
    assert "marking the light unavailable" in caplog.text
    assert "communication restored; marking the light available" in caplog.text
    # The transition is logged once, not on every later check.
    assert caplog.text.count("marking the light unavailable") == 1
    assert caplog.text.count("communication restored") == 1


def test_watchdog_marks_unavailable_after_the_window(monkeypatch, caplog):
    monkeypatch.setattr(device_mod, "STARTUP_DELAY_S", 1000)
    monkeypatch.setattr(device_mod, "UNAVAILABLE_AFTER_S", 0.05)
    monkeypatch.setattr(device_mod, "WATCHDOG_INTERVAL_S", 0.02)
    caplog.set_level(logging.INFO)

    async def _body():
        _hass, dev = _device()
        dev._wanted_since = time.monotonic()
        try:
            dev.async_start()
            for _ in range(40):
                if not dev.reachable:
                    break
                await asyncio.sleep(0.02)
            assert dev.reachable is False
            dev._note_success()
            assert dev.reachable is True
            assert dev._wanted_since is None
        finally:
            await dev.async_stop()

    asyncio.run(_body())
    assert "marking the light unavailable" in caplog.text
    assert "communication restored; marking the light available" in caplog.text


def test_live_link_is_reachable_even_if_the_clock_is_old():
    async def _body():
        _hass, dev = _device()
        try:
            dev._client = FakeClient()
            dev._wanted_since = time.monotonic() - device_mod.UNAVAILABLE_AFTER_S - 5
            dev._reachable = False
            dev._update_availability()
            assert dev.reachable is True
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_light_and_connectivity_follow_the_same_latch():
    async def _body():
        _hass, dev = _device()
        from custom_components.holman_igardener.binary_sensor import HolmanConnectivity
        from custom_components.holman_igardener.light import HolmanLight

        light = HolmanLight(dev, ADDRESS)
        sensor = HolmanConnectivity(dev, ADDRESS)
        await light.async_added_to_hass()
        await sensor.async_added_to_hass()
        try:
            assert light.unique_id == "c56ad4a14a88"
            assert light.name == "Holman iGardener RGB"
            assert sensor.unique_id == "c56ad4a14a88_connectivity"
            assert sensor.name == "Holman iGardener RGB Connectivity"
            assert sensor.device_class == "connectivity"
            assert sensor.entity_category == "diagnostic"
            assert sensor.device_info["identifiers"] == light.device_info["identifiers"]
            assert sensor.device_info["identifiers"] == {
                ("holman_igardener", "c56ad4a14a88")
            }
            assert light.available is True
            assert sensor.is_on is True
            assert sensor.available is True
            dev._wanted_since = time.monotonic() - device_mod.UNAVAILABLE_AFTER_S - 1
            dev._update_availability()
            assert light.available is False
            assert sensor.is_on is False
            assert sensor.available is True
            assert light.writes == 1
            assert sensor.writes == 1
            light._unsubs[0]()
            dev._client = FakeClient()
            dev._note_success()
            assert light.available is True
            assert sensor.is_on is True
            assert light.writes == 1
            assert sensor.writes == 2
        finally:
            await dev.async_stop()

    asyncio.run(_body())


def test_platforms_include_the_connectivity_sensor():
    from custom_components.holman_igardener import PLATFORMS

    assert PLATFORMS == ["light", "binary_sensor"]
