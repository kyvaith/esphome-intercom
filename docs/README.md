# Documentation

Welcome. These pages cover everything beyond the project pitch on the [top-level README](../README.md).

## Pick your path

- 🚀 **Start here**: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) is the decision
  tree that maps hardware and features (intercom only, VA + MWW, touch display,
  2-mic Speech Enhancement) to the right ready-to-flash config under
  [`yamls/`](../yamls/).

- 🧭 **Quick examples**: the top-level [README](../README.md#quick-start-examples)
  includes practical doorbell, room-to-room intercom, and manual boot phonebook
  examples.

- 🧾 **Release / upgrade notes**: [BREAKING_CHANGES.md](BREAKING_CHANGES.md)
  starts with the `2026.7.0-dev` prerelease notes for the source-based full
  media path, Sendspin testing and native 48 kHz intercom presets, then keeps
  older upgrade risks out of the main README. [RELEASE_2026_5_0.md](RELEASE_2026_5_0.md)
  contains the earlier "From PBX-like to PBX-lite" release note.

- 📚 **Configuration reference**: [reference.md](reference.md) covers every
  `intercom_api`, `esp_aec` and `esp_afe` option, every action and condition,
  Home Assistant services, and worked automation examples.

- 🔌 **Wire protocol**: [INTERCOM_PROTOCOL.md](INTERCOM_PROTOCOL.md) is the
  authoritative PBX-lite frame/reason/error/audio-format contract shared by the
  ESP C++ and HA Python implementations.

- 📒 **Phonebook protocol**: [PHONEBOOK_PROTOCOL.md](PHONEBOOK_PROTOCOL.md)
  documents canonical endpoint rows, `audio_mode`, `tx_formats`/`rx_formats`
  and how HA shapes TCP/UDP/HA rows for each ESP.

- 🧱 **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md) describes component
  decomposition, threading/core affinity, per-frame data flow, the
  `audio_processor` contract, and the drain protocol for glitch-free config
  changes.

- 🧯 **Troubleshooting**: [troubleshooting.md](troubleshooting.md) lists common
  symptoms (no devices found, no audio, echo, latency, ringing without connect,
  phonebook/discovery issues) with concrete checks.

- 🖼️ **Media refresh plan**: [MEDIA_SHOT_LIST.md](MEDIA_SHOT_LIST.md) lists
  screenshots, photos, GIFs and demo scenes that should replace obsolete README
  media.

## Per-component docs

Each ESPHome component ships its own README with the full option list, YAML snippets and component-specific notes:

- [`intercom_api`](../esphome/components/intercom_api/README.md), the PBX-lite TCP/UDP transport, call state machine and Home Assistant bridge.
- [`esp_audio_stack`](../esphome/components/esp_audio_stack/README.md), the
  full-duplex audio backend/wiki for shared codec buses, dual I2S MEMS/amp
  boards, Espressif rate/layout conversion, AEC reference capture, PSRAM
  placement and post-processor mic output.
- Full-experience media now uses ESPHome's source-based `speaker_source` path:
  HA media, announcements, local files and optional Sendspin streams feed one
  media player before the mixer arbitrates with intercom and Voice Assistant.
  The local [`speaker`](../esphome/components/speaker/README.md) fork remains
  documented for legacy/custom YAMLs that still use `platform: speaker`.
- [`esp_aec`](../esphome/components/esp_aec/README.md), standalone ESP-SR echo cancellation.
- [`esp_afe`](../esphome/components/esp_afe/README.md), the full Espressif AFE pipeline (AEC + NS + VAD + AGC, optional dual-mic Speech Enhancement).
- [`audio_processor`](../esphome/components/audio_processor/README.md), the abstract processor interface that `esp_aec` and `esp_afe` implement.
