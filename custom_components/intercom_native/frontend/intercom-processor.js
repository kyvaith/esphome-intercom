/**
 * Intercom AudioWorklet Processor
 * Based on Home Assistant's recorder-worklet.js
 * This processor runs in a separate audio thread and converts
 * Float32 audio samples to Int16 PCM format at 16kHz.
 */

const TARGET_SAMPLE_RATE = 16000;

class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetSamples = 512; // 32ms chunks @ 16kHz, matches ESP AUDIO_CHUNK_SIZE
    this._buffers = [
      new Int16Array(this._targetSamples),
      new Int16Array(this._targetSamples)
    ];
    this._activeBuffer = 0;
    this._writeIndex = 0;
    this._frameCount = 0;
    this._chunksSent = 0;
    this._totalSamplesProcessed = 0;

    // Resampling state - works for any input rate (44.1kHz, 48kHz, etc)
    this._resampleRatio = sampleRate / TARGET_SAMPLE_RATE;
    this._position = 0;
    this._lastSample = 0;

    // Send init message to main thread
    this.port.postMessage({
      type: "debug",
      message: `Worklet v2.4.0: ${sampleRate}Hz -> ${TARGET_SAMPLE_RATE}Hz`
    });
  }

  _writeSample(sample) {
    const s = Math.max(-1, Math.min(1, sample));
    this._buffers[this._activeBuffer][this._writeIndex++] =
      s < 0 ? s * 0x8000 : s * 0x7fff;

    if (this._writeIndex !== this._targetSamples) return;

    const frame = this._buffers[this._activeBuffer];
    this._chunksSent++;
    try {
      this.port.postMessage({ type: "audio", buffer: frame.buffer }, [frame.buffer]);
    } catch (err) {
      console.error("[IntercomProcessor] postMessage error:", err);
    }
    this._buffers[this._activeBuffer] = new Int16Array(this._targetSamples);
    this._activeBuffer ^= 1;
    this._writeIndex = 0;
  }

  process(inputList, _outputList, _parameters) {
    this._frameCount++;

    // Check input validity
    if (!inputList || inputList.length === 0) {
      return true;
    }

    if (!inputList[0] || inputList[0].length === 0) {
      return true;
    }

    const float32Data = inputList[0][0]; // First channel of first input
    if (!float32Data || float32Data.length === 0) {
      return true;
    }

    if (sampleRate === TARGET_SAMPLE_RATE) {
      for (let i = 0; i < float32Data.length; i++) this._writeSample(float32Data[i]);
    } else {
      const ratio = this._resampleRatio;
      while (this._position < float32Data.length) {
        const idx = Math.floor(this._position);
        const frac = this._position - idx;
        const a = idx > 0 ? float32Data[idx - 1] : this._lastSample;
        const b = float32Data[idx] ?? a;
        this._writeSample(a + (b - a) * frac);
        this._position += ratio;
      }
      this._position -= float32Data.length;
      this._lastSample = float32Data[float32Data.length - 1] || this._lastSample;
    }
    this._totalSamplesProcessed += float32Data.length;

    return true; // Keep processor alive
  }
}

registerProcessor("intercom-processor", RecorderProcessor);
