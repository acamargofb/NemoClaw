"""TADA RunPod Serverless handler — drop-in replacement for Chatterbox TTS.

Priority: voice_id (enrolled) → error (voice required, no default).
Enrollment: encodes reference audio with NVIDIA Parakeet ASR (auto-transcribe),
caches EncoderOutput so the encoder is never needed again for synthesis.
"""
import base64
import gc
import io
import logging
import os
import tempfile
import uuid
from pathlib import Path

import librosa
import runpod
import soundfile as sf
import torch
import torchaudio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVICE = os.environ.get("TTS_DEVICE", "cuda")
MODEL_ID = os.environ.get("TADA_MODEL", "HumeAI/tada-1b")
VOICES_DIR = Path("/tmp/tts-voices")
VOICES_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_RATE = 24000

# voice_registry: voice_id → Path of cached EncoderOutput (.pt)
voice_registry: dict[str, Path] = {}

# ── Load TADA model at startup (reused across warm invocations) ───────────────

logger.info("Loading TADA %s on %s …", MODEL_ID, DEVICE)
from tada.modules.encoder import Encoder, EncoderOutput
from tada.modules.tada import TadaForCausalLM

model = TadaForCausalLM.from_pretrained(
    MODEL_ID, torch_dtype=torch.bfloat16
).to(DEVICE).eval()
logger.info("TADA model ready")


def _load_encoder() -> Encoder:
    """Load encoder on-demand; caller must unload after use to free VRAM."""
    return Encoder.from_pretrained("HumeAI/tada-codec", subfolder="encoder").to(DEVICE)


# ── Handler ───────────────────────────────────────────────────────────────────

def handler(job: dict) -> dict:
    inp = job.get("input", {})
    action = inp.get("action", "synthesize")

    # ── health ────────────────────────────────────────────────────────────────
    if action == "health":
        return {"status": "ok", "device": DEVICE, "model": MODEL_ID, "voices": len(voice_registry)}

    # ── enroll ────────────────────────────────────────────────────────────────
    if action == "enroll":
        raw = base64.b64decode(inp.get("audio_base64", ""))
        suffix = inp.get("suffix", ".wav")

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(raw)
            upload_path = f.name

        voice_id = str(uuid.uuid4())
        ref_wav = VOICES_DIR / f"{voice_id}.wav"
        cache_pt = VOICES_DIR / f"{voice_id}.pt"

        try:
            arr, _ = librosa.load(upload_path, sr=SAMPLE_RATE, mono=True)
            sf.write(str(ref_wav), arr, SAMPLE_RATE)
        finally:
            Path(upload_path).unlink(missing_ok=True)

        # Encode reference audio — text=None → encoder auto-transcribes (Parakeet)
        encoder = _load_encoder()
        try:
            audio, sr = torchaudio.load(str(ref_wav))
            audio = audio.to(DEVICE)
            with torch.no_grad():
                prompt = encoder(audio, text=None, sample_rate=sr)
            prompt.save(str(cache_pt))
        finally:
            del encoder
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        voice_registry[voice_id] = cache_pt
        logger.info("Enrolled voice_id=%s", voice_id)
        return {"voice_id": voice_id}

    # ── synthesize ────────────────────────────────────────────────────────────
    if action == "synthesize":
        text = inp.get("text", "")
        voice_id = inp.get("voice_id")

        if not voice_id or voice_id not in voice_registry:
            return {"error": "voice_id not found — enroll a voice first"}

        cache_pt = voice_registry[voice_id]
        prompt = EncoderOutput.load(str(cache_pt), device=DEVICE)

        with torch.no_grad():
            output = model.generate(prompt=prompt, text=text)

        wav = output.audio[0].cpu().float()
        if wav.ndim > 1:
            wav = wav.squeeze(0)

        buf = io.BytesIO()
        sf.write(buf, wav.numpy(), SAMPLE_RATE, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return {"audio_base64": base64.b64encode(buf.getvalue()).decode()}

    return {"error": f"Unknown action: {action}"}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
