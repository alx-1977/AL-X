const begin = document.querySelector("#begin");
const activation = document.querySelector("#activation");
const status = document.querySelector("#status");
const diagnosticLog = document.querySelector("#diagnostic-log");
const diagnosticStage = document.querySelector("#diagnostic-stage");
const diagnosticElapsed = document.querySelector("#diagnostic-elapsed");
const diagnosticClear = document.querySelector("#diagnostic-clear");
const phaseLabels = {
  listening: "Listening",
  hearing: "I hear you",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Something interrupted me",
  disconnected: "Disconnected",
};

let socket;
let sending = false;
let playbackActive = false;
let pendingListening = false;
let audioParts = [];
let microphone;
let stageStartedAt = performance.now();
let firstAudioSent = false;
let firstAudioReceived = false;
let heardThisTurn = false;
let audioByteCount = 0;
let audioChunkCount = 0;
let ttsStartedAt;

function clockTime() {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

function diagnostic(message, tone = "info") {
  const line = document.createElement("div");
  line.className = "diagnostic-line";
  line.dataset.tone = tone;
  const timestamp = document.createElement("time");
  timestamp.textContent = clockTime();
  const content = document.createElement("span");
  content.textContent = message;
  line.append(timestamp, content);
  diagnosticLog.append(line);
  while (diagnosticLog.childElementCount > 80) diagnosticLog.firstElementChild.remove();
  diagnosticLog.scrollTop = diagnosticLog.scrollHeight;
}

function beginDiagnosticStage(label) {
  diagnosticStage.textContent = label;
  stageStartedAt = performance.now();
}

function elapsedText(milliseconds) {
  const totalSeconds = Math.max(0, milliseconds / 1000);
  const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, "0");
  const seconds = (totalSeconds % 60).toFixed(1).padStart(4, "0");
  return `${minutes}:${seconds}`;
}

function ttsElapsed() {
  if (ttsStartedAt === undefined) return "0.00 s";
  return `${((performance.now() - ttsStartedAt) / 1000).toFixed(2)} s`;
}

setInterval(() => {
  diagnosticElapsed.textContent = elapsedText(performance.now() - stageStartedAt);
}, 100);

diagnosticClear.addEventListener("click", () => {
  diagnosticLog.replaceChildren();
  diagnostic("Diagnostic display cleared");
});

diagnostic("Interface loaded", "ok");

function setPhase(phase) {
  document.body.dataset.phase = phase;
  status.textContent = phaseLabels[phase] ?? "AL/X";
}

function conversationId() {
  const stored = localStorage.getItem("alx.conversation_id");
  return stored ?? "";
}

async function acquireMicrophone() {
  beginDiagnosticStage("Requesting microphone access");
  diagnostic("Requesting browser microphone permission", "active");
  const context = new AudioContext();
  await context.resume();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  diagnostic(`Microphone access granted · browser audio ${context.sampleRate} Hz`, "ok");
  return { context, stream };
}

async function connectMicrophone(targetSampleRate) {
  if (!microphone) throw new Error("microphone is not active");
  const { context, stream } = microphone;
  await context.audioWorklet.addModule("/pcm-worklet.js");
  const source = context.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(context, "alx-pcm-capture", {
    processorOptions: { targetSampleRate },
  });
  const silent = context.createGain();
  silent.gain.value = 0;
  capture.port.onmessage = ({ data }) => {
    if (sending && socket?.readyState === WebSocket.OPEN) {
      socket.send(data);
      if (!firstAudioSent) {
        firstAudioSent = true;
        diagnostic("First microphone frame sent to AL/X", "active");
      }
    }
  };
  source.connect(capture).connect(silent).connect(context.destination);
  diagnostic(`Audio capture online · PCM ${targetSampleRate} Hz`, "ok");
}

async function releaseMicrophone() {
  if (!microphone) return;
  microphone.stream.getTracks().forEach((track) => track.stop());
  await microphone.context.close();
  microphone = undefined;
}

async function playResponse(mediaType) {
  playbackActive = true;
  sending = false;
  setPhase("speaking");
  const url = URL.createObjectURL(new Blob(audioParts, { type: mediaType }));
  audioParts = [];
  const audio = new Audio();
  audio.preload = "auto";
  const ready = new Promise((resolve, reject) => {
    audio.addEventListener("canplay", resolve, { once: true });
    audio.addEventListener("error", reject, { once: true });
  });
  audio.src = url;
  audio.load();
  try {
    await ready;
  } catch (error) {
    URL.revokeObjectURL(url);
    throw error;
  }
  diagnostic(`Browser audio buffer ready · ${ttsElapsed()}`, "ok");
  beginDiagnosticStage("Playing synthesized response");
  audio.onended = () => {
    URL.revokeObjectURL(url);
    playbackActive = false;
    pendingListening = false;
    sending = true;
    setPhase("listening");
    beginDiagnosticStage("Listening");
    diagnostic("Playback completed; microphone resumed", "ok");
    heardThisTurn = false;
  };
  audio.onerror = () => {
    URL.revokeObjectURL(url);
    playbackActive = false;
    setPhase("error");
    beginDiagnosticStage("Playback error");
    diagnostic("Browser could not play synthesized audio", "error");
  };
  await audio.play();
  diagnostic(`Playback started · ${ttsElapsed()} · ${audioChunkCount} chunks · ${audioByteCount} bytes`, "active");
}

function handleControl(message) {
  if (message.type === "session.ready") {
    diagnostic("Voice transport connected; session accepted", "ok");
    localStorage.setItem("alx.conversation_id", message.conversation_id);
    connectMicrophone(message.sample_rate_hz)
      .then(() => {
        sending = true;
        setPhase("listening");
        beginDiagnosticStage("Listening");
        diagnostic("AL/X is listening", "ok");
      })
      .catch((error) => {
        setPhase("error");
        beginDiagnosticStage("Audio capture error");
        diagnostic(`Audio capture failed · ${error.name}`, "error");
      });
    return;
  }
  if (message.type === "diagnostic") {
    if (message.code === "microphone.audio_received") {
      diagnostic("AL/X server received microphone audio", "ok");
    } else if (message.code === "reasoning.completed") {
      const seconds = (value) => `${(Number(value ?? 0) / 1000).toFixed(2)} s`;
      const effort = message.reasoning_effort
        ? ` · ${message.reasoning_effort} reasoning`
        : "";
      diagnostic(
        `Reasoning completed · ${message.model} · ${message.service_tier} tier${effort}`,
        "ok",
      );
      diagnostic(
        `Timing · first event ${seconds(message.first_event_ms)} · first answer ${seconds(message.first_content_ms)} · generation ${seconds(message.answer_generation_ms)} · total ${seconds(message.duration_ms)}`,
      );
      diagnostic(
        `Tokens · input ${message.input_tokens ?? 0} · cached ${message.cached_tokens ?? 0} · reasoning ${message.reasoning_tokens ?? 0} · output ${message.output_tokens ?? 0} · total ${message.total_tokens ?? 0}`,
      );
    } else if (message.code === "reasoning.failed") {
      diagnostic(
        `Reasoning provider failed after ${(Number(message.duration_ms ?? 0) / 1000).toFixed(2)} s · ${message.error_type ?? "unknown"}`,
        "error",
      );
    } else if (message.code === "tts.request_sent") {
      diagnostic(`TTS request sent · ${(Number(message.elapsed_ms ?? 0) / 1000).toFixed(2)} s`, "active");
    } else if (message.code === "tts.text_sent") {
      diagnostic(`First text sent · ${(Number(message.elapsed_ms ?? 0) / 1000).toFixed(2)} s`, "active");
    } else if (message.code === "tts.stream_connected") {
      const transport = message.transport === "websocket" ? "WebSocket" : "HTTP stream";
      diagnostic(`TTS ${transport} connected · ${(Number(message.elapsed_ms ?? 0) / 1000).toFixed(2)} s`, "ok");
    } else if (message.code === "tts.first_audio_byte") {
      diagnostic(`First audio byte received from ElevenLabs · ${(Number(message.elapsed_ms ?? 0) / 1000).toFixed(2)} s`, "ok");
    } else {
      diagnostic(`Server diagnostic · ${message.code ?? "unknown"}`);
    }
    return;
  }
  if (message.type === "audio.end") {
    diagnostic(`Speech synthesis stream completed · ${ttsElapsed()}`, "ok");
    playResponse(message.media_type).catch(() => setPhase("error"));
    return;
  }
  if (message.type !== "phase") return;
  if (message.value === "hearing" && !heardThisTurn) {
    heardThisTurn = true;
    beginDiagnosticStage("Transcribing speech");
    diagnostic("Speech detected; transcription in progress", "active");
  }
  if (message.value === "thinking") {
    beginDiagnosticStage("Core reasoning");
    diagnostic("Final transcription received", "ok");
    diagnostic("Authoritative Core reasoning in progress", "active");
  }
  if (message.value === "speaking") {
    audioByteCount = 0;
    audioChunkCount = 0;
    firstAudioReceived = false;
    ttsStartedAt = performance.now();
    beginDiagnosticStage("Synthesizing response");
    diagnostic("Core response accepted; speech synthesis started", "active");
  }
  if (message.value === "error") {
    beginDiagnosticStage("Voice pipeline stopped");
    diagnostic(`Pipeline error · ${message.reason ?? "unknown_error"}`, "error");
  }
  if (message.value === "thinking" || message.value === "speaking") sending = false;
  if (message.value === "speaking") audioParts = [];
  if (message.value === "listening" && playbackActive) {
    pendingListening = true;
    return;
  }
  if (message.value === "listening") {
    sending = true;
    heardThisTurn = false;
    beginDiagnosticStage("Listening");
    diagnostic("Pipeline recovered; AL/X is listening", "ok");
  }
  setPhase(message.value);
}

begin.addEventListener("click", async () => {
  activation.hidden = true;
  setPhase("listening");
  try {
    microphone = await acquireMicrophone();
  } catch (error) {
    setPhase("error");
    beginDiagnosticStage("Microphone unavailable");
    diagnostic(`Microphone access failed · ${error.name}`, "error");
    activation.hidden = false;
    return;
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const id = encodeURIComponent(conversationId());
  beginDiagnosticStage("Connecting voice transport");
  diagnostic("Opening local voice connection", "active");
  socket = new WebSocket(`${scheme}://${location.host}/voice?conversation_id=${id}`);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => diagnostic("Browser WebSocket opened", "ok");
  socket.onmessage = ({ data }) => {
    if (data instanceof ArrayBuffer) {
      audioParts.push(data);
      audioChunkCount += 1;
      audioByteCount += data.byteLength;
      if (!firstAudioReceived) {
        firstAudioReceived = true;
        diagnostic(`First audio byte received by browser · ${ttsElapsed()}`, "ok");
      }
      return;
    }
    handleControl(JSON.parse(data));
  };
  socket.onerror = () => {
    setPhase("error");
    beginDiagnosticStage("Connection error");
    diagnostic("Voice WebSocket reported a connection error", "error");
  };
  socket.onclose = () => {
    sending = false;
    releaseMicrophone().catch(() => {});
    setPhase("disconnected");
    activation.hidden = false;
    status.textContent = phaseLabels.disconnected;
    beginDiagnosticStage("Disconnected");
    diagnostic("Voice transport closed", "error");
  };
});
