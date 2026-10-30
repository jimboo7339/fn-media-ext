"""排查某部剧的集号归属：库里有哪些集、观看记录有哪些集、哪些记录挂不上 Season 树。

用法（容器里）：

    docker exec -it fn-media-ext python -m app.diagnose 剧名

只读快照和 app.db，不改任何东西。输出分三块：库里的 Seasons、挂不上 Season 树的
Episode、观看记录，最后给一句结论（库里最大集 / 能对上的最大集 / 对不上的记录）。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

from . import config, db, queries


def _open(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _show_key(tv: sqlite3.Row) -> str:
    return (tv["trim_id"] or "").strip() or tv["guid"]


def _label(season, episode) -> str:
    return queries.episode_code(season, episode) or "—"


def _stamp(ms: int) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%Y-%m-%d %H:%M")


def main(argv: list[str]) -> int:
    q = " ".join(argv).strip()
    if not q:
        print("用法: python -m app.diagnose <剧名或 trim_id>")
        return 2

    snap = config.SNAP_DIR / "trimmedia.db"
    if not snap.exists():
        print(f"找不到快照 {snap}，先让应用同步一次")
        return 2

    con = _open(snap)
    try:
        like = f"%{q}%"
        tvs = con.execute(
            "SELECT guid, trim_id, title FROM item "
            "WHERE type IN ('TV','Movie') AND (title LIKE ? OR trim_id LIKE ?) "
            "ORDER BY title",
            (like, like),
        ).fetchall()
        if not tvs:
            print(f"库里没有匹配「{q}」的剧集/电影")
            return 1

        keys = [_show_key(tv) for tv in tvs]
        catalog = queries.library_episodes(keys)
        valid = set()
        for key, hit in catalog.items():
            valid |= hit["guids"]

        print(f"匹配到 {len(tvs)} 部，快照 {snap}\n")
        for tv in tvs:
            key = _show_key(tv)
            print(f'《{tv["title"]}》  key={key}  guid={tv["guid"]}')
            seasons = con.execute(
                "SELECT s.guid, s.season_number, "
                "       (SELECT COUNT(*) FROM item e WHERE e.parent_guid = s.guid AND e.type = 'Episode') AS n "
                "FROM item s WHERE s.parent_guid = ? AND s.type = 'Season' "
                "ORDER BY s.season_number",
                (tv["guid"],),
            ).fetchall()
            for s in seasons:
                print(f'  Season {s["season_number"]}: {s["n"]} 集')
            if not seasons:
                print("  （没有 Season 节点）")

            trim = (tv["trim_id"] or "").strip()
            loose = con.execute(
                "SELECT e.guid, e.season_number, e.episode_number, e.parent_guid, e.trim_id, "
                "       COALESCE(p.type, '(父节点缺失)') AS ptype, p.title AS ptitle "
                "FROM item e LEFT JOIN item p ON p.guid = e.parent_guid "
                "WHERE e.type = 'Episode' "
                "  AND (e.parent_guid = ? OR e.parent_guid IS NULL OR e.parent_guid = '' "
                "       OR (e.trim_id <> '' AND e.trim_id = ?))",
                (tv["guid"], trim),
            ).fetchall()
            stray = [r for r in loose if r["ptype"] != "Season"]
            if stray:
                print(f'  ⚠ {len(stray)} 个 Episode 不挂 Season（父节点是 {stray[0]["ptype"]}）：')
                for r in stray[:8]:
                    parent = (r["parent_guid"] or "(空)")[:8]
                    print(f'     {_label(r["season_number"], r["episode_number"])}  guid={r["guid"]}  '
                          f'parent={parent}… ({r["ptype"]})')
            print()

        # 观看记录：按 show_title 或 show_key 找
        marks = ",".join("?" * len(keys))
        with db.session_scope() as app:
            events = app.execute(
                f"SELECT user_name, show_title, show_key, season, episode, item_guid, "
                f"       item_type, event_time "
                f"FROM watch_event WHERE show_title LIKE ? OR show_key IN ({marks}) "
                f"ORDER BY event_time",
                (q, *keys),
            ).fetchall()

        print(f"观看记录 {len(events)} 条（最近 12 条）：")
        for e in events[-12:]:
            ok = e["item_guid"] in valid
            print(f'  {_stamp(e["event_time"])}  {e["user_name"]:6}  '
                  f'{_label(e["season"], e["episode"]):8}  {"在库里" if ok else "✗ 不在 Season 树"}')
        if len(events) > 12:
            print("  … 只显示最近 12 条")

        # 全量检查：只扫最近 12 条会漏掉更早的孤儿记录
        stray: dict[str, list] = {}
        for e in events:
            if e["item_guid"] not in valid:
                stray.setdefault(e["item_guid"], []).append(e)

        if stray:
            total = sum(len(rows) for rows in stray.values())
            print(f"\n⚠ {total} 条记录的条目不在 Season 树里（{len(stray)} 个条目）：")
            for guid, rows in list(stray.items())[:10]:
                last = max(rows, key=lambda r: r["event_time"])
                users = "、".join(sorted({r["user_name"] for r in rows}))
                print(f'  {_label(last["season"], last["episode"])} · {len(rows)} 条 · '
                      f'最近 {_stamp(last["event_time"])} · {users}')
                facts = con.execute(
                    "SELECT guid, type, title, parent_guid, season_number, episode_number, trim_id "
                    "FROM item WHERE guid = ?", (guid,)
                ).fetchone()
                if facts is None:
                    print("     源库里已经没有这个条目（旧记录，或已被删除/重刮）")
                    continue
                print(f'     条目 type={facts["type"]} title={facts["title"]!r} '
                      f'trim={facts["trim_id"]!r} S{facts["season_number"]}E{facts["episode_number"]}')
                parent = None
                if facts["parent_guid"]:
                    parent = con.execute(
                        "SELECT guid, type, title, trim_id, parent_guid FROM item WHERE guid = ?",
                        (facts["parent_guid"],),
                    ).fetchone()
                if parent is None:
                    print("     父节点缺失（parent_guid 为空或指向不存在的条目）")
                else:
                    print(f'     父节点 type={parent["type"]} title={parent["title"]!r} '
                          f'trim={parent["trim_id"]!r}')
                    if parent["type"] == "Season" and parent["parent_guid"]:
                        tv = con.execute(
                            "SELECT type, title, trim_id FROM item WHERE guid = ?",
                            (parent["parent_guid"],),
                        ).fetchone()
                        if tv:
                            print(f'     祖父节点 type={tv["type"]} title={tv["title"]!r} '
                                  f'trim={tv["trim_id"]!r}')

        seen = max(((int(e["season"] or 0), int(e["episode"] or 0))
                    for e in events if e["item_guid"] in valid), default=(0, 0))
        all_seen = max(((int(e["season"] or 0), int(e["episode"] or 0)) for e in events), default=(0, 0))
        print()
        for key in keys:
            hit = catalog.get(key)
            if hit:
                print(f"  库里最大集（Season 树）：{_label(hit['season'], hit['episode'])}，共 {len(hit['guids'])} 集")
        print(f"  观看记录能对上的最大集：{_label(*seen)}")
        print(f"  观看记录里的最大集（不过滤）：{_label(*all_seen)}")
        if stray:
            print(f"  ⚠ {sum(len(v) for v in stray.values())} 条记录指向的条目不在 Season 树里，"
                  f"日报不会拿它们当「看到第几集」，只计入当天的记录数。")
            print("    常见原因：条目父节点丢失，或另一部剧借用了同一个 trim_id；"
                  "在飞牛里重新刮削/重新匹配通常能归位。")
        else:
            print("  观看记录和库里的集能对上，没有孤儿记录。")
    finally:
        con.close()
    return 0


if __name__ == "__main__":  # python -m app.diagnose 剧名
    raise SystemExit(main(sys.argv[1:]))