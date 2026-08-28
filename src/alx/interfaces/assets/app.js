const begin = document.querySelector("#begin");
const status = document.querySelector("#status");
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

function setPhase(phase) {
  document.body.dataset.phase = phase;
  status.textContent = phaseLabels[phase] ?? "AL/X";
}

function conversationId() {
  const stored = localStorage.getItem("alx.conversation_id");
  return stored ?? "";
}

async function prepareMicrophone(targetSampleRate) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const context = new AudioContext();
  await context.audioWorklet.addModule("/pcm-worklet.js");
  const source = context.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(context, "alx-pcm-capture", {
    processorOptions: { targetSampleRate },
  });
  const silent = context.createGain();
  silent.gain.value = 0;
  capture.port.onmessage = ({ data }) => {
    if (sending && socket?.readyState === WebSocket.OPEN) socket.send(data);
  };
  source.connect(capture).connect(silent).connect(context.destination);
}

async function playResponse(mediaType) {
  playbackActive = true;
  sending = false;
  setPhase("speaking");
  const url = URL.createObjectURL(new Blob(audioParts, { type: mediaType }));
  audioParts = [];
  const audio = new Audio(url);
  audio.onended = () => {
    URL.revokeObjectURL(url);
    playbackActive = false;
    pendingListening = false;
    sending = true;
    setPhase("listening");
  };
  audio.onerror = () => {
    URL.revokeObjectURL(url);
    playbackActive = false;
    setPhase("error");
  };
  await audio.play();
}

function handleControl(message) {
  if (message.type === "session.ready") {
    localStorage.setItem("alx.conversation_id", message.conversation_id);
    prepareMicrophone(message.sample_rate_hz)
      .then(() => {
        sending = true;
        setPhase("listening");
      })
      .catch(() => setPhase("error"));
    return;
  }
  if (message.type === "audio.end") {
    playResponse(message.media_type).catch(() => setPhase("error"));
    return;
  }
  if (message.type !== "phase") return;
  if (message.value === "thinking" || message.value === "speaking") sending = false;
  if (message.value === "speaking") audioParts = [];
  if (message.value === "listening" && playbackActive) {
    pendingListening = true;
    return;
  }
  setPhase(message.value);
}

begin.addEventListener("click", () => {
  begin.hidden = true;
  setPhase("listening");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const id = encodeURIComponent(conversationId());
  socket = new WebSocket(`${scheme}://${location.host}/voice?conversation_id=${id}`);
  socket.binaryType = "arraybuffer";
  socket.onmessage = ({ data }) => {
    if (data instanceof ArrayBuffer) {
      audioParts.push(data);
      return;
    }
    handleControl(JSON.parse(data));
  };
  socket.onerror = () => setPhase("error");
  socket.onclose = () => {
    sending = false;
    setPhase("disconnected");
    begin.hidden = false;
  };
});
