# -*- coding: utf-8 -*-
"""Deterministic synthetic event generator.

Produces data/events.jsonl covering every scenario the project must analyze,
with no search engine and no language model involved:

* hand-crafted sessions for the edge cases — consecutive zero-result rewrites,
  fallback queries, late-arriving (out-of-order) conversion, cross-tab
  conversion, truncated observation, duplicate beacons, background requests;
* a larger set of randomized sessions so the aggregate charts have volume and
  the two experiment buckets (A/B) differ measurably.

Run:  python3 scripts/generate_data.py
The JSONL is written in deliberately shuffled order so the ingestion layer
must rely on event_time rather than file order (late arrival is real here).
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

RNG = random.Random(20261115)
OUT = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
BASE_DAY = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)

# Query pools per word class. Some pairs are deliberate synonyms that the
# seeded explicit mapping will merge (e.g. 运动鞋 / 跑鞋 -> 运动鞋).
POOL = {
    "apparel":     ["红色连衣裙", "白色T恤", "牛仔外套", "羽绒服", "运动裤"],
    "shoes":       ["运动鞋", "跑鞋", "皮鞋", "凉鞋", "篮球鞋"],
    "electronics": ["蓝牙耳机", "机械键盘", "显示器", "充电宝", "数据线"],
    "home":        ["保温杯", "四件套", "台灯", "收纳盒", "香薰"],
    "beauty":      ["口红", "粉底液", "防晒霜", "面膜", "香水"],
}
# Queries the synthetic catalog cannot satisfy — always zero results.
IMPOSSIBLE = ["火星机票", "透明胶带 防水 耐高温 超长", "会飞的拖鞋",
              "100寸 折叠 手机", "永动机 模型"]


def ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def ev(event_id, dt, etype, sid, tab, ab, **kw):
    row = {
        "event_id": event_id,
        "event_time": ts(dt),
        "event_type": etype,
        "session_id": sid,
        "tab_id": tab,
        "ab_version": ab,
        "source": kw.pop("source", "web"),
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------- hand-crafted
def hand_crafted() -> list[dict]:
    rows: list[dict] = []
    day = BASE_DAY

    # S1: consecutive zero results -> recovery -> click -> add-to-cart -> order
    s, t, ab = "s1-zero-chain", "t1", "A"
    t0 = day.replace(hour=9, minute=5)
    chain = [("火星机票", 0), ("火星 机票 往返", 0), ("航天 科普 书", 42)]
    for i, (q, rc) in enumerate(chain):
        rows.append(ev(f"s1-search-{i}", t0 + timedelta(minutes=3 * i),
                       "search", s, t, ab, query=q, result_count=rc))
    rows.append(ev("s1-click", t0 + timedelta(minutes=11), "click", s, t, ab,
                   product_id="P-1001", position=2))
    rows.append(ev("s1-cart", t0 + timedelta(minutes=14), "add_to_cart", s, t, ab,
                   product_id="P-1001"))
    rows.append(ev("s1-order", t0 + timedelta(minutes=20), "order", s, t, ab,
                   product_id="P-1001", amount=129.0))
    # late session activity closes the 30-minute observation window
    rows.append(ev("s1-tail", t0 + timedelta(minutes=34), "click", s, t, ab,
                   product_id="P-1002", position=8))

    # S2: zero -> fallback to a broader term that returns many results, click.
    s, t, ab = "s2-fallback", "t1", "B"
    t0 = day.replace(hour=10, minute=20)
    rows.append(ev("s2-q0", t0, "search", s, t, ab,
                   query="蓝牙耳机 降噪 挂耳式 骨传导 限量", result_count=0))
    rows.append(ev("s2-q1", t0 + timedelta(minutes=2), "search", s, t, ab,
                   query="蓝牙耳机", result_count=86))  # fallback (broad, >=20)
    rows.append(ev("s2-click", t0 + timedelta(minutes=6), "click", s, t, ab,
                   product_id="P-2002", position=1))
    rows.append(ev("s2-tail", t0 + timedelta(minutes=33), "click", s, t, ab,
                   product_id="P-2003", position=4))

    # S3: zero result, then LATE conversion 24 min later — file order will put
    # it before earlier events; only event_time sorting can attribute it.
    s, t, ab = "s3-late-conv", "t1", "A"
    t0 = day.replace(hour=11, minute=2)
    rows.append(ev("s3-q0", t0, "search", s, t, ab,
                   query="会飞的拖鞋", result_count=0))
    rows.append(ev("s3-q1", t0 + timedelta(minutes=4), "search", s, t, ab,
                   query="拖鞋 夏季", result_count=35))
    rows.append(ev("s3-click", t0 + timedelta(minutes=8), "click", s, t, ab,
                   product_id="P-3003", position=3))
    rows.append(ev("s3-order-late", t0 + timedelta(minutes=27), "order", s, t, ab,
                   product_id="P-3003", amount=49.9))
    rows.append(ev("s3-tail", t0 + timedelta(minutes=35), "click", s, t, ab,
                   product_id="P-3004", position=6))

    # S4: truncated session — last event is only 12 min after the zero-result
    # search, so the fixed 30-min window is unobservable (session ended early).
    s, t, ab = "s4-truncated", "t1", "B"
    t0 = day.replace(hour=14, minute=40)
    rows.append(ev("s4-q0", t0, "search", s, t, ab,
                   query="永动机 模型", result_count=0))
    rows.append(ev("s4-q1", t0 + timedelta(minutes=5), "search", s, t, ab,
                   query="物理 实验 模型", result_count=11))
    rows.append(ev("s4-click", t0 + timedelta(minutes=12), "click", s, t, ab,
                   product_id="P-4004", position=5))

    # S5: cross-tab — search on t1, conversion reached on t2; session scope
    # must still credit the search, but t2 events never join the t1 path.
    s, ab = "s5-cross-tab", "A"
    t0 = day.replace(hour=15, minute=30)
    rows.append(ev("s5-q0", t0, "search", s, "t1", ab,
                   query="100寸 折叠 手机", result_count=0))
    rows.append(ev("s5-q1", t0 + timedelta(minutes=3), "search", s, "t1", ab,
                   query="折叠屏 手机", result_count=54))
    rows.append(ev("s5-click-tab2", t0 + timedelta(minutes=9), "click", s, "t2", ab,
                   product_id="P-5005", position=1))
    rows.append(ev("s5-order-tab2", t0 + timedelta(minutes=15), "order", s, "t2", ab,
                   product_id="P-5005", amount=4999.0))
    # tail on t1: window is observable session-wide even though the order came from t2
    rows.append(ev("s5-tail", t0 + timedelta(minutes=33), "click", s, "t1", ab,
                   product_id="P-5006", position=2))

    # S6: normalization surface — case/whitespace variants of the same query;
    # raw text is preserved, normalized form merges them.
    s, t, ab = "s6-normalize", "t1", "B"
    t0 = day.replace(hour=16, minute=0)
    rows.append(ev("s6-q0", t0, "search", s, t, ab,
                   query="  蓝牙耳机   ", result_count=73))
    rows.append(ev("s6-q1", t0 + timedelta(minutes=2), "search", s, t, ab,
                   query="蓝牙耳机", result_count=71))
    rows.append(ev("s6-q2", t0 + timedelta(minutes=4), "search", s, t, ab,
                   query="蓝牙 耳机", result_count=0))
    rows.append(ev("s6-tail", t0 + timedelta(minutes=35), "click", s, t, ab,
                   product_id="P-6006", position=3))

    # S7: duplicate beacons — same event resubmitted with a fresh id (caught by
    # fingerprint) plus one exact event_id repeat.
    s, t, ab = "s7-dup", "t1", "A"
    t0 = day.replace(hour=17, minute=10)
    rows.append(ev("s7-q0", t0, "search", s, t, ab,
                   query="透明胶带 防水 耐高温 超长", result_count=0))
    rows.append(ev("s7-q0-dup-id", t0, "search", s, t, ab,
                   query="透明胶带 防水 耐高温 超长", result_count=0))  # fingerprint dup
    rows.append(ev("s7-q0", t0 + timedelta(seconds=5), "search", s, t, ab,
                   query="透明胶带 防水 耐高温 超长", result_count=0))  # id dup
    rows.append(ev("s7-q1", t0 + timedelta(minutes=3), "search", s, t, ab,
                   query="透明胶带", result_count=28))
    rows.append(ev("s7-tail", t0 + timedelta(minutes=34), "click", s, t, ab,
                   product_id="P-7007", position=1))

    # S8: background / automated traffic — must be excluded, never in a path.
    s, t, ab = "s8-bg", "t1", "A"
    t0 = day.replace(hour=18, minute=0)
    rows.append(ev("s8-bg-prefetch", t0, "search", s, t, ab,
                   query="热门推荐", result_count=10, source="suggest_prefetch"))
    rows.append(ev("s8-bg-flag", t0 + timedelta(minutes=1), "search", s, t, ab,
                   query="保活探测", result_count=1, is_background=True))
    rows.append(ev("s8-bg-health", t0 + timedelta(minutes=2), "search", s, t, ab,
                   query="", result_count=0, source="api_health"))  # also empty
    rows.append(ev("s8-real", t0 + timedelta(minutes=4), "search", s, t, ab,
                   query="口红", result_count=64))

    # S9: synonym pair that the explicit mapping merges — two wordings, one
    # canonical query, both with results and a click.
    s, t, ab = "s9-synonym", "t1", "B"
    t0 = day.replace(hour=19, minute=15)
    rows.append(ev("s9-q0", t0, "search", s, t, ab, query="跑鞋", result_count=58))
    rows.append(ev("s9-q1", t0 + timedelta(minutes=3), "search", s, t, ab,
                   query="运动鞋", result_count=60))
    rows.append(ev("s9-click", t0 + timedelta(minutes=7), "click", s, t, ab,
                   product_id="P-9009", position=4))
    rows.append(ev("s9-tail", t0 + timedelta(minutes=33), "click", s, t, ab,
                   product_id="P-9010", position=2))

    return rows


# ---------------------------------------------------------------- randomized
def randomized(n_sessions: int = 92) -> list[dict]:
    rows: list[dict] = []
    classes = list(POOL)
    for k in range(n_sessions):
        sid = f"s-r{k:03d}"
        ab = "A" if k % 2 == 0 else "B"
        day = BASE_DAY + timedelta(days=RNG.randint(0, 6),
                                   hours=RNG.randint(0, 9),
                                   minutes=RNG.randint(0, 59))
        tab = "t1"
        t = day
        # Bucket B was tuned to reduce zero results (better recall): baseline
        # zero probability A=0.32, B=0.20.
        zero_p = 0.20 if ab == "B" else 0.32
        n_searches = RNG.choices([1, 2, 3, 4], weights=[40, 35, 18, 7])[0]
        clicked = False
        for i in range(n_searches):
            if i > 0 and RNG.random() < 0.35:
                # a rewrite after a previous zero: pick an impossible query first
                query = RNG.choice(IMPOSSIBLE)
                rc = 0
            elif RNG.random() < zero_p:
                query = RNG.choice(POOL[RNG.choice(classes)]) + " " + RNG.choice(
                    ["限定款", "绝版", "定制刻字"])
                rc = 0
            else:
                query = RNG.choice(POOL[RNG.choice(classes)])
                rc = RNG.choice([5, 12, 24, 47, 80, 120])
            rows.append(ev(f"{sid}-q{i}", t, "search", sid, tab, ab,
                           query=query, result_count=rc))
            if rc > 0 and not clicked and RNG.random() < (0.55 if ab == "B" else 0.42):
                rows.append(ev(f"{sid}-click{i}", t + timedelta(minutes=RNG.randint(1, 6)),
                               "click", sid, tab, ab,
                               product_id=f"P-{RNG.randint(1000, 9999)}",
                               position=RNG.randint(1, 10)))
                clicked = True
                if RNG.random() < 0.5:
                    rows.append(ev(f"{sid}-cart{i}",
                                   t + timedelta(minutes=RNG.randint(7, 12)),
                                   "add_to_cart", sid, tab, ab,
                                   product_id=f"P-{RNG.randint(1000, 9999)}"))
                    if RNG.random() < (0.6 if ab == "B" else 0.45):
                        # most orders inside the 30-min window; a few late
                        rows.append(ev(
                            f"{sid}-order{i}",
                            t + timedelta(minutes=RNG.choice([12, 18, 25, 33, 40])),
                            "order", sid, tab, ab,
                            product_id=f"P-{RNG.randint(1000, 9999)}",
                            amount=float(RNG.randint(30, 2000))))
            t += timedelta(minutes=RNG.randint(2, 8))

        # Most sessions have activity past the 30-minute window so they are
        # fully observed; the remainder end early and must be flagged incomplete.
        if k % 17 != 0 and RNG.random() < 0.78:
            rows.append(ev(f"{sid}-tail", t + timedelta(minutes=RNG.randint(31, 38)),
                           "click", sid, tab, ab,
                           product_id=f"P-{RNG.randint(1000, 9999)}",
                           position=RNG.randint(1, 10)))
    return rows


def main() -> None:
    rows = hand_crafted() + randomized()
    # Simulate late arrival: shuffle ingestion order. Keep event_time intact.
    RNG.shuffle(rows)
    for r in rows:
        r.pop("_truncated_marker", None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["event_type"]] = counts.get(r["event_type"], 0) + 1
    print(f"wrote {len(rows)} events -> {OUT}")
    print("by type:", counts)


if __name__ == "__main__":
    main()
