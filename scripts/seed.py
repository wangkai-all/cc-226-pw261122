# -*- coding: utf-8 -*-
"""One-shot bootstrap: create the database, seed the explicit mapping and
word-class rules, import data/events.jsonl, and build derived tables.

Run:  python3 -I scripts/seed.py
Re-runnable: mapping seeding is idempotent per label.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect, init_db  # noqa: E402
from app.ingestion import import_jsonl  # noqa: E402
from app.normalization import normalize_query  # noqa: E402
from app.pipeline import rebuild  # noqa: E402

DB = ROOT / "data" / "analysis.db"
EVENTS = ROOT / "data" / "events.jsonl"

# Explicit synonym mapping — the ONLY place synonym merging comes from.
INITIAL_MAPPING = [
    ("跑鞋", "运动鞋"),
    ("篮球鞋", "运动鞋"),
    ("口红", "唇膏"),
    ("t恤", "T恤".casefold()),
    ("蓝牙耳機", "蓝牙耳机"),
]

# Explicit word-class rules; first substring match wins.
WORD_CLASSES = [
    ("鞋", "shoes", 10),
    ("耳机|键盘|显示器|充电宝|数据线", "electronics", 20),
    ("连衣裙|t恤|外套|羽绒服|运动裤|裤", "apparel", 30),
    ("口红|唇膏|粉底|防晒|面膜|香水", "beauty", 40),
    ("保温杯|四件套|台灯|收纳|香薰", "home", 50),
]


def seed() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(DB)
    init_db(conn)

    exists = conn.execute("SELECT 1 FROM mapping_versions LIMIT 1").fetchone()
    if not exists:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO mapping_versions(label, created_at, note, is_active)"
            " VALUES ('v1 初始映射', ?, '页面维护的明确同义词', 1)", (now,))
        vid = cur.lastrowid
        for src, tgt in INITIAL_MAPPING:
            conn.execute(
                "INSERT INTO mapping_entries(version_id, source_term, target_term)"
                " VALUES (?,?,?)", (vid, normalize_query(src), normalize_query(tgt)))
    else:
        print("mapping already seeded, skipping")

    for pattern, wc, order in WORD_CLASSES:
        for kw in pattern.split("|"):
            conn.execute(
                "INSERT OR IGNORE INTO word_class_rules(keyword, word_class, rule_order)"
                " VALUES (?,?,?)", (normalize_query(kw), wc, order))
    conn.commit()

    if EVENTS.exists():
        counts = import_jsonl(conn, EVENTS)
        print("import:", counts)
    summary = rebuild(conn)
    print("pipeline:", summary)
    conn.close()


if __name__ == "__main__":
    seed()
