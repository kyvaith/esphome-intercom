"""Typed peer model for roster/routing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Peer:
    """One routable intercom endpoint known to Home Assistant."""

    kind: Literal["esp", "ha"]
    name: str
    host: str
    transport: Literal["tcp", "udp"]
    tcp_port: int
    udp_audio_port: int
    udp_control_port: int
    audio_mode: Literal["full_duplex", "mic_only", "speaker_only", "control_only"] = "full_duplex"
    tx_formats: list[str] | None = None
    rx_formats: list[str] | None = None
    device: dict[str, Any] | None = None

    @property
    def is_ha(self) -> bool:
        return self.kind == "ha"

    @property
    def device_id(self) -> str | None:
        if self.device is None:
            return None
        return self.device.get("device_id")
