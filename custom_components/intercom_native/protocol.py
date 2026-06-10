"""PBX-lite wire protocol helpers shared by HA transports.

This is the HA-side protocol library. Keep it aligned with
docs/INTERCOM_PROTOCOL.md and
esphome/components/intercom_api/intercom_protocol.h.
"""

from __future__ import annotations

import struct

from .audio_format import AudioFormat, LEGACY_AUDIO_FORMAT, audio_format_from_wire
from .const import (
    HEADER_SIZE,
    MAX_CALL_ID_LEN,
    MAX_NAME_LEN,
    MAX_REASON_LEN,
    MAX_ROUTE_ID_LEN,
)


MAX_PAYLOAD_SIZE = 0xFFFF
_HEADER_STRUCT = struct.Struct("<BH")
_FORMAT_STRUCT = struct.Struct("<IBBH")
_START_V2_MAGIC = b"ICAF2"
_ANSWER_V2_MAGIC = b"ICAA2"


def build_header(msg_type: int, length: int) -> bytes:
    """Encode a PBX-lite header: msg_type:u8 + length:u16 little-endian."""
    if not 0 <= msg_type <= 0xFF:
        raise ValueError(f"msg_type out of range: {msg_type}")
    if not 0 <= length <= 0xFFFF:
        raise ValueError(f"length out of range: {length}")
    return _HEADER_STRUCT.pack(msg_type, length)


def parse_header(data: bytes) -> tuple[int, int]:
    """Decode a 3-byte PBX-lite header."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"header truncated ({len(data)} < {HEADER_SIZE})")
    return _HEADER_STRUCT.unpack(data[:HEADER_SIZE])


def build_frame(msg_type: int, body: bytes = b"") -> bytes:
    """Return header + body for a framed TCP/UDP-control message."""
    if len(body) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"payload too large ({len(body)} > {MAX_PAYLOAD_SIZE})")
    return build_header(msg_type, len(body)) + body


def encode_call_id_prefix(call_id: str) -> bytes:
    """Encode the common prefix: call_id_len + call_id_utf8."""
    cid = call_id.encode("utf-8")
    if len(cid) > MAX_CALL_ID_LEN:
        raise ValueError(f"call_id too long ({len(cid)} > {MAX_CALL_ID_LEN})")
    return bytes([len(cid)]) + cid


def decode_call_id_prefix(data: bytes) -> tuple[str, int]:
    """Return (call_id, bytes_consumed). Raises on malformed input."""
    if len(data) < 1:
        raise ValueError("call_id prefix truncated (need >=1 byte)")
    cid_len = data[0]
    if len(data) < 1 + cid_len:
        raise ValueError(
            f"call_id truncated (header says {cid_len}, have {len(data) - 1})"
        )
    call_id = data[1 : 1 + cid_len].decode("utf-8", errors="replace")
    return call_id, 1 + cid_len


def encode_lp_string(s: str, max_len: int) -> bytes:
    """Length-prefixed UTF-8 string: u8 len + bytes."""
    raw = s.encode("utf-8")
    if len(raw) > max_len:
        raise ValueError(f"string too long ({len(raw)} > {max_len})")
    if len(raw) > 0xFF:
        raise ValueError(f"string exceeds u8 length prefix ({len(raw)} > 255)")
    return bytes([len(raw)]) + raw


def decode_lp_string(data: bytes) -> tuple[str, int]:
    """Decode a length-prefixed UTF-8 string. Returns (value, bytes_consumed)."""
    if len(data) < 1:
        raise ValueError("lp_string truncated (need >=1 byte)")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError(f"lp_string truncated (header says {n}, have {len(data) - 1})")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def _normalise_formats(formats: list[AudioFormat] | tuple[AudioFormat, ...] | None) -> list[AudioFormat]:
    if not formats:
        return [LEGACY_AUDIO_FORMAT]
    out = [fmt if isinstance(fmt, AudioFormat) else AudioFormat(**fmt) for fmt in formats]
    if len(out) > 8:
        raise ValueError("too many audio formats (max 8)")
    return out


def _encode_audio_format(fmt: AudioFormat) -> bytes:
    return _FORMAT_STRUCT.pack(fmt.sample_rate, fmt.format_id, fmt.channels, fmt.frame_ms)


def _decode_audio_format(data: bytes, off: int) -> tuple[AudioFormat, int]:
    if len(data) < off + _FORMAT_STRUCT.size:
        raise ValueError("audio format truncated")
    sample_rate, format_id, channels, frame_ms = _FORMAT_STRUCT.unpack_from(data, off)
    return audio_format_from_wire(sample_rate, format_id, channels, frame_ms), off + _FORMAT_STRUCT.size


def _encode_format_list(formats: list[AudioFormat]) -> bytes:
    return bytes((len(formats),)) + b"".join(_encode_audio_format(fmt) for fmt in formats)


def _decode_format_list(data: bytes, off: int) -> tuple[list[AudioFormat], int]:
    if len(data) < off + 1:
        raise ValueError("audio format list missing count")
    count = data[off]
    off += 1
    if count == 0:
        raise ValueError("audio format list is empty")
    formats: list[AudioFormat] = []
    for _ in range(count):
        fmt, off = _decode_audio_format(data, off)
        formats.append(fmt)
    return formats, off


def _all_legacy(*format_lists: list[AudioFormat]) -> bool:
    return all(formats == [LEGACY_AUDIO_FORMAT] for formats in format_lists)


def _encode_start_v2(caller_tx_formats: list[AudioFormat], caller_rx_formats: list[AudioFormat]) -> bytes:
    return (
        _START_V2_MAGIC
        + bytes((1,))
        + _encode_format_list(caller_tx_formats)
        + _encode_format_list(caller_rx_formats)
    )


def _decode_start_v2(data: bytes, off: int) -> tuple[dict, int]:
    if len(data) == off:
        return {
            "protocol_version": 1,
            "caller_tx_formats": [LEGACY_AUDIO_FORMAT],
            "caller_rx_formats": [LEGACY_AUDIO_FORMAT],
        }, off
    if not data.startswith(_START_V2_MAGIC, off):
        raise ValueError("unknown START extension")
    off += len(_START_V2_MAGIC)
    if len(data) < off + 1:
        raise ValueError("START extension missing version")
    version = data[off]
    off += 1
    if version != 1:
        raise ValueError(f"unsupported START audio extension version {version}")
    caller_tx_formats, off = _decode_format_list(data, off)
    caller_rx_formats, off = _decode_format_list(data, off)
    return {
        "protocol_version": 2,
        "caller_tx_formats": caller_tx_formats,
        "caller_rx_formats": caller_rx_formats,
    }, off


def build_start_body(
    call_id: str,
    caller_route: str,
    caller_name: str,
    dest_route: str,
    dest_name: str,
    *,
    caller_tx_formats: list[AudioFormat] | tuple[AudioFormat, ...] | None = None,
    caller_rx_formats: list[AudioFormat] | tuple[AudioFormat, ...] | None = None,
) -> bytes:
    """MSG_START body with optional v2 audio capabilities.

    Legacy senders produce the exact v1 body. v2 senders append the extension
    only when advertising non-legacy capabilities; v2 peers then negotiate each
    direction separately and confirm the selected formats in ANSWER.
    """
    tx_formats = _normalise_formats(caller_tx_formats)
    rx_formats = _normalise_formats(caller_rx_formats)
    base = (
        encode_call_id_prefix(call_id)
        + encode_lp_string(caller_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(caller_name, MAX_NAME_LEN)
        + encode_lp_string(dest_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(dest_name, MAX_NAME_LEN)
    )
    if _all_legacy(tx_formats, rx_formats):
        return base
    return base + _encode_start_v2(tx_formats, rx_formats)


def parse_start_body(body: bytes) -> dict:
    """Parse MSG_START body."""
    call_id, off = decode_call_id_prefix(body)
    caller_route, n = decode_lp_string(body[off:])
    off += n
    caller_name, n = decode_lp_string(body[off:])
    off += n
    dest_route, n = decode_lp_string(body[off:])
    off += n
    dest_name, n = decode_lp_string(body[off:])
    off += n
    ext, off = _decode_start_v2(body, off)
    if off != len(body):
        raise ValueError(f"START body has {len(body) - off} trailing bytes")
    return {
        "call_id": call_id,
        "caller_route": caller_route,
        "caller_name": caller_name,
        "dest_route": dest_route,
        "dest_name": dest_name,
        **ext,
    }


def build_call_id_only_body(call_id: str) -> bytes:
    """Body for MSG_RING / MSG_ANSWER / MSG_HANGUP (just the prefix)."""
    return encode_call_id_prefix(call_id)


def build_answer_body(
    call_id: str,
    *,
    caller_to_dest_format: AudioFormat | None = None,
    dest_to_caller_format: AudioFormat | None = None,
) -> bytes:
    body = encode_call_id_prefix(call_id)
    c2d = caller_to_dest_format or LEGACY_AUDIO_FORMAT
    d2c = dest_to_caller_format or LEGACY_AUDIO_FORMAT
    if c2d == LEGACY_AUDIO_FORMAT and d2c == LEGACY_AUDIO_FORMAT:
        return body
    return body + _ANSWER_V2_MAGIC + bytes((1,)) + _encode_audio_format(c2d) + _encode_audio_format(d2c)


def parse_answer_body(body: bytes) -> dict:
    call_id, off = decode_call_id_prefix(body)
    if off == len(body):
        return {
            "call_id": call_id,
            "protocol_version": 1,
            "caller_to_dest_format": LEGACY_AUDIO_FORMAT,
            "dest_to_caller_format": LEGACY_AUDIO_FORMAT,
        }
    if not body.startswith(_ANSWER_V2_MAGIC, off):
        raise ValueError("unknown ANSWER extension")
    off += len(_ANSWER_V2_MAGIC)
    if len(body) < off + 1:
        raise ValueError("ANSWER extension missing version")
    version = body[off]
    off += 1
    if version != 1:
        raise ValueError(f"unsupported ANSWER audio extension version {version}")
    caller_to_dest, off = _decode_audio_format(body, off)
    dest_to_caller, off = _decode_audio_format(body, off)
    if off != len(body):
        raise ValueError(f"ANSWER body has {len(body) - off} trailing bytes")
    return {
        "call_id": call_id,
        "protocol_version": 2,
        "caller_to_dest_format": caller_to_dest,
        "dest_to_caller_format": dest_to_caller,
    }


def build_decline_body(call_id: str, reason: str = "") -> bytes:
    """MSG_DECLINE body: prefix + reason (lp_string, possibly empty)."""
    return encode_call_id_prefix(call_id) + encode_lp_string(reason, MAX_REASON_LEN)


def parse_decline_body(body: bytes) -> dict:
    """Parse MSG_DECLINE body. Returns call_id, reason."""
    call_id, off = decode_call_id_prefix(body)
    reason, _ = decode_lp_string(body[off:])
    return {"call_id": call_id, "reason": reason}


def build_error_body(call_id: str, error_code: int, detail: str = "") -> bytes:
    """MSG_ERROR body: prefix + error_code(u8) + detail(lp_string)."""
    if not 0 <= error_code <= 0xFF:
        raise ValueError(f"error_code out of range: {error_code}")
    return (
        encode_call_id_prefix(call_id)
        + bytes([error_code])
        + encode_lp_string(detail, MAX_REASON_LEN)
    )


def parse_error_body(body: bytes) -> dict:
    """Parse MSG_ERROR body. Returns call_id, error_code, detail."""
    call_id, off = decode_call_id_prefix(body)
    if len(body) < off + 1:
        raise ValueError("MSG_ERROR body missing error_code")
    error_code = body[off]
    off += 1
    detail, _ = decode_lp_string(body[off:])
    return {
        "call_id": call_id,
        "error_code": error_code,
        "detail": detail,
    }
