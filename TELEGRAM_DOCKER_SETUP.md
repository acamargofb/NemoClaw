# NemoClaw Telegram Docker Setup

This guide documents the steps used to run NemoClaw in Docker with a Telegram bot and NVIDIA inference.

## Prerequisites

- Docker running locally
- A Telegram bot token from `@BotFather`
- An NVIDIA API key
- The NemoClaw image built locally

## 1. Build the image

From the NemoClaw repo directory:

```bash
docker build -t nemoclaw .
```

## 2. Start the container

Use plain ASCII quotes only. Each `\` must be the last character on the line.

```bash
docker rm -f nemoclaw

docker run --name nemoclaw \
  -e TELEGRAM_BOT_TOKEN='YOUR_TELEGRAM_BOT_TOKEN' \
  -e NVIDIA_API_KEY='YOUR_NVIDIA_API_KEY' \
  -e NEMOCLAW_MODEL='nvidia/nemotron-3-super-120b-a12b' \
  -e NEMOCLAW_INFERENCE_BASE_URL='https://integrate.api.nvidia.com/v1' \
  -d nemoclaw
```

## 3. Verify environment variables

```bash
docker exec nemoclaw printenv TELEGRAM_BOT_TOKEN
docker exec nemoclaw printenv NVIDIA_API_KEY
docker exec nemoclaw printenv NEMOCLAW_MODEL
```

Expected:

- `TELEGRAM_BOT_TOKEN` contains only the bot token
- `NVIDIA_API_KEY` contains only the NVIDIA key
- `NEMOCLAW_MODEL` is `nvidia/nemotron-3-super-120b-a12b`

## 4. Check the Telegram bridge log

```bash
docker exec nemoclaw cat /tmp/telegram-bridge.log
```

Expected log:

```text
[telegram] Model: nvidia/nemotron-3-super-120b-a12b
[telegram] Inference URL: https://integrate.api.nvidia.com/v1
[telegram] Bot @YourBotName connected
```

## 5. Test NVIDIA inference directly

This confirms the container can reach NVIDIA and authenticate correctly.

```bash
docker exec nemoclaw sh -lc 'curl -si https://integrate.api.nvidia.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NVIDIA_API_KEY" \
  -d "{\"model\":\"$NEMOCLAW_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"max_tokens\":20}"'
```

Expected:

- `HTTP/2 200`
- JSON response containing a chat completion

## 6. Test in Telegram

Open the bot in Telegram and send:

```text
/start
Hi
```

If working, the bot should respond normally.

## Troubleshooting

### `HTTP 401 Authentication failed`

Cause:

- `NVIDIA_API_KEY` is missing, invalid, expired, or accidentally set to the model name

Fix:

- Recreate the container with the correct `NVIDIA_API_KEY`

### `HTTP 404 page not found`

Cause:

- Broken inference request, wrong model setup, or malformed container env setup

Fix:

- Ensure `NEMOCLAW_INFERENCE_BASE_URL='https://integrate.api.nvidia.com/v1'`
- Ensure the running container is using the expected model
- Rebuild the image if needed

### Log says `NVIDIA_API_KEY required`

Cause:

- The container was started without the NVIDIA key
- Shell quoting broke the `docker run` command

Fix:

- Re-run `docker run` with straight quotes only: `'...'`
- Avoid smart quotes like `“...”`

### `TELEGRAM_BOT_TOKEN` contains extra text

Cause:

- A missing closing quote caused later `-e` flags to be folded into the token value

Fix:

- Recreate the container with a corrected command

## Notes From This Setup

- The working model was:

```text
nvidia/nemotron-3-super-120b-a12b
```

- The working NVIDIA inference base URL was:

```text
https://integrate.api.nvidia.com/v1
```

- The most important shell rule was:

```text
Use straight quotes and close every quote before the line-ending backslash.
```

## Voice Pipeline Latency

### Current Baseline (~20s)

| Stage | Time | Notes |
|---|---|---|
| STT — Whisper medium (CPU) | ~4s | Transcribes user audio |
| Agent — Nemotron 120B (NVIDIA API) | ~13s | Generates Max's reply |
| TTS — TADA on RunPod (warm) | ~3s | Synthesises Max's voice |

### What Has Already Been Applied

- Switched Whisper from `large-v3` → `medium` (~3× faster on CPU, no quality loss for short utterances)
- RunPod active workers set to `1` — worker stays warm, eliminates cold-start delays
- FlashBoot enabled on RunPod endpoint
- Constitutional rules removed from per-message prompt (were causing 120B model timeouts)
- Non-blocking server startup — voice server starts immediately, voice enrolment retries in background

### Further Reductions (~8–12s target)

**1. Replace local Whisper with a cloud STT API (saves ~3s)**

Use [Deepgram](https://deepgram.com) or [AssemblyAI](https://www.assemblyai.com) instead of local Whisper.
Both offer streaming transcription with ~300ms latency.

**2. Switch to a smaller inference model (saves ~8s)**

The 120B model is the dominant bottleneck. Swapping to a 7B–14B model on NVIDIA endpoints cuts agent inference to ~3–5s:

```env
NEMOCLAW_MODEL=nvidia/llama-3.1-nemotron-nano-8b-v1
```

**3. Stream TTS audio (saves perceived latency ~2s)**

Instead of waiting for the full WAV file, start playing audio as soon as the first sentence is synthesised.
Requires modifying `voice/server.py` and `mini-app/src/main.ts` to handle chunked audio.

### Hard Floor

With the current 120B model and RunPod serverless TTS, ~12s is the practical minimum.
To reach ~1s end-to-end you would need: dedicated GPU inference (not serverless), a 3B–7B model, streaming STT+TTS, and sub-100ms edge networking — the same class of infrastructure used by GPT-4o Voice.

## Security

If a Telegram bot token or NVIDIA API key is ever pasted into chat, terminal history, or screenshots that may be shared, rotate it immediately.
