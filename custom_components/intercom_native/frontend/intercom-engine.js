const HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__";
const FRAME_BYTES = 1024;
const WS_AUDIO = 1;
const CALL_EVENT = "intercom_native.call_event";
const ASSET_V = "3";
const HIDDEN_HANGUP_GRACE_MS = 15000;
const CONTROL_ACK_TIMEOUT_MS = 3000;
const ENGINE_TRANSITIONS = {
  IDLE: ["CALLING", "RINGING", "STREAMING", "ERROR"],
  CALLING: ["RINGING", "STREAMING", "ERROR"],
  RINGING: ["STREAMING", "ERROR"],
  STREAMING: ["ERROR"],
  ERROR: ["CALLING", "RINGING", "STREAMING"],
};

class IntercomEngine extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._state = "IDLE";
    this._deviceId = "";
    this._audioMode = "full_duplex";
    this._mediaStream = null;
    this._audioContext = null;
    this._captureNode = null;
    this._source = null;
    this._playbackNode = null;
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
    this._busConnection = null;
    this._busUnsub = null;
    this._callSubscribers = new Set();
    this._lastEvents = new Map();
    this._hiddenTimer = null;
    this._controlWaiter = null;

    window.addEventListener("pagehide", () => this.close("pagehide"));
    document.addEventListener("visibilitychange", () => this._onVisibility());
  }

  configure(hass) {
    this._hass = hass;
    const conn = hass?.connection || null;
    if (!conn || conn === this._busConnection) return;
    if (this._busUnsub) {
      this._busUnsub();
      this._busUnsub = null;
    }
    this._busConnection = conn;
    conn.subscribeEvents((event) => this._onBusEvent(event), CALL_EVENT)
      .then((unsub) => { this._busUnsub = unsub; })
      .catch((err) => {
        this._busConnection = null;
        console.warn("intercom-engine: call_event subscription failed", err);
      });
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
    const target = String(state || "").toUpperCase();
    if (target === this._state) return;
    if (target !== "IDLE" && !(ENGINE_TRANSITIONS[this._state] || []).includes(target)) {
      console.warn(`intercom-engine: ignored transition ${this._state} -> ${target}`);
      return;
    }
    this._state = target;
    this._emit();
  }

  _forceIdle() {
    this._state = "IDLE";
    this._emit();
  }

  _onBusEvent(event) {
    const data = event?.data;
    if (!data) return;
    const scope = (data.scope || "").toLowerCase();
    for (const id of [data.device_id, data.source_device_id, data.dest_device_id, data.session_device_id]) {
      if (id) this._lastEvents.set(`${id}|${scope}`, event);
    }
    for (const cb of this._callSubscribers) {
      try { cb(event); } catch (err) { console.error("intercom-engine subscriber", err); }
    }
  }

  subscribeCallEvents(cb) {
    this._callSubscribers.add(cb);
    for (const event of this._lastEvents.values()) {
      try { cb(event); } catch (err) { console.error("intercom-engine replay", err); }
    }
    return () => this._callSubscribers.delete(cb);
  }

  _onVisibility() {
    if (document.visibilityState === "hidden") {
      if (this.active && this._hiddenTimer === null) {
        this._hiddenTimer = window.setTimeout(() => {
          this._hiddenTimer = null;
          if (document.visibilityState === "hidden" && this.active) {
            this.close("hidden_timeout");
          }
        }, HIDDEN_HANGUP_GRACE_MS);
      }
    } else if (this._hiddenTimer !== null) {
      window.clearTimeout(this._hiddenTimer);
      this._hiddenTimer = null;
    }
  }

  async _wsUrl(deviceId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/intercom_native/ws?device_id=${encodeURIComponent(deviceId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async _connect(deviceId) {
    if (this._ws && this._deviceId === deviceId && this._ws.readyState === WebSocket.OPEN) return;
    await this.close("switch");
    this._deviceId = deviceId;
    this._ws = new WebSocket(await this._wsUrl(deviceId));
    this._ws.binaryType = "arraybuffer";
    this._ws.onmessage = (event) => this._handleMessage(event);
    this._ws.onclose = () => this._cleanupAudio("ws_close");
    await new Promise((resolve, reject) => {
      this._ws.onopen = resolve;
      this._ws.onerror = () => reject(new Error("Audio WebSocket failed"));
    });
  }

  _sendControl(payload, waitForReply = false) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      return waitForReply ? Promise.resolve(null) : null;
    }
    if (!waitForReply) {
      this._ws.send(JSON.stringify(payload));
      return null;
    }
    if (this._controlWaiter) {
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    const promise = new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        if (this._controlWaiter?.resolve === resolve) this._controlWaiter = null;
        resolve(null);
      }, CONTROL_ACK_TIMEOUT_MS);
      this._controlWaiter = { resolve, timer };
    });
    this._ws.send(JSON.stringify(payload));
    return promise;
  }

  _resolveControlWaiter(msg) {
    if (!this._controlWaiter) return;
    const waiter = this._controlWaiter;
    this._controlWaiter = null;
    window.clearTimeout(waiter.timer);
    waiter.resolve(msg);
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
        this._resolveControlWaiter(msg);
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

  _createAudioContext() {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    try {
      return new Ctor({ sampleRate: 16000 });
    } catch (_) {
      return new Ctor();
    }
  }

  async _setupAudio(deviceInfo) {
    this._audioMode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const sendToEsp = this._audioMode === "full_duplex" || this._audioMode === "speaker_only";
    const receiveFromEsp = this._audioMode === "full_duplex" || this._audioMode === "mic_only";
    if (!sendToEsp && !receiveFromEsp) return;

    this._audioContext = this._createAudioContext();
    if (this._audioContext.state === "suspended") await this._audioContext.resume();

    if (sendToEsp) {
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      await this._audioContext.audioWorklet.addModule(`/intercom-native/intercom-processor.js?v=${ASSET_V}`);
      this._source = this._audioContext.createMediaStreamSource(this._mediaStream);
      this._captureNode = new AudioWorkletNode(this._audioContext, "intercom-processor");
      this._captureNode.port.onmessage = (event) => {
        if (event.data?.type === "audio") this._sendAudio(event.data.buffer);
      };
      this._source.connect(this._captureNode);
    }

    if (receiveFromEsp) {
      await this._audioContext.audioWorklet.addModule(`/intercom-native/intercom-playback-processor.js?v=${ASSET_V}`);
      this._playbackNode = new AudioWorkletNode(this._audioContext, "intercom-playback-processor");
      this._playbackNode.port.onmessage = (event) => {
        if (event.data?.type !== "stats") return;
        this._stats = { ...this._stats, ...event.data };
        this._emit();
      };
      this._playbackNode.connect(this._audioContext.destination);
    }
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v) ? v : "full_duplex";
  }

  async startP2P(deviceInfo) {
    await this._connect(deviceInfo.device_id);
    await this._setupAudio(deviceInfo);
    this._resetStats();
    this._setState("CALLING");
    this._sendControl({ type: "start", device_id: deviceInfo.device_id, host: deviceInfo.host });
  }

  async startHaSoftphone(target, softphoneInfo) {
    const info = { ...(softphoneInfo || {}), device_id: HA_SOFTPHONE_DEVICE_ID, audio_mode: target.audio_mode || "full_duplex" };
    await this._connect(HA_SOFTPHONE_DEVICE_ID);
    await this._setupAudio(info);
    this._resetStats();
    this._setState("CALLING");
    this._sendControl({ type: "ha_softphone_start", target_device_id: target.device_id });
  }

  async answer(deviceInfo, sessionDeviceId) {
    const deviceId = sessionDeviceId || deviceInfo.device_id;
    await this._connect(deviceId);
    await this._setupAudio({ ...(deviceInfo || {}), device_id: deviceId });
    this._resetStats();
    this._setState("STREAMING");
    this._sendControl({ type: "answer", device_id: deviceId, host: deviceInfo?.host || "" });
  }

  async answerEspCall(deviceInfo) {
    await this._connect(deviceInfo.device_id);
    await this._setupAudio(deviceInfo);
    this._resetStats();
    this._setState("STREAMING");
    this._sendControl({ type: "answer_esp_call", device_id: deviceInfo.device_id, host: deviceInfo.host });
  }

  async stop(deviceId = this._deviceId) {
    await this._sendControl({ type: "stop", device_id: deviceId }, true);
    await this.close("stop", { sendHangup: false });
  }

  _resetStats() {
    this._stats = { sent: 0, received: 0, buffered_frames: 0, frames_drop: 0 };
  }

  async close(_reason = "", options = {}) {
    const sendHangup = options.sendHangup !== false;
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      if (sendHangup) this._sendControl({ type: "hangup" });
      this._ws.close();
    }
    this._ws = null;
    await this._cleanupAudio("close");
  }

  async _cleanupAudio(_reason) {
    if (this._hiddenTimer !== null) {
      window.clearTimeout(this._hiddenTimer);
      this._hiddenTimer = null;
    }
    if (this._controlWaiter) {
      window.clearTimeout(this._controlWaiter.timer);
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    if (this._captureNode) { this._captureNode.disconnect(); this._captureNode = null; }
    if (this._source) { this._source.disconnect(); this._source = null; }
    if (this._mediaStream) { this._mediaStream.getTracks().forEach((t) => t.stop()); this._mediaStream = null; }
    if (this._playbackNode) { this._playbackNode.disconnect(); this._playbackNode = null; }
    if (this._audioContext) { await this._audioContext.close().catch(() => {}); this._audioContext = null; }
    this._forceIdle();
  }
}

export const intercomEngine = globalThis.__intercomNativeEngine ||
  (globalThis.__intercomNativeEngine = new IntercomEngine());
