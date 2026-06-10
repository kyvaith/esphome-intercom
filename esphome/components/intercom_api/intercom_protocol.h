#pragma once

#include <cstdint>
#include <cstring>
#include <string>

namespace esphome {
namespace intercom_api {

static constexpr uint16_t INTERCOM_PORT = 6054;  // default TCP listen port

enum class MessageType : uint8_t {
  AUDIO = 0x01,
  START = 0x02,
  HANGUP = 0x03,   // BYE. Pre-streaming cancel uses DECLINE.
  PING = 0x04,
  PONG = 0x05,
  ERROR = 0x06,    // technical fault
  RING = 0x07,     // provisional: dest is presenting the call
  ANSWER = 0x08,   // final: dest accepted
  DECLINE = 0x09,  // empty reason = silent remote_hangup; non-empty = user-visible
};

inline const char *message_type_name(MessageType type) {
  switch (type) {
    case MessageType::AUDIO:
      return "AUDIO";
    case MessageType::START:
      return "START";
    case MessageType::HANGUP:
      return "HANGUP";
    case MessageType::PING:
      return "PING";
    case MessageType::PONG:
      return "PONG";
    case MessageType::ERROR:
      return "ERROR";
    case MessageType::RING:
      return "RING";
    case MessageType::ANSWER:
      return "ANSWER";
    case MessageType::DECLINE:
      return "DECLINE";
    default:
      return "UNKNOWN";
  }
}

inline const char *message_type_name(uint8_t type) {
  return message_type_name(static_cast<MessageType>(type));
}

// u8-length-prefixed string fields; capped well below UDP MTU.
static constexpr size_t INTERCOM_MAX_CALL_ID_LEN = 64;
static constexpr size_t INTERCOM_MAX_ROUTE_ID_LEN = 64;
static constexpr size_t INTERCOM_MAX_NAME_LEN = 64;
static constexpr size_t INTERCOM_MAX_REASON_LEN = 160;

// Wire layout. Keep aligned with docs/INTERCOM_PROTOCOL.md and
// custom_components/intercom_native/protocol.py.
//
//   header (3 bytes): u8 type | u16 length (LE)
//   body: call_id_len:u8 | call_id[call_id_len] | per-type tail
//   PING/PONG bodies use call_id_len = 0.
//
// Helpers return bytes written/read or 0 on failure.

inline size_t encode_call_id_prefix(uint8_t *out, size_t out_cap, const std::string &call_id) {
  if (call_id.size() > INTERCOM_MAX_CALL_ID_LEN) return 0;
  size_t need = 1 + call_id.size();
  if (out_cap < need) return 0;
  out[0] = static_cast<uint8_t>(call_id.size());
  std::memcpy(out + 1, call_id.data(), call_id.size());
  return need;
}

inline size_t decode_call_id_prefix(const uint8_t *in, size_t in_len, std::string *out_call_id) {
  if (in_len < 1) return 0;
  uint8_t cid_len = in[0];
  if (in_len < 1u + cid_len) return 0;
  out_call_id->assign(reinterpret_cast<const char *>(in + 1), cid_len);
  return 1u + cid_len;
}

inline size_t encode_lp_string(uint8_t *out, size_t out_cap, const std::string &s, size_t max_len) {
  if (s.size() > max_len || s.size() > 0xFF) return 0;
  size_t need = 1 + s.size();
  if (out_cap < need) return 0;
  out[0] = static_cast<uint8_t>(s.size());
  std::memcpy(out + 1, s.data(), s.size());
  return need;
}

inline size_t decode_lp_string(const uint8_t *in, size_t in_len, std::string *out) {
  if (in_len < 1) return 0;
  uint8_t len = in[0];
  if (in_len < 1u + len) return 0;
  out->assign(reinterpret_cast<const char *>(in + 1), len);
  return 1u + len;
}

enum class ErrorCode : uint8_t {
  BUSY = 0x01,
};

enum class PcmFormat : uint8_t {
  S16LE = 1,
  S24LE = 2,
  S24LE_IN_S32 = 3,
  S32LE = 4,
};

struct AudioFormat {
  uint32_t sample_rate{16000};
  PcmFormat pcm_format{PcmFormat::S16LE};
  uint8_t channels{1};
  uint16_t frame_ms{32};

  uint8_t container_bytes_per_sample() const {
    switch (this->pcm_format) {
      case PcmFormat::S16LE:
        return 2;
      case PcmFormat::S24LE:
        return 3;
      case PcmFormat::S24LE_IN_S32:
      case PcmFormat::S32LE:
        return 4;
      default:
        return 0;
    }
  }

  size_t nominal_frame_samples() const {
    return static_cast<size_t>((static_cast<uint64_t>(this->sample_rate) * this->frame_ms) / 1000u);
  }

  size_t nominal_frame_bytes() const {
    return this->nominal_frame_samples() * this->channels * this->container_bytes_per_sample();
  }

  bool is_valid() const {
    const bool valid_rate = this->sample_rate == 8000 || this->sample_rate == 12000 ||
                            this->sample_rate == 16000 || this->sample_rate == 24000 ||
                            this->sample_rate == 32000 || this->sample_rate == 44100 ||
                            this->sample_rate == 48000;
    const bool valid_channels = this->channels == 1 || this->channels == 2;
    const bool valid_frame = this->frame_ms == 10 || this->frame_ms == 20 || this->frame_ms == 32;
    const bool whole_frames = (static_cast<uint64_t>(this->sample_rate) * this->frame_ms) % 1000u == 0;
    return valid_rate && valid_channels && valid_frame && whole_frames && this->container_bytes_per_sample() != 0;
  }

  bool operator==(const AudioFormat &other) const {
    return this->sample_rate == other.sample_rate &&
           this->pcm_format == other.pcm_format &&
           this->channels == other.channels &&
           this->frame_ms == other.frame_ms;
  }
};

static constexpr AudioFormat LEGACY_AUDIO_FORMAT{};
static constexpr size_t INTERCOM_MAX_AUDIO_FORMATS = 8;

struct AudioFormatList {
  AudioFormat formats[INTERCOM_MAX_AUDIO_FORMATS]{};
  uint8_t count{1};
};

inline void audio_format_list_legacy(AudioFormatList *out) {
  out->formats[0] = LEGACY_AUDIO_FORMAT;
  out->count = 1;
}

inline bool encode_u32_le(uint8_t *out, size_t out_cap, uint32_t value) {
  if (out_cap < 4) return false;
  out[0] = static_cast<uint8_t>(value & 0xFF);
  out[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
  out[2] = static_cast<uint8_t>((value >> 16) & 0xFF);
  out[3] = static_cast<uint8_t>((value >> 24) & 0xFF);
  return true;
}

inline uint32_t decode_u32_le(const uint8_t *in) {
  return static_cast<uint32_t>(in[0]) |
         (static_cast<uint32_t>(in[1]) << 8) |
         (static_cast<uint32_t>(in[2]) << 16) |
         (static_cast<uint32_t>(in[3]) << 24);
}

inline size_t encode_audio_format(uint8_t *out, size_t out_cap, const AudioFormat &fmt) {
  if (!fmt.is_valid() || out_cap < 8) return 0;
  encode_u32_le(out, out_cap, fmt.sample_rate);
  out[4] = static_cast<uint8_t>(fmt.pcm_format);
  out[5] = fmt.channels;
  out[6] = static_cast<uint8_t>(fmt.frame_ms & 0xFF);
  out[7] = static_cast<uint8_t>((fmt.frame_ms >> 8) & 0xFF);
  return 8;
}

inline size_t decode_audio_format(const uint8_t *in, size_t in_len, AudioFormat *out) {
  if (in_len < 8) return 0;
  AudioFormat fmt;
  fmt.sample_rate = decode_u32_le(in);
  fmt.pcm_format = static_cast<PcmFormat>(in[4]);
  fmt.channels = in[5];
  fmt.frame_ms = static_cast<uint16_t>(static_cast<uint16_t>(in[6]) |
                                       (static_cast<uint16_t>(in[7]) << 8));
  if (!fmt.is_valid()) return 0;
  *out = fmt;
  return 8;
}

// Legacy default: 16 kHz mono int16, 512 samples per chunk = 32 ms.
static constexpr uint32_t SAMPLE_RATE = LEGACY_AUDIO_FORMAT.sample_rate;
static constexpr size_t AUDIO_CHUNK_BYTES = 1024;

struct __attribute__((packed)) MessageHeader {
  uint8_t type;
  uint16_t length;  // little-endian on the wire
};

static constexpr size_t HEADER_SIZE = sizeof(MessageHeader);

// Explicit LE (de)serialisers; never memcpy the packed struct.
inline void encode_header(uint8_t *out, const MessageHeader &h) {
  out[0] = h.type;
  out[1] = static_cast<uint8_t>(h.length & 0xFF);
  out[2] = static_cast<uint8_t>((h.length >> 8) & 0xFF);
}
inline MessageHeader decode_header(const uint8_t *in) {
  MessageHeader h;
  h.type = in[0];
  h.length = static_cast<uint16_t>(static_cast<uint16_t>(in[1]) |
                                   (static_cast<uint16_t>(in[2]) << 8));
  return h;
}
static constexpr size_t MAX_AUDIO_CHUNK = 16 * 1024;
static constexpr size_t UDP_SAFE_AUDIO_PAYLOAD_BYTES = 1200;
static constexpr size_t MAX_CONTROL_PAYLOAD = 512;
static constexpr size_t MAX_MESSAGE_SIZE = HEADER_SIZE + MAX_AUDIO_CHUNK;

static constexpr size_t RX_BUFFER_SIZE = 8192;   // ~256 ms / 4 browser chunks
static constexpr size_t TX_BUFFER_SIZE = 4096;   // ~128 ms / 4 chunks @ 32 ms

static constexpr uint32_t PING_INTERVAL_MS = 5000;
// 3x PING tolerates dropped pings on a flaky LAN before declaring the peer dead.
static constexpr uint32_t KEEPALIVE_DEADLINE_MS = PING_INTERVAL_MS * 3;

}  // namespace intercom_api
}  // namespace esphome
