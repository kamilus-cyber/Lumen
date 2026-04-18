#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────
# Lumen — Local Universal Memory Extraction Node
# Copyright 2026 Mycosa. All rights reserved.
# License: See LICENSE.txt — attribution required,
#          commercial use requires written permission.
#          wu-reasoning.com
# ─────────────────────────────────────────────────────────
# lumen_server.py — Lumen Proxy Server + Minimal Chat UI
# Version 0.1
#
# Runs as middleware between user and AI model.
# Intercepts every message, injects relevant memories, forwards to Ollama.
#
# Usage:
#   python lumen_server.py                    — start server (default port 8000)
#   python lumen_server.py --port 8080        — custom port
#   python lumen_server.py --config my.yaml   — custom config
#
# Endpoints:
#   GET  /              — minimal chat UI
#   POST /chat          — chat with memory injection (UI uses this)
#   POST /v1/chat       — OpenAI-compatible proxy endpoint
#   GET  /memories      — query memories (requires token)
#   GET  /health        — status check
#   POST /admin/token   — create access token (requires admin token)

import json
import os
import re
import sys
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import yaml

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

DEFAULT_CONFIG = "lumen_config.yaml"

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

# ─────────────────────────────────────────────────────────
# MEMORY QUERY
# ─────────────────────────────────────────────────────────

class MemoryQuery:
    """Lightweight memory retrieval — no heavy dependencies."""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_token_level(self, token: str) -> int:
        try:
            row = self.conn.execute(
                "SELECT level FROM access_tokens WHERE token = ?", (token,)
            ).fetchone()
            return row["level"] if row else 0
        except Exception:
            return 0

    def search(self, query: str, max_level: int = 1, limit: int = 8) -> list:
        """
        Simple keyword search across memories.
        Returns memories relevant to query within access level.
        """
        try:
            words = [w.lower() for w in re.split(r'\W+', query) if len(w) > 3]
            if not words:
                return self._recent(max_level, limit)

            # Score memories by keyword matches
            rows = self.conn.execute(
                """SELECT * FROM memories
                   WHERE security_level <= ? AND promoted = 1
                   ORDER BY confidence DESC""",
                (max_level,)
            ).fetchall()

            scored = []
            for row in rows:
                content_lower = row["content"].lower()
                tags = json.loads(row["tags"] or "[]")
                tags_lower = " ".join(tags).lower()
                score = sum(1 for w in words if w in content_lower or w in tags_lower)
                if score > 0:
                    scored.append((score, dict(row)))

            scored.sort(key=lambda x: (-x[0], -x[1]["confidence"]))
            return [m for _, m in scored[:limit]]

        except Exception as e:
            return []

    def _recent(self, max_level: int, limit: int) -> list:
        """Fallback — return most recent promoted memories."""
        try:
            rows = self.conn.execute(
                """SELECT * FROM memories
                   WHERE security_level <= ? AND promoted = 1
                   ORDER BY created_at DESC LIMIT ?""",
                (max_level, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_context_summary(self) -> str:
        """Get the most recent speed scan summary."""
        try:
            row = self.conn.execute(
                "SELECT domain, summary FROM context_map ORDER BY scanned_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return f"Domain: {row['domain']}. {row['summary']}"
        except Exception:
            pass
        return ""

    def format_for_injection(self, memories: list, context_summary: str = "") -> str:
        """Format memories as a clean context block for the AI prompt."""
        if not memories and not context_summary:
            return ""

        lines = ["[MEMORY CONTEXT]"]

        if context_summary:
            lines.append(f"Background: {context_summary}")
            lines.append("")

        if memories:
            lines.append("Relevant memories:")
            type_icons = {
                "fact": "📌", "preference": "💜", "rule": "⚖️",
                "decision": "🎯", "reminder": "🔔"
            }
            for m in memories:
                icon = type_icons.get(m["type"], "•")
                lines.append(f"{icon} [{m['type']}] {m['content']}")

        lines.append("[END MEMORY CONTEXT]")
        lines.append("")
        return "
".join(lines)


# ─────────────────────────────────────────────────────────
# OLLAMA PROXY
# ─────────────────────────────────────────────────────────

def call_ollama(host: str, model: str, messages: list, stream: bool = False) -> dict:
    """Send chat request to Ollama."""
    url = f"{host}/api/chat"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())
            return {"ok": True, "content": data["message"]["content"]}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────
# MINIMAL CHAT UI (single HTML page)
# ─────────────────────────────────────────────────────────

CHAT_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lumen</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: monospace; background: #0f0f0f; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
  #header { padding: 12px 20px; border-bottom: 1px solid #222; display: flex; align-items: center; gap: 10px; }
  #header span { color: #888; font-size: 12px; }
  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: #444; flex-shrink: 0; }
  #status-dot.ok { background: #4caf50; }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 6px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
  .msg.user { background: #1e1e2e; align-self: flex-end; color: #cdd6f4; }
  .msg.assistant { background: #181825; align-self: flex-start; color: #cdd6f4; border-left: 2px solid #6c7086; }
  .msg.system { background: transparent; align-self: center; color: #585b70; font-size: 12px; }
  .msg.memory { background: #1a1a2e; align-self: flex-start; color: #a6adc8; font-size: 12px; border-left: 2px solid #313244; padding: 6px 10px; }
  #input-area { padding: 12px 20px; border-top: 1px solid #222; display: flex; gap: 10px; }
  #input { flex: 1; background: #181825; border: 1px solid #313244; color: #cdd6f4; padding: 10px 14px; border-radius: 6px; font-family: monospace; font-size: 14px; resize: none; outline: none; }
  #input:focus { border-color: #6c7086; }
  #send { background: #313244; border: none; color: #cdd6f4; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-family: monospace; font-size: 14px; }
  #send:hover { background: #45475a; }
  #send:disabled { opacity: 0.4; cursor: not-allowed; }
  #token-bar { padding: 6px 20px; background: #111; border-bottom: 1px solid #1e1e2e; display: flex; gap: 10px; align-items: center; font-size: 12px; color: #585b70; }
  #token-input { background: #181825; border: 1px solid #313244; color: #888; padding: 3px 8px; border-radius: 4px; font-family: monospace; font-size: 12px; width: 220px; outline: none; }
  #show-memory { background: none; border: 1px solid #313244; color: #585b70; padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 12px; font-family: monospace; }
  #show-memory:hover { color: #888; }
</style>
</head>
<body>
<div id="header">
  <div id="status-dot"></div>
  <strong>🌟 Lumen</strong>
  <span id="model-label">connecting...</span>
</div>
<div id="token-bar">
  Access token: <input id="token-input" type="password" placeholder="leave empty for public access (level 1)">
  <button id="show-memory" onclick="loadMemories()">view memories</button>
  <span id="level-label"></span>
</div>
<div id="messages">
  <div class="msg system">Lumen memory-augmented chat. Your AI has access to extracted knowledge.</div>
</div>
<div id="input-area">
  <textarea id="input" rows="2" placeholder="Type a message... (Enter to send, Shift+Enter for newline)"></textarea>
  <button id="send" onclick="sendMessage()">Send</button>
</div>

<script>
  const messages = [];
  let thinking = false;

  async function checkHealth() {
    try {
      const r = await fetch('/health');
      const d = await r.json();
      document.getElementById('status-dot').className = 'ok';
      document.getElementById('model-label').textContent = d.model || 'ready';
    } catch(e) {
      document.getElementById('model-label').textContent = 'disconnected';
    }
  }

  function getToken() {
    return document.getElementById('token-input').value.trim();
  }

  function addMsg(role, text) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.textContent = text;
    document.getElementById('messages').appendChild(div);
    div.scrollIntoView({ behavior: 'smooth' });
    return div;
  }

  async function sendMessage() {
    if (thinking) return;
    const input = document.getElementById('input');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    addMsg('user', text);
    messages.push({ role: 'user', content: text });

    thinking = true;
    document.getElementById('send').disabled = true;
    const thinkingDiv = addMsg('system', '...');

    try {
      const r = await fetch('/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Lumen-Token': getToken()
        },
        body: JSON.stringify({ messages: messages })
      });

      const d = await r.json();
      thinkingDiv.remove();

      if (d.memory_context) {
        addMsg('memory', '🧠 ' + d.memory_context);
      }

      if (d.ok) {
        addMsg('assistant', d.content);
        messages.push({ role: 'assistant', content: d.content });
      } else {
        addMsg('system', '⚠ Error: ' + (d.error || 'unknown'));
      }
    } catch(e) {
      thinkingDiv.remove();
      addMsg('system', '⚠ Connection error');
    }

    thinking = false;
    document.getElementById('send').disabled = false;
    input.focus();
  }

  async function loadMemories() {
    const token = getToken();
    const r = await fetch('/memories?limit=20', {
      headers: { 'X-Lumen-Token': token }
    });
    const d = await r.json();
    if (d.memories) {
      const summary = d.memories.map(m =>
        `[L${m.security_level}] [${m.type}] ${m.content}`
      ).join('\n');
      addMsg('memory', '📚 Memories (level ' + (d.access_level||1) + '):\n' + (summary || 'none'));
    }
  }

  document.getElementById('input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  checkHealth();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────

class LumenHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _get_token(self) -> str:
        return self.headers.get("X-Lumen-Token", "").strip()

    def _resolve_access_level(self, token: str) -> int:
        config = self.server.config
        admin_token = config.get("server", {}).get("admin_token", "")

        if token and token == admin_token:
            return 3

        if not token:
            return config.get("server", {}).get("default_access_level", 1)

        level = self.server.memory.get_token_level(token)
        return level if level > 0 else 0  # 0 = invalid token

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self._html(CHAT_UI)

        elif path == "/health":
            config = self.server.config
            self._json({
                "status": "ok",
                "model": config["models"].get("extractor", "unknown"),
                "db": config["storage"]["database"],
                "time": datetime.now().isoformat()
            })

        elif path == "/memories":
            token = self._get_token()
            level = self._resolve_access_level(token)
            if level == 0:
                self._json({"error": "invalid token"}, 401)
                return

            query = params.get("q", [""])[0]
            limit = int(params.get("limit", [8])[0])
            memories = self.server.memory.search(query, max_level=level, limit=limit)
            self._json({"memories": memories, "access_level": level, "count": len(memories)})

        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/chat":
            self._handle_chat()

        elif path == "/v1/chat":
            self._handle_proxy()

        elif path == "/admin/token":
            self._handle_create_token()

        else:
            self._json({"error": "not found"}, 404)

    def _handle_chat(self):
        """Main chat endpoint — injects memories then calls Ollama."""
        token = self._get_token()
        level = self._resolve_access_level(token)

        if level == 0:
            self._json({"ok": False, "error": "invalid token"}, 401)
            return

        body = self._read_body()
        messages = body.get("messages", [])
        if not messages:
            self._json({"ok": False, "error": "no messages"}, 400)
            return

        config = self.server.config
        memory_db = self.server.memory

        # Get last user message for memory search
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )

        # Fetch relevant memories
        memories = memory_db.search(last_user, max_level=level, limit=8)
        context_summary = memory_db.get_context_summary()
        memory_block = memory_db.format_for_injection(memories, context_summary)

        # Build augmented message list
        augmented = list(messages)
        if memory_block:
            # Inject memory as system message at the start
            system_msg = {"role": "system", "content": memory_block}
            augmented = [system_msg] + augmented

        # Call Ollama
        ollama_host = config["models"].get("ollama_host", "http://localhost:11434")
        model = config["models"].get("extractor", "llama3.2:3b")

        # Use a different model for chat if specified
        chat_model = config["models"].get("chat", model)

        result = call_ollama(ollama_host, chat_model, augmented)

        response = {
            "ok": result["ok"],
            "content": result.get("content", ""),
            "error": result.get("error", ""),
            "memory_count": len(memories),
            "access_level": level
        }

        # Optionally include memory context summary in response for UI display
        if memories:
            short_summary = f"{len(memories)} memories injected (level ≤{level})"
            response["memory_context"] = short_summary

        self._json(response)

    def _handle_proxy(self):
        """OpenAI-compatible proxy — for external clients."""
        token = self._get_token()
        level = self._resolve_access_level(token)

        if level == 0:
            self._json({"error": "invalid token"}, 401)
            return

        body = self._read_body()
        messages = body.get("messages", [])

        config = self.server.config
        memory_db = self.server.memory

        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )

        memories = memory_db.search(last_user, max_level=level, limit=8)
        context_summary = memory_db.get_context_summary()
        memory_block = memory_db.format_for_injection(memories, context_summary)

        augmented = list(messages)
        if memory_block:
            augmented = [{"role": "system", "content": memory_block}] + augmented

        ollama_host = config["models"].get("ollama_host", "http://localhost:11434")
        model = body.get("model", config["models"].get("chat", config["models"].get("extractor")))

        result = call_ollama(ollama_host, model, augmented)

        # Return OpenAI-compatible format
        self._json({
            "id": "lumen-chat",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.get("content", "")
                },
                "finish_reason": "stop"
            }]
        })

    def _handle_create_token(self):
        """Admin endpoint to create access tokens."""
        token = self._get_token()
        config = self.server.config
        admin_token = config.get("server", {}).get("admin_token", "")

        if not admin_token or token != admin_token:
            self._json({"error": "admin access required"}, 403)
            return

        body = self._read_body()
        name = body.get("name", "unnamed")
        level = int(body.get("level", 1))

        if level < 1 or level > 3:
            self._json({"error": "level must be 1, 2, or 3"}, 400)
            return

        import secrets
        new_token = secrets.token_urlsafe(32)
        success = self.server.memory.storage.add_token(new_token, name, level)

        if success:
            self._json({"ok": True, "token": new_token, "name": name, "level": level})
        else:
            self._json({"error": "could not create token"}, 500)


# ─────────────────────────────────────────────────────────
# SERVER SETUP
# ─────────────────────────────────────────────────────────

class LumenMemoryServer:
    """Bundles memory query + storage reference for the HTTP server."""
    def __init__(self, config: dict):
        from lumen import LumenStorage
        db_path = config["storage"]["database"]
        self.storage = LumenStorage(db_path)
        self.conn = self.storage.conn
        self.conn.row_factory = sqlite3.Row
        self._storage_ref = self.storage

    def get_token_level(self, token: str) -> int:
        return self.storage.get_token_level(token)

    def search(self, query: str, max_level: int = 1, limit: int = 8) -> list:
        mq = MemoryQuery.__new__(MemoryQuery)
        mq.conn = self.conn
        return mq.search(query, max_level, limit)

    def get_context_summary(self) -> str:
        mq = MemoryQuery.__new__(MemoryQuery)
        mq.conn = self.conn
        return mq.get_context_summary()

    def format_for_injection(self, memories: list, context_summary: str = "") -> str:
        mq = MemoryQuery.__new__(MemoryQuery)
        mq.conn = self.conn
        return mq.format_for_injection(memories, context_summary)


class LumenServer(HTTPServer):
    def __init__(self, config: dict, *args, **kwargs):
        self.config = config
        self.memory = LumenMemoryServer(config)
        # Give handler access to storage for token creation
        self.memory.storage_ref = self.memory._storage_ref
        super().__init__(*args, **kwargs)


def main():
    config_path = DEFAULT_CONFIG
    port = 8000

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1

    if not os.path.exists(config_path):
        print(f"❌ Config not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)

    print("═" * 60)
    print("🌟 LUMEN SERVER")
    print("═" * 60)
    print(f"   Chat UI  : http://localhost:{port}")
    print(f"   Proxy    : http://localhost:{port}/v1/chat")
    print(f"   Model    : {config['models'].get('chat', config['models'].get('extractor'))}")
    print(f"   Database : {config['storage']['database']}")
    print("═" * 60)

    server = LumenServer(config, ("0.0.0.0", port), LumenHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("
⏸  Lumen server stopped")


if __name__ == "__main__":
    main()
