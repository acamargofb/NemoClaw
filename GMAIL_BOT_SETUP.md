# NemoClaw Gmail Bridge Setup

This guide adds a Gmail inbox to the same NemoClaw Docker container so email can be answered using the configured NVIDIA model.

## What This Bridge Does

- Polls unread messages in Gmail
- Sends the email body to NemoClaw's NVIDIA-backed inference flow
- Replies by email in the same Gmail thread
- Keeps short conversation history per sender

## Required Google Setup

Create or choose a Gmail account for the bot, then create OAuth credentials for the Gmail API.

### 1. Create a Google Cloud project

- Open Google Cloud Console
- Create a project for the bot

### 2. Enable the Gmail API

- Go to `APIs & Services`
- Enable `Gmail API`

### 3. Configure OAuth consent

- Create an OAuth consent screen
- Add your Gmail account as a test user if the app is in testing mode

### 4. Create OAuth credentials

- Create an OAuth client ID
- Choose `Desktop app`
- Save:
  - client ID
  - client secret

### 5. Generate a refresh token

Use Google's OAuth playground or a local OAuth flow to obtain a refresh token with Gmail scopes that allow reading and sending mail.

Recommended scopes:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.send`

Save:

- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`
- `GMAIL_ADDRESS`

## Docker Run Command

If you already use Telegram, you can run both bridges together:

```bash
docker rm -f nemoclaw

docker run --name nemoclaw \
  -e TELEGRAM_BOT_TOKEN='YOUR_TELEGRAM_BOT_TOKEN' \
  -e NVIDIA_API_KEY='YOUR_NVIDIA_API_KEY' \
  -e NEMOCLAW_MODEL='nvidia/nemotron-3-super-120b-a12b' \
  -e NEMOCLAW_INFERENCE_BASE_URL='https://integrate.api.nvidia.com/v1' \
  -e GMAIL_ADDRESS='yourbot@gmail.com' \
  -e GMAIL_CLIENT_ID='YOUR_GOOGLE_CLIENT_ID' \
  -e GMAIL_CLIENT_SECRET='YOUR_GOOGLE_CLIENT_SECRET' \
  -e GMAIL_REFRESH_TOKEN='YOUR_GOOGLE_REFRESH_TOKEN' \
  -e GMAIL_ALLOWED_SENDERS='friend1@example.com,friend2@example.com' \
  -e GMAIL_POLL_INTERVAL_MS='30000' \
  -d nemoclaw
```

Notes:

- `GMAIL_ALLOWED_SENDERS` is optional but recommended
- `GMAIL_POLL_INTERVAL_MS` is optional

## Verify The Bridge

Check the Gmail bridge log:

```bash
docker exec nemoclaw cat /tmp/gmail-bridge.log
```

Expected startup lines:

```text
[gmail] Address: yourbot@gmail.com
[gmail] Model: nvidia/nemotron-3-super-120b-a12b
[gmail] Inference URL: https://integrate.api.nvidia.com/v1
[gmail] Connected as yourbot@gmail.com
```

## Test It

1. Send an email to the bot Gmail address
2. Wait for the polling interval
3. Check for a reply in the same thread

You can also inspect the log:

```bash
docker exec nemoclaw cat /tmp/gmail-bridge.log
```

Expected runtime lines:

```text
[gmail] sender@example.com: Hello from email
[gmail] replied to sender@example.com: ...
```

## Troubleshooting

### Log says `GMAIL_CLIENT_ID required` or similar

One or more required Gmail OAuth env vars were not passed into the container.

### Log says OAuth token fetch failed

Common causes:

- wrong client ID or client secret
- invalid or revoked refresh token
- Gmail account not added as a test user in the consent screen

### Gmail connects but no replies are sent

Check:

- the sender is included in `GMAIL_ALLOWED_SENDERS`, if set
- the incoming email has a readable plain-text part
- NVIDIA inference still works from the container

### Gmail account receives mail but stays unread

If the bridge cannot parse or reply to the message, it logs the error in `/tmp/gmail-bridge.log`.

## Security

- Treat Gmail OAuth credentials like secrets
- Rotate any secret that was pasted into chat, screenshots, or terminal history
