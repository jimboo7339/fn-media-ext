"""``app.watchtime`` 的回归测试：时长口径的每条规则各验一遍。

运行：``.venv/Scripts/python.exe -m pytest tests -q``；没有 pytest 时直接
``.venv/Scripts/python.exe tests/test_watchtime.py``，末尾有手写的断言跑法。

时间一律用「分钟」写：``start`` 是这一集**第一次打开**的时刻，``pos`` 是最后位置，
``next_start`` 是同一用户下一条播放记录首次打开的时刻。封顶比的就是
``next_start - start`` 与 ``pos``。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import watchtime  # noqa: E402

MIN = 60


def ev(pos, start, dur=45, user="u1", show="tt1", season=1, episode=1, item=None):
    """造一条观看记录。``pos``/``start``/``dur`` 都是分钟。

    ``start`` 是这一集**第一次上报进度**的时刻（用纪元后第 1 分钟当 0 点，避免
    ``first_time=0`` 这种真实数据里不存在的边界值），``pos`` 是那一刻已经走到的位置；
    最后进度上报的时刻由两者相加得出。所以同一用户下一条记录的 ``start`` 必须大于
    上一条的 ``start + pos``，两条记录才不会在时间上叠在一起（真实数据就是如此：
    一集看完才会开始下一集）。
    """
    first = int((1 + start) * MIN * 1000)
    return {
        "user_name": user,
        "user_guid": f"guid-{user}",
        "show_key": show,
        "show_title": show,
        "item_guid": item or f"{show}-{season}-{episode}",
        "season": season,
        "episode": episode,
        "position": int(pos * MIN),
        "duration": int(dur * MIN),
        "first_time": first,
        "event_time": first + int(pos * MIN) * 1000,
        "bulk_group": None,
    }


def est(evs, skips=None):
    return watchtime.estimate(evs, skips if skips is not None else {})["est"]


def episode(data, **kw):
    """按时间线铺一集：``data`` 是 (位置, 起点) 分钟。

    一条记录由位置和起点唯一确定（最后进度 = 起点 + 位置），所以起点要由调用方给，
    才能表达「位置推到 50 分钟，但 8 分钟后就看下一集了」这种快进/倍速的情形。
    """
    pos, start = data
    return ev(pos, start, **kw)


def timeline(episodes, first_start=0, dur=45, **kw):
    """把一集接一集地铺开：下一集的起点 = 上一集的起点 + 它的位置。

    这是「老老实实看完」的时间线，任何封顶都不该生效——用来验封顶不会误伤常规观看。
    """
    out, cursor = [], first_start
    for i, pos in enumerate(episodes, start=1):
        out.append(ev(pos, cursor, dur=dur, episode=i, **kw))
        cursor += pos
    return out


def test_normal_watch_is_untouched():
    """一集接一集地看完：封顶不生效，估算就是每集位置之和。"""
    evs = timeline([45, 44, 40], dur=45)
    assert est(evs) == (45 + 44 + 40) * MIN


def test_position_far_ahead_of_walltime_is_capped():
    """2 倍速或拖动：位置推到 50 分，但 8 分钟后就开始看下一集了。"""
    evs = [episode((50, 0), dur=60, episode=1), episode((10, 8), dur=60, episode=2)]
    assert est(evs) == 8 * MIN + 10 * MIN


def test_walltime_cap_only_uses_the_gap():
    """上界就是「到下一次播放事件」的间隔，不是整集长度。"""
    evs = [episode((48, 0), dur=50, episode=1), episode((5, 20), dur=50, episode=2)]
    assert est(evs) == 20 * MIN + 5 * MIN


def test_cap_does_not_touch_the_last_episode():
    """封顶只影响还有下一集的那几集，最后一集保持原值。"""
    evs = [episode((45, 0), dur=45, episode=1), episode((45, 45), dur=45, episode=2)]
    # 第二集起点 45，位置 45：间隔（到第三条记录）用不上，保持 45
    assert est(evs) == 90 * MIN


def test_same_episode_duplicate_items_count_once():
    """同一集在库里有两个条目（不同 guid），只算一次。"""
    a = ev(30, 0, dur=45, episode=7, item="guid-A")
    dup = ev(41, 0, dur=45, episode=7, item="guid-B")
    assert est([a, dup]) == 41 * MIN
    # raw 口径仍是两条相加，页面上拿它当上界对照
    assert watchtime.raw_seconds([a, dup]) == (30 + 41) * MIN


def test_duplicate_items_share_one_timeline():
    """同集的重复条目不再被当成独立记录去和下一集比间隔。"""
    a = ev(30, 0, dur=45, episode=7, item="guid-A")
    dup = ev(41, 0, dur=45, episode=7, item="guid-B")
    nxt = episode((40, 41), dur=45, episode=8)
    assert est([a, dup, nxt]) == 41 * MIN + 40 * MIN


def test_bulk_mark_adds_no_time():
    """「标记已看」批量刷进度不产生播放位置，也不产生时长。"""
    a = ev(40, start=0, dur=45, episode=1)
    a["bulk_group"] = "u1:1000"
    assert est([a]) == 0
    assert watchtime.raw_seconds([a]) == 40 * MIN


def test_bulk_and_real_row_keep_the_real_one():
    """同集既有批量标记又有真实播放，真实的那条说了算。"""
    a = ev(40, start=0, dur=45, episode=1)
    a["bulk_group"] = "u1:1000"
    b = ev(40, start=0, dur=45, episode=1)
    assert est([a, b]) == 40 * MIN


def test_skip_intro_outro_is_deducted():
    a = ev(45, start=0, dur=45, episode=1)
    a["item_guid"] = "X1"
    skips = {("guid-u1", "X1"): (95, 130)}
    assert est([a], skips) == 45 * MIN - 225
    # 只认这一集的配置：配置挂在别的条目上就不扣
    assert est([a], {("guid-u1", "OTHER"): (95, 130)}) == 45 * MIN
    # 配置挂在别人身上也不扣
    assert est([a], {("guid-someone", "X1"): (95, 130)}) == 45 * MIN


def test_skip_deduction_never_goes_negative():
    """配置异常（跳片头+片尾比整集还长）时最多扣一半。"""
    a = ev(40, start=0, dur=40, episode=1)
    a["item_guid"] = "X1"
    assert est([a], {("guid-u1", "X1"): (9999, 9999)}) == 20 * MIN


def test_last_record_of_the_day_is_not_capped():
    """当天最后一条没有「下一次播放事件」可参照，保持位置原值（上界）。"""
    assert est([ev(40, start=0, dur=45, episode=1)]) == 40 * MIN


def test_unknown_duration_still_capped_within_twelve_hours():
    """流没探测出 duration 的集（duration=0），只要间隔在 12 小时内仍封顶。"""
    evs = [episode((48, 0), dur=0, episode=13, item="A"),
           episode((20, 3), dur=0, episode=14, item="B")]
    assert est(evs) == 3 * MIN + 20 * MIN


def test_unknown_duration_ignores_day_apart_records():
    """间隔超过 12 小时的记录不算同一次观看，不用来封顶。"""
    evs = [episode((48, 0), dur=0, episode=13, item="A"),
           episode((20, 13 * 60), dur=0, episode=14, item="B")]
    assert est(evs) == 48 * MIN + 20 * MIN


def test_known_duration_ignores_day_apart_records():
    evs = [episode((45, 0), dur=45, episode=1), episode((30, 24 * 60), dur=45, episode=2)]
    assert est(evs) == 45 * MIN + 30 * MIN


def test_users_are_capped_independently():
    """两个人各有一条时间线，不能互相封顶。"""
    a = ev(50, start=0, dur=60, user="u1", episode=1)
    b = ev(50, start=0, dur=60, user="u2", episode=1)
    assert est([a, b]) == 100 * MIN


def test_identity_groups_by_episode_not_item():
    e1 = ev(10, start=0, show="tt9", season=2, episode=5, item="g1")
    e2 = ev(10, start=0, show="tt9", season=2, episode=5, item="g2")
    e3 = ev(10, start=0, show="tt9", season=2, episode=6, item="g3")
    assert watchtime.identity(e1) == watchtime.identity(e2)
    assert watchtime.identity(e1) != watchtime.identity(e3)
    # 不同剧的同季同集也不能合并
    assert watchtime.identity(e3) != watchtime.identity(ev(10, start=0, show="tt8", season=2, episode=6))


def test_movie_without_episode_numbers_falls_back_to_item_guid():
    a = ev(90, start=0, dur=120, season=None, episode=None, item="movie-A")
    b = ev(90, start=0, dur=120, season=None, episode=None, item="movie-B")
    assert est([a, b]) == 180 * MIN      # 两部电影不能被当成同一「集」合并


def test_minutes_are_truncated_like_the_old_metric():
    """分钟数向下取整，和介绍里写的旧口径一致。"""
    got = watchtime.estimate([ev(10.9, start=0, dur=45, episode=1)], {})
    assert got["minutes"] == 10 and got["raw_minutes"] == 10


def test_estimate_never_exceeds_raw():
    """修正只会把数字压小；万一哪条规则反过来放大，这里会先炸。"""
    evs = [ev(50, start=0, dur=60, episode=1), ev(30, start=8, dur=60, episode=2),
           ev(20, start=30, dur=60, episode=1, item="dup")]
    got = watchtime.estimate(evs, {})
    assert got["est"] <= got["raw"]


def test_skip_spans_tolerates_a_missing_snapshot():
    """快照还没生成时读配置不能炸，返回空表即可（少扣一点好过算错）。"""
    import tempfile
    from pathlib import Path
    from app import config

    original = config.SNAP_DIR
    try:
        config.SNAP_DIR = Path(tempfile.mkdtemp()) / "nope"
        cached = watchtime._SPANS_CACHE.get("key")
        watchtime._SPANS_CACHE["key"] = None
        assert watchtime.skip_spans() == {}
    finally:
        config.SNAP_DIR = original
        watchtime._SPANS_CACHE["key"] = cached
        watchtime._SPANS_CACHE["map"] = {}


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                fails += 1
                print(f"  FAIL {name}: {exc}")
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
