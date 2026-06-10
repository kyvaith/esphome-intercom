"""Async UDP transport. Per-host consumer on the shared
IntercomUdpSocketManager; send_*() delegate to the manager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from homeassistant.core import HomeAssistant

from .const import (
    MSG_ANSWER,
    MSG_DECLINE,
    MSG_PING,
    MSG_PONG,
    MSG_RING,
    MSG_START,
    MSG_HANGUP,
    PING_INTERVAL,
)
from . import protocol
from .transport_base import IntercomTransport
from .udp_socket_manager import IntercomUdpSocketManager, _Consumer, get_manager

_LOGGER = logging.getLogger(__name__)

_ACK_RETRY_INTERVAL = 0.2
_ACK_RETRY_ATTEMPTS = 3


class IntercomUdpClient(IntercomTransport):
    """UDP per-peer transport. Sockets live in the shared manager."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        on_ringing: Optional[Callable[[], None]] = None,
        on_answered: Optional[Callable[[], None]] = None,
        on_stop_received: Optional[Callable[[], None]] = None,
        on_decline_received: Optional[Callable[[str], None]] = None,
        on_error_received: Optional[Callable[[int, str], None]] = None,
    ) -> None:
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
        self._hass = hass
        self._manager: Optional[IntercomUdpSocketManager] = None
        self._consumer_token: Optional[_Consumer] = None
        self._retry_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_peer_activity = 0.0

    @property
    def transport_name(self) -> str:
        return "udp"

    async def connect(self) -> bool:
        if self._connected:
            return True
        manager = get_manager(self._hass)
        if manager is None:
            _LOGGER.error(
                "[UDP#%d] No UdpSocketManager active (use_udp must be enabled)",
                self._instance_id,
            )
            return False

        self._manager = manager
        self._loop = asyncio.get_running_loop()
        self._consumer_token = manager.register_consumer(
            self.host,
            on_audio=self._handle_audio,
            on_control=self._handle_control,
        )
        self._set_connected(True, "udp_connect")
        self._disconnect_notified = False
        self._last_peer_activity = self._loop.time()
        self._keepalive_task = self._hass.async_create_task(self._keepalive_loop())
        _LOGGER.debug(
            "[UDP#%d] Registered consumer for %s on shared sockets",
            self._instance_id,
            self.host,
        )
        return True

    async def disconnect(self) -> None:
        _LOGGER.debug("[UDP#%d] Disconnecting (host=%s)", self._instance_id, self.host)
        self._set_connected(False, "udp_disconnect")
        self._set_streaming(False, "udp_disconnect")
        self._set_ringing(False, "udp_disconnect")

        self._awaiting_answer_ack = False

        await self._cancel_retry_task()
        await self._cancel_keepalive_task()
        if self._manager is not None:
            self._manager.unregister_consumer(self.host, token=self._consumer_token)
            self._manager = None
            self._consumer_token = None

        if not self._disconnect_notified and self._on_disconnected:
            self._disconnect_notified = True
            self._on_disconnected()

        _LOGGER.debug(
            "[UDP#%d] Disconnected (sent=%d recv=%d)",
            self._instance_id,
            self._audio_sent,
            self._audio_recv,
        )

    async def start_stream(self, caller_name: str = "") -> str:
        _LOGGER.debug(
            "[UDP#%d] start_stream(caller=%s)",
            self._instance_id,
            caller_name or "(none)",
        )
        if not self._connected and not await self.connect():
            return "error"

        self._set_streaming(False, "start_stream_reset")
        self._set_ringing(False, "start_stream_reset")
        self._awaiting_answer_ack = False

        # When HA is the caller, the identity is config.location_name
        # (whatever the user picked for the HA instance).
        ha_name = self._hass.config.location_name or ""
        eff_caller = caller_name or ha_name
        call_id = f"{eff_caller}<->{self.host}"
        body = protocol.build_start_body(
            call_id=call_id,
            caller_route=eff_caller,
            caller_name=eff_caller,
            dest_route="",
            dest_name="",
            caller_tx_formats=self.local_tx_formats,
            caller_rx_formats=self.local_rx_formats,
        )
        self.set_call_context(call_id, eff_caller)
        if not await self.send_control(MSG_START, data=body, retry=True):
            return "error"

        result = await self._wait_for_setup_result(timeout=2.0)
        await self._cancel_retry_task()
        if result is None:
            _LOGGER.warning(
                "[UDP#%d] START timed out waiting for ANSWER/RING/PONG",
                self._instance_id,
            )
            return "error"
        _LOGGER.debug("[UDP#%d] start_stream -> %s", self._instance_id, result)
        return result

    async def stop_stream(self) -> None:
        _LOGGER.debug("[UDP#%d] stop_stream()", self._instance_id)
        self._set_streaming(False, "stop_stream_local")
        self._set_ringing(False, "stop_stream_local")

        self._awaiting_answer_ack = False
        await self._cancel_retry_task()
        if self._connected:
            cid = self._current_call_id() or ""
            body = protocol.build_call_id_only_body(cid)
            await self.send_control(MSG_HANGUP, data=body)

    async def send_answer(self) -> bool:
        _LOGGER.debug("[UDP#%d] send_answer()", self._instance_id)
        if not self._connected:
            return False
        if not self._ringing:
            _LOGGER.warning("[UDP#%d] send_answer() but not ringing", self._instance_id)
            return False
        return await self.send_answer_blind()

    async def send_answer_blind(self) -> bool:
        _LOGGER.debug("[UDP#%d] send_answer_blind()", self._instance_id)
        if not self._connected:
            return False
        self._awaiting_answer_ack = True
        cid = self._current_call_id() or ""
        body = protocol.build_answer_body(
            cid,
            caller_to_dest_format=self.caller_to_dest_format,
            dest_to_caller_format=self.dest_to_caller_format,
        )
        ok = await self.send_control(MSG_ANSWER, data=body, retry=True)
        if ok:
            # Responder side: no inbound ANSWER will flip us via dispatch.
            self._set_streaming(True, "ANSWER_sent_responder")
        else:
            self._awaiting_answer_ack = False
        return ok

    async def send_decline(self, reason: str = "") -> bool:
        if not self._connected:
            return False
        self._awaiting_answer_ack = True
        cid = self._current_call_id() or ""
        body = protocol.build_decline_body(cid, reason)
        ok = await self.send_control(MSG_DECLINE, data=body, retry=True)
        if not ok:
            self._awaiting_answer_ack = False
        return ok

    def _fallback_anchor(self) -> str:
        return self._hass.config.location_name or ""

    async def send_audio(self, data: bytes) -> bool:
        if not self._connected:
            self._track_audio_drop("not_connected", len(data))
            return False
        if not self._streaming:
            self._track_audio_drop("not_streaming", len(data))
            return False
        if self._manager is None:
            self._track_audio_drop("no_manager", len(data))
            return False
        if self._manager.send_audio(self.host, data):
            self._audio_sent += 1
            return True
        self._track_audio_drop("send_failed", len(data))
        return False

    async def send_control(
        self,
        msg_type: int,
        data: bytes = b"",
        retry: bool = False,
    ) -> bool:
        if not self._connected or self._manager is None:
            return False
        if len(data) > protocol.MAX_PAYLOAD_SIZE:
            _LOGGER.error(
                "[UDP#%d] Control payload too large to send: %d > %d",
                self._instance_id,
                len(data),
                protocol.MAX_PAYLOAD_SIZE,
            )
            return False

        packet = protocol.build_frame(msg_type, data)
        if not self._manager.send_control(self.host, packet):
            return False
        _LOGGER.debug(
            "[UDP#%d] Sent control type=0x%02X len=%d to %s",
            self._instance_id, msg_type, len(data), self.host,
        )

        if retry and msg_type in (MSG_START, MSG_ANSWER, MSG_DECLINE):
            await self._start_retry_task(msg_type, packet)
        return True

    async def _send_pong_response(self) -> None:
        await self.send_control(MSG_PONG)

    async def send_ring(self) -> bool:
        if not self._connected:
            return False
        cid = self._current_call_id() or ""
        body = protocol.build_call_id_only_body(cid)
        ok = await self.send_control(MSG_RING, data=body)
        if ok:
            self._set_ringing(True, "RING_sent")
        return ok

    def _handle_audio(self, data: bytes) -> None:
        if not self._connected:
            return
        if self._loop is not None:
            self._last_peer_activity = self._loop.time()
        self._audio_recv += 1
        if self._on_audio:
            self._on_audio(data)

    def _handle_control(self, msg_type: int, payload: bytes) -> None:
        if not self._connected or self._loop is None:
            return
        self._last_peer_activity = self._loop.time()
        # _handle_message is a coroutine; schedule on the session's loop.
        self._loop.create_task(self._handle_message(msg_type, payload))

    async def _start_retry_task(self, msg_type: int, packet: bytes) -> None:
        await self._cancel_retry_task()
        self._retry_task = self._hass.async_create_task(self._retry_signal(msg_type, packet))

    async def _cancel_retry_task(self) -> None:
        if self._retry_task is None:
            return
        self._retry_task.cancel()
        try:
            await self._retry_task
        except asyncio.CancelledError:
            pass
        finally:
            self._retry_task = None

    async def _cancel_keepalive_task(self) -> None:
        if self._keepalive_task is None:
            return
        if asyncio.current_task() is self._keepalive_task:
            self._keepalive_task = None
            return
        self._keepalive_task.cancel()
        try:
            await self._keepalive_task
        except asyncio.CancelledError:
            pass
        finally:
            self._keepalive_task = None

    async def _retry_signal(self, msg_type: int, packet: bytes) -> None:
        try:
            for _ in range(_ACK_RETRY_ATTEMPTS - 1):
                await asyncio.sleep(_ACK_RETRY_INTERVAL)
                if not self._connected or self._manager is None:
                    return
                if msg_type == MSG_START:
                    if self._should_stop_retry(msg_type):
                        return
                elif not self._awaiting_answer_ack:
                    return
                self._manager.send_control(self.host, packet)
                _LOGGER.debug(
                    "[UDP#%d] Retried control type=0x%02X",
                    self._instance_id,
                    msg_type,
                )
            if msg_type in (MSG_ANSWER, MSG_DECLINE):
                self._awaiting_answer_ack = False
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("[UDP#%d] Retry task stopped: %s", self._instance_id, err)

    async def _keepalive_loop(self) -> None:
        deadline = PING_INTERVAL * 3
        try:
            while self._connected:
                await asyncio.sleep(PING_INTERVAL)
                if not self._connected:
                    return
                if not self._streaming:
                    self._last_peer_activity = (
                        self._loop.time() if self._loop is not None else 0.0
                    )
                    continue
                now = self._loop.time() if self._loop is not None else 0.0
                if self._last_peer_activity <= 0:
                    self._last_peer_activity = now
                silence = now - self._last_peer_activity
                if silence > deadline:
                    _LOGGER.warning(
                        "[UDP#%d] Peer keepalive timeout after %.1fs",
                        self._instance_id,
                        silence,
                    )
                    await self.disconnect()
                    return
                await self.send_control(MSG_PING)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("[UDP#%d] Keepalive task stopped: %s", self._instance_id, err)

    def _should_stop_retry(self, msg_type: int) -> bool:
        if not self._connected:
            return True
        if msg_type == MSG_START:
            return self._streaming or self._ringing
        return True
