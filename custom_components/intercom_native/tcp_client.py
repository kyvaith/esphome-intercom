"""TCP transport. Framed `<BH` header + payload on a persistent stream
shared by audio + control. Keepalive lives in _ping_loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from .const import (
    INTERCOM_PORT,
    HEADER_SIZE,
    MSG_AUDIO,
    MSG_START,
    MSG_HANGUP,
    MSG_PING,
    MSG_PONG,
    MSG_RING,
    MSG_ANSWER,
    MSG_DECLINE,
    CONNECT_TIMEOUT,
    PING_INTERVAL,
)
from .transport_base import IntercomTransport
from . import protocol

_LOGGER = logging.getLogger(__name__)


class IntercomTcpClient(IntercomTransport):
    """Async TCP transport. Inherits FSM dispatch from IntercomTransport."""

    def __init__(
        self,
        hass,
        host: str,
        port: int = INTERCOM_PORT,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        on_ringing: Optional[Callable[[], None]] = None,
        on_answered: Optional[Callable[[], None]] = None,
        on_stop_received: Optional[Callable[[], None]] = None,
        on_decline_received: Optional[Callable[[str], None]] = None,
        on_error_received: Optional[Callable[[int, str], None]] = None,
    ):
        super().__init__(
            host=host,
            on_audio=on_audio,
            on_disconnected=on_disconnected,
            on_ringing=on_ringing,
            on_answered=on_answered,
            on_stop_received=on_stop_received,
            on_decline_received=on_decline_received,
            on_error_received=on_error_received,
        )
        # Stored so background tasks register with HA's tracker.
        self._hass = hass
        self.port = port

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._disconnecting = False

        _LOGGER.debug("[TCP#%d] Created for %s:%d", self._instance_id, host, port)

    @property
    def transport_name(self) -> str:
        return "tcp"

    async def connect(self) -> bool:
        if self._connected:
            return True

        try:
            _LOGGER.debug("[TCP#%d] Connecting to %s:%d...", self._instance_id, self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            self._set_connected(True, "tcp_connect")
            self._disconnect_notified = False
            _LOGGER.debug("[TCP#%d] Connected", self._instance_id)

            self._receive_task = self._hass.async_create_task(self._receive_loop())
            self._ping_task = self._hass.async_create_task(self._ping_loop())

            return True

        except asyncio.TimeoutError:
            _LOGGER.error("[TCP#%d] Connection timeout", self._instance_id)
            return False
        except OSError as err:
            _LOGGER.error("[TCP#%d] Connection error: %s", self._instance_id, err)
            return False

    def adopt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Adopt a server-accepted socket; recv/ping loops run identically
        to the connect() path."""
        self._reader = reader
        self._writer = writer
        self._set_connected(True, "tcp_adopt")
        self._disconnect_notified = False
        self._receive_task = self._hass.async_create_task(self._receive_loop())
        self._ping_task = self._hass.async_create_task(self._ping_loop())
        _LOGGER.debug(
            "[TCP#%d] Adopted accepted socket from %s (callbacks set=%s)",
            self._instance_id, self.host, self._on_audio is not None,
        )

    async def disconnect(self) -> None:
        if self._disconnecting:
            return
        self._disconnecting = True
        _LOGGER.debug("[TCP#%d] Disconnecting", self._instance_id)

        try:
            self._set_connected(False, "tcp_disconnect")
            self._set_streaming(False, "tcp_disconnect")
            self._set_ringing(False, "tcp_disconnect")

            self._awaiting_answer_ack = False
            self._disconnect_notified = True

            if self._receive_task:
                self._receive_task.cancel()
                try:
                    await asyncio.wait_for(self._receive_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._receive_task = None

            if self._ping_task:
                self._ping_task.cancel()
                try:
                    await asyncio.wait_for(self._ping_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._ping_task = None

            if self._writer:
                try:
                    self._writer.close()
                    await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
                self._writer = None
                self._reader = None

            _LOGGER.debug("[TCP#%d] Disconnected (sent=%d recv=%d)",
                          self._instance_id, self._audio_sent, self._audio_recv)
        finally:
            self._disconnecting = False

    async def start_stream(self, caller_name: str = "") -> str:
        _LOGGER.debug("[TCP#%d] start_stream(caller=%s)",
                      self._instance_id, caller_name or "(none)")

        if not self._connected:
            if not await self.connect():
                return "error"

        # Reset state before sending START
        self._set_streaming(False, "start_stream_reset")
        self._set_ringing(False, "start_stream_reset")
        self._awaiting_answer_ack = False

        call_id = f"{caller_name or ''}<->{self.host}"
        body = protocol.build_start_body(
            call_id=call_id,
            caller_route=caller_name or "",
            caller_name=caller_name or "",
            dest_route="",
            dest_name="",
            caller_tx_formats=self.local_tx_formats,
            caller_rx_formats=self.local_rx_formats,
        )
        self.set_call_context(call_id, caller_name or "")
        if not await self._write_frame(MSG_START, body):
            return "error"

        result = await self._wait_for_setup_result(timeout=2.0)
        if result is None:
            if not self._connected:
                _LOGGER.error("[TCP#%d] Connection lost while waiting for response", self._instance_id)
            else:
                _LOGGER.warning("[TCP#%d] No ANSWER/RING received for START", self._instance_id)
            return "error"
        _LOGGER.debug("[TCP#%d] start_stream -> %s", self._instance_id, result)
        return result

    async def stop_stream(self) -> None:
        _LOGGER.debug("[TCP#%d] stop_stream()", self._instance_id)
        self._set_streaming(False, "stop_stream_local")

        self._awaiting_answer_ack = False

        if self._connected and self._writer:
            body = protocol.build_call_id_only_body(self._current_call_id())
            try:
                await asyncio.wait_for(self._write_frame(MSG_HANGUP, body), timeout=1.0)
                _LOGGER.debug("[TCP#%d] HANGUP sent", self._instance_id)
            except asyncio.TimeoutError:
                _LOGGER.warning("[TCP#%d] HANGUP timeout", self._instance_id)
            except Exception as err:
                _LOGGER.debug("[TCP#%d] HANGUP error: %s", self._instance_id, err)

    async def send_answer(self) -> bool:
        _LOGGER.debug("[TCP#%d] send_answer()", self._instance_id)

        if not self._connected or not self._writer:
            return False

        if not self._ringing:
            _LOGGER.warning("[TCP#%d] send_answer() but not ringing", self._instance_id)
            return False

        return await self._send_answer_inner()

    async def send_answer_blind(self) -> bool:
        """ANSWER without requiring local _ringing (fresh transport path)."""
        _LOGGER.debug("[TCP#%d] send_answer_blind()", self._instance_id)

        if not self._connected or not self._writer:
            return False

        return await self._send_answer_inner()

    async def _send_answer_inner(self) -> bool:
        body = protocol.build_answer_body(
            self._current_call_id(),
            caller_to_dest_format=self.caller_to_dest_format,
            dest_to_caller_format=self.dest_to_caller_format,
        )
        try:
            await asyncio.wait_for(self._write_frame(MSG_ANSWER, body), timeout=1.0)
            # Responder side: no inbound ANSWER to flip us via dispatch.
            self._set_streaming(True, "ANSWER_sent_responder")
            _LOGGER.debug("[TCP#%d] ANSWER sent", self._instance_id)
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("[TCP#%d] ANSWER timeout", self._instance_id)
            return False
        except Exception as err:
            _LOGGER.error("[TCP#%d] ANSWER error: %s", self._instance_id, err)
            return False

    async def send_decline(self, reason: str = "") -> bool:
        if not self._connected or not self._writer:
            return False
        body = protocol.build_decline_body(self._current_call_id(), reason)
        try:
            await asyncio.wait_for(self._write_frame(MSG_DECLINE, body), timeout=1.0)
            _LOGGER.debug("[TCP#%d] DECLINE sent (%s)", self._instance_id, reason or "(empty)")
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("[TCP#%d] DECLINE timeout", self._instance_id)
            return False
        except Exception as err:
            _LOGGER.error("[TCP#%d] DECLINE error: %s", self._instance_id, err)
            return False


    async def send_audio(self, data: bytes) -> bool:
        if not self._connected:
            self._track_audio_drop("not_connected", len(data))
            return False
        if not self._streaming:
            self._track_audio_drop("not_streaming", len(data))
            return False
        if not self._writer:
            self._track_audio_drop("no_writer", len(data))
            return False

        try:
            self._writer.write(protocol.build_frame(MSG_AUDIO, data))
            await asyncio.wait_for(self._writer.drain(), timeout=0.1)
            self._audio_sent += 1
            return True
        except asyncio.TimeoutError:
            self._track_audio_drop("drain_timeout", len(data))
            return False
        except Exception as err:
            _LOGGER.debug("[TCP#%d] Audio send failed: %s", self._instance_id, err)
            self._track_audio_drop("send_failed", len(data))
            return False

    async def _send_pong_response(self) -> None:
        await self._write_frame(MSG_PONG)

    async def send_ring(self) -> bool:
        if not self._connected or self._writer is None:
            return False
        body = protocol.build_call_id_only_body(self._current_call_id())
        try:
            await asyncio.wait_for(self._write_frame(MSG_RING, body), timeout=1.0)
            self._set_ringing(True, "RING_sent")
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("[TCP#%d] RING timeout", self._instance_id)
            return False

    async def _write_frame(self, msg_type: int, data: bytes = b"") -> bool:
        """Control frame header+payload+drain (audio uses send_audio)."""
        if not self._writer:
            return False
        try:
            self._writer.write(protocol.build_frame(msg_type, data))
            await self._writer.drain()
            return True
        except Exception as err:
            _LOGGER.error("[TCP#%d] Send error: %s", self._instance_id, err)
            return False

    async def _receive_loop(self) -> None:
        _LOGGER.debug("[TCP#%d] Receive loop started", self._instance_id)
        try:
            while self._connected and self._reader:
                # Keepalive is control-plane, not audio-plane. Streaming may
                # be silent under VAD/mute, so give PING/PONG the full
                # 3-interval deadline instead of requiring inbound AUDIO.
                read_timeout = (PING_INTERVAL * 3.0 + 1.0) if self._streaming else 60.0
                header_data = await asyncio.wait_for(
                    self._reader.readexactly(HEADER_SIZE), timeout=read_timeout
                )
                msg_type, length = protocol.parse_header(header_data)

                payload = b""
                if length > 0:
                    if length > protocol.MAX_PAYLOAD_SIZE:
                        _LOGGER.error("[TCP#%d] Protocol desync: bad length %d (max %d), closing",
                                     self._instance_id, length, protocol.MAX_PAYLOAD_SIZE)
                        raise ConnectionError("protocol desync")
                    payload = await asyncio.wait_for(
                        self._reader.readexactly(length), timeout=read_timeout
                    )

                await self._handle_message(msg_type, payload)

        except asyncio.TimeoutError:
            _LOGGER.warning("[TCP#%d] Read timeout (streaming=%s) - connection dead",
                           self._instance_id, self._streaming)
        except asyncio.IncompleteReadError:
            _LOGGER.info("[TCP#%d] Connection closed by peer", self._instance_id)
        except asyncio.CancelledError:
            _LOGGER.debug("[TCP#%d] Receive loop cancelled", self._instance_id)
        except ConnectionError as err:
            _LOGGER.error("[TCP#%d] Connection error: %s", self._instance_id, err)
        except Exception as err:
            _LOGGER.error("[TCP#%d] Receive error: %s", self._instance_id, err)
        finally:
            self._set_connected(False, "receive_loop_exit")
            self._set_streaming(False, "receive_loop_exit")
            if not self._disconnecting and not self._disconnect_notified and self._on_disconnected:
                self._disconnect_notified = True
                self._on_disconnected()

    async def _ping_loop(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(PING_INTERVAL)
                # Keep control liveness independent from audio. ESP handles
                # PING/PONG as transport-level frames, so this is safe during
                # streaming, ringing, and idle.
                if self._connected:
                    await self._write_frame(MSG_PING)
        except asyncio.CancelledError:
            pass
