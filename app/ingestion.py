# -*- coding: utf-8 -*-
"""Event ingestion: JSONL import with explicit, auditable filtering rules.

Every input line ends in exactly one of two places:

* ``raw_events``  — an accepted event;
* ``excluded_events`` — a rejected event carrying the rule that fired.

Recognition rules (fixed and visible in the UI):

* **invalid** — unparseable timestamp, unknown event type, missing required
  fields, or an empty search query.
* **background** — the beacon is flagged ``is_background`` or its source is an
  automated channel (``api_health`` / ``suggest_prefetch``).
* **duplicate** — same ``event_id`` seen before, or the same business
  fingerprint (session, type, event time, query/product) already stored. This
  catches re-sent beacons that minted a fresh id.

Events are stored in arrival order but carry their own ``event_time``; the
derivation pipeline sorts by event time, so late-arriving (out-of-order)
events are handled correctly on the next rebuild.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .normalization import normalize_query

VALID_TYPES = {"search", "click", "add_to_cart", "order"}
BACKGROUND_SOURCES = {"api_health", "suggest_prefetch"}
REQUIRED = ("event_id", "event_time", "event_type", "session_id", "ab_version")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fingerprint(ev: dict) -> tuple:
    return (
        ev.get("session_id"),
        ev.get("event_type"),
        ev.get("event_time"),
        ev.get("query"),
        ev.get("product_id"),
        ev.get("tab_id"),
    )


def _validate(ev: dict) -> str | None:
    """Return an exclusion reason, or None when the event is valid."""
    for field in REQUIRED:
        if not ev.get(field):
            return "invalid: missing field " + field
    if ev["event_type"] not in VALID_TYPES:
        return "invalid: unknown event_type"
    if _parse_time(ev.get("event_time")) is None:
        return "invalid: unparseable event_time"
    etype = ev["event_type"]
    if etype == "search":
        if not isinstance(ev.get("query"), str) or not ev["query"].strip():
            return "invalid: empty search query"
        rc = ev.get("result_count")
        if not isinstance(rc, int) or rc < 0:
            return "invalid: result_count must be a non-negative integer"
    if etype in ("click", "add_to_cart", "order") and not ev.get("product_id"):
        return "invalid: missing product_id"
    return None


def import_jsonl(conn: sqlite3.Connection, path: str | Path) -> dict[str, int]:
    path = Path(path)
    known_ids = {
        r["event_id"]
        for r in conn.execute("SELECT event_id FROM raw_events")
    } | {r["event_id"] for r in conn.execute("SELECT event_id FROM excluded_events")}
    known_prints = {
        (r["session_id"], r["event_type"], r["event_time"], r["query_raw"],
         r["product_id"], r["tab_id"])
        for r in conn.execute(
            "SELECT session_id, event_type, event_time, query_raw, product_id, tab_id "
            "FROM raw_events"
        )
    }

    counts = {"processed": 0, "inserted": 0, "duplicate": 0,
              "background": 0, "invalid": 0}
    batch_prints: set[tuple] = set()

    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            counts["processed"] += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                ev = {"event_id": f"__bad_json_{path.name}_{line_no}"}
                reason = "invalid: malformed JSON"
                if ev["event_id"] not in known_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO excluded_events"
                        "(event_id, rule, detail, payload) VALUES (?,?,?,?)",
                        (ev["event_id"], "invalid", reason, line),
                    )
                    known_ids.add(ev["event_id"])
                counts["invalid"] += 1
                continue

            event_id = str(ev.get("event_id") or f"__missing_id_{line_no}")
            reason = _validate(ev)

            if reason is None and (ev.get("is_background") or
                                   ev.get("source") in BACKGROUND_SOURCES):
                reason = "background: " + (
                    ev.get("source") if ev.get("source") in BACKGROUND_SOURCES
                    else "is_background flag"
                )

            fp = _fingerprint(ev) if reason is None else None
            if reason is None and (event_id in known_ids or fp in known_prints
                                   or fp in batch_prints):
                reason = "duplicate: repeated beacon"

            if reason is not None:
                rule = reason.split(":", 1)[0]
                counts[rule] = counts.get(rule, 0) + 1
                if event_id not in known_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO excluded_events"
                        "(event_id, rule, detail, payload) VALUES (?,?,?,?)",
                        (event_id, rule, reason, json.dumps(ev, ensure_ascii=False)),
                    )
                    known_ids.add(event_id)
                continue

            etype = ev["event_type"]
            query_raw = ev.get("query")
            conn.execute(
                "INSERT INTO raw_events(event_id, event_time, ingest_time, event_type,"
                " session_id, tab_id, ab_version, query_raw, query_norm, result_count,"
                " product_id, position, amount, is_background, source, client_ts_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    _parse_time(ev["event_time"]).isoformat(),
                    ev.get("ingest_time") or _now(),
                    etype,
                    ev["session_id"],
                    ev.get("tab_id") or "t0",
                    ev["ab_version"],
                    query_raw,
                    normalize_query(query_raw) if query_raw is not None else None,
                    ev.get("result_count"),
                    ev.get("product_id"),
                    ev.get("position"),
                    ev.get("amount"),
                    1 if ev.get("is_background") else 0,
                    ev.get("source") or "web",
                    ev.get("client_ts_ms"),
                ),
            )
            known_ids.add(event_id)
            known_prints.add(fp)
            batch_prints.add(fp)
            counts["inserted"] += 1

    conn.commit()
    return counts


def exclusion_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rule, COUNT(*) AS n FROM excluded_events GROUP BY rule ORDER BY n DESC"
    ).fetchall()
