#!/usr/bin/env python3
"""Export a design-preview dataset (read-only) from the three trim.media SQLite files.

Not the final ingest pipeline -- just enough to render a realistic mockup.
"""
import json
import sqlite3
import datetime
from collections import Counter, defaultdict

TZ = datetime.timezone(datetime.timedelta(hours=8))
BASE = "tmp"


def canon_show(con):
    """trim_id -> canonical TV row (prefer the one with most episodes)."""
    rows = con.execute("""
        SELECT trim_id, guid, title, posters, tmdb_id,
               (SELECT COUNT(*) FROM item s JOIN item e ON e.parent_guid=s.guid
                WHERE s.parent_guid=item.guid AND s.type='Season' AND e.type='Episode') eps
        FROM item WHERE type='TV' AND trim_id<>''
    """).fetchall()
    best = {}
    for tid, guid, title, posters, tmdb, eps in rows:
        cur = best.get(tid)
        if cur is None or eps > cur[5]:
            best[tid] = (tid, guid, title, posters, tmdb, eps)
    return best


def archive_meta(con):
    """trim_id -> (poster_path, genres, vote, overview) from TMDB archive."""
    out = {}
    for tid, data in con.execute("SELECT trim_id, data FROM item_archive WHERE item_type IN ('tv','movie')"):
        try:
            doc = json.loads(data)
        except Exception:
            continue
        node = doc.get("tv") or doc.get("movie") or {}
        if not isinstance(node, dict) or not node:
            continue
        if tid not in out or (not out[tid][0] and node.get("poster_path")):
            out[tid] = (
                node.get("poster_path") or "",
                [g.get("name") for g in (node.get("genres") or []) if isinstance(g, dict)],
                node.get("vote_average") or 0,
                (node.get("overview") or "")[:220],
            )
    return out


def main():
    con = sqlite3.connect(f"{BASE}/trimmedia.db")
    con.execute(f"ATTACH DATABASE '{BASE}/trimmedia_ext.db' AS ext")

    shows = canon_show(con)
    meta = archive_meta(con)

    # 飞牛删除账号时只把 user.status 置 0 并把名字改成 xxx_deleted_<ts>，
    # 这些账号不参与任何统计（源库仍以只读方式打开，不做任何修改）。
    users = {}
    for g, n, status in con.execute("SELECT guid, username, status FROM user"):
        if status == 0 or "_deleted_" in (n or ""):
            continue
        users[g] = n
    libs = {g: t for g, t in con.execute(
        "SELECT item_guid, (SELECT title FROM item WHERE guid=item_guid) FROM mediadb_config")}

    rows = con.execute("""
        SELECT p.user_guid, p.item_guid, p.ts, p.watched, p.create_time, p.update_time,
               i.type, i.title, i.trim_id, i.season_number, i.episode_number, i.runtime,
               s.guid, tv.guid, tv.trim_id, tv.title,
               (SELECT d.title FROM item_ancestor a JOIN item d ON d.guid=a.ancestor_guid
                WHERE a.item_guid=i.guid LIMIT 1) lib
        FROM item_user_play p
        JOIN item i ON i.guid=p.item_guid
        LEFT JOIN item s ON s.guid=i.parent_guid
        LEFT JOIN item tv ON tv.guid=s.parent_guid AND tv.type='TV'
        WHERE p.visible=1
        ORDER BY p.update_time
    """).fetchall()

    dur_cache = {}
    def duration(item_guid):
        if item_guid in dur_cache:
            return dur_cache[item_guid]
        r = con.execute("""SELECT MIN(ms.duration) FROM item_media m
                           JOIN media_stream ms ON ms.media_guid=m.guid
                           WHERE m.item_guid=? AND ms.codec_type='video' AND ms.duration>0""",
                        (item_guid,)).fetchone()
        v = (r[0] or 0) if r else 0
        dur_cache[item_guid] = v
        return v

    events = []
    for (ug, ig, ts, watched, ct, ut, itype, ititle, itrim, sn, en, runtime,
         season_guid, tv_guid, tv_trim, tv_title, lib) in rows:
        if ug not in users:  # 已删除账号 / user 表里已不存在的 guid：不统计
            continue
        # resolve show identity
        if itype in ("Episode", "Season", "TV"):
            tid = tv_trim or itrim or ""
            show = shows.get(tid)
            title = (show[2] if show else (tv_title or ititle or "未命名"))
            poster = meta.get(tid, ("",))[0]
            key = tid or (tv_guid or ig)
            genres = meta.get(tid, ("", [], 0, ""))[1]
        elif itype == "Movie":
            key = itrim or ig
            title = ititle or "未命名"
            poster = meta.get(itrim, ("",))[0]
            genres = meta.get(itrim, ("", [], 0, ""))[1]
        else:
            key = ig
            title = (ititle or "").strip() or "未命名"
            poster = ""
            genres = []

        d = duration(ig)
        total = d or (runtime * 60 if runtime else 0)
        pct = round(min(100, ts / total * 100)) if (total and ts) else None

        events.append({
            "u": ug, "user": users.get(ug, "?"),
            "k": key, "show": title, "poster": poster, "genres": genres,
            "type": itype, "ep": ititle,
            "sn": sn, "en": en,
            "t": ut, "ct": ct,
            "w": watched, "ts": ts, "dur": total, "pct": pct,
            "lib": lib or "",
        })

    # day aggregation
    days = defaultdict(lambda: {"n": 0, "u": Counter(), "eps": 0, "shows": set()})
    for e in events:
        day = datetime.datetime.fromtimestamp(e["t"] / 1000, TZ).strftime("%Y-%m-%d")
        d = days[day]
        d["n"] += 1
        d["u"][e["user"]] += 1
        if e["type"] == "Episode":
            d["eps"] += 1
        d["shows"].add(e["k"])

    now = datetime.datetime.now(TZ)
    data = {
        "now": int(now.timestamp() * 1000),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "users": [{"guid": g, "name": n} for g, n in users.items() if n in {e["user"] for e in events}],
        "libraries": sorted({e["lib"] for e in events if e["lib"]}),
        "events": events,
        "days": {k: {"n": v["n"], "u": dict(v["u"]), "eps": v["eps"], "shows": len(v["shows"])}
                 for k, v in days.items()},
    }

    out = "mockup/data.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("events", len(events), "days", len(days),
          "shows", len({e["k"] for e in events}), "posters", sum(1 for e in events if e["poster"]))
    print("range", min(e["t"] for e in events), max(e["t"] for e in events))
    print("size", len(json.dumps(data, ensure_ascii=False)))


if __name__ == "__main__":
    main()
