"""Read-side queries: timeline feed, activity strip, show matrix, rail cards."""
from __future__ import annotations

import calendar
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

from . import config, db, posters

PALETTE = ["#5b9cf8", "#f0a44a", "#5fc48a", "#c07ef0", "#e0716f", "#4fc3d9", "#c9a227", "#8f9bb3"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def now_ms() -> int:
    return int(datetime.now(config.TZ).timestamp() * 1000)


def load_events() -> list[dict]:
    with db.session_scope() as con:
        rows = con.execute("SELECT * FROM watch_event ORDER BY event_time DESC").fetchall()
    return [dict(r) for r in rows if r["event_time"]]


def day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, config.TZ).date()


def clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%H:%M")


def day_label(d: date) -> str:
    today = datetime.now(config.TZ).date()
    if d == today:
        prefix = "今天 · "
    elif d == today - timedelta(days=1):
        prefix = "昨天 · "
    else:
        prefix = ""
    return f"{prefix}{d.month} 月 {d.day} 日"


def color_of(name: str) -> str:
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def hue_of(name: str) -> int:
    return sum(ord(c) for c in name) * 37 % 360


def initial(name: str) -> str:
    return name[:1].upper() if name else "?"


def event_count(rows: list[dict]) -> int:
    """A collapsed batch counts once per batch, not once per member row."""
    seen: set[str] = set()
    total = 0
    for e in rows:
        group = e["bulk_group"]
        if not group:
            total += 1
        elif group not in seen:
            seen.add(group)
            total += e["bulk_size"] or 1
    return total


def episode_label(e: dict) -> str:
    return episode_code(e["season"], e["episode"])


def episode_code(season, episode) -> str:
    if season and episode:
        return f"S{int(season):02d}E{int(episode):02d}"
    if episode:
        return f"E{int(episode):02d}"
    return ""


def library_episodes(keys: list[str]) -> dict[str, dict]:
    """库里每部剧的集清单：{key: {"season", "episode", "guids"}}。

    只认 `Episode → Season → TV` 这条链，也就是飞牛界面上真正能看到的集。这里的
    「最新」是媒体库里存在的最新一集，和「谁看到哪一集」是两件事。

    `guids` 很关键：日报用它把观看记录和库里的集对齐。如果一条观看记录指向的
    Episode 不在树里（父节点丢失、被别的条目借用了 trim_id……），它就只算当天
    看过，不能拿来当「看到第几集」——否则会出现「看到 E30 / 更新至 E22」这种
    自相矛盾的卡片。
    """
    keys = [k for k in keys if k]
    path = config.SNAP_DIR / "trimmedia.db"
    if not keys or not path.exists():
        return {}

    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            for i in range(0, len(keys), 400):  # SQLite 的参数上限是 999
                chunk = keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                rows = con.execute(
                    "SELECT COALESCE(NULLIF(TRIM(tv.trim_id), ''), tv.guid) AS key, "
                    "       e.guid AS guid, s.season_number AS season, "
                    "       e.episode_number AS episode "
                    "FROM item e "
                    "JOIN item s  ON s.guid = e.parent_guid AND s.type = 'Season' "
                    "JOIN item tv ON tv.guid = s.parent_guid AND tv.type = 'TV' "
                    f"WHERE COALESCE(NULLIF(TRIM(tv.trim_id), ''), tv.guid) IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    season, episode = int(row["season"] or 0), int(row["episode"] or 0)
                    slot = out.setdefault(row["key"], {"season": 0, "episode": 0, "guids": set()})
                    slot["guids"].add(row["guid"])
                    if (season, episode) > (slot["season"], slot["episode"]):
                        slot["season"], slot["episode"] = season, episode
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


# ------------------------------------------------------------------ filters
def filter_state(days: int, raw_users: str, raw_libs: str) -> dict:
    excl = db.exclusions()
    cats = db.catalogs()
    all_users = [c["name"] for c in cats["account"] if c["events"] and c["name"] not in excl["account"]]
    all_libs = [c["name"] for c in cats["library"] if c["events"] and c["name"] not in excl["library"]]

    users = [u for u in (raw_users or "").split(",") if u] or all_users
    users = [u for u in users if u in all_users]
    libs = [l for l in (raw_libs or "").split(",") if l] or all_libs
    libs = [l for l in libs if l in all_libs]
    return {
        "days": days,
        "users": users,
        "libs": libs,
        "all_users": all_users,
        "all_libs": all_libs,
        "exclusions": excl,
        "catalogs": cats,
    }


def query_url(path: str, state: dict, days: int | None = None,
              users: list[str] | None = None, libs: list[str] | None = None) -> str:
    """Build a filter link, omitting parameters that still mean "everything"."""
    d = state["days"] if days is None else days
    us = state["users"] if users is None else users
    ls = state["libs"] if libs is None else libs
    params: dict[str, object] = {"days": d}
    if sorted(us) != sorted(state["all_users"]):
        params["u"] = ",".join(us)
    if sorted(ls) != sorted(state["all_libs"]):
        params["l"] = ",".join(ls)
    return f"{path}?{urlencode(params)}"


def nav_qs(state: dict) -> str:
    return query_url("", state)


def toggle_url(path: str, state: dict, kind: str, name: str) -> str:
    users, libs = list(state["users"]), list(state["libs"])
    target = users if kind == "user" else libs
    if name in target:
        target.remove(name)
    else:
        target.append(name)
    return query_url(path, state, users=users, libs=libs)


def visible(events: list[dict], state: dict, days: int | None = None) -> list[dict]:
    span = state["days"] if days is None else days
    cutoff = now_ms() - span * config.DAY_MS if span else 0
    users, libs = set(state["users"]), set(state["libs"])
    out = []
    for e in events:
        if e["event_time"] < cutoff:
            continue
        if e["user_name"] not in users:
            continue
        if (e["library"] or config.DEAD_LIBRARY) not in libs:
            continue
        out.append(e)
    return out


def excluded_summary(events: list[dict], state: dict) -> dict:
    excl = state["exclusions"]
    hit = [e for e in events if e["user_name"] in excl["account"] or (e["library"] and e["library"] in excl["library"])]
    libs = sorted({e["library"] for e in hit if e["library"] and e["library"] in excl["library"]})
    return {"count": len(hit), "libs": libs, "users": sorted(excl["account"])}


# -------------------------------------------------------------------- pages
def timeline_context(state: dict, path: str = "/") -> dict:
    events = load_events()
    evs = visible(events, state)
    days = defaultdict(list)
    for e in evs:
        days[day_of(e["event_time"])].append(e)

    feed = []
    for d in sorted(days, reverse=True):
        rows = days[d]
        people: list[str] = []
        for e in rows:
            if e["user_name"] not in people:
                people.append(e["user_name"])
        feed.append(
            {
                "date": d,
                "label": day_label(d),
                "weekday": WEEKDAYS[d.weekday()],
                "people": people[:4],
                "count": event_count(rows),
                "rows": _feed_items(rows),
            }
        )

    pcts = [e["pct"] for e in evs if e["pct"] and not e["watched"]]
    in_progress = {e["show_key"] for e in evs if not e["watched"]}
    kpis = {
        "events": event_count(evs),
        "active_days": len(days),
        "shows": len(in_progress),
        "avg_pct": round(sum(pcts) / len(pcts)) if pcts else 0,
    }

    return {
        "state": state,
        "total": len(events),
        "span": (f'{day_of(events[-1]["event_time"]):%Y-%m} → {day_of(events[0]["event_time"]):%Y-%m-%d}'
                 if events else ""),
        "kpis": kpis,
        "strip": activity_strip(events, state),
        "feed": feed,
        "rail": _rail(evs, events, state),
        "ranges": _ranges(state, path),
        "user_filters": _side_filters(state, path, "user"),
        "lib_filters": _side_filters(state, path, "library"),
    }


def _side_filters(state: dict, path: str, kind: str) -> list[dict]:
    cat = "account" if kind == "user" else "library"
    chosen = set(state["users"] if kind == "user" else state["libs"])
    names = state["all_users"] if kind == "user" else state["all_libs"]
    counts = {c["name"]: c["events"] for c in state["catalogs"][cat]}
    return [
        {
            "name": name,
            "count": counts.get(name, 0),
            "on": name in chosen,
            "href": toggle_url(path, state, kind, name),
            "href_detail": f"/user/{quote(name)}" if kind == "user" else "",
            "color": color_of(name) if kind == "user" else "",
        }
        for name in names
    ]


def default_filters(path: str = "/", days: int = 30) -> dict:
    """Sidebar member/library lists for pages that do not carry filter state of their own."""
    state = filter_state(days, "", "")
    return {
        "user_filters": _side_filters(state, path, "user"),
        "lib_filters": _side_filters(state, path, "library"),
    }


def _feed_items(rows: list[dict]) -> list[dict]:
    seen_groups: set[str] = set()
    out: list[dict] = []
    for e in rows:
        group = e["bulk_group"]
        if group:
            if group in seen_groups:
                continue
            seen_groups.add(group)
            members = [r for r in rows if r["bulk_group"] == group]
            shows = []
            for m in members:
                if m["show_title"] not in shows:
                    shows.append(m["show_title"])
            out.append(
                {
                    "bulk": True,
                    "count": len(members),
                    "watched": sum(1 for m in members if m["watched"]),
                    "user": e["user_name"],
                    "key": e["show_key"],
                    "time": clock(max(m["event_time"] for m in members)),
                    "shows": shows[:3],
                    "more": max(0, len(shows) - 3),
                    "color": color_of(e["user_name"]),
                }
            )
            continue
        out.append(
            {
                "bulk": False,
                "id": e["id"],
                "key": e["show_key"],
                "show": e["show_title"],
                "ep": episode_label(e),
                "title": e["episode_title"] or "",
                "user": e["user_name"],
                "time": clock(e["event_time"]),
                "pct": e["pct"],
                "watched": e["watched"],
                "library": e["library"] or "库已删除",
                "genres": e["genres"] or "",
                "poster": e["poster"] or "",
                "hue": hue_of(e["show_title"]),
                "initial": initial(e["show_title"]),
                "is_movie": e["item_type"] == "Movie",
            }
        )
    return out


def activity_strip(events: list[dict], state: dict, days: int = 90) -> dict:
    today = datetime.now(config.TZ).date()
    start = today - timedelta(days=days - 1)
    users = [u for u in state["all_users"] if any(e["user_name"] == u for e in events)]
    allowed = set(state["users"])
    libs = set(state["libs"])

    counts: dict[tuple[str, date], int] = Counter()
    for e in events:
        if e["user_name"] not in allowed or (e["library"] or config.DEAD_LIBRARY) not in libs:
            continue
        d = day_of(e["event_time"])
        if start <= d <= today:
            counts[(e["user_name"], d)] += 1

    rows = []
    for u in users:
        cells = [
            {"date": start + timedelta(days=i), "n": counts.get((u, start + timedelta(days=i)), 0)}
            for i in range(days)
        ]
        rows.append(
            {
                "name": u,
                "color": color_of(u),
                "initial": initial(u),
                "total": sum(c["n"] for c in cells),
                "cells": cells,
                "on": u in allowed,
            }
        )
    months = []
    for i in range(days):
        d = start + timedelta(days=i)
        if i == 0 or d.day == 1:
            months.append({"i": i, "label": f"{d.month}月"})
    return {"rows": rows, "start": start, "end": today, "months": months, "days": days}


def _rail(evs: list[dict], all_events: list[dict], state: dict) -> dict:
    latest: dict[tuple[str, str], dict] = {}
    for e in sorted(evs, key=lambda x: x["event_time"]):
        latest[(e["user_name"], e["show_key"])] = e

    progress = []
    for (user, _key), e in sorted(latest.items(), key=lambda kv: kv[1]["event_time"], reverse=True):
        if e["watched"] or (e["pct"] is not None and e["pct"] >= 98):
            continue
        progress.append(
            {
                "show": e["show_title"],
                "ep": episode_label(e),
                "user": user,
                "pct": e["pct"] if e["pct"] is not None else 12,
                "key": e["show_key"],
                "poster": e["poster"] or "",
                "color": color_of(e["show_title"]),
                "hue": hue_of(e["show_title"]),
                "initial": initial(e["show_title"]),
            }
        )
        if len(progress) >= 7:
            break

    by_show: dict[str, set[str]] = defaultdict(set)
    last_seen: dict[str, int] = defaultdict(int)
    for e in evs:
        by_show[e["show_key"]].add(e["user_name"])
        last_seen[e["show_key"]] = max(last_seen[e["show_key"]], e["event_time"])
    duo = [
        {"show": next(x["show_title"] for x in evs if x["show_key"] == k), "key": k,
         "users": sorted(v), "n": len(v)}
        for k, v in sorted(by_show.items(), key=lambda kv: -last_seen[kv[0]])
        if len(v) >= 2
    ][:6]

    return {"progress": progress, "duo": duo, "excluded": excluded_summary(all_events, state)}


RANGES = (7, 30, 90, 0)
RANGE_LABELS = ((7, "近 7 天"), (30, "近 30 天"), (90, "近 90 天"), (0, "全部"))


def _ranges(state: dict, path: str) -> list[dict]:
    out = []
    for value, label in RANGE_LABELS:
        out.append(
            {
                "label": label,
                "on": state["days"] == value,
                "href": query_url(path, state, days=value),
            }
        )
    return out


def matrix_context(state: dict) -> dict:
    events = load_events()
    evs = visible(events, state, days=0)
    users = [u for u in state["all_users"] if any(e["user_name"] == u for e in evs)]

    shows: dict[str, dict] = {}
    for e in evs:
        g = shows.setdefault(
            e["show_key"],
            {"key": e["show_key"], "show": e["show_title"], "genres": e["genres"] or "",
             "poster": e["poster"] or "", "cells": {}, "last": 0, "events": 0},
        )
        c = g["cells"].setdefault(e["user_name"], {"n": 0, "last": 0, "pct": None, "ep": 0})
        c["n"] += 1
        g["events"] += 1
        g["last"] = max(g["last"], e["event_time"])
        if e["event_time"] >= c["last"]:
            c["last"] = e["event_time"]
            c["pct"] = e["pct"]
            c["ep"] = e["episode"] or 0

    rows = []
    for g in sorted(shows.values(), key=lambda x: -x["last"]):
        rows.append(
            {
                "key": g["key"],
                "show": g["show"],
                "genres": g["genres"],
                "poster": g["poster"],
                "color": color_of(g["show"]),
                "hue": hue_of(g["show"]),
                "initial": initial(g["show"]),
                "events": g["events"],
                "cells": [g["cells"].get(u) for u in users],
            }
        )
    return {
        "state": state,
        "users": users,
        "rows": rows,
        "together": _together(evs),
        "colors": {u: color_of(u) for u in users},
        "strip": activity_strip(events, state),
        "excluded": excluded_summary(events, state),
        "user_filters": _side_filters(state, "/matrix", "user"),
        "lib_filters": _side_filters(state, "/matrix", "library"),
    }


# ------------------------------------------------------------ detail pages
def stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%m-%d %H:%M")


def _hidden(e: dict, excl: dict) -> bool:
    """Excluded accounts and libraries are hidden everywhere, detail pages too."""
    return e["user_name"] in excl["account"] or (e["library"] or config.DEAD_LIBRARY) in excl["library"]


def _snapshot_episode_count(show_key: str) -> int:
    """How many episodes the show has, read from the snapshot (never the source)."""
    path = config.SNAP_DIR / "trimmedia.db"
    if not show_key or not path.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM item e "
                "JOIN item s ON s.guid = e.parent_guid AND s.type = 'Season' "
                "JOIN item t ON t.guid = s.parent_guid AND t.trim_id = ? "
                "WHERE e.type = 'Episode'",
                (show_key,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def show_context(show_key: str) -> dict | None:
    """Everything known about one show: who watched it, how far, episode by episode."""
    excl = db.exclusions()
    evs = [e for e in load_events() if e["show_key"] == show_key and not _hidden(e, excl)]
    if not evs:
        return None
    head = evs[0]
    is_movie = head["item_type"] == "Movie"
    total = _snapshot_episode_count(show_key)

    by_user: dict[str, list[dict]] = defaultdict(list)
    for e in evs:
        by_user[e["user_name"]].append(e)

    people = []
    for name, es in by_user.items():
        touched = {e["item_guid"] for e in es}
        done = {e["item_guid"] for e in es if e["watched"] or (e["pct"] or 0) >= 98}
        seen_eps = [e for e in es if e["episode"]]
        far = max(seen_eps, key=lambda x: (x["season"] or 0, x["episode"] or 0)) if seen_eps else None
        last = max(e["event_time"] for e in es)
        far_ep = (far["episode"] or 0) if far else 0
        if total and far_ep:
            pct = round(far_ep * 100 / total)
        else:
            pct = max((e["pct"] or 0) for e in es)
        people.append(
            {
                "name": name,
                "color": color_of(name),
                "initial": initial(name),
                "events": len(es),
                "touched": len(touched),
                "done": len(done),
                "far_ep": far_ep,
                "pct": pct,
                "far": episode_label(far) if far else "",
                "last": last,
                "last_label": stamp(last),
                "last_day": day_label(day_of(last)),
            }
        )
    people.sort(key=lambda p: -p["last"])

    episodes: dict[str, dict] = {}
    for e in evs:
        g = episodes.setdefault(
            e["item_guid"],
            {"guid": e["item_guid"], "season": e["season"], "episode": e["episode"],
             "title": e["episode_title"] or "", "users": [], "pct": None, "done": False,
             "n": 0, "last": 0},
        )
        g["n"] += 1
        if e["user_name"] not in g["users"]:
            g["users"].append(e["user_name"])
        if e["event_time"] >= g["last"]:
            g["last"] = e["event_time"]
            g["pct"] = e["pct"]
            g["done"] = bool(e["watched"]) or (e["pct"] or 0) >= 98

    eps = []
    for g in sorted(episodes.values(), key=lambda x: (x["season"] or 0, x["episode"] or 0, x["last"])):
        g["label"] = episode_label(g)
        g["time"] = stamp(g["last"])
        g["people"] = [{"name": u, "color": color_of(u), "initial": initial(u)} for u in g["users"]]
        eps.append(g)
    if is_movie:
        eps = []

    done_all = {e["item_guid"] for e in evs if e["watched"] or (e["pct"] or 0) >= 98}
    first = min(e["event_time"] for e in evs)
    kpis = {
        "people": len(people),
        "events": len(evs),
        "done": len(done_all),
        "total": total if total and not is_movie else 0,
        "pct": head["pct"] or 0,
        "watched": bool(head["watched"]),
        "first": datetime.fromtimestamp(first / 1000, config.TZ).strftime("%Y-%m-%d"),
    }

    recent = [
        {
            "user": e["user_name"],
            "color": color_of(e["user_name"]),
            "ep": episode_label(e),
            "title": e["episode_title"] or "",
            "time": stamp(e["event_time"]),
            "day": day_label(day_of(e["event_time"])),
            "pct": e["pct"],
            "watched": bool(e["watched"]),
        }
        for e in evs[:20]
    ]

    return {
        "key": show_key,
        "is_movie": is_movie,
        "head": {
            "title": head["show_title"],
            "library": head["library"] or "未归属",
            "genres": head["genres"] or "",
            "poster": head["poster"] or "",
            "hue": hue_of(head["show_title"]),
            "initial": initial(head["show_title"]),
            "kind": "电影" if is_movie else "剧集",
        },
        "people": people,
        "eps": eps,
        "kpis": kpis,
        "recent": recent,
    }


def user_context(name: str) -> dict | None:
    """One person's habits: how much, what, when, and who they watch alongside."""
    excl = db.exclusions()
    if name in excl["account"]:
        return None
    all_events = load_events()
    evs = [e for e in all_events if e["user_name"] == name and not _hidden(e, excl)]
    if not evs:
        return None

    days = sorted({day_of(e["event_time"]) for e in evs})
    longest = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    now = datetime.now(config.TZ).date()
    streak_now = longest if days and (now - days[-1]).days <= 1 else 0
    if streak_now and len(days) > 1:
        tail = 1
        for i in range(len(days) - 1, 0, -1):
            if (days[i] - days[i - 1]).days == 1:
                tail += 1
            else:
                break
        streak_now = tail

    position: dict[str, int] = {}
    for e in evs:
        position[e["item_guid"]] = max(position.get(e["item_guid"], 0), e["position"] or 0)
    minutes = sum(position.values()) // 60

    libs = Counter(e["library"] or "未归属" for e in evs)
    lib_rows = [
        {"name": k, "n": v, "pct": round(v * 100 / len(evs))} for k, v in libs.most_common()
    ]

    hours = [0] * 24
    for e in evs:
        hours[datetime.fromtimestamp(e["event_time"] / 1000, config.TZ).hour] += 1
    peak = max(hours)
    hour_rows = [{"h": i, "n": n, "pct": round(n * 100 / peak) if peak else 0, "label": f"{i:02d}"} for i, n in enumerate(hours)]
    weekdays = [0] * 7
    for e in evs:
        weekdays[day_of(e["event_time"]).weekday()] += 1
    wd_peak = max(weekdays)
    wd_rows = [{"name": WEEKDAYS[i], "n": n, "pct": round(n * 100 / wd_peak) if wd_peak else 0} for i, n in enumerate(weekdays)]

    latest: dict[str, dict] = {}
    agg: dict[str, dict] = {}
    for e in sorted(evs, key=lambda x: x["event_time"]):
        latest[e["show_key"]] = e
        a = agg.setdefault(e["show_key"], {"n": 0, "eps": set(), "done": set()})
        a["n"] += 1
        a["eps"].add(e["item_guid"])
        if e["watched"] or (e["pct"] or 0) >= 98:
            a["done"].add(e["item_guid"])

    shows = []
    for key, e in latest.items():
        a = agg[key]
        finished_show = len(a["done"]) == len(a["eps"]) and len(a["eps"]) >= 2
        shows.append(
            {
                "key": key, "show": e["show_title"], "ep": episode_label(e),
                "title": e["episode_title"] or "", "pct": e["pct"] if e["pct"] is not None else 0,
                "done": finished_show, "events": a["n"], "eps": len(a["eps"]), "done_eps": len(a["done"]),
                "last": e["event_time"], "time": stamp(e["event_time"]),
                "poster": e["poster"] or "", "hue": hue_of(e["show_title"]),
                "initial": initial(e["show_title"]),
            }
        )
    shows.sort(key=lambda x: -x["last"])
    watching = [s for s in shows if not s["done"]]
    following = watching[:8]
    finished = [s for s in shows if s["done"]]

    mine = {e["show_key"] for e in evs}
    others: dict[str, set[str]] = defaultdict(set)
    for e in all_events:
        if e["user_name"] == name or _hidden(e, excl):
            continue
        if e["show_key"] in mine:
            others[e["user_name"]].add(e["show_key"])
    coop = [
        {"name": u, "n": len(v), "color": color_of(u), "initial": initial(u)}
        for u, v in sorted(others.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]
    ]

    recent = [
        {
            "show": e["show_title"], "key": e["show_key"], "ep": episode_label(e),
            "title": e["episode_title"] or "", "time": stamp(e["event_time"]),
            "day": day_label(day_of(e["event_time"])), "pct": e["pct"],
            "watched": bool(e["watched"]),
        }
        for e in evs[:20]
    ]

    return {
        "name": name,
        "color": color_of(name),
        "mark": initial(name),
        "kpis": {
            "events": len(evs),
            "days": len(days),
            "following": len(watching),
            "minutes": minutes,
        },
        "first": datetime.fromtimestamp(min(e["event_time"] for e in evs) / 1000, config.TZ).strftime("%Y-%m-%d"),
        "last": stamp(max(e["event_time"] for e in evs)),
        "streak": longest,
        "streak_now": streak_now,
        "peak_hour": f"{hours.index(peak):02d}:00" if peak else "—",
        "libs": lib_rows,
        "hours": hour_rows,
        "weekdays": wd_rows,
        "following": following,
        "finished": finished[:6],
        "coop": coop,
        "recent": recent,
    }


# ------------------------------------------------------- shared aggregation
def allowed_events() -> list[dict]:
    """Every event the exclusions still allow; the analysis pages start here."""
    excl = db.exclusions()
    return [e for e in load_events() if not _hidden(e, excl)]


def minutes_of(evs: list[dict]) -> int:
    """Estimated minutes: the furthest position reached per episode, summed."""
    pos: dict[str, int] = {}
    for e in evs:
        pos[e["item_guid"]] = max(pos.get(e["item_guid"], 0), e["position"] or 0)
    return sum(pos.values()) // 60


def _show_index(evs: list[dict]) -> list[dict]:
    g: dict[str, dict] = {}
    for e in evs:
        s = g.setdefault(
            e["show_key"],
            {"key": e["show_key"], "show": e["show_title"], "poster": e["poster"] or "",
             "hue": hue_of(e["show_title"]), "initial": initial(e["show_title"]),
             "library": e["library"] or "未归属", "events": 0, "users": set(), "items": set(),
             "done": set(), "last": 0, "first": 0, "minutes": 0, "pos": {}},
        )
        s["events"] += 1
        s["users"].add(e["user_name"])
        s["items"].add(e["item_guid"])
        s["last"] = max(s["last"], e["event_time"])
        s["first"] = e["event_time"] if not s["first"] else min(s["first"], e["event_time"])
        s["pos"][e["item_guid"]] = max(s["pos"].get(e["item_guid"], 0), e["position"] or 0)
        if e["watched"] or (e["pct"] or 0) >= 98:
            s["done"].add(e["item_guid"])
    out = []
    for s in g.values():
        s["people"] = len(s.pop("users"))
        s["episodes"] = len(s.pop("items"))
        s["done_eps"] = len(s.pop("done"))
        s["minutes"] = sum(s.pop("pos").values()) // 60
        s["last_label"] = stamp(s["last"])
        s["first_label"] = stamp(s["first"])
        out.append(s)
    out.sort(key=lambda s: (-s["events"], s["show"]))
    return out


def _people_index(evs: list[dict]) -> list[dict]:
    g: dict[str, dict] = {}
    for e in evs:
        u = g.setdefault(
            e["user_name"],
            {"name": e["user_name"], "color": color_of(e["user_name"]), "initial": initial(e["user_name"]),
             "events": 0, "items": set(), "shows": set(), "days": set(), "pos": {}, "done": set(),
             "last": 0, "first": 0},
        )
        u["events"] += 1
        u["items"].add(e["item_guid"])
        u["shows"].add(e["show_key"])
        u["days"].add(day_of(e["event_time"]))
        u["pos"][e["item_guid"]] = max(u["pos"].get(e["item_guid"], 0), e["position"] or 0)
        u["last"] = max(u["last"], e["event_time"])
        u["first"] = e["event_time"] if not u["first"] else min(u["first"], e["event_time"])
        if e["watched"] or (e["pct"] or 0) >= 98:
            u["done"].add(e["item_guid"])
    out = []
    for u in g.values():
        u["episodes"] = len(u.pop("items"))
        u["shows_n"] = len(u.pop("shows"))
        u["days_n"] = len(u.pop("days"))
        u["done_eps"] = len(u.pop("done"))
        u["minutes"] = sum(u.pop("pos").values()) // 60
        u["last_label"] = stamp(u["last"])
        u["first_label"] = stamp(u["first"])
        out.append(u)
    out.sort(key=lambda u: (-u["minutes"], u["name"]))
    return out


# -------------------------------------------------------------------- search
def search_context(raw_q: str) -> dict:
    q = (raw_q or "").strip()
    evs = allowed_events()
    shows = _show_index(evs)
    people = _people_index(evs)
    if not q:
        return {"q": q, "hot": shows[:8], "members": people, "shows": [], "users": [], "events": [],
                "event_total": 0, "total_events": len(evs), "total_shows": len(shows)}
    ql = q.lower()
    hits = [s for s in shows if ql in s["show"].lower()]
    users = [u for u in people if ql in u["name"].lower()]
    matched = []
    for e in evs:
        label = episode_label(e)
        hay = f'{e["show_title"]}\n{e["episode_title"] or ""}\n{label}\n{e["user_name"]}'.lower()
        if ql in hay:
            matched.append(e)
    rows = [
        {
            "key": e["show_key"], "show": e["show_title"], "ep": episode_label(e),
            "title": e["episode_title"] or "", "user": e["user_name"],
            "color": color_of(e["user_name"]), "time": stamp(e["event_time"]),
            "pct": e["pct"], "watched": bool(e["watched"]),
            "poster": e["poster"] or "", "hue": hue_of(e["show_title"]),
            "initial": initial(e["show_title"]), "library": e["library"] or "未归属",
        }
        for e in matched[:60]
    ]
    return {"q": q, "hot": [], "members": [], "shows": hits[:12], "users": users[:8],
            "events": rows, "event_total": len(matched), "total_events": len(evs), "total_shows": len(shows)}


# -------------------------------------------------------------------- recap
def _period_events(evs: list[dict], mode: str, period: str) -> list[dict]:
    if mode == "year":
        year = int(period)
        return [e for e in evs if day_of(e["event_time"]).year == year]
    year, month = int(period[:4]), int(period[5:7])
    return [e for e in evs if (day_of(e["event_time"]).year, day_of(e["event_time"]).month) == (year, month)]


def recap_context(mode: str, period: str) -> dict:
    evs = allowed_events()
    mode = "year" if mode == "year" else "month"
    months = sorted({(day_of(e["event_time"]).year, day_of(e["event_time"]).month) for e in evs})
    years = sorted({y for y, _ in months})
    if not months:
        return {"mode": mode, "empty": True}

    if mode == "year":
        keys = [str(y) for y in years]
        if period not in keys:
            period = keys[-1]
        label = f"{period} 年"
        index = keys.index(period)
        prev_p = keys[index - 1] if index > 0 else ""
        next_p = keys[index + 1] if index + 1 < len(keys) else ""
        prev_l = (f"{prev_p} 年", f"/recap?mode=year&p={prev_p}") if prev_p else None
        next_l = (f"{next_p} 年", f"/recap?mode=year&p={next_p}") if next_p else None
    else:
        keys = [f"{y:04d}-{m:02d}" for y, m in months]
        if period not in keys:
            period = keys[-1]
        label = f"{period[:4]} 年 {int(period[5:7])} 月"
        index = keys.index(period)
        prev_p = keys[index - 1] if index > 0 else ""
        next_p = keys[index + 1] if index + 1 < len(keys) else ""
        prev_l = (prev_p, f"/recap?mode=month&p={prev_p}") if prev_p else None
        next_l = (next_p, f"/recap?mode=month&p={next_p}") if next_p else None

    cur = _period_events(evs, mode, period)
    prev = _period_events(evs, mode, prev_p) if prev_p else []

    days = defaultdict(int)
    for e in cur:
        days[day_of(e["event_time"])] += 1
    busiest = None
    if days:
        d = max(days, key=lambda k: days[k])
        busiest = {"date": f"{d.month} 月 {d.day} 日", "weekday": WEEKDAYS[d.weekday()], "n": days[d]}

    shows = [s for s in _show_index(cur)][:6]
    people = [u for u in _people_index(cur)]
    for u in people:
        u["max_events"] = people[0]["events"] if people else 1
    people = people[:8]

    born = {s["key"]: s["first"] for s in _show_index(evs)}
    start = min((e["event_time"] for e in cur), default=0)
    end = max((e["event_time"] for e in cur), default=0)
    fresh = [
        s for s in _show_index(cur)
        if start and start <= born.get(s["key"], 0) <= end and s["events"] >= 1
    ][:8]

    if mode == "year":
        counts = Counter(day_of(e["event_time"]).month for e in cur)
        trend = [{"label": f"{m}月", "n": counts.get(m, 0)} for m in range(1, 13)]
    else:
        year, month = int(period[:4]), int(period[5:7])
        last = calendar.monthrange(year, month)[1]
        counts = Counter(day_of(e["event_time"]).day for e in cur)
        trend = [{"label": str(d), "n": counts.get(d, 0)} for d in range(1, last + 1)]
    peak = max((t["n"] for t in trend), default=0)
    for t in trend:
        t["pct"] = round(t["n"] * 100 / peak) if peak else 0

    cur_min = minutes_of(cur)
    prev_min = minutes_of(prev)
    cur_n = event_count(cur)
    prev_n = event_count(prev)

    def delta(now_v: int, was_v: int, floor: int) -> int | None:
        """Compared against a base too small to be meaningful, a delta is just noise."""
        if was_v < floor:
            return None
        return round((now_v - was_v) * 100 / was_v)

    return {
        "mode": mode,
        "empty": False,
        "period": period,
        "label": label,
        "prev": prev_l,
        "next": next_l,
        "kpis": {
            "events": cur_n,
            "raw": len(cur),
            "minutes": cur_min,
            "hours": cur_min // 60,
            "people": len({e["user_name"] for e in cur}),
            "days": len(days),
            "shows": len({e["show_key"] for e in cur}),
            "delta_events": delta(cur_n, prev_n, 10),
            "delta_minutes": delta(cur_min, prev_min, 60),
        },
        "busiest": busiest,
        "shows": shows,
        "people": people,
        "fresh": fresh,
        "trend": trend,
        "trend_days": mode == "month",
        "prev_label": (prev_l[0] if prev_l else ""),
    }


# ------------------------------------------------------------------ dropouts
def dropout_context(days: int) -> dict:
    evs = allowed_events()
    now = now_ms()
    cutoff = now - days * config.DAY_MS
    latest: dict[tuple[str, str], dict] = {}
    agg: dict[tuple[str, str], dict] = {}
    for e in sorted(evs, key=lambda x: x["event_time"]):
        latest[(e["user_name"], e["show_key"])] = e
        a = agg.setdefault((e["user_name"], e["show_key"]), {"items": set(), "done": set()})
        a["items"].add(e["item_guid"])
        if e["watched"] or (e["pct"] or 0) >= 98:
            a["done"].add(e["item_guid"])

    rows = []
    for (user, key), e in latest.items():
        if e["watched"] or (e["pct"] or 0) >= 98 or e["event_time"] > cutoff:
            continue
        a = agg[(user, key)]
        idle = max(0, (now - e["event_time"]) // config.DAY_MS)
        rows.append(
            {
                "user": user, "color": color_of(user), "initial": initial(user),
                "key": key, "show": e["show_title"], "ep": episode_label(e),
                "title": e["episode_title"] or "", "pct": e["pct"],
                "last": e["event_time"], "last_label": stamp(e["event_time"]),
                "idle": idle, "episodes": len(a["items"]), "done_eps": len(a["done"]),
                "poster": e["poster"] or "", "hue": hue_of(e["show_title"]),
                "initial_show": initial(e["show_title"]), "library": e["library"] or "未归属",
            }
        )
    rows.sort(key=lambda r: (-r["idle"], r["show"]))

    per_user = Counter(r["user"] for r in rows)
    per_show = Counter(r["show"] for r in rows)
    show_keys = {r["show"]: r["key"] for r in rows}
    user_rows = [
        {"name": u, "n": n, "color": color_of(u), "initial": initial(u),
         "events": sum(1 for r in rows if r["user"] == u)}
        for u, n in per_user.most_common()
    ]
    peak = user_rows[0]["n"] if user_rows else 1
    for u in user_rows:
        u["pct"] = round(u["n"] * 100 / peak) if peak else 0
    show_rows = [
        {"show": s, "key": show_keys.get(s, ""), "n": n,
         "pct": round(n * 100 / (per_show.most_common(1)[0][1] or 1))}
        for s, n in per_show.most_common()
    ]
    library_rows = Counter(r["library"] for r in rows).most_common()

    return {
        "days": days,
        "rows": rows[:80],
        "total": len(rows),
        "people": len(per_user),
        "shows": len(per_show),
        "avg_idle": round(sum(r["idle"] for r in rows) / len(rows)) if rows else 0,
        "user_rows": user_rows[:8],
        "show_rows": show_rows[:8],
        "library_rows": library_rows,
    }


# -------------------------------------------------------------------- stats
def stats_context() -> dict:
    evs = allowed_events()
    people = _people_index(evs)
    peak_min = people[0]["minutes"] if people else 1
    for u in people:
        u["pct"] = round(u["minutes"] * 100 / peak_min) if peak_min else 0

    shows = _show_index(evs)
    by_events = shows[:12]
    by_minutes = [s for s in sorted(shows, key=lambda s: -s["minutes"]) if s["minutes"] > 0][:12]
    for group, field in ((by_events, "events"), (by_minutes, "minutes")):
        top = group[0][field] if group else 1
        for s in group:
            s["pct"] = round(s[field] * 100 / top) if top else 0

    days = Counter(day_of(e["event_time"]) for e in evs)
    busiest = days.most_common(1)[0] if days else None
    return {
        "people": people,
        "by_events": by_events,
        "by_minutes": by_minutes,
        "kpis": {
            "events": event_count(evs),
            "raw": len(evs),
            "minutes": minutes_of(evs),
            "people": len(people),
            "shows": len(shows),
            "days": len(days),
            "busiest": (f"{busiest[0].month} 月 {busiest[0].day} 日 · {busiest[1]} 条" if busiest else "—"),
        },
    }


# ------------------------------------------------------------------ together
TOGETHER_WINDOW = 24 * 3600 * 1000
CLOSE_WINDOW = 3 * 3600 * 1000


def _together(evs: list[dict]) -> dict:
    """Same episode, two people, one calendar day. Within three hours it was near-simultaneous;
    beyond that the honest description is only "the same day". Sessions crossing midnight are
    dropped: two people ten hours apart either side of midnight were not watching together."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in evs:
        groups[(e["show_key"], e["item_guid"])].append(e)
    sessions = []
    for (key, _guid), rows in groups.items():
        rows.sort(key=lambda x: x["event_time"])
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1]["event_time"] - rows[i]["event_time"] <= TOGETHER_WINDOW:
                j += 1
            window = rows[i:j + 1]
            users = sorted({w["user_name"] for w in window})
            # A window may run past midnight; "same day" has to mean the same calendar day,
            # otherwise the label reads backwards and the claim is not true.
            same_day = day_of(window[0]["event_time"]) == day_of(window[-1]["event_time"])
            if len(users) >= 2 and same_day:
                first, last = window[0], window[-1]
                sessions.append(
                    {
                        "key": key, "show": first["show_title"], "ep": episode_label(first),
                        "title": first["episode_title"] or "",
                        "users": [{"name": u, "color": color_of(u), "initial": initial(u)} for u in users],
                        "n": len(users),
                        "label": f'{stamp(first["event_time"])} – {clock(last["event_time"])}',
                        "gap": (last["event_time"] - first["event_time"]) // 60000,
                        "poster": first["poster"] or "", "hue": hue_of(first["show_title"]),
                        "initial": initial(first["show_title"]), "end": last["event_time"],
                    }
                )
            i = j + 1
    sessions.sort(key=lambda s: (-s["n"], -s["end"]))
    close = [s for s in sessions if s["gap"] * 60000 <= CLOSE_WINDOW]
    day = [s for s in sessions if s["gap"] * 60000 > CLOSE_WINDOW]
    return {"close": close[:8], "day": day[:8], "total": len(sessions)}


# ---------------------------------------------------------------- libraries
def _library_health() -> dict:
    """已删除的媒体库：item_ancestor 还指着一条已经不存在的 MediaDB 记录。

    这是历史遗留，不是数据错误——播放记录留着，但媒体库本身被删了，
    所以这些记录在应用里没有媒体库归属。
    """
    path = config.SNAP_DIR / "trimmedia.db"
    empty = {"dead": 0, "items": 0, "orphan_items": 0}
    if not path.exists():
        return empty
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            dead, items = con.execute(
                "SELECT COUNT(DISTINCT a.ancestor_guid), COUNT(DISTINCT a.item_guid) "
                "FROM item_ancestor a LEFT JOIN item d ON d.guid = a.ancestor_guid "
                "WHERE d.guid IS NULL"
            ).fetchone()
            orphan = con.execute(
                "SELECT COUNT(*) FROM item i WHERE i.type IN ('TV','Movie','Season','Episode') "
                "AND NOT EXISTS (SELECT 1 FROM item_ancestor a WHERE a.item_guid = i.guid)"
            ).fetchone()[0]
            return {"dead": int(dead or 0), "items": int(items or 0), "orphan_items": int(orphan or 0)}
        finally:
            con.close()
    except sqlite3.Error:
        return empty


def library_context() -> dict:
    evs = allowed_events()
    excl = db.exclusions()
    cats = db.catalogs()
    libs: dict[str, dict] = {}
    for e in evs:
        name = e["library"] or "已删除的媒体库"
        L = libs.setdefault(
            name,
            {"name": name, "events": 0, "users": set(), "shows": set(), "pos": {}, "last": 0, "first": 0,
             "posters": set(), "no_pct": 0, "no_title": 0},
        )
        L["events"] += 1
        L["users"].add(e["user_name"])
        L["shows"].add(e["show_key"])
        L["pos"][e["item_guid"]] = max(L["pos"].get(e["item_guid"], 0), e["position"] or 0)
        L["last"] = max(L["last"], e["event_time"])
        L["first"] = e["event_time"] if not L["first"] else min(L["first"], e["event_time"])
        if e["poster"]:
            L["posters"].add(e["show_key"])
        if not (e["duration"] or 0):
            L["no_pct"] += 1
        if e["item_type"] == "Episode" and not e["episode_title"]:
            L["no_title"] += 1

    now = now_ms()
    rows = []
    for L in libs.values():
        shows_n = len(L["shows"])
        rows.append(
            {
                "name": L["name"], "events": L["events"], "people": len(L["users"]), "shows": shows_n,
                "minutes": sum(L["pos"].values()) // 60,
                "last_label": stamp(L["last"]), "first_label": stamp(L["first"]),
                "idle": max(0, (now - L["last"]) // config.DAY_MS),
                "poster_n": len(L["posters"]),
                "poster_pct": round(len(L["posters"]) * 100 / shows_n) if shows_n else 0,
                "no_pct": L["no_pct"], "no_title": L["no_title"],
            }
        )
    rows.sort(key=lambda r: -r["events"])

    by_show: dict[str, set[str]] = defaultdict(set)
    titles = {}
    for e in evs:
        by_show[e["show_key"]].add(e["library"] or "未归属")
        titles[e["show_key"]] = e["show_title"]
    cross = sorted(
        ({"show": titles[k], "key": k, "libs": sorted(v)} for k, v in by_show.items() if len(v) > 1),
        key=lambda x: x["show"],
    )
    index = _show_index(evs)
    no_poster = [s["show"] for s in index if not s["poster"]]
    no_duration = [s["show"] for s in index if s["minutes"] == 0]
    stale = [
        {"show": s["show"], "key": s["key"], "days": max(0, (now - s["last"]) // config.DAY_MS)}
        for s in index if s["last"] and (now - s["last"]) // config.DAY_MS >= 90
    ]
    stale.sort(key=lambda s: -s["days"])
    empty_libs = [c["name"] for c in cats["library"] if not c["events"]]

    pstats = posters.stats([{"show_key": s["key"], "poster": s["poster"]} for s in index])

    return {
        "rows": rows,
        "lib_health": _library_health(),
        "posters": pstats,
        "cross": cross,
        "no_poster": no_poster,
        "no_duration": no_duration,
        "stale": stale[:10],
        "empty_libs": empty_libs,
        "excluded_libs": sorted(excl["library"]),
        "excluded_users": sorted(excl["account"]),
        "kpis": {
            "libs": len(rows),
            "shows": len(index),
            "events": event_count(evs),
            "minutes": minutes_of(evs),
        },
    }
