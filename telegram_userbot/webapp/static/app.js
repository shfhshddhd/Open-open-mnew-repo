(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const notice = document.getElementById("notice");
  const retry = document.getElementById("retry");
  const refreshState = document.getElementById("refresh-state");
  const startLiveButton = document.getElementById("start-live");
  const stopLiveButton = document.getElementById("stop-live");
  const micToggle = document.getElementById("mic-toggle");
  const pttToggle = document.getElementById("ptt-toggle");
  const talkButton = document.getElementById("talk");
  const gainInput = document.getElementById("gain");
  const gainValue = document.getElementById("gain-value");
  const gainPresets = Array.from(document.querySelectorAll(".gain-preset"));
  const MAX_GAIN = 100000000;

  let ticket = "";
  let liveSocket = null;
  let mediaStream = null;
  let audioContext = null;
  let audioWorkletNode = null;
  let silentGain = null;
  let receiveAudioContext = null;
  let receiveWorkletNode = null;
  let micEnabled = false;
  let pushToTalk = false;
  let pushActive = false;
  let liveActive = false;
  let voiceConnected = false;
  let statusTimer = null;

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.colorScheme === "light") {
      document.documentElement.dataset.telegramTheme = "light";
    }
  }

  function setNotice(message, kind = "") {
    notice.textContent = message;
    notice.className = `notice ${kind}`.trim();
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function showError(message) {
    setNotice(message, "error");
    retry.hidden = false;
    refreshState.classList.add("paused");
  }

  function setLiveControlsEnabled(enabled) {
    micToggle.disabled = !enabled;
    pttToggle.disabled = !enabled;
    talkButton.disabled = !enabled || !pushToTalk;
    gainInput.disabled = !enabled;
    gainPresets.forEach((button) => {
      button.disabled = !enabled;
    });
  }

  function normalizeGain(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 100;
    return Math.max(0, Math.min(MAX_GAIN, Math.trunc(parsed)));
  }

  function setGain(value, send = false) {
    const normalized = normalizeGain(value);
    gainInput.value = String(normalized);
    gainValue.textContent = String(normalized);
    if (send) sendCommand({ type: "gain", value: normalized });
  }

  function updateMicUi() {
    micToggle.textContent = micEnabled ? "Mic ON" : "Mic OFF";
    micToggle.setAttribute("aria-pressed", String(micEnabled));
    setText("mic-status", micEnabled && liveActive ? "On" : "Off");
    setText(
      "mic-detail",
      micEnabled ? "Audio is being sent when live is active" : "Audio is muted",
    );
  }

  function updatePttUi() {
    pttToggle.textContent = pushToTalk ? "PTT ON" : "PTT OFF";
    pttToggle.setAttribute("aria-pressed", String(pushToTalk));
    talkButton.disabled = !liveActive || !pushToTalk;
    talkButton.classList.toggle("active", pushActive);
    setText(
      "ptt-detail",
      pushToTalk ? "Hold the button below to send audio" : "Mic sends while enabled",
    );
  }

  function sendCommand(payload) {
    if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return false;
    liveSocket.send(JSON.stringify(payload));
    return true;
  }

  function syncControls() {
    sendCommand({
      type: "controls",
      mic_enabled: micEnabled,
      push_to_talk: pushToTalk,
      push_active: pushActive,
    });
  }

  function setState(payload) {
    if (payload.voice_chat !== undefined) {
      const voice = payload.voice_chat;
      voiceConnected = Boolean(voice.connected);
      setText("vc-status", voice.connected ? "Connected" : "Not connected");
      setText(
        "vc-detail",
        voice.connected ? voice.title || "Active Voice Chat" : "Use .vcjoin in the control bot",
      );
      if (voice.volume !== undefined && document.activeElement !== gainInput) {
        setGain(voice.volume);
      }
      if (voice.live) applyLiveState(voice.live, voice.volume);
      return;
    }
    if (payload.voice_connected !== undefined) {
      voiceConnected = Boolean(payload.voice_connected);
    }
    if (payload.live) applyLiveState(payload.live, payload.volume);
  }

  function applyLiveState(live, serverGain) {
    liveActive = Boolean(live.active);
    if (serverGain !== undefined && document.activeElement !== gainInput) {
      setGain(serverGain);
    }
    gainValue.textContent = gainInput.value;
    if (live.mic_enabled !== undefined) micEnabled = Boolean(live.mic_enabled);
    if (live.push_to_talk !== undefined) pushToTalk = Boolean(live.push_to_talk);
    if (live.push_active !== undefined) pushActive = Boolean(live.push_active);
    startLiveButton.disabled = liveActive || !voiceConnected;
    stopLiveButton.disabled = !liveActive;
    setLiveControlsEnabled(liveActive);
    updateMicUi();
    updatePttUi();
    setText("live-status", liveActive ? "Streaming" : "Stopped");
    setText("frame-status", String(live.frames || 0));
    setText(
      "live-detail",
      liveActive
        ? "Browser microphone frames are being sent to the connected Telegram Voice Chat."
        : "Use .vcjoin from the control bot first, then start the live microphone.",
    );
    if (liveActive) {
      refreshState.classList.remove("paused");
    }
  }

  function workletSource() {
    return `
      class PcmCaptureProcessor extends AudioWorkletProcessor {
        constructor() {
          super();
          this.sourceRate = sampleRate;
          this.targetRate = 48000;
          this.ratio = this.sourceRate / this.targetRate;
          this.buffer = [];
          this.position = 0;
           // NTgCalls external PCM input is fixed at 10 ms. With the
           // 48 kHz mono PCM16 stream configured by the server, that is
           // exactly 480 samples / 960 bytes per frame.
           this.frameSize = 480;
        }

        process(inputs) {
          const channels = inputs[0];
          if (!channels || channels.length === 0 || channels[0].length === 0) {
            return true;
          }
          const length = channels[0].length;
          for (let i = 0; i < length; i += 1) {
            let sample = 0;
            for (let channel = 0; channel < channels.length; channel += 1) {
              sample += channels[channel][i] || 0;
            }
            this.buffer.push(sample / channels.length);
          }

          while (this.buffer.length - this.position >= 2) {
            const available = Math.floor((this.buffer.length - 1 - this.position) / this.ratio);
            if (available < this.frameSize) break;
            const pcm = new Int16Array(this.frameSize);
            for (let i = 0; i < this.frameSize; i += 1) {
              const position = this.position + i * this.ratio;
              const index = Math.floor(position);
              const fraction = position - index;
              const value = this.buffer[index] * (1 - fraction) + this.buffer[index + 1] * fraction;
              const clipped = Math.max(-1, Math.min(1, value));
              pcm[i] = clipped < 0 ? clipped * 32768 : clipped * 32767;
            }
            this.position += this.frameSize * this.ratio;
            const consumed = Math.floor(this.position);
            if (consumed > 0) {
              this.buffer.splice(0, consumed);
              this.position -= consumed;
            }
            this.port.postMessage(pcm.buffer, [pcm.buffer]);
          }
          return true;
        }
      }
      registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
    `;
  }

  function receiveWorkletSource() {
    return `
      class PcmPlaybackProcessor extends AudioWorkletProcessor {
        constructor() {
          super();
          this.queue = [];
          this.current = null;
          this.position = 0;
          this.port.onmessage = (event) => {
            const channels = event.data && event.data.channels;
            const buffers = event.data && event.data.buffers;
            if (!buffers || !buffers.length || (channels !== 1 && channels !== 2)) return;
            this.queue.push({ channels, buffers });
            if (this.queue.length > 12) this.queue.shift();
          };
        }

        process(_inputs, outputs) {
          const output = outputs[0];
          const left = output[0];
          const right = output[1] || output[0];
          for (let i = 0; i < left.length; i += 1) {
            if (!this.current || this.position >= this.current.buffers[0].length) {
              this.current = this.queue.shift() || null;
              this.position = 0;
            }
            if (!this.current) {
              left[i] = 0;
              right[i] = 0;
              continue;
            }
            left[i] = this.current.buffers[0][this.position] || 0;
            right[i] = this.current.channels === 2
              ? (this.current.buffers[1][this.position] || 0)
              : left[i];
            this.position += 1;
          }
          return true;
        }
      }
      registerProcessor("pcm-playback-processor", PcmPlaybackProcessor);
    `;
  }

  async function startReceivePlayback() {
    if (receiveWorkletNode) return;
    receiveAudioContext = new AudioContext({ sampleRate: 48000 });
    const blob = new Blob([receiveWorkletSource()], { type: "application/javascript" });
    const moduleUrl = URL.createObjectURL(blob);
    try {
      await receiveAudioContext.audioWorklet.addModule(moduleUrl);
    } finally {
      URL.revokeObjectURL(moduleUrl);
    }
    receiveWorkletNode = new AudioWorkletNode(receiveAudioContext, "pcm-playback-processor", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [2],
    });
    receiveWorkletNode.connect(receiveAudioContext.destination);
    await receiveAudioContext.resume();
  }

  async function stopReceivePlayback() {
    if (receiveWorkletNode) {
      receiveWorkletNode.disconnect();
      receiveWorkletNode = null;
    }
    if (receiveAudioContext) {
      await receiveAudioContext.close().catch(() => {});
      receiveAudioContext = null;
    }
  }

  function playReceivedFrame(data) {
    if (!receiveWorkletNode || data.byteLength < 2) return;
    const input = new DataView(data);
    const sampleCount = data.byteLength / 2;
    // Telegram playback is 48 kHz PCM16. Common frame sizes identify
    // interleaved stereo versus mono without changing the original samples.
    const channels = sampleCount >= 960 && sampleCount % 2 === 0 ? 2 : 1;
    const frames = Math.floor(sampleCount / channels);
    const channelBuffers = Array.from(
      { length: channels },
      () => new Float32Array(frames),
    );
    for (let frame = 0; frame < frames; frame += 1) {
      for (let channel = 0; channel < channels; channel += 1) {
        const sample = input.getInt16((frame * channels + channel) * 2, true);
        channelBuffers[channel][frame] = sample < 0 ? sample / 32768 : sample / 32767;
      }
    }
    receiveWorkletNode.port.postMessage(
      { channels, buffers: channelBuffers },
      channelBuffers.map((buffer) => buffer.buffer),
    );
  }

  async function startCapture() {
    if (audioWorkletNode) return;
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Keep playback on a separate context and let the browser suppress
        // acoustic speaker echo in the microphone input only.
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: false,
      },
      video: false,
    });
    try {
      audioContext = new AudioContext({ sampleRate: 48000 });
    } catch (_error) {
      audioContext = new AudioContext();
    }
    const blob = new Blob([workletSource()], { type: "application/javascript" });
    const moduleUrl = URL.createObjectURL(blob);
    try {
      await audioContext.audioWorklet.addModule(moduleUrl);
    } finally {
      URL.revokeObjectURL(moduleUrl);
    }
    const source = audioContext.createMediaStreamSource(mediaStream);
    audioWorkletNode = new AudioWorkletNode(audioContext, "pcm-capture-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
    });
    silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    audioWorkletNode.port.onmessage = (event) => {
      if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
      if (!micEnabled || (pushToTalk && !pushActive)) return;
      // A congested socket means the proxy/native sender is behind. Dropping
      // the oldest unsent audio is preferable to turning a short hiccup into
      // seconds of delayed, robotic playback.
      if (liveSocket.bufferedAmount > 3840) return;
      liveSocket.send(event.data);
    };
    source.connect(audioWorkletNode);
    audioWorkletNode.connect(silentGain);
    silentGain.connect(audioContext.destination);
    await audioContext.resume();
  }

  async function stopCapture() {
    if (audioWorkletNode) {
      audioWorkletNode.port.onmessage = null;
      audioWorkletNode.disconnect();
      audioWorkletNode = null;
    }
    if (silentGain) {
      silentGain.disconnect();
      silentGain = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
    if (audioContext) {
      await audioContext.close().catch(() => {});
      audioContext = null;
    }
  }

  function closeLiveSocket() {
    if (!liveSocket) return;
    liveSocket.onclose = null;
    liveSocket.close();
    liveSocket = null;
  }

  async function startLive() {
    if (liveSocket || liveActive) return;
    if (!ticket || !voiceConnected) {
      showError("Join a Telegram Voice Chat with .vcjoin before starting live audio.");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError("This Telegram Mini App cannot access a microphone.");
      return;
    }
    startLiveButton.disabled = true;
    setNotice("Requesting microphone permission…");
    try {
      await startCapture();
      await startReceivePlayback();
    } catch (error) {
      await stopCapture();
      await stopReceivePlayback();
      startLiveButton.disabled = !voiceConnected;
      showError(`Microphone permission failed: ${error.message || "unknown error"}`);
      return;
    }

    const websocketUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${new URL("api/live", location.href).pathname}`;
    liveSocket = new WebSocket(websocketUrl);
    liveSocket.binaryType = "arraybuffer";
    liveSocket.onopen = () => {
      liveSocket.send(JSON.stringify({ type: "auth", ticket }));
      setNotice("Connecting the microphone to Telegram…");
    };
    liveSocket.onmessage = (event) => {
      if (typeof event.data !== "string") {
        playReceivedFrame(event.data);
        return;
      }
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      if (payload.type === "ready") {
        sendCommand({ type: "start" });
      } else if (payload.type === "state") {
        setState(payload);
        if (payload.live && payload.live.active) {
          setNotice("Live microphone is reaching the Telegram Voice Chat.", "success");
        }
      } else if (payload.type === "error") {
        showError(payload.message || "Live audio failed.");
        if (!liveActive) {
          stopCapture().catch(() => {});
          closeLiveSocket();
          startLiveButton.disabled = !voiceConnected;
        }
      }
    };
    liveSocket.onerror = () => {
      showError("The live audio connection failed.");
    };
    liveSocket.onclose = () => {
      liveSocket = null;
      liveActive = false;
      stopCapture().catch(() => {});
      stopReceivePlayback().catch(() => {});
      setLiveControlsEnabled(false);
      startLiveButton.disabled = !voiceConnected;
      stopLiveButton.disabled = true;
      setText("live-status", "Stopped");
    };
  }

  async function stopLive() {
    sendCommand({ type: "stop" });
    await stopCapture();
    await stopReceivePlayback();
    closeLiveSocket();
    liveActive = false;
    setLiveControlsEnabled(false);
    startLiveButton.disabled = !voiceConnected;
    stopLiveButton.disabled = true;
    setText("live-status", "Stopped");
    setNotice("Live microphone stopped.", "success");
  }

  function updateControlsOnServer() {
    syncControls();
    updateMicUi();
    updatePttUi();
  }

  async function authenticate() {
    retry.hidden = true;
    refreshState.classList.remove("paused");
    if (!tg || !tg.initData) {
      showError("Open this page from the Telegram bot to continue.");
      return;
    }

    setNotice("Authenticating with Telegram…");
    const response = await fetch("api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData: tg.initData }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      voiceConnected = false;
      startLiveButton.disabled = true;
      if (payload.code === "not_hosted") {
        showError("No active hosted account was found. Use /host in the control bot first.");
      } else {
        showError(payload.message || "Telegram authentication failed.");
      }
      return;
    }
    ticket = payload.ticket;
    setNotice("Securely connected.", "success");
    await refreshStatus();
    if (!statusTimer) statusTimer = window.setInterval(() => refreshStatus().catch(() => {}), 5000);
  }

  async function refreshStatus() {
    const response = await fetch("api/status", {
      headers: { Authorization: `Bearer ${ticket}` },
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      if (payload.code === "not_hosted") {
        showError("Your hosted Telegram account is no longer active. Use /host in the control bot first.");
        return;
      }
      showError(payload.message || "The authorization expired. Try again.");
      return;
    }

    const account = payload.hosted && payload.session_active;
    setText("account-status", account ? "Connected" : "Offline");
    setText("account-detail", account ? `Telegram ID ${payload.telegram_user_id}` : "Hosted session unavailable");
    setState(payload);
  }

  micToggle.addEventListener("click", () => {
    micEnabled = !micEnabled;
    updateControlsOnServer();
  });

  pttToggle.addEventListener("click", () => {
    pushToTalk = !pushToTalk;
    pushActive = false;
    updateControlsOnServer();
  });

  function setPushActive(active) {
    if (!pushToTalk || !liveActive) return;
    pushActive = active;
    sendCommand({ type: "controls", push_active: pushActive });
    updatePttUi();
  }

  talkButton.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    talkButton.setPointerCapture?.(event.pointerId);
    setPushActive(true);
  });
  talkButton.addEventListener("pointerup", () => setPushActive(false));
  talkButton.addEventListener("pointercancel", () => setPushActive(false));
  talkButton.addEventListener("lostpointercapture", () => setPushActive(false));

  gainInput.addEventListener("input", () => {
    gainValue.textContent = gainInput.value || "0";
  });
  gainInput.addEventListener("change", () => {
    setGain(gainInput.value, true);
  });
  gainPresets.forEach((button) => {
    button.addEventListener("click", () => setGain(button.dataset.gain, true));
  });

  startLiveButton.addEventListener("click", () => startLive().catch((error) => {
    showError(error.message || "Could not start live audio.");
  }));
  stopLiveButton.addEventListener("click", () => stopLive().catch(() => {}));
  retry.addEventListener("click", () => authenticate().catch(() => showError("Could not reach the Mini App server.")));

  updateMicUi();
  updatePttUi();
  authenticate().catch(() => showError("Could not reach the Mini App server."));
})();