const begin = document.querySelector("#begin");
const activation = document.querySelector("#activation");
const status = document.querySelector("#status");
const diagnosticLog = document.querySelector("#diagnostic-log");
const consoleForm = document.querySelector("#console-form");
const consoleInput = document.querySelector("#console-input");
const diagnosticStage = document.querySelector("#diagnostic-stage");
const diagnosticElapsed = document.querySelector("#diagnostic-elapsed");
const diagnosticClear = document.querySelector("#diagnostic-clear");
const taskRow = document.querySelector("#task-row");
const taskLabel = document.querySelector("#task-label");
const taskElapsed = document.querySelector("#task-elapsed");
// Law 1: these name a system state and nothing more. First-person or
// user-directed wording here reads as AL/X speaking when she has not reasoned,
// so the gate whitelists exactly these labels.
const phaseLabels = {
  ready: "Ready",
  listening: "Listening",
  hearing: "Hearing",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Error",
  disconnected: "Disconnected",
};

let socket;
let sending = false;
// True while the drainer holds the voice. Read as a guard, not just written.
let playbackActive = false;
let pendingListening = false;
let audioParts = [];
// Sealed utterances waiting their turn. One voice, so they queue rather than
// competing; each is already the Core's own wording, in the order it decided.
let playbackQueue = [];
let microphone;
let stageStartedAt = performance.now();
let firstAudioSent = false;
let firstAudioReceived = false;
let heardThisTurn = false;
let audioByteCount = 0;
let audioChunkCount = 0;
let ttsStartedAt;
// The running external task, or null. The server ticks slowly and Core may be
// idle for the whole wait, so the row keeps its own clock from the elapsed
// figure the last tick reported: `at` is when that figure was true locally.
let runningTask = null;

function clockTime() {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

// A line carries two independent labels. `stream` says where the data came
// from; `tone` says how it reads. Future SERIAL or BUILD sources are new
// stream values, not a new renderer — and neither label ever decides where
// keyboard input goes.
function diagnostic(message, tone = "info", stream = "SYSTEM") {
  const line = document.createElement("div");
  line.className = "diagnostic-line";
  line.dataset.tone = tone;
  line.dataset.stream = stream;
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
  paintTask();
}, 100);

function taskClock(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60).toString().padStart(2, "0")}:${(whole % 60)
    .toString()
    .padStart(2, "0")}`;
}

// Identifiers, a state and a duration. Nothing a reviewer said reaches here,
// because nothing a reviewer said reaches the browser.
const taskStates = {
  requested: "Requested",
  waiting_for_result: "Running",
  status_unknown: "Status unknown",
  completed: "Completed",
  failed: "Failed",
};

function paintTask() {
  if (runningTask === null) {
    taskRow.hidden = true;
    return;
  }
  const drift = (performance.now() - runningTask.at) / 1000;
  // A settled task stops counting: its elapsed time is a fact about how long
  // it took, not a clock that keeps running.
  const seconds = runningTask.settled
    ? runningTask.seconds
    : runningTask.seconds + drift;
  const state = taskStates[runningTask.state] ?? runningTask.state;
  taskRow.dataset.state = runningTask.state;
  taskLabel.textContent = `${state} · ${runningTask.service} · ${runningTask.subject}`;
  taskElapsed.textContent = taskClock(seconds);
  taskRow.hidden = false;
}

function showTask(message) {
  const state = String(message.state ?? "");
  runningTask = {
    state,
    service: String(message.service ?? ""),
    subject: String(message.subject ?? ""),
    seconds: Number(message.elapsed_seconds ?? 0),
    at: performance.now(),
    settled: state === "completed" || state === "failed",
  };
  paintTask();
}

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

// AL/X has one voice, so at most one utterance is ever audible. The server
// already serialises generation: every source — speech, typed, mail and other
// background events, autonomous turns — funnels through one queue and one
// synthesis path there. But `audio.end` announces that a stream finished
// *arriving*, not that it finished *playing*, so an event whose turn ran while
// the previous blob was still audible used to start a second Audio element
// over the top of it. Two elements, one output device, two AL/X voices.
//
// Sealed here instead: each utterance's chunks become a blob at `audio.end`,
// the blob waits its turn, and one drainer plays them strictly in order. This
// preserves the ordering the server already decided rather than inventing one,
// and nothing is dropped — a queued utterance is delayed by exactly the length
// of the one ahead of it.
function enqueueUtterance(mediaType) {
  // Sealed at arrival. Clearing the shared buffer inside playback let a second
  // stream's chunks land in `audioParts` before the first had taken them,
  // merging two utterances into one blob.
  if (!audioParts.length) return;
  playbackQueue.push(new Blob(audioParts, { type: mediaType }));
  audioParts = [];
  void drainPlaybackQueue();
}

async function drainPlaybackQueue() {
  // The guard that makes this a queue rather than a race. `playbackActive` was
  // already being set and cleared; it was simply never read, so every arrival
  // started a new element regardless of what was already sounding.
  if (playbackActive) return;
  playbackActive = true;
  sending = false;
  try {
    while (playbackQueue.length) {
      // Shifted before playing, so a blob that fails is already out of the
      // queue and cannot be retried forever.
      await playOneUtterance(playbackQueue.shift());
    }
  } finally {
    // Reached however the last utterance ended — completed, failed, refused or
    // throwing — so playback can never be left permanently stuck.
    playbackActive = false;
    pendingListening = false;
    sending = true;
    setPhase("listening");
    beginDiagnosticStage("Listening");
    heardThisTurn = false;
  }
}

// Resolves when this utterance stops being audible, for any reason. It never
// rejects: a failed utterance must not prevent the queue behind it from
// playing, so the failure is reported and the drainer continues.
function playOneUtterance(blob) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio();
    let settled = false;
    const finish = (message, tone) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      diagnostic(message, tone);
      resolve();
    };
    audio.onended = () => finish("Playback completed", "ok");
    audio.onerror = () => finish("Browser playback failed; continuing", "error");
    audio.preload = "auto";
    audio.addEventListener(
      "canplay",
      () => {
        diagnostic(`Browser audio buffer ready · ${ttsElapsed()}`, "ok");
        beginDiagnosticStage("Playing synthesized response");
        setPhase("speaking");
        audio.play().then(
          () => diagnostic(
            `Playback started · ${ttsElapsed()} · ${audioChunkCount} chunks · ${audioByteCount} bytes`,
            "active",
          ),
          () => finish("Browser refused synthesized audio; continuing", "error"),
        );
      },
      { once: true },
    );
    audio.addEventListener(
      "error",
      () => finish("Browser could not prepare synthesized audio; continuing", "error"),
      { once: true },
    );
    try {
      audio.src = url;
      audio.load();
    } catch (error) {
      finish("Browser could not load synthesized audio; continuing", "error");
    }
  });
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
  if (message.type === "alx.text") {
    diagnostic(`ALX > ${message.content}`, "ok", message.stream || "ALX");
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
      // 402/403 from a reasoning provider means credit exhausted or the key
      // refused. Naming the condition is a technical diagnostic, not AL/X
      // speaking: without it a spent account looks like an unexplained hang.
      const status = Number(message.status_code ?? 0);
      const cause =
        status === 402 || status === 403
          ? " · provider rejected the key: credit or spending limit"
          : "";
      diagnostic(
        `Reasoning provider failed after ${(Number(message.duration_ms ?? 0) / 1000).toFixed(2)} s · ${message.error_type ?? "unknown"}${cause}`,
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
    } else if (message.code === "task.status") {
      // A live row rather than a log line: an outstanding task is a state the
      // console should show, not an event that scrolls away.
      showTask(message);
    } else {
      diagnostic(`Server diagnostic · ${message.code ?? "unknown"}`);
    }
    return;
  }
  if (message.type === "audio.end") {
    diagnostic(`Speech synthesis stream completed · ${ttsElapsed()}`, "ok");
    enqueueUtterance(message.media_type);
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
  // Not cleared here any more. The buffer is sealed and emptied at
  // `audio.end`, so clearing on `speaking` would discard chunks of an
  // utterance that is still arriving.
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

// --- typed input ----------------------------------------------------------
//
// The keyboard's destination is fixed here, in code, and is never read out of
// what was typed. There is deliberately no command grammar: a line beginning
// with a slash is a line beginning with a slash, and it reaches AL/X verbatim.
// When a second input target exists it will be an explicit selection, not a
// prefix the console interprets.
const typedHistory = [];
let historyCursor = 0;

function submitTypedLine() {
  const content = consoleInput.value.trim();
  if (!content) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    diagnostic("Not connected · start AL/X first", "error");
    return;
  }
  socket.send(JSON.stringify({ type: "person.text", content }));
  diagnostic(`You > ${content}`, "info", "ALX");
  typedHistory.push(content);
  historyCursor = typedHistory.length;
  consoleInput.value = "";
  consoleInput.style.height = "auto";
}

consoleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitTypedLine();
});

consoleInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitTypedLine();
    return;
  }
  if (event.key === "ArrowUp" && !consoleInput.value.includes("\n")) {
    if (!typedHistory.length) return;
    event.preventDefault();
    historyCursor = Math.max(0, historyCursor - 1);
    consoleInput.value = typedHistory[historyCursor] ?? "";
    return;
  }
  if (event.key === "ArrowDown" && !consoleInput.value.includes("\n")) {
    if (!typedHistory.length) return;
    event.preventDefault();
    historyCursor = Math.min(typedHistory.length, historyCursor + 1);
    consoleInput.value = typedHistory[historyCursor] ?? "";
  }
});

// Grow with multiline input, within the height the panel allows.
consoleInput.addEventListener("input", () => {
  consoleInput.style.height = "auto";
  consoleInput.style.height = `${consoleInput.scrollHeight}px`;
});
