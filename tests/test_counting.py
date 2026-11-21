"""集数口径的回归测试：批量标记的行也是真实看过的一集。

背景：2026-01-27 有一次 145 集的批量标记。修复前 ``_show_index`` / ``_people_index``
把批量行从 ``items`` 里剔掉，导致侧栏显示「153 条」、矩阵按行计数、而画像和排行
只认「8 集」——同一个动作在不同页面是三个数字。修复后所有页面的集数口径一致。

运行：``.venv/Scripts/python.exe tests/test_counting.py``
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import queries  # noqa: E402


def ev(user="u1", show="s1", item="i1", season=1, episode=1, event_time=1_000_000,
       first_time=None, watched=0, pct=None, bulk=None, size=0, position=0, duration=0):
    """一条最小可用的 watch_event（字段与 ingest.build_events 的产物对齐）。"""
    return {
        "user_guid": "g-" + user, "user_name": user,
        "show_key": show, "show_title": "剧" + show,
        "item_guid": item, "item_type": "Episode",
        "season": season, "episode": episode, "episode_title": "",
        "event_time": event_time, "first_time": first_time or event_time,
        "watched": watched, "position": position, "duration": duration, "pct": pct,
        "library": "lib", "poster": "", "genres": "",
        "bulk_group": bulk, "bulk_size": size,
    }


def test_people_index_counts_bulk_rows_as_episodes():
    rows = [
        ev(item="i1", episode=1, event_time=1000),
        ev(item="i2", episode=2, event_time=2000, bulk="u1:1000", size=2),
        ev(item="i3", episode=3, event_time=3000, bulk="u1:1000", size=2),
    ]
    people = {p["name"]: p for p in queries._people_index(rows)}
    assert people["u1"]["events"] == 3
    assert people["u1"]["episodes"] == 3, "批量标记的行必须计入集数"


def test_show_index_counts_bulk_rows_as_episodes():
    rows = [
        ev(item="i1", episode=1, event_time=1000),
        ev(item="i2", episode=2, event_time=2000, bulk="u1:1000", size=2),
        ev(item="i3", episode=3, event_time=3000, bulk="u1:1000", size=2),
    ]
    shows = {s["key"]: s for s in queries._show_index(rows)}
    assert shows["s1"]["events"] == 3
    assert shows["s1"]["episodes"] == 3


def test_same_identity_still_dedupes_across_bulk_boundary():
    # 同一集既被批量标记又有单独记录（理论少见，但 trim_id 归并下会出现）：
    # 集数按身份去重，记录数照加
    rows = [
        ev(item="i1", event_time=1000),
        ev(item="i9", event_time=2000, bulk="u1:1000", size=1),
    ]
    people = {p["name"]: p for p in queries._people_index(rows)}
    assert people["u1"]["events"] == 2
    assert people["u1"]["episodes"] == 1


def test_event_count_expands_bulk_group_once():
    rows = [
        ev(item="i1", event_time=1000),
        ev(item="i2", event_time=2000, bulk="u1:1000", size=3),
        ev(item="i3", event_time=3000, bulk="u1:1000", size=3),
        ev(item="i4", event_time=4000, bulk="u1:1000", size=3),
    ]
    assert queries.event_count(rows) == 4, "折叠组按 bulk_size 记一次（3）+ 单行（1）"


def test_bulk_rows_do_not_inflate_watch_minutes():
    # 口径统一只影响「集数」；批量标记没有播放位置，时长必须仍然是 0。
    # 事件间隔给到小时级，避免触发「到下一次播放的间隔封顶」把位置压没。
    h = 3_600_000
    rows = [
        ev(item="i1", episode=1, event_time=1 * h, first_time=1 * h,
           position=1800, duration=2400, pct=75),
        ev(item="i2", episode=2, event_time=2 * h, first_time=2 * h,
           watched=1, bulk="u1:1000", size=2),
        ev(item="i3", episode=3, event_time=3 * h, first_time=3 * h,
           watched=1, bulk="u1:1000", size=2),
    ]
    people = {p["name"]: p for p in queries._people_index(rows)}
    assert people["u1"]["minutes"] == 30, "只有真实播放位置折算时长"


def test_done_eps_counts_bulk_marks():
    rows = [
        ev(item="i1", episode=1, event_time=1000, watched=1),
        ev(item="i2", episode=2, event_time=2000, watched=1, bulk="u1:1000", size=2),
        ev(item="i3", episode=3, event_time=3000, watched=1, bulk="u1:1000", size=2),
    ]
    people = {p["name"]: p for p in queries._people_index(rows)}
    assert people["u1"]["done_eps"] == 3


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