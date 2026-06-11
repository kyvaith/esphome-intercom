# Phonebook Protocol

This document defines the phonebook contract for PBX-lite intercom firmware
and the Home Assistant `intercom_native` integration.

The standard HA-managed firmware flow is endpoint-first:

1. Each ESP publishes its local endpoint as one canonical row through the
   `intercom_endpoint` text sensor.
2. Home Assistant builds the central roster from those endpoint rows and its
   own HA peer row.
3. HA publishes a short `sensor.intercom_phonebook` state and puts the full CSV
   roster in that sensor's `phonebook` attribute.
4. ESP firmware subscribes to the `phonebook` attribute and shapes the rows for
   its active transport.

## Goals

- One logical phonebook model for TCP, UDP, HA, and cross-protocol routing.
- Friendly name remains the public intercom identity.
- Transport protocol is an explicit field of each endpoint.
- HA may route cross-protocol calls, but must not hide the real destination or
  rewrite user-facing call reasons.
- ESP firmware, HA integration, and the card must agree on the same call/FSM
  contract.

## Entry Model

```text
PhonebookEntry
  name: string              # canonical friendly name
  address: string           # IPv4/IPv6/hostname
  protocol: tcp|udp|ha
  audio_port: uint16        # TCP signaling port OR UDP audio port
  control_port: uint16?     # UDP framed-control port, absent for TCP
  audio_mode: full_duplex|mic_only|speaker_only|control_only?
  tx_formats: string?       # semicolon-separated AudioFormat tokens
  rx_formats: string?       # semicolon-separated AudioFormat tokens
  route_id: string?         # optional technical route hint
  role: esp|ha?             # optional metadata
```

Rules:

- `name` is the deduplication key and display identity.
- Same `name` means the same intercom endpoint; endpoint changes replace the
  previous endpoint.
- `protocol` is mandatory in canonical rows.
- For TCP, `audio_port` is the TCP framed signaling/audio port.
- For UDP, `audio_port` is raw PCM audio and `control_port` is framed PBX-lite
  signaling.
- HA as a peer is represented as a first-class entry, not a special
  `device is None` branch.

## Canonical CSV Rows

The wire format is CSV. Each row is one endpoint:

```text
Name|tcp|ip|tcp_port
Name|udp|ip|udp_audio_port|udp_control_port
Name|ha|ip|tcp_port|udp_audio_port|udp_control_port
```

Current endpoints may append audio capability fields:

```text
Name|tcp|ip|tcp_port|audio_mode|tx_formats|rx_formats
Name|udp|ip|udp_audio_port|udp_control_port|audio_mode|tx_formats|rx_formats
```

`tx_formats` and `rx_formats` are semicolon-separated tokens:

```text
sample_rate:pcm_format:channels:frame_ms
```

Each list may contain up to 8 formats, ordered by endpoint preference. The
first compatible format selected by the constrained side wins during START /
ANSWER negotiation. `pcm_format` is the source of truth for container size and
significant bits; do not treat it as independent from a separate
`bits_per_sample` field.

Example:

```text
Panel|tcp|192.168.1.40|6054|full_duplex|16000:s16le:1:32|48000:s32le:1:20
```

Missing capability fields mean the legacy format `16000:s16le:1:32`. For UDP
rows every advertised frame must fit in one safe datagram; HA rejects UDP
formats above the safe payload threshold instead of relying on IP
fragmentation. TCP rows may advertise larger frames because TCP carries framed
payloads and does not depend on datagram MTU.

`ha` is a bridge/role marker. Its row carries both HA transport endpoints so TCP
firmware can shape it to `Name|tcp|ip|tcp_port` and UDP firmware can shape it to
`Name|udp|ip|udp_audio_port|udp_control_port` locally. Cross-protocol ESP rows
are shaped the same way: the display/call destination name stays the real peer,
but the dial endpoint points to HA.

## Short Manual Rows

Firmware also accepts short rows for local YAML scripts:

```text
Name
Name|ip
Name|ip|port
Name|ip|audio_port|control_port
```

Short rows are interpreted according to the receiving device transport. They
are useful for fixed local ESP-only scripts, but public packages should publish
canonical rows because canonical rows can represent TCP, UDP and HA in one
roster.

## HA Publisher Model

- HA builds one logical roster of `PhonebookEntry` objects.
- HA exposes that roster through one authoritative entity.

```text
sensor.intercom_phonebook                       # short state: "N entries"
sensor.intercom_phonebook.attributes.phonebook  # protocol-aware CSV roster
```

Firmware packages subscribe to the `phonebook` attribute, not to the short sensor
state. `intercom_api` normalizes that roster locally:

- TCP firmware keeps TCP peers direct and shapes UDP peers to the HA TCP bridge.
- UDP firmware keeps UDP peers direct and shapes TCP peers to the HA UDP bridge.
- Cross-protocol entries point to HA, but preserve the real destination name in
  the call payload so HA can bridge.

## ESP Phonebook Model

`ContactEntry` contains at least:

```cpp
struct ContactEntry {
  std::string name;
  std::string ip;
  ContactProtocol protocol;
  uint16_t port;
  uint16_t control_port;
  uint8_t missing_count;
};
```

Behavior:

- Merge by `name`.
- Keep slot order stable on updates.
- Do not silently drop malformed endpoint data without diagnostic logging.
- Batch replace/upsert should be explicit.
- Pruning by missing-count is optional internal behavior, not the primary user
  model.

## Routing Semantics

Same protocol:

```text
ESP A -> phonebook entry for ESP B -> direct ESP A <-> ESP B
```

Cross protocol:

```text
ESP A -> phonebook entry for ESP B via HA -> HA bridge -> ESP B
```

HA PBX override:

```text
ESP A -> HA entry for every destination -> HA bridge -> selected destination
```

HA softphone:

```text
Selected contact name == hass.config.location_name
```

In that case the card acts as a softphone extension. Otherwise the card mirrors
the selected ESP and uses the ESP's real call/answer and decline/hangup
controls.

## Reason Contract

Reasons are protocol payload.

- `DECLINE(reason)` carries a UTF-8 reason string.
- Empty decline during cancel/hangup is rendered as normal remote hangup by the
  peer.
- Non-empty reason is surfaced verbatim to the caller.
- HA bridge must forward reasons, not replace them.
- DND is implemented as `DECLINE("DND")`.
- User automation reasons are allowed and must transit unchanged.

Canonical reasons currently expected by UI/automation:

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

Free-form text is also valid.

## mDNS Mode

Standard HA-managed firmware does not run ESP-side mDNS announce/discovery.
The ESP publishes `intercom_endpoint` over the native ESPHome API and HA owns
the central phonebook.

Use this rule when choosing a discovery path:

- With HA installed: use the `phonebook` attribute of
  `sensor.intercom_phonebook`.
- Without HA as phonebook authority: optionally use ESP-side mDNS discovery.

Do not combine ESP-side mDNS discovery with the standard HA-managed packages as
a way to solve routing. In VPN, VLAN or routed subnet deployments, the fix is
correct address advertisement and bidirectional reachability for the endpoints
inside `sensor.intercom_phonebook`'s `phonebook` attribute.

For ESP-only deployments, include `packages/intercom/mdns_discovery.yaml`.
That package enables both:

- mDNS announce: publishes TXT `endpoint=<Name|protocol|ip|ports>`.
- mDNS discovery: scans `_intercom-tcp._tcp` and `_intercom-udp._udp`, parses
  the same endpoint rows, and merges matching peers into the normal phonebook.

HA advertises its own peer row on both services when the corresponding
transport is enabled:

```text
Name|ha|ip|tcp_port|udp_audio_port|udp_control_port
```

That HA mDNS advertisement is for compatibility with ESP-only discovery flows.
It is not the source of truth for normal HA-managed firmware.

## Non-Goals

- Do not introduce SIP, SDP, WebRTC, MQTT, or protobuf just to solve local LAN
  phonebook routing.
- Do not replace friendly-name call IDs with opaque IDs in this phase.
- Do not make the HA card infer private routing rules that belong in the
  phonebook/protocol contract.
