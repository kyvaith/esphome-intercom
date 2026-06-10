const DEFAULT_FORMAT = Object.freeze({ sampleRate: 16000, pcmFormat: "s16le", channels: 1, frameMs: 32 });

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

class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._format = normaliseFormat(options?.processorOptions?.format);
    this._frameBytes = this._format.frameSamples * this._format.channels * this._format.bytesPerSample;
    this._buffer = new ArrayBuffer(this._frameBytes);
    this._view = new DataView(this._buffer);
    this._writeSample = 0;
    this._position = 0;
    this._lastSample = 0;
  }

  _encode(sample, sampleIndex) {
    const s = Math.max(-1, Math.min(1, sample));
    const offset = sampleIndex * this._format.bytesPerSample;
    if (this._format.pcmFormat === "s16le") {
      this._view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    } else if (this._format.pcmFormat === "s24le") {
      const v = Math.trunc(s < 0 ? s * 0x800000 : s * 0x7fffff);
      this._view.setUint8(offset, v & 0xff);
      this._view.setUint8(offset + 1, (v >> 8) & 0xff);
      this._view.setUint8(offset + 2, (v >> 16) & 0xff);
    } else if (this._format.pcmFormat === "s24le_in_s32") {
      this._view.setInt32(offset, Math.trunc(s < 0 ? s * 0x80000000 : s * 0x7fffff00), true);
    } else {
      this._view.setInt32(offset, Math.trunc(s < 0 ? s * 0x80000000 : s * 0x7fffffff), true);
    }
  }

  _writeMono(sample) {
    for (let ch = 0; ch < this._format.channels; ch++) {
      this._encode(sample, this._writeSample * this._format.channels + ch);
    }
    this._writeSample++;
    if (this._writeSample !== this._format.frameSamples) return;

    const frame = this._buffer;
    this.port.postMessage({ type: "audio", buffer: frame }, [frame]);
    this._buffer = new ArrayBuffer(this._frameBytes);
    this._view = new DataView(this._buffer);
    this._writeSample = 0;
  }

  process(inputList) {
    const input = inputList?.[0]?.[0];
    if (!input?.length) return true;

    const ratio = sampleRate / this._format.sampleRate;
    if (ratio === 1) {
      for (let i = 0; i < input.length; i++) this._writeMono(input[i]);
    } else {
      while (this._position < input.length) {
        const idx = Math.floor(this._position);
        const frac = this._position - idx;
        const a = idx > 0 ? input[idx - 1] : this._lastSample;
        const b = input[idx] ?? a;
        this._writeMono(a + (b - a) * frac);
        this._position += ratio;
      }
      this._position -= input.length;
      this._lastSample = input[input.length - 1] || this._lastSample;
    }
    return true;
  }
}

registerProcessor("intercom-processor", RecorderProcessor);
