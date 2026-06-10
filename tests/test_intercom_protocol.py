#!/usr/bin/env python3
"""Golden fixtures for the intercom PBX-lite wire protocol.

The repository intentionally has Python and C++ protocol implementations.
These tests pin the Python implementation to canonical byte fixtures; the same
hex strings are documented in docs/INTERCOM_PROTOCOL.md for the ESP side.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.intercom_native"
PKG_DIR = ROOT / "custom_components" / "intercom_native"


def _load_intercom_module(name: str):
    """Load a module without importing HA-heavy package __init__.py."""
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


if __name__ == "__main__":
    unittest.main()
