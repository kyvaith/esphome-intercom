# 2026.7.0-dev prerelease notes

This prerelease is for field testing the next audio/media generation before it
is promoted to a stable release.

## Highlights

- `intercom_native` keeps the dedicated binary audio WebSocket, page-level card
  engine, server-authoritative teardown and negotiated PCM formats introduced
  in the previous dev cycle.
- Home Assistant bridge audio conversion is explicit and vectorized through
  NumPy, including anti-aliased sample-rate conversion.
- ESP endpoints publish canonical rows only after ESPHome reports a real IPv4
  address, and they republish on Wi-Fi or Ethernet IP changes.
- Browser/card reload during an active HA softphone call can explicitly rebind
  to the server session within a short grace window, so HA remains the lifecycle
  authority without forcing every dashboard refresh to tear down the call.
- Maintained full-experience YAMLs now use the `speaker_source` media path:
  normal HA media, announcements, timers, local files and Sendspin all enter one
  media player, then the mixer arbitrates with intercom and Voice Assistant.
- Sendspin / Music Assistant is included as an experimental source in full
  profiles. It uses 48 kHz mono PCM, PSRAM decode buffers and an adjustable
  180 ms static delay.
- Native ESPHome intercom-only presets now advertise 48 kHz PCM on the native
  I2S paths. TCP uses 20 ms frames; UDP uses 10 ms frames to stay below the
  default 1200-byte UDP payload ceiling.

## Compatibility notes

- AFE/AEC-backed microphone branches remain 16 kHz/s16/mono. That is the
  Espressif AFE/AEC output format. Use HA PBX bridging when mixing those
  endpoints with higher-rate native endpoints.
- UDP sends one complete PCM frame per datagram. The default limit is 1200
  bytes. Larger frames require TCP or an explicit `udp_max_payload` override on
  both ESPHome and Home Assistant after testing the LAN path.
- Sendspin is experimental in this project. It should not affect intercom,
  TTS, timers or normal HA media when unused, but micro-glitch tuning is still
  ongoing for some device/network combinations.

## Suggested test matrix

- HA softphone to ESP and ESP to HA at the legacy 16 kHz AFE path.
- Native intercom-only TCP at 48 kHz both directions.
- Native intercom-only UDP at 48 kHz/10 ms, verifying the endpoint is accepted
  by HA and does not exceed `udp_max_payload`.
- Full-experience media playback, timer sound, ringtone and intercom priority.
- Sendspin single speaker and grouped speakers, with static delay adjustment.
