import WebApp from "@twa-dev/sdk";

// ── Config ────────────────────────────────────────────────────────────────────

const VOICE_SERVER_URL = (import.meta.env.VITE_VOICE_SERVER_URL as string) || "wss://localhost:8765";

// ── Telegram init ─────────────────────────────────────────────────────────────

WebApp.ready();
WebApp.expand();

const chatId = String(WebApp.initDataUnsafe?.user?.id ?? "dev-" + Date.now());

// ── DOM refs ──────────────────────────────────────────────────────────────────

const landingScreen = document.getElementById("landing-screen")!;
const chatScreen = document.getElementById("chat-screen")!;
const chatWithMaxBtn = document.getElementById("chat-with-max-btn") as HTMLButtonElement;
const backBtn = document.getElementById("back-btn") as HTMLButtonElement;
const chatAvatar = document.getElementById("chat-avatar")!;
const connStatus = document.getElementById("conn-status")!;
const transcriptEl = document.getElementById("transcript")!;
const talkBtn = document.getElementById("talk-btn") as HTMLButtonElement;

// ── State ─────────────────────────────────────────────────────────────────────

let ws: WebSocket | null = null;
let reconnectAttempt = 0;
let isRecording = false;
let mediaRecorder: MediaRecorder | null = null;
let audioContext: AudioContext | null = null;
let audioQueue: ArrayBuffer[] = [];
let isPlaying = false;
let sessionActive = false;

// ── Entry ─────────────────────────────────────────────────────────────────────

showLanding();

// ── Screen navigation ─────────────────────────────────────────────────────────

function showLanding() {
  landingScreen.style.display = "flex";
  chatScreen.style.display = "none";
  sessionActive = false;
  ws?.close();
  ws = null;
}

function showChat() {
  landingScreen.style.display = "none";
  chatScreen.style.display = "flex";
  sessionActive = true;
  transcriptEl.innerHTML = "";
  connectWs();
}

chatWithMaxBtn.addEventListener("click", () => showChat());
backBtn.addEventListener("click", () => showLanding());

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connectWs() {
  connStatus.textContent = "Connecting…";
  connStatus.style.color = "#888";
  const url = `${VOICE_SERVER_URL}/voice/${chatId}`;
  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    connStatus.textContent = "● Connected";
    connStatus.style.color = "#22c55e";
    reconnectAttempt = 0;
    addMessage("hint", "Max is ready — hold the button and speak");
  };

  ws.onmessage = (e) => handleServerMessage(e.data as ArrayBuffer);

  ws.onclose = () => {
    if (!sessionActive) return;
    connStatus.textContent = "Reconnecting…";
    connStatus.style.color = "#888";
    const delay = Math.min(2 ** reconnectAttempt * 1000, 30000);
    reconnectAttempt++;
    setTimeout(connectWs, delay);
  };

  ws.onerror = () => ws?.close();
}

function handleServerMessage(data: ArrayBuffer) {
  const buf = new Uint8Array(data);
  if (buf.length === 0) return;
  const kind = buf[0];
  const payload = buf.slice(1);

  if (kind === 0x00) return; // keepalive

  if (kind === 0x01) {
    audioQueue.push(payload.buffer);
    if (!isPlaying) playNextChunk();
    return;
  }

  if (kind === 0x02) {
    const msg = JSON.parse(new TextDecoder().decode(payload));
    if (msg.type === "transcript") {
      addMessage("user", msg.text);
    } else if (msg.type === "agent_reply") {
      addMessage("agent", msg.text);
      chatAvatar.classList.add("speaking");
    } else if (msg.type === "audio_end") {
      chatAvatar.classList.remove("speaking");
    }
  }
}

async function playNextChunk() {
  if (audioQueue.length === 0) { isPlaying = false; return; }
  isPlaying = true;
  if (!audioContext) audioContext = new AudioContext();
  const chunk = audioQueue.shift()!;
  try {
    const audioBuffer = await audioContext.decodeAudioData(chunk);
    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioContext.destination);
    source.onended = playNextChunk;
    source.start();
  } catch {
    playNextChunk();
  }
}

// ── Push-to-talk ──────────────────────────────────────────────────────────────

talkBtn.addEventListener("pointerdown", startTalking);
talkBtn.addEventListener("pointerup", stopTalking);
talkBtn.addEventListener("pointerleave", stopTalking);

let recordingChunks: BlobPart[] = [];

async function startTalking() {
  if (isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;
  isRecording = true;
  talkBtn.classList.add("active");
  recordingChunks = [];

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { sampleRate: 24000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) recordingChunks.push(e.data);
  };
  mediaRecorder.onstop = async () => {
    const blob = new Blob(recordingChunks, { type: "audio/webm" });
    const ab = await blob.arrayBuffer();
    if (ws?.readyState === WebSocket.OPEN && ab.byteLength > 0) {
      const frame = new Uint8Array(ab.byteLength + 1);
      frame[0] = 0x01;
      frame.set(new Uint8Array(ab), 1);
      ws.send(frame);
      ws.send(new Uint8Array([0x04]));
    }
  };
  mediaRecorder.start(200);
}

function stopTalking() {
  if (!isRecording) return;
  isRecording = false;
  talkBtn.classList.remove("active");
  mediaRecorder?.stop();
  mediaRecorder?.stream.getTracks().forEach((t) => t.stop());
  mediaRecorder = null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function addMessage(role: "user" | "agent" | "hint", text: string) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = role === "user" ? `You: ${text}` : role === "agent" ? `Max: ${text}` : text;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}
