#!/usr/bin/env node
// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Gmail → NVIDIA API bridge.
 *
 * Polls unread Gmail messages via the Gmail API and replies with the
 * NVIDIA model response.
 *
 * Required env:
 *   GMAIL_ADDRESS
 *   GMAIL_CLIENT_ID
 *   GMAIL_CLIENT_SECRET
 *   GMAIL_REFRESH_TOKEN
 *   NVIDIA_API_KEY
 *
 * Optional env:
 *   NEMOCLAW_MODEL
 *   NEMOCLAW_INFERENCE_BASE_URL
 *   GMAIL_ALLOWED_SENDERS      comma-separated allowlist
 *   GMAIL_POLL_INTERVAL_MS     default 30000
 */

const https = require("https");

const DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b";
const DEFAULT_INFERENCE_BASE_URL = "https://integrate.api.nvidia.com/v1";
const DEFAULT_POLL_INTERVAL_MS = 30_000;
const LEGACY_MODEL_ALIASES = {
  "nvidia/llama-3.3-nemotron-super-49b-v1": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
};

function normalizeInferenceBaseUrl(rawUrl) {
  const value = String(rawUrl || DEFAULT_INFERENCE_BASE_URL).trim();
  return value
    .replace(/\/chat\/completions\/?$/u, "")
    .replace(/\/+$/u, "");
}

function resolveModel(model) {
  const value = String(model || DEFAULT_MODEL).trim();
  return LEGACY_MODEL_ALIASES[value] || value;
}

function loadConfig(env = process.env) {
  const required = [
    "GMAIL_ADDRESS",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "NVIDIA_API_KEY",
  ];
  for (const name of required) {
    if (!env[name]) throw new Error(`${name} required`);
  }

  const configuredModel = String(env.NEMOCLAW_MODEL || DEFAULT_MODEL).trim();
  const pollIntervalMs = Number.parseInt(env.GMAIL_POLL_INTERVAL_MS || "", 10);

  return {
    gmailAddress: String(env.GMAIL_ADDRESS).trim().toLowerCase(),
    clientId: String(env.GMAIL_CLIENT_ID).trim(),
    clientSecret: String(env.GMAIL_CLIENT_SECRET).trim(),
    refreshToken: String(env.GMAIL_REFRESH_TOKEN).trim(),
    apiKey: String(env.NVIDIA_API_KEY).trim(),
    configuredModel,
    model: resolveModel(configuredModel),
    inferenceBaseUrl: normalizeInferenceBaseUrl(env.NEMOCLAW_INFERENCE_BASE_URL),
    pollIntervalMs: Number.isFinite(pollIntervalMs) && pollIntervalMs >= 5000
      ? pollIntervalMs
      : DEFAULT_POLL_INTERVAL_MS,
    allowedSenders: env.GMAIL_ALLOWED_SENDERS
      ? env.GMAIL_ALLOWED_SENDERS.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)
      : null,
  };
}

function requestJson(options, body, contentType = "application/json") {
  return new Promise((resolve, reject) => {
    const payload = body == null
      ? null
      : (typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body));
    const req = https.request(
      {
        ...options,
        headers: {
          ...(options.headers || {}),
          ...(payload == null ? {} : {
            "Content-Type": contentType,
            "Content-Length": Buffer.byteLength(payload),
          }),
        },
      },
      (res) => {
        let buf = "";
        res.on("data", (chunk) => (buf += chunk));
        res.on("end", () => {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error(`HTTP ${res.statusCode}: ${buf.slice(0, 500)}`));
            return;
          }
          if (!buf) {
            resolve({});
            return;
          }
          try {
            resolve(JSON.parse(buf));
          } catch {
            reject(new Error(`Failed to parse response: ${buf.slice(0, 500)}`));
          }
        });
      },
    );
    req.on("error", reject);
    if (payload != null) req.write(payload);
    req.end();
  });
}

function base64UrlDecode(input) {
  const normalized = String(input || "")
    .replace(/-/gu, "+")
    .replace(/_/gu, "/")
    .padEnd(Math.ceil(String(input || "").length / 4) * 4, "=");
  return Buffer.from(normalized, "base64").toString("utf8");
}

function base64UrlEncode(input) {
  return Buffer.from(input, "utf8")
    .toString("base64")
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function getHeader(headers, name) {
  return headers?.find((header) => header.name?.toLowerCase() === name.toLowerCase())?.value || "";
}

function extractPlainText(payload) {
  if (!payload) return "";
  if (payload.mimeType === "text/plain" && payload.body?.data) {
    return base64UrlDecode(payload.body.data);
  }
  if (payload.parts?.length) {
    for (const part of payload.parts) {
      const text = extractPlainText(part);
      if (text) return text;
    }
  }
  if (payload.body?.data) {
    return base64UrlDecode(payload.body.data);
  }
  return "";
}

function extractEmailAddress(raw) {
  const match = String(raw || "").match(/<([^>]+)>/u);
  return (match ? match[1] : raw || "").trim().toLowerCase();
}

function buildReplySubject(subject) {
  const value = String(subject || "").trim();
  if (!value) return "Re: Your message";
  return /^re:/iu.test(value) ? value : `Re: ${value}`;
}

async function fetchAccessToken(config) {
  const body = new URLSearchParams({
    client_id: config.clientId,
    client_secret: config.clientSecret,
    refresh_token: config.refreshToken,
    grant_type: "refresh_token",
  }).toString();

  const result = await requestJson({
    hostname: "oauth2.googleapis.com",
    path: "/token",
    method: "POST",
  }, body, "application/x-www-form-urlencoded");

  if (!result.access_token) throw new Error("OAuth token response missing access_token");
  return result.access_token;
}

async function gmailApi(accessToken, path, method = "GET", body = null) {
  return requestJson({
    hostname: "gmail.googleapis.com",
    path,
    method,
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  }, body);
}

async function callNvidiaAPI(config, messages) {
  const url = new URL(`${config.inferenceBaseUrl}/chat/completions`);
  const body = {
    model: config.model,
    messages,
    max_tokens: 1024,
    temperature: 0.7,
  };

  const data = await requestJson({
    protocol: url.protocol,
    hostname: url.hostname,
    port: url.port || undefined,
    path: url.pathname,
    method: "POST",
    headers: {
      Authorization: `Bearer ${config.apiKey}`,
    },
  }, body);

  return data.choices?.[0]?.message?.content || "(no response)";
}

async function listUnreadMessages(accessToken) {
  const params = new URLSearchParams({
    q: "is:unread in:inbox -from:me",
    maxResults: "10",
  });
  const result = await gmailApi(accessToken, `/gmail/v1/users/me/messages?${params.toString()}`);
  return result.messages || [];
}

async function getMessageDetails(accessToken, messageId) {
  return gmailApi(accessToken, `/gmail/v1/users/me/messages/${messageId}?format=full`);
}

async function markMessageRead(accessToken, messageId) {
  await gmailApi(accessToken, `/gmail/v1/users/me/messages/${messageId}/modify`, "POST", {
    removeLabelIds: ["UNREAD"],
  });
}

async function sendReply(accessToken, config, message, replyText) {
  const headers = message.payload?.headers || [];
  const to = extractEmailAddress(getHeader(headers, "Reply-To") || getHeader(headers, "From"));
  const subject = buildReplySubject(getHeader(headers, "Subject"));
  const messageId = getHeader(headers, "Message-Id");
  const references = getHeader(headers, "References");
  const rawMessage = [
    `From: ${config.gmailAddress}`,
    `To: ${to}`,
    `Subject: ${subject}`,
    "Content-Type: text/plain; charset=UTF-8",
    "MIME-Version: 1.0",
    ...(messageId ? [`In-Reply-To: ${messageId}`] : []),
    ...(messageId ? [`References: ${[references, messageId].filter(Boolean).join(" ").trim()}`] : []),
    "",
    replyText,
    "",
  ].join("\r\n");

  await gmailApi(accessToken, "/gmail/v1/users/me/messages/send", "POST", {
    raw: base64UrlEncode(rawMessage),
    threadId: message.threadId,
  });
}

const sessions = new Map();
const inFlight = new Set();

async function handleMessage(config, accessToken, messageRef) {
  if (inFlight.has(messageRef.id)) return;
  inFlight.add(messageRef.id);

  try {
    const message = await getMessageDetails(accessToken, messageRef.id);
    const headers = message.payload?.headers || [];
    const fromRaw = getHeader(headers, "From");
    const fromEmail = extractEmailAddress(fromRaw);
    if (!fromEmail || fromEmail === config.gmailAddress) return;
    if (config.allowedSenders && !config.allowedSenders.includes(fromEmail)) {
      console.log(`[gmail] ignored sender ${fromEmail}`);
      return;
    }

    const bodyText = extractPlainText(message.payload).trim();
    if (!bodyText) {
      console.log(`[gmail] skipped empty body from ${fromEmail}`);
      await markMessageRead(accessToken, messageRef.id);
      return;
    }

    console.log(`[gmail] ${fromEmail}: ${bodyText.slice(0, 120).replace(/\s+/gu, " ")}`);

    if (!sessions.has(fromEmail)) sessions.set(fromEmail, []);
    const history = sessions.get(fromEmail);
    history.push({ role: "user", content: bodyText });
    if (history.length > 20) history.splice(0, history.length - 20);

    const reply = await callNvidiaAPI(config, history);
    history.push({ role: "assistant", content: reply });
    await sendReply(accessToken, config, message, reply);
    await markMessageRead(accessToken, messageRef.id);
    console.log(`[gmail] replied to ${fromEmail}: ${reply.slice(0, 120).replace(/\s+/gu, " ")}`);
  } catch (err) {
    console.error(`[gmail] message error ${messageRef.id}: ${err.message}`);
  } finally {
    inFlight.delete(messageRef.id);
  }
}

async function poll(config) {
  try {
    const accessToken = await fetchAccessToken(config);
    const messages = await listUnreadMessages(accessToken);
    for (const message of messages) {
      await handleMessage(config, accessToken, message);
    }
  } catch (err) {
    console.error(`[gmail] poll error: ${err.message}`);
  } finally {
    setTimeout(() => poll(config), config.pollIntervalMs);
  }
}

async function main() {
  let config;
  try {
    config = loadConfig();
  } catch (err) {
    console.error(`[gmail] ${err.message}`);
    process.exit(1);
  }

  console.log(`[gmail] Address: ${config.gmailAddress}`);
  console.log(`[gmail] Model: ${config.configuredModel}`);
  if (config.model !== config.configuredModel) {
    console.log(`[gmail] Remapped model to supported ID: ${config.model}`);
  }
  console.log(`[gmail] Inference URL: ${config.inferenceBaseUrl}`);
  console.log(`[gmail] Poll interval: ${config.pollIntervalMs}ms`);

  const accessToken = await fetchAccessToken(config);
  const profile = await gmailApi(accessToken, "/gmail/v1/users/me/profile");
  console.log(`[gmail] Connected as ${profile.emailAddress}`);
  poll(config);
}

if (require.main === module && process.argv[1] === __filename) {
  main().catch((err) => {
    console.error(`[gmail] fatal error: ${err.message}`);
    process.exit(1);
  });
}

module.exports = {
  buildReplySubject,
  extractEmailAddress,
  extractPlainText,
  loadConfig,
  normalizeInferenceBaseUrl,
  resolveModel,
};
