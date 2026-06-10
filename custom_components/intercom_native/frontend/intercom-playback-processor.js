const FRAME_SAMPLES = 512;
const RING_FRAMES = 8;
const START_FRAMES = 2;
const DROP_FRAMES = 6;

class IntercomPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ring = new Int16Array(FRAME_SAMPLES * RING_FRAMES);
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
      if (data?.type === "audio" && data.buffer) {
        this._push(new Int16Array(data.buffer));
      }
    };
  }

  _push(frame) {
    if (frame.length !== FRAME_SAMPLES) return;
    if (this._available >= FRAME_SAMPLES * DROP_FRAMES) {
      this._read = (this._read + FRAME_SAMPLES) % this._ring.length;
      this._available -= FRAME_SAMPLES;
      this._framesDrop++;
    }
    for (let i = 0; i < FRAME_SAMPLES; i++) {
      this._ring[this._write] = frame[i];
      this._write = (this._write + 1) % this._ring.length;
    }
    this._available += FRAME_SAMPLES;
    this._framesIn++;
    if (!this._started && this._available >= FRAME_SAMPLES * START_FRAMES) {
      this._started = true;
    }
  }

  process(_inputs, outputs) {
    const out = outputs?.[0]?.[0];
    if (!out) return true;

    if (!this._started) {
      out.fill(0);
    } else {
      for (let i = 0; i < out.length; i++) {
        if (this._available <= 0) {
          out[i] = 0;
          this._started = false;
          continue;
        }
        out[i] = this._ring[this._read] / 32768;
        this._read = (this._read + 1) % this._ring.length;
        this._available--;
      }
    }

    this._framesOut++;
    if (currentTime - this._lastStats >= 1) {
      this._lastStats = currentTime;
      this.port.postMessage({
        type: "stats",
        buffered_frames: Math.floor(this._available / FRAME_SAMPLES),
        frames_in: this._framesIn,
        frames_out: this._framesOut,
        frames_drop: this._framesDrop,
      });
    }
    return true;
  }
}

registerProcessor("intercom-playback-processor", IntercomPlaybackProcessor);
