#!/usr/bin/env python3
"""Probe intercom_native or an ESP intercom endpoint with a synthetic caller.

Usage:
  HA_TOKEN=... tools/intercom_softphone_probe.py --ha 192.168.1.10
  tools/intercom_softphone_probe.py --target 192.168.1.47 --dest-name "WS3" --expect answer
  tools/intercom_softphone_probe.py --target 192.168.1.47 --send-tone 880 --record-wav /tmp/ws3.wav

When HA_TOKEN is available the script can also subscribe to Home Assistant's
intercom_native.call_event stream. Audio on the wire uses the same negotiated
PCM format tokens as the firmware, for example 16000:s16le:1:32.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
import wave
import importlib.util
from collections.abc import Callable
from pathlib import Path


INTERCOM_PORT = 6054
ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "intercom_native"
PKG_NAME = "custom_components.intercom_native"


def _load_intercom_module(name: str):
    if "custom_components" not in sys.modules:
        pkg = type(sys)("custom_components")
        pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = pkg
    if PKG_NAME not in sys.modules:
        pkg = type(sys)(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format_mod = _load_intercom_module("audio_format")
protocol_mod = _load_intercom_module("protocol")
audio_pcm_mod = _load_intercom_module("audio_pcm")
AudioFormat = audio_format_mod.AudioFormat
parse_audio_format_token = audio_format_mod.parse_audio_format_token

HEADER_SIZE = 3
MAX_PAYLOAD_SIZE = 0xFFFF
MAX_CALL_ID_LEN = 64
MAX_ROUTE_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_REASON_LEN = 160
MSG_START = 0x02
MSG_HANGUP = 0x03
MSG_PING = 0x04
MSG_PONG = 0x05
MSG_ERROR = 0x06
MSG_RING = 0x07
MSG_ANSWER = 0x08
MSG_DECLINE = 0x09
MSG_AUDIO = 0x01

DEFAULT_FORMAT = AudioFormat(16000, "s16le", 1, 32)


OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def build_header(msg_type: int, length: int) -> bytes:
    return struct.pack("<BH", msg_type, length)


def parse_header(data: bytes) -> tuple[int, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError("short header")
    return struct.unpack("<BH", data[:HEADER_SIZE])


def build_frame(msg_type: int, body: bytes = b"") -> bytes:
    if len(body) > MAX_PAYLOAD_SIZE:
        raise ValueError("payload too large")
    return build_header(msg_type, len(body)) + body


def encode_lp_string(value: str, max_len: int) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > max_len:
        raise ValueError(f"string too long ({len(raw)} > {max_len})")
    return bytes([len(raw)]) + raw


def encode_call_id_prefix(call_id: str) -> bytes:
    raw = call_id.encode("utf-8")
    if len(raw) > MAX_CALL_ID_LEN:
        raise ValueError(f"call_id too long ({len(raw)} > {MAX_CALL_ID_LEN})")
    return bytes([len(raw)]) + raw


def build_start_body(
    call_id: str,
    caller_route: str,
    caller_name: str,
    dest_route: str,
    dest_name: str,
    caller_tx_formats: list[AudioFormat] | None = None,
    caller_rx_formats: list[AudioFormat] | None = None,
) -> bytes:
    return protocol_mod.build_start_body(
        call_id,
        caller_route,
        caller_name,
        dest_route,
        dest_name,
        caller_tx_formats=caller_tx_formats,
        caller_rx_formats=caller_rx_formats,
    )


def build_call_id_only_body(call_id: str) -> bytes:
    return encode_call_id_prefix(call_id)


def decode_call_id_prefix(data: bytes) -> tuple[str, int]:
    if not data:
        raise ValueError("missing call_id length")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError("truncated call_id")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def decode_lp_string(data: bytes) -> tuple[str, int]:
    if not data:
        raise ValueError("missing string length")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError("truncated string")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def describe_payload(msg_type: int, payload: bytes) -> str:
    try:
        if msg_type == MSG_START:
            parsed = protocol_mod.parse_start_body(payload)
            return (
                f" call_id={parsed['call_id']!r}"
                f" tx={[f.wire_token() for f in parsed['caller_tx_formats']]}"
                f" rx={[f.wire_token() for f in parsed['caller_rx_formats']]}"
            )
        if msg_type == MSG_ANSWER:
            parsed = protocol_mod.parse_answer_body(payload)
            return (
                f" call_id={parsed['call_id']!r}"
                f" caller_to_dest={parsed['caller_to_dest_format'].wire_token()}"
                f" dest_to_caller={parsed['dest_to_caller_format'].wire_token()}"
            )
        if msg_type in (MSG_RING, MSG_ANSWER, MSG_HANGUP):
            call_id, _ = decode_call_id_prefix(payload)
            return f" call_id={call_id!r}"
        if msg_type == MSG_DECLINE:
            call_id, off = decode_call_id_prefix(payload)
            reason, _ = decode_lp_string(payload[off:])
            return f" call_id={call_id!r} reason={reason!r}"
        if msg_type == MSG_ERROR:
            call_id, off = decode_call_id_prefix(payload)
            code = payload[off] if len(payload) > off else None
            detail, _ = decode_lp_string(payload[off + 1 :]) if code is not None else ("", 0)
            return f" call_id={call_id!r} code={code} detail={detail!r}"
    except ValueError as err:
        return f" malformed={err}"
    return ""


def _http_get_json(base_url: str, token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MinimalWebSocket:
    def __init__(self, host: str, port: int, path: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.5)
        self._rx_buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket HTTP upgrade closed")
            response += chunk
        header_block, _, self._rx_buffer = response.partition(b"\r\n\r\n")
        if b" 101 " not in header_block.split(b"\r\n", 1)[0]:
            raise RuntimeError(response.decode("utf-8", errors="replace"))

    def send_json(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(OP_TEXT, raw)

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._recv_frame(deadline - time.monotonic())
            if frame is None:
                continue
            opcode, payload = frame
            if opcode == OP_TEXT:
                return json.loads(payload.decode("utf-8"))
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                return None
        return None

    def _recv_exact(self, n: int, timeout: float) -> bytes | None:
        self.sock.settimeout(max(0.05, timeout))
        data = b""
        if self._rx_buffer:
            data = self._rx_buffer[:n]
            self._rx_buffer = self._rx_buffer[n:]
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except socket.timeout:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _recv_frame(self, timeout: float) -> tuple[int, bytes] | None:
        head = self._recv_exact(2, timeout)
        if head is None:
            return None
        b0, b1 = head
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = self._recv_exact(2, timeout)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8, timeout)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = self._recv_exact(4, timeout) if masked else b""
        payload = self._recv_exact(length, timeout) if length else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload


class EventCollector:
    def __init__(self, base_url: str, token: str) -> None:
        parsed = base_url.removeprefix("http://").removeprefix("https://")
        host, _, port_s = parsed.partition(":")
        self.host = host
        self.port = int(port_s or "8123")
        self.token = token
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._ws: MinimalWebSocket | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            if self._error:
                raise self._error
            raise RuntimeError("HA event subscription did not become ready")
        if self._error:
            raise self._error

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=2)
        if self._error is not None:
            print(f"HA_WS_ERROR {self._error}", file=sys.stderr)

    def _run(self) -> None:
        try:
            self._ws = MinimalWebSocket(self.host, self.port, "/api/websocket")
            ws = self._ws
            first = ws.recv_json(timeout=5)
            if not first or first.get("type") != "auth_required":
                raise RuntimeError(f"unexpected HA WS greeting: {first}")
            ws.send_json({"type": "auth", "access_token": self.token})
            auth = ws.recv_json(timeout=5)
            if not auth or auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA WS auth failed: {auth}")
            ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "intercom_native.call_event"})
            while True:
                msg = ws.recv_json(timeout=5)
                if not msg:
                    raise RuntimeError("HA event subscription timed out")
                if msg.get("id") == 1:
                    if not msg.get("success", False):
                        raise RuntimeError(f"HA event subscription failed: {msg}")
                    break
            ws.send_json({"id": 2, "type": "intercom_native/ha_softphone_state"})
            self._ready.set()
            while not self._stop.is_set():
                msg = ws.recv_json(timeout=0.5)
                if not msg:
                    continue
                if msg.get("type") == "event":
                    self.events.append(msg.get("event", {}).get("data", {}))
                    print("HA_EVENT", json.dumps(self.events[-1], sort_keys=True))
                elif msg.get("id") == 2:
                    if msg.get("success", True):
                        print("HA_SOFTPHONE_STATE", json.dumps(msg.get("result", {}), sort_keys=True))
                    else:
                        raise RuntimeError(f"HA softphone state command failed: {msg}")
        except OSError as err:
            if not self._stop.is_set():
                self._error = err
                self._ready.set()
        except Exception as err:
            self._error = err
            self._ready.set()


def _read_control_frame(sock: socket.socket, timeout: float) -> tuple[int, bytes] | None:
    sock.settimeout(timeout)
    try:
        header = sock.recv(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            return None
        msg_type, length = parse_header(header)
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk
        return msg_type, payload
    except socket.timeout:
        return None


def _read_udp_control_frame(sock: socket.socket, timeout: float) -> tuple[int, bytes] | None:
    sock.settimeout(timeout)
    try:
        data, _addr = sock.recvfrom(HEADER_SIZE + MAX_PAYLOAD_SIZE)
    except socket.timeout:
        return None
    if not data:
        return None
    msg_type, length = parse_header(data)
    payload = data[HEADER_SIZE : HEADER_SIZE + length]
    if len(payload) != length:
        raise ValueError(f"truncated UDP control payload ({len(payload)} != {length})")
    return msg_type, payload


def _message_name(msg_type: int) -> str:
    return {
        MSG_AUDIO: "AUDIO",
        MSG_START: "START",
        MSG_HANGUP: "HANGUP",
        MSG_PING: "PING",
        MSG_PONG: "PONG",
        MSG_RING: "RING",
        MSG_ANSWER: "ANSWER",
        MSG_DECLINE: "DECLINE",
        MSG_ERROR: "ERROR",
    }.get(msg_type, f"0x{msg_type:02X}")


def _wav_format(path: str, wav: wave.Wave_read) -> AudioFormat:
    width = wav.getsampwidth()
    pcm = {2: "s16le", 3: "s24le", 4: "s32le"}.get(width)
    if pcm is None:
        raise ValueError(f"{path}: unsupported WAV sample width {width}")
    return AudioFormat(wav.getframerate(), pcm, wav.getnchannels(), 20)


def _chunk_bytes(fmt: AudioFormat) -> int:
    return fmt.nominal_frame_bytes


def _frame_seconds(fmt: AudioFormat) -> float:
    return fmt.frame_ms / 1000.0


def _load_wav_chunks(path: str, tx_format: AudioFormat) -> list[bytes]:
    with wave.open(path, "rb") as wav:
        src_format = _wav_format(path, wav)
        raw = wav.readframes(wav.getnframes())
    chunks = []
    src_frame_bytes = src_format.nominal_frame_bytes
    for off in range(0, len(raw), src_frame_bytes):
        chunk = raw[off : off + src_frame_bytes]
        if len(chunk) < src_frame_bytes:
            chunk += b"\x00" * (src_frame_bytes - len(chunk))
        chunks.append(audio_pcm_mod.convert_audio_frame(chunk, src_format, tx_format))
    return chunks


def _encode_tone_sample(value: float, fmt: AudioFormat) -> bytes:
    if fmt.pcm_format.value == "s16le":
        sample = int(value * (32768 if value < 0 else 32767))
        return sample.to_bytes(2, "little", signed=True)
    if fmt.pcm_format.value == "s24le":
        sample = int(value * (8388608 if value < 0 else 8388607))
        return (sample & 0xFFFFFF).to_bytes(3, "little")
    if fmt.pcm_format.value == "s24le_in_s32":
        sample = int(value * (8388608 if value < 0 else 8388607)) << 8
        return sample.to_bytes(4, "little", signed=True)
    sample = int(value * (2147483648 if value < 0 else 2147483647))
    return sample.to_bytes(4, "little", signed=True)


def _tone_chunks(freq_hz: float, seconds: float, amplitude: float, tx_format: AudioFormat) -> list[bytes]:
    if seconds <= 0:
        return []
    amplitude = max(0.0, min(1.0, amplitude))
    total_samples = int(seconds * tx_format.sample_rate)
    frame_samples = tx_format.nominal_frame_samples
    chunks = []
    for base in range(0, total_samples, frame_samples):
        samples = bytearray()
        for i in range(frame_samples):
            n = base + i
            value = 0
            if n < total_samples:
                value = amplitude * math.sin(2.0 * math.pi * freq_hz * n / tx_format.sample_rate)
            encoded = _encode_tone_sample(value, tx_format)
            for _ in range(tx_format.channels):
                samples.extend(encoded)
        chunks.append(bytes(samples))
    return chunks


def _audio_chunks_from_args(args: argparse.Namespace, tx_format: AudioFormat) -> list[bytes]:
    if args.send_wav:
        return _load_wav_chunks(args.send_wav, tx_format)
    if args.send_tone:
        return _tone_chunks(args.send_tone, args.tone_seconds, args.tone_amplitude, tx_format)
    return []


def _write_recording(path: str, chunks: list[bytes], rx_format: AudioFormat) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(rx_format.channels)
        wav.setsampwidth(rx_format.container_bytes_per_sample)
        wav.setframerate(rx_format.sample_rate)
        wav.writeframes(b"".join(chunks))


class AudioSender:
    def __init__(self, send_audio: Callable[[bytes], None], chunks: list[bytes], repeat: bool, frame_seconds: float) -> None:
        self._send_audio = send_audio
        self.chunks = chunks
        self.repeat = repeat
        self.frame_seconds = frame_seconds
        self.sent_chunks = 0
        self.sent_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.chunks or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        index = 0
        next_send = time.monotonic()
        while not self._stop.is_set() and self.chunks:
            chunk = self.chunks[index]
            try:
                with self._lock:
                    self._send_audio(chunk)
            except OSError:
                return
            self.sent_chunks += 1
            self.sent_bytes += len(chunk)
            index += 1
            if index >= len(self.chunks):
                if not self.repeat:
                    return
                index = 0
            next_send += self.frame_seconds
            delay = next_send - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)


def run_probe(args: argparse.Namespace) -> int:
    token = os.environ.get("HA_TOKEN", "")
    target_host = args.target or args.ha
    ha_events = args.ha_events
    if ha_events == "auto":
        ha_events_enabled = bool(token)
    else:
        ha_events_enabled = ha_events == "on"
    if ha_events_enabled and not token:
        print("HA_TOKEN env var is required when --ha-events=on", file=sys.stderr)
        return 2

    base_url = args.ha_url or f"http://{args.ha}:8123"
    config = _http_get_json(base_url, token, "/api/config") if ha_events_enabled else {}
    dest_name = args.dest_name or config.get("location_name") or "Home Assistant"
    call_id = args.call_id or f"{args.caller_name}<->{dest_name}:{int(time.time())}"
    tx_formats = [parse_audio_format_token(token) for token in (args.tx_format or [DEFAULT_FORMAT.wire_token()])]
    rx_formats = [parse_audio_format_token(token) for token in (args.rx_format or [DEFAULT_FORMAT.wire_token()])]
    tx_format = tx_formats[0]
    rx_format = rx_formats[0]

    collector = None
    if ha_events_enabled:
        collector = EventCollector(base_url, token)
        collector.start()
        time.sleep(0.3)

    sender_format = tx_format
    audio_chunks = _audio_chunks_from_args(args, sender_format)
    rx_audio: list[bytes] = []
    transport = args.transport
    sock: socket.socket | None = None
    audio_sock: socket.socket | None = None
    control_target = (target_host, args.udp_control_port)
    audio_target = (target_host, args.udp_audio_port)

    if transport == "tcp":
        sock = socket.create_connection((target_host, args.port), timeout=5)

        def send_control_frame(msg_type: int, body: bytes = b"") -> None:
            assert sock is not None
            sock.sendall(build_frame(msg_type, body))

        def read_next_frame(timeout: float) -> tuple[int, bytes] | None:
            assert sock is not None
            return _read_control_frame(sock, timeout=timeout)

        def send_audio_chunk(chunk: bytes) -> None:
            assert sock is not None
            sock.sendall(build_frame(MSG_AUDIO, chunk))

    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", args.local_udp_control_port))
        audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        audio_sock.bind(("", args.local_udp_audio_port))
        local_audio = audio_sock.getsockname()[1]
        if local_audio != args.local_udp_audio_port:
            print(f"UDP_AUDIO local_port={local_audio}")

        def send_control_frame(msg_type: int, body: bytes = b"") -> None:
            assert sock is not None
            sock.sendto(build_frame(msg_type, body), control_target)

        def read_next_frame(timeout: float) -> tuple[int, bytes] | None:
            assert sock is not None
            return _read_udp_control_frame(sock, timeout=timeout)

        def send_audio_chunk(chunk: bytes) -> None:
            assert audio_sock is not None
            audio_sock.sendto(chunk, audio_target)

    sender = AudioSender(send_audio_chunk, audio_chunks, args.repeat_audio, _frame_seconds(sender_format))
    body = build_start_body(
        call_id=call_id,
        caller_route=args.caller_route,
        caller_name=args.caller_name,
        dest_route=args.dest_route,
        dest_name=dest_name,
        caller_tx_formats=tx_formats,
        caller_rx_formats=rx_formats,
    )
    send_control_frame(MSG_START, body)
    print(
        f"SENT START transport={transport} target={target_host!r} caller={args.caller_name!r} "
        f"dest={dest_name!r} call_id={call_id!r} "
        f"tx={[f.wire_token() for f in tx_formats]} rx={[f.wire_token() for f in rx_formats]}"
    )
    if audio_chunks:
        print(
            f"AUDIO_TX queued chunks={len(audio_chunks)} bytes={sum(len(c) for c in audio_chunks)} "
            f"repeat={args.repeat_audio}"
        )

    deadline = time.monotonic() + args.listen_seconds
    observed: list[str] = []
    answered = False
    try:
        while time.monotonic() < deadline:
            if transport == "udp" and audio_sock is not None:
                audio_sock.settimeout(0.0)
                while True:
                    try:
                        data, _addr = audio_sock.recvfrom(MAX_PAYLOAD_SIZE)
                    except (BlockingIOError, socket.timeout):
                        break
                    if data:
                        rx_audio.append(data)
                        if len(rx_audio) == 1 or len(rx_audio) % 50 == 0:
                            print(f"AUDIO_RX chunks={len(rx_audio)} bytes={sum(len(c) for c in rx_audio)}")
            frame = read_next_frame(timeout=0.5)
            if frame is None:
                continue
            msg_type, payload = frame
            observed.append(_message_name(msg_type).lower())
            if msg_type == MSG_AUDIO:
                rx_audio.append(payload)
                if len(rx_audio) == 1 or len(rx_audio) % 50 == 0:
                    print(f"AUDIO_RX chunks={len(rx_audio)} bytes={sum(len(c) for c in rx_audio)}")
                continue
            print(f"{transport.upper()}_RX {_message_name(msg_type)} len={len(payload)}{describe_payload(msg_type, payload)}")
            if msg_type == MSG_PING:
                send_control_frame(MSG_PONG)
            if msg_type == MSG_ANSWER:
                answer = protocol_mod.parse_answer_body(payload)
                tx_format = answer["caller_to_dest_format"]
                rx_format = answer["dest_to_caller_format"]
                if tx_format != sender_format:
                    sender.stop()
                    sender_format = tx_format
                    audio_chunks = _audio_chunks_from_args(args, sender_format)
                    sender = AudioSender(send_audio_chunk, audio_chunks, args.repeat_audio, _frame_seconds(sender_format))
                answered = True
                sender.start()
                if not args.record_wav and not audio_chunks:
                    time.sleep(1.0)
                    break
            if msg_type in (MSG_DECLINE, MSG_ERROR):
                time.sleep(0.5)
                break
            if msg_type == MSG_RING and not args.keep_ringing:
                time.sleep(1.0)
                break
    finally:
        sender.stop()
        try:
            send_control_frame(MSG_HANGUP, build_call_id_only_body(call_id))
        except Exception:
            pass
        if sock is not None:
            sock.close()
        if audio_sock is not None:
            audio_sock.close()
        time.sleep(0.5)
        if collector is not None:
            collector.stop()
        if args.record_wav:
            _write_recording(args.record_wav, rx_audio, rx_format)
            print(f"AUDIO_RX wrote {args.record_wav} chunks={len(rx_audio)} bytes={sum(len(c) for c in rx_audio)}")
        if audio_chunks:
            print(f"AUDIO_TX sent chunks={sender.sent_chunks} bytes={sender.sent_bytes} answered={answered}")

    if args.expect:
        return 0 if args.expect.lower() in observed else 1

    if not ha_events_enabled:
        return 0 if observed else 1

    assert collector is not None
    ringing_events = [
        e for e in collector.events
        if e.get("device_id") == "__intercom_native_ha_softphone__"
        and e.get("state") == "ringing"
    ]
    return 0 if ringing_events else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha", default="192.168.1.10", help="HA host/IP for event subscription and default TCP target")
    parser.add_argument("--target", default="", help="TCP intercom target host/IP; defaults to --ha")
    parser.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument("--ha-url", default="", help="HA base URL, default http://HA:8123")
    parser.add_argument("--ha-events", choices=("auto", "on", "off"), default="auto",
                        help="Subscribe to HA intercom events. auto enables it when HA_TOKEN is set.")
    parser.add_argument("--port", type=int, default=INTERCOM_PORT)
    parser.add_argument("--udp-audio-port", type=int, default=6054)
    parser.add_argument("--udp-control-port", type=int, default=6055)
    parser.add_argument("--local-udp-audio-port", type=int, default=6054)
    parser.add_argument("--local-udp-control-port", type=int, default=0)
    parser.add_argument("--caller-name", default="Codex Fake ESP 1")
    parser.add_argument("--caller-route", default="codex-fake-1")
    parser.add_argument("--dest-name", default="")
    parser.add_argument("--dest-route", default="")
    parser.add_argument("--call-id", default="")
    parser.add_argument("--listen-seconds", type=float, default=6.0)
    parser.add_argument("--expect", choices=("ring", "answer", "decline", "error"), default="")
    parser.add_argument("--tx-format", action="append", default=None,
                        help=f"Caller TX capability token, repeatable. Default: {DEFAULT_FORMAT.wire_token()}")
    parser.add_argument("--rx-format", action="append", default=None,
                        help=f"Caller RX capability token, repeatable. Default: {DEFAULT_FORMAT.wire_token()}")
    parser.add_argument("--send-tone", type=float, default=0.0, metavar="HZ",
                        help="Send a generated sine tone after ANSWER, e.g. 880.")
    parser.add_argument("--tone-seconds", type=float, default=3.0)
    parser.add_argument("--tone-amplitude", type=float, default=0.20)
    parser.add_argument("--send-wav", default="", help="Send a WAV after ANSWER; it is converted to --tx-format.")
    parser.add_argument("--repeat-audio", action="store_true", help="Loop --send-tone/--send-wav until listen timeout.")
    parser.add_argument("--record-wav", default="", help="Record received AUDIO frames using the answered RX format.")
    parser.add_argument("--keep-ringing", action="store_true", help="Do not exit immediately after RING.")
    return run_probe(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
