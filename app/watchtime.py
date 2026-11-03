"""观看时长的估算口径：一处实现，所有页面共用。

飞牛只在 ``item_user_play.ts`` 里保留每集的**最后一个**播放位置，于是「时长」只能
从位置推。直接用位置求和会系统性地偏高，原因有三：

1. ``ts`` 是媒体时间轴上的位置，不是墙上时间——2 倍速看完 60 分钟的电影，``ts``
   照样推到 3600，而实际只花了 30 分钟（源库里没有任何播放速度字段）。
2. 拖动快进同样把位置推远，跳过的内容照样计入。
3. 同一集在库里可能存在多个条目（同一 trim_id 下重复的 Episode），两条记录都带
   位置，按 ``item_guid`` 去重就会把一集算成两集。

这里做三件事来收窄误差：

* **按集去重**——同一部剧的同一季同一集只算一次，而不是按条目 guid 算；
* **扣掉应用自动跳过的片头片尾**——``item_play_config`` 里的 ``skip_opening`` /
  ``skip_ending``（秒），这段内容用户根本没看；
* **墙上时间封顶**——``first_time`` 是这一集第一次打开的时刻，用它到下一次播放
  事件的间隔作为上界：一集真实花掉的时间不可能超过这段间隔。

三者都只是**把上界收紧**，无法还原真实播放耗时：已经发生的历史里，倍速和拖动没有
留下任何可反推的痕迹。所以对外一律按「估算」表述，同时保留 ``raw``（未修正的位置
求和）好让页面对照。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import config

MAX_CONTIGUOUS_MS = 12 * 3600 * 1000   # 间隔超过 12 小时就不算同一次观看
UNKNOWN_DURATION_CAP_MS = 12 * 3600 * 1000  # 时长未知时不接受超过 12 小时的单集
FALLBACK_SKIP_SECONDS = 226            # 没有配置时的兜底提示值：95s 片头 + 131s 片尾


def identity(e: dict) -> tuple:
    """一集的自然身份：剧 + 季 + 集。电影和散片没有集号，退回条目 guid。

    用它而不是 ``item_guid``：同一 trim_id 下重复的 Episode 条各自有 guid，但用户
    眼里就是同一集。
    """
    if (e.get("season") or 0) or (e.get("episode") or 0):
        return ("ep", e.get("show_key") or "", e["season"] or 0, e["episode"] or 0)
    return ("item", e.get("item_guid") or "")


def span_start(e: dict) -> int:
    """这条记录所代表的那一次观看的起点（ms）。

    不写 ``a or b``：起点为 0 是合法值（时间戳纪元），用 ``or`` 会被当成缺省而退回
    事件的最后更新时间，把那一次观看的跨度算短。
    """
    for field in ("first_time", "event_time"):
        value = e.get(field)
        if value:
            return int(value)
    return 0


def skip_cut(e: dict, skips: dict[tuple[str, str], tuple[int, int]] | None) -> int:
    """飞牛自动跳过的片头+片尾秒数，只针对这条记录的**那一集**。

    配置是「每个用户每个条目」一份，所以用 (user_guid, item_guid) 去查；查不到就是
    0，绝不套用别的剧的数字——那会把本该算进去的时长凭空扣掉。
    """
    if not skips or e.get("bulk_group"):
        return 0
    got = skips.get((e.get("user_guid") or "", e.get("item_guid") or ""))
    if not got:
        return 0
    opening, ending = got
    cut = max(0, int(opening or 0)) + max(0, int(ending or 0))
    duration = int(e.get("duration") or 0)
    if duration > 0:
        cut = min(cut, duration // 2)   # 配置异常时最多扣一半，避免出现负数时长
    return cut


def raw_seconds(evs: list[dict]) -> float:
    """未修正的口径：每集取最大位置后求和（页面上作为上界对照）。

    这里**按条目 guid 去重**，刻意保留「同一集两个条目被算两次」的旧行为：它是上界，
    不是正确答案。
    """
    best: dict[tuple, int] = {}
    for e in evs:
        key = (e.get("user_name") or "", e.get("item_guid") or "")
        best[key] = max(best.get(key, 0), int(e.get("position") or 0))
    return float(sum(best.values()))


def estimate(evs: list[dict], skips: dict | None = None) -> dict:
    """估算时长。返回 raw/est（秒）与逐集明细。

    ``est`` 的算法：先按「人 + 剧 + 季 + 集」去重（位置取最大、起点取最早），扣掉
    该集自动跳过的片头片尾，再用「起点到下一次播放事件」的间隔封顶。
    """
    if skips is None:
        skips = skip_spans()

    groups: dict[tuple, dict] = {}
    for e in evs:
        key = (e.get("user_name") or "", identity(e))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"user": e.get("user_name") or "", "pos": 0, "start": None,
                               "duration": 0, "bulk": True, "cut": 0}
        g["pos"] = max(g["pos"], int(e.get("position") or 0))
        g["duration"] = max(g["duration"], int(e.get("duration") or 0))
        start = span_start(e)
        if start and (g["start"] is None or start < g["start"]):
            g["start"] = start          # 起点取最早的一次打开
        if not e.get("bulk_group"):
            g["bulk"] = False          # 只要有一条不是批量标记，这一集就算真看过
        g["cut"] = max(g["cut"], skip_cut(e, skips))

    by_user: dict[str, list[tuple[int, tuple, dict]]] = defaultdict(list)
    for key, g in groups.items():
        by_user[g["user"]].append((g["start"] or 0, key, g))
    for mine in by_user.values():
        mine.sort(key=lambda x: x[0])

    raw = raw_seconds(evs)
    est = 0.0
    per_group: dict[tuple, float] = {}
    for mine in by_user.values():
        for i, (start, key, g) in enumerate(mine):
            if g["bulk"]:                      # 批量标记不产生播放位置，也不产生时长
                per_group[key] = 0.0
                continue
            pos = max(0.0, g["pos"] - g["cut"])
            if i + 1 < len(mine):
                gap = mine[i + 1][0] - start
                # 走到下一次播放事件之前花掉的时间就是这一集的上界。位置比它还远，
                # 只可能是快进或倍速，取上界。正常看完一集时 position ≈ duration，
                # 间隔比它长，封顶不会生效。时长不明（流没探测出 duration）时只接受
                # 12 小时以内的间隔，避免拿隔天的记录当证据。
                if g["duration"] > 0:
                    if 0 < gap <= MAX_CONTIGUOUS_MS:
                        pos = min(pos, gap / 1000.0)
                elif 0 < gap <= UNKNOWN_DURATION_CAP_MS:
                    pos = min(pos, gap / 1000.0)
            per_group[key] = pos
            est += pos
    return {"raw": raw, "est": est, "groups": per_group,
            "minutes": int(est) // 60, "raw_minutes": int(raw) // 60}


def per_group_seconds(evs: list[dict], skips: dict | None = None) -> dict[tuple, float]:
    """每人每集的估算秒数（值就是该集算进总时长的部分）。"""
    return estimate(evs, skips)["groups"]


# ------------------------------------------------------------------ 片头片尾
_SPANS_CACHE: dict[str, object] = {"key": None, "map": {}}


def skip_spans() -> dict[tuple[str, str], tuple[int, int]]:
    """读快照里的 ``item_play_config``：{(user_guid, item_guid): (片头秒, 片尾秒)}。

    随快照刷新（用 ``trimmedia.db`` 的 mtime 当缓存键）；读不到就返回空表，少扣一点
    总比因为配置读不到而把好好的一集算短了强。
    """
    path = config.SNAP_DIR / "trimmedia.db"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _SPANS_CACHE.get("key") == key:
        return _SPANS_CACHE["map"]                     # type: ignore[return-value]
    spans: dict[tuple[str, str], tuple[int, int]] = {}
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            for row in con.execute(
                "SELECT user_guid, item_guid, skip_opening, skip_ending FROM item_play_config"
            ):
                spans[(row[0] or "", row[1] or "")] = (int(row[2] or 0), int(row[3] or 0))
        finally:
            con.close()
    except sqlite3.Error:
        spans = {}
    _SPANS_CACHE["key"] = key
    _SPANS_CACHE["map"] = spans
    return spans
