# -*- coding: utf-8 -*-
"""Aggregation layer. All metrics are computed from derived tables so the UI
and the session drill-down always reconcile to the same numbers.

Rate metrics (recovery / click / add-to-cart / conversion) are computed over
fully observed nodes only — sessions that ended before the fixed window closes
are counted separately, never silently dropped or treated as failures.
"""
from __future__ import annotations

import sqlite3
from typing import Any

_RATE_DENOM = "is_zero_result = 1 AND window_observed = 1"


def _filters(params: dict[str, Any]) -> tuple[str, list]:
    where, binds = ["1=1"], []
    if params.get("date_from"):
        where.append("event_time >= ?")
        binds.append(params["date_from"])
    if params.get("date_to"):
        where.append("event_time <= ?")
        binds.append(params["date_to"])
    if params.get("ab_version"):
        where.append("ab_version = ?")
        binds.append(params["ab_version"])
    if params.get("word_class"):
        where.append("word_class = ?")
        binds.append(params["word_class"])
    if params.get("mapping_version_id"):
        where.append(
            "(SELECT mapping_version_id FROM query_paths p WHERE p.path_id = query_nodes.path_id) = ?"
        )
        binds.append(int(params["mapping_version_id"]))
    return " AND ".join(where), binds


def overview(conn: sqlite3.Connection, params: dict[str, Any]) -> dict:
    where, binds = _filters(params)
    total_nodes = conn.execute(
        f"SELECT COUNT(*) n FROM query_nodes WHERE {where}", binds
    ).fetchone()["n"]
    zero_all = conn.execute(
        f"SELECT COUNT(*) n FROM query_nodes WHERE {where} AND is_zero_result = 1",
        binds,
    ).fetchone()["n"]
    zero_obs = conn.execute(
        f"SELECT COUNT(*) n FROM query_nodes WHERE {where} AND {_RATE_DENOM}",
        binds,
    ).fetchone()["n"]
    zero_incomplete = conn.execute(
        f"SELECT COUNT(*) n FROM query_nodes WHERE {where}"
        " AND is_zero_result = 1 AND window_observed = 0",
        binds,
    ).fetchone()["n"]

    rate_row = conn.execute(
        f"""SELECT
              COALESCE(SUM(found_results),0)   AS found,
              COALESCE(SUM(has_click),0)       AS clicks,
              COALESCE(SUM(has_add_to_cart),0) AS carts,
              COALESCE(SUM(has_conversion),0)  AS orders
            FROM query_nodes WHERE {where} AND {_RATE_DENOM}""",
        binds,
    ).fetchone()
    fallback = conn.execute(
        f"SELECT COUNT(*) n FROM query_nodes WHERE {where} AND is_fallback = 1",
        binds,
    ).fetchone()["n"]
    paths = conn.execute(
        "SELECT COUNT(*) n FROM query_paths p WHERE EXISTS "
        f"(SELECT 1 FROM query_nodes n WHERE n.path_id = p.path_id AND {where})",
        binds,
    ).fetchone()["n"]
    sessions = conn.execute(
        f"SELECT COUNT(DISTINCT session_id) n FROM query_nodes WHERE {where}",
        binds,
    ).fetchone()["n"]

    def pct(num: int) -> float:
        return round(100.0 * num / zero_obs, 2) if zero_obs else 0.0

    return {
        "paths": paths,
        "sessions": sessions,
        "search_nodes": total_nodes,
        "zero_result_nodes": zero_all,
        "zero_result_rate_pct": round(100.0 * zero_all / total_nodes, 2) if total_nodes else 0.0,
        "observed_zero_nodes": zero_obs,
        "incomplete_zero_nodes": zero_incomplete,
        "fallback_nodes": fallback,
        "recovery_rate_pct": pct(rate_row["found"]),
        "click_rate_pct": pct(rate_row["clicks"]),
        "cart_rate_pct": pct(rate_row["carts"]),
        "conversion_rate_pct": pct(rate_row["orders"]),
    }


def _breakdown(conn: sqlite3.Connection, params: dict, column: str) -> list[dict]:
    where, binds = _filters(params)
    rows = conn.execute(
        f"""
        SELECT {column} AS bucket,
               COUNT(*) AS total_nodes,
               SUM(is_zero_result) AS zero_nodes,
               SUM(CASE WHEN {_RATE_DENOM} THEN 1 ELSE 0 END) AS observed_zero,
               SUM(CASE WHEN is_zero_result = 1 AND window_observed = 0
                        THEN 1 ELSE 0 END) AS incomplete_zero,
               SUM(CASE WHEN {_RATE_DENOM} THEN found_results ELSE 0 END) AS recovered,
               SUM(CASE WHEN {_RATE_DENOM} THEN has_click ELSE 0 END) AS clicks,
               SUM(CASE WHEN {_RATE_DENOM} THEN has_add_to_cart ELSE 0 END) AS carts,
               SUM(CASE WHEN {_RATE_DENOM} THEN has_conversion ELSE 0 END) AS orders,
               SUM(is_fallback) AS fallbacks
        FROM query_nodes WHERE {where}
        GROUP BY {column} ORDER BY total_nodes DESC""",
        binds,
    ).fetchall()
    out = []
    for r in rows:
        denom = r["observed_zero"] or 0
        out.append({
            "bucket": r["bucket"],
            "total_nodes": r["total_nodes"],
            "zero_nodes": r["zero_nodes"],
            "observed_zero_nodes": r["observed_zero"],
            "incomplete_zero_nodes": r["incomplete_zero"],
            "zero_rate_pct": round(100.0 * r["zero_nodes"] / r["total_nodes"], 2),
            "recovery_rate_pct": round(100.0 * r["recovered"] / denom, 2) if denom else None,
            "click_rate_pct": round(100.0 * r["clicks"] / denom, 2) if denom else None,
            "cart_rate_pct": round(100.0 * r["carts"] / denom, 2) if denom else None,
            "conversion_rate_pct": round(100.0 * r["orders"] / denom, 2) if denom else None,
            "fallback_nodes": r["fallbacks"],
        })
    return out


def by_word_class(conn, params):
    return _breakdown(conn, params, "word_class")


def by_ab_version(conn, params):
    return _breakdown(conn, params, "ab_version")


def top_zero_queries(conn, params, limit=20):
    where, binds = _filters(params)
    rows = conn.execute(
        f"""
        SELECT canonical_query AS query, query_raw, word_class, ab_version,
               COUNT(*) AS zero_nodes,
               SUM(CASE WHEN window_observed = 1 THEN 1 ELSE 0 END) AS observed,
               SUM(CASE WHEN window_observed = 1 THEN found_results ELSE 0 END) AS recovered,
               SUM(CASE WHEN window_observed = 1 THEN has_click ELSE 0 END) AS clicks,
               SUM(CASE WHEN window_observed = 1 THEN has_conversion ELSE 0 END) AS orders
        FROM query_nodes WHERE {where} AND is_zero_result = 1
        GROUP BY canonical_query ORDER BY zero_nodes DESC LIMIT ?""",
        binds + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def timeseries(conn, params, days=14):
    where, binds = _filters(params)
    rows = conn.execute(
        f"""
        SELECT substr(event_time, 1, 10) AS day,
               COUNT(*) AS searches,
               SUM(is_zero_result) AS zero_nodes,
               SUM(CASE WHEN {_RATE_DENOM} THEN 1 ELSE 0 END) AS observed_zero,
               SUM(CASE WHEN {_RATE_DENOM} THEN found_results ELSE 0 END) AS recovered,
               SUM(CASE WHEN {_RATE_DENOM} THEN has_conversion ELSE 0 END) AS orders
        FROM query_nodes WHERE {where}
        GROUP BY day ORDER BY day""",
        binds,
    ).fetchall()
    out = []
    for r in rows:
        denom = r["observed_zero"] or 0
        out.append({
            "day": r["day"],
            "searches": r["searches"],
            "zero_nodes": r["zero_nodes"],
            "zero_rate_pct": round(100.0 * r["zero_nodes"] / r["searches"], 2),
            "recovery_rate_pct": round(100.0 * r["recovered"] / denom, 2) if denom else None,
            "conversion_rate_pct": round(100.0 * r["orders"] / denom, 2) if denom else None,
        })
    return out


def list_paths(conn, params, limit=100, offset=0):
    where, binds = _filters(params)
    if params.get("has_zero"):
        where += " AND p.has_zero_result = 1"
    if params.get("incomplete_only"):
        where += " AND p.session_observed = 0"
    total = conn.execute(
        "SELECT COUNT(*) n FROM query_paths p WHERE EXISTS "
        f"(SELECT 1 FROM query_nodes n WHERE n.path_id = p.path_id AND {where})",
        binds,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT p.* FROM query_paths p
            WHERE EXISTS (SELECT 1 FROM query_nodes n WHERE n.path_id = p.path_id AND {where})
            ORDER BY p.start_time DESC LIMIT ? OFFSET ?""",
        binds + [limit, offset],
    ).fetchall()
    paths = []
    for r in rows:
        nodes = conn.execute(
            "SELECT * FROM query_nodes WHERE path_id = ? ORDER BY seq", (r["path_id"],)
        ).fetchall()
        paths.append({"path": dict(r), "nodes": [dict(n) for n in nodes]})
    return {"total": total, "paths": paths}


def session_detail(conn, session_id: str) -> dict:
    path_rows = conn.execute(
        "SELECT * FROM query_paths WHERE session_id = ? ORDER BY start_time", (session_id,)
    ).fetchall()
    paths = []
    for pr in path_rows:
        nodes = conn.execute(
            "SELECT * FROM query_nodes WHERE path_id = ? ORDER BY seq", (pr["path_id"],)
        ).fetchall()
        paths.append({"path": dict(pr), "nodes": [dict(n) for n in nodes]})
    events = conn.execute(
        "SELECT * FROM derived_events WHERE session_id = ? ORDER BY event_time", (session_id,)
    ).fetchall()
    raw = conn.execute(
        "SELECT event_id, event_time, event_type, tab_id, query_raw, result_count,"
        " product_id, position, amount, source FROM raw_events"
        " WHERE session_id = ? ORDER BY event_time",
        (session_id,),
    ).fetchall()
    return {
        "session_id": session_id,
        "paths": paths,
        "derived_events": [dict(e) for e in events],
        "raw_events": [dict(e) for e in raw],
    }


# Metric name -> SQL expression used for the click-through drill-down.
DRILL_METRICS = {
    "recovery": ("found_results", "is_zero_result = 1 AND window_observed = 1"),
    "click": ("has_click", "is_zero_result = 1 AND window_observed = 1"),
    "cart": ("has_add_to_cart", "is_zero_result = 1 AND window_observed = 1"),
    "conversion": ("has_conversion", "is_zero_result = 1 AND window_observed = 1"),
    "zero": ("is_zero_result", "1=1"),
    "incomplete": ("1 - window_observed", "is_zero_result = 1"),
    "fallback": ("is_fallback", "1=1"),
}


def drill_sessions(conn, params: dict, metric: str) -> list[dict]:
    """Return the sessions behind a clickable metric, with per-node counts."""
    if metric not in DRILL_METRICS:
        raise ValueError(f"unknown metric {metric}")
    col, predicate = DRILL_METRICS[metric]
    where, binds = _filters(params)
    rows = conn.execute(
        f"""
        SELECT session_id,
               COUNT(*) AS matching_nodes,
               MIN(event_time) AS first_seen,
               MAX(event_time) AS last_seen
        FROM query_nodes
        WHERE {where} AND {predicate} AND {col} = 1
        GROUP BY session_id ORDER BY matching_nodes DESC, first_seen LIMIT 200""",
        binds,
    ).fetchall()
    return [dict(r) for r in rows]
