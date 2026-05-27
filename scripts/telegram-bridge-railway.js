#!/usr/bin/env node
// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Telegram → NVIDIA API bridge (Railway direct mode).
 *
 * Calls the NVIDIA inference API directly without going through the
 * OpenClaw gateway (which requires OpenShell, unavailable on Railway).
 *
 * Env:
 *   TELEGRAM_BOT_TOKEN          — from @BotFather
 *   NVIDIA_API_KEY              — for inference
 *   NEMOCLAW_MODEL              — model ID (baked in at build time)
 *   NEMOCLAW_INFERENCE_BASE_URL — inference base URL (baked in at build time)
 *   ALLOWED_CHAT_IDS            — comma-separated Telegram chat IDs (optional)
 */

const https = require("https");

const DEFAULT_MODEL = "nvidia/llama-4-scout-17b-16e-instruct";
const DEFAULT_INFERENCE_BASE_URL = "https://integrate.api.nvidia.com/v1";
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
  const token = env.TELEGRAM_BOT_TOKEN;
  const apiKey = env.NVIDIA_API_KEY;
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN required");
  if (!apiKey) throw new Error("NVIDIA_API_KEY required");

  const configuredModel = String(env.NEMOCLAW_MODEL || DEFAULT_MODEL).trim();
  return {
    token,
    apiKey,
    configuredModel,
    model: resolveModel(configuredModel),
    inferenceBaseUrl: normalizeInferenceBaseUrl(env.NEMOCLAW_INFERENCE_BASE_URL),
    allowedChats: env.ALLOWED_CHAT_IDS
      ? env.ALLOWED_CHAT_IDS.split(",").map((s) => s.trim()).filter(Boolean)
      : null,
  };
}

let offset = 0;
const sessions = new Map(); // chatId → message history
let config = null;

// ── NVIDIA API ────────────────────────────────────────────────────

function callNvidiaAPI(messages) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${config.inferenceBaseUrl}/chat/completions`);
    const body = JSON.stringify({
      model: config.model,
      messages,
      max_tokens: 1024,
      temperature: 0.7,
    });

    const req = https.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || undefined,
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${config.apiKey}`,
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          if (res.statusCode !== 200) {
            reject(new Error(`HTTP ${res.statusCode}: ${buf.slice(0, 300)}`));
            return;
          }
          try {
            const data = JSON.parse(buf);
            resolve(data.choices?.[0]?.message?.content || "(no response)");
          } catch {
            reject(new Error(`Failed to parse response: ${buf.slice(0, 300)}`));
          }
        });
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ── Telegram API ──────────────────────────────────────────────────

function tgApi(method, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = https.request(
      {
        hostname: "api.telegram.org",
        path: `/bot${config.token}/${method}`,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
      },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          try { resolve(JSON.parse(buf)); } catch { resolve({ ok: false, error: buf }); }
        });
      },
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

async function sendMessage(chatId, text, replyTo) {
  const chunks = [];
  for (let i = 0; i < text.length; i += 4000) chunks.push(text.slice(i, i + 4000));
  for (const chunk of chunks) {
    await tgApi("sendMessage", {
      chat_id: chatId,
      text: chunk,
      reply_to_message_id: replyTo,
      parse_mode: "Markdown",
    }).catch(() =>
      tgApi("sendMessage", { chat_id: chatId, text: chunk, reply_to_message_id: replyTo }),
    );
  }
}

async function sendTyping(chatId) {
  await tgApi("sendChatAction", { chat_id: chatId, action: "typing" }).catch(() => {});
}

// ── Poll loop ─────────────────────────────────────────────────────

async function poll() {
  try {
    const res = await tgApi("getUpdates", { offset, timeout: 30 });

    if (res.ok && res.result?.length > 0) {
      for (const update of res.result) {
        offset = update.update_id + 1;

        const msg = update.message;
        if (!msg?.text) continue;

        const chatId = String(msg.chat.id);

        if (config.allowedChats && !config.allowedChats.includes(chatId)) {
          console.log(`[telegram] ignored chat ${chatId}`);
          continue;
        }

        const userName = msg.from?.first_name || "someone";
        console.log(`[telegram] [${chatId}] ${userName}: ${msg.text}`);

        if (msg.text === "/start") {
          await sendMessage(chatId,
            `NemoClaw — powered by ${config.configuredModel}\n\nSend me a message and I'll respond using NVIDIA AI.`,
            msg.message_id);
          continue;
        }

        if (msg.text === "/reset") {
          sessions.delete(chatId);
          await sendMessage(chatId, "Session reset.", msg.message_id);
          continue;
        }

        await sendTyping(chatId);
        const typingInterval = setInterval(() => sendTyping(chatId), 4000);

        // Build message history for context
        if (!sessions.has(chatId)) sessions.set(chatId, []);
        const history = sessions.get(chatId);
        history.push({ role: "user", content: msg.text });

        // Keep last 20 messages to avoid token limits
        if (history.length > 20) history.splice(0, history.length - 20);

        try {
          const response = await callNvidiaAPI(history);
          clearInterval(typingInterval);
          history.push({ role: "assistant", content: response });
          console.log(`[telegram] [${chatId}] assistant: ${response.slice(0, 100)}...`);
          await sendMessage(chatId, response, msg.message_id);
        } catch (err) {
          clearInterval(typingInterval);
          console.error(`[telegram] inference error: ${err.message}`);
          await sendMessage(chatId, `Error: ${err.message}`, msg.message_id);
        }
      }
    }
  } catch (err) {
    console.error("[telegram] Poll error:", err.message);
  }

  setTimeout(poll, 100);
}

// ── Main ──────────────────────────────────────────────────────────

async function main() {
  try {
    config = loadConfig();
  } catch (err) {
    console.error(`[telegram] ${err.message}`);
    process.exit(1);
  }

  console.log(`[telegram] Model: ${config.configuredModel}`);
  if (config.model !== config.configuredModel) {
    console.log(`[telegram] Remapped model to supported ID: ${config.model}`);
  }
  console.log(`[telegram] Inference URL: ${config.inferenceBaseUrl}`);

  const me = await tgApi("getMe", {});
  if (!me.ok) {
    console.error("[telegram] Failed to connect to Telegram:", JSON.stringify(me));
    process.exit(1);
  }

  console.log(`[telegram] Bot @${me.result.username} connected`);
  if (config.allowedChats) console.log(`[telegram] Allowed chats: ${config.allowedChats.join(", ")}`);

  poll();
}

if (require.main === module && process.argv[1] === __filename) {
  main();
}

module.exports = {
  loadConfig,
  normalizeInferenceBaseUrl,
  resolveModel,
};
