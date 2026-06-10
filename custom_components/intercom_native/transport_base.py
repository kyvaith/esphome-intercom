"""Abstract base for intercom_native transports.

Implementations: tcp_client.IntercomTcpClient (framed),
udp_client.IntercomUdpClient (raw PCM + framed control).

Coroutines run on the HA event loop; callbacks fire from the receive
task and must be scheduled via loop.call_soon_threadsafe() if off-loop.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .audio_format import AudioFormat, LEGACY_AUDIO_FORMAT
from .const import (
    MSG_AUDIO,
    MSG_START,
    MSG_HANGUP,
    MSG_PING,
    MSG_PONG,
    MSG_ERROR,
    MSG_RING,
    MSG_ANSWER,
    MSG_DECLINE,
)
from . import protocol

_LOGGER = logging.getLogger(__name__)

MAX_PAYLOAD_SIZE = protocol.MAX_PAYLOAD_SIZE


class IntercomTransport(ABC):
    """Audio + control between HA and one ESP. Subclasses own framing;
    _handle_message() centralises FSM dispatch so TCP and UDP can't drift."""

    _instance_counter = 0

    def __init__(
        self,
        host: str,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        on_ringing: Optional[Callable[[], None]] = None,
        on_answered: Optional[Callable[[], None]] = None,
        on_stop_received: Optional[Callable[[], None]] = None,
        on_decline_received: Optional[Callable[[str], None]] = None,
        on_error_received: Optional[Callable[[int, str], None]] = None,
    ):
        IntercomTransport._instance_counter += 1
        self._instance_id = IntercomTransport._instance_counter

        self.host = host
        self._on_audio = on_audio
        self._on_disconnected = on_disconnected
        self._on_ringing = on_ringing
        self._on_answered = on_answered
        self._on_stop_received = on_stop_received
        self._on_decline_received = on_decline_received
        self._on_error_received = on_error_received

        self._connected = False
        self._streaming = False
        self._ringing = False
        self._state_changed = asyncio.Event()

        # Outbound ANSWER/DECLINE UDP retry window (PBX-lite).
        self._awaiting_answer_ack = False

        self._audio_sent = 0
        self._audio_recv = 0
        # Per-reason drop counter; rate-limited log at 1 + every 100th.
        self._audio_send_dropped: dict[str, int] = {}
        self._disconnect_notified = False

        # Latest inbound MSG_START caller_name; read by the session layer.
        self.last_caller_name: str = ""
        # Exact PBX-lite call id for the active leg. This must be echoed in
        # terminal/control replies; reconstructing it from names is not safe
        # for bridged or cross-transport calls.
        self._call_id: str = ""
        self.peer_tx_formats: list[AudioFormat] = [LEGACY_AUDIO_FORMAT]
        self.peer_rx_formats: list[AudioFormat] = [LEGACY_AUDIO_FORMAT]
        self.local_tx_formats: list[AudioFormat] = [LEGACY_AUDIO_FORMAT]
        self.local_rx_formats: list[AudioFormat] = [LEGACY_AUDIO_FORMAT]
        self.caller_to_dest_format: AudioFormat = LEGACY_AUDIO_FORMAT
        self.dest_to_caller_format: AudioFormat = LEGACY_AUDIO_FORMAT

    # === Public API ===

    def set_callbacks(self, callbacks: "TransportCallbacks") -> None:
        """Wire callbacks onto a transport built before the session existed
        (TCP listener accept path)."""
        self._on_audio = callbacks.on_audio
        self._on_disconnected = callbacks.on_disconnected
        self._on_ringing = callbacks.on_ringing
        self._on_answered = callbacks.on_answered
        self._on_stop_received = callbacks.on_stop_received
        self._on_decline_received = callbacks.on_decline_received
        self._on_error_received = callbacks.on_error_received

    def set_call_context(self, call_id: str, caller_name: str = "") -> None:
        """Seed the active PBX-lite call context from a parsed START.

        HA may act as a bridge leg without receiving the START through this
        transport instance (UDP unsolicited routing parses it in the shared
        socket manager first). The session layer then seeds the exact call_id
        here so replies match the caller FSM stale-cid gate.
        """
        self._call_id = call_id or ""
        if caller_name:
            self.last_caller_name = caller_name

    def set_selected_audio_formats(self, caller_to_dest: AudioFormat, dest_to_caller: AudioFormat) -> None:
        self.caller_to_dest_format = caller_to_dest
        self.dest_to_caller_format = dest_to_caller

    def set_local_audio_formats(self, tx_formats: list[AudioFormat], rx_formats: list[AudioFormat]) -> None:
        self.local_tx_formats = tx_formats
        self.local_rx_formats = rx_formats

    def _gate_call_id(self, call_id: str, msg_name: str) -> bool:
        """Return True when a control frame belongs to a stale call."""
        if not call_id:
            return False
        if not self._call_id:
            self._call_id = call_id
            return False
        if call_id == self._call_id:
            return False
        _LOGGER.debug(
            "[%s#%d] ignoring stale %s for call_id=%s (current=%s)",
            self.transport_name,
            self._instance_id,
            msg_name,
            call_id,
            self._call_id,
        )
        return True

    @staticmethod
    def _parse_call_id_only(payload: bytes) -> str:
        if not payload:
            return ""
        call_id, _ = protocol.decode_call_id_prefix(payload)
        return call_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def is_ringing(self) -> bool:
        return self._ringing

    # FSM transition setters: every state mutation logs before->after +
    # cause so diagnostics can prove which path drove the change.

    def _set_streaming(self, value: bool, cause: str) -> None:
        if self._streaming == value:
            return
        _LOGGER.debug(
            "[%s#%d] _streaming %s->%s cause=%s",
            self.transport_name, self._instance_id,
            self._streaming, value, cause,
        )
        self._streaming = value
        self._state_changed.set()

    def _set_ringing(self, value: bool, cause: str) -> None:
        if self._ringing == value:
            return
        _LOGGER.debug(
            "[%s#%d] _ringing %s->%s cause=%s",
            self.transport_name, self._instance_id,
            self._ringing, value, cause,
        )
        self._ringing = value
        self._state_changed.set()

    def _set_connected(self, value: bool, cause: str) -> None:
        if self._connected == value:
            return
        _LOGGER.debug(
            "[%s#%d] _connected %s->%s cause=%s",
            self.transport_name, self._instance_id,
            self._connected, value, cause,
        )
        self._connected = value
        self._state_changed.set()

    def _track_audio_drop(self, reason: str, data_len: int) -> None:
        """Counter + rate-limited debug for a dropped outbound audio frame."""
        n = self._audio_send_dropped.get(reason, 0) + 1
        self._audio_send_dropped[reason] = n
        if n == 1 or n % 100 == 0:
            _LOGGER.debug(
                "[%s#%d] send_audio DROP reason=%s streaming=%s connected=%s "
                "len=%d total[%s]=%d",
                self.transport_name, self._instance_id, reason,
                self._streaming, self._connected, data_len, reason, n,
            )

    @abstractmethod
    async def connect(self) -> bool:
        """Open transport-specific resources. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close transport-specific resources. Idempotent."""

    @abstractmethod
    async def start_stream(self, caller_name: str = "") -> str:
        """Send START + wait. Returns "streaming" / "ringing" / "error"."""

    @abstractmethod
    async def stop_stream(self) -> None:
        """Best-effort STOP."""

    @abstractmethod
    async def send_answer(self) -> bool:
        """ANSWER from the ringing state (HA-card answering an ESP call)."""

    async def send_ring(self) -> bool:
        """RING-back an inbound START without answering. No-op default."""
        return False

    @abstractmethod
    async def send_answer_blind(self) -> bool:
        """ANSWER without requiring local _ringing (fresh transport path)."""

    @abstractmethod
    async def send_decline(self, reason: str = "") -> bool:
        """DECLINE with optional UTF-8 reason."""

    async def send_decline_for_call_id(self, call_id: str, reason: str = "") -> bool:
        old_call_id = self._call_id
        self._call_id = call_id or ""
        try:
            return await self.send_decline(reason)
        finally:
            self._call_id = old_call_id

    @abstractmethod
    async def send_audio(self, data: bytes) -> bool:
        """Best-effort audio send."""

    # === Shared FSM dispatch (called by subclass receive paths) ===

    async def _handle_message(self, msg_type: int, payload: bytes) -> None:
        """Centralised FSM dispatch. Audio frames bypass for speed."""
        if msg_type == MSG_AUDIO:
            self._handle_audio_payload(payload)
            return
        if msg_type == MSG_START:
            await self._handle_start(payload)
            return
        if msg_type == MSG_PONG:
            _LOGGER.debug("[%s#%d] PONG - keepalive (ignored)",
                          self.transport_name, self._instance_id)
            return
        if msg_type == MSG_RING:
            self._handle_ring(payload)
            return
        if msg_type == MSG_ANSWER:
            self._handle_answer(payload)
            return
        if msg_type == MSG_DECLINE:
            self._handle_decline(payload)
            return
        if msg_type == MSG_HANGUP:
            self._handle_hangup(payload)
            return
        if msg_type == MSG_PING:
            _LOGGER.debug("[%s#%d] PING -> PONG",
                          self.transport_name, self._instance_id)
            await self._send_pong_response()
            return
        if msg_type == MSG_ERROR:
            self._handle_error(payload)

    def _handle_audio_payload(self, payload: bytes) -> None:
        self._audio_recv += 1
        if self._on_audio:
            self._on_audio(payload)

    async def _handle_start(self, payload: bytes) -> None:
        caller = ""
        try:
            parsed = protocol.parse_start_body(payload)
            caller = parsed["caller_name"]
            incoming_call_id = parsed["call_id"]
            if self._call_id:
                if incoming_call_id == self._call_id:
                    _LOGGER.debug(
                        "[%s#%d] duplicate START for active call_id=%s",
                        self.transport_name, self._instance_id, incoming_call_id,
                    )
                    if self._streaming:
                        await self.send_answer_blind()
                    else:
                        await self.send_ring()
                else:
                    _LOGGER.info(
                        "[%s#%d] START for %s while busy with %s - DECLINE busy",
                        self.transport_name, self._instance_id,
                        incoming_call_id or "(empty)", self._call_id,
                    )
                    await self.send_decline_for_call_id(incoming_call_id, "busy")
                return
            self.set_call_context(parsed["call_id"], caller)
            self.peer_tx_formats = parsed["caller_tx_formats"]
            self.peer_rx_formats = parsed["caller_rx_formats"]
        except ValueError as err:
            self._log_malformed("START", err)
            self.last_caller_name = caller
            return

        _LOGGER.debug("[%s#%d] START received from %s, ringing locally",
                      self.transport_name, self._instance_id,
                      caller or "(unknown)")
        if not self._streaming:
            self._set_ringing(True, "START_inbound")
            if self._on_ringing:
                self._on_ringing()

    def _handle_ring(self, payload: bytes) -> None:
        try:
            call_id = self._parse_call_id_only(payload)
        except ValueError as err:
            self._log_malformed("RING", err)
            return
        if self._gate_call_id(call_id, "RING"):
            return
        _LOGGER.debug("[%s#%d] RING received",
                      self.transport_name, self._instance_id)
        self._set_ringing(True, "RING_inbound")
        if self._on_ringing:
            self._on_ringing()

    def _handle_answer(self, payload: bytes) -> None:
        try:
            parsed = protocol.parse_answer_body(payload)
            call_id = parsed["call_id"]
            self.set_selected_audio_formats(
                parsed["caller_to_dest_format"],
                parsed["dest_to_caller_format"],
            )
        except ValueError as err:
            self._log_malformed("ANSWER", err)
            return
        if self._gate_call_id(call_id, "ANSWER"):
            return
        _LOGGER.debug("[%s#%d] ANSWER received from ESP",
                      self.transport_name, self._instance_id)
        self._awaiting_answer_ack = False
        self._set_ringing(False, "ANSWER_inbound")
        self._set_streaming(True, "ANSWER_inbound")
        if self._on_answered:
            self._on_answered()

    def _handle_decline(self, payload: bytes) -> None:
        reason = ""
        try:
            parsed = protocol.parse_decline_body(payload)
            if self._gate_call_id(parsed["call_id"], "DECLINE"):
                return
            reason = parsed["reason"]
        except ValueError as err:
            self._log_malformed("DECLINE", err)
            return
        _LOGGER.info("[%s#%d] DECLINE from ESP: %s",
                     self.transport_name, self._instance_id, reason or "(empty)")
        self._awaiting_answer_ack = False
        self._set_streaming(False, "DECLINE_inbound")
        self._set_ringing(False, "DECLINE_inbound")
        if self._on_decline_received:
            self._on_decline_received(reason)

    def _handle_hangup(self, payload: bytes) -> None:
        try:
            call_id = self._parse_call_id_only(payload)
        except ValueError as err:
            self._log_malformed("HANGUP", err)
            return
        if self._gate_call_id(call_id, "HANGUP"):
            return
        _LOGGER.debug("[%s#%d] HANGUP received from ESP",
                      self.transport_name, self._instance_id)
        self._awaiting_answer_ack = False
        self._set_streaming(False, "HANGUP_inbound")
        self._set_ringing(False, "HANGUP_inbound")
        if self._on_stop_received:
            self._on_stop_received()

    def _handle_error(self, payload: bytes) -> None:
        try:
            parsed = protocol.parse_error_body(payload)
        except ValueError as err:
            self._log_malformed("ERROR", err)
            return
        if self._gate_call_id(parsed["call_id"], "ERROR"):
            return
        error_code = parsed["error_code"]
        detail = parsed.get("detail", "")
        _LOGGER.error("[%s#%d] ERROR from ESP: code=%d detail=%s",
                      self.transport_name, self._instance_id,
                      error_code, detail or "(none)")
        self._awaiting_answer_ack = False
        self._set_streaming(False, "ERROR_inbound")
        self._set_ringing(False, "ERROR_inbound")
        if self._on_error_received:
            self._on_error_received(error_code, detail)

    def _log_malformed(self, msg_name: str, err: ValueError) -> None:
        _LOGGER.warning("[%s#%d] malformed %s body: %s",
                        self.transport_name, self._instance_id, msg_name, err)

    @abstractmethod
    async def _send_pong_response(self) -> None:
        """Reply to a received PING with PONG. Subclass-specific framing."""

    @property
    @abstractmethod
    def transport_name(self) -> str:
        """Short label for logs ("tcp" or "udp")."""

    # === Helper: build a header for either transport ===

    @staticmethod
    def build_header(msg_type: int, length: int) -> bytes:
        return protocol.build_header(msg_type, length)

    def _fallback_anchor(self) -> str:
        """Anchor for HA-as-caller call_ids. UDP overrides; TCP keeps empty."""
        return ""

    def _current_call_id(self) -> str:
        if self._call_id:
            return self._call_id
        anchor = self.last_caller_name or self._fallback_anchor()
        return f"{anchor}<->{self.host}" if anchor else ""

    async def _wait_for_setup_result(self, timeout: float = 2.0) -> Optional[str]:
        """Wait for ANSWER/RING state without polling the event loop."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._state_changed.clear()
            if not self._connected:
                return None
            if self._streaming:
                return "streaming"
            if self._ringing:
                return "ringing"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
