# -*- coding: utf-8 -*-
"""Derivation pipeline: turn accepted raw events into query paths and metrics.

Rebuildable by construction — every derived table is deleted and recomputed
from ``raw_events`` plus the currently active mapping version. Editing the
synonym mapping therefore produces a clean before/after comparison.

Path rules
----------
* Searches are grouped by ``(session_id, tab_id)`` and ordered by event time.
  Two searches more than ``idle_gap_minutes`` apart start a new path.
* Cross-tab behavior is explicit: path identity is per tab, but clicks /
  add-to-cart / order events are attributed at **session scope**, so a
  conversion reached on a second tab still credits the originating search.

Observation rules
-----------------
* The observation window is fixed at ``observation_window_minutes`` (30) from
  each search's event time.
* A node is *observed* only when the session has an event at or beyond the
  window end. Sessions ending earlier are marked with an explicit
  ``incomplete_reason`` and are excluded from rate metrics by default.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import timedelta

from .normalization import (
    canonicalize,
    classify,
    load_mapping,
    load_word_class_rules,
)


def _dt(row: sqlite3.Row):
    from datetime import datetime
    return datetime.fromisoformat(row["event_time"])


def _active_mapping_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT version_id FROM mapping_versions WHERE is_active = 1"
    ).fetchone()
    if row:
        return row["version_id"]
    row = conn.execute(
        "SELECT version_id FROM mapping_versions ORDER BY version_id LIMIT 1"
    ).fetchone()
    if row:
        return row["version_id"]
    cur = conn.execute(
        "INSERT INTO mapping_versions(label, created_at, note, is_active) "
        "VALUES ('v1 初始映射', datetime('now'), '初始版本', 1)"
    )
    return cur.lastrowid


def rebuild(conn: sqlite3.Connection) -> dict:
    from datetime import datetime

    cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config")}
    window = timedelta(minutes=int(cfg["observation_window_minutes"]))
    idle_gap = timedelta(minutes=int(cfg["idle_gap_minutes"]))
    fallback_min = int(cfg["min_fallback_result_count"])

    mapping_version_id = _active_mapping_version(conn)
    mapping = load_mapping(conn, mapping_version_id)
    class_rules = load_word_class_rules(conn)

    conn.executescript(
        "DELETE FROM query_nodes; DELETE FROM query_paths; DELETE FROM derived_events;"
    )

    events = conn.execute(
        "SELECT * FROM raw_events ORDER BY event_time, event_id"
    ).fetchall()
    if not events:
        conn.commit()
        return {"paths": 0, "nodes": 0, "observed_zero_nodes": 0}

    data_end = max(_dt(e) for e in events)

    # Session-level time bounds drive the completeness decision.
    sess_first: dict[str, datetime] = {}
    sess_last: dict[str, datetime] = {}
    searches: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    other_events: dict[str, list[sqlite3.Row]] = defaultdict(list)

    for ev in events:
        dt = _dt(ev)
        sid = ev["session_id"]
        sess_first[sid] = min(sess_first.get(sid, dt), dt)
        sess_last[sid] = max(sess_last.get(sid, dt), dt)
        if ev["event_type"] == "search":
            searches[(sid, ev["tab_id"])].append(ev)
        else:
            other_events[sid].append(ev)
            conn.execute(
                "INSERT OR IGNORE INTO derived_events"
                "(event_id, session_id, tab_id, event_type, event_time,"
                " product_id, position, amount) VALUES (?,?,?,?,?,?,?,?)",
                (ev["event_id"], sid, ev["tab_id"], ev["event_type"],
                 ev["event_time"], ev["product_id"], ev["position"], ev["amount"]),
            )

    path_count = node_count = observed_zero = 0

    for (sid, tab), rows in sorted(searches.items()):
        rows.sort(key=lambda r: (r["event_time"], r["event_id"]))
        # Split into paths on the idle gap.
        chains: list[list[sqlite3.Row]] = []
        current: list[sqlite3.Row] = []
        prev_dt = None
        for r in rows:
            dt = _dt(r)
            if prev_dt is not None and dt - prev_dt > idle_gap:
                chains.append(current)
                current = []
            current.append(r)
            prev_dt = dt
        if current:
            chains.append(current)

        session_end = sess_last[sid]
        session_events = other_events.get(sid, [])

        for chain in chains:
            cur = conn.execute(
                "INSERT INTO query_paths(session_id, tab_id, ab_version,"
                " mapping_version_id, start_time, end_time, node_count,"
                " has_zero_result, session_observed) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid, tab, chain[0]["ab_version"], mapping_version_id,
                    chain[0]["event_time"], chain[-1]["event_time"], len(chain),
                    1 if any(r["result_count"] == 0 for r in chain) else 0,
                    1 if sess_last[sid] >= sess_first[sid] + window else 0,
                ),
            )
            path_id = cur.lastrowid
            path_count += 1

            prev_was_zero = False
            for seq, r in enumerate(chain):
                dt = _dt(r)
                win_end = dt + window
                if session_end >= win_end:
                    observed, reason = 1, None
                elif data_end < win_end:
                    observed, reason = 0, "truncated_window"
                else:
                    observed, reason = 0, "session_ended_early"

                rc = r["result_count"]
                is_zero = 1 if rc == 0 else 0
                # A fallback is a broader rewording that succeeds right after a
                # zero-result node: results return above the configured floor.
                is_fallback = 1 if (prev_was_zero and not is_zero and rc >= fallback_min) else 0
                prev_was_zero = bool(is_zero)

                # Recovery: a later rewrite in the SAME path finds results while
                # this node's observation window is still open.
                found_after = 0
                if is_zero:
                    for later in chain[seq + 1:]:
                        later_dt = _dt(later)
                        if later_dt > win_end:
                            break
                        if later["result_count"] > 0:
                            found_after = 1
                            break

                # Outcome attribution: session-scope events within the fixed
                # window, regardless of the tab on which they occurred.
                found = found_after if is_zero else 1
                has_click = has_cart = has_order = 0
                for oe in session_events:
                    o_dt = _dt(oe)
                    if dt < o_dt <= win_end:
                        if oe["event_type"] == "click":
                            has_click = 1
                        elif oe["event_type"] == "add_to_cart":
                            has_cart = 1
                        elif oe["event_type"] == "order":
                            has_order = 1

                q_norm = r["query_norm"]
                canonical = canonicalize(q_norm, mapping)
                word_class = classify(q_norm, class_rules)

                conn.execute(
                    "INSERT INTO query_nodes(path_id, session_id, tab_id, ab_version,"
                    " seq, event_id, event_time, query_raw, query_norm, canonical_query,"
                    " word_class, result_count, is_zero_result, is_fallback,"
                    " window_observed, incomplete_reason, found_results, has_click,"
                    " has_add_to_cart, has_conversion)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        path_id, sid, tab, r["ab_version"], seq, r["event_id"],
                        r["event_time"], r["query_raw"], q_norm, canonical,
                        word_class, rc, is_zero, is_fallback, observed, reason,
                        found, has_click, has_cart, has_order,
                    ),
                )
                node_count += 1
                if is_zero and observed:
                    observed_zero += 1

    conn.commit()
    return {"paths": path_count, "nodes": node_count,
            "observed_zero_nodes": observed_zero,
            "mapping_version_id": mapping_version_id}
