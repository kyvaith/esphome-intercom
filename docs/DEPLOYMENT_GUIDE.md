# Deployment guide

A short map of the YAML tree so you can pick the right starting point for
a new device without reading every file.

```
yamls/
├── intercom-only/     no wake word, no voice assistant
│   ├── single-bus/    esp_audio_stack owns a coordinated mic/speaker bus
│   └── dual-bus/      esp_audio_stack owns separate I2S RX/TX controllers
├── full-experience/   intercom + wake word + voice assistant
│   ├── single-bus/    esp_audio_stack + esp_aec or esp_afe
│   ├── dual-bus/      esp_audio_stack full profiles with separate RX/TX buses
│   └── esphome-native/ native ESPHome audio for processed/separate paths
└── experimental/      bring-up/reference YAMLs, not release baselines
```

Device-specific lab/debug YAMLs live under local-only `yamls/debug/`.
Reusable debug overlays live under `packages/debug/`.

## Minimum versions

- ESPHome `2026.5.x` or newer is required for the maintained YAMLs.
- Home Assistant Core `2026.5.0` or newer is required for the bundled
  `intercom_native` integration/card.

## Decision tree

Follow the first branch that matches your hardware and intent.

1. **Do you want wake word + voice assistant on the device, or only
   intercom-style room-to-room calls?**
   - Just intercom → `yamls/intercom-only/`
   - Wake word / VA too → `yamls/full-experience/`

2. **How many I2S buses does your hardware expose?**
   - One shared audio bus, codec bus, or software-AEC/reference path →
     `single-bus/`
   - Two independent native mic/speaker paths with no software AEC requirement
     → native ESPHome examples under `intercom-only/esphome-native/`
   - Two mic/speaker I2S buses that still need software AEC/reference handling
     → `intercom-only/dual-bus/` for the maintained `esp_audio_stack` profiles.

3. **(full-experience) Which audio backend/processor profile fits?**
   - Codec/TDM boards and dual-mic targets → `esp_afe` presets with ESP-SR AFE,
     AEC, NS, AGC, VAD, and hardware/TDM playback reference.
   - Generic one-mic no-codec targets with 4 MB flash → `generic-s3-full-aec-*`,
     using standalone `esp_aec` plus the lighter `previous_frame` reference.
   - Generic one-mic no-codec targets with larger flash → `generic-s3-full-afe-*`,
     using `esp_afe` plus the canonical TYPE2-style software reference.

4. **Network transport: TCP (default), UDP, or both?**
   - **TCP** (default): framed PBX-lite protocol on `tcp_port` (default 6054). Start here for routed networks, VLANs, HA Container/Docker installs, Wi-Fi segments with filtering, or any deployment where predictable delivery matters more than shaving protocol overhead.
   - **UDP**: pick the matching `*-udp.yaml` variant from the same tier as the TCP file (`intercom-only` or `full-experience/single-bus`). Audio on `udp_audio_port` (default 6054, different protocol stack from TCP), control on `udp_control_port` (default 6055). Same `MessageHeader` framing on the control socket as TCP, raw negotiated PCM on the audio socket. UDP is a good fit for simple LANs where low latency is the priority and the network passes the audio/control ports cleanly; packet loss is audible because audio datagrams are not retransmitted. The default `udp_max_payload` is 1200 bytes; raise it in both ESPHome YAML and HA integration options only after verifying larger UDP datagrams on your LAN.
   - The HA `Intercom Native` integration can serve **both protocols at the same time**: tick `use_tcp` and/or `use_udp` in the config flow (defaults: TCP on, UDP off). HA acts as the bridge for cross-protocol calls.

### Cross-protocol bridges (TCP <-> UDP)

A deployment with some ESPs on TCP firmware and some on UDP firmware bridges across protocols transparently through HA. The decision is made per-leg of every `BridgeSession`, so source and destination can use different transports.

In standard HA-managed packages, ESP firmware does not advertise or discover
peers over mDNS. Each ESP publishes `sensor.<device>_intercom_endpoint` through
the native ESPHome API, HA builds the `phonebook` attribute of
`sensor.intercom_phonebook`, and firmware subscribes to that attribute.

For ESP-only deployments without HA, include
`packages/intercom/mdns_discovery.yaml`. That package advertises and discovers
the same canonical endpoint row over mDNS TXT:

```text
endpoint=Name|tcp|ip|tcp_port
endpoint=Name|udp|ip|udp_audio_port|udp_control_port
```

Cross-protocol bridging is HA's job, not mDNS's. mDNS never crosses protocols.

What you can mix (HA handles the bridge automatically):

| Source firmware | Dest firmware | Bridge result |
|---|---|---|
| TCP | TCP | TCP <-> TCP via HA TCP client per leg |
| TCP | UDP | TCP source leg + UDP dest leg, audio queued through HA |
| UDP | TCP | UDP source leg + TCP dest leg, audio queued through HA |
| UDP | UDP | both legs share HA's UDP socket manager |

### Audio format deployment rules

The intercom transport is no longer globally fixed to 16 kHz/s16. Audio format
is derived from the real component attached to each direction:

| Direction source/sink | Published format behavior |
|---|---|
| `esp_afe` / `esp_aec` processed microphone output | 16 kHz, s16, mono; this is the Espressif esp-sr surface. |
| Native ESPHome microphone/speaker path | The declared component format, if it is one of the supported intercom formats. |
| HA browser softphone TX | Mono formats supported by the browser engine. |
| HA browser softphone RX | Mono/stereo formats supported by the browser engine. |

Formats are per direction, not per device. A full-duplex device can transmit
`16000:s16le:1:32` from its AFE mic branch while receiving a different format
on its native speaker branch. START advertises the caller's TX/RX capabilities;
ANSWER confirms the exact caller-to-destination and destination-to-caller
formats selected for that call.

Home Assistant bridge sessions can convert between different negotiated leg
formats. The converter handles:

- sample-rate conversion;
- PCM container conversion (`s16le`, `s24le`, `s24le_in_s32`, `s32le`);
- mono/stereo channel mapping;
- frame-duration reframing when source and destination `frame_ms` differ.

Direct ESP-to-ESP calls do not have HA in the media path. Those calls require a
common format between caller TX and callee RX, and between callee TX and caller
RX. If no common format exists, the call is rejected with
`incompatible_audio_format` instead of falling back silently.

UDP carries one complete PCM frame per datagram. Any UDP endpoint format whose
frame payload is above the configured `udp_max_payload` is rejected by HA
during phonebook parsing. Use TCP for high-rate/stereo/32-bit formats or lower
the rate, channel count, container size or frame duration for UDP.

The default threshold is 1200 bytes of PCM payload per frame. It is deliberately
below the 1500-byte LAN MTU because MTU includes IP/UDP headers and may be
reduced by IPv6, VLANs, VPNs or tunnels. This keeps UDP audio to one
unfragmented datagram on ordinary home networks. ESPHome also checks this at
compile time for UDP YAMLs and fails with a clear error if `audio.tx` or
`audio.rx` is too large. Advanced LANs can raise `udp_max_payload` in both the
ESPHome YAML and the HA integration options.

### go2rtc as an optional secondary consumer

Audio path HA <-> ESP runs natively on UDP; **go2rtc is not required**.
If you already use go2rtc for cameras/streams, you can tap the audio
without disrupting the call routing: configure an `exec:` source in
go2rtc to read raw PCM from the same UDP audio port (default 6054):

```yaml
streams:
  intercom_spotpear_ball_v2:
    - exec:ffmpeg -f s16le -ar 16000 -ac 1 -i udp://0.0.0.0:6054 -c:a libopus -f rtsp rtsp://127.0.0.1:8554/intercom_spotpear_ball_v2#audio=opus
```

Caveats:
- The `ffmpeg` `-f/-ar/-ac` arguments must match the negotiated PCM format on
  the tapped endpoint. The example above is only correct for
  `16000:s16le:1:*`.
- The ESP audio socket binds **only during a call** (lazy lifecycle). Pair go2rtc with HA "snapshot on call" or similar so the consumer is up at the right time.
- This is one-way (ESP -> go2rtc). The go2rtc backchannel (browser -> ESP audio) goes through the Intercom Native card path on the HA side, not through go2rtc, to avoid the WebRTC/Opus transcode hop.
- HA 2024.11+ ships go2rtc bundled - no manual install. HASSOS friendly.

## Concrete examples

| Device | Chip | Mics | Config | YAML |
|---|---|---|---|---|
| Waveshare S3 Audio board | ESP32-S3 | 2 (ES7210 TDM, ref slot 2) | full + afe | `full-experience/single-bus/waveshare-s3-full-afe-tcp.yaml` |
| Waveshare S3 Audio board (UDP) | ESP32-S3 | 2 (ES7210 TDM, ref slot 2) | full + afe + udp | `full-experience/single-bus/waveshare-s3-full-afe-udp.yaml` |
| Waveshare P4 touch panel portrait (experimental, TCP) | ESP32-P4 | 2 (ES7210 TDM, ref slot 1) | full + afe | `full-experience/single-bus/waveshare-p4-touch-full-afe-tcp-portrait.yaml` |
| Waveshare P4 touch panel portrait (experimental, UDP) | ESP32-P4 | 2 (ES7210 TDM, ref slot 1) | full + afe + udp | `full-experience/single-bus/waveshare-p4-touch-full-afe-udp-portrait.yaml` |
| Waveshare P4 touch panel landscape (experimental, TCP) | ESP32-P4 | 2 (ES7210 TDM, ref slot 1) | full + afe | `full-experience/single-bus/waveshare-p4-touch-full-afe-tcp-landscape.yaml` |
| Waveshare P4 touch panel landscape (experimental, UDP) | ESP32-P4 | 2 (ES7210 TDM, ref slot 1) | full + afe + udp | `full-experience/single-bus/waveshare-p4-touch-full-afe-udp-landscape.yaml` |
| Spotpear Ball v2 | ESP32-S3 | 1 (ES8311 ADC) | full + afe (SR low-cost) | `full-experience/single-bus/spotpear-ball-v2-full-afe-tcp.yaml` |
| Spotpear Ball v2 (UDP) | ESP32-S3 | 1 (ES8311 ADC) | full + afe + udp | `full-experience/single-bus/spotpear-ball-v2-full-afe-udp.yaml` |
| Spotpear Ball v2 intercom-only | ESP32-S3 | 1 | intercom + single | `intercom-only/single-bus/spotpear-ball-v2-intercom-tcp.yaml` |
| Generic S3 speaker + MEMS dual-bus (TCP) | ESP32-S3 | 1 | intercom + dual + previous-frame ref | `intercom-only/dual-bus/generic-s3-intercom-tcp.yaml` |
| Generic S3 speaker + MEMS dual-bus (UDP) | ESP32-S3 | 1 | intercom + dual + previous-frame ref | `intercom-only/dual-bus/generic-s3-intercom-udp.yaml` |
| Generic S3 single-bus MEMS+amp | ESP32-S3 | 1 | intercom + duplex (TCP) | `intercom-only/single-bus/generic-s3-intercom-tcp.yaml` |
| Generic S3 single-bus MEMS+amp (UDP) | ESP32-S3 | 1 | intercom + duplex (UDP) | `intercom-only/single-bus/generic-s3-intercom-udp.yaml` |
| Spotpear Ball v2 intercom-only (UDP, LVGL) | ESP32-S3 | 1 | intercom + duplex (UDP) | `intercom-only/single-bus/spotpear-ball-v2-intercom-udp.yaml` |
| Generic S3 single-bus MEMS+amp + VA/MWW light | ESP32-S3 | 1 | full + aec + previous-frame ref | `full-experience/single-bus/generic-s3-full-aec-tcp.yaml` |
| Generic S3 single-bus MEMS+amp + VA/MWW light (UDP) | ESP32-S3 | 1 | full + aec + udp + previous-frame ref | `full-experience/single-bus/generic-s3-full-aec-udp.yaml` |
| Generic S3 single-bus MEMS+amp + VA/MWW AFE | ESP32-S3 | 1 | full + afe + TYPE2 ref, >4 MB app slot | `full-experience/single-bus/generic-s3-full-afe-tcp.yaml` |
| Generic S3 single-bus MEMS+amp + VA/MWW AFE (UDP) | ESP32-S3 | 1 | full + afe + udp + TYPE2 ref, >4 MB app slot | `full-experience/single-bus/generic-s3-full-afe-udp.yaml` |

### TDM ref slot per board

`packages/codec/es7210_tdm.yaml` is the Korvo-2 baseline (MIC3 / slot 2 = AEC reference at 30 dB). Boards with different DAC -> ADC wiring must override the affected PGA register from their own `on_boot` lambda **after** the baseline script runs and set `tdm_ref_slot` accordingly:

| Board | DAC routed to | YAML setting | PGA override |
|---|---|---|---|
| WS3 / Spotpear / Korvo-2 | MIC3 / slot 2 | `tdm_ref_slot: 2` (baseline) | none |
| Waveshare P4 Touch | MIC2 / slot 1 | `tdm_ref_slot: 1` | reset MIC2 PGA to 0 dB |

If the chosen ref slot stays silent while the speaker is active, the audio stack driver emits a one-shot WARN ("TDM AEC reference silent for N frames..."). See [troubleshooting](troubleshooting.md#warn-tdm-aec-reference-silent).

### AEC engine standard: VOIP for intercom-only, SR for full-experience

The `AEC Mode` select in every public YAML is restricted to a single esp-sr
engine to keep runtime mode switches stable. esp-sr's `aec_create()` has a
silent FFT-table calloc-fail bug on cross-engine transitions when
`filter_length > 4`; staying inside one engine sidesteps it entirely.

| Tier | filter_length | mode default | select runtime | Engine |
|---|---|---|---|---|
| **Intercom-only** (no MWW) | 8 | `voip_high_perf` | `voip_low_cost`, `voip_high_perf` | dios_ssp_aec |
| **Full-experience AEC** (with MWW) | 4 | `sr_low_cost` | `sr_low_cost`, `sr_high_perf` | esp_aec3 |

Why the split:

- **Intercom-only** wants human-ear quality, so the VOIP modes' non-linear
  residual echo suppressor is exactly what you want. `filter_length: 8` is
  required because voip frames are 16 ms each (vs 32 ms in SR), so 8 taps
  give the same 128 ms of echo coverage as `filter_length: 4` in SR.
- **Full-experience AEC** runs MWW on the post-AEC mic, and the VOIP RES
  destroys the spectral features the wake-word neural model relies on
  (10/10 → 2/10 detection rate observed). SR modes are linear-only and
  preserve the spectrum, so MWW keeps working.

The maintained generic AEC profiles explicitly use `aec_reference:
previous_frame` to avoid compiling the TYPE2 ring path. The component default is
`ring_buffer`, which is the Espressif/ADF TYPE2-style software reference and is
used by the heavier generic AFE preset.

## Home Assistant network requirements

`intercom_native` binds **TCP** and **UDP listener** sockets directly. Whether that works depends on how HA is installed:

| HA install | Default network mode | Result |
|---|---|---|
| HA OS / Supervised | container is `--network=host` | works out of the box; mDNS multicast passes correctly |
| HA Container (Docker) | bridge unless flagged | **must** be started with `--network=host` (also recommended by official HA docs). Bridge mode would require manual port forwarding for `tcp_port` / `udp_audio_port` / `udp_control_port`, plus an mDNS reflector and a `network: announced_addresses` override - not recommended |
| HA Core in venv | listens on host LAN | works out of the box |

Default ports (configurable from the integration config flow):

| Port | Purpose | Default |
|---|---|---|
| `tcp_port` | PBX-lite framed TCP | 6054 |
| `udp_audio_port` | Raw negotiated PCM audio | 6054 (different protocol stack from TCP) |
| `udp_control_port` | UDP `MessageHeader` signaling | 6055 |
| `udp_max_payload` | Maximum raw PCM bytes per UDP audio datagram | 1200 |

If `network.async_get_announce_addresses(hass)` returns empty, the integration logs a WARN: HA cannot enter the phonebook as a peer, so ESPs in `routing_mode: ha_pbx` cannot route until you configure either `network: announced_addresses:` or an `external_url`. Direct (`device_independent`) routing is unaffected.

If a port bind fails, the config entry transitions to `ConfigEntryError` instead of running half-broken.

## HA peer name

The HA peer name in every ESP phonebook is `hass.config.location_name` - whatever the user typed in HA Settings -> System -> General. It is **never hardcoded** to "Home Assistant" or any localized default. Examples: "Home", "Beach House", "Office", any user-chosen string.

Standard packages derive the HA peer name from the HA row in `sensor.intercom_phonebook`. The ESP-side default of `ha_peer_name_` is empty: an ESP in `routing_mode: ha_pbx` with no peer name yet logs an ERROR rather than guessing.

If you skip the standard packages and set up the phonebook purely from a YAML script, call `esphome.<slug>_set_ha_peer_name(name=hass.config.location_name)` yourself once at boot.

## Loading contacts from YAML

Public packages normally subscribe to the `phonebook` attribute of
`sensor.intercom_phonebook`; that is the standard contact source. If you
intentionally bypass that path, populate the phonebook from ESPHome YAML with
the native `intercom_api` actions:

```yaml
script:
  - id: load_manual_contacts
    then:
      - intercom_api.set_contacts:
          id: intercom
          contacts_csv: "Beach House|ha|192.168.1.10|6054|6054|6055,Kitchen|tcp|192.168.1.20|6054,Garage|udp|192.168.1.30|6054|6055"
      - intercom_api.set_ha_peer_name:
          id: intercom
          name: "Beach House"
```

The standard HA-callable ESPHome services remain call-control only:
`start_call(dest)`, `decline_call(reason)` and `set_ha_peer_name(name)`.

Notes:

- Dedup is by name only. Same name = same slot, no duplicates. Endpoint conflict: last writer wins (documented in `phonebook.h`).
- Empty phonebook at boot is normal: HA sensor subscription or a YAML script populates it.

## Routing policy (per device, runtime)

`routing_mode` lives on the ESP, set from YAML (or flipped at runtime via the `Routing Mode` switch in HA when exposed):

| Mode | Behavior |
|---|---|
| `device_independent` (default) | ESP dials the phonebook entry directly (peer ip+port from contacts). |
| `ha_pbx` | ESP dials the HA peer named by `hass.config.location_name`; HA bridges to the real destination. `dest_name` is preserved in the payload so HA knows where to forward. |

This is **per-device** and runtime-toggleable. There is no global integration "mode".

## Hardware sizing

- **Generic S3 full AEC** is the lighter 4 MB-oriented full-experience preset:
  VA, MWW, media, mixer and intercom stay enabled, while the processor is
  standalone `esp_aec` with `aec_reference: previous_frame`.
- **Generic S3 full AFE** is the heavier no-codec AFE preset: it enables
  `esp_afe` with NS/AGC/VAD and the TYPE2-style software reference. Use an app
  slot larger than the default 4 MB OTA slot; 8 MB or 16 MB flash is preferred.
  Move the example BCLK/LRCLK/DIN/LED pins away from ESP32-S3R8/S3R8V PSRAM pins.
- **AFE + 2 mic Speech Enhancement + two concurrent HTTPS streams** (music + TTS) is
  tight on internal RAM. On S3 boards enable
  `esp_audio_stack.audio_task_stack_in_psram: true` (see the
  `esp_audio_stack/README.md` "Advanced options" section). Keep hardware
  crypto enabled unless a current benchmark on the target board proves it
  should be changed.
- **1-mic SR low-cost** (Spotpear Ball v2) does not stress the budget and needs
  no extra tuning.
- **ESP32-P4** has a different memory and bus profile from S3. Treat P4
  tuning as board-specific: keep the provided P4 preset defaults unless a
  runtime trace says otherwise.

## Path resolution: local vs remote

Each public YAML carries one line per external resource. Three knobs:

- `substitutions.ext_components_source` - where ESPHome resolves
  `external_components` from. Local: `../../../esphome/components`.
  Remote: `github://OWNER/REPO@BRANCH`.
- `substitutions.assets_base` - where image/audio assets are fetched
  from. Local: `../../../`. Remote:
  `https://github.com/OWNER/REPO/raw/BRANCH/`.
- `packages:` entries - each one is either `!include ../../../packages/<name>.yaml`
  (local) or `github://OWNER/REPO/packages/<name>.yaml@BRANCH` (remote).

Public YAMLs ship in **remote mode** so they compile directly from GitHub
without manual path edits. Stable release YAMLs should point at `main`.
Development test YAMLs may intentionally point at `dev`; users testing that
train should download YAMLs from the `dev` branch so every package, component,
and asset resolves from the same branch.

If you are developing inside a local clone and want the YAMLs to point
back at your working tree, run:

```bash
./scripts/yaml_paths.sh local
```

To switch them back to release mode, point them at `main`, a release
branch, or your own fork:

```bash
./scripts/yaml_paths.sh remote --branch dev
./scripts/yaml_paths.sh remote --branch main
./scripts/yaml_paths.sh remote --url github://YOUR-USER/esphome-intercom --branch YOUR-BRANCH
./scripts/yaml_paths.sh check
```

For one-off local tweaks (wifi credentials, GPIO swaps, board-specific
sdkconfig), create a sibling `<board>-local.yaml` (gitignored via
`*-local.yaml`) that imports the public YAML and overrides only the
blocks you need.

## When to touch the sdkconfig

The YAMLs ship sdkconfig defaults sized for the common case. Board-specific
overrides that the defaults do not cover:

- `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY: "y"`, required if you set
  `esp_audio_stack.audio_task_stack_in_psram: true`. Already on in our
  YAMLs.
- `CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC` keeps large TLS allocations out of
  internal RAM. Some S3 full-experience YAMLs also enable
  `CONFIG_MBEDTLS_DYNAMIC_BUFFER`, but this is a workload-specific tradeoff:
  it can reduce steady-state TLS buffer cost while adding cold-path allocation
  churn. Do not copy it between boards without measuring first TTS/media start,
  largest internal block, and recovery after URL/HTTP errors.

## Pointers

- Per-component docs: `esphome/components/<name>/README.md`.
- Architecture overview: `docs/ARCHITECTURE.md`.
- Troubleshooting: `docs/troubleshooting.md`.
- YAML path toggle helper: `scripts/yaml_paths.sh`.
