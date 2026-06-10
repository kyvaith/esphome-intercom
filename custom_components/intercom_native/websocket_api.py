"""WebSocket API for Intercom Native integration."""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Dict, Optional

AUDIO_QUEUE_SIZE = 8  # max pending chunks; drop oldest when full

from aiohttp import WSMsgType, web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .audio_format import (
    HA_BROWSER_RX_FORMATS,
    HA_BROWSER_TX_FORMATS,
    LEGACY_AUDIO_FORMAT,
    AudioFormat,
    choose_common_format,
    parse_audio_format_list,
    require_udp_safe_formats,
)
from .audio_pcm import convert_audio_frame
from .audio_ws import decode_audio_frame, encode_audio_frame
from .const import DOMAIN, HA_PEER_FALLBACK_NAME, HA_SOFTPHONE_DEVICE_ID
from .fsm import (
    SessionState,
    TerminalReason,
    can_transition,
    localize_bridge_reason,
    terminal_reason_for_decline,
    terminal_state_for_decline,
)
from .transport_base import IntercomTransport

_LOGGER = logging.getLogger(__name__)

# WebSocket command types
WS_TYPE_START = f"{DOMAIN}/start"
WS_TYPE_STOP = f"{DOMAIN}/stop"
WS_TYPE_ANSWER = f"{DOMAIN}/answer"
WS_TYPE_LIST = f"{DOMAIN}/list_devices"
WS_TYPE_RESOLVE_DEVICE = f"{DOMAIN}/resolve_device"
WS_TYPE_HA_SOFTPHONE_START = f"{DOMAIN}/ha_softphone_start"
WS_TYPE_HA_SOFTPHONE_STATE = f"{DOMAIN}/ha_softphone_state"
WS_TYPE_SET_HA_SOFTPHONE_DND = f"{DOMAIN}/set_ha_softphone_dnd"

# Active sessions: device_id -> IntercomSession
_sessions: Dict[str, "IntercomSession"] = {}

# Active bridges: bridge_id -> BridgeSession
_bridges: Dict[str, "BridgeSession"] = {}

CALL_EVENT = "intercom_native.call_event"
WS_AUDIO_IDLE_TIMEOUT = 5.0
WS_AUDIO_WATCHDOG_INTERVAL = 1.0


async def _ws_send_json(ws: web.WebSocketResponse, payload: dict[str, Any]) -> bool:
    if ws.closed:
        return False
    try:
        await ws.send_str(json.dumps(payload))
        return True
    except (ConnectionError, RuntimeError):
        return False


def _session_audio_payload(session: "IntercomSession", state: str) -> dict[str, Any]:
    return {
        "state": state,
        "tx_format": session.tx_format.wire_token(),
        "rx_format": session.rx_format.wire_token(),
    }


def _ha_softphone_store(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "ha_softphone",
        {"dnd": False},
    )


def _ha_softphone_dnd(hass: HomeAssistant) -> bool:
    return bool(_ha_softphone_store(hass).get("dnd"))


def _ha_softphone_state(hass: HomeAssistant) -> dict[str, Any]:
    store = _ha_softphone_store(hass)
    return {
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "session_device_id": store.get("session_device_id", ""),
        "dnd": _ha_softphone_dnd(hass),
        "busy": bool(store.get("session_device_id")),
        "state": store.get("state", "idle"),
        "caller": store.get("caller", ""),
        "peer_name": store.get("peer_name", ""),
    }


def _ha_softphone_session_device_id(hass: HomeAssistant) -> str:
    return str(_ha_softphone_store(hass).get("session_device_id") or "")


def _set_ha_softphone_call_state(
    hass: HomeAssistant,
    state: str,
    *,
    session_device_id: str = "",
    caller: str = "",
    peer_name: str = "",
    **extra: Any,
) -> None:
    store = _ha_softphone_store(hass)
    terminal = state in {"idle", "disconnected", "declined", "error"}
    if terminal:
        session_device_id = store.get("session_device_id", session_device_id)
        caller = store.get("caller", caller)
        peer_name = store.get("peer_name", peer_name)
        store.pop("session_device_id", None)
        store.pop("state", None)
        store.pop("caller", None)
        store.pop("peer_name", None)
    else:
        store["session_device_id"] = session_device_id
        store["state"] = state
        store["caller"] = caller
        store["peer_name"] = peer_name

    payload = {
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "session_device_id": session_device_id or "",
        "state": state,
        "caller": caller or "",
        "peer_name": peer_name or "",
    }
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    _fire_call_event(hass, payload, "session")


def _put_latest(queue: asyncio.Queue, data: bytes) -> None:
    """Non-blocking latest-wins enqueue for realtime audio frames."""
    try:
        queue.put_nowait(data)
        return
    except asyncio.QueueFull:
        pass

    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass

    try:
        queue.put_nowait(data)
    except asyncio.QueueFull:
        pass


def _call_event_type(state: str, reason: str | None = None) -> str:
    state_l = (state or "").lower()
    reason_l = (reason or "").lower()
    if state_l == "ringing":
        return "ringing"
    if state_l in ("calling", "outgoing", "forwarding"):
        return "outgoing"
    if state_l in ("connected", "streaming"):
        return "answered"
    if state_l in ("error", "failed"):
        return "failed"
    if state_l in ("idle", "disconnected", "declined", "ended"):
        return "missed" if reason_l == TerminalReason.TIMEOUT.value else "ended"
    return state_l or "state"


def _fire_call_event(hass: HomeAssistant, payload: dict[str, Any], scope: str) -> None:
    event = dict(payload)
    state = str(event.get("state") or "")
    reason = event.get("reason")
    event["scope"] = scope
    event["type"] = _call_event_type(state, str(reason) if reason is not None else None)
    hass.bus.async_fire(CALL_EVENT, event)


def _audio_mode(value: str | None) -> str:
    mode = (value or "full_duplex").strip().lower()
    return mode if mode in {"full_duplex", "mic_only", "speaker_only", "control_only"} else "full_duplex"


def _has_mic(mode: str | None) -> bool:
    return _audio_mode(mode) in {"full_duplex", "mic_only"}


def _has_speaker(mode: str | None) -> bool:
    return _audio_mode(mode) in {"full_duplex", "speaker_only"}


def _device_formats(device: dict | None, key: str) -> list[AudioFormat]:
    if not device:
        return [LEGACY_AUDIO_FORMAT]
    value = device.get(key)
    if isinstance(value, str):
        raw = value
    else:
        raw = ";".join(value or [])
    try:
        formats = parse_audio_format_list(raw)
        if device.get("transport") == "udp":
            require_udp_safe_formats(
                formats,
                context=f"{device.get('name') or device.get('device_id')} UDP {key}",
            )
        return formats
    except ValueError as err:
        _LOGGER.warning("Ignoring invalid %s on %s: %s", key, device.get("name") or device.get("device_id"), err)
        return [LEGACY_AUDIO_FORMAT]


async def _device_audio_mode(hass: HomeAssistant, device_id: str) -> str:
    if device_id == HA_SOFTPHONE_DEVICE_ID:
        return "full_duplex"
    for device in await _get_intercom_devices(hass):
        if device.get("device_id") == device_id:
            return _audio_mode(device.get("audio_mode"))
    return "full_duplex"


from .transport_helpers import (
    TransportCallbacks,
    build_transport as _build_transport_impl,
    cancel_task,
    configured_transport_type,
    stop_transport,
)


class IntercomSession:
    """Manages a single intercom session between browser and ESP."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        host: str,
        transport_type: str | None = None,
        transport: IntercomTransport | None = None,
        call_id: str = "",
        caller_name: str = "",
        audio_mode: str = "full_duplex",
        local_tx_formats: list[AudioFormat] | None = None,
        local_rx_formats: list[AudioFormat] | None = None,
        peer_tx_formats: list[AudioFormat] | None = None,
        peer_rx_formats: list[AudioFormat] | None = None,
    ):
        """Initialize session."""
        self.hass = hass
        self.device_id = device_id
        self.host = host
        self.transport_type = transport_type or configured_transport_type(hass, host)
        self._initial_call_id = call_id
        self._initial_caller_name = caller_name
        self.audio_mode = _audio_mode(audio_mode)
        self.local_tx_formats = local_tx_formats or [LEGACY_AUDIO_FORMAT]
        self.local_rx_formats = local_rx_formats or [LEGACY_AUDIO_FORMAT]
        self.peer_tx_formats = peer_tx_formats or [LEGACY_AUDIO_FORMAT]
        self.peer_rx_formats = peer_rx_formats or [LEGACY_AUDIO_FORMAT]
        self.tx_format = LEGACY_AUDIO_FORMAT
        self.rx_format = LEGACY_AUDIO_FORMAT

        self._transport: Optional[IntercomTransport] = transport
        self._state = SessionState.IDLE
        self._tx_queue: asyncio.Queue = asyncio.Queue(maxsize=AUDIO_QUEUE_SIZE)
        self._tx_task: Optional[asyncio.Task] = None
        self._cleanup_scheduled = False
        self._audio_ws: web.WebSocketResponse | None = None
        self._audio_ws_task: asyncio.Task | None = None
        self._last_browser_audio = time.monotonic()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_ringing(self) -> bool:
        return self._state in (SessionState.RINGING_IN, SessionState.RINGING_OUT)

    @property
    def is_active(self) -> bool:
        return self._state is SessionState.STREAMING

    def _transition(
        self,
        target: SessionState,
        *,
        event: str | bool | None = None,
        **extra: Any,
    ) -> bool:
        current = self._state
        if target is current:
            return False
        if not can_transition(current, target):
            _LOGGER.debug("Session %s ignored transition %s -> %s", self.device_id, current, target)
            return False
        self._state = target

        if target is SessionState.STREAMING and _has_speaker(self.audio_mode):
            if self._tx_task is None or self._tx_task.done():
                self._tx_task = self.hass.async_create_task(self._tx_sender())

        if event is False:
            return True
        wire = str(event) if event else ""
        if not wire:
            return True

        payload = {"device_id": self.device_id, "state": wire}
        payload.update(extra)
        _fire_call_event(self.hass, payload, "session")

        if _ha_softphone_session_device_id(self.hass) == self.device_id:
            if target is SessionState.ENDED:
                _set_ha_softphone_call_state(
                    self.hass,
                    wire,
                    reason=extra.get("reason"),
                    origin=extra.get("origin"),
                    code=extra.get("code"),
                )
            elif target is SessionState.STREAMING:
                peer = self._initial_caller_name or ""
                _set_ha_softphone_call_state(
                    self.hass,
                    "streaming",
                    session_device_id=self.device_id,
                    caller=peer,
                    peer_name=peer,
                )
        return True

    # --- Callbacks for transport (shared by start() and answer_esp_call()) ---

    def _on_audio(self, data: bytes) -> None:
        """Handle audio from ESP - push to the bound browser audio socket."""
        if not self.is_active or not _has_mic(self.audio_mode):
            return
        if self._audio_ws is None or self._audio_ws.closed:
            return
        self.hass.async_create_task(self._send_audio_ws(data))

    async def _send_audio_ws(self, data: bytes) -> None:
        ws = self._audio_ws
        if ws is None or ws.closed:
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            await ws.send_bytes(encode_audio_frame(data))

    def bind_audio_ws(self, ws: web.WebSocketResponse) -> None:
        old_ws = self._audio_ws
        if old_ws is ws:
            self._last_browser_audio = time.monotonic()
            return
        if old_ws is not None and not old_ws.closed:
            self.hass.async_create_task(old_ws.close())
        self._audio_ws = ws
        self._last_browser_audio = time.monotonic()
        if self._audio_ws_task is None or self._audio_ws_task.done():
            self._audio_ws_task = self.hass.async_create_task(self._audio_watchdog())

    async def unbind_audio_ws(self, ws: web.WebSocketResponse, *, stop: bool = True) -> None:
        if self._audio_ws is not ws:
            return
        self._audio_ws = None
        await cancel_task(self._audio_ws_task)
        self._audio_ws_task = None
        if stop:
            await _stop_device_sessions(self.device_id, hass=self.hass)

    async def _audio_watchdog(self) -> None:
        try:
            while self.is_active:
                await asyncio.sleep(WS_AUDIO_WATCHDOG_INTERVAL)
                ws = self._audio_ws
                if ws is None or ws.closed:
                    await _stop_device_sessions(self.device_id, hass=self.hass)
                    return
                if _has_speaker(self.audio_mode) and time.monotonic() - self._last_browser_audio > WS_AUDIO_IDLE_TIMEOUT:
                    await _stop_device_sessions(self.device_id, hass=self.hass)
                    return
        except asyncio.CancelledError:
            pass

    def _on_disconnected(self) -> None:
        self._transition(
            SessionState.ENDED,
            event="disconnected",
            reason=TerminalReason.REMOTE_DEVICE_LOST.value,
        )
        # Mirror STOP/DECLINE/ERROR teardown so a raw socket death
        # doesn't leave a stale _sessions entry blocking new MSG_START.
        self._schedule_remote_cleanup(TerminalReason.REMOTE_DEVICE_LOST.value)

    def _on_ringing(self) -> None:
        """ESP is ringing on an outgoing HA call."""
        self._transition(SessionState.RINGING_OUT, event="ringing")

    def _on_answered(self) -> None:
        """ESP answered the call, streaming started."""
        self._transition(SessionState.STREAMING, event="streaming")

    def _on_stop_received(self) -> None:
        """ESP sent HANGUP from its side."""
        _LOGGER.info("Session received HANGUP from ESP: %s", self.device_id)
        self._transition(SessionState.ENDED, event="idle", reason=TerminalReason.REMOTE_HANGUP.value)
        self._schedule_remote_cleanup("HANGUP_inbound")

    def _on_decline_received(self, reason: str) -> None:
        """ESP sent DECLINE(reason)."""
        _LOGGER.info("Session received DECLINE from ESP (%s): %s", reason, self.device_id)
        self._transition(
            SessionState.ENDED,
            event=terminal_state_for_decline(reason),
            reason=terminal_reason_for_decline(reason),
        )
        self._schedule_remote_cleanup("DECLINE_inbound")

    def _on_error_received(self, code: int, detail: str = "") -> None:
        """ESP sent ERROR (technical fault)."""
        _LOGGER.info("Session received ERROR from ESP (code=%d detail=%s): %s",
                     code, detail or "(none)", self.device_id)
        self._transition(SessionState.ENDED, event="error", code=code, reason=detail or str(code))
        self._schedule_remote_cleanup("ERROR_inbound")

    def _schedule_remote_cleanup(self, cause: str) -> None:
        """Tear down on ESP-initiated termination (STOP/DECLINE/ERROR).

        Without it the transport stays connected and the next unsolicited
        MSG_START from the same host hits the stale consumer instead of
        the router, and the call drops silently.
        """
        if self._cleanup_scheduled:
            return
        self._cleanup_scheduled = True

        async def _run() -> None:
            try:
                await self.stop(send_signaling=False)
            except Exception:
                _LOGGER.exception(
                    "Session %s: cleanup after %s failed", self.device_id, cause,
                )
            if _sessions.get(self.device_id) is self:
                _sessions.pop(self.device_id, None)
                _LOGGER.debug(
                    "Session %s removed from registry (cause=%s)",
                    self.device_id, cause,
                )

        self.hass.async_create_task(_run())

    def _create_transport(self) -> IntercomTransport:
        """Create the configured transport with standard callbacks."""
        callbacks = TransportCallbacks(
            on_audio=self._on_audio,
            on_disconnected=self._on_disconnected,
            on_ringing=self._on_ringing,
            on_answered=self._on_answered,
            on_stop_received=self._on_stop_received,
            on_decline_received=self._on_decline_received,
            on_error_received=self._on_error_received,
        )

        if self._transport is not None:
            # Adopted-socket path: IntercomTcpSocketManager built the
            # transport with placeholder callbacks; rewire them here.
            _LOGGER.debug(
                "Session %s wiring callbacks onto adopted transport[%s#%d]",
                self.device_id,
                self._transport.transport_name,
                self._transport._instance_id,
            )
            self._transport.set_callbacks(callbacks)
            if self._initial_call_id:
                self._transport.set_call_context(
                    self._initial_call_id,
                    self._initial_caller_name,
                )
            self._transport.set_local_audio_formats([self.tx_format], [self.rx_format])
            self._transport.set_selected_audio_formats(self.tx_format, self.rx_format)
            return self._transport

        transport = _build_transport_impl(self.hass, self.host, self.transport_type, callbacks)
        if self._initial_call_id:
            transport.set_call_context(self._initial_call_id, self._initial_caller_name)
        transport.set_local_audio_formats([self.tx_format], [self.rx_format])
        transport.set_selected_audio_formats(self.tx_format, self.rx_format)
        return transport

    def _negotiate_outgoing_formats(self) -> bool:
        tx = choose_common_format(self.peer_rx_formats, self.local_tx_formats)
        rx = choose_common_format(self.peer_tx_formats, self.local_rx_formats)
        if tx is None or rx is None:
            _LOGGER.error(
                "No compatible audio format for %s: local_tx=%s peer_rx=%s peer_tx=%s local_rx=%s",
                self.device_id,
                [fmt.wire_token() for fmt in self.local_tx_formats],
                [fmt.wire_token() for fmt in self.peer_rx_formats],
                [fmt.wire_token() for fmt in self.peer_tx_formats],
                [fmt.wire_token() for fmt in self.local_rx_formats],
            )
            return False
        self.tx_format = tx
        self.rx_format = rx
        return True

    def _negotiate_incoming_formats(self) -> bool:
        if self._transport is None:
            return False
        caller_to_dest = choose_common_format(self._transport.peer_tx_formats, self.local_rx_formats)
        dest_to_caller = choose_common_format(self._transport.peer_rx_formats, self.local_tx_formats)
        if caller_to_dest is None or dest_to_caller is None:
            _LOGGER.error(
                "No compatible inbound audio format for %s: peer_tx=%s local_rx=%s local_tx=%s peer_rx=%s",
                self.device_id,
                [fmt.wire_token() for fmt in self._transport.peer_tx_formats],
                [fmt.wire_token() for fmt in self.local_rx_formats],
                [fmt.wire_token() for fmt in self.local_tx_formats],
                [fmt.wire_token() for fmt in self._transport.peer_rx_formats],
            )
            return False
        self.rx_format = caller_to_dest
        self.tx_format = dest_to_caller
        self._transport.set_selected_audio_formats(caller_to_dest, dest_to_caller)
        return True

    async def start(self) -> str:
        """Start the session. Returns "streaming" / "ringing" / "error"."""
        if self._state is SessionState.STREAMING:
            return "streaming"
        if self._state is not SessionState.IDLE:
            _LOGGER.warning("Session %s start refused in state %s", self.device_id, self._state)
            return "error"

        while not self._tx_queue.empty():
            try:
                self._tx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if not self._negotiate_outgoing_formats():
            self._transition(SessionState.ENDED, event="error", reason="incompatible_audio_format")
            return "error"

        self._transport = self._create_transport()

        if not await self._transport.connect():
            self._transition(SessionState.ENDED, event=False)
            return "error"

        self._transition(SessionState.CONNECTING, event="calling")

        caller_name = (self.hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME
        result = await self._transport.start_stream(caller_name=caller_name)

        if result == "streaming":
            self._transition(SessionState.STREAMING, event="streaming")
            return "streaming"
        elif result == "ringing":
            # The RING wire event is fired by _on_ringing when the transport
            # delivers it; the command response carries the immediate result.
            self._transition(SessionState.RINGING_OUT, event=False)
            return "ringing"
        else:
            self._transition(SessionState.ENDED, event=False)
            await self._transport.disconnect()
            return "error"

    async def stop(self, send_signaling: bool = True) -> None:
        """Stop the session. send_signaling=False = ESP already terminated:
        the matching _on_*_received already fired the reason event, stay
        silent here so the card keeps that reason."""
        if send_signaling:
            self._transition(
                SessionState.ENDED,
                event="idle",
                reason=TerminalReason.LOCAL_HANGUP.value,
            )
        else:
            self._transition(SessionState.ENDED, event=False)
        while not self._tx_queue.empty():
            try:
                self._tx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await cancel_task(self._tx_task)
        self._tx_task = None
        await cancel_task(self._audio_ws_task)
        self._audio_ws_task = None
        ws = self._audio_ws
        self._audio_ws = None
        if ws is not None and not ws.closed:
            await ws.close()

        await stop_transport(self._transport, send_signaling=send_signaling)
        self._transport = None

    async def decline(self, reason: str = "") -> bool:
        """Decline a call locally, optionally propagating a semantic reason."""
        if not self._transport:
            return False
        if not reason:
            await self.stop()
            return True
        if not await self._transport.send_decline(reason):
            return False
        self._transition(SessionState.ENDED, event="idle", reason=reason)
        await self.stop(send_signaling=False)
        return True

    async def answer(self) -> bool:
        """Send ANSWER to an incoming ringing ESP. True on success."""
        if self._state is not SessionState.RINGING_IN or not self._transport:
            _LOGGER.warning("answer() refused: state=%s transport=%s", self._state, bool(self._transport))
            return False
        if not self._negotiate_incoming_formats():
            self._transition(SessionState.ENDED, event="error", reason="incompatible_audio_format")
            return False

        result = await self._transport.send_answer()
        if result:
            self._on_answered()
            _LOGGER.debug("ANSWER sent to ESP")
        return result

    async def start_ringing(self, caller_name: str = "") -> bool:
        """RING back an ESP-initiated MSG_START without answering.

        Lets the card auto-answer (localStorage flag) or present the call
        to the user. Caller then invokes answer() or stop().
        """
        if self._state in (SessionState.STREAMING, SessionState.RINGING_IN, SessionState.RINGING_OUT):
            return True
        self._transport = self._create_transport()
        if not await self._transport.connect():
            return False
        if not await self._transport.send_ring():
            await self._transport.disconnect()
            return False
        self._transition(SessionState.RINGING_IN, event="ringing", caller=caller_name)
        return True

    async def answer_esp_call(self) -> str:
        """Answer an ESP-initiated call. Returns "streaming" / "error"."""
        if self._state is SessionState.STREAMING:
            return "streaming"

        self._transport = self._create_transport()

        if not await self._transport.connect():
            return "error"
        self._transition(SessionState.CONNECTING, event=False)

        if not await self._transport.send_answer_blind():
            await self._transport.disconnect()
            return "error"
        self._on_answered()
        return "streaming"

    async def _tx_sender(self) -> None:
        """Single task that sends audio from queue to the active transport."""
        try:
            while self.is_active and self._transport:
                data = await self._tx_queue.get()
                await self._transport.send_audio(data)
        except asyncio.CancelledError:
            pass

    def queue_audio(self, data: bytes) -> None:
        """Non-blocking enqueue; drops oldest on full to keep latency bounded."""
        if not self.is_active or not _has_speaker(self.audio_mode):
            return
        expected = self.tx_format.nominal_frame_bytes
        if len(data) != expected:
            _LOGGER.warning(
                "Dropping browser audio for %s: got %d bytes, expected %d for %s",
                self.device_id,
                len(data),
                expected,
                self.tx_format.wire_token(),
            )
            return
        self._last_browser_audio = time.monotonic()
        _put_latest(self._tx_queue, data)


class BridgeSession:
    """Audio bridge between two ESPs.

    Per-direction queues + dedicated sender tasks keep hangup/stop
    races out of the audio path.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        bridge_id: str,
        source_device_id: str,
        source_host: str,
        source_name: str,
        dest_device_id: str,
        dest_host: str,
        dest_name: str,
        source_transport_type: str | None = None,
        dest_transport_type: str | None = None,
        source_transport: IntercomTransport | None = None,
        source_call_id: str = "",
        source_audio_mode: str = "full_duplex",
        dest_audio_mode: str = "full_duplex",
        source_tx_formats: list[AudioFormat] | None = None,
        source_rx_formats: list[AudioFormat] | None = None,
        dest_tx_formats: list[AudioFormat] | None = None,
        dest_rx_formats: list[AudioFormat] | None = None,
    ):
        """`source_transport` is set when the TCP listener already
        accepted the source leg; otherwise start() builds it."""
        self.hass = hass
        self.bridge_id = bridge_id
        self.source_device_id = source_device_id
        self.source_host = source_host
        self.source_name = source_name  # caller name forwarded to dest
        self.dest_device_id = dest_device_id
        self.dest_host = dest_host
        self.dest_name = dest_name  # callee name forwarded to source
        self.source_transport_type = source_transport_type or configured_transport_type(hass, source_host)
        self.dest_transport_type = dest_transport_type or configured_transport_type(hass, dest_host)
        self.source_call_id = source_call_id or bridge_id
        self.source_audio_mode = _audio_mode(source_audio_mode)
        self.dest_audio_mode = _audio_mode(dest_audio_mode)
        self.source_tx_formats = source_tx_formats or [LEGACY_AUDIO_FORMAT]
        self.source_rx_formats = source_rx_formats or [LEGACY_AUDIO_FORMAT]
        self.dest_tx_formats = dest_tx_formats or [LEGACY_AUDIO_FORMAT]
        self.dest_rx_formats = dest_rx_formats or [LEGACY_AUDIO_FORMAT]
        self.source_to_ha_format = LEGACY_AUDIO_FORMAT
        self.ha_to_source_format = LEGACY_AUDIO_FORMAT
        self.dest_to_ha_format = LEGACY_AUDIO_FORMAT
        self.ha_to_dest_format = LEGACY_AUDIO_FORMAT

        self._source_client: Optional[IntercomTransport] = source_transport
        self._dest_client: Optional[IntercomTransport] = None
        self._active = False
        self._state = "new"

        self._q_source_to_dest: asyncio.Queue = asyncio.Queue(maxsize=AUDIO_QUEUE_SIZE)
        self._q_dest_to_source: asyncio.Queue = asyncio.Queue(maxsize=AUDIO_QUEUE_SIZE)

        self._sender_s2d: Optional[asyncio.Task] = None
        self._sender_d2s: Optional[asyncio.Task] = None

        self._stop_lock = asyncio.Lock()
        self._source_answer_notified = False
        self._source_ring_notified = False
        self._setup_decline_reason: str | None = None
        self._terminal_fired = False
        self._terminal_reason: str | None = None
        self._terminal_origin: str | None = None
        self._stopping = False

    def _push_audio(self, queue: asyncio.Queue, data: bytes) -> None:
        _put_latest(queue, data)

    def _pick_bridge_formats(self) -> None:
        source_tx = self._source_client.peer_tx_formats if self._source_client else self.source_tx_formats
        source_rx = self._source_client.peer_rx_formats if self._source_client else self.source_rx_formats
        s2d_common = choose_common_format(source_tx, self.dest_rx_formats)
        d2s_common = choose_common_format(self.dest_tx_formats, source_rx)

        self.source_to_ha_format = s2d_common or source_tx[0]
        self.ha_to_dest_format = s2d_common or self.dest_rx_formats[0]
        self.dest_to_ha_format = d2s_common or self.dest_tx_formats[0]
        self.ha_to_source_format = d2s_common or source_rx[0]

        if self._source_client is not None:
            self._source_client.set_selected_audio_formats(
                self.source_to_ha_format,
                self.ha_to_source_format,
            )
        if self._dest_client is not None:
            self._dest_client.set_local_audio_formats(
                [self.ha_to_dest_format],
                [self.dest_to_ha_format],
            )

    def _convert_bridge_audio(self, data: bytes, src: AudioFormat, dst: AudioFormat, direction: str) -> bytes | None:
        try:
            return convert_audio_frame(data, src, dst)
        except ValueError as err:
            _LOGGER.warning("Bridge %s dropped invalid %s audio frame: %s", self.bridge_id, direction, err)
            return None

    async def _notify_source_answered(self) -> None:
        """Forward ANSWER to the source so it commits to STREAMING
        immediately instead of waiting for the first reverse audio frame."""
        if self._source_answer_notified or not self._active or self._source_client is None:
            return
        self._source_answer_notified = True

        try:
            ok = await self._source_client.send_answer_blind()
        except Exception as err:
            self._source_answer_notified = False
            _LOGGER.error("Bridge source ANSWER notify failed: %s", err)
            return

        if not ok:
            self._source_answer_notified = False
            _LOGGER.warning("Bridge source ANSWER notify not acknowledged")
            return

        _LOGGER.debug("Bridge source promoted to STREAMING: %s", self.bridge_id)

    async def replay_source_start(self, transport: IntercomTransport | None = None) -> None:
        """Idempotent response for duplicate START with the same bridge id."""
        client = transport or self._source_client
        if client is None:
            return
        temporary = transport is not None and transport is not self._source_client
        if temporary:
            client.set_callbacks(TransportCallbacks())
            client.set_call_context(self.source_call_id, self.source_name)
            if not client.is_connected and not await client.connect():
                return
        try:
            if self._terminal_fired:
                await client.send_decline(self._terminal_reason or "")
            elif self._source_answer_notified or self._state == "streaming":
                await client.send_answer_blind()
            else:
                await client.send_ring()
        finally:
            if temporary:
                try:
                    await client.disconnect()
                except Exception:
                    _LOGGER.debug("Duplicate START transport cleanup failed", exc_info=True)

    async def _notify_source_ringing(self) -> None:
        """Forward RING to the source so its UI flips to 'X is ringing'."""
        if self._source_ring_notified or self._source_client is None:
            return
        try:
            ok = await self._source_client.send_ring()
        except Exception as err:
            _LOGGER.error("Bridge source RING notify failed: %s", err)
            return
        if not ok:
            _LOGGER.debug("Bridge source RING notify not acknowledged")
            return
        self._source_ring_notified = True

    async def _decline_source_setup(self, reason: str) -> None:
        """Tell the already-calling source why bridge setup is being refused."""
        if self._source_client is None:
            return
        try:
            await self._source_client.send_decline(reason)
            if getattr(self._source_client, "transport_name", "") == "udp":
                await asyncio.sleep(0.45)
        except Exception as err:
            _LOGGER.warning(
                "Bridge source setup DECLINE(%s) failed for %s: %s",
                reason,
                self.bridge_id,
                err,
            )

    async def _sender_loop(
        self,
        queue: asyncio.Queue,
        client: IntercomTransport,
        direction: str
    ) -> None:
        """Pull from `queue` and send_audio to `client` while active."""
        _LOGGER.debug("Bridge sender %s started", direction)
        try:
            while self._active:
                data = await queue.get()
                if self._active and client:
                    await client.send_audio(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOGGER.error("Bridge sender %s fatal error: %s - stopping bridge", direction, e)
            self.hass.async_create_task(self.stop())
        finally:
            _LOGGER.debug("Bridge sender %s stopped", direction)

    def _source_audio(self, data: bytes) -> None:
        if self._active and _has_mic(self.source_audio_mode) and _has_speaker(self.dest_audio_mode):
            converted = self._convert_bridge_audio(
                data,
                self.source_to_ha_format,
                self.ha_to_dest_format,
                "source_to_dest",
            )
            if converted is not None:
                self._push_audio(self._q_source_to_dest, converted)

    def _dest_audio(self, data: bytes) -> None:
        if self._active and _has_mic(self.dest_audio_mode) and _has_speaker(self.source_audio_mode):
            converted = self._convert_bridge_audio(
                data,
                self.dest_to_ha_format,
                self.ha_to_source_format,
                "dest_to_source",
            )
            if converted is not None:
                self._push_audio(self._q_dest_to_source, converted)

    def _source_disconnected(self) -> None:
        _LOGGER.debug("Bridge source disconnected: %s", self.bridge_id)
        self._fire_terminal_event(
            "disconnected",
            reason=TerminalReason.REMOTE_DEVICE_LOST.value,
            origin="source",
        )
        self.hass.async_create_task(self.stop())

    def _dest_disconnected(self) -> None:
        _LOGGER.debug("Bridge dest disconnected: %s", self.bridge_id)
        self._fire_terminal_event(
            "disconnected",
            reason=TerminalReason.REMOTE_DEVICE_LOST.value,
            origin="dest",
        )
        self.hass.async_create_task(self.stop())

    def _source_answered(self) -> None:
        _LOGGER.debug("Bridge source answered: %s", self.bridge_id)

    def _dest_answered(self) -> None:
        _LOGGER.debug("Bridge dest answered: %s", self.bridge_id)
        self.hass.async_create_task(self._complete_bridge_answer())

    async def _complete_bridge_answer(self) -> None:
        await self._notify_source_answered()
        if self._active and not self._sender_s2d:
            self._state = "streaming"
            self._start_sender_tasks()

    def _source_stop(self) -> None:
        _LOGGER.info("Bridge source sent HANGUP: %s", self.bridge_id)
        self._fire_terminal_event(
            "disconnected",
            reason=TerminalReason.REMOTE_HANGUP.value,
            origin="source",
        )
        self.hass.async_create_task(self.stop())

    def _dest_stop(self) -> None:
        _LOGGER.info("Bridge dest sent HANGUP: %s", self.bridge_id)
        self._fire_terminal_event(
            "disconnected",
            reason=TerminalReason.REMOTE_HANGUP.value,
            origin="dest",
        )
        self.hass.async_create_task(self.stop())

    def _dest_ringing(self) -> None:
        _LOGGER.info("Bridge dest ringing: %s", self.bridge_id)
        self._state = "ringing"
        self._fire_state_event("ringing")
        self.hass.async_create_task(self._notify_source_ringing())

    async def _propagate_decline(self, origin: str, reason: str) -> None:
        terminal_state = "declined" if reason else "disconnected"
        terminal_reason = terminal_reason_for_decline(reason)
        if not self._active:
            self._setup_decline_reason = terminal_reason
        target = self._dest_client if origin == "source" else self._source_client
        _LOGGER.info("Bridge %s sent DECLINE (%s): %s", origin, reason, self.bridge_id)
        if target is not None:
            try:
                await target.send_decline(reason)
            except Exception as err:
                _LOGGER.warning("Bridge decline propagation failed (%s): %s", origin, err)
        self._fire_terminal_event(terminal_state, reason=terminal_reason, origin=origin)
        await self.stop(send_signaling=False)

    def _source_decline(self, reason: str) -> None:
        self.hass.async_create_task(self._propagate_decline("source", reason))

    def _dest_decline(self, reason: str) -> None:
        self.hass.async_create_task(self._propagate_decline("dest", reason))

    def _source_error(self, code: int, detail: str = "") -> None:
        _LOGGER.warning(
            "Bridge source sent ERROR (code=%d detail=%s): %s",
            code,
            detail or "(none)",
            self.bridge_id,
        )
        self._fire_terminal_event("error", reason=detail or str(code), origin="source")
        self.hass.async_create_task(self.stop())

    def _dest_error(self, code: int, detail: str = "") -> None:
        _LOGGER.warning(
            "Bridge dest sent ERROR (code=%d detail=%s): %s",
            code,
            detail or "(none)",
            self.bridge_id,
        )
        self._fire_terminal_event("error", reason=detail or str(code), origin="dest")
        self.hass.async_create_task(self.stop())

    def _source_callbacks(self) -> TransportCallbacks:
        return TransportCallbacks(
            on_audio=self._source_audio,
            on_disconnected=self._source_disconnected,
            on_ringing=lambda: None,
            on_answered=self._source_answered,
            on_stop_received=self._source_stop,
            on_decline_received=self._source_decline,
            on_error_received=self._source_error,
        )

    def _dest_callbacks(self) -> TransportCallbacks:
        return TransportCallbacks(
            on_audio=self._dest_audio,
            on_disconnected=self._dest_disconnected,
            on_ringing=self._dest_ringing,
            on_answered=self._dest_answered,
            on_stop_received=self._dest_stop,
            on_decline_received=self._dest_decline,
            on_error_received=self._dest_error,
        )

    def _wire_source_client(self) -> None:
        callbacks = self._source_callbacks()
        if self._source_client is None:
            self._source_client = _build_transport_impl(
                self.hass,
                self.source_host,
                self.source_transport_type,
                callbacks,
            )
        else:
            self._source_client.set_callbacks(callbacks)
            _LOGGER.debug(
                "[%s#%d] callbacks wired to bridge %s (source leg)",
                self._source_client.transport_name,
                self._source_client._instance_id,
                self.bridge_id,
            )
        self._source_client.set_call_context(self.source_call_id, self.source_name)

    def _build_dest_client(self) -> IntercomTransport:
        return _build_transport_impl(
            self.hass,
            self.dest_host,
            self.dest_transport_type,
            self._dest_callbacks(),
        )

    async def start(self) -> str:
        """Open both legs. Returns "connected" / "ringing" / "error"."""
        if self._active:
            return "connected"

        self._state = "setup"
        self._wire_source_client()
        self._dest_client = self._build_dest_client()
        source_client = self._source_client
        dest_client = self._dest_client
        self._pick_bridge_formats()

        self._fire_state_event("calling")

        if not source_client.is_connected:
            source_connected = await source_client.connect()
            if not source_connected:
                _LOGGER.error("Bridge: failed to connect to source %s", self.source_host)
                return "error"

        dest_connected = await dest_client.connect()
        if not dest_connected:
            _LOGGER.error("Bridge: failed to connect to dest %s", self.dest_host)
            await self._decline_source_setup(TerminalReason.UNREACHABLE.value)
            await source_client.disconnect()
            return "error"

        # Source already started its own call (its FSM is OUTGOING); a
        # second START would trip the collision DECLINE. Only dest gets
        # START; ANSWER is forwarded back via _notify_source_answered().
        dest_result = await dest_client.start_stream(caller_name=self.source_name)

        if dest_result == "error":
            _LOGGER.error("Bridge: failed to start stream on dest leg")
            if not self._setup_decline_reason:
                await self._decline_source_setup(TerminalReason.UNREACHABLE.value)
            await source_client.disconnect()
            await dest_client.disconnect()
            return "error"

        self._active = True
        self._source_answer_notified = False

        # Wait to start senders until the dest actually answers.
        if dest_result == "ringing":
            self._state = "ringing"
            _LOGGER.info("Bridge waiting for dest to answer: %s <-> %s",
                        self.source_host, self.dest_host)
            return "ringing"

        self._state = "streaming"
        await self._notify_source_answered()
        self._start_sender_tasks()

        _LOGGER.info("Bridge started: %s <-> %s", self.source_host, self.dest_host)

        return "connected"

    def _start_sender_tasks(self) -> None:
        """Start the audio sender tasks."""
        if (
            _has_mic(self.source_audio_mode) and _has_speaker(self.dest_audio_mode)
            and self._sender_s2d is None and self._dest_client
        ):
            self._sender_s2d = self.hass.async_create_task(
                self._sender_loop(self._q_source_to_dest, self._dest_client, "s2d")
            )
        if (
            _has_mic(self.dest_audio_mode) and _has_speaker(self.source_audio_mode)
            and self._sender_d2s is None and self._source_client
        ):
            self._sender_d2s = self.hass.async_create_task(
                self._sender_loop(self._q_dest_to_source, self._source_client, "d2s")
            )
        self._fire_state_event("connected")

    async def _cancel_sender_tasks(self) -> None:
        await cancel_task(self._sender_s2d)
        await cancel_task(self._sender_d2s)
        self._sender_s2d = None
        self._sender_d2s = None

    def _drain_audio_queues(self) -> None:
        for queue in (self._q_source_to_dest, self._q_dest_to_source):
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _close_dest_client(self, send_signaling: bool = True) -> None:
        await stop_transport(self._dest_client, send_signaling=send_signaling)
        self._dest_client = None

    def _fire_state_event(
        self, state: str, reason: str | None = None, origin: str | None = None,
    ) -> None:
        """Fire the unified intercom_native.call_event bridge event.

        `origin` ("source"/"dest") lets the card label which leg
        produced the terminal signal; `reason` is the literal protocol
        text (None when none was provided).
        """
        payload = {
            "bridge_id": self.bridge_id,
            "source_device_id": self.source_device_id,
            "source_name": self.source_name,
            "dest_device_id": self.dest_device_id,
            "dest_name": self.dest_name,
            "state": state,
        }
        if reason is not None:
            payload["reason"] = reason
        if origin is not None:
            payload["origin"] = origin
        _fire_call_event(self.hass, payload, "bridge")

    def _localized_terminal_reason(
        self, role: str, reason: str | None, origin: str | None,
    ) -> str | None:
        """Translate a bridge terminal reason into the device's perspective."""
        return localize_bridge_reason(role, reason, origin)

    def _fire_device_terminal_events(
        self, state: str, reason: str | None, origin: str | None,
    ) -> None:
        """Mirror a bridge terminal event as per-device state events.

        Cards configured for an ESP-to-ESP call mirror their source ESP, not
        the bridge object. Emitting the terminal reason on the per-device event
        path makes the card keep the real reason when the mirrored ESP returns
        to idle.
        """
        device_state = "idle" if state == "disconnected" else state
        for role, device_id, peer_device_id, peer_name in (
            ("source", self.source_device_id, self.dest_device_id, self.dest_name),
            ("dest", self.dest_device_id, self.source_device_id, self.source_name),
        ):
            payload: dict[str, Any] = {
                "device_id": device_id,
                "state": device_state,
                "bridge_id": self.bridge_id,
                "bridge_role": role,
                "peer_device_id": peer_device_id,
                "peer_name": peer_name,
            }
            localized_reason = self._localized_terminal_reason(role, reason, origin)
            if localized_reason is not None:
                payload["reason"] = localized_reason
            if origin in ("source", "dest"):
                payload["origin"] = "self" if role == origin else "remote"
                payload["bridge_origin"] = origin
            _fire_call_event(self.hass, payload, "session")

    def _fire_terminal_event(
        self, state: str, reason: str | None = None, origin: str | None = None,
    ) -> None:
        if self._terminal_fired:
            return
        self._terminal_fired = True
        self._terminal_reason = reason if reason is not None else state
        self._terminal_origin = origin
        self._fire_state_event(state, reason=reason, origin=origin)
        self._fire_device_terminal_events(state, reason=reason, origin=origin)

    async def answer_dest(self) -> bool:
        """Send ANSWER on the dest leg from the ringing state."""
        if self._dest_client is None:
            return False
        ok = await self._dest_client.send_answer()
        if ok:
            await self._notify_source_answered()
            if self._active and not self._sender_s2d:
                self._state = "streaming"
                self._start_sender_tasks()
        return ok

    def _fire_forward_state(
        self,
        state: str,
        new_dest_name: str | None = None,
        old_dest_name: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "bridge_id": self.bridge_id,
            "source_device_id": self.source_device_id,
            "source_name": self.source_name,
            "new_dest_name": new_dest_name or self.dest_name,
            "state": state,
        }
        if old_dest_name is not None:
            payload["old_dest_name"] = old_dest_name
        _fire_call_event(self.hass, payload, "forward")

    def _forward_dest_answered(self) -> None:
        _LOGGER.debug("Bridge new dest answered: %s", self.bridge_id)
        self.hass.async_create_task(self._complete_forward_answer())

    async def _complete_forward_answer(self) -> None:
        await self._notify_source_answered()
        if self._active and not self._sender_s2d:
            self._state = "streaming"
            self._start_sender_tasks()
        self._fire_forward_state("connected")

    def _forward_dest_ringing(self) -> None:
        _LOGGER.debug("Bridge new dest ringing: %s", self.bridge_id)
        self._state = "ringing"
        # Forward leg uses a fresh dest; re-arm RING notify so the source
        # flips back to 'X is ringing' on the new destination.
        self._source_ring_notified = False
        self.hass.async_create_task(self._notify_source_ringing())
        self._fire_forward_state("ringing")

    def _forward_dest_decline(self, reason: str) -> None:
        _LOGGER.debug("Bridge new dest DECLINE (%s): %s", reason, self.bridge_id)
        self.hass.async_create_task(self._propagate_forward_decline(reason))

    async def _propagate_forward_decline(self, reason: str) -> None:
        terminal_state = "declined" if reason else "disconnected"
        terminal_reason = terminal_reason_for_decline(reason)
        if self._source_client is not None:
            try:
                await self._source_client.send_decline(reason)
            except Exception as err:
                _LOGGER.warning("Forward decline propagation failed: %s", err)
        self._fire_terminal_event(terminal_state, reason=terminal_reason, origin="dest")
        await self.stop(send_signaling=False)

    def _forward_dest_callbacks(self) -> TransportCallbacks:
        return TransportCallbacks(
            on_audio=self._dest_audio,
            on_disconnected=self._dest_disconnected,
            on_ringing=self._forward_dest_ringing,
            on_answered=self._forward_dest_answered,
            on_stop_received=self._dest_stop,
            on_decline_received=self._forward_dest_decline,
            on_error_received=self._dest_error,
        )

    async def _fail_forward(self, new_dest_name: str) -> str:
        self._fire_forward_state("failed", new_dest_name=new_dest_name)
        # Inline cleanup; calling stop() would deadlock on _stop_lock.
        self._active = False
        self._state = "ended"
        await self._close_dest_client(send_signaling=False)
        if self._source_client:
            await self._source_client.stop_stream()
            await self._source_client.disconnect()
            self._source_client = None
        _bridges.pop(self.bridge_id, None)
        self._fire_terminal_event(
            "idle",
            reason=TerminalReason.UNREACHABLE.value,
            origin="dest",
        )
        return "error"

    async def forward_to(
        self,
        new_dest_device_id: str,
        new_dest_host: str,
        new_dest_name: str,
        new_dest_transport_type: str | None = None,
        new_dest_audio_mode: str = "full_duplex",
        new_dest_tx_formats: list[AudioFormat] | None = None,
        new_dest_rx_formats: list[AudioFormat] | None = None,
    ) -> str:
        """Replace the dest leg in place. Returns "connected"/"ringing"/"error"."""
        async with self._stop_lock:
            if not self._active or not self._source_client:
                return "error"

            old_dest_name = self.dest_name

            _LOGGER.debug(
                "Forwarding call: %s -> %s (was %s)",
                self.source_name, new_dest_name, old_dest_name,
            )

            self._fire_forward_state(
                "forwarding",
                new_dest_name=new_dest_name,
                old_dest_name=old_dest_name,
            )
            await self._cancel_sender_tasks()
            self._drain_audio_queues()
            await self._close_dest_client()

            # bridge_id stays for the lifetime of the bridge.
            self.dest_device_id = new_dest_device_id
            self.dest_host = new_dest_host
            self.dest_name = new_dest_name
            self.dest_transport_type = new_dest_transport_type or self.dest_transport_type
            self.dest_audio_mode = _audio_mode(new_dest_audio_mode)
            self.dest_tx_formats = new_dest_tx_formats or self.dest_tx_formats
            self.dest_rx_formats = new_dest_rx_formats or self.dest_rx_formats

            self._dest_client = _build_transport_impl(
                self.hass,
                new_dest_host,
                self.dest_transport_type,
                self._forward_dest_callbacks(),
            )
            self._pick_bridge_formats()

            # 6. Connect and start stream to new dest
            if not await self._dest_client.connect():
                _LOGGER.error("Forward failed: cannot connect to %s", new_dest_host)
                return await self._fail_forward(new_dest_name)

            result = await self._dest_client.start_stream(
                caller_name=self.source_name
            )

            if result == "error":
                _LOGGER.error("Forward failed: stream start error for %s", new_dest_name)
                return await self._fail_forward(new_dest_name)

            if result == "ringing":
                _LOGGER.debug("Forward: new dest %s ringing", new_dest_name)
                self._state = "ringing"
                return "ringing"

            self._state = "streaming"
            await self._notify_source_answered()
            self._start_sender_tasks()
            self._fire_forward_state("connected", new_dest_name=new_dest_name)
            return "connected"

    async def stop(
        self,
        send_signaling: bool = True,
        local_origin: str | None = None,
    ) -> None:
        """Stop the bridge.

        `local_origin` ("source"/"dest") fires a `disconnected` event
        with reason="local_hangup" before teardown so the card on that
        leg surfaces "Local hangup" on its ended screen.
        """
        async with self._stop_lock:
            if self._stopping:
                return
            if (
                not self._active
                and self._source_client is None
                and self._dest_client is None
                and self._sender_s2d is None
                and self._sender_d2s is None
            ):
                return

            self._stopping = True
            self._active = False
            self._state = "ended"

            if local_origin in ("source", "dest"):
                self._fire_terminal_event(
                    "disconnected",
                    reason=TerminalReason.LOCAL_HANGUP.value,
                    origin=local_origin,
                )

            await self._cancel_sender_tasks()
            self._drain_audio_queues()

            if self._source_client:
                if send_signaling:
                    await self._source_client.stop_stream()
                await self._source_client.disconnect()
                self._source_client = None

            if self._dest_client:
                if send_signaling:
                    await self._dest_client.stop_stream()
                await self._dest_client.disconnect()
                self._dest_client = None

            _bridges.pop(self.bridge_id, None)

            _LOGGER.info("Bridge stopped and removed: %s", self.bridge_id)
            if not self._terminal_fired:
                self._fire_state_event("idle")


class IntercomAudioWebSocketView(HomeAssistantView):
    """Dedicated browser audio/control WebSocket."""

    url = "/api/intercom_native/ws"
    name = "api:intercom_native:ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        device_id = request.query.get("device_id", "")
        session: IntercomSession | None = _sessions.get(device_id)
        if session is not None:
            session.bind_audio_ws(ws)
            await _ws_send_json(ws, _session_audio_payload(session, "streaming" if session.is_active else "ringing"))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    session = _sessions.get(device_id)
                    if session is not None:
                        with contextlib.suppress(ValueError):
                            session.queue_audio(decode_audio_frame(msg.data))
                    continue
                if msg.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                session = await self._handle_control(hass, ws, device_id, payload)
                if session is not None:
                    device_id = session.device_id
                    session.bind_audio_ws(ws)
        except Exception:
            _LOGGER.exception("Browser audio WebSocket failed for %s", device_id)
        finally:
            session = _sessions.get(device_id)
            if session is not None:
                await session.unbind_audio_ws(ws)
        return ws

    async def _handle_control(
        self,
        hass: HomeAssistant,
        ws: web.WebSocketResponse,
        bound_device_id: str,
        payload: dict[str, Any],
    ) -> IntercomSession | None:
        kind = str(payload.get("type") or "")
        device_id = str(payload.get("device_id") or bound_device_id)

        if kind == "start":
            host = str(payload.get("host") or "")
            if not device_id or not host:
                await _ws_send_json(ws, {"error": "missing start target"})
                return None
            target = next(
                (d for d in await _get_intercom_devices(hass) if d.get("device_id") == device_id),
                None,
            )
            if device_id in _sessions:
                await _sessions.pop(device_id).stop()
            session = IntercomSession(
                hass=hass,
                device_id=device_id,
                host=host,
                transport_type=configured_transport_type(hass, host),
                audio_mode=(target or {}).get("audio_mode") or await _device_audio_mode(hass, device_id),
                local_tx_formats=list(HA_BROWSER_TX_FORMATS),
                local_rx_formats=list(HA_BROWSER_RX_FORMATS),
                peer_tx_formats=_device_formats(target, "tx_formats"),
                peer_rx_formats=_device_formats(target, "rx_formats"),
            )
            result = await session.start()
            if result in ("streaming", "ringing"):
                _sessions[device_id] = session
                session.bind_audio_ws(ws)
                if not await _ws_send_json(ws, _session_audio_payload(session, result)):
                    await session.unbind_audio_ws(ws)
                    return None
                return session
            await _ws_send_json(ws, {"error": "connection_failed"})
            return None

        if kind == "ha_softphone_start":
            target_device_id = str(payload.get("target_device_id") or "")
            target = next(
                (d for d in await _get_intercom_devices(hass) if d.get("device_id") == target_device_id),
                None,
            )
            if target is None or not target.get("host"):
                await _ws_send_json(ws, {"error": "target_not_found"})
                return None
            if _sessions:
                await _ws_send_json(ws, {"error": "busy"})
                return None
            session = IntercomSession(
                hass=hass,
                device_id=HA_SOFTPHONE_DEVICE_ID,
                host=target["host"],
                transport_type=configured_transport_type(hass, target["host"]),
                audio_mode=target.get("audio_mode", "full_duplex"),
                local_tx_formats=list(HA_BROWSER_TX_FORMATS),
                local_rx_formats=list(HA_BROWSER_RX_FORMATS),
                peer_tx_formats=_device_formats(target, "tx_formats"),
                peer_rx_formats=_device_formats(target, "rx_formats"),
            )
            result = await session.start()
            if result in ("streaming", "ringing"):
                _sessions[HA_SOFTPHONE_DEVICE_ID] = session
                _set_ha_softphone_call_state(
                    hass,
                    result,
                    session_device_id=HA_SOFTPHONE_DEVICE_ID,
                    peer_name=target.get("name") or "",
                    target_device_id=target_device_id,
                )
                session.bind_audio_ws(ws)
                if not await _ws_send_json(ws, _session_audio_payload(session, result)):
                    await session.unbind_audio_ws(ws)
                    return None
                return session
            await _ws_send_json(ws, {"error": "connection_failed"})
            return None

        if kind == "answer":
            session = _sessions.get(device_id)
            ok = await session.answer() if session is not None else False
            await _ws_send_json(ws, _session_audio_payload(session, "streaming") if ok else {"state": "error"})
            return session if ok else None

        if kind == "answer_esp_call":
            host = str(payload.get("host") or "")
            existing = _sessions.get(device_id)
            if existing is not None and existing.is_ringing:
                ok = await existing.answer()
                await _ws_send_json(ws, _session_audio_payload(existing, "streaming") if ok else {"state": "error"})
                return existing if ok else None
            if device_id in _sessions:
                await _sessions.pop(device_id).stop()
            session = IntercomSession(
                hass=hass,
                device_id=device_id,
                host=host,
                transport_type=configured_transport_type(hass, host),
                audio_mode=await _device_audio_mode(hass, device_id),
            )
            result = await session.answer_esp_call()
            if result == "streaming":
                _sessions[device_id] = session
                session.bind_audio_ws(ws)
                if not await _ws_send_json(ws, _session_audio_payload(session, "streaming")):
                    await session.unbind_audio_ws(ws)
                    return None
                return session
            await _ws_send_json(ws, {"error": "connection_failed"})
            return None

        if kind in ("stop", "hangup"):
            await _stop_device_sessions(device_id, hass=hass)
            await _ws_send_json(ws, {"state": "idle"})
            return None

        await _ws_send_json(ws, {"error": "unknown_control"})
        return None


def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register WebSocket API commands."""
    hass.http.register_view(IntercomAudioWebSocketView())
    websocket_api.async_register_command(hass, websocket_start)
    websocket_api.async_register_command(hass, websocket_ha_softphone_start)
    websocket_api.async_register_command(hass, websocket_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_set_ha_softphone_dnd)
    websocket_api.async_register_command(hass, websocket_stop)
    websocket_api.async_register_command(hass, websocket_answer)
    websocket_api.async_register_command(hass, websocket_answer_esp_call)
    websocket_api.async_register_command(hass, websocket_list_devices)
    websocket_api.async_register_command(hass, websocket_resolve_device)
    websocket_api.async_register_command(hass, websocket_decline)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_START,
        vol.Required("device_id"): str,
        vol.Required("host"): str,
    }
)
@websocket_api.async_response
async def websocket_start(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Start intercom session."""
    device_id = msg["device_id"]
    host = msg["host"]
    msg_id = msg["id"]

    _LOGGER.debug("Start request: device=%s host=%s", device_id, host)

    try:
        target = next(
            (d for d in await _get_intercom_devices(hass) if d.get("device_id") == device_id),
            None,
        )
        # Stop existing session if any
        if device_id in _sessions:
            old_session = _sessions.pop(device_id)
            await old_session.stop()

        session = IntercomSession(
            hass=hass,
            device_id=device_id,
            host=host,
            transport_type=configured_transport_type(hass, host),
            audio_mode=(target or {}).get("audio_mode") or await _device_audio_mode(hass, device_id),
            local_tx_formats=list(HA_BROWSER_TX_FORMATS),
            local_rx_formats=list(HA_BROWSER_RX_FORMATS),
            peer_tx_formats=_device_formats(target, "tx_formats"),
            peer_rx_formats=_device_formats(target, "rx_formats"),
        )
        result = await session.start()

        if result == "streaming":
            _sessions[device_id] = session
            _LOGGER.debug("Session started (streaming): %s", device_id)
            connection.send_result(msg_id, {"success": True, **_session_audio_payload(session, "streaming")})
        elif result == "ringing":
            _sessions[device_id] = session
            _LOGGER.debug("Session started (ringing): %s", device_id)
            connection.send_result(msg_id, {"success": True, **_session_audio_payload(session, "ringing")})
        else:
            _LOGGER.error("Session failed: %s", device_id)
            connection.send_error(msg_id, "connection_failed", f"Failed to connect to {host}")
    except Exception as err:
        _LOGGER.exception("Start exception: %s", err)
        connection.send_error(msg_id, "exception", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_HA_SOFTPHONE_START,
        vol.Required("target_device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_ha_softphone_start(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Start a browser/HA softphone call to an ESP endpoint."""
    msg_id = msg["id"]
    target_device_id = msg["target_device_id"]

    if _sessions:
        connection.send_error(msg_id, "busy", "HA softphone already has an active call")
        return

    target = next(
        (d for d in await _get_intercom_devices(hass) if d.get("device_id") == target_device_id),
        None,
    )
    if target is None or not target.get("host"):
        connection.send_error(msg_id, "not_found", f"Target device not available: {target_device_id}")
        return

    session = IntercomSession(
        hass=hass,
        device_id=HA_SOFTPHONE_DEVICE_ID,
        host=target["host"],
        transport_type=configured_transport_type(hass, target["host"]),
        audio_mode=target.get("audio_mode", "full_duplex"),
        local_tx_formats=list(HA_BROWSER_TX_FORMATS),
        local_rx_formats=list(HA_BROWSER_RX_FORMATS),
        peer_tx_formats=_device_formats(target, "tx_formats"),
        peer_rx_formats=_device_formats(target, "rx_formats"),
    )
    result = await session.start()
    if result in ("streaming", "ringing"):
        _sessions[HA_SOFTPHONE_DEVICE_ID] = session
        _set_ha_softphone_call_state(
            hass,
            result,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            peer_name=target.get("name") or "",
            target_device_id=target_device_id,
        )
        connection.send_result(msg_id, {"success": True, **_session_audio_payload(session, result)})
        return

    connection.send_error(msg_id, "connection_failed", f"Failed to connect to {target.get('name') or target_device_id}")


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_HA_SOFTPHONE_STATE,
    }
)
@websocket_api.async_response
async def websocket_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    connection.send_result(msg["id"], _ha_softphone_state(hass))


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_HA_SOFTPHONE_DND,
        vol.Required("dnd"): bool,
    }
)
@websocket_api.async_response
async def websocket_set_ha_softphone_dnd(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    store = _ha_softphone_store(hass)
    store["dnd"] = bool(msg["dnd"])
    state = _ha_softphone_state(hass)
    _fire_call_event(hass, state, "session")
    connection.send_result(msg["id"], state)


def _find_bridge_by_source(source_device_id: str) -> "BridgeSession | None":
    """Find an active bridge where the given device is the source (caller)."""
    for bridge in _bridges.values():
        if bridge.source_device_id == source_device_id and bridge._active:
            return bridge
    return None


async def _async_shutdown_all() -> None:
    """Best-effort teardown of sessions / bridges / subscribers on unload."""
    for device_id in list(_sessions.keys()):
        session = _sessions.pop(device_id, None)
        if session is None:
            continue
        try:
            await session.stop(send_signaling=False)
        except Exception:
            _LOGGER.exception("Session stop on unload failed for %s", device_id)

    for bridge_id in list(_bridges.keys()):
        bridge = _bridges.pop(bridge_id, None)
        if bridge is None:
            continue
        try:
            await bridge.stop(send_signaling=False)
        except Exception:
            _LOGGER.exception("Bridge stop on unload failed for %s", bridge_id)

def _device_state(hass: HomeAssistant, device: dict) -> str:
    """Return the mirrored ESP intercom_state for stop recovery decisions."""
    state_eid = (device.get("entities") or {}).get("intercom_state")
    if not state_eid:
        return ""
    state = hass.states.get(state_eid)
    return (state.state if state is not None else "").lower()


def _device_caller_name(hass: HomeAssistant, device: dict) -> str:
    caller_eid = (device.get("entities") or {}).get("incoming_caller")
    if not caller_eid:
        return ""
    state = hass.states.get(caller_eid)
    value = (state.state if state is not None else "").strip()
    return "" if value.lower() in ("", "unknown", "unavailable") else value


def _is_ha_peer_name(hass: HomeAssistant, name: str) -> bool:
    wanted = (name or "").strip().lower()
    ha_name = (hass.config.location_name or "").strip().lower() or HA_PEER_FALLBACK_NAME.lower()
    return wanted in ("home assistant", ha_name)


def _is_direct_esp_incoming(hass: HomeAssistant, device: dict) -> bool:
    state = _device_state(hass, device)
    if state not in ("ringing", "incoming"):
        return False
    caller = _device_caller_name(hass, device)
    return bool(caller and not _is_ha_peer_name(hass, caller))


async def _press_esp_call_button(hass: HomeAssistant, device: dict) -> bool:
    button_eid = (device.get("entities") or {}).get("call")
    if not button_eid:
        _LOGGER.warning("Cannot answer %s: no call button entity", device.get("name"))
        return False
    try:
        await hass.services.async_call("button", "press", {"entity_id": button_eid}, blocking=True)
        _LOGGER.info("Pressed %s to answer mirrored ESP call on %s", button_eid, device.get("name"))
        return True
    except Exception:
        _LOGGER.exception("Failed pressing %s to answer %s", button_eid, device.get("name"))
        return False


async def _force_esp_stop_from_state(hass: HomeAssistant, device_id: str) -> bool:
    """Force an ESP to leave its local call FSM when HA has no live session.

    Browser/card reloads can leave HA with no IntercomSession/BridgeSession
    while the ESP is still OUTGOING/RINGING/STREAMING. Use the decline
    button as the idempotent local teardown command: on RINGING it declines,
    on OUTGOING it cancels, on STREAMING firmware maps it to stop(), and on
    an already-idle ESP it is a no-op. Do not press the call button here:
    call is a toggle and a stale mirrored state could start a new call.
    """
    device = next(
        (d for d in await _get_intercom_devices(hass) if d.get("device_id") == device_id),
        None,
    )
    if device is None:
        _LOGGER.warning("Force-stop requested for unknown intercom device_id=%s", device_id)
        return False

    state = _device_state(hass, device)
    if state in ("", "idle", "unknown", "unavailable"):
        _LOGGER.debug("Force-stop skipped for %s: mirrored state=%s",
                      device.get("name"), state or "(empty)")
        return False

    entities = device.get("entities") or {}
    button_eid = entities.get("decline")
    if not button_eid:
        _LOGGER.warning(
            "Force-stop cannot act on %s: no decline button entity (state=%s)",
            device.get("name"), state,
        )
        return False

    try:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": button_eid},
            blocking=True,
        )
        _LOGGER.info(
            "Force-stop pressed %s for %s (mirrored_state=%s)",
            button_eid, device.get("name"), state,
        )
        return True
    except Exception:
        _LOGGER.exception(
            "Force-stop failed pressing %s for %s (mirrored_state=%s)",
            button_eid, device.get("name"), state,
        )
        return False


async def _stop_device_sessions(
    device_id: str,
    hass: HomeAssistant | None = None,
    *,
    force_esp: bool = True,
) -> bool:
    """Stop sessions/bridges involving `device_id`. True if any stop action ran.

    If HA has no live session/bridge but the device state says it is in a call,
    force the ESP-side FSM through its own buttons. That makes HANGUP a
    recovery command, not just a cleanup for HA-owned sessions.
    """
    stopped = False

    session = _sessions.pop(device_id, None)
    if session:
        await session.stop()
        _LOGGER.debug("Session stopped: %s", device_id)
        stopped = True

    # Pass local_origin so the bridge labels the ended screen "Local
    # hangup" instead of bare "Disconnected".
    bridges_to_stop = [
        (bid, bridge) for bid, bridge in _bridges.items()
        if bridge.source_device_id == device_id or bridge.dest_device_id == device_id
    ]
    for bridge_id, bridge in bridges_to_stop:
        if bridge.source_device_id == device_id:
            origin = "source"
        elif bridge.dest_device_id == device_id:
            origin = "dest"
        else:
            origin = None

        # When the card mirrors a real ESP in a bridge, hangup must behave like
        # pressing that ESP's own hangup/decline button. If HA tears the bridge
        # down directly, it sends HANGUP back to the local leg and the ESP records
        # a misleading remote_hangup. Let the local ESP emit the terminal signal
        # first; the bridge callback will propagate it to the other leg.
        if origin in ("source", "dest") and force_esp and hass is not None:
            if await _force_esp_stop_from_state(hass, device_id):
                stopped = True
                for _ in range(10):
                    if bridge_id not in _bridges or bridge._stopping or bridge._terminal_fired:
                        break
                    await asyncio.sleep(0.1)
                if bridge_id not in _bridges:
                    continue

        if _bridges.pop(bridge_id, None) is None:
            continue
        await bridge.stop(local_origin=origin)
        _LOGGER.debug("Bridge stopped for device %s (origin=%s): %s",
                      device_id, origin, bridge_id)
        stopped = True

    if not stopped and force_esp and hass is not None:
        if device_id == HA_SOFTPHONE_DEVICE_ID:
            return False
        stopped = await _force_esp_stop_from_state(hass, device_id)

    return stopped


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_STOP,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_stop(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Stop intercom session or bridge involving this device."""
    device_id = msg["device_id"]
    msg_id = msg["id"]

    _LOGGER.debug("websocket_stop called: device_id=%s", device_id)

    stopped = await _stop_device_sessions(device_id, hass=hass)
    connection.send_result(msg_id, {"success": True, "stopped": stopped})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_ANSWER,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_answer(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Answer a ringing call (send ANSWER to ESP)."""
    device_id = msg["device_id"]
    msg_id = msg["id"]

    # First check P2P sessions
    session = _sessions.get(device_id)
    if session:
        result = await session.answer()
        if result:
            connection.send_result(msg_id, {"success": True, **_session_audio_payload(session, "streaming")})
        else:
            connection.send_error(msg_id, "error", "Failed to send answer")
        return

    # Check bridges - device_id might be the dest (callee) of a bridge
    for bridge in _bridges.values():
        if bridge.dest_device_id == device_id:
            result = await bridge.answer_dest()
            if result:
                connection.send_result(msg_id, {"success": True})
            else:
                connection.send_error(msg_id, "error", "Failed to send answer to bridge dest")
            return

    # Direct ESP->ESP calls are not HA sessions/bridges. If the mirrored ESP
    # says it is ringing from another ESP, answer through its real Call button.
    device = next(
        (d for d in await _get_intercom_devices(hass) if d.get("device_id") == device_id),
        None,
    )
    if device is not None and _is_direct_esp_incoming(hass, device):
        if await _press_esp_call_button(hass, device):
            connection.send_result(msg_id, {"success": True, "mode": "mirror"})
        else:
            connection.send_error(msg_id, "error", "Failed to press ESP call button")
        return

    connection.send_error(msg_id, "not_found", f"No session or bridge for {device_id}")


WS_TYPE_ANSWER_ESP_CALL = f"{DOMAIN}/answer_esp_call"


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_ANSWER_ESP_CALL,
        vol.Required("device_id"): str,
        vol.Required("host"): str,
    }
)
@websocket_api.async_response
async def websocket_answer_esp_call(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Answer an ESP -> HA call. Reuses any ringing session from the
    unsolicited handler; otherwise opens a fresh one and sends ANSWER."""
    device_id = msg["device_id"]
    host = msg["host"]
    msg_id = msg["id"]

    _LOGGER.debug("Answer ESP call: device=%s host=%s", device_id, host)

    try:
        target = next(
            (d for d in await _get_intercom_devices(hass) if d.get("device_id") == device_id),
            None,
        )
        # Reuse any ringing session created by the unsolicited handler;
        # rebuilding the transport would race the consumer registry and
        # drop the inbound PONG.
        existing = _sessions.get(device_id)
        if existing is not None and existing.is_ringing:
            _LOGGER.debug("Reusing ringing session for %s", device_id)
            ok = await existing.answer()
            if ok:
                _LOGGER.info("Answered ESP call via existing ringing session: %s", device_id)
                connection.send_result(msg_id, {"success": True, **_session_audio_payload(existing, "streaming")})
            else:
                _LOGGER.error("Failed to answer (existing ringing session): %s", device_id)
                connection.send_error(msg_id, "connection_failed", f"Failed to connect to {host}")
            return

        # No ringing session: rare (card opened before any MSG_START).
        if device_id in _sessions:
            old_session = _sessions.pop(device_id)
            await old_session.stop()

        session = IntercomSession(
            hass=hass,
            device_id=device_id,
            host=host,
            transport_type=configured_transport_type(hass, host),
            audio_mode=(target or {}).get("audio_mode") or await _device_audio_mode(hass, device_id),
            local_tx_formats=list(HA_BROWSER_TX_FORMATS),
            local_rx_formats=list(HA_BROWSER_RX_FORMATS),
            peer_tx_formats=_device_formats(target, "tx_formats"),
            peer_rx_formats=_device_formats(target, "rx_formats"),
        )
        result = await session.answer_esp_call()

        if result == "streaming":
            _sessions[device_id] = session
            _LOGGER.info("Answered ESP call (streaming): %s", device_id)
            connection.send_result(msg_id, {"success": True, **_session_audio_payload(session, "streaming")})
        else:
            _LOGGER.error("Failed to answer ESP call: %s", device_id)
            connection.send_error(msg_id, "connection_failed", f"Failed to connect to {host}")
    except Exception as err:
        _LOGGER.exception("Answer ESP call exception: %s", err)
        connection.send_error(msg_id, "exception", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_LIST,
    }
)
@websocket_api.async_response
async def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """List ESPHome devices with intercom capability."""
    devices = [_ha_softphone_device(hass), *(await _get_intercom_devices(hass))]
    connection.send_result(msg["id"], {"devices": devices})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_RESOLVE_DEVICE,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_resolve_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    selector = msg.get("device_id") or ""
    if selector == HA_SOFTPHONE_DEVICE_ID:
        connection.send_result(msg["id"], {"device": _ha_softphone_device(hass)})
        return
    from .device_resolver import get_resolver
    device = await get_resolver(hass).resolve_selector(selector)
    connection.send_result(msg["id"], {"device": device})


def _ha_softphone_device(hass: HomeAssistant) -> dict[str, Any]:
    """Synthetic HA endpoint for external protocol callers."""
    name = (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME
    return {
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "name": name,
        "route_id": name,
        "host": "",
        "transport": "ha",
        "audio_mode": "full_duplex",
        "tx_formats": [fmt.wire_token() for fmt in HA_BROWSER_TX_FORMATS],
        "rx_formats": [fmt.wire_token() for fmt in HA_BROWSER_RX_FORMATS],
        "esphome_id": "",
        "entities": {},
        "softphone": True,
    }


async def _get_intercom_devices(hass: HomeAssistant) -> list:
    """Thin wrapper over IntercomDeviceResolver."""
    from .device_resolver import get_resolver
    return await get_resolver(hass).list_devices()


WS_TYPE_DECLINE = f"{DOMAIN}/decline"


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DECLINE,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_decline(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Decline / stop any session or bridge involving this device."""
    device_id = msg["device_id"]
    msg_id = msg["id"]

    _LOGGER.debug("Decline request for device: %s", device_id)

    stopped = await _stop_device_sessions(device_id, hass=hass)
    connection.send_result(msg_id, {"success": True, "stopped": stopped})
