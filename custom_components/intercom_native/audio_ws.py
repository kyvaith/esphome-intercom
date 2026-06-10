"""Binary browser-audio framing for Intercom Native."""

from __future__ import annotations

AUDIO_FRAME_TYPE = 1
LEGACY_AUDIO_CHUNK_BYTES = 1024


def encode_audio_frame(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("empty audio payload")
    return bytes((AUDIO_FRAME_TYPE,)) + payload


def decode_audio_frame(frame: bytes) -> bytes:
    if len(frame) < 2 or frame[0] != AUDIO_FRAME_TYPE:
        raise ValueError("invalid audio frame")
    return frame[1:]
