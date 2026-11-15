# -*- coding: utf-8 -*-
"""End-to-end behavioral tests on a temporary database.

Run: python3 -I -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db
from app.ingestion import import_jsonl
from app.mapping import (
    activate, clone_active, upsert_entry, upsert_word_class_rule,
)
from app.normalization import canonicalize, classify, normalize_query
from app.pipeline import rebuild
from app import metrics

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def eid(i):
    return f"e{i}"


def write_jsonl(rows):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                      encoding="utf-8")
    for r in rows:
        tmp.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.close()
    return Path(tmp.name)


def base(event_id, dt, etype, sid, **kw):
    row = {
        "event_id": event_id, "event_time": iso(dt), "event_type": etype,
        "session_id": sid, "tab_id": kw.pop("tab", "t1"),
        "ab_version": kw.pop("ab", "A"), "source": kw.pop("source", "web"),
    }
    row.update(kw)
    return row


def search(i, dt, sid, query, rc, **kw):
    return base(eid(i), dt, "search", sid, query=query, result_count=rc, **kw)


def click(i, dt, sid, pid="P1", tab="t1", **kw):
    return base(eid(i), dt, "click", sid, product_id=pid, tab=tab, **kw)


def order(i, dt, sid, pid="P1", tab="t1", **kw):
    return base(eid(i), dt, "order", sid, product_id=pid, tab=tab,
                amount=99.0, **kw)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = connect(self.tmp.name)
        init_db(self.conn)
        upsert_word_class_rule(self.conn, "鞋", "shoes", 10)

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def build(self, rows):
        path = write_jsonl(rows)
        counts = import_jsonl(self.conn, path)
        rebuild(self.conn)
        return counts

    def node(self, sid, seq):
        return self.conn.execute(
            "SELECT * FROM query_nodes WHERE session_id=? AND seq=?",
            (sid, seq)).fetchone()

    def test_normalization_mechanical_only(self):
        self.assertEqual(normalize_query("  蓝牙   耳机 "), "蓝牙 耳机")
        self.assertEqual(normalize_query("ABC"), "abc")
        self.assertEqual(normalize_query(None), "")

    def test_synonym_only_through_explicit_mapping(self):
        # Without a mapping entry, canonical equals normalized.
        self.assertEqual(canonicalize("跑鞋", {}), "跑鞋")
        vid = clone_active(self.conn, "t", "")
        upsert_entry(self.conn, vid, "跑鞋", "运动鞋")
        activate(self.conn, vid)
        self.assertEqual(canonicalize("跑鞋", {"跑鞋": "运动鞋"}), "运动鞋")
        # token-level: unmapped tokens pass through
        self.assertEqual(canonicalize("红色 跑鞋", {"跑鞋": "运动鞋"}),
                         "红色 运动鞋")

    def test_word_class_default_other_and_rule_match(self):
        rules = [("鞋", "shoes")]
        self.assertEqual(classify("跑鞋", rules), "shoes")
        self.assertEqual(classify("火星机票", rules), "other")

    def test_duplicate_and_background_rules(self):
        rows = [
            search(1, T0, "s", "鞋", 0),
            dict(search(2, T0, "s", "鞋", 0), event_id="e2-resend"),  # fingerprint dup, fresh id
            search(1, T0, "s", "鞋", 0),                               # id dup
            base("e3", T0, "search", "s", query="预热",
                 result_count=1, source="suggest_prefetch"),
            base("e4", T0, "search", "s", query="后台",
                 result_count=1, is_background=True),
            base("e5", T0, "search", "s", query="", result_count=0),
        ]
        counts = self.build(rows)
        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(counts["duplicate"], 2)
        self.assertEqual(counts["background"], 2)
        self.assertEqual(counts["invalid"], 1)

    def test_zero_chain_recovery_click_conversion_observed(self):
        rows = [
            search(1, T0, "s", "火星机票", 0),
            search(2, T0 + timedelta(minutes=3), "s", "航天书", 40),
            click(3, T0 + timedelta(minutes=6), "s"),
            order(4, T0 + timedelta(minutes=12), "s"),
            click(5, T0 + timedelta(minutes=33), "s"),  # closes the window
        ]
        self.build(rows)
        n0 = self.node("s", 0)
        self.assertEqual(n0["is_zero_result"], 1)
        self.assertEqual(n0["window_observed"], 1)
        self.assertEqual(n0["found_results"], 1)       # recovered via rewrite
        self.assertEqual(n0["has_click"], 1)
        self.assertEqual(n0["has_conversion"], 1)

    def test_late_arriving_out_of_order_conversion(self):
        # File order is deliberately shuffled; event_time must drive attribution.
        rows = [
            order(9, T0 + timedelta(minutes=20), "s"),
            click(8, T0 + timedelta(minutes=8), "s"),
            search(2, T0 + timedelta(minutes=4), "s", "拖鞋", 30),
            search(1, T0, "s", "会飞的拖鞋", 0),
            click(7, T0 + timedelta(minutes=33), "s"),
        ]
        self.build(rows)
        n0 = self.node("s", 0)
        self.assertEqual(n0["has_conversion"], 1)
        self.assertEqual(n0["found_results"], 1)

    def test_truncated_session_flagged_not_counted_as_failure(self):
        rows = [
            search(1, T0, "s", "永动机", 0),
            click(2, T0 + timedelta(minutes=10), "s"),  # only 10 min in
        ]
        self.build(rows)
        n0 = self.node("s", 0)
        self.assertEqual(n0["window_observed"], 0)
        self.assertIn(n0["incomplete_reason"],
                      ("session_ended_early", "truncated_window"))
        ov = metrics.overview(self.conn, {})
        self.assertEqual(ov["incomplete_zero_nodes"], 1)
        # rates computed over observed nodes only; denominator 0 -> 0.0
        self.assertEqual(ov["observed_zero_nodes"], 0)

    def test_cross_tab_path_split_but_session_attribution(self):
        rows = [
            search(1, T0, "s", "折叠手机", 0, tab="t1"),
            search(2, T0 + timedelta(minutes=3), "s", "折叠屏", 50, tab="t1"),
            click(3, T0 + timedelta(minutes=9), "s", tab="t2"),
            order(4, T0 + timedelta(minutes=15), "s", tab="t2"),
            click(5, T0 + timedelta(minutes=33), "s", tab="t1"),
        ]
        self.build(rows)
        # only one path, on t1 — t2 events never form a search path
        paths = self.conn.execute(
            "SELECT DISTINCT tab_id FROM query_paths WHERE session_id='s'").fetchall()
        self.assertEqual([r["tab_id"] for r in paths], ["t1"])
        n0 = self.node("s", 0)
        self.assertEqual(n0["has_click"], 1)
        self.assertEqual(n0["has_conversion"], 1)

    def test_idle_gap_splits_paths(self):
        rows = [
            search(1, T0, "s", "鞋", 5),
            search(2, T0 + timedelta(minutes=45), "s", "帽", 5),
            click(3, T0 + timedelta(minutes=70), "s"),
        ]
        self.build(rows)
        n_paths = self.conn.execute(
            "SELECT COUNT(*) c FROM query_paths WHERE session_id='s'").fetchone()["c"]
        self.assertEqual(n_paths, 2)

    def test_fallback_detection(self):
        rows = [
            search(1, T0, "s", "蓝牙耳机 降噪 限量", 0),
            search(2, T0 + timedelta(minutes=2), "s", "蓝牙耳机", 86),
            click(3, T0 + timedelta(minutes=33), "s"),
        ]
        self.build(rows)
        self.assertEqual(self.node("s", 1)["is_fallback"], 1)
        # a narrow result set below the floor is not a fallback
        rows2 = [
            search(4, T0, "s2", "x 限定", 0),
            search(5, T0 + timedelta(minutes=2), "s2", "x", 3),
            click(6, T0 + timedelta(minutes=33), "s2"),
        ]
        self.build(rows2)
        self.assertEqual(self.node("s2", 1)["is_fallback"], 0)

    def test_mapping_activation_rebuilds_and_differs_in_grouping(self):
        rows = [
            search(1, T0, "s1", "跑鞋", 10),
            search(2, T0, "s2", "运动鞋", 10),
        ]
        self.build(rows)
        before = self.conn.execute(
            "SELECT COUNT(DISTINCT canonical_query) c FROM query_nodes").fetchone()["c"]
        self.assertEqual(before, 2)
        vid = clone_active(self.conn, "merge", "")
        upsert_entry(self.conn, vid, "跑鞋", "运动鞋")
        activate(self.conn, vid)
        rebuild(self.conn)
        after = self.conn.execute(
            "SELECT COUNT(DISTINCT canonical_query) c FROM query_nodes").fetchone()["c"]
        self.assertEqual(after, 1)
        # raw wording preserved
        raw = self.conn.execute(
            "SELECT DISTINCT query_raw FROM query_nodes ORDER BY 1").fetchall()
        self.assertEqual([r["query_raw"] for r in raw], ["跑鞋", "运动鞋"])

    def test_drill_returns_sessions_behind_metric(self):
        rows = [
            search(1, T0, "s1", "火星机票", 0),
            search(2, T0 + timedelta(minutes=2), "s1", "航天书", 30),
            click(3, T0 + timedelta(minutes=33), "s1"),
        ]
        self.build(rows)
        sessions = metrics.drill_sessions(self.conn, {}, "recovery")
        self.assertEqual([r["session_id"] for r in sessions], ["s1"])
        with self.assertRaises(ValueError):
            metrics.drill_sessions(self.conn, {}, "nope")

    def test_ab_filter_and_denominator_discipline(self):
        rows = [
            search(1, T0, "sa", "鞋 绝版", 0, ab="A"),
            click(2, T0 + timedelta(minutes=33), "sa", ab="A"),
            search(3, T0, "sb", "帽 绝版", 0, ab="B"),
            # B session ends early -> incomplete, excluded from rates
        ]
        self.build(rows)
        ov_a = metrics.overview(self.conn, {"ab_version": "A"})
        ov_b = metrics.overview(self.conn, {"ab_version": "B"})
        self.assertEqual(ov_a["observed_zero_nodes"], 1)
        self.assertEqual(ov_b["observed_zero_nodes"], 0)
        self.assertEqual(ov_b["incomplete_zero_nodes"], 1)


if __name__ == "__main__":
    unittest.main()
