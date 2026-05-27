"""TTS wrapper — runs Chatterbox locally, via RunPod Serverless, or a Railway/Colab GPU service.

Priority order (first match wins):
  1. RUNPOD_ENDPOINT_ID + RUNPOD_API_KEY  → RunPod Serverless (recommended for production)
  2. RAILWAY_TTS_URL                       → Railway / Colab ngrok server
  3. (neither set)                         → local Chatterbox on MPS/CPU
"""
import asyncio
import base64
import io
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "")
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
RAILWAY_TTS_URL = os.environ.get("RAILWAY_TTS_URL", "").rstrip("/")
RAILWAY_TTS_API_KEY = os.environ.get("RAILWAY_TTS_API_KEY", "")
SAMPLE_RATE = 24000


def _ref_wav(path: str) -> Optional[str]:
    p = Path(path)
    for candidate in [p.with_suffix(".wav"), p]:
        if candidate.exists():
            return str(candidate)
    return None


def _read_rid(path: str) -> Optional[str]:
    rid = Path(path).with_suffix(".rid")
    return rid.read_text().strip() if rid.exists() else None


def _write_rid(path: str, voice_id: str):
    Path(path).with_suffix(".rid").write_text(voice_id)


# ── RunPod Serverless client ──────────────────────────────────────────────────

class _RunpodTTS:
    def __init__(self, endpoint_id: str, api_key: str):
        self._base = f"https://api.runpod.ai/v2/{endpoint_id}"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _call(self, action: dict, timeout: int = 120) -> dict:
        import httpx
        resp = httpx.post(
            f"{self._base}/runsync",
            json={"input": action},
            headers=self._headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "FAILED":
            raise RuntimeError(f"RunPod job failed: {data.get('error')}")
        if status not in ("COMPLETED", None):
            raise RuntimeError(f"RunPod unexpected status '{status}': {data}")
        output = data.get("output")
        if output is None:
            raise RuntimeError(f"RunPod returned no output: {data}")
        if isinstance(output, dict) and "error" in output:
            raise RuntimeError(f"RunPod handler error: {output['error']}")
        return output

    def load(self):
        logger.info("RunPod TTS ready (endpoint=%s)", self._base.split("/")[-1])

    def enroll_voice(self, wav_path: str, output_pt_path: str) -> str:
        ref_path = str(Path(output_pt_path).with_suffix(".wav"))
        shutil.copy2(wav_path, ref_path)
        with open(ref_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        # Allow 5 min for cold start + model load + enroll
        result = self._call({"action": "enroll", "audio_base64": audio_b64, "suffix": ".wav"}, timeout=300)
        voice_id = result["voice_id"]
        _write_rid(ref_path, voice_id)
        logger.info("RunPod TTS: enrolled voice_id=%s", voice_id)
        return ref_path

    def synthesize_sync(self, text: str, voice_pt_path: Optional[str], custom_voice_wav: Optional[str], voice_id_override: Optional[str] = None) -> bytes:
        voice_id = voice_id_override
        if not voice_id and voice_pt_path:
            ref = _ref_wav(voice_pt_path)
            if ref:
                voice_id = _read_rid(ref)
                if not voice_id:
                    with open(ref, "rb") as f:
                        audio_b64 = base64.b64encode(f.read()).decode()
                    result = self._call({"action": "enroll", "audio_base64": audio_b64, "suffix": ".wav"}, timeout=300)
                    voice_id = result["voice_id"]
                    _write_rid(ref, voice_id)

        payload: dict = {"action": "synthesize", "text": text}
        if voice_id:
            payload["voice_id"] = voice_id

        result = self._call(payload, timeout=180)
        return base64.b64decode(result["audio_base64"])


# ── Railway / Colab HTTP client ───────────────────────────────────────────────

class _RailwayTTS:
    def __init__(self, url: str, api_key: str):
        self._url = url
        self._headers = {"x-api-key": api_key} if api_key else {}

    def load(self):
        import httpx
        resp = httpx.get(f"{self._url}/health", timeout=10)
        resp.raise_for_status()
        logger.info("Railway TTS: %s", resp.json())

    def enroll_voice(self, wav_path: str, output_pt_path: str) -> str:
        import httpx
        ref_path = str(Path(output_pt_path).with_suffix(".wav"))
        shutil.copy2(wav_path, ref_path)
        with open(ref_path, "rb") as f:
            resp = httpx.post(
                f"{self._url}/enroll",
                files={"audio": ("voice.wav", f, "audio/wav")},
                headers=self._headers,
                timeout=60,
            )
        resp.raise_for_status()
        voice_id = resp.json()["voice_id"]
        _write_rid(ref_path, voice_id)
        logger.info("Railway TTS: enrolled voice_id=%s", voice_id)
        return ref_path

    def synthesize_sync(self, text: str, voice_pt_path: Optional[str], custom_voice_wav: Optional[str], voice_id_override: Optional[str] = None) -> bytes:
        import httpx
        voice_id = None
        if voice_pt_path:
            ref = _ref_wav(voice_pt_path)
            if ref:
                voice_id = _read_rid(ref)
                if not voice_id:
                    with open(ref, "rb") as f:
                        resp = httpx.post(
                            f"{self._url}/enroll",
                            files={"audio": ("voice.wav", f, "audio/wav")},
                            headers=self._headers,
                            timeout=60,
                        )
                    resp.raise_for_status()
                    voice_id = resp.json()["voice_id"]
                    _write_rid(ref, voice_id)

        data = {"text": text}
        if voice_id:
            data["voice_id"] = voice_id

        resp = httpx.post(
            f"{self._url}/synthesize",
            data=data,
            headers=self._headers,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.content


# ── Local Chatterbox ──────────────────────────────────────────────────────────

class _LocalTTS:
    def __init__(self, device: str):
        self._device = device
        self._model = None
        self._cached_voice_ref: Optional[str] = None

    def load(self):
        from chatterbox.tts import ChatterboxTTS
        logger.info("TTS: loading Chatterbox on %s", self._device)
        self._model = ChatterboxTTS.from_pretrained(device=self._device)
        logger.info("TTS: warming up")
        self._model.generate("Hello.")
        logger.info("TTS: ready")

    def enroll_voice(self, wav_path: str, output_pt_path: str) -> str:
        ref_path = str(Path(output_pt_path).with_suffix(".wav"))
        shutil.copy2(wav_path, ref_path)
        self._model.prepare_conditionals(ref_path)
        self._cached_voice_ref = ref_path
        logger.info("TTS: enrolled → %s", ref_path)
        return ref_path

    def synthesize_sync(self, text: str, voice_pt_path: Optional[str], custom_voice_wav: Optional[str], voice_id_override: Optional[str] = None) -> bytes:
        import torchaudio
        ref = None
        if voice_pt_path:
            ref = _ref_wav(voice_pt_path)
        if ref is None and custom_voice_wav and Path(custom_voice_wav).exists():
            ref = custom_voice_wav

        if ref:
            if ref != self._cached_voice_ref:
                logger.info("TTS: loading voice ref %s", ref)
                self._model.prepare_conditionals(ref)
                self._cached_voice_ref = ref
            wav = self._model.generate(text)
        else:
            wav = self._model.generate(text)

        buf = io.BytesIO()
        torchaudio.save(buf, wav, self._model.sr, format="wav")
        return buf.getvalue()


# ── Public TTS class (auto-selects backend) ───────────────────────────────────

class TTS:
    def __init__(self, device: str = "mps"):
        if RUNPOD_ENDPOINT_ID and RUNPOD_API_KEY:
            logger.info("TTS: using RunPod backend (endpoint=%s)", RUNPOD_ENDPOINT_ID)
            self._backend = _RunpodTTS(RUNPOD_ENDPOINT_ID, RUNPOD_API_KEY)
        elif RAILWAY_TTS_URL:
            logger.info("TTS: using Railway backend at %s", RAILWAY_TTS_URL)
            self._backend = _RailwayTTS(RAILWAY_TTS_URL, RAILWAY_TTS_API_KEY)
        else:
            logger.info("TTS: using local Chatterbox on %s", device)
            self._backend = _LocalTTS(device)
        self._lock = asyncio.Lock()

    def load(self):
        self._backend.load()

    def enroll_voice(self, wav_path: str, output_pt_path: str) -> str:
        return self._backend.enroll_voice(wav_path, output_pt_path)

    async def synthesize(
        self,
        agent_text: str,
        voice_pt_path: Optional[str] = None,
        custom_voice_wav: Optional[str] = None,
        voice_id: Optional[str] = None,
    ) -> bytes:
        import functools
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(
                    self._backend.synthesize_sync,
                    agent_text,
                    voice_pt_path,
                    custom_voice_wav,
                    voice_id,
                ),
            )
