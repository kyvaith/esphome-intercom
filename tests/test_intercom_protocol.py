#!/usr/bin/env python3
"""Golden fixtures for the intercom PBX-lite wire protocol.

The repository intentionally has Python and C++ protocol implementations.
These tests pin the Python implementation to canonical byte fixtures; the same
hex strings are documented in docs/INTERCOM_PROTOCOL.md for the ESP side.
"""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.intercom_native"
PKG_DIR = ROOT / "custom_components" / "intercom_native"


def _load_intercom_module(name: str):
    """Load a module without importing HA-heavy package __init__.py."""
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg

    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


const = _load_intercom_module("const")
protocol = _load_intercom_module("protocol")
fsm = _load_intercom_module("fsm")
audio_ws = _load_intercom_module("audio_ws")


def _install_websocket_api_fakes() -> None:
    if "homeassistant.core" not in sys.modules:
        ha = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        ws_api = types.ModuleType("homeassistant.components.websocket_api")
        http = types.ModuleType("homeassistant.components.http")
        core = types.ModuleType("homeassistant.core")

        def identity_decorator(*_args, **_kwargs):
            def _wrap(fn):
                return fn
            return _wrap

        ws_api.ActiveConnection = object
        ws_api.websocket_command = identity_decorator
        ws_api.async_response = lambda fn: fn
        ws_api.async_register_command = lambda *_args, **_kwargs: None
        http.HomeAssistantView = type("HomeAssistantView", (), {})
        core.HomeAssistant = type("HomeAssistant", (), {})
        core.callback = lambda fn: fn

        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.components"] = components
        sys.modules["homeassistant.components.websocket_api"] = ws_api
        sys.modules["homeassistant.components.http"] = http
        sys.modules["homeassistant.core"] = core

    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        web = types.ModuleType("aiohttp.web")
        aiohttp.WSMsgType = types.SimpleNamespace(BINARY=1, TEXT=2)
        web.WebSocketResponse = type("WebSocketResponse", (), {})
        web.Request = type("Request", (), {})
        web.StreamResponse = type("StreamResponse", (), {})
        aiohttp.web = web
        sys.modules["aiohttp"] = aiohttp
        sys.modules["aiohttp.web"] = web

    if "voluptuous" not in sys.modules:
        vol = types.ModuleType("voluptuous")
        vol.Required = lambda key: key
        sys.modules["voluptuous"] = vol

    helpers_name = f"{PKG_NAME}.transport_helpers"
    if helpers_name not in sys.modules:
        helpers = types.ModuleType(helpers_name)

        class TransportCallbacks:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        async def cancel_task(task, timeout=1.0):
            if task is None:
                return
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        async def stop_transport(transport, send_signaling=True):
            if transport is None:
                return
            if send_signaling:
                await transport.stop_stream()
            await transport.disconnect()

        helpers.TransportCallbacks = TransportCallbacks
        helpers.build_transport = lambda *_args, **_kwargs: None
        helpers.cancel_task = cancel_task
        helpers.configured_transport_type = lambda *_args, **_kwargs: "tcp"
        helpers.stop_transport = stop_transport
        sys.modules[helpers_name] = helpers

    transport_base_name = f"{PKG_NAME}.transport_base"
    if transport_base_name not in sys.modules:
        transport_base = types.ModuleType(transport_base_name)
        transport_base.IntercomTransport = object
        sys.modules[transport_base_name] = transport_base


_install_websocket_api_fakes()
websocket_api = _load_intercom_module("websocket_api")


class FakeBus:
    def __init__(self):
        self.events = []

    def async_fire(self, event_type, payload):
        self.events.append((event_type, dict(payload)))


class FakeConfig:
    location_name = "Test HA"


class FakeHass:
    def __init__(self):
        self.data = {const.DOMAIN: {}}
        self.bus = FakeBus()
        self.config = FakeConfig()
        self.states = {}
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class FakeTransport:
    transport_name = "fake"
    _instance_id = 1

    def __init__(self, result="streaming"):
        self.result = result
        self.callbacks = None
        self.stop_count = 0
        self.disconnect_count = 0
        self.audio_sent = []

    def set_callbacks(self, callbacks):
        self.callbacks = callbacks

    def set_call_context(self, *_args):
        pass

    async def connect(self):
        return True

    async def start_stream(self, **_kwargs):
        return self.result

    async def stop_stream(self):
        self.stop_count += 1
        return True

    async def disconnect(self):
        self.disconnect_count += 1
        return True

    async def send_audio(self, data):
        self.audio_sent.append(data)
        return True


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


class IntercomProtocolFixturesTest(unittest.TestCase):
    def test_binary_audio_frame_round_trip(self) -> None:
        payload = bytes((i & 0xFF) for i in range(audio_ws.AUDIO_CHUNK_BYTES))
        frame = audio_ws.encode_audio_frame(payload)
        self.assertEqual(frame[0], audio_ws.AUDIO_FRAME_TYPE)
        self.assertEqual(audio_ws.decode_audio_frame(frame), payload)

    def test_binary_audio_frame_rejects_wrong_shape(self) -> None:
        with self.assertRaises(ValueError):
            audio_ws.encode_audio_frame(b"short")
        with self.assertRaises(ValueError):
            audio_ws.decode_audio_frame(bytes((audio_ws.AUDIO_FRAME_TYPE + 1,)) + (b"\0" * audio_ws.AUDIO_CHUNK_BYTES))

    def test_ping_frame_fixture(self) -> None:
        body = protocol.build_call_id_only_body("")
        self.assertEqual(protocol.build_frame(const.MSG_PING, body).hex(), "04010000")

    def test_start_frame_fixture(self) -> None:
        body = protocol.build_start_body("A<->B", "A", "Panel A", "B", "Panel B")
        self.assertEqual(
            protocol.build_frame(const.MSG_START, body).hex(),
            "021a0005413c2d3e4201410750616e656c204101420750616e656c2042",
        )
        self.assertEqual(
            protocol.parse_start_body(body),
            {
                "call_id": "A<->B",
                "caller_route": "A",
                "caller_name": "Panel A",
                "dest_route": "B",
                "dest_name": "Panel B",
            },
        )

    def test_decline_reason_fixture(self) -> None:
        body = protocol.build_decline_body("A<->B", "DND")
        self.assertEqual(
            protocol.build_frame(const.MSG_DECLINE, body).hex(),
            "090a0005413c2d3e4203444e44",
        )
        self.assertEqual(
            protocol.parse_decline_body(body),
            {"call_id": "A<->B", "reason": "DND"},
        )

    def test_error_detail_fixture(self) -> None:
        body = protocol.build_error_body("A<->B", 1, "busy")
        self.assertEqual(
            protocol.build_frame(const.MSG_ERROR, body).hex(),
            "060c0005413c2d3e42010462757379",
        )
        self.assertEqual(
            protocol.parse_error_body(body),
            {"call_id": "A<->B", "error_code": 1, "detail": "busy"},
        )

    def test_free_form_utf8_reason_round_trips(self) -> None:
        reason = "non rompere i coglioni"
        body = protocol.build_decline_body("Spotpear<->WS3", reason)
        self.assertEqual(protocol.parse_decline_body(body)["reason"], reason)

    def test_truncated_body_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            protocol.parse_start_body(b"\x05A")
        with self.assertRaises(ValueError):
            protocol.parse_decline_body(b"\x05A<->B\x10short")
        with self.assertRaises(ValueError):
            protocol.parse_error_body(b"\x05A<->B")

    def test_limits_are_enforced(self) -> None:
        with self.assertRaises(ValueError):
            protocol.build_call_id_only_body("x" * (const.MAX_CALL_ID_LEN + 1))
        with self.assertRaises(ValueError):
            protocol.build_decline_body("A<->B", "x" * (const.MAX_REASON_LEN + 1))

    def test_decline_semantics_match_fsm_contract(self) -> None:
        self.assertEqual(fsm.terminal_state_for_decline(""), "idle")
        self.assertEqual(
            fsm.terminal_reason_for_decline(""),
            fsm.TerminalReason.REMOTE_HANGUP.value,
        )
        self.assertEqual(fsm.terminal_state_for_decline("DND"), "declined")
        self.assertEqual(fsm.terminal_reason_for_decline("DND"), "DND")

    def test_bridge_reason_localization(self) -> None:
        self.assertEqual(
            fsm.localize_bridge_reason("source", "local_hangup", "source"),
            "local_hangup",
        )
        self.assertEqual(
            fsm.localize_bridge_reason("dest", "local_hangup", "source"),
            "remote_hangup",
        )
        self.assertEqual(
            fsm.localize_bridge_reason("dest", "busy", "source"),
            "busy",
        )


class IntercomWebSocketSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        websocket_api._sessions.clear()
        websocket_api._bridges.clear()
        self.hass = FakeHass()

    async def asyncTearDown(self) -> None:
        for session in list(websocket_api._sessions.values()):
            await session.stop(send_signaling=False)
        websocket_api._sessions.clear()
        websocket_api._bridges.clear()
        for task in self.hass.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.hass.tasks, return_exceptions=True)

    async def _started_session(self, device_id="device-a", transport=None):
        transport = transport or FakeTransport()
        session = websocket_api.IntercomSession(
            hass=self.hass,
            device_id=device_id,
            host="192.0.2.10",
            transport=transport,
            audio_mode="full_duplex",
        )
        self.assertEqual(await session.start(), "streaming")
        websocket_api._sessions[device_id] = session
        return session, transport

    async def test_audio_ws_close_stops_bound_session_and_sends_hangup(self) -> None:
        session, transport = await self._started_session()
        ws = FakeWebSocket()
        session.bind_audio_ws(ws)

        await session.unbind_audio_ws(ws)

        self.assertNotIn("device-a", websocket_api._sessions)
        self.assertEqual(transport.stop_count, 1)
        self.assertEqual(transport.disconnect_count, 1)

    async def test_audio_ws_watchdog_hangs_up_idle_browser_audio(self) -> None:
        old_interval = websocket_api.WS_AUDIO_WATCHDOG_INTERVAL
        old_timeout = websocket_api.WS_AUDIO_IDLE_TIMEOUT
        websocket_api.WS_AUDIO_WATCHDOG_INTERVAL = 0.01
        websocket_api.WS_AUDIO_IDLE_TIMEOUT = 0.02
        try:
            session, transport = await self._started_session()
            ws = FakeWebSocket()
            session.bind_audio_ws(ws)

            await asyncio.sleep(0.08)

            self.assertNotIn("device-a", websocket_api._sessions)
            self.assertEqual(transport.stop_count, 1)
            self.assertEqual(transport.disconnect_count, 1)
            self.assertTrue(ws.closed)
        finally:
            websocket_api.WS_AUDIO_WATCHDOG_INTERVAL = old_interval
            websocket_api.WS_AUDIO_IDLE_TIMEOUT = old_timeout

    async def test_issue_53_socket_kill_clears_session_before_second_call(self) -> None:
        first, first_transport = await self._started_session("device-a")
        first_ws = FakeWebSocket()
        first.bind_audio_ws(first_ws)
        first.queue_audio(b"\1" * audio_ws.AUDIO_CHUNK_BYTES)
        await asyncio.sleep(0)

        await first.unbind_audio_ws(first_ws)

        self.assertNotIn("device-a", websocket_api._sessions)
        self.assertEqual(first_transport.stop_count, 1)
        self.assertTrue(first._tx_task is None or first._tx_task.done())

        second_transport = FakeTransport()
        second, _ = await self._started_session("device-a", second_transport)
        second_ws = FakeWebSocket()
        second.bind_audio_ws(second_ws)

        self.assertIs(websocket_api._sessions.get("device-a"), second)
        self.assertEqual(len(websocket_api._sessions), 1)
        self.assertIsNot(first, second)
        self.assertTrue(second._tx_task is not None and not second._tx_task.done())


if __name__ == "__main__":
    unittest.main()
