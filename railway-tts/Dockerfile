FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

WORKDIR /app

# System deps for librosa/soundfile/ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV TTS_DEVICE=cuda
ENV VOICES_DIR=/tmp/tts-voices
ENV PORT=8000

EXPOSE 8000

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT}
