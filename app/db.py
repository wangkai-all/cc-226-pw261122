# -*- coding: utf-8 -*-
"""SQLite storage layer for the query-reformulation analysis project.

The schema keeps *raw* facts (imported events) strictly separate from *derived*
facts (paths, node-level metrics). Every derived table can be rebuilt from the
raw tables, which makes mapping edits a clean rebuild instead of a patch.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------------
-- Raw imported events. One row per accepted event; the original query text is
-- preserved verbatim in query_raw while query_norm holds the canonical form.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_events (
    event_id        TEXT PRIMARY KEY,
    event_time      TEXT NOT NULL,          -- ISO-8601 UTC, event time (not ingest time)
    ingest_time     TEXT NOT NULL,          -- ISO-8601 UTC, when the file was imported
    event_type      TEXT NOT NULL,          -- search | click | add_to_cart | order
    session_id      TEXT NOT NULL,
    tab_id          TEXT,                   -- browser tab; NULL => session's primary tab
    ab_version      TEXT NOT NULL,          -- A | B (experiment bucket of the session)
    query_raw       TEXT,                   -- original wording, never overwritten
    query_norm      TEXT,                   -- whitespace/case canonical form
    result_count    INTEGER,                -- search only
    product_id      TEXT,                   -- click/add_to_cart/order
    position        INTEGER,                -- result position for clicks
    amount          REAL,                   -- order only
    is_background   INTEGER NOT NULL DEFAULT 0,  -- headless/auto request flag
    source          TEXT NOT NULL DEFAULT 'web', -- web | api_health | suggest_prefetch
    client_ts_ms    INTEGER                 -- client timestamp for dedup fallback
);

-- Events rejected during ingestion, with the explicit rule that rejected them.
CREATE TABLE IF NOT EXISTS excluded_events (
    event_id    TEXT PRIMARY KEY,
    rule        TEXT NOT NULL,              -- duplicate | background | invalid
    detail      TEXT,
    payload     TEXT NOT NULL               -- original JSON, for audit
);

-- Editable configuration (observation window, idle gap, thresholds).
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Explicit, page-maintained synonym mapping. version_id groups published
-- snapshots; is_active marks the single snapshot used when deriving paths.
CREATE TABLE IF NOT EXISTS mapping_versions (
    version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    note         TEXT,
    is_active    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mapping_entries (
    mapping_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL REFERENCES mapping_versions(version_id),
    source_term   TEXT NOT NULL,            -- normalized term/query
    target_term   TEXT NOT NULL,            -- normalized canonical term
    UNIQUE(version_id, source_term)
);

-- Editable word-class rules. First matching keyword wins (rule_order); a query
-- matching none falls into the "other" class. Explicit mapping only.
CREATE TABLE IF NOT EXISTS word_class_rules (
    rule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL UNIQUE,       -- normalized substring
    word_class  TEXT NOT NULL,
    rule_order  INTEGER NOT NULL DEFAULT 100
);

-- ---------------------------------------------------------------------------
-- Derived: query paths. A path is a chain of searches within one session+tab,
-- split when the gap between consecutive searches exceeds the idle limit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_paths (
    path_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT NOT NULL,
    tab_id             TEXT NOT NULL,
    ab_version         TEXT NOT NULL,
    mapping_version_id INTEGER NOT NULL,
    start_time         TEXT NOT NULL,
    end_time           TEXT NOT NULL,
    node_count         INTEGER NOT NULL,
    has_zero_result    INTEGER NOT NULL,
    session_observed   INTEGER NOT NULL     -- session had >= observation window after its first event
);
CREATE INDEX IF NOT EXISTS idx_paths_session ON query_paths(session_id);

-- One row per search node inside a path.
CREATE TABLE IF NOT EXISTS query_nodes (
    node_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path_id              INTEGER NOT NULL REFERENCES query_paths(path_id),
    session_id           TEXT NOT NULL,
    tab_id               TEXT NOT NULL,
    ab_version           TEXT NOT NULL,
    seq                  INTEGER NOT NULL,   -- 0-based position inside the path
    event_id             TEXT NOT NULL,
    event_time           TEXT NOT NULL,
    query_raw            TEXT NOT NULL,
    query_norm           TEXT NOT NULL,
    canonical_query      TEXT NOT NULL,       -- after explicit synonym mapping
    word_class           TEXT NOT NULL,
    result_count         INTEGER NOT NULL,
    is_zero_result       INTEGER NOT NULL,
    is_fallback          INTEGER NOT NULL DEFAULT 0, -- broader term after a zero-result node
    -- observation of the fixed window starting at this search
    window_observed      INTEGER NOT NULL,
    incomplete_reason    TEXT,                -- NULL | truncated_window | session_ended_early
    -- outcome flags, attributed within the observation window (session scope,
    -- clicks/carts/orders may arrive on another tab)
    found_results        INTEGER NOT NULL DEFAULT 0,
    has_click            INTEGER NOT NULL DEFAULT 0,
    has_add_to_cart      INTEGER NOT NULL DEFAULT 0,
    has_conversion       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON query_nodes(path_id);
CREATE INDEX IF NOT EXISTS idx_nodes_zero ON query_nodes(is_zero_result, window_observed);

-- Derived non-search events retained for session detail views.
CREATE TABLE IF NOT EXISTS derived_events (
    event_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    tab_id      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    product_id  TEXT,
    position    INTEGER,
    amount      REAL
);
CREATE INDEX IF NOT EXISTS idx_dev_session ON derived_events(session_id);
"""

DEFAULT_CONFIG = {
    "observation_window_minutes": "30",
    "idle_gap_minutes": "30",
    "min_fallback_result_count": "20",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def get_config(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config")}


def set_config(conn: sqlite3.Connection, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        conn.execute(
            "INSERT INTO config(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    conn.commit()
