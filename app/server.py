# -*- coding: utf-8 -*-
"""Stdlib HTTP server: JSON API under /api/* + static frontend from web/.

Run with:  python3 run.py   (or python3 -m app.server)
No third-party dependencies — http.server, sqlite3, json only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import mapping as mapping_mod
from . import metrics
from .db import connect, get_config, init_db, set_config
from .ingestion import exclusion_summary, import_jsonl
from .pipeline import rebuild

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "app" / "web"
DB_PATH = os.environ.get("SEARCH_DB", str(ROOT / "data" / "analysis.db"))
IMPORT_DIR = ROOT / "data"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def get_conn() -> sqlite3.Connection:
    conn = connect(DB_PATH)
    init_db(conn)
    return conn


def _params(query: str) -> dict:
    parsed = parse_qs(query)
    return {k: v[0] for k, v in parsed.items() if v}


class Handler(BaseHTTPRequestHandler):
    server_version = "SearchReformulation/1.0"

    # ---------------------------------------------------------------- helpers
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # quieter console
        pass

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, url.query
        try:
            if path.startswith("/api/"):
                return self._api_get(path, _params(query))
            return self._static(path)
        except Exception as exc:  # pragma: no cover - defensive
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path.startswith("/api/"):
                return self._api_post(url.path, self._body())
            self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            self._json({"error": str(exc)}, 500)

    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR)) or not target.is_file():
            target = WEB_DIR / "index.html"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ GET handlers
    def _api_get(self, path: str, p: dict):
        conn = get_conn()
        try:
            if path == "/api/overview":
                return self._json(metrics.overview(conn, p))
            if path == "/api/breakdown/word_class":
                return self._json(metrics.by_word_class(conn, p))
            if path == "/api/breakdown/ab_version":
                return self._json(metrics.by_ab_version(conn, p))
            if path == "/api/timeseries":
                return self._json(metrics.timeseries(conn, p))
            if path == "/api/top_zero_queries":
                return self._json(metrics.top_zero_queries(conn, p))
            if path == "/api/paths":
                limit = int(p.pop("limit", 100))
                offset = int(p.pop("offset", 0))
                return self._json(metrics.list_paths(conn, p, limit, offset))
            if path == "/api/session":
                return self._json(metrics.session_detail(conn, p["session_id"]))
            if path == "/api/drill":
                return self._json(metrics.drill_sessions(conn, p, p["metric"]))
            if path == "/api/mapping/versions":
                return self._json(mapping_mod.list_versions(conn))
            if path == "/api/mapping/entries":
                return self._json(
                    mapping_mod.list_entries(conn, int(p["version_id"])))
            if path == "/api/word_class_rules":
                return self._json(mapping_mod.list_word_class_rules(conn))
            if path == "/api/config":
                return self._json(get_config(conn))
            if path == "/api/quality":
                rows = exclusion_summary(conn)
                total_raw = conn.execute(
                    "SELECT COUNT(*) n FROM raw_events").fetchone()["n"]
                return self._json({
                    "excluded": [dict(r) for r in rows],
                    "accepted_events": total_raw,
                    "excluded_events": conn.execute(
                        "SELECT COUNT(*) n FROM excluded_events").fetchone()["n"],
                    "examples": [dict(r) for r in conn.execute(
                        "SELECT event_id, rule, detail FROM excluded_events"
                        " ORDER BY rule LIMIT 50")],
                })
            if path == "/api/filters":
                return self._json({
                    "word_classes": [r["word_class"] for r in conn.execute(
                        "SELECT DISTINCT word_class FROM query_nodes ORDER BY 1")],
                    "ab_versions": [r["ab_version"] for r in conn.execute(
                        "SELECT DISTINCT ab_version FROM query_paths ORDER BY 1")],
                    "mapping_versions": mapping_mod.list_versions(conn),
                })
            self._json({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()

    # ----------------------------------------------------------- POST handlers
    def _api_post(self, path: str, body: dict):
        conn = get_conn()
        try:
            if path == "/api/import":
                file_name = os.path.basename(body.get("file", "events.jsonl"))
                target = (IMPORT_DIR / file_name).resolve()
                if not str(target).startswith(str(IMPORT_DIR)) or not target.is_file():
                    raise ValueError(f"file not found in data dir: {file_name}")
                counts = import_jsonl(conn, target)
                rebuild(conn)
                return self._json({"import": counts, "pipeline": _pipeline_summary(conn)})

            if path == "/api/rebuild":
                return self._json(rebuild(conn))

            if path == "/api/config":
                set_config(conn, body)
                rebuild(conn)
                return self._json({"ok": True, "config": get_config(conn)})

            if path == "/api/mapping/clone":
                vid = mapping_mod.clone_active(
                    conn, body["label"], body.get("note", ""))
                return self._json({"version_id": vid})

            if path == "/api/mapping/entry":
                mapping_mod.upsert_entry(
                    conn, int(body["version_id"]),
                    body["source_term"], body["target_term"])
                return self._json({"ok": True})

            if path == "/api/mapping/entry/delete":
                mapping_mod.delete_entry(
                    conn, int(body["version_id"]), int(body["mapping_id"]))
                return self._json({"ok": True})

            if path == "/api/mapping/activate":
                before = metrics.overview(conn, {})
                mapping_mod.activate(conn, int(body["version_id"]))
                rebuild(conn)
                after = metrics.overview(conn, {})
                return self._json({"before": before, "after": after})

            if path == "/api/word_class_rule":
                mapping_mod.upsert_word_class_rule(
                    conn, body["keyword"], body["word_class"],
                    int(body.get("rule_order", 100)))
                rebuild(conn)
                return self._json({"ok": True})

            if path == "/api/word_class_rule/delete":
                mapping_mod.delete_word_class_rule(conn, int(body["rule_id"]))
                rebuild(conn)
                return self._json({"ok": True})

            self._json({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()


def _pipeline_summary(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) paths FROM query_paths").fetchone()
    nodes = conn.execute(
        "SELECT COUNT(*) n FROM query_nodes").fetchone()["n"]
    return {"paths": row["paths"], "nodes": nodes}


def main(port: int = 8000) -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Search reformulation analytics -> http://127.0.0.1:{port}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main(int(os.environ.get("PORT", "8000")))
