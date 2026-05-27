# NemoClaw — Railway Deployment Guide

This guide covers cloning NemoClaw, building the Docker image for Railway, deploying it, and adding a Telegram bot.

---

## Prerequisites

- [OrbStack](https://orbstack.dev) or Docker Desktop installed and running
- A [Docker Hub](https://hub.docker.com) account
- A [Railway](https://railway.app) account
- An [NVIDIA API key](https://build.nvidia.com)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/NVIDIA/NemoClaw.git
cd NemoClaw
```

---

## Step 2 — Install a Docker runtime (macOS)

Docker Desktop is not required. OrbStack is a lighter alternative:

```bash
brew install orbstack
open /Applications/OrbStack.app
```

Verify Docker is running:

```bash
docker info
```

---

## Step 3 — Build the base image

The project uses a two-layer Docker build. Build the base image first (cached heavy layers):

```bash
docker build \
  -f Dockerfile.base \
  -t ghcr.io/nvidia/nemoclaw/sandbox-base:latest \
  .
```

---

## Step 4 — Build the production image (local test)

```bash
docker build \
  --build-arg NEMOCLAW_BUILD_ID=$(date +%s) \
  -t nemoclaw:latest \
  .
```

> The two warnings about `NEMOCLAW_PROVIDER_KEY` are harmless — it is a provider name, not a secret.

---

## Step 5 — Log in to Docker Hub and push

```bash
docker login
docker tag nemoclaw:latest YOUR_DOCKERHUB_USERNAME/nemoclaw:latest
docker push YOUR_DOCKERHUB_USERNAME/nemoclaw:latest
```

---

## Step 6 — Build for linux/amd64 (required for Railway)

Railway runs on x86_64. If you are on Apple Silicon (M-series), you must cross-compile:

**Rebuild the base for amd64:**

```bash
docker buildx build \
  --platform linux/amd64 \
  -f Dockerfile.base \
  -t YOUR_DOCKERHUB_USERNAME/nemoclaw-base:latest \
  --push \
  .
```

**Rebuild the production image for amd64:**

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BASE_IMAGE=YOUR_DOCKERHUB_USERNAME/nemoclaw-base:latest \
  --build-arg NEMOCLAW_INFERENCE_BASE_URL=https://integrate.api.nvidia.com/v1 \
  --build-arg NEMOCLAW_BUILD_ID=$(date +%s) \
  -t YOUR_DOCKERHUB_USERNAME/nemoclaw:latest \
  --push \
  .
```

> `NEMOCLAW_INFERENCE_BASE_URL` must point to the real NVIDIA inference endpoint.
> The Dockerfile default (`https://inference.local/v1`) is a placeholder and will not work.

---

## Step 7 — Deploy to Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy a Docker Image**
2. Enter your image URL: `YOUR_DOCKERHUB_USERNAME/nemoclaw:latest`
3. Click **Deploy**
4. In **Settings → Public Networking** → click **Generate Domain**
5. In **Variables** tab, add:

| Variable | Value |
|---|---|
| `NVIDIA_API_KEY` | Your NVIDIA API key |
| `CHAT_UI_URL` | The Railway domain URL generated above |

---

## Step 8 — Add Telegram bot support

### Create the bot

1. Open Telegram → search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the bot token you receive

### Find your Telegram chat ID

1. Open Telegram → search for **@userinfobot**
2. Send any message — it replies with your numeric chat ID

### Add variables to Railway

In your Railway service → **Variables** tab, add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `ALLOWED_CHAT_IDS` | Your chat ID (optional, restricts access) |

### Rebuild with Telegram bridge

The Telegram bridge (`scripts/telegram-bridge-railway.js`) is a direct-mode bridge
that calls `openclaw agent` inside the container instead of SSH-ing into an OpenShell
sandbox. This is required for Railway deployments.

The `scripts/nemoclaw-start.sh` entrypoint automatically starts the bridge when
`TELEGRAM_BOT_TOKEN` is set.

Rebuild and push the image to apply these changes:

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BASE_IMAGE=YOUR_DOCKERHUB_USERNAME/nemoclaw-base:latest \
  --build-arg NEMOCLAW_INFERENCE_BASE_URL=https://integrate.api.nvidia.com/v1 \
  --build-arg NEMOCLAW_BUILD_ID=$(date +%s) \
  -t YOUR_DOCKERHUB_USERNAME/nemoclaw:latest \
  --push \
  .
```

Then in Railway → **Deployments** → **⋯** → **Redeploy**.

### Test the bot

Open Telegram, find your bot, and send a message. The bridge forwards it to the
OpenClaw agent and returns the response.

Commands:
- `/start` — introduction message
- `/reset` — reset the conversation session

---

## Architecture overview

```
Telegram user
     │
     ▼
telegram-bridge-railway.js   (Node.js, inside container)
     │
     ▼ openclaw agent --local
OpenClaw agent
     │
     ▼
NVIDIA inference API (https://integrate.api.nvidia.com/v1)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Exec format error` on Railway | Image built for arm64, Railway needs amd64 | Rebuild with `--platform linux/amd64` |
| Container crashes on start | Docker daemon not running | Start OrbStack or Docker Desktop |
| Bot does not respond | Bridge not started or wrong inference URL | Check `TELEGRAM_BOT_TOKEN` is set and rebuild with correct `NEMOCLAW_INFERENCE_BASE_URL` |
| Inference fails | Placeholder inference URL baked in | Rebuild with `--build-arg NEMOCLAW_INFERENCE_BASE_URL=https://integrate.api.nvidia.com/v1` |

---

## Files modified for Railway deployment

| File | Change |
|---|---|
| `scripts/telegram-bridge-railway.js` | New — direct-mode Telegram bridge for Railway |
| `scripts/nemoclaw-start.sh` | Added Telegram bridge auto-start on both root and non-root paths |
| `Dockerfile` | Added `COPY` for `telegram-bridge-railway.js` |
