"""某一天新入库的剧集与电影——「今天又更新了什么」。

为什么不把它派生进 `app.db`：入库时间和观看记录没有任何关系，把它写成派生表只会让
`app.db` 无谓地变大，而它只在生成日报时用一次。这里直接查快照（几千行，毫秒级），
快照不存在时返回空，日报照常生成。

`item.create_time` 是飞牛把条目写进媒体库的时间，也就是「入库」；同一批导入的整季
会共享同一个时间戳，所以这里按剧聚合，不按条铺开——「入库 24 集」比 24 行有用。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from . import config


def _empty() -> dict:
    return {"groups": 0, "episodes": 0, "movies": 0, "total": 0, "shows": []}


def _bounds(day: date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=config.TZ)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _ep_label(season, episode) -> str:
    if season and episode:
        return f"S{int(season):02d}E{int(episode):02d}"
    if episode:
        return f"E{int(episode):02d}"
    return ""


def summary(day: date) -> dict:
    """New episodes / movies whose catalog time falls inside `day` (app timezone)."""
    path = config.SNAP_DIR / "trimmedia.db"
    if not path.exists():
        return _empty()

    start, end = _bounds(day)
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            episodes = con.execute(
                "SELECT e.guid, e.season_number AS season, "
                "       e.episode_number AS episode, "
                "       tv.guid AS show_guid, tv.title AS show_title, "
                "       tv.trim_id AS trim_id, tv.posters AS posters "
                "FROM item e "
                "JOIN item s  ON s.guid = e.parent_guid AND s.type = 'Season' "
                "JOIN item tv ON tv.guid = s.parent_guid AND tv.type = 'TV' "
                "WHERE e.type = 'Episode' AND e.create_time >= ? AND e.create_time < ?",
                (start, end),
            ).fetchall()
            movies = con.execute(
                "SELECT m.guid, m.title AS show_title, "
                "       m.trim_id AS trim_id, m.posters AS posters "
                "FROM item m WHERE m.type = 'Movie' "
                "AND m.create_time >= ? AND m.create_time < ?",
                (start, end),
            ).fetchall()
            libs = _libraries(con, start, end)
        finally:
            con.close()
    except sqlite3.Error:
        return _empty()

    groups: dict[str, dict] = {}
    for row in episodes:
        key = (row["trim_id"] or "").strip() or row["show_guid"]
        g = groups.setdefault(
            key,
            {
                "key": key,
                "show": (row["show_title"] or "").strip() or key,
                "kind": "剧集",
                "poster": (row["posters"] or "").split(",")[0].strip().lstrip("/"),
                "library": libs.get(row["guid"], ""),
                "n": 0,
                "seasons": set(),
                "eps": [],
                "first": "",
                "last": "",
                "contiguous": False,
                "_libs": {},
            },
        )
        g["n"] += 1
        if row["season"]:
            g["seasons"].add(int(row["season"]))
        g["eps"].append((row["season"] or 0, row["episode"] or 0))
        lib = libs.get(row["guid"])
        if lib:
            g["_libs"][lib] = g["_libs"].get(lib, 0) + 1
        if not g["poster"] and row["posters"]:
            g["poster"] = (row["posters"] or "").split(",")[0].strip().lstrip("/")

    shows = []
    for g in groups.values():
        eps = sorted(set(g.pop("eps")))
        g["first"] = _ep_label(*eps[0])
        g["last"] = _ep_label(*eps[-1])
        seasons = sorted(g.pop("seasons"))
        g["season"] = seasons[0] if len(seasons) == 1 else 0
        g["n_seasons"] = len(seasons)
        # 单季、集号连续时才敢写「E01–E04」；中间缺集就只报总数
        g["contiguous"] = (
            len(seasons) == 1
            and len(eps) > 1
            and eps[-1][1] - eps[0][1] + 1 == len(eps)
        )
        libs_of_g = g.pop("_libs")
        if libs_of_g:
            g["library"] = max(libs_of_g.items(), key=lambda kv: kv[1])[0]
        shows.append(g)

    for row in movies:
        key = (row["trim_id"] or "").strip() or row["guid"]
        shows.append(
            {
                "key": key,
                "show": (row["show_title"] or "").strip() or key,
                "kind": "电影",
                "poster": (row["posters"] or "").split(",")[0].strip().lstrip("/"),
                "library": libs.get(row["guid"], ""),
                "n": 1,
                "season": 0,
                "n_seasons": 1,
                "first": "",
                "last": "",
                "contiguous": False,
            }
        )
    for g in shows:
        g["label"] = _row_label(g)
        g["detail"] = _row_detail(g)
    shows.sort(key=lambda g: (g["kind"] == "电影", -g["n"], g["show"]))

    episode_n = sum(g["n"] for g in shows if g["kind"] == "剧集")
    movie_n = sum(1 for g in shows if g["kind"] == "电影")
    return {
        "groups": len(shows),
        "episodes": episode_n,
        "movies": movie_n,
        "total": episode_n + movie_n,
        "shows": shows,
    }


def _row_label(g: dict) -> str:
    """One line for the push message, including the title."""
    if g["kind"] == "电影":
        return f"电影《{g['show']}》入库"
    return f"《{g['show']}》{_row_detail(g)}"


def _row_detail(g: dict) -> str:
    """The same information without the title, for the page table."""
    if g["kind"] == "电影":
        return "入库"
    if g["n"] == 1:
        return f"入库 {g['first']}" if g["first"] else "入库 1 集"
    if g["contiguous"]:
        return f"入库 {g['n']} 集（{g['first']}–{g['last'][-3:]}）"
    if g["n_seasons"] > 1:
        return f"入库 {g['n']} 集（{g['n_seasons']} 季）"
    return f"入库 {g['n']} 集"


def _libraries(con: sqlite3.Connection, start: int, end: int) -> dict[str, str]:
    """Which media library each new item belongs to (best effort)."""
    try:
        rows = con.execute(
            "SELECT a.item_guid, MIN(d.title) AS lib FROM item_ancestor a "
            "JOIN item d ON d.guid = a.ancestor_guid AND d.type = 'MediaDB' "
            "WHERE a.item_guid IN (SELECT guid FROM item WHERE create_time >= ? AND create_time < ?) "
            "GROUP BY a.item_guid",
            (start, end),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r["item_guid"]: (r["lib"] or "") for r in rows}