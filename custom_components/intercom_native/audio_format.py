"""Intercom PCM format contract shared by transports and browser audio."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class PcmFormat(StrEnum):
    S16LE = "s16le"
    S24LE = "s24le"
    S24LE_IN_S32 = "s24le_in_s32"
    S32LE = "s32le"


class PcmFormatId(IntEnum):
    S16LE = 1
    S24LE = 2
    S24LE_IN_S32 = 3
    S32LE = 4


_FORMAT_TO_ID = {
    PcmFormat.S16LE: PcmFormatId.S16LE,
    PcmFormat.S24LE: PcmFormatId.S24LE,
    PcmFormat.S24LE_IN_S32: PcmFormatId.S24LE_IN_S32,
    PcmFormat.S32LE: PcmFormatId.S32LE,
}
_ID_TO_FORMAT = {value: key for key, value in _FORMAT_TO_ID.items()}
_CONTAINER_BYTES = {
    PcmFormat.S16LE: 2,
    PcmFormat.S24LE: 3,
    PcmFormat.S24LE_IN_S32: 4,
    PcmFormat.S32LE: 4,
}
_SIGNIFICANT_BITS = {
    PcmFormat.S16LE: 16,
    PcmFormat.S24LE: 24,
    PcmFormat.S24LE_IN_S32: 24,
    PcmFormat.S32LE: 32,
}

SUPPORTED_SAMPLE_RATES = frozenset({8000, 12000, 16000, 24000, 32000, 44100, 48000})
SUPPORTED_CHANNELS = frozenset({1, 2})
SUPPORTED_FRAME_MS = frozenset({10, 20, 32})
COMMON_FRAME_MS = 20
UDP_SAFE_PAYLOAD_BYTES = 1200


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int = 16000
    pcm_format: PcmFormat = PcmFormat.S16LE
    channels: int = 1
    frame_ms: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(self, "pcm_format", PcmFormat(self.pcm_format))
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(f"unsupported sample_rate {self.sample_rate}")
        if self.channels not in SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported channel count {self.channels}")
        if self.frame_ms not in SUPPORTED_FRAME_MS:
            raise ValueError(f"unsupported frame_ms {self.frame_ms}")
        if (self.sample_rate * self.frame_ms) % 1000 != 0:
            raise ValueError(
                f"sample_rate {self.sample_rate} and frame_ms {self.frame_ms} do not form whole PCM frames"
            )

    @property
    def format_id(self) -> int:
        return int(_FORMAT_TO_ID[self.pcm_format])

    @property
    def significant_bits(self) -> int:
        return _SIGNIFICANT_BITS[self.pcm_format]

    @property
    def container_bytes_per_sample(self) -> int:
        return _CONTAINER_BYTES[self.pcm_format]

    @property
    def nominal_frame_samples(self) -> int:
        return (self.sample_rate * self.frame_ms) // 1000

    @property
    def exact_frame_samples(self) -> bool:
        return (self.sample_rate * self.frame_ms) % 1000 == 0

    @property
    def nominal_frame_bytes(self) -> int:
        return self.nominal_frame_samples * self.channels * self.container_bytes_per_sample

    @property
    def udp_safe(self) -> bool:
        return self.nominal_frame_bytes <= UDP_SAFE_PAYLOAD_BYTES

    def wire_tuple(self) -> tuple[int, int, int, int]:
        return (self.sample_rate, self.format_id, self.channels, self.frame_ms)

    def wire_token(self) -> str:
        return f"{self.sample_rate}:{self.pcm_format.value}:{self.channels}:{self.frame_ms}"


LEGACY_AUDIO_FORMAT = AudioFormat()


def _browser_formats(*, channels: tuple[int, ...]) -> tuple[AudioFormat, ...]:
    formats: list[AudioFormat] = [LEGACY_AUDIO_FORMAT]
    for rate in sorted(SUPPORTED_SAMPLE_RATES):
        for frame_ms in sorted(SUPPORTED_FRAME_MS):
            if (rate * frame_ms) % 1000 != 0:
                continue
            for fmt in PcmFormat:
                for channel_count in channels:
                    candidate = AudioFormat(rate, fmt, channel_count, frame_ms)
                    if candidate not in formats:
                        formats.append(candidate)
    return tuple(formats)


HA_BROWSER_TX_FORMATS = _browser_formats(channels=(1,))
HA_BROWSER_RX_FORMATS = _browser_formats(channels=(1, 2))


def pcm_format_from_id(format_id: int) -> PcmFormat:
    try:
        return _ID_TO_FORMAT[PcmFormatId(format_id)]
    except (KeyError, ValueError) as err:
        raise ValueError(f"unsupported pcm_format id {format_id}") from err


def audio_format_from_wire(
    sample_rate: int,
    format_id: int,
    channels: int,
    frame_ms: int,
) -> AudioFormat:
    return AudioFormat(
        sample_rate=sample_rate,
        pcm_format=pcm_format_from_id(format_id),
        channels=channels,
        frame_ms=frame_ms,
    )


def parse_audio_format_token(token: str | None) -> AudioFormat:
    if not token:
        return LEGACY_AUDIO_FORMAT
    parts = [part.strip() for part in token.split(":")]
    if len(parts) != 4:
        raise ValueError(f"invalid audio format token '{token}'")
    sample_rate, pcm, channels, frame_ms = parts
    return AudioFormat(
        sample_rate=int(sample_rate),
        pcm_format=PcmFormat(pcm),
        channels=int(channels),
        frame_ms=int(frame_ms),
    )


def parse_audio_format_list(value: str | None) -> list[AudioFormat]:
    if not value:
        return [LEGACY_AUDIO_FORMAT]
    formats = [parse_audio_format_token(part.strip()) for part in value.split(";") if part.strip()]
    if not formats:
        return [LEGACY_AUDIO_FORMAT]
    if len(formats) > 8:
        raise ValueError("too many audio formats (max 8)")
    return formats


def require_udp_safe_formats(formats: list[AudioFormat], *, context: str) -> list[AudioFormat]:
    oversized = [fmt for fmt in formats if not fmt.udp_safe]
    if oversized:
        examples = ", ".join(f"{fmt.wire_token()} ({fmt.nominal_frame_bytes} bytes)" for fmt in oversized[:3])
        raise ValueError(
            f"{context} contains UDP audio frames above {UDP_SAFE_PAYLOAD_BYTES} bytes: {examples}; "
            "use TCP or lower sample_rate/channels/bit depth/frame_ms"
        )
    return formats


def choose_common_format(preferred: list[AudioFormat], supported: list[AudioFormat]) -> AudioFormat | None:
    supported_set = set(supported)
    for fmt in preferred:
        if fmt in supported_set:
            return fmt
    return None
