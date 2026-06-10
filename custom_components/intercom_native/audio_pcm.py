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


def _resample_channel(samples: list[float], out_frames: int) -> list[float]:
    if not samples or out_frames <= 0:
        return [0.0] * max(0, out_frames)
    if len(samples) == out_frames:
        return samples
    if out_frames == 1:
        return [samples[0]]
    scale = (len(samples) - 1) / (out_frames - 1)
    out: list[float] = []
    for index in range(out_frames):
        pos = index * scale
        left = int(pos)
        right = min(left + 1, len(samples) - 1)
        frac = pos - left
        out.append(samples[left] + (samples[right] - samples[left]) * frac)
    return out


def convert_audio_frame(data: bytes, src: AudioFormat, dst: AudioFormat) -> bytes:
    """Convert one PCM frame between negotiated intercom formats.

    Matching formats return the original bytes. Otherwise HA performs explicit
    channel mapping, linear sample-rate conversion and PCM container conversion.
    """
    if src == dst:
        return data
    src_channels = _decode_frame(data, src)
    out_frames = dst.nominal_frame_samples
    resampled = [_resample_channel(channel, out_frames) for channel in src_channels]
    out = bytearray(out_frames * dst.channels * dst.container_bytes_per_sample)
    offset = 0
    for frame in range(out_frames):
        for channel in range(dst.channels):
            sample = resampled[min(channel, len(resampled) - 1)][frame]
            encoded = _encode_sample(sample, dst.pcm_format)
            out[offset:offset + len(encoded)] = encoded
            offset += len(encoded)
    return bytes(out)
