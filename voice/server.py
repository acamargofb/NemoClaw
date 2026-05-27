"""Voice pipeline WebSocket server.

Pipeline per turn:
  1. Client streams audio (Opus) → STT → text transcript
  2. Text → NemoClaw agent (via voice-agent-bridge.js HTTP)
  3. Agent text → PersonaPlex TTS (in user's cloned voice) → audio
  4. Audio streamed back to client as Opus

WebSocket protocol (binary frames):
  0x01 + opus_bytes  — audio (both directions)
  0x02 + utf8_text   — text event (transcript or agent reply)
  0x03 + utf8_json   — control ({"type": "start"|"stop"|"enrolled"})
  0x00               — handshake / keepalive
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx
import librosa
import numpy as np
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from session import SessionStore
from stt import STT
from tts import TTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_BRIDGE_URL = os.environ.get("VOICE_AGENT_BRIDGE_URL", "http://127.0.0.1:3099")
VOICE_SAMPLES_DIR = os.environ.get("VOICE_SAMPLES_DIR", str(Path(__file__).parent / "voice_samples"))
TTS_DEVICE = os.environ.get("PERSONAPLEX_DEVICE", os.environ.get("TTS_DEVICE", "mps"))
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
# Good Vibes / guru voice: when set, all TTS uses this pre-enrolled RunPod voice_id
GURU_VOICE_ID = os.environ.get("GURU_VOICE_ID", "")
# Path to guru WAV file — if set, server re-enrolls on startup to get a fresh voice_id
GURU_VOICE_WAV = os.environ.get("GURU_VOICE_WAV", "")
# Persona system prompt prepended to every agent query (e.g. "You are Max Lowenstein...")
GURU_PERSONA = os.environ.get("GURU_PERSONA", "")
# MaloKlaw constitutional skills — loaded from file at startup
MALOKLAW_SKILLS_PATH = os.environ.get(
    "MALOKLAW_SKILLS_PATH",
    str(Path(__file__).parent / "personas" / "MALOKLAW_SKILLS.md"),
)
_maloklaw_constitution: str = ""
if Path(MALOKLAW_SKILLS_PATH).exists():
    _maloklaw_constitution = Path(MALOKLAW_SKILLS_PATH).read_text().strip()
    logger.info("MaloKlaw constitutional skills loaded from %s", MALOKLAW_SKILLS_PATH)
else:
    logger.warning("MaloKlaw skills file not found at %s", MALOKLAW_SKILLS_PATH)

Path(VOICE_SAMPLES_DIR).mkdir(parents=True, exist_ok=True)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="NemoClaw Voice Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

stt = STT(model_size="medium", device="auto")
tts = TTS(device=TTS_DEVICE)
sessions = SessionStore(VOICE_SAMPLES_DIR)


@app.on_event("startup")
async def startup():
    logger.info("Voice server ready.")
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_background_enroll())


async def _background_enroll():
    """Enroll guru voice in background, retrying until RunPod worker is ready."""
    global GURU_VOICE_ID
    if not GURU_VOICE_WAV or not Path(GURU_VOICE_WAV).exists():
        logger.warning("GURU_VOICE_WAV not set or missing — skipping enrollment")
        return
    loop = asyncio.get_event_loop()
    for attempt in range(30):
        try:
            voice_id = await loop.run_in_executor(None, _enroll_guru_wav, GURU_VOICE_WAV)
            GURU_VOICE_ID = voice_id
            logger.info("Guru voice enrolled: voice_id=%s", GURU_VOICE_ID)
            return
        except Exception as e:
            logger.info("Enrollment attempt %d/30 failed (%s) — retrying in 30s", attempt + 1, e)
            await asyncio.sleep(30)
    logger.error("Guru voice enrollment failed after 30 attempts")


def _enroll_guru_wav(wav_path: str) -> str:
    import base64
    import httpx
    with open(wav_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    runpod_endpoint = os.environ.get("RUNPOD_ENDPOINT_ID", "")
    runpod_key = os.environ.get("RUNPOD_API_KEY", "")
    resp = httpx.post(
        f"https://api.runpod.ai/v2/{runpod_endpoint}/runsync",
        json={"input": {"action": "enroll", "audio_base64": audio_b64, "suffix": ".wav"}},
        headers={"Authorization": f"Bearer {runpod_key}", "Content-Type": "application/json"},
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["output"]["voice_id"]


async def _cleanup_loop():
    while True:
        await asyncio.sleep(300)
        await sessions.cleanup()


# ── Voice enrollment ─────────────────────────────────────────────────────────

@app.post("/voice/enroll")
async def enroll_voice(
    chat_id: str = Form(...),
    audio: UploadFile = File(...),
):
    """Accept a ~30s voice recording and save it as the Chatterbox voice reference."""
    raw = await audio.read()

    # Write raw upload to temp file with correct extension so ffmpeg can detect format
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(raw)
        raw_upload_path = f.name

    # Resample to 24kHz mono WAV for PersonaPlex
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name

    try:
        audio_array, sr = librosa.load(raw_upload_path, sr=24000, mono=True)
        sf.write(wav_path, audio_array, 24000)

        pt_path = str(Path(VOICE_SAMPLES_DIR) / f"{_safe_id(chat_id)}.pt")
        raw_path = str(Path(VOICE_SAMPLES_DIR) / f"{_safe_id(chat_id)}.wav")
        # Save raw WAV for fallback
        Path(raw_path).write_bytes(Path(wav_path).read_bytes())

        tts.enroll_voice(wav_path, pt_path)
    finally:
        Path(wav_path).unlink(missing_ok=True)
        Path(raw_upload_path).unlink(missing_ok=True)

    return JSONResponse({"enrolled": True, "chat_id": chat_id})


# ── WebSocket voice session ───────────────────────────────────────────────────

@app.websocket("/voice/{chat_id}")
async def voice_ws(websocket: WebSocket, chat_id: str):
    await websocket.accept()

    session = await sessions.get_or_create(chat_id)
    logger.info(f"[{chat_id}] connected session={session.session_id}")

    # Send handshake
    await websocket.send_bytes(b"\x00")

    # Send enrollment status
    enrolled = session.voice_pt_path is not None
    await websocket.send_bytes(
        b"\x03" + json.dumps({"type": "enrolled", "enrolled": enrolled}).encode()
    )

    audio_buffer = bytearray()
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))

    try:
        async for message in websocket.iter_bytes():
            if not message:
                continue

            kind = message[0]
            payload = message[1:]

            if kind == 0x00:  # keepalive
                session.touch()
                continue

            if kind == 0x03:  # control
                try:
                    ctrl = json.loads(payload.decode())
                    if ctrl.get("type") == "stop":
                        break
                except Exception:
                    pass
                continue

            if kind == 0x01:  # audio chunk
                audio_buffer.extend(payload)
                continue

            if kind == 0x04:  # end of utterance — process buffered audio
                if not audio_buffer:
                    continue
                raw_audio = bytes(audio_buffer)
                audio_buffer.clear()
                session.touch()

                asyncio.create_task(
                    _process_turn(websocket, session, raw_audio)
                )

    except WebSocketDisconnect:
        logger.info(f"[{chat_id}] disconnected")
    finally:
        heartbeat_task.cancel()


def _clean_for_tts(text: str, max_chars: int = 200) -> str:
    """Strip markdown and truncate to a speakable length."""
    import re as _re
    # Remove markdown: headers, bold/italic, bullet points, code
    text = _re.sub(r"#{1,6}\s*", "", text)
    text = _re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = _re.sub(r"`[^`]*`", "", text)
    text = _re.sub(r"^\s*[-*•]\s+", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"\n+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    # Truncate at the last sentence boundary within max_chars
    truncated = text[:max_chars]
    last_end = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    return truncated[: last_end + 1] if last_end > 0 else truncated


async def _process_turn(websocket: WebSocket, session, raw_audio: bytes):
    chat_id = session.chat_id

    # Step 1: STT
    transcript = await asyncio.get_event_loop().run_in_executor(
        None, stt.transcribe, raw_audio
    )
    if not transcript:
        logger.debug(f"[{chat_id}] empty transcript, skipping")
        return

    logger.info(f"[{chat_id}] transcript: {transcript!r}")
    await _send_text(websocket, {"type": "transcript", "text": transcript})

    # Step 2: NemoClaw agent — ask for a short spoken reply
    persona_prefix = f"[Persona: {GURU_PERSONA}] " if GURU_PERSONA else ""
    voice_prompt = (
        f"{persona_prefix}"
        f"[Voice mode: reply in 1-2 spoken sentences, no markdown, no lists] {transcript}"
    )
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{AGENT_BRIDGE_URL}/query",
                json={"message": voice_prompt, "sessionId": session.session_id},
            )
            resp.raise_for_status()
            agent_response = resp.json().get("response", "")
    except Exception as e:
        logger.error(f"[{chat_id}] agent error: {e}")
        agent_response = "Sorry, I couldn't reach the agent right now."

    if not agent_response:
        return

    # Strip markdown and cap length for TTS
    agent_response = _clean_for_tts(agent_response)

    logger.info(f"[{chat_id}] agent: {agent_response[:100]!r}...")
    await _send_text(websocket, {"type": "agent_reply", "text": agent_response})

    # Step 3: TTS — use guru's pre-enrolled voice if configured, else user's voice
    try:
        wav_bytes = await tts.synthesize(
            agent_text=agent_response,
            voice_pt_path=None if GURU_VOICE_ID else session.voice_pt_path,
            voice_id=GURU_VOICE_ID or None,
        )
        if wav_bytes:
            # Send entire WAV in one message — client decodes the complete file
            await websocket.send_bytes(b"\x01" + wav_bytes)
            await _send_text(websocket, {"type": "audio_end"})
    except Exception as e:
        logger.error(f"[{chat_id}] TTS error: {e}")


async def _send_text(ws: WebSocket, data: dict):
    await ws.send_bytes(b"\x02" + json.dumps(data).encode())


async def _heartbeat(ws: WebSocket):
    try:
        while True:
            await asyncio.sleep(30)
            await ws.send_bytes(b"\x00")
    except Exception:
        pass


def _safe_id(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)[:64]
