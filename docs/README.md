# Documentation

Welcome. These pages cover everything beyond the project pitch on the [top-level README](../README.md).

## Pick your path

- **Choosing a YAML preset**: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) is a decision tree that maps hardware and features (intercom only, VA + MWW, touch display, 2-mic Speech Enhancement) to the right ready-to-flash config under [`yamls/`](../yamls/).

- **Quick start examples**: the top-level [README](../README.md#quick-start-examples) now includes practical doorbell, room-to-room intercom, and manual boot phonebook examples.

- **Release / upgrade notes**: [BREAKING_CHANGES.md](BREAKING_CHANGES.md)
  starts with the `2026.6.3` binary-softphone and negotiated-audio notes and
  keeps upgrade risks out of the main README. [RELEASE_2026_5_0.md](RELEASE_2026_5_0.md)
  contains the earlier "From PBX-like to PBX-lite" release note.

- **Media refresh plan**: [MEDIA_SHOT_LIST.md](MEDIA_SHOT_LIST.md) lists the screenshots, photos, GIFs and demo scenes that should replace the obsolete README media.

- **Configuration reference**: [reference.md](reference.md) covers every `intercom_api`, `esp_aec` and `esp_afe` option, every action and condition, the Home Assistant services, and worked automation examples.

- **Wire protocol**: [INTERCOM_PROTOCOL.md](INTERCOM_PROTOCOL.md) is the
  authoritative PBX-lite frame/reason/error/audio-format contract shared by the
  ESP C++ and HA Python implementations.

- **Phonebook protocol**: [PHONEBOOK_PROTOCOL.md](PHONEBOOK_PROTOCOL.md)
  documents canonical endpoint rows, `audio_mode`, `tx_formats`/`rx_formats`
  and how HA shapes TCP/UDP/HA rows for each ESP.

- **How the audio stack works**: [ARCHITECTURE.md](ARCHITECTURE.md) describes component decomposition, the threading and core-affinity model, the data flow per frame, the `audio_processor` contract, and the drain protocol for glitch-free config changes.

- **Troubleshooting**: [troubleshooting.md](troubleshooting.md) lists common symptoms (no devices found, no audio, echo, latency, ringing without connect, phonebook/discovery issues) with concrete checks.

## Per-component docs

Each ESPHome component ships its own README with the full option list, YAML snippets and component-specific notes:

- [`intercom_api`](../esphome/components/intercom_api/README.md), the PBX-lite TCP/UDP transport, call state machine and Home Assistant bridge.
- [`esp_audio_stack`](../esphome/components/esp_audio_stack/README.md), the
  full-duplex audio backend/wiki for shared codec buses, dual I2S MEMS/amp
  boards, Espressif rate/layout conversion, AEC reference capture, PSRAM
  placement and post-processor mic output.
- [`speaker`](../esphome/components/speaker/README.md), the narrow ESPHome
  speaker/media-player fork used by full-experience YAMLs so media pause
  releases the playback pipeline before TTS, timers or intercom need the
  speaker graph.
- [`esp_aec`](../esphome/components/esp_aec/README.md), standalone ESP-SR echo cancellation.
- [`esp_afe`](../esphome/components/esp_afe/README.md), the full Espressif AFE pipeline (AEC + NS + VAD + AGC, optional dual-mic Speech Enhancement).
- [`audio_processor`](../esphome/components/audio_processor/README.md), the abstract processor interface that `esp_aec` and `esp_afe` implement.
