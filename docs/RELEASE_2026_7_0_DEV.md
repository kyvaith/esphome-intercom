# 2026.7.0-dev — negotiated audio formats, speaker-source media path and Sendspin field testing

This is a **development prerelease** for field testing the next intercom and
media generation before it becomes a stable release. It contains large changes
in Home Assistant, the Lovelace card, the ESP intercom protocol and the
full-experience audio path.

Use it if you want to help test. Stay on the latest stable release if the
device is installed somewhere where temporary audio glitches or call-routing
regressions would be a problem.

## 🏠 Intercom Native / Home Assistant

- 🧬 Added negotiated PCM audio formats to the HA side of the PBX-lite stack.
- 🔁 Audio formats are now **per direction**, not one global device format:
  `tx_format` describes device-to-wire audio and `rx_format` describes
  wire-to-device audio.
- 🎚️ HA can bridge endpoints with different TX/RX formats by converting the
  audio explicitly instead of pretending every call is 16 kHz/s16/mono.
- 🧮 HA bridge conversion is vectorized through NumPy and uses anti-aliased
  sample-rate conversion for downsampling.
- 🧩 START and ANSWER now carry versioned trailing audio-format blocks. Legacy
  16 kHz/s16/mono endpoints remain wire-compatible because the extension is
  omitted on pure legacy calls.
- 🧭 HA now validates endpoint-declared formats before routing calls. Incompatible
  peers fail with a readable `incompatible_audio_format` reason instead of
  producing distorted audio.
- 📡 TCP and UDP clients/managers understand the negotiated format selected for
  each call leg.
- 🛡️ Browser audio WebSocket sessions remain server-authoritative: if the socket
  is truly gone, the server ends the call.
- 🔄 Browser/card reload during an active HA softphone call can explicitly rebind
  to the existing server session within a short grace window, avoiding the old
  "dashboard refresh killed or desynced the softphone call" behavior.
- 🚫 Duplicate terminal/bridge events were tightened so cards should see one
  final reason instead of compensating for repeated `disconnected` events.
- 🧪 `tools/intercom_softphone_probe.py` was expanded for negotiated-format
  testing, tone/WAV injection, received-audio WAV capture and direct ESP/HA
  endpoint probes.

## 🎛️ Lovelace Card

- 🧠 The card keeps the page-level audio engine model from the previous dev
  cycle: one browser audio engine per page, cards as views.
- 🔌 Audio still uses the dedicated binary WebSocket, not the shared HA frontend
  WebSocket.
- 📦 The card receives the negotiated effective formats and configures capture
  and playback from that contract.
- 🎧 Capture and playback worklets support dynamic PCM settings instead of
  assuming 16 kHz `Int16Array` everywhere.
- 🔄 Softphone calls survive normal Lovelace reload/reconnect flows when the
  browser rebinds inside the server grace window.
- 🧭 Device identity remains `device_id` based in the frontend. Legacy
  name/esphome-id matching is resolved server-side, once.
- 🧪 The card version exposed in Lovelace is `v2026.7.0-dev`.

## 📞 Intercom Protocol

- 🧾 Protocol audio capability is now described with explicit PCM tokens such as
  `16000:s16le:1:32` and `48000:s16le:1:20`.
- 🔀 TX and RX capabilities are advertised separately, so a device can expose
  16 kHz microphone audio while accepting 48 kHz speaker audio.
- ✅ ANSWER confirms the effective caller-to-destination and
  destination-to-caller formats. The caller no longer has to guess how the
  remote side will send audio.
- 🧯 Mixed old/new fleets are handled conservatively: pure legacy calls stay
  legacy, while unsupported high-rate calls are declined or ended instead of
  playing garbage.
- 📚 `docs/INTERCOM_PROTOCOL.md` and `docs/PHONEBOOK_PROTOCOL.md` were updated
  for the format-aware endpoint rows and negotiated call setup.

## 🔊 ESP Audio Runtime

- 🧱 Runtime audio now works in explicit **audio frames/chunks** derived from the
  negotiated stream format instead of treating every path as fixed
  milliseconds around 16 kHz/s16/mono.
- 📐 Intercom TX/RX chunk sizes, ring sizes and frame validation are derived from
  `AudioFormat`.
- 🎚️ ESP speaker stream info is updated from the selected RX format before
  intercom playback, so native speaker paths can receive higher-rate PCM.
- 🎙️ AFE/AEC microphone branches remain 16 kHz/s16/mono because that is the
  Espressif AFE/AEC output contract.
- 🔄 Native ESPHome microphone/speaker paths can advertise higher-rate PCM when
  they bypass AFE/AEC.
- 🧼 TCP accept/error logs and several numeric intercom errors were made clearer
  for users reading logs.
- 🧰 Shared socket helpers and smaller transport cleanups reduced duplicated TCP
  and UDP code.
- 🧱 ESP audio stack dependencies and stream setup were tightened for cleaner
  build and runtime behavior.
- ⚙️ Maintained full YAMLs now enable ESPHome performance optimization through
  YAML build options, not one-off local build commands.

## 🎚️ Full-Experience Media Pipeline

- 🔊 Maintained full-experience presets now use the ESPHome `speaker_source`
  media path as the default media architecture.
- 🧩 Normal HA media, announcements, timer sounds, local files and Sendspin all
  enter one media player/source path, then the mixer arbitrates against
  intercom and Voice Assistant.
- 🥇 Intercom keeps priority through its dedicated mixer source.
- ⏲️ Timer/ringtone/media playback now follow the same source arbitration model
  instead of drifting through separate speaker paths.
- 🧹 The project-local speaker fork remains available for legacy/custom YAMLs,
  but maintained full profiles are moving to the `speaker_source` path.
- 🔇 Mute paths and source priority rules were kept aligned with the full UI
  state machine.

## 🎵 Sendspin / Music Assistant

- 🧪 Sendspin is included in full-experience profiles as an **experimental**
  Music Assistant source.
- 🎧 The current field-test default is 48 kHz mono PCM.
- 🧠 Decode buffers are configured for PSRAM where supported.
- ⏱️ Full profiles use an adjustable static delay baseline; WS3/Spotpear field
  testing currently uses `180 ms`.
- 👥 Grouped-speaker playback is part of the requested test matrix.
- ⚠️ Micro-glitch tuning is still ongoing for some device/network combinations.
  This prerelease intentionally exposes the feature so the real field data can
  be collected.

## 📡 Endpoint / Phonebook / Network Recovery

- 🌐 ESP endpoints now wait for a real ESPHome network IPv4 address before
  publishing the canonical endpoint sensor.
- 🔁 Endpoint rows are requested again when Wi-Fi or Ethernet IP changes, instead
  of requiring a reboot or relying on YAML-level automations.
- 🧭 HA rebuilds the phonebook from endpoint sensors and routes through canonical
  rows.
- 🧹 Documentation was cleaned up to state the HA-managed phonebook model once,
  instead of repeating the same "do not rely on mDNS as the primary contract"
  guidance in several places.

## 📦 YAML Presets

- 🧪 Native ESPHome intercom-only presets now advertise 48 kHz PCM where the
  native I2S path can support it.
- 🚦 TCP native intercom presets use larger 20 ms PCM frames.
- 🚦 UDP native intercom presets use 10 ms frames by default to stay under the
  conservative UDP payload ceiling.
- 🎙️ AFE/AEC full profiles keep 16 kHz TX from the AFE branch and can use
  higher-rate RX toward the speaker branch.
- 🔊 WS3 and Spotpear full profiles are aligned around 48 kHz RX for better
  HA-to-ESP playback quality.
- ⚙️ Full profiles include the shared performance build option baseline.
- 🧭 Release YAML references for this prerelease point to the `dev` branch.

## 🚧 UDP Payload Policy

- 📦 UDP sends one complete PCM frame per datagram.
- 🛡️ The default safe payload ceiling is `1200` bytes to avoid depending on IP
  fragmentation across Wi-Fi, VPNs or routed networks.
- ✅ ESPHome validation rejects UDP audio formats whose frame size exceeds the
  configured ceiling and tells the user to use TCP, reduce the format/frame size,
  or opt in with `udp_max_payload`.
- ⚠️ Larger LAN/Jumbo-frame deployments can override the limit, but both ESPHome
  and Home Assistant must be configured consistently and tested on that LAN.

## 📚 Documentation

- 📖 README was reworked for the 2026.7.0-dev audio/media direction.
- 📘 `docs/reference.md`, architecture docs, protocol docs and troubleshooting
  docs were audited for stale 16 kHz-only assumptions.
- 🧭 YAML selection docs now call out native high-rate paths, AFE/AEC 16 kHz
  branches, TCP vs UDP tradeoffs and Sendspin experimental status.
- 🧾 Breaking/compatibility notes were updated for negotiated formats and the
  new media path.
- 💚 Donation/support messaging was made more visible near the top of the README
  while keeping the existing footer banner.

## ⚠️ Compatibility / Upgrade Notes from 2026.6.x

- Custom ESP YAMLs using `intercom_api` should review the new `audio.tx` and
  `audio.rx` settings if they want anything other than legacy 16 kHz/s16/mono.
- AFE/AEC TX remains 16 kHz/s16/mono. This is expected and should not be treated
  as an intercom transport limitation.
- HA bridging is the supported way to connect devices with incompatible direct
  audio formats.
- UDP high-rate audio is limited by datagram size. Use TCP unless the UDP frame
  size is known to fit the configured payload ceiling.
- Sendspin is experimental. Normal intercom, HA media, TTS and timers should
  continue to work when Sendspin is unused, but grouped Sendspin playback still
  needs field validation.
- The HA custom integration now requires NumPy. HA OS wheel availability was
  checked for modern Python/aarch64/x86_64 targets, but this remains one of the
  areas to watch during prerelease testing.
- Legacy mixed-fleet behavior is conservative: a new high-rate endpoint calling
  an old firmware endpoint may fall back only when both sides are legacy; if the
  format cannot be confirmed safely, the call is rejected or ended.

## 🧪 Suggested Field Test Matrix

- 🏠 HA softphone -> ESP and ESP -> HA on AFE/AEC devices at 16 kHz.
- 🔄 Browser refresh/reload during an active HA softphone call; the card should
  converge back to the server session.
- 🎙️ Native intercom-only TCP at 48 kHz both directions.
- 📡 Native intercom-only UDP at 48 kHz/10 ms, verifying HA accepts the endpoint
  and the frame size stays below `udp_max_payload`.
- 🔀 ESP-to-ESP direct calls with matching formats.
- 🌉 ESP-to-ESP calls through HA PBX bridging with different TX/RX formats.
- ✅ Auto Answer on/off from both sides: ESP ringing, HA ringing, Answer,
  Decline and Hangup.
- 🎛️ Hybrid card behavior: Call from HA, Answer/Decline mirrored to the attached
  ESP where applicable.
- 🔊 Full-experience media playback, timer sound, ringtone and intercom priority.
- 🎵 Sendspin single speaker, duplicate cards open, grouped speakers and static
  delay adjustment.
- 🧯 Long playback/call sessions with logs checked for underruns, stuck media
  sources, stale sessions and warning spam.
