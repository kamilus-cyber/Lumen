# Lumen — Local Universal Memory Extraction Node

> Your knowledge. Your machine. Your AI.

Lumen ingests your documents, emails, and web pages — extracts structured
knowledge using a local AI model — and gives any AI assistant persistent,
searchable memory. No cloud. No API. No data leaving your machine.

**3,031 memories from 1,373 emails. Zero bytes sent to cloud.**

---

## What it does

1. **Ingest** — Text files, PDFs, emails, websites — fed at a controlled pace
2. **Extract** — Local AI model extracts facts, decisions, names, amounts, action items
3. **Store** — Structured memory in local SQLite with 3-tier access control
4. **Serve** — Memory-augmented chat UI or proxy endpoint for any AI client

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) running locally
- `pip install pyyaml pdfplumber` (pdfplumber optional, for PDF support)
- 4GB VRAM minimum (for llama3.2:3b)

---

## Quick start

```bash
# 1. Copy config and edit it
cp lumen_config.yaml my_lumen_config.yaml

# 2. IMPORTANT: Change admin_token in the config before running
#    server:
#      admin_token: "your-secret-token-here"

# 3. Run the full pipeline (scan + ingest + extract)
python lumen.py run

# 4. Start the chat server
python lumen_server.py --port 8000

# 5. Open browser at http://localhost:8000
```

---

## CLI commands

```
python lumen.py run          — full pipeline (scan + ingest + extract)
python lumen.py scan         — speed scan only (quick context map)
python lumen.py ingest       — run one ingestion cycle
python lumen.py review       — show unpromoted memories for review
python lumen.py promote <id> — promote a specific memory
python lumen.py autopromote  — promote all high-confidence memories
python lumen.py status       — show ingestion progress and stats
python lumen.py export       — export memory to JSON
```

---

## Security notes

- **Change `admin_token`** in `lumen_config.yaml` before exposing the server
  to any network. The default is a placeholder — it is not secure.
- The server binds to `0.0.0.0` by default — accessible on all interfaces.
  For local-only use, modify to `127.0.0.1` in `lumen_server.py`.
- Access levels (1=public, 2=internal, 3=confidential) control which memories
  are served to which tokens. Configure carefully for sensitive domains.

---

## Who it's for

Built for GDPR-sensitive European organisations that need AI-augmented
knowledge management without sending client data to third-party servers.

Law firms · Medical practices · Financial advisors ·
Engineering firms · Government contractors · Research institutions

---

## License

Copyright 2026 Mycosa. See [LICENSE.txt](LICENSE.txt).

Attribution required. Commercial use requires written permission.
Contact: wu-reasoning.com

---

## Part of the Wu ecosystem

Lumen is part of a broader ecosystem of local-first AI tools built on
geometric reasoning principles. See wu-reasoning.com for the full picture.
