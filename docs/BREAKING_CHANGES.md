# Breaking changes

## 2026.7.0-dev: source-based full media path and native 48 kHz intercom presets

`2026.7.0-dev` is a prerelease. It is intended for users who want to test the
new full-experience audio/media path and Sendspin/Music Assistant integration
before the next stable release.

Maintained full-experience YAMLs now use ESPHome's `speaker_source` media
player path. Media, announcements, timer sounds, local audio files and optional
Sendspin streams enter one media player and then flow through the existing
mixer. Intercom still has its own mixer source and keeps priority through the
existing call-state logic. Custom YAMLs copied from older full-experience files
should migrate away from `platform: speaker` media-player blocks and local
`files:` entries toward `media_source` plus `media_player.play_media` with
`audio-file://...` URLs.

Sendspin is included in maintained full-experience profiles as an experimental
media source. It is not required for normal HA media, TTS, timer sounds,
ringtones, Voice Assistant or intercom calls. If Sendspin causes glitches in a
specific installation, disable only that source or continue testing on TCP/HA
media while leaving the shared mixer path in place.

Native ESPHome intercom-only presets now use 48 kHz PCM where the actual
native I2S microphone or speaker path supports it. TCP native profiles use
20 ms frames. UDP native profiles use 10 ms frames so 48 kHz/s16/mono remains
below the default 1200-byte UDP datagram limit. AFE/AEC-backed profiles keep
16 kHz TX because that is the Espressif AFE/AEC output format; this is a local
branch constraint, not a global intercom transport limit.

UDP custom formats are validated against `udp_max_payload` at build time and by
Home Assistant when publishing the phonebook. The default is intentionally
conservative at 1200 bytes per audio frame. If you deliberately run larger LAN
datagrams, set the same larger `udp_max_payload` in the ESPHome YAML and the
Intercom Native integration options; otherwise use TCP for high-rate, stereo or
32-bit PCM.

ESP endpoint publication now waits for a valid IPv4 address from ESPHome's
network API and republishes on Wi-Fi or Ethernet IP events. Endpoint sensors
should no longer publish incomplete rows before the board has a usable address.

Older upgrade notes are kept in their original GitHub release pages. This file
tracks only the current upgrade delta from `2026.6.3` to `2026.7.0-dev`.
