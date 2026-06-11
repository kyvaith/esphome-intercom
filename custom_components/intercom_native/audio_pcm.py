"""PCM frame conversion between negotiated intercom audio formats.

The HA bridge may need to convert between two ESP/browser legs with different
PCM formats. The implementation keeps decode/encode vectorized and uses a
stateful anti-aliased rational resampler so downsampling does not fold content
above the destination Nyquist frequency back into the audible band.
"""

from __future__ import annotations

from math import gcd

import numpy as np

from .audio_format import AudioFormat, PcmFormat

_RESAMPLER_TAPS_PER_PHASE = 24
_KAISER_BETA = 9.0
_ROLLOFF = 0.945


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def _decode_sample(data: bytes, offset: int, fmt: PcmFormat) -> float:
    """Scalar decode kept for tests and debug tooling; streams use _decode_frame."""
    if fmt is PcmFormat.S16LE:
        return int.from_bytes(data[offset:offset + 2], "little", signed=True) / 32768.0
    if fmt is PcmFormat.S24LE:
        return _sign_extend(int.from_bytes(data[offset:offset + 3], "little"), 24) / 8388608.0
    if fmt is PcmFormat.S24LE_IN_S32:
        return (int.from_bytes(data[offset:offset + 4], "little", signed=True) >> 8) / 8388608.0
    return int.from_bytes(data[offset:offset + 4], "little", signed=True) / 2147483648.0


def _encode_sample(sample: float, fmt: PcmFormat) -> bytes:
    """Scalar encode kept for tests and debug tooling; streams use _encode_frame."""
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


def _decode_frame(data: bytes, fmt: AudioFormat) -> np.ndarray:
    """Decode PCM bytes to a float64 array shaped (channels, samples)."""
    stride = fmt.container_bytes_per_sample * fmt.channels
    if len(data) % stride:
        raise ValueError(
            f"audio frame length {len(data)} is not aligned to {fmt.wire_token()}"
        )
    if fmt.pcm_format is PcmFormat.S16LE:
        flat = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    elif fmt.pcm_format is PcmFormat.S24LE:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        value = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int8).astype(np.int32) << 16)
        )
        flat = value.astype(np.float64) / 8388608.0
    elif fmt.pcm_format is PcmFormat.S24LE_IN_S32:
        flat = (np.frombuffer(data, dtype="<i4") >> 8).astype(np.float64) / 8388608.0
    else:
        flat = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    return flat.reshape(-1, fmt.channels).T


def _encode_frame(channels: np.ndarray, dst: AudioFormat) -> bytes:
    """Encode a float array shaped (channels, samples) to destination PCM bytes."""
    interleaved = np.clip(channels.T.reshape(-1), -1.0, None)
    if dst.pcm_format is PcmFormat.S16LE:
        scaled = np.minimum(interleaved * 32768.0, 32767.0)
        return scaled.astype("<i2").tobytes()
    if dst.pcm_format is PcmFormat.S24LE:
        scaled = np.minimum(interleaved * 8388608.0, 8388607.0).astype("<i4")
        return scaled.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if dst.pcm_format is PcmFormat.S24LE_IN_S32:
        scaled = np.minimum(interleaved * 8388608.0, 8388607.0).astype("<i4") << 8
        return scaled.astype("<i4").tobytes()
    scaled = np.minimum(interleaved * 2147483648.0, 2147483647.0)
    return scaled.astype("<i4").tobytes()


def _map_channels(channels: np.ndarray, out_channels: int) -> np.ndarray:
    in_channels = channels.shape[0]
    if out_channels == 1 and in_channels > 1:
        return channels.mean(axis=0, keepdims=True)
    if out_channels <= in_channels:
        return channels[:out_channels]
    pad = np.repeat(channels[-1:], out_channels - in_channels, axis=0)
    return np.concatenate([channels, pad], axis=0)


class _PolyphaseResampler:
    """Stateful rational resampler with a Kaiser-windowed low-pass filter."""

    def __init__(self, src_rate: int, dst_rate: int, in_samples: int, channels: int) -> None:
        g = gcd(src_rate, dst_rate)
        self._up = dst_rate // g
        self._down = src_rate // g
        self._identity = self._up == self._down
        if self._identity:
            return

        out_samples = (in_samples * self._up) // self._down
        taps = _RESAMPLER_TAPS_PER_PHASE
        filter_len = taps * self._up
        n = np.arange(filter_len) - (filter_len - 1) / 2.0
        cutoff = _ROLLOFF * min(1.0 / self._up, 1.0 / self._down)
        kernel = cutoff * np.sinc(cutoff * n) * np.kaiser(filter_len, _KAISER_BETA)
        kernel *= self._up / kernel.reshape(taps, self._up).sum(axis=0).max()

        self._history = taps
        out_index = np.arange(out_samples)
        upsampled = out_index * self._down
        phase = upsampled % self._up
        center = upsampled // self._up
        tap_index = np.arange(taps)
        self._gather = self._history + center[:, None] - tap_index[None, :]
        self._coeffs = kernel[phase[:, None] + tap_index[None, :] * self._up]
        self._tail = np.zeros((channels, self._history), dtype=np.float64)

    def process(self, channels: np.ndarray) -> np.ndarray:
        if self._identity:
            return channels
        x = np.concatenate([self._tail, channels], axis=1)
        self._tail = x[:, -self._history:]
        return np.einsum("ot,cot->co", self._coeffs, x[:, self._gather], optimize=True)


def convert_audio_frame(data: bytes, src: AudioFormat, dst: AudioFormat) -> bytes:
    """Convert one PCM frame between negotiated intercom formats.

    Matching formats return the original bytes. Use PcmFrameConverter for
    streams, especially when sample rate or frame duration changes.
    """
    if src == dst:
        return data
    if src.frame_ms != dst.frame_ms:
        raise ValueError(
            f"frame_ms conversion requires stateful reframing: {src.wire_token()} -> {dst.wire_token()}"
        )
    channels = _map_channels(_decode_frame(data, src), dst.channels)
    resampler = _PolyphaseResampler(
        src.sample_rate, dst.sample_rate, channels.shape[1], dst.channels
    )
    return _encode_frame(resampler.process(channels), dst)


class PcmFrameConverter:
    """Stateful PCM converter that also reframes differing frame durations."""

    def __init__(self, src: AudioFormat, dst: AudioFormat) -> None:
        self.src = src
        self.dst = dst
        if (src.frame_ms * dst.sample_rate) % 1000:
            raise ValueError(
                f"{src.wire_token()} cannot be reframed exactly at {dst.sample_rate} Hz"
            )
        self._resampler = _PolyphaseResampler(
            src.sample_rate, dst.sample_rate, src.nominal_frame_samples, dst.channels
        )
        self._pending = np.empty((dst.channels, 0), dtype=np.float64)

    def convert(self, data: bytes) -> list[bytes]:
        if len(data) != self.src.nominal_frame_bytes:
            raise ValueError(
                f"audio frame length {len(data)} does not match {self.src.wire_token()} "
                f"({self.src.nominal_frame_bytes} bytes)"
            )
        if self.src == self.dst:
            return [data]

        channels = _map_channels(_decode_frame(data, self.src), self.dst.channels)
        self._pending = np.concatenate(
            [self._pending, self._resampler.process(channels)], axis=1
        )

        out: list[bytes] = []
        frame_samples = self.dst.nominal_frame_samples
        while self._pending.shape[1] >= frame_samples:
            out.append(_encode_frame(self._pending[:, :frame_samples], self.dst))
            self._pending = self._pending[:, frame_samples:]
        return out
