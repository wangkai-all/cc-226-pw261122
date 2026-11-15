# -*- coding: utf-8 -*-
"""Synonym mapping and word-class rule management.

Mappings are versioned snapshots: the active version is what the derivation
pipeline uses. Editing means cloning the active snapshot into a new version,
changing entries there, then activating it and rebuilding derived tables. The
old versions remain available, so the UI can diff metrics across versions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .normalization import normalize_query


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mapping_versions ORDER BY version_id"
    ).fetchall()
    out = []
    for r in rows:
        n = conn.execute(
            "SELECT COUNT(*) n FROM mapping_entries WHERE version_id = ?",
            (r["version_id"],),
        ).fetchone()["n"]
        d = dict(r)
        d["entry_count"] = n
        out.append(d)
    return out


def list_entries(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM mapping_entries WHERE version_id = ? "
        "ORDER BY source_term", (version_id,))]


def active_version_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT version_id FROM mapping_versions WHERE is_active = 1"
    ).fetchone()
    return row["version_id"] if row else None


def clone_active(conn: sqlite3.Connection, label: str, note: str = "") -> int:
    src = active_version_id(conn)
    cur = conn.execute(
        "INSERT INTO mapping_versions(label, created_at, note, is_active)"
        " VALUES (?,?,?,0)", (label, _now(), note),
    )
    new_id = cur.lastrowid
    if src is not None:
        conn.execute(
            "INSERT INTO mapping_entries(version_id, source_term, target_term)"
            " SELECT ?, source_term, target_term FROM mapping_entries WHERE version_id = ?",
            (new_id, src),
        )
    conn.commit()
    return new_id


def upsert_entry(conn: sqlite3.Connection, version_id: int,
                 source_term: str, target_term: str) -> None:
    src = normalize_query(source_term)
    tgt = normalize_query(target_term)
    if not src or not tgt:
        raise ValueError("source and target must be non-empty after normalization")
    conn.execute(
        "INSERT INTO mapping_entries(version_id, source_term, target_term) VALUES (?,?,?)"
        " ON CONFLICT(version_id, source_term) DO UPDATE SET target_term = excluded.target_term",
        (version_id, src, tgt),
    )
    conn.commit()


def delete_entry(conn: sqlite3.Connection, version_id: int, mapping_id: int) -> None:
    conn.execute(
        "DELETE FROM mapping_entries WHERE mapping_id = ? AND version_id = ?",
        (mapping_id, version_id),
    )
    conn.commit()


def activate(conn: sqlite3.Connection, version_id: int) -> None:
    exists = conn.execute(
        "SELECT 1 FROM mapping_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if not exists:
        raise ValueError("unknown version_id")
    conn.execute("UPDATE mapping_versions SET is_active = 0")
    conn.execute(
        "UPDATE mapping_versions SET is_active = 1 WHERE version_id = ?", (version_id,)
    )
    conn.commit()


# ---------------------------------------------------------------- word classes

def list_word_class_rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM word_class_rules ORDER BY rule_order, keyword")]


def upsert_word_class_rule(conn: sqlite3.Connection, keyword: str,
                           word_class: str, rule_order: int = 100) -> None:
    kw = normalize_query(keyword)
    if not kw or not word_class.strip():
        raise ValueError("keyword and word_class must be non-empty")
    conn.execute(
        "INSERT INTO word_class_rules(keyword, word_class, rule_order) VALUES (?,?,?)"
        " ON CONFLICT(keyword) DO UPDATE SET word_class = excluded.word_class,"
        " rule_order = excluded.rule_order",
        (kw, word_class.strip(), int(rule_order)),
    )
    conn.commit()


def delete_word_class_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM word_class_rules WHERE rule_id = ?", (rule_id,))
    conn.commit()
