#!/usr/bin/env node
// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * HTTP micro-server that wraps runAgentInSandbox() for use by the voice pipeline.
 *
 * POST /query  { message: string, sessionId: string }
 *           -> { response: string }
 *
 * Env:
 *   NVIDIA_API_KEY  — for inference inside the sandbox
 *   SANDBOX_NAME    — sandbox name (default: maloclaw-assistant)
 *   PORT            — port to listen on (default: 3099)
 */

const http = require("http");
const { execFileSync, spawn } = require("child_process");
const { resolveOpenshell } = require("../bin/lib/resolve-openshell");
const { shellQuote, validateName } = require("../bin/lib/runner");

const OPENSHELL = resolveOpenshell();
if (!OPENSHELL) {
  console.error("openshell not found on PATH or in common locations");
  process.exit(1);
}

const API_KEY = process.env.NVIDIA_API_KEY;
const SANDBOX = process.env.SANDBOX_NAME || "maloclaw-assistant";
const PORT = parseInt(process.env.PORT || "3099", 10);

if (!API_KEY) { console.error("NVIDIA_API_KEY required"); process.exit(1); }
try { validateName(SANDBOX, "SANDBOX_NAME"); } catch (e) { console.error(e.message); process.exit(1); }

function runAgentInSandbox(message, sessionId) {
  return new Promise((resolve) => {
    const sshConfig = execFileSync(OPENSHELL, ["sandbox", "ssh-config", SANDBOX], { encoding: "utf-8" });

    const confDir = require("fs").mkdtempSync("/tmp/nemoclaw-voice-ssh-");
    const confPath = `${confDir}/config`;
    require("fs").writeFileSync(confPath, sshConfig, { mode: 0o600 });

    const safeSessionId = String(sessionId).replace(/[^a-zA-Z0-9-]/g, "");
    const cmd = `export NVIDIA_API_KEY=${shellQuote(API_KEY)} && openclaw agent --agent main --local -m ${shellQuote(message)} --session-id ${shellQuote("voice-" + safeSessionId)}`;

    const proc = spawn("ssh", ["-T", "-F", confPath, `openshell-${SANDBOX}`, cmd], {
      timeout: 120000,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (d) => (stdout += d.toString()));
    proc.stderr.on("data", (d) => (stderr += d.toString()));

    proc.on("close", (code) => {
      try { require("fs").unlinkSync(confPath); require("fs").rmdirSync(confDir); } catch { /* ignored */ }

      const lines = stdout.split("\n");
      const responseLines = lines.filter(
        (l) =>
          !l.startsWith("Setting up NemoClaw") &&
          !l.startsWith("[plugins]") &&
          !l.startsWith("(node:") &&
          !l.includes("NemoClaw ready") &&
          !l.includes("NemoClaw registered") &&
          !l.includes("openclaw agent") &&
          !l.includes("┌─") &&
          !l.includes("│ ") &&
          !l.includes("└─") &&
          l.trim() !== "",
      );

      const response = responseLines.join("\n").trim();

      if (response) resolve(response);
      else if (code !== 0) resolve(`Agent error (code ${code}): ${stderr.trim().slice(0, 300)}`);
      else resolve("(no response)");
    });

    proc.on("error", (err) => resolve(`Error: ${err.message}`));
  });
}

const server = http.createServer((req, res) => {
  if (req.method !== "POST" || req.url !== "/query") {
    res.writeHead(404);
    res.end();
    return;
  }

  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", async () => {
    try {
      const { message, sessionId } = JSON.parse(body);
      if (!message || typeof message !== "string") {
        res.writeHead(400);
        res.end(JSON.stringify({ error: "message required" }));
        return;
      }
      const response = await runAgentInSandbox(message, sessionId || "default");
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ response }));
    } catch (err) {
      res.writeHead(500);
      res.end(JSON.stringify({ error: err.message }));
    }
  });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[voice-agent-bridge] listening on http://127.0.0.1:${PORT}`);
  console.log(`[voice-agent-bridge] sandbox: ${SANDBOX}`);
});
