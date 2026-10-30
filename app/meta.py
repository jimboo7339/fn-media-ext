"""刮削完整度盘点：找出「元数据不全」的条目，并记住上次的结论。

为什么要记：飞牛自己的 `TaskItemScrap` 每 24 小时跑一次，但它只处理
`fetch_status=0`（待抓取）的条目；一旦被标成 1，即使字段是空的也不会再重试。
所以「哪些条目值得请求重刮」这个判断，得有人做——这里做。

两个刻意的设计：

1. **不每次全扫描。** 源库快照的 mtime 没变，就直接用上次存在 `app.db` 里的结论。
   飞牛一旦写了库（刮削成功、扫描入库），快照 mtime 就会变，那时才重新盘点。
2. **只在本地库里记状态**，一个字节都不写回飞牛。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from . import config, db, posters

# 缺失项 -> 中文标签
LABELS = {
    "overview": "简介",
    "rating": "评分",
    "date": "首播/上映日期",
    "backdrop": "背景图",
    "poster_path": "海报路径",
    "poster_file": "海报文件",
    "episodes": "集数",
    "still": "剧照",
    "ep_overview": "简介",
    "ep_date": "播出日期",
}


def _labels(missing: str, ep_missing: int = 0, ep_total: int = 0) -> list[str]:
    out = []
    ep_codes = [c for c in missing.split(",") if c in ("still", "ep_overview", "ep_date")]
    if ep_missing and ep_codes:
        # ep_missing 是三种缺失之和（一集可能同时缺剧照和简介），不是缺的集数
        parts = "、".join(LABELS[c] for c in ep_codes)
        out.append(f"单集缺{parts}：{ep_total} 集里 {ep_missing} 处")
    for c in (c for c in missing.split(",") if c and c not in ("still", "ep_overview", "ep_date")):
        out.append(LABELS.get(c, c))
    return out


def _stamp(ms: int) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%m-%d %H:%M")


def _snapshot() -> str:
    return str(config.SNAP_DIR / "trimmedia.db")


def _scan_rows() -> tuple[list[dict], bool]:
    """读快照，按 trim_id 归并成「一部剧一行」。第二个返回值是「有没有查海报文件」。"""
    path = _snapshot()
    check_posters = config.IMG_ROOT.exists()
    shows: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return [], check_posters
    try:
        con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT trim_id, title, type, overview, posters, vote_average, first_air_date, "
            "       release_date, backdrops, number_of_episodes, fetch_status, update_time "
            "FROM item WHERE type IN ('TV','Movie')"
        ):
            key = (r["trim_id"] or "").strip() or f'guid:{r["title"]}'
            cur = shows.get(key)
            # 同一部剧可能在多个媒体库里有副本，取字段最全的那一行
            score = sum(
                1
                for f in ("overview", "posters", "vote_average", "first_air_date", "release_date", "backdrops")
                if str(r[f] or "").strip() not in ("", "0", "0.0")
            )
            if cur is None or score > cur["_score"]:
                shows[key] = {
                    "show_key": key,
                    "title": (r["title"] or "").strip() or key,
                    "type": r["type"],
                    "overview": (r["overview"] or "").strip(),
                    "poster": (r["posters"] or "").strip(),
                    "vote": str(r["vote_average"] or "").strip(),
                    "date": (r["first_air_date"] or r["release_date"] or "").strip(),
                    "backdrop": (r["backdrops"] or "").strip(),
                    "episodes": r["number_of_episodes"] or 0,
                    "fetch_status": str(r["fetch_status"]),
                    "scraped_at": int(r["update_time"] or 0),
                    "_score": score,
                }
    except sqlite3.Error:
        return [], check_posters
    finally:
        con.close()

    # 单集层：把「缺剧照/缺简介/缺播出日期」的集数聚到所属剧上。
    # 飞牛在单集上同样用 fetch_status 标记「已抓取」，所以这些缺口不会被自动重试。
    ep_stats: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            con.row_factory = sqlite3.Row
            for r in con.execute(
                "SELECT tv.trim_id AS trim_id, COUNT(*) eps,"
                " SUM(CASE WHEN e.still_path IS NULL OR e.still_path='' THEN 1 ELSE 0 END) no_still,"
                " SUM(CASE WHEN e.overview  IS NULL OR e.overview=''  THEN 1 ELSE 0 END) no_overview,"
                " SUM(CASE WHEN e.air_date  IS NULL OR e.air_date=''  THEN 1 ELSE 0 END) no_date"
                " FROM item e"
                " JOIN item s  ON s.guid  = e.parent_guid AND s.type='Season'"
                " JOIN item tv ON tv.guid = s.parent_guid AND tv.type='TV'"
                " GROUP BY tv.trim_id"
            ):
                key = (r["trim_id"] or "").strip()
                if key:
                    ep_stats[key] = {
                        "eps": r["eps"], "no_still": r["no_still"],
                        "no_overview": r["no_overview"], "no_date": r["no_date"],
                    }
        finally:
            con.close()
    except sqlite3.Error:
        pass

    for s in shows.values():
        missing = []
        if not s["overview"]:
            missing.append("overview")
        if not s["vote"] or s["vote"] == "0":
            missing.append("rating")
        if not s["date"]:
            missing.append("date")
        if not s["backdrop"]:
            missing.append("backdrop")
        if not s["poster"]:
            missing.append("poster_path")
        elif check_posters and not posters.resolve(s["poster"]):
            missing.append("poster_file")
        ep = ep_stats.get(s["show_key"])
        if s["type"] == "TV" and ep:
            if ep["no_still"]:
                missing.append("still")
            if ep["no_overview"]:
                missing.append("ep_overview")
            if ep["no_date"]:
                missing.append("ep_date")
            s["ep_total"] = ep["eps"]
            s["ep_missing"] = ep["no_still"] + ep["no_overview"] + ep["no_date"]
        if s["type"] == "TV" and not s["episodes"]:
            missing.append("episodes")
        s["missing"] = ",".join(missing)
        s["complete"] = 0 if missing else 1
        s["missing_labels"] = _labels(s["missing"], s.get("ep_missing", 0), s.get("ep_total", 0))
        s.pop("_score")
    return list(shows.values()), check_posters


def scan(force: bool = False) -> dict:
    """盘点一次；源库没变化且已有结论时直接复用。"""
    path = _snapshot()
    try:
        mtime = int(config.SNAP_DIR.joinpath("trimmedia.db").stat().st_mtime)
    except OSError:
        return {
            "meta": {"thin": 0, "complete": 0, "requested": 0, "rows": [], "checked_at": 0,
                     "fresh": False, "posters_checked": False, "error": "找不到源库快照"},
        }

    stored = db.get_setting("meta_scan_mtime")
    reuse = (not force) and stored == str(mtime)
    if not reuse:
        rows, check_posters = _scan_rows()
        now = int(time.time() * 1000)
        with db.session_scope() as con:
            for s in rows:
                con.execute(
                    "INSERT INTO meta_probe(show_key, title, kind, missing, complete, fetch_status, "
                    "                       scraped_at, checked_at, ep_missing, ep_total) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(show_key) DO UPDATE SET "
                    "  title=excluded.title, kind=excluded.kind, missing=excluded.missing, "
                    "  complete=excluded.complete, fetch_status=excluded.fetch_status, "
                    "  scraped_at=excluded.scraped_at, checked_at=excluded.checked_at, "
                    "  ep_missing=excluded.ep_missing, ep_total=excluded.ep_total",
                    (s["show_key"], s["title"], s["type"], s["missing"], s["complete"],
                     s["fetch_status"], s["scraped_at"], now,
                     s.get("ep_missing", 0), s.get("ep_total", 0)),
                )
            con.execute("DELETE FROM meta_probe WHERE checked_at <> ?", (now,))
        db.set_setting("meta_scan_mtime", str(mtime))
        db.set_setting("meta_posters_checked", "1" if check_posters else "0")

    with db.session_scope() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM meta_probe ORDER BY complete, title"
        )]
    posters_checked = db.get_setting("meta_posters_checked") == "1"

    thin = [r for r in rows if not r["complete"]]
    for r in thin:
        r["missing_labels"] = _labels(r["missing"] or "", r["ep_missing"], r["ep_total"])
        r["scraped_label"] = _stamp(r["scraped_at"])
        r["requested_label"] = _stamp(r["requested_at"])
    return {
        "meta": {
            "rows": thin,
            "thin": len(thin),
            "complete": len(rows) - len(thin),
            "requested": sum(1 for r in rows if r["requested_at"]),
            "checked_at": max((r["checked_at"] for r in rows), default=0),
            "checked_label": _stamp(max((r["checked_at"] for r in rows), default=0)),
            "fresh": not reuse,
            "posters_checked": posters_checked,
        }
    }