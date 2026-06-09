/**
 * Intercom Card v2.0.0 - Pure mirror of ESP state
 *
 * The card is a simple frontend that mirrors the ESP's intercom_state entity.
 * No complex internal state tracking - just read ESP state and render UI.
 *
 * ESP States -> Card UI:
 * - Idle       -> Show destination + Call button
 * - Calling    -> Show "Calling [dest]..." + Hangup
 * - Ringing    -> Show "Incoming [caller]" + Answer/Decline
 * - Streaming  -> Show "In Call [peer]" + Hangup
 */

const INTERCOM_CARD_VERSION = (() => {
  try {
    const raw = new URL(import.meta.url).searchParams.get("v") || "";
    return raw.split("-")[0] || "dev";
  } catch (_) {
    return "dev";
  }
})();
const HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__";

// Lazy gate for verbose logs. Errors and warnings always emit.
// Enable in the browser console with localStorage.intercom_debug = "1".
const _ic_dbg = (() => {
  try { return localStorage.getItem("intercom_debug") === "1"; }
  catch (_) { return false; }
})();
const _ic_log = {
  error: console.error.bind(console),
  warn: console.warn.bind(console),
  info: _ic_dbg ? console.info.bind(console) : () => {},
  debug: _ic_dbg ? console.debug.bind(console) : () => {},
};

class IntercomCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    // UI transition states only
    this._starting = false;
    this._stopping = false;
    this._sessionState = null;
    this._sessionCaller = "";
    this._activeSessionDeviceId = null;
    this._softphoneDnd = false;
    this._softphoneTargetDeviceId = null;
    this._softphoneStateLoaded = false;
    this._softphoneStateLoading = false;

    // Browser audio path (active when the card itself is the call origin).
    this._audioContext = null;
    this._mediaStream = null;
    this._workletNode = null;
    this._source = null;
    this._playbackContext = null;
    this._gainNode = null;
    this._nextPlayTime = 0;
    this._unsubscribeAudio = null;
    this._chunksSent = 0;
    this._chunksReceived = 0;
    this._cleanupTask = null;

    // Device info
    this._activeDeviceInfo = null;
    this._availableDevices = [];
    this._availableDevicesLoading = false;
    this._availableDevicesRetryTimer = null;
    this._callMode = null;  // 'softphone' | 'bridge' | null

    // Entity IDs (discovered once)
    this._intercomStateEntityId = null;
    this._transportEntityId = null;
    this._callerEntityId = null;
    this._destinationEntityId = null;
    this._lastReasonEntityId = null;
    this._previousButtonEntityId = null;
    this._nextButtonEntityId = null;
    this._callButtonEntityId = null;
    this._declineButtonEntityId = null;

    // Audio streaming active (for P2P)
    this._audioStreaming = false;

    // Persistent error message (survives _render() DOM rebuild)
    this._errorMsg = "";

    // Auto-answer
    this._autoAnswer = false;
    this._autoAnswering = false;  // Prevents re-entry during auto-answer
    this._deepLinkAnswerConsumed = false;

    // Remote-end progress / ended-call surface comes from the unified HA
    // `intercom_native.call_event` bus event. We mirror it here so _render()
    // can switch the outgoing label to "X is ringing..."
    // and pop a brief "Call ended. Reason: ..." panel without polling.
    this._destRinging = false;
    this._lastEndInfo = null;          // {peer, reason, until_ms} | null
    this._lastEndClearTimer = null;
    this._unsubCallEvents = null;

    // Static skeleton: built once per mode, then mutated via textContent/
    // hidden/className. Eliminates innerHTML interpolation of untrusted
    // strings (peer, destination, caller, decline reason).
    this._els = null;
    this._skeletonMode = null;  // 'main' | 'unconfigured' | null
  }

  connectedCallback() {
    if (this._hass) this._subscribeBusEvents();
  }

  disconnectedCallback() {
    if (this._unsubCallEvents) {
      this._unsubCallEvents();
      this._unsubCallEvents = null;
    }
    if (this._lastEndClearTimer) {
      clearTimeout(this._lastEndClearTimer);
      this._lastEndClearTimer = null;
    }
    if (this._availableDevicesRetryTimer) {
      clearTimeout(this._availableDevicesRetryTimer);
      this._availableDevicesRetryTimer = null;
    }
    this._cleanup();
  }

  async _subscribeBusEvents() {
    if (!this._hass?.connection || this._unsubCallEvents) return;
    try {
      this._unsubCallEvents = await this._hass.connection.subscribeEvents(
        (e) => this._onCallEvent(e),
        "intercom_native.call_event",
      );
    } catch (err) {
      console.warn("intercom-card: failed to subscribe intercom_native.call_event", err);
    }
  }

  _eventConcernsThisCard(payload) {
    const myId = this._activeDeviceInfo?.device_id || this._getConfigDeviceId();
    if (!myId || !payload) return false;
    if (this._isHaSoftphoneMode()) {
      return payload.device_id === HA_SOFTPHONE_DEVICE_ID
          || payload.session_device_id === this._activeSessionDeviceId
          || payload.device_id === this._activeSessionDeviceId;
    }
    return payload.source_device_id === myId
        || payload.dest_device_id === myId
        || payload.device_id === myId;
  }

  _onCallEvent(event) {
    const scope = (event?.data?.scope || "").toLowerCase();
    if (scope === "bridge") {
      this._onBridgeStateEvent(event);
    } else if (scope === "session") {
      this._onSessionStateEvent(event);
    } else if (scope === "forward") {
      this._onForwardStateEvent(event);
    }
  }

  _onForwardStateEvent(event) {
    const data = event?.data;
    if (!this._eventConcernsThisCard(data)) return;
    const st = (data.state || "").toLowerCase();
    const peer = data.new_dest_name || data.old_dest_name || data.peer_name || "";
    if (st === "ringing") {
      this._destRinging = true;
    } else if (st === "connected") {
      this._destRinging = false;
      this._clearEndReason(false);
    } else if (st === "failed") {
      this._destRinging = false;
      this._captureEndReason("error", data.reason || "forward_failed", "remote", peer);
    }
    this._render();
  }

  _onBridgeStateEvent(event) {
    const data = event?.data;
    if (!this._eventConcernsThisCard(data)) return;
    const st = (data.state || "").toLowerCase();
    const origin = (data.origin || "").toLowerCase() || null;
    const reason = data.reason || "";
    const mirrorEspReason = this._usesEspReasonForTerminalDisplay();

    // Translate bridge-relative origin (source/dest) into card-relative
    // perspective (self/remote): the same disconnected event must read
    // as "Local hangup" on the leg that hung up and "Remote hangup" on
    // the other leg.
    const myId = this._activeDeviceInfo?.device_id || this._getConfigDeviceId();
    let perspective = null;
    if (origin === "source")      perspective = (data.source_device_id === myId) ? "self" : "remote";
    else if (origin === "dest")   perspective = (data.dest_device_id   === myId) ? "self" : "remote";
    else if (origin === "self" || origin === "remote") perspective = origin;
    const peer = data.source_device_id === myId ? data.dest_name
      : data.dest_device_id === myId ? data.source_name
      : data.peer_name;

    if (st === "ringing") {
      this._destRinging = true;
    } else if (st === "connected" || st === "streaming") {
      this._destRinging = false;
      this._clearEndReason(false);
    }
    if (mirrorEspReason && (st === "declined" || st === "error" || st === "disconnected")) {
      this._destRinging = false;
      this._render();
      return;
    }
    if (st === "declined") {
      this._destRinging = false;
      this._captureEndReason("declined", reason, perspective || origin, peer);
    } else if (st === "error") {
      this._destRinging = false;
      this._captureEndReason("error", reason, perspective || origin, peer);
    } else if (st === "disconnected") {
      this._destRinging = false;
      // The bridge fires `disconnected` twice on a normal teardown:
      // once from `on_*_stop` with reason+origin set (the real cause),
      // then again from `_on_disconnected` after the transport closes
      // with neither field. Skip the second one so the card keeps the
      // explicit reason on the ended-screen.
      if (reason || origin || !this._lastEndInfo) {
        // Bridge labels are bridge-relative (the leg that produced the
        // signal). Flip them to card-relative: the same hangup must
        // read "Local" on the leg that hung up and "Remote" on the
        // other leg.
        let localized = reason;
        if (reason === "remote_device_lost") localized = "remote_device_lost";
        else if (perspective === "self" && reason === "remote_hangup") localized = "local_hangup";
        else if (perspective === "remote" && reason === "local_hangup") localized = "remote_hangup";
        this._captureEndReason("disconnected", localized, perspective || origin, peer);
      }
    }
    this._render();
  }

  _onSessionStateEvent(event) {
    const data = event?.data;
    if (!this._eventConcernsThisCard(data)) return;
    const st = (data.state || "").toLowerCase();
    const terminalState = this._isTerminalSessionState(st);
    const reason = data.reason || "";
    const origin = (data.origin || "").toLowerCase() || null;
    const peer = data.peer_name || "";
    const mirrorEspReason = this._usesEspReasonForTerminalDisplay();
    if (this._isConfiguredSoftphone()) {
      if (data.session_device_id || (data.device_id && data.device_id !== HA_SOFTPHONE_DEVICE_ID)) {
        this._activeSessionDeviceId = data.session_device_id || data.device_id;
      }
      const outgoingRinging =
        this._isHaSoftphoneMode() &&
        st === "ringing" &&
        !data.caller &&
        (data.device_id === HA_SOFTPHONE_DEVICE_ID || this._activeSessionDeviceId === HA_SOFTPHONE_DEVICE_ID);
      this._sessionState = outgoingRinging
        ? "outgoing"
        : (st === "disconnected" ? "idle" : (st || "idle"));
      if (outgoingRinging) this._destRinging = true;
      if (Object.prototype.hasOwnProperty.call(data, "dnd")) this._softphoneDnd = !!data.dnd;
      if (data.caller || data.peer_name) this._sessionCaller = data.caller || data.peer_name;
      if (terminalState) {
        this._sessionCaller = "";
        this._activeSessionDeviceId = null;
      }
      if (
        this._isHaSoftphoneMode() &&
        st === "ringing" &&
        this._autoAnswer &&
        !this._autoAnswering &&
        !this._starting
      ) {
        this._autoAnswering = true;
        this._tryAutoAnswer();
      }
    }
    if (st === "streaming" || st === "ringing") {
      this._clearEndReason(false);
      if (st === "streaming") this._destRinging = false;
    } else if (mirrorEspReason && terminalState) {
      // ESP-to-ESP card mode mirrors the ESP text sensors. Bridge/session
      // events are useful for HA softphone state, but the terminal reason
      // shown here must come from the ESP's own last_reason sensor.
      this._destRinging = false;
    } else if ((st === "idle" || st === "disconnected") && reason) {
      const endOrigin = (origin === "self" || origin === "remote")
        ? origin
        : (reason === "local_hangup" ? "self" : "remote");
      const isDisconnectReason =
        reason === "local_hangup" ||
        reason === "remote_hangup" ||
        reason === "remote_device_lost";
      this._captureEndReason(
        isDisconnectReason ? "disconnected" : "declined",
        reason,
        endOrigin,
        peer,
      );
    } else if (st === "declined") {
      this._captureEndReason("declined", reason, origin || "remote", peer);
    } else if (st === "error") {
      this._captureEndReason("error", String(data.code ?? ""), origin || "remote", peer);
    }
    if (terminalState && this._isSoftphoneContext()) {
      this._cleanupAfterTerminalSession();
    }
    this._render();
  }

  _isTerminalSessionState(state) {
    return state === "idle" ||
           state === "disconnected" ||
           state === "declined" ||
           state === "error";
  }

  _hasBrowserAudioPath() {
    return !!(
      this._audioStreaming ||
      this._mediaStream ||
      this._workletNode ||
      this._source ||
      this._audioContext ||
      this._playbackContext ||
      this._unsubscribeAudio
    );
  }

  _cleanupAfterTerminalSession() {
    this._autoAnswering = false;
    this._starting = false;
    this._stopping = false;
    if (!this._hasBrowserAudioPath() || this._cleanupTask) return;
    this._cleanupTask = this._cleanup()
      .catch((err) => console.warn("intercom-card: softphone cleanup failed", err))
      .finally(() => {
        this._cleanupTask = null;
        this._render();
      });
  }

  _captureEndReason(kind, reason, origin, peerOverride = "") {
    const peer = peerOverride || this._getCallerName() || this._getDestination() || "";
    this._lastEndInfo = { kind, reason, origin, peer, until_ms: Date.now() + 5000 };
    if (this._lastEndClearTimer) clearTimeout(this._lastEndClearTimer);
    this._lastEndClearTimer = setTimeout(() => {
      this._lastEndInfo = null;
      this._lastEndClearTimer = null;
      this._render();
    }, 5000);
  }

  _clearEndReason(doRender = true) {
    if (this._lastEndClearTimer) {
      clearTimeout(this._lastEndClearTimer);
      this._lastEndClearTimer = null;
    }
    this._lastEndInfo = null;
    if (doRender) this._render();
  }

  _formatEndReason(info) {
    if (!info) return "";
    const { kind, reason, origin } = info;
    const knownReason = this._formatKnownReason(reason);
    if (knownReason) return knownReason;
    // origin can be "self"/"remote" (perspective from this card's
    // device, set by _onBridgeStateEvent) or the raw bridge-relative
    // "source"/"dest" when this card is HA itself (no device match).
    const isSelf = origin === "self";
    const who = isSelf ? null
      : origin === "remote" ? "Remote"
      : origin === "source" ? "Caller"
      : origin === "dest"   ? "Callee"
      : null;

    if (kind === "disconnected") {
      if (reason === "local_hangup")  return "Local hangup";
      if (reason === "remote_hangup") return who ? `${who} hung up` : "Remote hangup";
      if (reason === "remote_device_lost") return who ? `${who} lost` : "Remote device lost";
      return reason || "Disconnected";
    }
    if (kind === "declined") {
      if (isSelf) return reason ? `Local decline: "${reason}"` : "Local decline";
      const head = who ? `${who} declined` : "Declined";
      return reason ? `${head}: "${reason}"` : head;
    }
    if (kind === "error") {
      const numericCode = reason && /^[0-9]+$/.test(String(reason));
      if (isSelf) {
        if (!reason) return "Local error";
        return numericCode ? `Local error (code ${reason})` : `Local error: "${reason}"`;
      }
      const head = who ? `${who} error` : "Error";
      if (!reason) return head;
      return numericCode ? `${head} (code ${reason})` : `${head}: "${reason}"`;
    }
    return reason || kind;
  }

  _reasonKey(reason) {
    const text = String(reason || "").trim();
    if (!text) return "";
    if (text === "DND") return "DND";
    const normalized = text.toLowerCase().replace(/[\s-]+/g, "_");
    const known = new Set([
      "local_hangup",
      "remote_hangup",
      "remote_device_lost",
      "declined",
      "timeout",
      "busy",
      "unreachable",
      "protocol_error",
      "bridge_error",
    ]);
    return known.has(normalized) ? normalized : "";
  }

  _formatKnownReason(reason) {
    switch (this._reasonKey(reason)) {
      case "local_hangup": return "Local hangup";
      case "remote_hangup": return "Remote hangup";
      case "remote_device_lost": return "Remote device lost";
      case "declined": return "Declined";
      case "timeout": return "Timeout";
      case "busy": return "Busy";
      case "unreachable": return "Unreachable";
      case "protocol_error": return "Protocol error";
      case "bridge_error": return "Bridge error";
      case "DND": return "DND";
      default: return "";
    }
  }

  setConfig(config) {
    this.config = config;
    this._softphoneTargetDeviceId =
      this._loadSoftphoneTargetPreference() ||
      this._softphoneTargetDeviceId;
    // Load auto-answer preference from localStorage
    const deviceId = this._autoAnswerStorageId();
    if (deviceId) {
      this._autoAnswer = localStorage.getItem(`intercom_auto_answer_${deviceId}`) === "true";
    }
    this._render();
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;

    // Devices populate the destination cycler.
    if (hass && this._availableDevices.length === 0) {
      this._loadAvailableDevices();
    }
    if (hass && this._isHaSoftphoneMode() && !this._softphoneStateLoaded) {
      this._loadSoftphoneState();
    }

    // Discover entity IDs once
    if (hass && !this._intercomStateEntityId) {
      this._findEntityIds();
    }

    // Subscribe to HA bus events once we have a hass.connection
    if (hass && !this._unsubCallEvents && hass.connection) {
      this._subscribeBusEvents();
    }

    // Re-render when ESP state or destination changes
    if (hass) {
      let needsRender = false;
      let newEspState = null;
      let espStateChanged = false;
      let lastReasonChanged = false;

      // Check intercom_state
      if (this._intercomStateEntityId) {
        const stateEntity = hass.states[this._intercomStateEntityId];
        const oldStateEntity = oldHass?.states?.[this._intercomStateEntityId];
        newEspState = stateEntity?.state?.toLowerCase();
        if (stateEntity?.state !== oldStateEntity?.state) {
          needsRender = true;
          espStateChanged = true;
        }
      }

      // Check destination (drives contact-cycler label).
      if (this._destinationEntityId) {
        const destEntity = hass.states[this._destinationEntityId];
        const oldDestEntity = oldHass?.states?.[this._destinationEntityId];
        if (destEntity?.state !== oldDestEntity?.state) {
          needsRender = true;
        }
      }

      if (this._transportEntityId) {
        const transportEntity = hass.states[this._transportEntityId];
        const oldTransportEntity = oldHass?.states?.[this._transportEntityId];
        if (transportEntity?.state !== oldTransportEntity?.state) {
          needsRender = true;
        }
      }

      if (this.config?.show_extended_info) {
        for (const device of this._availableDevices) {
          const transportEntityId = device?.entities?.intercom_transport;
          if (!transportEntityId) continue;
          if (hass.states[transportEntityId]?.state !== oldHass?.states?.[transportEntityId]?.state) {
            needsRender = true;
            break;
          }
        }
      }

      // Check caller (for incoming call info)
      if (this._callerEntityId) {
        const callerEntity = hass.states[this._callerEntityId];
        const oldCallerEntity = oldHass?.states?.[this._callerEntityId];
        if (callerEntity?.state !== oldCallerEntity?.state) {
          needsRender = true;
        }
      }

      // Check terminal reason. For direct ESP-to-ESP calls HA is only
      // mirroring the source ESP, so the reason comes from the ESP's
      // intercom_api last-reason entity, not from a HA bridge event.
      if (this._lastReasonEntityId) {
        const reasonEntity = hass.states[this._lastReasonEntityId];
        const oldReasonEntity = oldHass?.states?.[this._lastReasonEntityId];
        if (reasonEntity?.state !== oldReasonEntity?.state) {
          needsRender = true;
          lastReasonChanged = true;
        }
      }

      // CRITICAL: Cleanup audio when ESP goes to Idle
      if (espStateChanged && newEspState === "idle") {
        if (this._audioStreaming && !this._starting) {
          this._cleanup();
        }
        this._errorMsg = "";
        this._autoAnswering = false;
        if (!this._lastEndInfo) this._captureMirroredLastReason();
      } else if (lastReasonChanged && this._getEspState().toLowerCase() === "idle") {
        if (!this._lastEndInfo) this._captureMirroredLastReason();
      }

      // Card auto_answer fires only when the ESP is dialling HA itself.
      // ESP-to-ESP bridged calls go through the callee's firmware
      // auto_answer instead.
      if (espStateChanged && this._autoAnswer && !this._autoAnswering && !this._starting) {
        const isCallingHa = (newEspState === "calling" || newEspState === "outgoing")
            && this._getDestination() === this._getHaName();
        if (isCallingHa) {
          this._autoAnswering = true;
          this._tryAutoAnswer();
        }
      }

      this._maybeAnswerFromUrl(newEspState);

      if (needsRender) {
        this._render();
      }
    }
  }

  _shouldAnswerFromUrl() {
    if (this._deepLinkAnswerConsumed) return false;
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = (params.get("intercom_answer") || "").toLowerCase();
      return value === "1" || value === "true" || value === "yes";
    } catch (_) {
      return false;
    }
  }

  _clearAnswerUrlParam() {
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("intercom_answer");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    } catch (_) {
      // Best effort only. Leaving the parameter is harmless because the local
      // consumed flag prevents repeated answers in this card instance.
    }
  }

  _maybeAnswerFromUrl(espState) {
    if (!this._shouldAnswerFromUrl()) return;
    if (this._autoAnswering || this._starting) return;
    const state = (espState || this._getEspState()).toLowerCase();
    const isCallingHa = (state === "calling" || state === "outgoing")
        && this._getDestination() === this._getHaName();
    if (!isCallingHa) return;

    this._deepLinkAnswerConsumed = true;
    this._clearAnswerUrlParam();
    this._autoAnswering = true;
    this._tryAutoAnswer({ requirePersistentPermission: false });
  }

  _getConfigDeviceId() {
    if (this._isHaSoftphoneMode()) return HA_SOFTPHONE_DEVICE_ID;
    return this.config?.entity_id || this.config?.device_id;
  }

  _isHaSoftphoneMode() {
    return (this.config?.mode || this.config?.card_mode || "hybrid") === "ha_softphone";
  }

  _autoAnswerStorageId() {
    return this._isHaSoftphoneMode()
      ? HA_SOFTPHONE_DEVICE_ID
      : (this.config?.entity_id || this.config?.device_id);
  }

  _softphoneTargetStorageKey() {
    return `intercom_softphone_target_${this.config?.name || "default"}`;
  }

  _loadSoftphoneTargetPreference() {
    try { return localStorage.getItem(this._softphoneTargetStorageKey()) || ""; }
    catch (_) { return ""; }
  }

  _saveSoftphoneTargetPreference(deviceId) {
    try {
      if (deviceId) localStorage.setItem(this._softphoneTargetStorageKey(), deviceId);
      else localStorage.removeItem(this._softphoneTargetStorageKey());
    } catch (_) {}
  }

  _sessionDeviceId() {
    return this._activeSessionDeviceId || this._activeDeviceInfo?.device_id || this._getConfigDeviceId();
  }

  // Get current ESP state from entity
  _getEspState() {
    if (this._isConfiguredSoftphone()) return this._sessionState || "idle";
    if (!this._hass || !this._intercomStateEntityId) return "unknown";
    const entity = this._hass.states[this._intercomStateEntityId];
    return entity?.state || "unknown";
  }

  _isConfiguredSoftphone() {
    if (this._isHaSoftphoneMode()) return true;
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return !!device?.softphone;
  }

  _isEspUnavailable() {
    if (!this._hass) return false;

    const configuredDevice = this._availableDevices.find(d => this._deviceMatchesConfig(d));
    const stateEntityId =
      this._intercomStateEntityId ||
      configuredDevice?.entities?.intercom_state;
    if (stateEntityId) {
      const state = (this._hass.states[stateEntityId]?.state || "").toLowerCase();
      return state === "unavailable";
    }

    const endpointEntityId = configuredDevice?.entities?.intercom_endpoint;
    if (endpointEntityId) {
      const state = (this._hass.states[endpointEntityId]?.state || "").toLowerCase();
      return state === "unavailable";
    }

    return false;
  }

  // Get caller name from entity
  _getCallerName() {
    if (this._isConfiguredSoftphone()) return this._sessionCaller || "";
    if (!this._hass || !this._callerEntityId) return "";
    const entity = this._hass.states[this._callerEntityId];
    const state = entity?.state;
    if (!state || state === "unknown" || state === "") return "";
    return state;
  }

  // The HA peer is identified by the instance friendly name (location_name).
  // The integration sensor prepends location_name as the first contact, and
  // intercom_api selects it by index, so the destination text shown by the
  // ESP equals location_name. Compare against this everywhere instead of the
  // hardcoded "Home Assistant" string literal.
  _getHaName() {
    return this._hass?.config?.location_name || "intercom-native";
  }

  // Get destination from entity
  _getDestination() {
    if (this._isHaSoftphoneMode()) {
      return this._getSoftphoneTargetDevice()?.name || "No endpoint";
    }
    if (!this._hass || !this._destinationEntityId) return this._getHaName();
    const entity = this._hass.states[this._destinationEntityId];
    return entity?.state || this._getHaName();
  }

  _softphoneTargets() {
    return this._availableDevices.filter(d => d && !d.softphone && d.device_id);
  }

  _getSoftphoneTargetDevice() {
    const targets = this._softphoneTargets();
    if (targets.length === 0) return null;
    const wanted = this._softphoneTargetDeviceId;
    return targets.find(d => d.device_id === wanted) || targets[0];
  }

  _normaliseTransport(value) {
    const v = String(value || "").trim().toLowerCase();
    return (v === "tcp" || v === "udp") ? v.toUpperCase() : "";
  }

  _transportFromEntity(entityId) {
    if (!this._hass || !entityId) return "";
    return this._normaliseTransport(this._hass.states[entityId]?.state);
  }

  _deviceMatchesConfig(device) {
    const deviceId = this._getConfigDeviceId();
    return !!device && !!deviceId && (
      device.device_id === deviceId ||
      device.esphome_id === deviceId ||
      device.name === deviceId ||
      device.name?.toLowerCase().replace(/\s+/g, "-") === deviceId
    );
  }

  _findDeviceByName(name) {
    const wanted = (name || "").trim();
    if (!wanted) return null;
    return this._availableDevices.find(d => (d.name || "").trim() === wanted) || null;
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v)
      ? v
      : "full_duplex";
  }

  _audioModeLabel(mode) {
    switch (this._normaliseAudioMode(mode)) {
      case "mic_only": return "MIC";
      case "speaker_only": return "SPK";
      case "control_only": return "CTRL";
      default: return "FULL";
    }
  }

  _getOwnTransport() {
    const direct = this._transportFromEntity(this._transportEntityId);
    if (direct) return direct;
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return this._transportFromEntity(device?.entities?.intercom_transport) ||
           this._normaliseTransport(device?.transport);
  }

  _getDestinationTransport(destination) {
    const device = this._findDeviceByName(destination);
    return this._transportFromEntity(device?.entities?.intercom_transport) ||
           this._normaliseTransport(device?.transport);
  }

  _getOwnAudioMode() {
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return this._normaliseAudioMode(device?.audio_mode);
  }

  _getDestinationAudioMode(destination) {
    if (this._isHaName(destination)) return "full_duplex";
    const device = this._findDeviceByName(destination);
    return this._normaliseAudioMode(device?.audio_mode);
  }

  _formatHeaderTitle(baseName) {
    const name = baseName || "Intercom";
    if (!this.config?.show_extended_info) return name;
    const transport = this._getOwnTransport();
    const mode = this._audioModeLabel(this._getOwnAudioMode());
    return transport ? `${name} - ${transport}/${mode}` : `${name} - ${mode}`;
  }

  _formatModeLabel(destination) {
    if (this._isHaSoftphoneMode()) {
      if (!this.config?.show_extended_info) return "HA - ESP";
      const target = this._getSoftphoneTargetDevice();
      const destTransport = this._transportFromEntity(target?.entities?.intercom_transport) ||
                            this._normaliseTransport(target?.transport);
      const destMode = this._audioModeLabel(this._normaliseAudioMode(target?.audio_mode));
      return destTransport ? `HA - ESP ${destTransport} ${destMode}` : `HA - ESP ${destMode}`;
    }
    if (this._isHaName(destination)) return "Home Assistant - ESP";
    if (!this.config?.show_extended_info) return "ESP - ESP";

    const sourceTransport = this._getOwnTransport();
    const destTransport = this._getDestinationTransport(destination);
    const sourceMode = this._audioModeLabel(this._getOwnAudioMode());
    const destMode = this._audioModeLabel(this._getDestinationAudioMode(destination));
    if (sourceTransport && destTransport && sourceTransport !== destTransport) {
      return `Inter-protocol ${sourceTransport}/${sourceMode}-${destTransport}/${destMode}`;
    }
    if (sourceTransport && destTransport) return `ESP - ESP ${sourceTransport} ${sourceMode}-${destMode}`;
    return "ESP - ESP";
  }

  _isHaName(name) {
    return (name || "").trim() === this._getHaName();
  }

  _isSoftphoneContext() {
    if (this._isHaSoftphoneMode()) return true;
    if (this._isConfiguredSoftphone()) return true;
    if (this._callMode === "softphone") return true;
    if (this._callMode === "mirror") return false;

    const state = this._getEspState().toLowerCase();
    if (state === "ringing" || state === "incoming" ||
        state === "streaming" || state === "answering") {
      const caller = this._getCallerName();
      if (caller) return this._isHaName(caller);
    }

    return this._isHaName(this._getDestination());
  }

  _usesEspReasonForTerminalDisplay() {
    return !this._isSoftphoneContext();
  }

  async _pressEspButton(entityId, label) {
    if (!entityId) throw new Error(`${label} button not available`);
    await this._hass.callService("button", "press", { entity_id: entityId });
  }

  _getLastReason() {
    if (!this._hass || !this._lastReasonEntityId) return "";
    const entity = this._hass.states[this._lastReasonEntityId];
    const value = entity?.state || "";
    return value === "unknown" || value === "unavailable" ? "" : value;
  }

  _captureMirroredLastReason() {
    const reason = this._getLastReason();
    if (!reason) return;
    const reasonKey = this._reasonKey(reason);
    const isHangup =
      reasonKey === "local_hangup" ||
      reasonKey === "remote_hangup" ||
      reasonKey === "remote_device_lost";
    // Mirror mode shows the ESP terminal reason as-is. If the card is a
    // HA/browser softphone, terminal direction comes from call_event instead.
    this._captureEndReason(
      isHangup ? "disconnected" : "declined",
      reason,
      reasonKey === "local_hangup" ? "self" : "remote",
    );
  }

  async _findEntityIds() {
    if (!this._hass) return;
    if (this._isHaSoftphoneMode()) return;

    const deviceInfo = await this._getDeviceInfo();
    const configDeviceId = this._getConfigDeviceId();
    const targetDeviceId = deviceInfo?.device_id || configDeviceId;
    if (!targetDeviceId) return;

    // Use entities mapping from backend
    if (deviceInfo?.entities && typeof deviceInfo.entities === "object") {
      const e = deviceInfo.entities;
      this._intercomStateEntityId = e.intercom_state || null;
      this._transportEntityId = e.intercom_transport || null;
      this._callerEntityId = e.incoming_caller || null;
      this._destinationEntityId = e.destination || null;
      this._lastReasonEntityId = e.last_reason || null;
      this._previousButtonEntityId = e.previous || null;
      this._nextButtonEntityId = e.next || null;
      this._callButtonEntityId = e.call || null;
      this._declineButtonEntityId = e.decline || null;
      this._render();
      return;
    }

    // Fallback: entity registry
    try {
      const registry = await this._hass.connection.sendMessagePromise({
        type: "config/entity_registry/list",
      });
      if (!registry) return;

      for (const entity of registry) {
        if (entity.device_id !== targetDeviceId) continue;
        const id = entity.entity_id;
        if (id.includes("intercom_state")) this._intercomStateEntityId = id;
        else if (id.includes("intercom_transport")) this._transportEntityId = id;
        else if (id.includes("caller")) this._callerEntityId = id;
        else if (id.includes("destination")) this._destinationEntityId = id;
        else if (id.includes("intercom_last_reason") || id.includes("last_reason") || id.includes("end_reason")) this._lastReasonEntityId = id;
        else if (id.startsWith("button.") && id.includes("previous")) this._previousButtonEntityId = id;
        else if (id.startsWith("button.") && id.includes("next")) this._nextButtonEntityId = id;
        else if (id.startsWith("button.") && id.includes("call") && !id.includes("decline")) this._callButtonEntityId = id;
        else if (id.startsWith("button.") && id.includes("decline")) this._declineButtonEntityId = id;
      }
      this._render();
    } catch (err) {
      console.error("Entity discovery failed:", err);
    }
  }

  async _loadAvailableDevices() {
    if (!this._hass || this._availableDevicesLoading) return;
    if (!this._isIntercomNativeLoaded()) {
      this._scheduleAvailableDevicesLoad();
      return;
    }
    this._availableDevicesLoading = true;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/list_devices",
      });
      if (result?.devices) {
        this._availableDevices = result.devices;
        if (this._isHaSoftphoneMode() && !this._softphoneTargetDeviceId) {
          this._softphoneTargetDeviceId = this._softphoneTargets()[0]?.device_id || null;
        }
        this._render();
      }
    } catch (err) {
      if (this._isUnknownCommandError(err)) this._scheduleAvailableDevicesLoad();
      else console.error("Failed to load devices:", err);
    } finally {
      this._availableDevicesLoading = false;
    }
  }

  _isIntercomNativeLoaded() {
    const components = this._hass?.config?.components;
    return !Array.isArray(components) || components.includes("intercom_native");
  }

  _isUnknownCommandError(err) {
    const code = String(err?.code || err?.error || "");
    const message = String(err?.message || "");
    return code.includes("unknown_command") || message.includes("unknown command");
  }

  _scheduleAvailableDevicesLoad() {
    if (this._availableDevicesRetryTimer) return;
    this._availableDevicesRetryTimer = setTimeout(() => {
      this._availableDevicesRetryTimer = null;
      this._loadAvailableDevices();
    }, 2000);
  }

  _render() {
    const customName = this.config?.name || "";
    const name = customName || "Intercom";
    const deviceId = this._getConfigDeviceId();

    if (!deviceId) {
      this._renderUnconfigured(name);
      return;
    }

    if (this._skeletonMode !== "main") {
      this._buildSkeletonMain();
      this._skeletonMode = "main";
    }
    const els = this._els;

    const espState = this._getEspState();
    const destination = this._getDestination();
    const caller = this._getCallerName();

    let statusText = "";
    let statusReason = "";
    let statusClass = "disconnected";
    let showAnswer = false;
    let showHangup = false;
    let showCall = false;
    const buttonDisabled = this._starting || this._stopping;

    let espDeviceName = this._activeDeviceInfo?.name;
    if (!espDeviceName && deviceId) {
      const device = this._availableDevices.find(d =>
        this._deviceMatchesConfig(d)
      );
      espDeviceName = device?.name;
    }
    const displayName = customName || espDeviceName || name;
    espDeviceName = espDeviceName || displayName;

    if (!this._isHaSoftphoneMode() && this._isEspUnavailable()) {
      els.headerName.textContent = this._formatHeaderTitle(displayName);
      els.destRow.hidden = true;
      els.offlinePanel.hidden = false;
      els.answerBtn.hidden = true;
      els.declineBtn.hidden = true;
      els.hangupBtn.hidden = true;
      els.callBtn.hidden = true;
      els.placeholderBtn.hidden = true;
      els.autoAnswerRow.hidden = true;
      els.statusIndicator.className = "status-indicator unavailable";
      els.statusText.textContent = "ESP unavailable";
      els.statusReason.textContent = "Device is offline";
      els.statusReason.hidden = false;
      els.stats.textContent = "";
      els.err.textContent = "";
      return;
    }
    els.offlinePanel.hidden = true;

    switch (espState.toLowerCase()) {
      case "idle":
        if (this._lastEndInfo) {
          const reasonLabel = this._formatEndReason(this._lastEndInfo);
          const peerLabel = this._lastEndInfo.peer ? ` with ${this._lastEndInfo.peer}` : "";
          statusText = `Call${peerLabel} ended.`;
          statusReason = `Reason: ${reasonLabel}`;
          statusClass = "disconnected";
          showCall = true;
        } else if (this._isHaSoftphoneMode() && this._softphoneDnd) {
          statusText = "Do Not Disturb";
          statusReason = "Incoming calls to Home Assistant are declined.";
          statusClass = "disconnected";
          showCall = true;
        } else {
          statusText = "Ready";
          statusClass = "disconnected";
          showCall = true;
        }
        break;
      case "calling":
      case "outgoing":
        if (destination === this._getHaName()) {
          statusText = `Incoming: ${espDeviceName}`;
          statusClass = "ringing";
          showAnswer = true;
        } else {
          statusText = this._destRinging
            ? `${destination} is ringing...`
            : `Calling ${destination}...`;
          statusClass = this._destRinging ? "ringing" : "transitioning";
          showHangup = true;
        }
        break;
      case "ringing":
      case "incoming":
        statusText = `Incoming: ${caller || "Unknown"}`;
        statusClass = "ringing";
        showAnswer = true;
        break;
      case "streaming":
      case "answering":
        statusText = `In Call: ${caller || destination || "Active"}`;
        statusClass = "connected";
        showHangup = true;
        break;
      default:
        statusText = espState;
        statusClass = "disconnected";
        showCall = true;
    }

    if (this._starting) statusText = "Connecting...";
    if (this._stopping) statusText = "Ending call...";

    els.headerName.textContent = this._formatHeaderTitle(displayName);

    // Hybrid cards mirror one ESP endpoint. When that ESP selects HA, the
    // browser acts as the HA leg for that ESP; ha_softphone mode is the
    // independent HA endpoint with its own destination selector.
    els.destRow.hidden = !showCall;
    els.destValue.textContent = destination;
    if (els.destSelect) {
      els.destSelect.hidden = !this._isHaSoftphoneMode();
      els.destValueWrap.classList.toggle("selecting", this._isHaSoftphoneMode());
      this._renderSoftphoneDestinationSelect(els.destSelect);
    }
    const softphoneMode = this._isHaSoftphoneMode();
    els.prevBtn.disabled = buttonDisabled || softphoneMode;
    els.nextBtn.disabled = buttonDisabled || softphoneMode;
    els.prevBtn.hidden = softphoneMode;
    els.nextBtn.hidden = softphoneMode;
    els.prevBtn.style.display = softphoneMode ? "none" : "";
    els.nextBtn.style.display = softphoneMode ? "none" : "";

    // Action buttons: exactly one set visible at a time.
    els.answerBtn.hidden = !showAnswer;
    els.declineBtn.hidden = !showAnswer;
    els.hangupBtn.hidden = !showHangup;
    els.callBtn.hidden = !showCall;
    els.placeholderBtn.hidden = showAnswer || showHangup || showCall;
    els.answerBtn.disabled = buttonDisabled;
    els.declineBtn.disabled = buttonDisabled;
    els.hangupBtn.disabled = buttonDisabled;
    els.callBtn.disabled = buttonDisabled;

    // Status
    els.statusIndicator.className = "status-indicator " + statusClass;
    els.statusText.textContent = statusText;
    els.statusReason.textContent = statusReason;
    els.statusReason.hidden = !statusReason;

    // Auto-answer row (only when call button is the visible action)
    els.autoAnswerRow.hidden = !showCall;
    els.autoAnswerCheckbox.checked = !!this._autoAnswer;
    if (els.dndRow) {
      els.dndRow.hidden = !this._isHaSoftphoneMode();
      els.dndCheckbox.checked = !!this._softphoneDnd;
    }

    // Stats line
    if (this._audioStreaming) {
      els.stats.textContent = `Sent: ${this._chunksSent} | Recv: ${this._chunksReceived}`;
    } else {
      els.stats.textContent = this._formatModeLabel(destination);
    }

    // Error
    els.err.textContent = this._errorMsg;
  }

  _renderUnconfigured(name) {
    if (this._skeletonMode !== "unconfigured") {
      this._buildSkeletonUnconfigured();
      this._skeletonMode = "unconfigured";
    }
    this._els.headerName.textContent = name;
  }

  // Static-skeleton builders. Construct DOM once via createElement +
  // textContent (no innerHTML interpolation of dynamic strings); _render
  // then mutates textContent / className / hidden / disabled. The inline
  // <style> block is the only innerHTML use and contains no untrusted
  // data, so it is XSS-safe.
  _buildSkeletonMain() {
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      :host {
        display: block;
        --intercom-card-surface: var(--ha-card-background, var(--card-background-color, white));
        --intercom-control-surface: transparent;
        --intercom-control-hover-surface: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
      }
      .card {
        background: var(--intercom-card-surface);
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
        padding: 16px;
      }
      .header { font-size: 1.2em; font-weight: 500; margin-bottom: 16px; color: var(--primary-text-color); }

      .destination-row {
        display: flex; align-items: center; justify-content: center;
        gap: 12px; margin-bottom: 16px;
      }
      .destination-row[hidden] { display: none; }
      .nav-btn {
        width: 36px; height: 36px; border-radius: 50%;
        border: 1px solid var(--divider-color, #ccc);
        background: var(--intercom-control-surface);
        background-color: var(--intercom-control-surface);
        color: var(--primary-text-color); cursor: pointer;
        font-size: 1.2em; display: flex; align-items: center; justify-content: center;
      }
      .nav-btn:hover { background: var(--intercom-control-hover-surface); }
      .nav-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      .destination-value {
        flex: 1; text-align: center; font-size: 1.1em; font-weight: 500;
        color: var(--primary-text-color); padding: 8px 0;
      }
      .destination-value.selecting { padding: 0; }
      .destination-value.selecting .destination-text { display: none; }
      .destination-select {
        width: 100%; box-sizing: border-box; padding: 8px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px; background: var(--intercom-control-surface);
        background-color: var(--intercom-control-surface);
        color: var(--primary-text-color); font-size: 0.95em;
        color-scheme: light dark;
        box-shadow: none;
      }
      .destination-select[hidden] { display: none; }
      .destination-label {
        font-size: 0.75em; color: var(--secondary-text-color);
        display: block; margin-bottom: 2px;
      }

      .button-container { display: flex; justify-content: center; gap: 20px; margin-bottom: 16px; }
      .offline-panel {
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 8px; min-height: 132px; margin-bottom: 14px;
        color: var(--error-color, #f44336);
      }
      .offline-panel[hidden] { display: none; }
      .offline-icon ha-icon { --mdc-icon-size: 64px; }
      .offline-title { font-size: 1.1em; font-weight: 600; color: var(--primary-text-color); }
      .intercom-button {
        width: 100px; height: 100px; border-radius: 50%; border: none; cursor: pointer;
        font-size: 1em; font-weight: bold; transition: all 0.2s ease;
        display: flex; align-items: center; justify-content: center;
      }
      .intercom-button[hidden] { display: none; }
      .intercom-button.small { width: 80px; height: 80px; font-size: 0.9em; }
      .intercom-button.call { background: #4caf50; color: white; }
      .intercom-button.answer { background: #4caf50; color: white; animation: ring-pulse 1s infinite; }
      .intercom-button.decline { background: #f44336; color: white; animation: ring-pulse 1s infinite; }
      .intercom-button.hangup { background: #f44336; color: white; }
      .intercom-button:disabled { opacity: 0.5; cursor: not-allowed; animation: none; }
      @keyframes ring-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }

      .status { text-align: center; color: var(--secondary-text-color); font-size: 0.9em; }
      .status-reason { text-align: center; color: var(--secondary-text-color); font-size: 0.85em; margin-top: 4px; padding: 0 12px; word-wrap: break-word; }
      .status-reason[hidden] { display: none; }
      .status-indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
      .status-indicator.connected { background: #4caf50; }
      .status-indicator.disconnected { background: #9e9e9e; }
      .status-indicator.unavailable { background: #f44336; }
      .status-indicator.transitioning { background: #ff9800; animation: blink 0.5s infinite; }
      .status-indicator.ringing { background: #ff9800; animation: blink 0.5s infinite; }
      @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

      .stats { font-size: 0.75em; color: #666; margin-top: 8px; text-align: center; }
      .error { color: #f44336; font-size: 0.85em; text-align: center; margin-top: 8px; }
      .auto-answer-row {
        display: flex; align-items: center; justify-content: center;
        gap: 8px; margin-top: 10px; font-size: 0.85em; color: var(--secondary-text-color);
      }
      .auto-answer-row[hidden] { display: none; }
      .auto-answer-row input { cursor: pointer; accent-color: var(--primary-color); }
      .auto-answer-row label { cursor: pointer; user-select: none; }
      .version { font-size: 0.65em; color: #999; text-align: right; margin-top: 8px; }
    `;
    root.appendChild(style);

    const card = document.createElement("div");
    card.className = "card";

    const header = document.createElement("div");
    header.className = "header";
    const headerName = document.createTextNode("");
    header.appendChild(headerName);
    card.appendChild(header);

    // Destination row
    const destRow = document.createElement("div");
    destRow.className = "destination-row";
    const prevBtn = document.createElement("button");
    prevBtn.className = "nav-btn";
    prevBtn.title = "Previous";
    prevBtn.textContent = "<";
    const destValueWrap = document.createElement("div");
    destValueWrap.className = "destination-value";
    const destLabel = document.createElement("span");
    destLabel.className = "destination-label";
    destLabel.textContent = "Destination";
    destValueWrap.appendChild(destLabel);
    const destValue = document.createTextNode("");
    const destText = document.createElement("span");
    destText.className = "destination-text";
    destText.appendChild(destValue);
    destValueWrap.appendChild(destText);
    const destSelect = document.createElement("select");
    destSelect.className = "destination-select";
    destSelect.hidden = true;
    destValueWrap.appendChild(destSelect);
    const nextBtn = document.createElement("button");
    nextBtn.className = "nav-btn";
    nextBtn.title = "Next";
    nextBtn.textContent = ">";
    destRow.appendChild(prevBtn);
    destRow.appendChild(destValueWrap);
    destRow.appendChild(nextBtn);
    card.appendChild(destRow);

    const offlinePanel = document.createElement("div");
    offlinePanel.className = "offline-panel";
    offlinePanel.hidden = true;
    const offlineIcon = document.createElement("div");
    offlineIcon.className = "offline-icon";
    const offlineHaIcon = document.createElement("ha-icon");
    offlineHaIcon.setAttribute("icon", "mdi:phone-off");
    offlineIcon.appendChild(offlineHaIcon);
    const offlineTitle = document.createElement("div");
    offlineTitle.className = "offline-title";
    offlineTitle.textContent = "ESP unavailable";
    offlinePanel.appendChild(offlineIcon);
    offlinePanel.appendChild(offlineTitle);
    card.appendChild(offlinePanel);

    // Button container with all four action buttons + a placeholder.
    // Visibility toggled in _render via [hidden].
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "button-container";
    const answerBtn = document.createElement("button");
    answerBtn.className = "intercom-button small answer";
    answerBtn.textContent = "Answer";
    const declineBtn = document.createElement("button");
    declineBtn.className = "intercom-button small decline";
    declineBtn.textContent = "Decline";
    const hangupBtn = document.createElement("button");
    hangupBtn.className = "intercom-button hangup";
    hangupBtn.textContent = "Hangup";
    const callBtn = document.createElement("button");
    callBtn.className = "intercom-button call";
    callBtn.textContent = "Call";
    const placeholderBtn = document.createElement("button");
    placeholderBtn.className = "intercom-button";
    placeholderBtn.textContent = "...";
    placeholderBtn.disabled = true;
    buttonContainer.appendChild(answerBtn);
    buttonContainer.appendChild(declineBtn);
    buttonContainer.appendChild(hangupBtn);
    buttonContainer.appendChild(callBtn);
    buttonContainer.appendChild(placeholderBtn);
    card.appendChild(buttonContainer);

    // Status line + optional reason on its own row
    const statusRow = document.createElement("div");
    statusRow.className = "status";
    const statusIndicator = document.createElement("span");
    statusIndicator.className = "status-indicator disconnected";
    statusRow.appendChild(statusIndicator);
    statusRow.appendChild(document.createTextNode(" "));
    const statusText = document.createTextNode("");
    statusRow.appendChild(statusText);
    card.appendChild(statusRow);

    const statusReason = document.createElement("div");
    statusReason.className = "status-reason";
    statusReason.hidden = true;
    card.appendChild(statusReason);

    // Auto-answer toggle
    const autoAnswerRow = document.createElement("div");
    autoAnswerRow.className = "auto-answer-row";
    const autoAnswerCheckbox = document.createElement("input");
    autoAnswerCheckbox.type = "checkbox";
    autoAnswerCheckbox.id = "auto-answer-cb";
    const autoAnswerLabel = document.createElement("label");
    autoAnswerLabel.htmlFor = "auto-answer-cb";
    autoAnswerLabel.textContent = "Auto Answer";
    autoAnswerRow.appendChild(autoAnswerCheckbox);
    autoAnswerRow.appendChild(autoAnswerLabel);
    card.appendChild(autoAnswerRow);

    const dndRow = document.createElement("div");
    dndRow.className = "auto-answer-row";
    const dndCheckbox = document.createElement("input");
    dndCheckbox.type = "checkbox";
    dndCheckbox.id = "ha-softphone-dnd-cb";
    const dndLabel = document.createElement("label");
    dndLabel.htmlFor = "ha-softphone-dnd-cb";
    dndLabel.textContent = "Do Not Disturb";
    dndRow.appendChild(dndCheckbox);
    dndRow.appendChild(dndLabel);
    card.appendChild(dndRow);

    const stats = document.createElement("div");
    stats.className = "stats";
    card.appendChild(stats);

    const err = document.createElement("div");
    err.className = "error";
    card.appendChild(err);

    const version = document.createElement("div");
    version.className = "version";
    version.textContent = "v" + INTERCOM_CARD_VERSION;
    card.appendChild(version);

    root.appendChild(card);

    this._els = {
      headerName,
      destRow, destValueWrap, destValue, destSelect, prevBtn, nextBtn, offlinePanel,
      answerBtn, declineBtn, hangupBtn, callBtn, placeholderBtn,
      statusIndicator, statusText, statusReason,
      autoAnswerRow, autoAnswerCheckbox, dndRow, dndCheckbox,
      stats, err,
    };

    this._attachEventHandlers();
  }

  _buildSkeletonUnconfigured() {
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .card {
        background: var(--ha-card-background, var(--card-background-color, white));
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
        padding: 16px;
      }
      .header { font-size: 1.2em; font-weight: 500; margin-bottom: 16px; color: var(--primary-text-color); }
      .unconfigured { text-align: center; color: var(--secondary-text-color); padding: 20px; font-style: italic; }
      .version { font-size: 0.65em; color: #999; text-align: right; margin-top: 8px; }
    `;
    root.appendChild(style);

    const card = document.createElement("div");
    card.className = "card";

    const header = document.createElement("div");
    header.className = "header";
    const headerName = document.createTextNode("");
    header.appendChild(headerName);
    card.appendChild(header);

    const unconfigured = document.createElement("div");
    unconfigured.className = "unconfigured";
    unconfigured.textContent = "Please configure the card to select an intercom device.";
    card.appendChild(unconfigured);

    const version = document.createElement("div");
    version.className = "version";
    version.textContent = "v" + INTERCOM_CARD_VERSION;
    card.appendChild(version);

    root.appendChild(card);

    this._els = { headerName };
  }

  _attachEventHandlers() {
    const els = this._els;
    if (!els) return;
    els.autoAnswerCheckbox.onchange = () => this._toggleAutoAnswer();
    if (els.dndCheckbox) els.dndCheckbox.onchange = () => this._toggleDnd();
    els.callBtn.onclick = () => this._startCall();
    els.hangupBtn.onclick = () => this._hangup();
    els.answerBtn.onclick = () => this._answer();
    els.declineBtn.onclick = () => this._decline();
    els.prevBtn.onclick = () => this._prevContact();
    els.nextBtn.onclick = () => this._nextContact();
    if (els.destSelect) {
      els.destSelect.onchange = (event) => this._setSoftphoneTarget(event.target.value);
    }
  }

  _renderSoftphoneDestinationSelect(select) {
    const targets = this._softphoneTargets();
    const current = this._getSoftphoneTargetDevice();
    const options = [];
    if (targets.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No endpoints";
      options.push(opt);
    } else {
      for (const device of targets) {
        const opt = document.createElement("option");
        opt.value = device.device_id;
        opt.textContent = device.name || device.device_id;
        if (device.device_id === current?.device_id) opt.selected = true;
        options.push(opt);
      }
    }
    select.replaceChildren(...options);
    select.disabled = this._starting || this._stopping || targets.length === 0;
  }

  _setSoftphoneTarget(deviceId) {
    this._softphoneTargetDeviceId = deviceId || null;
    this._saveSoftphoneTargetPreference(this._softphoneTargetDeviceId);
    this._render();
  }

  async _prevContact() {
    if (this._isHaSoftphoneMode()) {
      return;
    }
    if (this._previousButtonEntityId) {
      await this._hass.callService("button", "press", { entity_id: this._previousButtonEntityId });
    }
  }

  async _nextContact() {
    if (this._isHaSoftphoneMode()) {
      return;
    }
    if (this._nextButtonEntityId) {
      await this._hass.callService("button", "press", { entity_id: this._nextButtonEntityId });
    }
  }

  async _startCall() {
    const deviceInfo = await this._getDeviceInfo();
    if (this._isHaSoftphoneMode()) {
      await this._startHaSoftphoneCall(deviceInfo);
      return;
    }
    if (deviceInfo?.softphone) {
      this._showError("Set card mode to Home Assistant softphone to call from HA");
      return;
    }
    if (!deviceInfo?.host) {
      this._showError("Device not available");
      return;
    }

    this._activeDeviceInfo = deviceInfo;
    this._starting = true;
    this._errorMsg = "";
    this._render();

    try {
      const destination = this._getDestination();

      // Destination = HA: the card is the HA/browser softphone for this ESP.
      // Destination = another ESP: mirror the ESP's own Call button.
      if (!this._isHaName(destination)) {
        this._callMode = "mirror";
        await this._pressEspButton(this._callButtonEntityId, "Call");
      } else {
        this._callMode = "softphone";
        await this._startP2P(deviceInfo);
      }
    } catch (err) {
      this._showError(err.message || String(err));
      await this._cleanup();
    } finally {
      this._starting = false;
      this._render();
    }
  }

  async _setupMicAndSpeaker(deviceInfo) {
    const mode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const sendToEsp = mode === "full_duplex" || mode === "speaker_only";
    const receiveFromEsp = mode === "full_duplex" || mode === "mic_only";

    if (sendToEsp) {
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });

      const track = this._mediaStream.getAudioTracks()[0];
      const trackSampleRate = track?.getSettings?.().sampleRate;
      this._audioContext = new (window.AudioContext || window.webkitAudioContext)(
        trackSampleRate ? { sampleRate: trackSampleRate } : undefined
      );
      if (this._audioContext.state === "suspended") await this._audioContext.resume();

      this._source = this._audioContext.createMediaStreamSource(this._mediaStream);

      await this._audioContext.audioWorklet.addModule(`/intercom-native/intercom-processor.js?v=${INTERCOM_CARD_VERSION}`);
      this._workletNode = new AudioWorkletNode(this._audioContext, "intercom-processor");
      this._workletNode.port.onmessage = (e) => {
        if (e.data.type === "audio") this._sendAudio(new Int16Array(e.data.buffer));
      };
      this._source.connect(this._workletNode);
    }

    if (receiveFromEsp) {
      this._playbackContext = new (window.AudioContext || window.webkitAudioContext)();
      this._gainNode = this._playbackContext.createGain();
      this._gainNode.gain.value = 1.0;
      this._gainNode.connect(this._playbackContext.destination);
    }
  }

  async _startP2P(deviceInfo) {
    await this._setupMicAndSpeaker(deviceInfo);

    const result = await this._hass.connection.sendMessagePromise({
      type: "intercom_native/start",
      device_id: deviceInfo.device_id,
      host: deviceInfo.host,
    });
    if (!result.success) throw new Error("Start failed");

    this._unsubscribeAudio = await this._hass.connection.subscribeMessage(
      (msg) => this._handleAudioMessage(msg),
      { type: "intercom_native/subscribe_audio", device_id: deviceInfo.device_id }
    );

    this._audioStreaming = true;
    this._chunksSent = 0;
    this._chunksReceived = 0;
  }

  async _startHaSoftphoneCall(softphoneInfo) {
    const target = this._getSoftphoneTargetDevice();
    if (!target?.device_id || !target?.host) {
      this._showError("No endpoint available");
      return;
    }

    const sessionInfo = {
      ...(softphoneInfo || {}),
      device_id: HA_SOFTPHONE_DEVICE_ID,
      name: this._getHaName(),
      audio_mode: target.audio_mode || "full_duplex",
      softphone: true,
    };
    this._activeDeviceInfo = sessionInfo;
    this._activeSessionDeviceId = HA_SOFTPHONE_DEVICE_ID;
    this._starting = true;
    this._callMode = "softphone";
    this._errorMsg = "";
    this._render();

    try {
      await this._setupMicAndSpeaker(sessionInfo);
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/ha_softphone_start",
        target_device_id: target.device_id,
      });
      if (!result?.success) throw new Error("Start failed");
      this._sessionState = result.state === "ringing" ? "outgoing" : (result.state || "calling");
      this._destRinging = result.state === "ringing";
      this._sessionCaller = target.name || "";
      this._unsubscribeAudio = await this._hass.connection.subscribeMessage(
        (msg) => this._handleAudioMessage(msg),
        { type: "intercom_native/subscribe_audio", device_id: HA_SOFTPHONE_DEVICE_ID }
      );
      this._audioStreaming = true;
      this._chunksSent = 0;
      this._chunksReceived = 0;
    } catch (err) {
      this._showError(err.message || String(err));
      await this._cleanup();
    } finally {
      this._starting = false;
      this._render();
    }
  }

  async _answerEspCall(deviceInfo) {
    await this._setupMicAndSpeaker(deviceInfo);

    const result = await this._hass.connection.sendMessagePromise({
      type: "intercom_native/answer_esp_call",
      device_id: deviceInfo.device_id,
      host: deviceInfo.host,
    });
    if (!result.success) throw new Error("Answer failed");

    this._unsubscribeAudio = await this._hass.connection.subscribeMessage(
      (msg) => this._handleAudioMessage(msg),
      { type: "intercom_native/subscribe_audio", device_id: deviceInfo.device_id }
    );

    this._audioStreaming = true;
    this._chunksSent = 0;
    this._chunksReceived = 0;
  }

  async _startBridge(sourceDevice, destinationName) {
    const destDevice = this._availableDevices.find(d => d.name === destinationName);
    if (!destDevice?.device_id) {
      throw new Error(`Destination "${destinationName}" not available`);
    }

    // PBX-lite: HA never opens a bridge directly. Calling intercom_native.call
    // tells the source ESP to start its own outgoing call (FSM goes to
    // OUTGOING and emits MSG_START). HA reactively opens the BridgeSession
    // when that unsolicited MSG_START arrives.
    await this._hass.callService("intercom_native", "call", {
      source: sourceDevice.device_id,
      device_id: destDevice.device_id,
    });
    // The bridge is created asynchronously HA-side when the source ESP
    // emits MSG_START; we track its state via the intercom_state sensor.
  }

  async _answer() {
    const deviceInfo = await this._getDeviceInfo();
    if (!deviceInfo?.device_id) {
      this._showError("Device not found");
      return;
    }

    this._starting = true;
    this._activeDeviceInfo = deviceInfo;
    this._errorMsg = "";
    this._render();

    try {
      const espState = this._getEspState().toLowerCase();
      const destination = this._getDestination();
      const softphone = this._isSoftphoneContext();

      if (this._isHaSoftphoneMode()) {
        const sessionDeviceId = this._sessionDeviceId();
        const peer = this._availableDevices.find(d => d.device_id === sessionDeviceId) || deviceInfo;
        const sessionInfo = {
          ...(deviceInfo || {}),
          device_id: sessionDeviceId,
          audio_mode: peer?.audio_mode || "full_duplex",
          softphone: true,
        };
        this._activeDeviceInfo = sessionInfo;
        this._activeSessionDeviceId = sessionDeviceId;
        this._callMode = "softphone";
        await this._setupMicAndSpeaker(sessionInfo);
        const res = await this._hass.connection.sendMessagePromise({
          type: "intercom_native/answer",
          device_id: sessionDeviceId,
        });
        if (!res?.success) throw new Error("Answer failed");
        this._unsubscribeAudio = await this._hass.connection.subscribeMessage(
          (msg) => this._handleAudioMessage(msg),
          { type: "intercom_native/subscribe_audio", device_id: sessionDeviceId }
        );
        this._audioStreaming = true;
        this._chunksSent = 0;
        this._chunksReceived = 0;
        return;
      }

      // Check if ESP is calling HA (outgoing + destination matches the HA instance)
      if ((espState === "outgoing" || espState === "calling") && this._isHaName(destination)) {
        // ESP is calling us - answer with proper ANSWER message (not START)
        this._callMode = "softphone";
        await this._answerEspCall(deviceInfo);
      } else if (!softphone) {
        // Mirror mode: use the ESP's real smart Call button (ringing -> answer).
        this._callMode = "mirror";
        await this._pressEspButton(this._callButtonEntityId, "Call");
      } else {
        // HA/browser softphone session.
        this._callMode = "softphone";
        const res = await this._hass.connection.sendMessagePromise({
          type: "intercom_native/answer",
          device_id: deviceInfo.device_id,
        });

        if (!res?.success && this._callButtonEntityId) {
          // Fallback: press call button on ESP
          await this._hass.callService("button", "press", { entity_id: this._callButtonEntityId });
        }
      }
    } catch (err) {
      this._showError(err.message || String(err));
      await this._cleanup();
    } finally {
      this._starting = false;
      this._render();
    }
  }

  async _decline() {
    const deviceInfo = await this._getDeviceInfo();
    if (!deviceInfo?.device_id) {
      this._showError("Device not found");
      return;
    }

    this._stopping = true;
    this._errorMsg = "";
    this._render();

    try {
      if (this._isSoftphoneContext()) {
        await this._hass.connection.sendMessagePromise({
          type: "intercom_native/decline",
          device_id: this._sessionDeviceId(),
        });
      } else {
        await this._pressEspButton(this._declineButtonEntityId, "Decline");
      }
    } catch (err) {
      this._showError(err.message || String(err));
    } finally {
      this._stopping = false;
      this._render();
    }
  }

  async _hangup() {
    this._stopping = true;
    this._errorMsg = "";
    this._render();

    try {
      const deviceInfo = this._activeDeviceInfo || await this._getDeviceInfo();
      if (!deviceInfo?.device_id) {
        throw new Error("Device not found");
      }
      this._activeDeviceInfo = deviceInfo;

      if (this._isSoftphoneContext()) {
        await this._hass.connection.sendMessagePromise({
          type: "intercom_native/stop",
          device_id: this._sessionDeviceId(),
        });
      } else {
        // Mirror mode: Hangup is the ESP's Decline button. Firmware maps
        // Decline during STREAMING to stop(), and idle is a no-op.
        await this._pressEspButton(this._declineButtonEntityId, "Decline");
      }
      if (this._isSoftphoneContext()) {
        this._captureEndReason("disconnected", "local_hangup", "self");
      }
    } catch (err) {
      console.error("Hangup error:", err);
      this._showError(err.message || String(err));
    }

    await this._cleanup();
    this._stopping = false;
    this._render();
  }

  async _cleanup() {
    const wasSoftphone = this._isSoftphoneContext();
    if (this._unsubscribeAudio) { this._unsubscribeAudio(); this._unsubscribeAudio = null; }
    if (this._mediaStream) { this._mediaStream.getTracks().forEach(t => t.stop()); this._mediaStream = null; }
    if (this._workletNode) { this._workletNode.disconnect(); this._workletNode = null; }
    if (this._source) { this._source.disconnect(); this._source = null; }
    if (this._audioContext) { await this._audioContext.close().catch(() => {}); this._audioContext = null; }
    if (this._playbackContext) { await this._playbackContext.close().catch(() => {}); this._playbackContext = null; }
    this._gainNode = null;
    this._nextPlayTime = 0;
    this._activeDeviceInfo = null;
    this._callMode = null;
    this._audioStreaming = false;
    if (wasSoftphone) {
      this._sessionState = null;
      this._sessionCaller = "";
      this._activeSessionDeviceId = null;
    }
  }

  async _tryAutoAnswer(options = {}) {
    const requirePersistentPermission = options.requirePersistentPermission !== false;
    // Check if browser has persistent mic permission
    try {
      if (requirePersistentPermission && navigator.permissions?.query) {
        const perm = await navigator.permissions.query({ name: "microphone" });
        if (perm.state !== "granted") {
          _ic_log.info("intercom: auto-answer skipped, mic permission not persistent");
          this._autoAnswering = false;
          return;
        }
      }
      // permissions.query not available or permission granted: try answering
      _ic_log.info("intercom: auto-answering call");
      await this._answer();
    } catch (e) {
      console.warn("intercom: auto-answer failed", e);
    } finally {
      this._autoAnswering = false;
    }
  }

  _toggleAutoAnswer() {
    this._autoAnswer = !this._autoAnswer;
    const deviceId = this._autoAnswerStorageId();
    if (deviceId) {
      localStorage.setItem(`intercom_auto_answer_${deviceId}`, this._autoAnswer.toString());
    }
    // If enabling, request mic permission now (user gesture from the toggle click)
    const device = this._availableDevices.find(d => this._deviceMatchesConfig(d));
    const needsBrowserMic = ["full_duplex", "speaker_only"].includes(
      this._normaliseAudioMode(device?.audio_mode)
    );
    if (this._autoAnswer && needsBrowserMic && navigator.mediaDevices?.getUserMedia) {
      navigator.mediaDevices.getUserMedia({ audio: true })
        .then(stream => {
          // Got permission, release stream immediately
          stream.getTracks().forEach(t => t.stop());
          _ic_log.info("intercom: mic permission granted for auto-answer");
        })
        .catch(err => {
          console.warn("intercom: mic permission denied, auto-answer may not work", err);
        });
    }
    this._render();
  }

  async _toggleDnd() {
    const next = !this._softphoneDnd;
    this._softphoneDnd = next;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/set_ha_softphone_dnd",
        dnd: next,
      });
      this._softphoneDnd = !!result?.dnd;
    } catch (err) {
      this._softphoneDnd = !next;
      this._showError(err.message || String(err));
    }
    this._render();
  }

  async _loadSoftphoneState() {
    if (!this._hass?.connection || this._softphoneStateLoading) return;
    this._softphoneStateLoading = true;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/ha_softphone_state",
      });
      this._softphoneDnd = !!result?.dnd;
      if (result?.state && result.state !== "idle") {
        this._sessionState = result.state;
        this._activeSessionDeviceId = result.session_device_id || null;
        this._sessionCaller = result.caller || "";
      }
      this._softphoneStateLoaded = true;
    } catch (err) {
      if (!this._isUnknownCommandError(err)) console.warn("intercom: failed loading HA softphone state", err);
    } finally {
      this._softphoneStateLoading = false;
      this._render();
    }
  }

  _cycleSoftphoneTarget(delta) {
    const targets = this._softphoneTargets();
    if (targets.length === 0) return;
    const current = this._getSoftphoneTargetDevice();
    const idx = Math.max(0, targets.findIndex(d => d.device_id === current?.device_id));
    const next = targets[(idx + delta + targets.length) % targets.length];
    this._softphoneTargetDeviceId = next.device_id;
    this._render();
  }

  async _getDeviceInfo() {
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/list_devices",
      });
      if (result?.devices) {
        const configId = this.config.entity_id || this.config.device_id;
        if (this._isHaSoftphoneMode()) {
          return result.devices.find(d => d.device_id === HA_SOFTPHONE_DEVICE_ID) || {
            device_id: HA_SOFTPHONE_DEVICE_ID,
            name: this._getHaName(),
            audio_mode: "full_duplex",
            softphone: true,
          };
        }
        return result.devices.find(d =>
          d.device_id === configId ||
          d.esphome_id === configId ||
          d.name === configId ||
          d.name?.toLowerCase().replace(/\s+/g, '-') === configId
        );
      }
    } catch (err) {
      console.error("Failed to get device info:", err);
    }
    return null;
  }

  _sendAudio(int16Array) {
    if (!this._audioStreaming || !this._activeDeviceInfo) return;
    const bytes = new Uint8Array(
      int16Array.buffer,
      int16Array.byteOffset,
      int16Array.byteLength
    );
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + 0x8000, bytes.length)));
    }
    this._hass.connection.sendMessage({
      type: "intercom_native/audio",
      device_id: this._activeDeviceInfo.device_id,
      audio: btoa(binary),
    });
    this._chunksSent++;
    if (this._chunksSent % 25 === 0) this._updateStats();
  }

  _handleAudioMessage(msg) {
    if (!msg || !this._activeDeviceInfo) return;
    if (msg.device_id !== this._activeDeviceInfo.device_id) return;
    if (!this._audioStreaming || !this._playbackContext) return;

    this._chunksReceived++;
    if (this._chunksReceived % 50 === 0) this._updateStats();

    try {
      const binary = atob(msg.audio);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

      const int16 = new Int16Array(bytes.buffer);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768.0;

      this._playScheduled(float32);
    } catch (err) { _ic_log.debug("intercom: audio error", err); }
  }

  _playScheduled(float32) {
    if (!this._playbackContext || !this._gainNode) return;
    try {
      const buffer = this._playbackContext.createBuffer(1, float32.length, 16000);
      buffer.getChannelData(0).set(float32);
      const now = this._playbackContext.currentTime;
      if (this._nextPlayTime < now) this._nextPlayTime = now + 0.01;
      if (this._nextPlayTime - now > 0.2) { this._nextPlayTime = now + 0.02; return; }
      const src = this._playbackContext.createBufferSource();
      src.buffer = buffer;
      src.connect(this._gainNode);
      src.start(this._nextPlayTime);
      this._nextPlayTime += buffer.duration;
    } catch (err) { _ic_log.debug("intercom: audio error", err); }
  }

  _updateStats() {
    if (this._els?.stats && this._audioStreaming) {
      this._els.stats.textContent = `Sent: ${this._chunksSent} | Recv: ${this._chunksReceived}`;
    }
  }

  _showError(msg) {
    this._errorMsg = msg || "";
    if (this._els?.err) this._els.err.textContent = this._errorMsg;
  }

  getCardSize() { return 3; }

  static getConfigElement() {
    return document.createElement("intercom-card-editor");
  }

  static getStubConfig() {
    return { name: "Intercom" };
  }
}

// Card editor
class IntercomCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._devices = [];
    this._devicesLoaded = false;
    this._devicesLoading = false;
    this._devicesRetryTimer = null;
    this._els = null;
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._devicesLoaded) this._loadDevices();
  }

  disconnectedCallback() {
    if (this._devicesRetryTimer) {
      clearTimeout(this._devicesRetryTimer);
      this._devicesRetryTimer = null;
    }
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v)
      ? v
      : "full_duplex";
  }

  _audioModeLabel(mode) {
    switch (this._normaliseAudioMode(mode)) {
      case "mic_only": return "MIC";
      case "speaker_only": return "SPK";
      case "control_only": return "CTRL";
      default: return "FULL";
    }
  }

  async _loadDevices() {
    if (!this._hass || this._devicesLoaded || this._devicesLoading) return;
    if (!this._isIntercomNativeLoaded()) {
      this._scheduleLoadDevices();
      return;
    }
    this._devicesLoading = true;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "intercom_native/list_devices",
      });
      if (result?.devices) {
        this._devices = result.devices;
        this._devicesLoaded = true;
        this._render();
      }
    } catch (err) {
      if (this._isUnknownCommandError(err)) this._scheduleLoadDevices();
      else console.error("Failed to load devices:", err);
    } finally {
      this._devicesLoading = false;
    }
  }

  _isIntercomNativeLoaded() {
    const components = this._hass?.config?.components;
    return !Array.isArray(components) || components.includes("intercom_native");
  }

  _isUnknownCommandError(err) {
    const code = String(err?.code || err?.error || "");
    const message = String(err?.message || "");
    return code.includes("unknown_command") || message.includes("unknown command");
  }

  _scheduleLoadDevices() {
    if (this._devicesRetryTimer) return;
    this._devicesRetryTimer = setTimeout(() => {
      this._devicesRetryTimer = null;
      this._loadDevices();
    }, 2000);
  }

  // Static skeleton + textContent mutation. The select option list is
  // rebuilt via createElement (no innerHTML) because device_id / name
  // come from HA / user config and must not be templated into HTML.
  _buildSkeleton() {
    this.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      .form-group { margin-bottom: 16px; }
      .form-group label { display: block; margin-bottom: 4px; font-weight: 500; color: var(--primary-text-color); }
      .form-group input, .form-group select {
        width: 100%; padding: 8px; border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px; background: var(--card-background-color, white);
        color: var(--primary-text-color); font-size: 1em; box-sizing: border-box;
      }
      .checkbox-group label { display: flex; align-items: center; gap: 8px; }
      .checkbox-group input { width: auto; padding: 0; }
      .info { color: var(--secondary-text-color); font-size: 0.85em; margin-top: 8px; }
      .hidden { display: none; }
    `;
    this.appendChild(style);

    const wrap = document.createElement("div");
    wrap.style.padding = "16px";

    const modeGroup = document.createElement("div");
    modeGroup.className = "form-group";
    const modeLabel = document.createElement("label");
    modeLabel.textContent = "Card Mode";
    modeGroup.appendChild(modeLabel);
    const modeSelect = document.createElement("select");
    modeSelect.id = "mode-select";
    const hybridOpt = document.createElement("option");
    hybridOpt.value = "hybrid";
    hybridOpt.textContent = "Hybrid ESP card";
    const softphoneOpt = document.createElement("option");
    softphoneOpt.value = "ha_softphone";
    softphoneOpt.textContent = "Home Assistant softphone";
    modeSelect.appendChild(hybridOpt);
    modeSelect.appendChild(softphoneOpt);
    modeGroup.appendChild(modeSelect);
    const modeInfo = document.createElement("div");
    modeInfo.className = "info";
    modeGroup.appendChild(modeInfo);
    wrap.appendChild(modeGroup);

    // Device picker
    const deviceGroup = document.createElement("div");
    deviceGroup.className = "form-group";
    const deviceLabel = document.createElement("label");
    deviceLabel.textContent = "Intercom Device";
    deviceGroup.appendChild(deviceLabel);
    const select = document.createElement("select");
    select.id = "entity-select";
    deviceGroup.appendChild(select);
    const deviceInfo = document.createElement("div");
    deviceInfo.className = "info";
    deviceGroup.appendChild(deviceInfo);
    wrap.appendChild(deviceGroup);

    // Name input
    const nameGroup = document.createElement("div");
    nameGroup.className = "form-group";
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Card Name (optional)";
    nameGroup.appendChild(nameLabel);
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.id = "name-input";
    nameInput.placeholder = "Intercom";
    nameGroup.appendChild(nameInput);
    wrap.appendChild(nameGroup);

    const extendedInfoGroup = document.createElement("div");
    extendedInfoGroup.className = "form-group checkbox-group";
    const extendedInfoLabel = document.createElement("label");
    const extendedInfoInput = document.createElement("input");
    extendedInfoInput.type = "checkbox";
    extendedInfoInput.id = "show-extended-info-input";
    extendedInfoLabel.appendChild(extendedInfoInput);
    extendedInfoLabel.appendChild(document.createTextNode(" Extended information"));
    extendedInfoGroup.appendChild(extendedInfoLabel);
    wrap.appendChild(extendedInfoGroup);

    this.appendChild(wrap);

    modeSelect.onchange = (e) => this._modeChanged(e.target.value);
    select.onchange = (e) => this._valueChanged("device_id", e.target.value);
    nameInput.onchange = (e) => this._valueChanged("name", e.target.value);
    extendedInfoInput.onchange = (e) => this._boolChanged("show_extended_info", e.target.checked);

    this._els = {
      modeSelect, modeInfo,
      deviceGroup, select, deviceInfo,
      nameInput, extendedInfoInput,
    };
  }

  _render() {
    if (!this._els) this._buildSkeleton();
    const els = this._els;
    const mode = this._config.mode || this._config.card_mode || "hybrid";
    const softphoneMode = mode === "ha_softphone";
    els.modeSelect.value = softphoneMode ? "ha_softphone" : "hybrid";
    els.modeInfo.textContent = softphoneMode
      ? "One Home Assistant endpoint: this card rings only for HA softphone calls and can call any ESP endpoint."
      : "Hybrid ESP card: mirrors one ESP endpoint; HA destination uses the browser as the HA leg for that ESP.";
    els.deviceGroup.classList.toggle("hidden", softphoneMode);

    // Rebuild the select option list safely: replaceChildren + per-row
    // createElement; option.value/textContent setters reject HTML injection.
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "-- Select device --";
    const newOptions = [placeholder];
    for (const d of this._devices) {
      const opt = document.createElement("option");
      opt.value = d.device_id;
      opt.textContent = `${d.name} (${this._audioModeLabel(d.audio_mode)})`;
      if ((this._config.device_id || this._config.entity_id) === d.device_id) opt.selected = true;
      newOptions.push(opt);
    }
    els.select.replaceChildren(...newOptions);

    if (!this._devicesLoaded) {
      els.deviceInfo.textContent = "Loading...";
    } else if (this._devices.length === 0) {
      els.deviceInfo.textContent = "No devices found";
    } else {
      const selected = this._devices.find(d => d.device_id === (this._config.device_id || this._config.entity_id));
      els.deviceInfo.textContent = selected
        ? `Audio: ${this._normaliseAudioMode(selected.audio_mode).replace("_", " ")}`
        : (softphoneMode ? "Home Assistant softphone does not belong to an ESP." : "Required for hybrid mode.");
    }

    els.nameInput.value = this._config.name || "";
    els.extendedInfoInput.checked = !!this._config.show_extended_info;
  }

  _valueChanged(key, value) {
    const newConfig = { ...this._config };
    if (value) newConfig[key] = value;
    else delete newConfig[key];
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: newConfig }, bubbles: true, composed: true }));
  }

  _modeChanged(value) {
    const newConfig = { ...this._config };
    if (value === "ha_softphone") {
      newConfig.mode = "ha_softphone";
      delete newConfig.device_id;
      delete newConfig.entity_id;
      delete newConfig.target_device_id;
    } else {
      delete newConfig.mode;
      delete newConfig.card_mode;
      delete newConfig.target_device_id;
    }
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: newConfig }, bubbles: true, composed: true }));
  }

  _boolChanged(key, checked) {
    const newConfig = { ...this._config };
    if (checked) newConfig[key] = true;
    else delete newConfig[key];
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: newConfig }, bubbles: true, composed: true }));
  }
}

// Idempotent define so HMR / re-installs don't throw.
if (!customElements.get("intercom-card")) {
  customElements.define("intercom-card", IntercomCard);
}
if (!customElements.get("intercom-card-editor")) {
  customElements.define("intercom-card-editor", IntercomCardEditor);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "intercom-card",
  name: "Intercom Card",
  description: "ESP intercom control - PBX-lite mirror of ESP state",
  preview: true,
});
