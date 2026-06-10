const HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__";
const FRAME_BYTES = 1024;
const WS_AUDIO = 1;

class IntercomEngine extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._state = "IDLE";
    this._deviceId = "";
    this._audioMode = "full_duplex";
    this._mediaStream = null;
    this._captureContext = null;
    this._captureNode = null;
    this._source = null;
    this._playbackContext = null;
    this._playbackNode = null;
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };

    window.addEventListener("pagehide", () => this.close("pagehide"));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this._sendControl({ type: "hangup" });
    });
  }

  configure(hass) {
    this._hass = hass;
  }

  get active() {
    return this._state !== "IDLE";
  }

  get deviceId() {
    return this._deviceId;
  }

  statsText() {
    if (!this.active) return "";
    return `Sent: ${this._stats.sent} | Recv: ${this._stats.received} | Buf: ${this._stats.buffered_frames}`;
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        state: this._state,
        device_id: this._deviceId,
        stats: { ...this._stats },
      },
    }));
  }

  _setState(state) {
    this._state = state;
    this._emit();
  }

  _wsUrl(deviceId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/intercom_native/ws?device_id=${encodeURIComponent(deviceId)}`;
    return `${proto}//${window.location.host}${path}`;
  }

  async _connect(deviceId) {
    if (this._ws && this._deviceId === deviceId && this._ws.readyState === WebSocket.OPEN) return;
    await this.close("switch");
    this._deviceId = deviceId;
    this._ws = new WebSocket(this._wsUrl(deviceId));
    this._ws.binaryType = "arraybuffer";
    this._ws.onmessage = (event) => this._handleMessage(event);
    this._ws.onclose = () => this._cleanupAudio("ws_close");
    await new Promise((resolve, reject) => {
      this._ws.onopen = resolve;
      this._ws.onerror = () => reject(new Error("Audio WebSocket failed"));
    });
  }

  _sendControl(payload) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    this._ws.send(JSON.stringify(payload));
  }

  _sendAudio(buffer) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength !== FRAME_BYTES) return;
    const frame = new Uint8Array(FRAME_BYTES + 1);
    frame[0] = WS_AUDIO;
    frame.set(bytes, 1);
    this._ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _handleMessage(event) {
    if (typeof event.data === "string") {
      try {
        const msg = JSON.parse(event.data);
        if (msg.state) this._setState(String(msg.state).toUpperCase());
        if (msg.error) this.dispatchEvent(new CustomEvent("error", { detail: msg.error }));
      } catch (_) {}
      return;
    }
    const raw = new Uint8Array(event.data);
    if (raw[0] !== WS_AUDIO || raw.byteLength !== FRAME_BYTES + 1 || !this._playbackNode) return;
    const payload = raw.slice(1).buffer;
    this._playbackNode.port.postMessage({ type: "audio", buffer: payload }, [payload]);
    this._stats.received++;
    if ((this._stats.received & 31) === 0) this._emit();
  }

  async _createAudioContext(sampleRate = 16000) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    try {
      return new Ctor({ sampleRate });
    } catch (_) {
      return new Ctor();
    }
  }

  async _setupAudio(deviceInfo) {
    this._audioMode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const sendToEsp = this._audioMode === "full_duplex" || this._audioMode === "speaker_only";
    const receiveFromEsp = this._audioMode === "full_duplex" || this._audioMode === "mic_only";

    if (sendToEsp) {
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this._captureContext = await this._createAudioContext(16000);
      if (this._captureContext.state === "suspended") await this._captureContext.resume();
      await this._captureContext.audioWorklet.addModule(`/intercom-native/intercom-processor.js?v=${Date.now()}`);
      this._source = this._captureContext.createMediaStreamSource(this._mediaStream);
      this._captureNode = new AudioWorkletNode(this._captureContext, "intercom-processor");
      this._captureNode.port.onmessage = (event) => {
        if (event.data?.type === "audio") this._sendAudio(event.data.buffer);
      };
      this._source.connect(this._captureNode);
    }

    if (receiveFromEsp) {
      this._playbackContext = await this._createAudioContext(16000);
      if (this._playbackContext.state === "suspended") await this._playbackContext.resume();
      await this._playbackContext.audioWorklet.addModule(`/intercom-native/intercom-playback-processor.js?v=${Date.now()}`);
      this._playbackNode = new AudioWorkletNode(this._playbackContext, "intercom-playback-processor");
      this._playbackNode.port.onmessage = (event) => {
        if (event.data?.type !== "stats") return;
        this._stats = { ...this._stats, ...event.data };
        this._emit();
      };
      this._playbackNode.connect(this._playbackContext.destination);
    }
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v) ? v : "full_duplex";
  }

  async startP2P(deviceInfo) {
    await this._connect(deviceInfo.device_id);
    await this._setupAudio(deviceInfo);
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
    this._setState("CALLING");
    this._sendControl({ type: "start", device_id: deviceInfo.device_id, host: deviceInfo.host });
  }

  async startHaSoftphone(target, softphoneInfo) {
    const info = { ...(softphoneInfo || {}), device_id: HA_SOFTPHONE_DEVICE_ID, audio_mode: target.audio_mode || "full_duplex" };
    await this._connect(HA_SOFTPHONE_DEVICE_ID);
    await this._setupAudio(info);
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
    this._setState("CALLING");
    this._sendControl({ type: "ha_softphone_start", target_device_id: target.device_id });
  }

  async answer(deviceInfo, sessionDeviceId) {
    const deviceId = sessionDeviceId || deviceInfo.device_id;
    await this._connect(deviceId);
    await this._setupAudio({ ...(deviceInfo || {}), device_id: deviceId });
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
    this._setState("STREAMING");
    this._sendControl({ type: "answer", device_id: deviceId, host: deviceInfo?.host || "" });
  }

  async answerEspCall(deviceInfo) {
    await this._connect(deviceInfo.device_id);
    await this._setupAudio(deviceInfo);
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
    this._setState("STREAMING");
    this._sendControl({ type: "answer_esp_call", device_id: deviceInfo.device_id, host: deviceInfo.host });
  }

  async stop(deviceId = this._deviceId) {
    this._sendControl({ type: "stop", device_id: deviceId });
    await this.close("stop");
  }

  async close(_reason = "") {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._sendControl({ type: "hangup" });
      this._ws.close();
    }
    this._ws = null;
    await this._cleanupAudio("close");
  }

  async _cleanupAudio(_reason) {
    if (this._captureNode) { this._captureNode.disconnect(); this._captureNode = null; }
    if (this._source) { this._source.disconnect(); this._source = null; }
    if (this._mediaStream) { this._mediaStream.getTracks().forEach((t) => t.stop()); this._mediaStream = null; }
    if (this._captureContext) { await this._captureContext.close().catch(() => {}); this._captureContext = null; }
    if (this._playbackNode) { this._playbackNode.disconnect(); this._playbackNode = null; }
    if (this._playbackContext) { await this._playbackContext.close().catch(() => {}); this._playbackContext = null; }
    this._setState("IDLE");
  }
}

export const intercomEngine = globalThis.__intercomNativeEngine ||
  (globalThis.__intercomNativeEngine = new IntercomEngine());
