#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────
# Lumen — Local Universal Memory Extraction Node
# Copyright 2026 Mycosa. All rights reserved.
# License: See LICENSE.txt — attribution required,
#          commercial use requires written permission.
#          wu-reasoning.com
# ─────────────────────────────────────────────────────────
# lumen.py — Local Universal Memory Extraction Node
# Version 0.1
#
# Usage:
#   python lumen.py run          — start full pipeline (scan + ingest + extract)
#   python lumen.py scan         — speed scan only (quick context map)
#   python lumen.py ingest       — run one ingestion cycle
#   python lumen.py review       — show unpromoted memories
#   python lumen.py promote <id> — promote memory to confirmed
#   python lumen.py autopromote  — promote all high-confidence memories
#   python lumen.py status       — show ingestion progress and stats
#   python lumen.py export       — export memory to JSON

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, date
from pathlib import Path

import yaml

# ─────────────────────────────────────────────────────────
# CONFIG LOADER
# ─────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = "lumen_config.yaml"

def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        config = yaml.safe_load(f)
    return config

# ─────────────────────────────────────────────────────────
# STORAGE — self-contained SQLite
# ─────────────────────────────────────────────────────────

class LumenStorage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        print(f"💾 Lumen: storage ready ({db_path})")

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                content        TEXT NOT NULL,
                type           TEXT NOT NULL,
                confidence     REAL NOT NULL,
                tags           TEXT DEFAULT '[]',
                source         TEXT,
                promoted       INTEGER DEFAULT 0,
                security_level INTEGER DEFAULT 1,
                created_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access_tokens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token       TEXT NOT NULL UNIQUE,
                name        TEXT,
                level       INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_map (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                domain      TEXT,
                topics      TEXT,
                entities    TEXT,
                summary     TEXT,
                scanned_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingestion_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                page        INTEGER NOT NULL,
                total_pages INTEGER,
                status      TEXT DEFAULT 'pending',
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS learnings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                summary     TEXT,
                source      TEXT,
                created_at  TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def add_memory(self, content: str, memory_type: str, confidence: float,
                   tags: list = None, source: str = None,
                   security_level: int = 1) -> int:
        tags_json = json.dumps(tags or [])
        cursor = self.conn.execute(
            """INSERT INTO memories (content, type, confidence, tags, source, security_level, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (content, memory_type, confidence, tags_json, source,
             security_level, datetime.now().isoformat())
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_memories(self, memory_type: str = None, promoted: bool = None,
                     min_confidence: float = 0.0, max_security_level: int = 3) -> list:
        query = "SELECT * FROM memories WHERE confidence >= ? AND security_level <= ?"
        params = [min_confidence, max_security_level]
        if memory_type:
            query += " AND type = ?"
            params.append(memory_type)
        if promoted is not None:
            query += " AND promoted = ?"
            params.append(1 if promoted else 0)
        query += " ORDER BY confidence DESC, created_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def add_token(self, token: str, name: str, level: int) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO access_tokens (token, name, level, created_at)
                   VALUES (?, ?, ?, ?)""",
                (token, name, level, datetime.now().isoformat())
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def get_token_level(self, token: str) -> int:
        """Returns access level for token, or 0 if invalid."""
        row = self.conn.execute(
            "SELECT level FROM access_tokens WHERE token = ?", (token,)
        ).fetchone()
        return row["level"] if row else 0

    def list_tokens(self) -> list:
        rows = self.conn.execute(
            "SELECT id, name, level, created_at FROM access_tokens ORDER BY level DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def promote_memory(self, memory_id: int) -> bool:
        result = self.conn.execute(
            "UPDATE memories SET promoted = 1 WHERE id = ?", (memory_id,)
        )
        self.conn.commit()
        return result.rowcount > 0

    def save_context_map(self, source: str, domain: str, topics: list,
                         entities: list, summary: str):
        self.conn.execute(
            """INSERT INTO context_map (source, domain, topics, entities, summary, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source, domain, json.dumps(topics), json.dumps(entities),
             summary, datetime.now().isoformat())
        )
        self.conn.commit()

    def get_context_map(self, source: str = None) -> list:
        if source:
            rows = self.conn.execute(
                "SELECT * FROM context_map WHERE source = ? ORDER BY scanned_at DESC",
                (source,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM context_map ORDER BY scanned_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        promoted = self.conn.execute("SELECT COUNT(*) FROM memories WHERE promoted=1").fetchone()[0]
        by_type = self.conn.execute(
            "SELECT type, COUNT(*) as count FROM memories GROUP BY type"
        ).fetchall()
        return {
            "total_memories": total,
            "promoted": promoted,
            "unpromoted": total - promoted,
            "by_type": {r["type"]: r["count"] for r in by_type}
        }

    def export_json(self, path: str):
        memories = self.get_memories()
        context = self.get_context_map()
        data = {
            "exported_at": datetime.now().isoformat(),
            "stats": self.get_stats(),
            "context_maps": context,
            "memories": memories
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"📤 Lumen: exported to {path}")


# ─────────────────────────────────────────────────────────
# MODEL INTERFACE
# ─────────────────────────────────────────────────────────

class ModelInterface:
    def __init__(self, config: dict):
        self.provider = config["models"]["provider"]
        self.extractor = config["models"]["extractor"]
        self.summarizer = config["models"]["summarizer"]
        self.scanner = config["models"]["scanner"]
        self.ollama_host = config["models"].get("ollama_host", "http://localhost:11434")

    def run(self, model: str, prompt: str, timeout: int = 45) -> str:
        if self.provider == "ollama":
            return self._ollama(model, prompt, timeout)
        return ""

    def _ollama(self, model: str, prompt: str, timeout: int) -> str:
        try:
            env = os.environ.copy()
            env["OLLAMA_HOST"] = self.ollama_host
            result = subprocess.run(
                ["ollama", "run", model],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""

    def available(self, model: str) -> bool:
        try:
            result = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=5
            )
            return model.split(":")[0] in result.stdout
        except Exception:
            return False


# ─────────────────────────────────────────────────────────
# SPEED SCANNER
# ─────────────────────────────────────────────────────────

SCAN_PROMPT = """You are a document context analyzer. Read this excerpt and extract:
1. The main domain/field (e.g. "legal", "software development", "medical research")
2. Key topics (up to 5)
3. Key entities — people, projects, organizations, concepts (up to 8)
4. A one-sentence summary of what this document is about

Document excerpt:
{text}

Respond ONLY with valid JSON, no explanation:
{{
  "domain": "...",
  "topics": ["topic1", "topic2"],
  "entities": ["entity1", "entity2"],
  "summary": "..."
}}

JSON:"""

class SpeedScanner:
    def __init__(self, config: dict, storage: LumenStorage, model: ModelInterface):
        self.config = config["speed_scan"]
        self.storage = storage
        self.model = model
        self.scanner_model = model.scanner
        self.lines_per_chunk = self.config.get("lines_per_chunk", 50)
        self.max_chunks = self.config.get("max_chunks", 20)

    def scan(self, text: str, source: str) -> dict:
        """Quick pass over document to build context map."""
        print(f"⚡ Lumen: speed scanning {source}...")

        lines = text.split("
")
        # Sample chunks evenly across the document
        total_lines = len(lines)
        step = max(1, total_lines // self.max_chunks)

        chunks = []
        for i in range(0, total_lines, step):
            chunk = "
".join(lines[i:i + self.lines_per_chunk])
            chunks.append(chunk)
            if len(chunks) >= self.max_chunks:
                break

        # Aggregate results across chunks
        all_topics = []
        all_entities = []
        domains = []
        summaries = []

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            raw = self.model.run(
                self.scanner_model,
                SCAN_PROMPT.format(text=chunk[:1500]),
                timeout=30
            )

            parsed = self._parse_json(raw)
            if parsed:
                if parsed.get("domain"):
                    domains.append(parsed["domain"])
                all_topics.extend(parsed.get("topics", []))
                all_entities.extend(parsed.get("entities", []))
                if parsed.get("summary"):
                    summaries.append(parsed["summary"])

        # Deduplicate
        topics = list(dict.fromkeys(all_topics))[:10]
        entities = list(dict.fromkeys(all_entities))[:15]
        domain = max(set(domains), key=domains.count) if domains else "general"
        summary = summaries[0] if summaries else "No summary available."

        context = {
            "domain": domain,
            "topics": topics,
            "entities": entities,
            "summary": summary
        }

        self.storage.save_context_map(
            source=source,
            domain=domain,
            topics=topics,
            entities=entities,
            summary=summary
        )

        print(f"⚡ Lumen: scan complete")
        print(f"   Domain   : {domain}")
        print(f"   Topics   : {', '.join(topics[:5])}")
        print(f"   Entities : {', '.join(entities[:5])}")
        print(f"   Summary  : {summary[:100]}")

        return context

    def _parse_json(self, raw: str) -> dict:
        if not raw:
            return {}
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            return json.loads(raw.strip())
        except Exception:
            return {}


# ─────────────────────────────────────────────────────────
# EXTRACTOR (Pearl logic — generalized)
# ─────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a memory extraction system for the domain: {domain}
{domain_description}

Read this text chunk and extract ONLY genuinely memorable, reusable information.

MEMORY TYPES:
{memory_types}

TEXT CHUNK:
{text}

Extract up to {max_memories} memories. Output EXACTLY this JSON format (nothing else):
[
  {{
    "content": "The specific memorable fact or rule (1-2 sentences)",
    "type": "fact|preference|rule|decision|reminder|code|command|architecture",
    "confidence": 0.0-1.0,
    "tags": ["tag1", "tag2"]
  }}
]

Rules:
- Only extract information worth remembering long-term
- Do NOT extract pleasantries, filler, or obvious context
- If nothing is memorable, return: []
- Return ONLY valid JSON

JSON:"""

MEMORY_TYPE_DESCRIPTIONS = {
    "fact":       "fact        : Objective truth about the subject matter",
    "preference": "preference  : How the user or subject likes things done",
    "rule":       "rule        : A constraint or behavioral directive",
    "decision":   "decision    : A choice that was made and should be remembered",
    "reminder":   "reminder    : Something to check or follow up on"
}

class Extractor:
    def __init__(self, config: dict, storage: LumenStorage, model: ModelInterface):
        self.extraction_config = config["extraction"]
        self.storage = storage
        self.model = model
        self.extractor_model = model.extractor
        self.domain = self.extraction_config.get("domain", "general")
        self.domain_description = self.extraction_config.get("domain_description", "")
        self.memory_types = self.extraction_config.get("memory_types", ["fact", "preference", "rule", "decision", "reminder"])
        self.auto_promote_threshold = self.extraction_config.get("auto_promote_threshold", 0.85)
        self.min_store_threshold = self.extraction_config.get("min_store_threshold", 0.50)
        self.max_memories = self.extraction_config.get("max_memories_per_chunk", 3)

    def _auto_security_level(self, content: str, tags: list) -> int:
        """
        Heuristic security classification.
        Level 1 = public, Level 2 = internal, Level 3 = confidential.
        Users can always override manually via review.
        """
        content_lower = content.lower()
        tags_lower = [t.lower() for t in (tags or [])]

        confidential_signals = [
            "password", "secret", "token", "api key", "credential",
            "salary", "private", "confidential", "restricted",
            "personal", "ssn", "passport", "medical"
        ]
        internal_signals = [
            "internal", "employee", "team", "budget", "strategy",
            "roadmap", "decision", "project", "client", "customer"
        ]

        for signal in confidential_signals:
            if signal in content_lower or signal in tags_lower:
                return 3

        for signal in internal_signals:
            if signal in content_lower or signal in tags_lower:
                return 2

        return 1

    def extract(self, text: str, source: str = None) -> list:
        """Extract memories from a text chunk."""
        type_descriptions = "
".join([
            MEMORY_TYPE_DESCRIPTIONS[t]
            for t in self.memory_types
            if t in MEMORY_TYPE_DESCRIPTIONS
        ])

        prompt = EXTRACTION_PROMPT.format(
            domain=self.domain,
            domain_description=self.domain_description,
            memory_types=type_descriptions,
            text=text[:800],
            max_memories=self.max_memories
        )

        raw = self.model.run(self.extractor_model, prompt, timeout=30)
        candidates = self._parse_candidates(raw)

        stored = []
        for c in candidates:
            if c["confidence"] < self.min_store_threshold:
                continue
            if c["type"] not in self.memory_types:
                continue

            security_level = self._auto_security_level(c["content"], c.get("tags", []))

            mem_id = self.storage.add_memory(
                content=c["content"],
                memory_type=c["type"],
                confidence=c["confidence"],
                tags=c.get("tags", []),
                source=source,
                security_level=security_level
            )

            # Auto-promote high confidence memories
            if c["confidence"] >= self.auto_promote_threshold:
                self.storage.promote_memory(mem_id)
                c["auto_promoted"] = True
            else:
                c["auto_promoted"] = False

            c["id"] = mem_id
            c["security_level"] = security_level
            stored.append(c)

        return stored

    def _parse_candidates(self, raw: str) -> list:
        if not raw:
            return []
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            candidates = json.loads(raw.strip())
            if not isinstance(candidates, list):
                return []
            valid = []
            for c in candidates:
                if all(k in c for k in ["content", "type", "confidence"]):
                    if isinstance(c["confidence"], (int, float)):
                        valid.append(c)
            return valid
        except Exception:
            return []


# ─────────────────────────────────────────────────────────
# INPUT READERS
# ─────────────────────────────────────────────────────────

def read_text_file(path: str) -> str:
    with open(path, "r", errors="ignore") as f:
        return f.read()

def read_pdf(path: str) -> str:
    try:
        import pdfplumber
        text = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text.append(t)
        return "
".join(text)
    except ImportError:
        print("⚠️  Lumen: pdfplumber not installed — run 'pip install pdfplumber'")
        return ""
    except Exception as e:
        print(f"⚠️  Lumen: could not read PDF {path} — {e}")
        return ""

def fetch_url(url: str) -> str:
    try:
        import urllib.request
        headers = {"User-Agent": "Lumen/0.1 memory extractor"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8", errors="ignore")
        # Basic HTML strip
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        print(f"⚠️  Lumen: could not fetch {url} — {e}")
        return ""

def collect_sources(config: dict) -> list:
    """Returns list of (source_name, text) tuples from all enabled inputs."""
    sources = []
    inputs = config.get("inputs", {})

    # Text/markdown files
    file_config = inputs.get("files", {})
    if file_config.get("enabled", False):
        formats = file_config.get("formats", ["txt", "md"])
        recursive = file_config.get("recursive", False)
        for path_str in file_config.get("paths", []):
            path = Path(path_str)
            if path.is_file():
                text = read_text_file(str(path))
                if text:
                    sources.append((str(path), text))
            elif path.is_dir():
                pattern = "**/*" if recursive else "*"
                for fmt in formats:
                    for file in path.glob(f"{pattern}.{fmt}"):
                        text = read_text_file(str(file))
                        if text:
                            sources.append((str(file), text))

    # PDFs
    pdf_config = inputs.get("pdfs", {})
    if pdf_config.get("enabled", False):
        recursive = pdf_config.get("recursive", False)
        for path_str in pdf_config.get("paths", []):
            path = Path(path_str)
            if path.is_file() and path.suffix.lower() == ".pdf":
                text = read_pdf(str(path))
                if text:
                    sources.append((str(path), text))
            elif path.is_dir():
                pattern = "**/*.pdf" if recursive else "*.pdf"
                for file in path.glob(pattern):
                    text = read_pdf(str(file))
                    if text:
                        sources.append((str(file), text))

    # Websites
    web_config = inputs.get("websites", {})
    if web_config.get("enabled", False):
        for url in web_config.get("urls", []):
            text = fetch_url(url)
            if text:
                sources.append((url, text))

    return sources


# ─────────────────────────────────────────────────────────
# INGESTION STATE
# ─────────────────────────────────────────────────────────

class IngestionState:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"sources": {}}

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_cursor(self, source: str) -> int:
        return self.state["sources"].get(source, {}).get("cursor", 0)

    def set_cursor(self, source: str, cursor: int, total_pages: int = None):
        if source not in self.state["sources"]:
            self.state["sources"][source] = {}
        self.state["sources"][source]["cursor"] = cursor
        self.state["sources"][source]["last_fed"] = datetime.now().isoformat()
        if total_pages:
            self.state["sources"][source]["total_pages"] = total_pages
        self.save()

    def is_complete(self, source: str, total_pages: int) -> bool:
        cursor = self.get_cursor(source)
        return cursor >= total_pages


# ─────────────────────────────────────────────────────────
# LUMEN CORE
# ─────────────────────────────────────────────────────────

class Lumen:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config = load_config(config_path)
        self.storage = LumenStorage(self.config["storage"]["database"])
        self.model = ModelInterface(self.config)
        self.scanner = SpeedScanner(self.config, self.storage, self.model)
        self.extractor = Extractor(self.config, self.storage, self.model)
        self.ingest_config = self.config["ingestion"]
        self.state = IngestionState(self.ingest_config.get("state_file", ".lumen_state.json"))
        self.page_size = self.ingest_config.get("page_size_lines", 100)
        self.pages_per_hour = self.ingest_config.get("pages_per_hour", 10)
        print("🌟 Lumen initialized")

    def _paginate(self, text: str) -> list:
        lines = text.split("
")
        pages = []
        for i in range(0, len(lines), self.page_size):
            page = "
".join(lines[i:i + self.page_size])
            if page.strip():
                pages.append(page)
        return pages

    def scan_all(self):
        """Run speed scan on all sources."""
        sources = collect_sources(self.config)
        if not sources:
            print("⚠️  Lumen: no input sources found")
            return
        for source_name, text in sources:
            self.scanner.scan(text, source_name)

    def ingest_cycle(self) -> int:
        """Run one ingestion cycle — feed one page per source that has pending pages."""
        sources = collect_sources(self.config)
        total_extracted = 0

        for source_name, text in sources:
            pages = self._paginate(text)
            cursor = self.state.get_cursor(source_name)

            if cursor >= len(pages):
                continue  # This source is complete

            page = pages[cursor]
            memories = self.extractor.extract(page, source=source_name)
            total_extracted += len(memories)

            cursor += 1
            self.state.set_cursor(source_name, cursor, len(pages))

            if memories:
                print(f"📜 [{source_name}] page {cursor}/{len(pages)} → {len(memories)} memories")
            else:
                print(f"📜 [{source_name}] page {cursor}/{len(pages)} → nothing memorable")

        return total_extracted

    def run(self):
        """Full pipeline — scan then continuous ingestion."""
        print("
" + "═" * 60)
        print("🌟 LUMEN — Starting full pipeline")
        print("═" * 60 + "
")

        # Speed scan first
        if self.config["speed_scan"].get("enabled", True):
            print("Phase 1: Speed Scan")
            self.scan_all()
            print()

        # Ingestion loop
        print("Phase 2: Deep Ingestion")
        sleep_seconds = 3600 / self.pages_per_hour

        while True:
            try:
                n = self.ingest_cycle()

                # Check if all sources complete
                sources = collect_sources(self.config)
                all_done = all(
                    self.state.is_complete(src, len(self._paginate(txt)))
                    for src, txt in sources
                )
                if all_done:
                    print("
✅ Lumen: all sources fully ingested")
                    self.print_status()
                    break

                time.sleep(sleep_seconds)

            except KeyboardInterrupt:
                print("
⏸  Lumen: paused — progress saved")
                break
            except Exception as e:
                print(f"❌ Lumen: cycle error — {e}")
                time.sleep(10)

    def print_status(self):
        stats = self.storage.get_stats()
        print("
" + "═" * 60)
        print("🌟 LUMEN STATUS")
        print("═" * 60)
        print(f"  Total memories : {stats['total_memories']}")
        print(f"  Promoted       : {stats['promoted']}")
        print(f"  Pending review : {stats['unpromoted']}")
        if stats["by_type"]:
            print(f"  By type        : {stats['by_type']}")

        sources = collect_sources(self.config)
        print(f"
  Sources        : {len(sources)}")
        for source_name, text in sources:
            pages = self._paginate(text)
            cursor = self.state.get_cursor(source_name)
            pct = int(cursor / len(pages) * 100) if pages else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            print(f"  [{bar}] {pct}% {Path(source_name).name}")
        print("═" * 60 + "
")

    def review(self, memory_type: str = None):
        memories = self.storage.get_memories(memory_type=memory_type, promoted=False)
        if not memories:
            print("🌟 Lumen: no unpromoted memories")
            return

        type_icons = {
            "fact": "📌", "preference": "💜", "rule": "⚖️",
            "decision": "🎯", "reminder": "🔔"
        }

        by_type = {}
        for m in memories:
            by_type.setdefault(m["type"], []).append(m)

        print("
" + "═" * 60)
        print("🌟 LUMEN — MEMORIES FOR REVIEW")
        print("═" * 60)

        for mtype, items in sorted(by_type.items()):
            icon = type_icons.get(mtype, "•")
            print(f"
{icon} {mtype.upper()} ({len(items)})")
            for m in items:
                conf_bar = "█" * int(m["confidence"] * 10) + "░" * (10 - int(m["confidence"] * 10))
                print(f"  [{m['id']:3}] [{conf_bar}] {m['content'][:70]}")

        print(f"
{'═' * 60}")
        print("To promote: python lumen.py promote <id>")
        print("To promote all: python lumen.py autopromote")
        print("═" * 60 + "
")

    def autopromote(self, min_confidence: float = None):
        threshold = min_confidence or self.config["extraction"].get("auto_promote_threshold", 0.85)
        memories = self.storage.get_memories(promoted=False, min_confidence=threshold)
        promoted = 0
        for m in memories:
            if m["type"] == "fact":
                self.storage.promote_memory(m["id"])
                promoted += 1
        print(f"⬆️  Lumen: promoted {promoted} memories")


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def main():
    print("═" * 60)
    print("🌟 LUMEN — Local Universal Memory Extraction Node")
    print("═" * 60)

    config_path = DEFAULT_CONFIG_PATH
    # Allow custom config path: python lumen.py --config myconfig.yaml run
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = sys.argv[idx + 1]
            sys.argv.pop(idx)
            sys.argv.pop(idx)

    if not os.path.exists(config_path):
        print(f"❌ Config not found: {config_path}")
        print("   Copy lumen_config.yaml to your working directory and configure it.")
        sys.exit(1)

    lumen = Lumen(config_path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        lumen.run()

    elif cmd == "scan":
        lumen.scan_all()

    elif cmd == "ingest":
        n = lumen.ingest_cycle()
        print(f"✅ Extracted {n} memories this cycle")

    elif cmd == "review":
        mtype = sys.argv[2] if len(sys.argv) > 2 else None
        lumen.review(mtype)

    elif cmd == "promote":
        if len(sys.argv) < 3:
            print("Usage: python lumen.py promote <id>")
            return
        mem_id = int(sys.argv[2])
        success = lumen.storage.promote_memory(mem_id)
        print(f"✅ Memory {mem_id} promoted" if success else f"❌ Memory {mem_id} not found")

    elif cmd == "autopromote":
        min_conf = float(sys.argv[2]) if len(sys.argv) > 2 else None
        lumen.autopromote(min_conf)

    elif cmd == "status":
        lumen.print_status()

    elif cmd == "export":
        export_path = lumen.config["storage"].get("export_path", "./lumen_export.json")
        lumen.storage.export_json(export_path)

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: run | scan | ingest | review | promote <id> | autopromote | status | export")


if __name__ == "__main__":
    main()
