#include "intercom_api.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cstdint>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "../audio_processor/audio_utils.h"

namespace esphome {
namespace intercom_api {

static const char *const TAG = "intercom_api.audio";
static constexpr uint8_t MAX_TX_BURST = 4;

void IntercomApi::debug_log_pcm_level_(const char *label, const uint8_t *pcm, size_t bytes,
                                       uint32_t &last_log_ms, uint32_t &frame_count) {
  frame_count++;
  const uint32_t now = millis();
  if (now - last_log_ms < 1000)
    return;
  last_log_ms = now;

  const size_t samples = bytes / sizeof(int16_t);
  if (pcm == nullptr || samples == 0) {
    ESP_LOGI(TAG, "AudioDebug[%s]: frames=%u bytes=%u empty state=%s",
             label, frame_count, (unsigned) bytes, this->get_call_state_str());
    return;
  }

  const auto levels = compute_levels_dbfs_i16(reinterpret_cast<const int16_t *>(pcm), samples);
  const char *path = "direct";
  ESP_LOGI(TAG,
           "AudioDebug[%s]: frames=%u bytes=%u samples=%u peak=%u peak_dbfs=%.1f rms_dbfs=%.1f "
           "intercom_volume=%.3f state=%s path=%s",
           label, frame_count, (unsigned) bytes, (unsigned) samples, (unsigned) levels.peak,
           levels.peak_dbfs, levels.rms_dbfs,
           this->volume_.load(std::memory_order_relaxed), this->get_call_state_str(), path);
}

#ifdef USE_INTERCOM_API_MIC
// === TX Task (Core 0) - Mic to Network ===

void IntercomApi::tx_task(void *param) {
  static_cast<IntercomApi *>(param)->tx_task_();
}

bool IntercomApi::is_tx_stream_ready_() const {
  return this->active_.load(std::memory_order_acquire) &&
         this->streaming_.load(std::memory_order_acquire) &&
         this->transport_ != nullptr && this->transport_->is_connected();
}

void IntercomApi::send_chunk_(const uint8_t *data, size_t length) {
  if (!this->is_tx_stream_ready_())
    return;
  if (this->audio_debug_) {
    this->debug_log_pcm_level_("tx_network", data, length,
                               this->audio_debug_last_tx_log_ms_, this->audio_debug_tx_frames_);
  }
  this->transport_->send_audio_frame(data, length);
}

void IntercomApi::process_tx_chunk_(const uint8_t *audio_chunk) {
  this->send_chunk_(audio_chunk, AUDIO_CHUNK_BYTES);
}

bool IntercomApi::read_tx_chunk_(uint8_t *audio_chunk) {
  return this->mic_buffer_->read(audio_chunk, AUDIO_CHUNK_BYTES, 0) == AUDIO_CHUNK_BYTES;
}

void IntercomApi::tx_task_() {
  ESP_LOGD(TAG, "TX task started");

  uint8_t *const audio_chunk = this->tx_audio_chunk_;

  while (true) {
    if (!this->is_tx_stream_ready_() || this->mic_buffer_->available() < AUDIO_CHUNK_BYTES) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    uint8_t burst = 0;
    while (this->is_tx_stream_ready_() && this->mic_buffer_->available() >= AUDIO_CHUNK_BYTES &&
           burst < MAX_TX_BURST) {
      if (!this->read_tx_chunk_(audio_chunk)) {
        break;
      }
      this->process_tx_chunk_(audio_chunk);
      burst++;
    }

    if (burst < MAX_TX_BURST) {
      // Producer notifies after every write; a give after this task's last take stays pending.
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    }
  }
}

// === Microphone Callback ===

void IntercomApi::on_microphone_data_(const uint8_t *data, size_t len) {
  if (!this->is_tx_stream_ready_()) {
    return;
  }
  if (this->mic_buffer_ == nullptr || data == nullptr || len == 0) {
    return;
  }

  // intercom_api accepts 16-bit mono PCM at 16 kHz. Direct microphones are
  // validated at config time; MicrophoneSource converts raw/experimental mics.
  const int16_t *src = reinterpret_cast<const int16_t *>(data);
  const size_t total_samples = len / sizeof(int16_t);
  // Skip our gain when esp_audio_stack owns the mic_gain entity (already applied upstream).
  int16_t *mic_converted = this->mic_converted_.load(std::memory_order_acquire);
  const float effective_gain = mic_converted != nullptr
      ? this->mic_gain_.load(std::memory_order_relaxed)
      : 1.0f;
  const bool needs_processing =
      mic_converted != nullptr && (effective_gain != 1.0f || this->dc_offset_removal_);

  if (needs_processing) {
    // Chunk by MIC_CONVERTED_SAMPLES so a long mic frame doesn't overflow
    // the staging buffer when gain/DC processing is on.
    size_t off = 0;
    while (off < total_samples) {
      const size_t chunk = std::min(total_samples - off, kMicConvertedSamples);
      if (this->dc_offset_removal_) {
        for (size_t i = 0; i < chunk; i++) {
          mic_converted[i] = scale_sample(this->dc_blocker_.process(src[off + i]), effective_gain);
        }
      } else {
        scale_block_i16(src + off, mic_converted, chunk, effective_gain);
      }
      this->mic_buffer_->write(mic_converted, chunk * sizeof(int16_t));
      if (this->tx_task_handle_ != nullptr) {
        xTaskNotifyGive(this->tx_task_handle_);
      }
      off += chunk;
    }
  } else {
    this->mic_buffer_->write(data, len);
    if (this->tx_task_handle_ != nullptr) {
      xTaskNotifyGive(this->tx_task_handle_);
    }
  }
}
#endif  // USE_INTERCOM_API_MIC

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32
