"""HA-side TCP listener for inbound PBX-lite calls. Symmetric to
udp_socket_manager.IntercomUdpSocketManager."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from homeassistant.core import HomeAssistant

from . import protocol
from .const import DOMAIN, HEADER_SIZE, INTERCOM_PORT, MSG_START
from .tcp_client import IntercomTcpClient

_LOGGER = logging.getLogger(__name__)

# Newly accepted sockets must send MSG_START immediately; anything
# slower is either a probe or a scanner. Keep the bound tight.
FIRST_FRAME_TIMEOUT_S = 2.0


UnsolicitedTcpCallback = Callable[[
    str,          # caller_name
    str,          # caller_route
    str,          # dest_name
    str,          # dest_route
    str,          # call_id
    str,          # host (peer IP)
    IntercomTcpClient,  # adopted transport, already connected
    list,         # caller_tx_formats
    list,         # caller_rx_formats
], Awaitable[None]]


class IntercomTcpSocketManager:
    """Single per-HA TCP listener; parses the first MSG_START and hands
    the wired transport to the unsolicited callback."""

    def __init__(self, hass: HomeAssistant, port: int = INTERCOM_PORT) -> None:
        self.hass = hass
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._unsolicited_callback: Optional[UnsolicitedTcpCallback] = None

    def set_unsolicited_callback(self, callback: UnsolicitedTcpCallback) -> None:
        self._unsolicited_callback = callback

    async def start(self) -> bool:
        """Bind and start the accept loop. Returns False on bind failure."""
        if self._server is not None:
            return True
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host="0.0.0.0",
                port=self.port,
                reuse_address=True,
            )
        except OSError as err:
            _LOGGER.error(
                "TCP listener bind on port %d failed: %s",
                self.port, err,
            )
            self._server = None
            return False
        _LOGGER.info("TCP listener bound on 0.0.0.0:%d", self.port)
        return True

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        _LOGGER.info("TCP listener stopped")

    async def _read_first_start(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
    ) -> dict[str, str] | None:
        """Read and parse the required first MSG_START frame."""
        header = await self._read_first_frame_header(reader, writer, host)
        if header is None:
            return None

        try:
            msg_type, length = protocol.parse_header(header)
        except ValueError:
            _LOGGER.warning("TCP from %s: malformed header", host)
            self._abort(writer)
            return None

        if length > protocol.MAX_PAYLOAD_SIZE:
            _LOGGER.warning(
                "TCP from %s: declared body %d > MAX_PAYLOAD_SIZE %d, dropping",
                host, length, protocol.MAX_PAYLOAD_SIZE,
            )
            self._abort(writer)
            return None

        payload = await self._read_first_frame_payload(reader, writer, host, length)
        if payload is None:
            return None

        if msg_type != MSG_START:
            _LOGGER.warning(
                "TCP from %s: first frame is type=0x%02X, expected MSG_START - dropping",
                host, msg_type,
            )
            self._abort(writer)
            return None

        try:
            return protocol.parse_start_body(payload)
        except ValueError as err:
            _LOGGER.warning("TCP from %s: malformed MSG_START body: %s", host, err)
            self._abort(writer)
            return None

    async def _read_first_frame_header(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
    ) -> bytes | None:
        try:
            return await asyncio.wait_for(
                reader.readexactly(HEADER_SIZE),
                timeout=FIRST_FRAME_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            _LOGGER.debug(
                "TCP from %s: no first frame within %.1fs, dropping",
                host,
                FIRST_FRAME_TIMEOUT_S,
            )
        except Exception as err:
            _LOGGER.debug("TCP from %s: header read error: %s", host, err)
        self._abort(writer)
        return None

    async def _read_first_frame_payload(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        length: int,
    ) -> bytes | None:
        try:
            # readexactly(0) returns b"" without I/O, so the path is
            # uniform regardless of body length.
            return await asyncio.wait_for(
                reader.readexactly(length),
                timeout=FIRST_FRAME_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            _LOGGER.debug("TCP from %s: incomplete first frame body", host)
            self._abort(writer)
            return None

    def _adopt_transport(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        parsed: dict[str, str],
    ) -> IntercomTcpClient:
        """Turn the accepted socket into the transport used by routing."""
        transport = IntercomTcpClient(hass=self.hass, host=host, port=self.port)
        transport.adopt(reader, writer)
        transport.set_call_context(
            parsed.get("call_id") or "",
            parsed.get("caller_name") or "",
        )
        transport.peer_tx_formats = parsed.get("caller_tx_formats") or transport.peer_tx_formats
        transport.peer_rx_formats = parsed.get("caller_rx_formats") or transport.peer_rx_formats
        _LOGGER.debug(
            "[TCP listener] adopted socket from %s as transport#%d "
            "(caller=%s call_id=%s)",
            host,
            transport._instance_id,
            parsed.get("caller_name") or "(unknown)",
            parsed.get("call_id") or "(none)",
        )
        return transport

    async def _dispatch_unsolicited_start(
        self,
        parsed: dict[str, str],
        host: str,
        transport: IntercomTcpClient,
    ) -> None:
        try:
            await self._unsolicited_callback(
                parsed["caller_name"],
                parsed["caller_route"],
                parsed["dest_name"],
                parsed["dest_route"],
                parsed["call_id"],
                host,
                transport,
                parsed.get("caller_tx_formats") or [],
                parsed.get("caller_rx_formats") or [],
            )
        except Exception:
            _LOGGER.exception(
                "TCP from %s: unsolicited callback raised, tearing down transport",
                host,
            )
            try:
                await transport.disconnect()
            except Exception:
                pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read the first frame, dispatch via the unsolicited callback."""
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else ""

        parsed = await self._read_first_start(reader, writer, host)
        if parsed is None:
            return

        if self._unsolicited_callback is None:
            _LOGGER.warning(
                "TCP from %s: MSG_START received but no callback registered, dropping",
                host,
            )
            self._abort(writer)
            return

        # Adopt the live socket; routing wires real callbacks before the
        # recv loop processes the next frame.
        transport = self._adopt_transport(reader, writer, host, parsed)
        await self._dispatch_unsolicited_start(parsed, host, transport)

    @staticmethod
    def _abort(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
        except Exception:
            pass


def get_manager(hass: HomeAssistant) -> Optional[IntercomTcpSocketManager]:
    return hass.data.get(DOMAIN, {}).get("tcp_manager")


def get_tcp_manager(hass: HomeAssistant) -> Optional[IntercomTcpSocketManager]:
    return get_manager(hass)
