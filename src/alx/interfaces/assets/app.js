const begin = document.querySelector("#begin");
const activation = document.querySelector("#activation");
const status = document.querySelector("#status");
const diagnosticLog = document.querySelector("#diagnostic-log");
const consoleForm = document.querySelector("#console-form");
const consoleInput = document.querySelector("#console-input");
const diagnosticStage = document.querySelector("#diagnostic-stage");
const diagnosticElapsed = document.querySelector("#diagnostic-elapsed");
const diagnosticClear = document.querySelector("#diagnostic-clear");
const diagnosticDetails = document.querySelector("#diagnostic-details");
const taskRows = document.querySelector("#task-rows");
const codingCancel = document.querySelector("#coding-cancel");
let activeCodingJobId = "";
let codingCancelTimeout;
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
const activityLabels = {
  reasoning: "Reasoning",
  coding: "Coding",
  reviewing: "Reviewing",
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
let heardThisTurn = false;
// The utterance whose audio is arriving now. Its counts and clock travel with
// its blob into the playback queue: shared counters were reset by the next
// utterance's synthesis, so a queued reply reported another reply's timing.
let arriving;
let lastCodingTransition = "";
// Each external task keeps its own clock. A single global row caused one
// concurrent review to overwrite another and made the display untrue.
const runningTasks = new Map();
// Plan attentions whose automatic offers are exhausted, by goal, plan and
// attention. Structural state: shown until the runtime says it is resolved.
const blockedPlans = new Map();
const terminalTaskRetentionMilliseconds = 10_000;

const clockFormat = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

// The server stamps each event when it happens. Using that, rather than when
// the browser happened to receive it, is what keeps a line's time true: a
// late burst used to show one second for two minutes of work.
function eventDate(at) {
  const parsed = at ? new Date(at) : undefined;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed : new Date();
}

// A line carries independent labels. `stream` says where the data came from;
// `tone` says how it reads; `subsystem` names the part of AL/X it concerns;
// `detail` marks low-level telemetry shown only under Details. None of them
// ever decides where keyboard input goes.
function diagnostic(message, tone = "info", stream = "SYSTEM", options = {}) {
  const line = document.createElement("div");
  line.className = "diagnostic-line";
  line.dataset.tone = tone;
  line.dataset.stream = stream;
  if (options.detail) line.dataset.detail = "true";
  if (options.background) line.dataset.background = "true";
  const when = eventDate(options.at);
  const timestamp = document.createElement("time");
  timestamp.dateTime = when.toISOString();
  timestamp.textContent = clockFormat.format(when);
  const subsystem = document.createElement("b");
  subsystem.textContent = options.subsystem ?? "";
  const content = document.createElement("span");
  content.textContent = message;
  line.append(timestamp, subsystem, content);
  diagnosticLog.append(line);
  while (diagnosticLog.childElementCount > 240) diagnosticLog.firstElementChild.remove();
  diagnosticLog.scrollTop = diagnosticLog.scrollHeight;
}

// Low-level telemetry: kept, but behind Details.
function detail(message, tone = "info", options = {}) {
  diagnostic(message, tone, "SYSTEM", { subsystem: "DIAG", ...options, detail: true });
}

function showDetails(shown) {
  diagnosticLog.dataset.details = shown ? "shown" : "hidden";
  diagnosticDetails.setAttribute("aria-pressed", shown ? "true" : "false");
  diagnosticLog.scrollTop = diagnosticLog.scrollHeight;
  try {
    localStorage.setItem("alx.trace_details", shown ? "shown" : "hidden");
  } catch (error) {
    // A remembered preference only; the toggle still works without it.
  }
}

diagnosticDetails.addEventListener("click", () => {
  showDetails(diagnosticLog.dataset.details !== "shown");
});

try {
  showDetails(localStorage.getItem("alx.trace_details") === "shown");
} catch (error) {
  showDetails(false);
}

// A measured duration, or an honest absence. A provider that does not report
// a figure is shown as not reporting it, never as zero.
function seconds(milliseconds) {
  const value = Number(milliseconds);
  return milliseconds === undefined || milliseconds === null || !Number.isFinite(value)
    ? "not reported"
    : `${(value / 1000).toFixed(2)} s`;
}

const purposeLabels = {
  interpreting_request: "Interpreting request",
  assessing_event: "Assessing external event",
  reviewing_completed_work: "Reviewing completed work",
  revisiting_follow_up: "Revisiting requested follow-up",
  evaluating_plan: "Evaluating plan progress",
  evaluating_goal_state: "Evaluating goal state",
  reviewing_result: "Reviewing result",
  reviewing_memories: "Reviewing retrieved memories",
  reconsidering_refusal: "Reconsidering after refusal",
  correcting_decision: "Correcting rejected decision",
  continuing_work: "Continuing remaining work",
  preparing_response: "Preparing response",
  deciding_next_step: "Deciding next step",
};

function reasoningTokens(message) {
  if (message.usage_measured !== true) return "Tokens · not reported by provider";
  const count = (value) => (Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "not reported");
  return `Tokens · input ${count(message.input_tokens)} · cached ${count(message.cached_tokens)} · cache write ${count(message.cache_write_tokens)} · output ${count(message.output_tokens)} · reasoning ${count(message.reasoning_tokens)} · total ${count(message.total_tokens)}`;
}

const traceTones = {
  started: "active", waiting: "active", completed: "ok", info: "info",
  refused: "error", failed: "error",
};

// One operator step: which subsystem is active and what it is doing.
function showTrace(message) {
  const parts = [message.label];
  if (message.reference && message.reference !== message.label) parts.push(message.reference);
  if (message.count !== undefined) parts.push(`${message.count} steps`);
  if (message.duration_ms !== undefined) parts.push(seconds(message.duration_ms));
  if (message.reason_code) parts.push(message.reason_code);
  const subsystem = String(message.subsystem ?? "").toUpperCase();
  diagnostic(parts.join(" · "), traceTones[message.status] ?? "info", "SYSTEM", {
    subsystem, at: message.at, background: message.background === true,
  });
  // The stage bar names the step now running, with its own clock, so a long
  // model call reads as the work it is rather than as silence.
  if (["started", "waiting"].includes(message.status) && message.background !== true) {
    beginDiagnosticStage(`${subsystem} · ${message.label}`, message.at);
  }
}

// The foreground stage: what the conversation itself is doing. It is shown
// only when no background task is running; see renderStage.
let foregroundStage = "Interface ready";

function beginDiagnosticStage(label, at) {
  foregroundStage = label;
  // Measured from when the step began on the server, not from arrival.
  const lag = at ? Math.max(0, Date.now() - eventDate(at).getTime()) : 0;
  stageStartedAt = performance.now() - lag;
  renderStage();
}

// The background coding job, from its own telemetry. Its clocks are the
// server's timestamps, so they keep running between observations.
let backgroundJob;

const codingStageLabels = {
  plan: "Planning",
  execution: "Coding",
  correction: "Correcting",
  review: "Local review",
  verify: "Verifying",
  test: "Testing",
  commit: "Committing",
};

function backgroundStatus() {
  if (backgroundJob) return backgroundJob;
  // A running external task, in the order the rows were added.
  for (const task of runningTasks.values()) {
    if (task.settled) continue;
    const runtime = (task.seconds * 1000) + (performance.now() - task.at);
    return {
      label: task.service ? `${task.service} review` : "External task",
      startedAt: Date.now() - runtime,
      lastActivityAt: task.lastActivityAt,
    };
  }
  return undefined;
}

// One place decides the bar. While background work exists it holds the bar:
// speaking, listening and console events change the foreground stage
// underneath it but never replace it. Only actual task activity moves the
// "last activity" clock.
function renderStage() {
  const background = backgroundStatus();
  if (background) {
    const now = Date.now();
    diagnosticStage.textContent =
      `${background.label} · ${taskClock((now - background.startedAt) / 1000)}` +
      ` · last activity ${taskClock((now - background.lastActivityAt) / 1000)}`;
    diagnosticElapsed.textContent = "";
    return;
  }
  diagnosticStage.textContent = foregroundStage;
  diagnosticElapsed.textContent = elapsedText(performance.now() - stageStartedAt);
}

function elapsedText(milliseconds) {
  const totalSeconds = Math.max(0, milliseconds / 1000);
  const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, "0");
  const seconds = (totalSeconds % 60).toFixed(1).padStart(4, "0");
  return `${minutes}:${seconds}`;
}

function showCodingStatus(message) {
  const nextCodingJobId = message.terminal || message.phase === "commit"
    ? "" : String(message.job_id ?? "");
  if (nextCodingJobId !== activeCodingJobId || message.terminal) {
    clearTimeout(codingCancelTimeout);
    codingCancel.disabled = false;
  }
  activeCodingJobId = nextCodingJobId;
  codingCancel.hidden = !activeCodingJobId;
  if (message.transition === "CASE started" || message.terminal) codingCancel.disabled = false;
  // The bar shows the job's stage, its runtime and the time since its last
  // real activity, and nothing else; a finished job gives the bar back.
  if (message.terminal) {
    if (!backgroundJob || backgroundJob.jobId === message.job_id) backgroundJob = undefined;
  } else {
    const phase = String(message.phase ?? "");
    backgroundJob = {
      jobId: message.job_id,
      label: codingStageLabels[phase] ?? (phase ? phase[0].toUpperCase() + phase.slice(1) : "Coding"),
      startedAt: eventDate(message.started_at).getTime(),
      lastActivityAt: eventDate(message.last_activity_at).getTime(),
    };
  }
  renderStage();
  const transition = String(message.transition ?? "");
  const key = `${message.job_id ?? ""}:${transition}`;
  if (transition && key !== lastCodingTransition) {
    diagnostic(`${message.job_id ?? "CASE"} · ${transition}`,
      message.stalled || (message.terminal && !["succeeded", "no_change_required"].includes(message.outcome))
        ? "error" : "active", "CODING", { subsystem: "CODING", at: message.at });
    lastCodingTransition = key;
  }
}

codingCancel.addEventListener("click", () => {
  if (!activeCodingJobId || !socket || socket.readyState !== WebSocket.OPEN) return;
  const requestedJobId = activeCodingJobId;
  socket.send(JSON.stringify({ type: "coding.cancel", job_id: requestedJobId }));
  codingCancel.disabled = true;
  clearTimeout(codingCancelTimeout);
  codingCancelTimeout = setTimeout(() => {
    if (activeCodingJobId === requestedJobId) codingCancel.disabled = false;
  }, 5000);
});

function sinceSynthesis(utterance) {
  return `${((performance.now() - utterance.startedAt) / 1000).toFixed(2)} s after synthesis start`;
}

setInterval(() => {
  paintTasks();
  renderStage();
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
  waiting_for_result: "Waiting for result",
  status_unknown: "Status unknown",
  completed: "Completed",
  failed: "Failed",
  observer_unavailable: "Observer unavailable",
};

function paintTasks() {
  const now = performance.now();
  for (const [taskId, task] of runningTasks.entries()) {
    if (task.settled && now >= task.expiresAt) {
      task.row.remove();
      runningTasks.delete(taskId);
      continue;
    }
    const drift = (now - task.at) / 1000;
    const seconds = task.settled ? task.seconds : task.seconds + drift;
    task.row.dataset.state = task.state;
    task.label.textContent = `${taskStates[task.state] ?? task.state} · ${task.service} · ${task.subject}`;
    task.elapsed.textContent = taskClock(seconds);
  }
  taskRows.hidden = runningTasks.size === 0 && blockedPlans.size === 0;
}

function showPlanAttention(message) {
  const key = `${message.goal_id ?? ""}:${message.plan_id ?? ""}:${message.attention_seq ?? ""}`;
  const existing = blockedPlans.get(key);
  if (message.state !== "blocked") {
    if (existing) existing.remove();
    blockedPlans.delete(key);
    paintTasks();
    return;
  }
  const row = existing ?? document.createElement("div");
  row.className = "diagnostics__task";
  row.dataset.state = "blocked";
  row.textContent = `Plan attention blocked · automatic reasoning stopped · ${message.reason ?? "unknown"} · goal ${message.goal_id ?? ""}`;
  if (!existing) {
    taskRows.append(row);
    blockedPlans.set(key, row);
  }
  paintTasks();
}

function showTask(message) {
  const taskId = String(message.task_id ?? "");
  if (!taskId) return;
  const state = String(message.state ?? "");
  let task = runningTasks.get(taskId);
  if (!task) {
    const row = document.createElement("div");
    row.className = "diagnostics__task";
    const label = document.createElement("span");
    const elapsed = document.createElement("time");
    row.append(label, elapsed);
    taskRows.append(row);
    task = { row, label, elapsed };
    runningTasks.set(taskId, task);
  }
  const now = performance.now();
  const settled = ["completed", "failed", "observer_unavailable"].includes(state);
  // A changed state is task activity; a repeated report of the same state
  // is not.
  if (task.state !== state || task.lastActivityAt === undefined) {
    task.lastActivityAt = eventDate(message.at).getTime();
  }
  Object.assign(task, {
    state,
    service: String(message.service ?? ""),
    subject: String(message.subject ?? ""),
    seconds: Number(message.elapsed_seconds ?? 0),
    at: now,
    settled,
    expiresAt: settled ? now + terminalTaskRetentionMilliseconds : undefined,
  });
  paintTasks();
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
  try {
    return localStorage.getItem("alx.conversation_id") ?? "";
  } catch (error) {
    return "";
  }
}

async function acquireMicrophone() {
  beginDiagnosticStage("Requesting microphone access");
  diagnostic("Requesting browser microphone permission", "active");
  const context = new AudioContext();
  await context.resume();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  detail(`Microphone access granted · browser audio ${context.sampleRate} Hz`, "ok", { subsystem: "VOICE" });
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
        detail("First microphone frame sent to AL/X", "active", { subsystem: "VOICE" });
      }
    }
  };
  source.connect(capture).connect(silent).connect(context.destination);
  detail(`Audio capture online · PCM ${targetSampleRate} Hz`, "ok", { subsystem: "VOICE" });
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
  // Audio is never dropped for want of its bookkeeping: without a record its
  // timing starts here, which is the earliest moment actually known.
  const utterance = arriving ?? { startedAt: performance.now(), chunks: audioParts.length, bytes: 0 };
  arriving = undefined;
  if (!audioParts.length) return;
  utterance.blob = new Blob(audioParts, { type: mediaType });
  utterance.sealedAt = performance.now();
  playbackQueue.push(utterance);
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
    diagnostic("Waiting for input", "info", "SYSTEM", { subsystem: "IDLE" });
    heardThisTurn = false;
  }
}

// Resolves when this utterance stops being audible, for any reason. It never
// rejects: a failed utterance must not prevent the queue behind it from
// playing, so the failure is reported and the drainer continues.
function playOneUtterance(utterance) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(utterance.blob);
    const audio = new Audio();
    let settled = false;
    const finish = (message, tone) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      diagnostic(message, tone, "SYSTEM", { subsystem: "VOICE" });
      resolve();
    };
    audio.onended = () => finish("Playback completed", "ok");
    audio.onerror = () => finish("Browser playback failed; continuing", "error");
    audio.preload = "auto";
    audio.addEventListener(
      "canplay",
      () => {
        // Queue wait is reported separately: a reply that waited behind
        // another was ready long before it could be heard.
        const queued = ((performance.now() - utterance.sealedAt) / 1000).toFixed(2);
        detail(`Browser audio buffer ready · ${sinceSynthesis(utterance)} · queued ${queued} s`, "ok", { subsystem: "VOICE" });
        beginDiagnosticStage("Playing synthesized response");
        setPhase("speaking");
        audio.play().then(
          () => diagnostic(
            `Playback started · ${sinceSynthesis(utterance)} · ${utterance.chunks} chunks · ${utterance.bytes} bytes`,
            "active", "SYSTEM", { subsystem: "VOICE" },
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
  if (message.type === "coding.cancel.ack") {
    if (message.job_id === activeCodingJobId && !message.accepted) {
      clearTimeout(codingCancelTimeout);
      codingCancel.disabled = false;
    }
    return;
  }
  if (message.type === "session.ready") {
    diagnostic("Voice transport connected; session accepted", "ok");
    try {
      localStorage.setItem("alx.conversation_id", message.conversation_id);
    } catch (error) {
      diagnostic("Conversation continuity is unavailable in this browser", "error");
    }
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
    diagnostic(`ALX > ${message.content}`, "ok", message.stream || "ALX", { subsystem: "AL/X" });
    return;
  }
  if (message.type === "activity") {
    const label = activityLabels[message.value];
    if (label) beginDiagnosticStage(label);
    return;
  }
  if (message.type === "diagnostic") {
    const at = message.at;
    const background = message.background === true;
    if (message.code === "trace") {
      showTrace(message);
    } else if (message.code === "coding.status") {
      showCodingStatus(message);
    } else if (message.code === "microphone.audio_received") {
      detail("AL/X server received microphone audio", "ok", { at });
    } else if (message.code === "reasoning.completed") {
      // One operator line saying which call finished and how long it took;
      // model, timing and token figures go under Details. A figure the
      // provider did not report is shown as not reported, never as zero.
      const purpose = purposeLabels[message.purpose] ?? `${message.kind ?? "model"} call`;
      diagnostic(`${purpose} · done · ${seconds(message.duration_ms)}`, "ok", "SYSTEM", {
        subsystem: message.kind === "core" || !message.kind ? "CORE" : String(message.kind).toUpperCase(),
        at, background,
      });
      const tier = message.service_tier ? ` · ${message.service_tier} tier` : "";
      const effort = message.reasoning_effort ? ` · ${message.reasoning_effort} reasoning` : "";
      detail(`Model · ${message.provider ?? "provider"} · ${message.model ?? "model not reported"}${tier}${effort}`, "ok", { at, background });
      const timings = [`wall ${seconds(message.duration_ms)}`];
      if (message.api_duration_ms !== undefined) timings.push(`API ${seconds(message.api_duration_ms)}`);
      if (message.first_event_ms !== undefined) timings.push(`first event ${seconds(message.first_event_ms)}`);
      if (message.first_content_ms !== undefined) timings.push(`first answer ${seconds(message.first_content_ms)}`);
      if (message.answer_generation_ms !== undefined) timings.push(`generation ${seconds(message.answer_generation_ms)}`);
      detail(`Timing · ${timings.join(" · ")}`, "info", { at, background });
      detail(reasoningTokens(message), "info", { at, background });
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
        `Reasoning provider failed after ${seconds(message.duration_ms)} · ${message.error_code ?? message.error_type ?? "unknown"}${cause}`,
        "error", "SYSTEM", { subsystem: "CORE", at, background },
      );
    } else if (message.code === "autonomous.reasoning_disabled") {
      diagnostic("External event skipped · autonomous reasoning disabled", "info", "SYSTEM", { subsystem: "CORE", at });
    } else if (message.code === "core.checkpointed") {
      diagnostic(`Turn checkpointed · ${message.reason ?? "checkpointed"}`, "info", "SYSTEM", { subsystem: "CORE", at });
    } else if (message.code === "tts.request_sent") {
      detail(`TTS request sent · ${seconds(message.elapsed_ms)}`, "active", { subsystem: "VOICE", at });
    } else if (message.code === "tts.stream_connected") {
      const transport = message.transport === "websocket" ? "WebSocket" : "HTTP stream";
      detail(`TTS ${transport} connected · ${seconds(message.elapsed_ms)}`, "ok", { subsystem: "VOICE", at });
    } else if (message.code === "tts.first_audio_byte") {
      detail(`First audio byte received from ElevenLabs · ${seconds(message.elapsed_ms)}`, "ok", { subsystem: "VOICE", at });
    } else if (message.code === "plan.attention") {
      // A live row, like a task: work that is waiting for AL/X is a state.
      showPlanAttention(message);
    } else if (message.code === "task.status") {
      // A live row rather than a log line: an outstanding task is a state the
      // console should show, not an event that scrolls away.
      showTask(message);
    } else {
      detail(`Server diagnostic · ${message.code ?? "unknown"}`, "info", { at });
    }
    return;
  }
  if (message.type === "audio.end") {
    if (arriving) detail(`Speech synthesis stream completed · ${sinceSynthesis(arriving)}`, "ok", { subsystem: "VOICE" });
    enqueueUtterance(message.media_type);
    return;
  }
  if (message.type !== "phase") return;
  if (message.value === "hearing" && !heardThisTurn) {
    heardThisTurn = true;
    beginDiagnosticStage("Transcribing speech");
    diagnostic("Speech detected · transcribing", "active", "SYSTEM", { subsystem: "VOICE" });
  }
  if (message.value === "thinking") {
    beginDiagnosticStage("CORE · Request received");
    if (message.input_origin === "speech_transcript") {
      diagnostic("Final transcription received", "ok", "SYSTEM", { subsystem: "VOICE" });
    }
    diagnostic("Request received", "active", "SYSTEM", { subsystem: "CORE" });
  }
  if (message.value === "speaking") {
    // A new utterance begins arriving. Its own record, sealed with its blob.
    arriving = { startedAt: performance.now(), chunks: 0, bytes: 0, firstByte: false };
    beginDiagnosticStage("Synthesizing response");
    diagnostic("Speech synthesis started", "active", "SYSTEM", { subsystem: "VOICE" });
  }
  if (message.value === "error") {
    beginDiagnosticStage("Voice pipeline stopped");
    diagnostic(`Pipeline error · ${message.reason ?? "unknown_error"}`, "error", "SYSTEM", { subsystem: "VOICE" });
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
    diagnostic("Waiting for input", "info", "SYSTEM", { subsystem: "IDLE" });
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
  socket.onopen = () => detail("Browser WebSocket opened", "ok", { subsystem: "VOICE" });
  socket.onmessage = ({ data }) => {
    if (data instanceof ArrayBuffer) {
      audioParts.push(data);
      if (arriving) {
        arriving.chunks += 1;
        arriving.bytes += data.byteLength;
        if (!arriving.firstByte) {
          arriving.firstByte = true;
          detail(`First audio byte received by browser · ${sinceSynthesis(arriving)}`, "ok", { subsystem: "VOICE" });
        }
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
    clearTimeout(codingCancelTimeout);
    activeCodingJobId = "";
    codingCancel.hidden = true;
    codingCancel.disabled = false;
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
  diagnostic(`You > ${content}`, "info", "ALX", { subsystem: "YOU" });
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
