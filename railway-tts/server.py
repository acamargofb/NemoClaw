"""Chatterbox TTS microservice for Railway GPU deployment.

Endpoints:
  GET  /health              — liveness check
  POST /enroll              — upload reference WAV, get voice_id back
  POST /synthesize          — text + voice_id → WAV audio bytes
"""
import io
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import torch
import torchaudio
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("TTS_API_KEY", "")
DEVICE = os.environ.get("TTS_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/tmp/tts-voices"))
VOICES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NemoClaw TTS Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

model = None
# voice_id -> True (conditionals are loaded into model.conds per-request)
voice_registry: dict[str, Path] = {}


def _check_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.on_event("startup")
async def startup():
    global model
    from chatterbox.tts import ChatterboxTTS
    logger.info("Loading Chatterbox on %s …", DEVICE)
    model = ChatterboxTTS.from_pretrained(device=DEVICE)
    logger.info("Warming up …")
    model.generate("Hello.")
    logger.info("TTS service ready.")


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "voices": len(voice_registry)}


@app.post("/enroll")
async def enroll(
    audio: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None),
):
    _check_key(x_api_key)
    raw = await audio.read()

    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(raw)
        upload_path = f.name

    voice_id = str(uuid.uuid4())
    ref_path = VOICES_DIR / f"{voice_id}.wav"

    try:
        import librosa
        import soundfile as sf
        audio_array, _ = librosa.load(upload_path, sr=24000, mono=True)
        sf.write(str(ref_path), audio_array, 24000)
        # Pre-compute conditionals so first synthesis is instant
        model.prepare_conditionals(str(ref_path))
    finally:
        Path(upload_path).unlink(missing_ok=True)

    voice_registry[voice_id] = ref_path
    logger.info("Enrolled voice %s", voice_id)
    return {"voice_id": voice_id}


@app.post("/synthesize")
async def synthesize(
    text: str = Form(...),
    voice_id: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None),
):
    _check_key(x_api_key)

    if voice_id and voice_id in voice_registry:
        ref = str(voice_registry[voice_id])
        wav = model.generate(text, audio_prompt_path=ref)
        logger.info("Synthesized with voice %s: %r", voice_id, text[:60])
    else:
        wav = model.generate(text)
        logger.info("Synthesized with default voice: %r", text[:60])

    buf = io.BytesIO()
    torchaudio.save(buf, wav, model.sr, format="wav")
    return Response(content=buf.getvalue(), media_type="audio/wav")
