"""PCM frame conversion helpers for HA-side intercom bridging."""

from __future__ import annotations

from .audio_format import AudioFormat, PcmFormat


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def _decode_sample(data: bytes, offset: int, fmt: PcmFormat) -> float:
    if fmt is PcmFormat.S16LE:
        return int.from_bytes(data[offset:offset + 2], "little", signed=True) / 32768.0
    if fmt is PcmFormat.S24LE:
        return _sign_extend(int.from_bytes(data[offset:offset + 3], "little"), 24) / 8388608.0
    if fmt is PcmFormat.S24LE_IN_S32:
        return (int.from_bytes(data[offset:offset + 4], "little", signed=True) >> 8) / 8388608.0
    return int.from_bytes(data[offset:offset + 4], "little", signed=True) / 2147483648.0


def _encode_sample(sample: float, fmt: PcmFormat) -> bytes:
    sample = max(-1.0, min(1.0, sample))
    if fmt is PcmFormat.S16LE:
        value = int(sample * (32768 if sample < 0 else 32767))
        return value.to_bytes(2, "little", signed=True)
    if fmt is PcmFormat.S24LE:
        value = int(sample * (8388608 if sample < 0 else 8388607))
        return (value & 0xFFFFFF).to_bytes(3, "little")
    if fmt is PcmFormat.S24LE_IN_S32:
        value = int(sample * (8388608 if sample < 0 else 8388607)) << 8
        return value.to_bytes(4, "little", signed=True)
    value = int(sample * (2147483648 if sample < 0 else 2147483647))
    return value.to_bytes(4, "little", signed=True)


def _decode_frame(data: bytes, fmt: AudioFormat) -> list[list[float]]:
    stride = fmt.container_bytes_per_sample * fmt.channels
    if len(data) % stride:
        raise ValueError(
            f"audio frame length {len(data)} is not aligned to {fmt.wire_token()}"
        )
    frames = len(data) // stride
    channels = [[] for _ in range(fmt.channels)]
    offset = 0
    for _ in range(frames):
        for channel in range(fmt.channels):
            channels[channel].append(_decode_sample(data, offset, fmt.pcm_format))
            offset += fmt.container_bytes_per_sample
    return channels


def _map_channels(channels: list[list[float]], out_channels: int) -> list[list[float]]:
    if out_channels == 1 and len(channels) > 1:
        return [[sum(frame) / len(channels) for frame in zip(*channels)]]
    if out_channels <= len(channels):
        return channels[:out_channels]
    return channels + [channels[-1]] * (out_channels - len(channels))


def _resample_channel(samples: list[float], out_frames: int, src_rate: int, dst_rate: int) -> list[float]:
    if not samples or out_frames <= 0:
        return [0.0] * max(0, out_frames)
    if len(samples) == out_frames:
        return samples
    ratio = src_rate / dst_rate
    out: list[float] = []
    for index in range(out_frames):
        pos = index * ratio
        left = int(pos)
        if left >= len(samples):
            out.append(samples[-1])
            continue
        right = min(left + 1, len(samples) - 1)
        frac = pos - left
        out.append(samples[left] + (samples[right] - samples[left]) * frac)
    return out


def _encode_frame(channels: list[list[float]], dst: AudioFormat) -> bytes:
    out_frames = len(channels[0]) if channels else 0
    out = bytearray(out_frames * dst.channels * dst.container_bytes_per_sample)
    offset = 0
    for frame in range(out_frames):
        for channel in range(dst.channels):
            encoded = _encode_sample(channels[channel][frame], dst.pcm_format)
            out[offset:offset + len(encoded)] = encoded
            offset += len(encoded)
    return bytes(out)


def convert_audio_frame(data: bytes, src: AudioFormat, dst: AudioFormat) -> bytes:
    """Convert one PCM frame between negotiated intercom formats.

    Matching formats return the original bytes. Otherwise HA performs explicit
    channel mapping, linear sample-rate conversion and PCM container conversion.
    """
    if src == dst:
        return data
    if src.frame_ms != dst.frame_ms:
        raise ValueError(
            f"frame_ms conversion requires stateful reframing: {src.wire_token()} -> {dst.wire_token()}"
        )
    src_channels = _map_channels(_decode_frame(data, src), dst.channels)
    out_frames = dst.nominal_frame_samples
    resampled = [
        _resample_channel(channel, out_frames, src.sample_rate, dst.sample_rate)
        for channel in src_channels
    ]
    return _encode_frame(resampled, dst)


class PcmFrameConverter:
    """Stateful PCM converter that also reframes differing frame durations."""

    def __init__(self, src: AudioFormat, dst: AudioFormat) -> None:
        self.src = src
        self.dst = dst
        self._pending = [[] for _ in range(dst.channels)]

    def convert(self, data: bytes) -> list[bytes]:
        if len(data) != self.src.nominal_frame_bytes:
            raise ValueError(
                f"audio frame length {len(data)} does not match {self.src.wire_token()} "
                f"({self.src.nominal_frame_bytes} bytes)"
            )
        if self.src == self.dst:
            return [data]

        src_channels = _map_channels(_decode_frame(data, self.src), self.dst.channels)
        chunk_frames = (self.src.frame_ms * self.dst.sample_rate) // 1000
        if self.src.frame_ms * self.dst.sample_rate % 1000:
            raise ValueError(
                f"{self.src.wire_token()} cannot be reframed exactly at {self.dst.sample_rate} Hz"
            )
        resampled = [
            _resample_channel(channel, chunk_frames, self.src.sample_rate, self.dst.sample_rate)
            for channel in src_channels
        ]
        for channel, samples in enumerate(resampled):
            self._pending[channel].extend(samples)

        out: list[bytes] = []
        frame_samples = self.dst.nominal_frame_samples
        while len(self._pending[0]) >= frame_samples:
            frame_channels = [channel[:frame_samples] for channel in self._pending]
            out.append(_encode_frame(frame_channels, self.dst))
            for channel in self._pending:
                del channel[:frame_samples]
        return out
