const DEFAULT_FORMAT = Object.freeze({ sampleRate: 16000, pcmFormat: "s16le", channels: 1, frameMs: 32 });
const RING_FRAMES = 8;
const START_FRAMES = 2;
const DROP_FRAMES = 6;

function normaliseFormat(value) {
  const fmt = value || DEFAULT_FORMAT;
  const sampleRate = Number(fmt.sampleRate) || DEFAULT_FORMAT.sampleRate;
  const frameMs = Number(fmt.frameMs) || DEFAULT_FORMAT.frameMs;
  const channels = Number(fmt.channels) || DEFAULT_FORMAT.channels;
  const pcmFormat = ["s16le", "s24le", "s24le_in_s32", "s32le"].includes(fmt.pcmFormat)
    ? fmt.pcmFormat
    : DEFAULT_FORMAT.pcmFormat;
  const bytesPerSample = pcmFormat === "s16le" ? 2 : pcmFormat === "s24le" ? 3 : 4;
  return {
    sampleRate,
    frameMs,
    channels,
    pcmFormat,
    bytesPerSample,
    frameSamples: Math.floor((sampleRate * frameMs) / 1000),
  };
}

class IntercomPlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._format = normaliseFormat(options?.processorOptions?.format);
    this._contextFrameSamples = Math.max(1, Math.round(this._format.frameSamples * sampleRate / this._format.sampleRate));
    this._ring = new Float32Array(this._contextFrameSamples * this._format.channels * RING_FRAMES);
    this._read = 0;
    this._write = 0;
    this._available = 0;
    this._started = false;
    this._framesIn = 0;
    this._framesOut = 0;
    this._framesDrop = 0;
    this._lastStats = 0;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data?.type === "audio" && data.buffer) this._push(data.buffer);
    };
  }

  _decode(view, sampleIndex) {
    const offset = sampleIndex * this._format.bytesPerSample;
    if (this._format.pcmFormat === "s16le") return view.getInt16(offset, true) / 32768;
    if (this._format.pcmFormat === "s24le") {
      let v = view.getUint8(offset) | (view.getUint8(offset + 1) << 8) | (view.getUint8(offset + 2) << 16);
      if (v & 0x800000) v |= 0xff000000;
      return v / 8388608;
    }
    if (this._format.pcmFormat === "s24le_in_s32") return view.getInt32(offset, true) / 2147483648;
    return view.getInt32(offset, true) / 2147483648;
  }

  _push(buffer) {
    const frameBytes = this._format.frameSamples * this._format.channels * this._format.bytesPerSample;
    if (buffer.byteLength !== frameBytes) return;
    if (this._available >= this._contextFrameSamples * this._format.channels * DROP_FRAMES) {
      this._read = (this._read + this._contextFrameSamples * this._format.channels) % this._ring.length;
      this._available -= this._contextFrameSamples * this._format.channels;
      this._framesDrop++;
    }
    const view = new DataView(buffer);
    for (let i = 0; i < this._contextFrameSamples; i++) {
      const srcPos = i * this._format.sampleRate / sampleRate;
      const base = Math.floor(srcPos);
      const frac = srcPos - base;
      for (let ch = 0; ch < this._format.channels; ch++) {
        const a = this._decode(view, base * this._format.channels + ch);
        const bIndex = Math.min(this._format.frameSamples - 1, base + 1);
        const b = this._decode(view, bIndex * this._format.channels + ch);
        this._ring[this._write] = a + (b - a) * frac;
        this._write = (this._write + 1) % this._ring.length;
      }
    }
    this._available += this._contextFrameSamples * this._format.channels;
    this._framesIn++;
    if (!this._started && this._available >= this._contextFrameSamples * this._format.channels * START_FRAMES) {
      this._started = true;
    }
  }

  process(_inputs, outputs) {
    const channels = outputs?.[0] || [];
    if (!channels.length) return true;

    for (let i = 0; i < channels[0].length; i++) {
      if (!this._started || this._available < this._format.channels) {
        for (const out of channels) out[i] = 0;
        this._started = false;
        continue;
      }
      for (let ch = 0; ch < channels.length; ch++) {
        channels[ch][i] = this._ring[(this._read + Math.min(ch, this._format.channels - 1)) % this._ring.length];
      }
      this._read = (this._read + this._format.channels) % this._ring.length;
      this._available -= this._format.channels;
    }

    this._framesOut++;
    if (currentTime - this._lastStats >= 1) {
      this._lastStats = currentTime;
      this.port.postMessage({
        type: "stats",
        buffered_frames: Math.floor(this._available / (this._contextFrameSamples * this._format.channels)),
        frames_in: this._framesIn,
        frames_out: this._framesOut,
        frames_drop: this._framesDrop,
      });
    }
    return true;
  }
}

registerProcessor("intercom-playback-processor", IntercomPlaybackProcessor);
