"""ingest._mark_bulk 的判定规则：同一人 60 秒内 ≥5 条折叠为一批。

运行：``.venv/Scripts/python.exe tests/test_ingest_bulk.py``
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ingest  # noqa: E402


def ev(user="u1", item="i1", t=0):
    return {
        "user_name": user, "item_guid": f"{item}", "show_key": "s1", "show_title": "剧",
        "event_time": t, "bulk_group": None, "bulk_size": 0,
    }


def test_six_rapid_marks_collapse_into_one_batch():
    events = [ev(item=f"i{i}", t=1000 * i) for i in range(6)]
    ingest._mark_bulk(events)
    groups = {e["bulk_group"] for e in events}
    assert len(groups) == 1 and None not in groups
    assert all(e["bulk_size"] == 6 for e in events)


def test_three_marks_stay_individual():
    events = [ev(item=f"i{i}", t=1000 * i) for i in range(3)]
    ingest._mark_bulk(events)
    assert all(e["bulk_group"] is None for e in events)


def test_gap_beyond_60s_splits_batches():
    # 前 5 条挤在 10 秒内，第 6 条隔了 10 分钟
    events = [ev(item=f"i{i}", t=1000 * i) for i in range(5)]
    events.append(ev(item="i5", t=1000 * 600))
    ingest._mark_bulk(events)
    batch = [e for e in events if e["bulk_group"]]
    solo = [e for e in events if not e["bulk_group"]]
    assert len(batch) == 5 and len(solo) == 1
    assert solo[0]["item_guid"] == "i5"


def test_different_users_never_share_a_group():
    events = [ev(user="a", item=f"a{i}", t=1000 * i) for i in range(5)]
    events += [ev(user="b", item=f"b{i}", t=1000 * i) for i in range(5)]
    ingest._mark_bulk(events)
    groups = {(e["user_name"], e["bulk_group"]) for e in events}
    user_groups = {}
    for user, group in groups:
        user_groups.setdefault(user, set()).add(group)
    assert all(len(g) == 1 for g in user_groups.values()), "每个用户各自成批"
    a_group = next(iter(user_groups["a"]))
    b_group = next(iter(user_groups["b"]))
    assert a_group != b_group


def test_mutation_is_idempotent_for_sorted_input():
    events = [ev(item=f"i{i}", t=1000 * i) for i in range(6)]
    ingest._mark_bulk(events)
    first = {e["item_guid"]: e["bulk_group"] for e in events}
    ingest._mark_bulk(events)
    again = {e["item_guid"]: e["bulk_group"] for e in events}
    assert first == again


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print("  ok  ", fn.__name__)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("  FAIL", fn.__name__, "->", exc)
    print(f"{ok}/{len(fns)} passed")