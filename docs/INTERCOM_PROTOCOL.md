# Intercom Wire Protocol

This document is the shared PBX-lite wire contract for the ESPHome
`intercom_api` component and the Home Assistant `intercom_native` integration.
Python and C++ implementations must treat this file and the test fixtures as
the source of truth.

## Transport Model

TCP and UDP use the same control message format.

- TCP: every frame is `MessageHeader + body` on `tcp_port` (default 6054).
- UDP control: every datagram is `MessageHeader + body` on
  `udp_control_port` (default 6055).
- UDP audio: raw negotiated PCM datagrams on `udp_audio_port` (default 6054).
  No `MessageHeader` is present on the audio socket. A full audio frame must
  fit in one safe datagram; oversized UDP formats are rejected during setup.

## Header

All framed control messages start with a 3-byte little-endian header:

```text
type:   u8
length: u16 little-endian body byte length
```

The header is intentionally serialized field-by-field. Do not `memcpy` a C
struct onto the wire.

## Message Types

| Code | Name | Body |
|---:|---|---|
| `0x01` | `AUDIO` | TCP-only negotiated raw PCM bytes. UDP audio uses the audio socket without a header. |
| `0x02` | `START` | `call_id` prefix + caller/destination strings + optional audio capabilities. |
| `0x03` | `HANGUP` | `call_id` only. Established-call BYE. |
| `0x04` | `PING` | Empty call id prefix (`00`). |
| `0x05` | `PONG` | Empty call id prefix (`00`). |
| `0x06` | `ERROR` | `call_id` prefix + `error_code:u8` + detail string. |
| `0x07` | `RING` | `call_id` only. Destination is ringing locally. |
| `0x08` | `ANSWER` | `call_id` prefix + optional selected audio formats. Destination accepted. |
| `0x09` | `DECLINE` | `call_id` prefix + reason string. |

## Body Strings

The common prefix is:

```text
call_id_len: u8
call_id:     UTF-8 bytes
```

Every additional string is length-prefixed the same way:

```text
len:   u8
value: UTF-8 bytes
```

Maximum lengths:

| Field | Max bytes |
|---|---:|
| `call_id` | 64 |
| `route_id` | 64 |
| display name | 64 |
| reason/detail | 160 |

UTF-8 is passed through. Receivers should reject truncated fields and tolerate
unknown free-form text in reason/detail fields.

## START

```text
call_id_prefix
caller_route: lp-string
caller_name:  lp-string
dest_route:   lp-string
dest_name:    lp-string
[optional v2 audio extension]
```

`call_id` remains human-readable: `Caller<->Destination`.

Friendly names are the public endpoint identity. `route` fields may be the same
friendly name during the current protocol generation; implementations must not
replace the display destination with HA when HA is only bridging a cross-protocol
call.

### START v2 Audio Extension

Legacy 16 kHz/s16/mono/32 ms peers omit the extension. Peers that support other
formats append:

```text
magic:   "ICAF2"
version: u8 = 1
caller_tx_formats: format-list   # caller microphone/source -> wire
caller_rx_formats: format-list   # wire -> caller speaker/sink
```

A format list is `count:u8` followed by up to 8 `AudioFormat` entries:

```text
sample_rate: u32 little-endian
pcm_format:  u8   # 1=s16le, 2=s24le, 3=s24le_in_s32, 4=s32le
channels:    u8
frame_ms:    u16 little-endian
```

Supported sample rates are `8000`, `12000`, `16000`, `24000`, `32000`,
`44100` and `48000` Hz. Supported frame durations are `10`, `20` and `32` ms
only when `sample_rate * frame_ms / 1000` is an integer number of samples.
Channels are mono or stereo. 24-bit audio is explicit: packed `s24le` and
24-bit samples carried in a 32-bit little-endian container (`s24le_in_s32`) are
different PCM formats.

Audio format is per direction, not per device. A device can legitimately
transmit one format and receive another. If an ESP source is the Espressif
AFE/AEC output, that branch is locally constrained to 16 kHz/s16/mono by
esp-sr. Native ESPHome microphones/speakers and the browser softphone can use
their declared formats independently.

The effective format is the first compatible item from the constrained endpoint
side. Home Assistant may bridge different formats by explicit PCM conversion.
Direct ESP-to-ESP calls without a common format must fail with a readable
`DECLINE("incompatible_audio_format")` or equivalent error; they must not fall
back silently.

## ANSWER

Legacy peers send only the `call_id` prefix. v2 peers append the selected
formats so the caller configures both audio directions deterministically:

```text
call_id_prefix
magic: "ICAA2"
version: u8 = 1
caller_to_dest_format: AudioFormat
dest_to_caller_format: AudioFormat
```

## HANGUP vs DECLINE

- `HANGUP(call_id)` is the normal BYE for an established call.
- `DECLINE(call_id, "")` is setup-phase cancel/dismiss and renders as ordinary
  `remote_hangup` on the peer.
- `DECLINE(call_id, reason)` carries a user-visible reason verbatim.

Canonical reasons include:

```text
local_hangup
remote_hangup
remote_device_lost
declined
timeout
busy
unreachable
protocol_error
bridge_error
DND
```

Free-form reasons are valid. HA as PBX-lite must forward reason strings, not
consume or rewrite them. DND is implemented as `DECLINE("DND")`.

## ERROR

`ERROR` is for protocol/transport faults, not normal user decline.

```text
call_id_prefix
error_code: u8
detail:     lp-string
```

Current code defines `BUSY = 0x01`, but normal busy signaling should prefer
`DECLINE("busy")` when a call is being rejected cleanly.

## Audio

The legacy default is `16000:s16le:1:32`, which is 512 samples / 1024 bytes per
frame. It remains the default for old peers and for AFE/AEC-backed branches.

TCP `AUDIO` frames carry one complete negotiated PCM frame as the body. The
header length is `u16`, so the protocol limit is 65535 bytes; implementations
allocate only the negotiated frame size.

UDP audio datagrams carry one complete negotiated PCM frame without the 3-byte
header. The implementation intentionally avoids relying on IP fragmentation by
default: UDP formats whose frame payload exceeds `udp_max_payload` are rejected
with `unsupported_udp_audio_format`.

The default `udp_max_payload` is 1200 bytes. This is a conservative UDP payload
limit, not a jumbo-frame limit. A standard Ethernet/Wi-Fi MTU of 1500 bytes
still includes IP and UDP headers: IPv4 leaves 1472 bytes for UDP payload, IPv6
leaves 1452 bytes, and VLAN/VPN/tunnel overhead can lower the real path MTU.
Intercom keeps the default below that area so one lost IP fragment cannot
discard a whole audio frame.

Installations with a verified larger LAN path may raise `udp_max_payload` in
both the ESPHome `intercom_api` YAML and the Home Assistant integration
options. Larger PCM frames belong on TCP unless the operator deliberately opts
into larger UDP datagrams or UDP packetization is introduced in a future
protocol version.

Home Assistant bridge conversion is explicit. When HA is in the media path and
the two legs negotiate different formats, HA converts PCM between the selected
source-leg output format and destination-leg input format. This conversion is
not part of the ESP wire protocol and is not a silent fallback for direct
ESP-to-ESP calls. Direct calls require common formats and reject the call when
the intersection is empty.

Compressed media codecs are deliberately outside this realtime wire contract.
ESPHome may decode MP3, FLAC, Opus or WAV in media-player/source pipelines, but
intercom `AUDIO` remains raw negotiated PCM. Adding a compressed intercom mode
would require a separate measured codec profile with explicit realtime
encode/decode, jitter and CPU/PSRAM budgets; it must not be inferred from
ESPHome playback-decoder availability.

## Browser Audio WebSocket

The Lovelace softphone does not use the shared HA frontend WebSocket for audio.
It opens the authenticated endpoint:

```text
/api/intercom_native/ws?device_id=<device_id>
```

This socket is session-bound. Binary messages are:

```text
type:    u8 = 1  # AUDIO
payload: one complete negotiated PCM frame
```

Text messages are JSON controls such as `start`, `ha_softphone_start`,
`answer`, `answer_esp_call`, `hangup` and error/state replies. Successful setup
replies include `tx_format` and `rx_format` tokens so the browser worklets know
exactly how to encode capture and decode playback.

If the browser WebSocket closes, HA stops the bound session and hangs up the ESP
leg. This is intentional: a dropped browser audio socket ends the call cleanly
instead of trying to reconnect mid-call.

Dashboard call-state updates use the scoped
`intercom_native/subscribe_call_events` HA WebSocket command. Cards should not
subscribe directly to HA's generic `subscribe_events` stream for
`intercom_native.call_event`, because Home Assistant blocks arbitrary custom
event subscriptions for non-admin users.

## Keepalive

PING/PONG bodies are one byte: `00`. The current ESP constants use a 5 second
interval and a 15 second peer-lost deadline. When the deadline expires during a
call, callers should return to idle with `remote_device_lost`.

## Canonical Fixture Examples

The Python test suite under `tests/test_intercom_protocol.py` pins these binary
fixtures:

```text
PING frame:
04 01 00 00

START A<->B, A/"Panel A" to B/"Panel B":
02 1a 00 05 41 3c 2d 3e 42 01 41 07 50 61 6e 65 6c 20 41 01 42 07 50 61 6e 65 6c 20 42

DECLINE A<->B, reason DND:
09 0a 00 05 41 3c 2d 3e 42 03 44 4e 44

ERROR A<->B, code 1, detail busy:
06 0c 00 05 41 3c 2d 3e 42 01 04 62 75 73 79
```

When any implementation changes framing, message constants, or reason/error
semantics, these fixtures must be updated intentionally in the same change.
