"""Speech-to-text using faster-whisper with VAD gating."""
import io
import logging
import numpy as np
import soundfile as sf
import librosa
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # Whisper expects 16kHz


class STT:
    def __init__(self, model_size: str = "large-v3", device: str = "auto"):
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"Loading Whisper {model_size} on {device} ({compute_type})")
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
        )
        logger.info("Whisper ready")

    def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """Transcribe raw audio bytes (webm/ogg/wav) to text."""
        audio_array = self._decode_audio(audio_bytes)
        if audio_array is None or len(audio_array) < SAMPLE_RATE * 0.3:
            return ""

        with np.errstate(all="ignore"):
            segments, _ = self.model.transcribe(
                audio_array,
                language="en",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        logger.debug(f"STT: {text!r}")
        return text

    def _decode_audio(self, audio_bytes: bytes) -> "np.ndarray | None":
        try:
            buf = io.BytesIO(audio_bytes)
            audio, sr = sf.read(buf, dtype="float32", always_2d=False)
            if sr != SAMPLE_RATE:
                audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            return audio.astype(np.float32)
        except Exception:
            # soundfile can't read webm — call ffmpeg directly
            import tempfile, os, subprocess
            tmp_in = tmp_out = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
                    f.write(audio_bytes)
                    tmp_in = f.name
                tmp_out = tmp_in.replace(".webm", ".wav")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_in, "-ar", str(SAMPLE_RATE), "-ac", "1", tmp_out],
                    check=True, capture_output=True,
                )
                audio, _ = sf.read(tmp_out, dtype="float32", always_2d=False)
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                return audio.astype(np.float32)
            except Exception as e:
                logger.error(f"Audio decode failed: {e}")
                return None
            finally:
                if tmp_in and os.path.exists(tmp_in):
                    os.unlink(tmp_in)
                if tmp_out and os.path.exists(tmp_out):
                    os.unlink(tmp_out)
