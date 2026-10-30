"""Turn the read-only Trim media databases into flat watch events.

The source databases are never modified: they are copied into ``data/snap`` with
SQLite's online backup API (or a plain file copy when that fails on a read-only
WAL mount) and only the copies are queried.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import traceback
from pathlib import Path

from . import config, db

_LOCK = threading.Lock()
_STATE = {"synced_at": 0, "busy": False}
_LAST_ATTEMPT = {"at": 0.0}


# --------------------------------------------------------------- snapshots
def _stale(src: Path, dst: Path) -> bool:
    """True when the source may have changed since the last snapshot."""
    if not dst.exists():
        return True
    newest = src.stat().st_mtime
    wal = Path(str(src) + "-wal")
    if wal.exists():
        newest = max(newest, wal.stat().st_mtime)
    return newest >= dst.stat().st_mtime - 1


def snapshot_one(name: str) -> Path:
    """Copy one source db into the snapshot dir. Returns the snapshot path."""
    src = config.DB_DIR / name
    dst = config.SNAP_DIR / name
    if not src.exists():
        raise FileNotFoundError(f"{src} 不存在")
    if not _stale(src, dst):
        return dst

    tmp = dst.with_suffix(".tmp")
    for leftover in (tmp, dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm")):
        if leftover.exists():
            leftover.unlink()

    try:
        source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            target = sqlite3.connect(tmp)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        tmp.replace(dst)
        return dst
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)

    # Read-only WAL directory: fall back to copying the file set.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(src) + suffix)
        if p.exists():
            shutil.copy2(p, str(dst) + suffix)
    return dst


def snapshots() -> dict[str, Path]:
    out = {}
    for name in config.SOURCE_DBS:
        try:
            out[name] = snapshot_one(name)
        except FileNotFoundError:
            continue
    return out


# ------------------------------------------------------------------ derive
def _archive_meta(ext: sqlite3.Connection) -> dict[str, dict]:
    """Only the show/movie node matters: the archive also stores one row per
    episode and season under the very same trim_id, and those would shadow it."""
    meta: dict[str, dict] = {}
    try:
        rows = ext.execute(
            "SELECT trim_id, item_type, data FROM item_archive "
            "WHERE item_type IN ('tv', 'movie') ORDER BY item_type"
        ).fetchall()
    except sqlite3.Error:
        return meta
    for trim_id, item_type, raw in rows:
        if trim_id in meta:
            continue
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(doc, list):
            doc = doc[0] if doc else {}
        node = doc.get(item_type) if isinstance(doc, dict) else None
        if not isinstance(node, dict):
            node = doc if isinstance(doc, dict) else {}
        genres = [g.get("name") for g in (node.get("genres") or []) if g.get("name")]
        meta[trim_id] = {
            "poster": (node.get("poster_path") or "").lstrip("/"),
            "genres": genres,
            "title": node.get("name") or node.get("title") or "",
            "vote": node.get("vote_average") or 0,
        }
    return meta


def _load_items(con: sqlite3.Connection) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for row in con.execute(
        "SELECT guid, trim_id, type, title, parent_guid, posters, runtime, "
        "season_number, episode_number, release_date FROM item"
    ):
        items[row["guid"]] = {
            "guid": row["guid"],
            "trim_id": (row["trim_id"] or "").strip(),
            "type": row["type"],
            "title": row["title"] or "",
            "parent": row["parent_guid"] or "",
            "posters": (row["posters"] or "").split(",")[0].strip().lstrip("/"),
            "runtime": row["runtime"] or 0,
            "season": row["season_number"],
            "episode": row["episode_number"],
            "release_date": row["release_date"],
        }
    return items


def _episode_counts(con: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in con.execute(
        "SELECT s.parent_guid AS tv, COUNT(*) AS n FROM item e "
        "JOIN item s ON s.guid = e.parent_guid AND s.type = 'Season' "
        "WHERE e.type = 'Episode' GROUP BY s.parent_guid"
    ):
        counts[row["tv"]] = row["n"]
    return counts


def _library_map(con: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in con.execute(
        "SELECT a.item_guid, d.title FROM item_ancestor a "
        "JOIN item d ON d.guid = a.ancestor_guid WHERE d.type = 'MediaDB'"
    ):
        out[row["item_guid"]] = row["title"]
    return out


def _duration_map(con: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in con.execute(
        "SELECT m.item_guid, MIN(s.duration) AS d FROM item_media m "
        "JOIN media_stream s ON s.media_guid = m.guid "
        "WHERE s.codec_type = 'video' AND s.duration > 0 GROUP BY m.item_guid"
    ):
        out[row["item_guid"]] = int(row["d"] or 0)
    return out


def _users(con: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for row in con.execute("SELECT guid, username FROM user"):
            out[row["guid"]] = row["username"]
    except sqlite3.Error:
        pass
    return out


def _libraries(con: sqlite3.Connection) -> list[str]:
    try:
        return [r["title"] for r in con.execute("SELECT title FROM item WHERE type = 'MediaDB' ORDER BY sort_num, title")]
    except sqlite3.Error:
        return []


def build_events(con: sqlite3.Connection, ext: sqlite3.Connection) -> tuple[list[dict], dict]:
    items = _load_items(con)
    counts = _episode_counts(con)
    lib_map = _library_map(con)
    dur_map = _duration_map(con)
    users = _users(con)
    archive = _archive_meta(ext)

    # Best TV row per trim_id (the one with the most episodes wins).
    tv_best: dict[str, str] = {}
    for guid, it in items.items():
        if it["type"] != "TV" or not it["trim_id"]:
            continue
        cur = tv_best.get(it["trim_id"])
        if cur is None or counts.get(guid, 0) > counts.get(cur, 0):
            tv_best[it["trim_id"]] = guid

    def canonical(tv: dict) -> dict:
        g = tv_best.get(tv["trim_id"])
        return items.get(g, tv) if g else tv

    def show_of(item: dict) -> dict | None:
        if item["type"] == "TV":
            return canonical(item)
        if item["trim_id"]:
            g = tv_best.get(item["trim_id"])
            if g:
                return items[g]
        parent = items.get(item["parent"])
        for _ in range(4):
            if not parent:
                return None
            if parent["type"] == "TV":
                return canonical(parent)
            parent = items.get(parent["parent"])
        return None

    rows = con.execute("SELECT * FROM item_user_play WHERE visible = 1").fetchall()
    events: list[dict] = []
    for p in rows:
        item = items.get(p["item_guid"])
        # Season / TV 行上的播放记录是「整季/整剧标记」，它并不指向某一集：
        # 飞牛会在 Season 行的 episode_number 里写一个数（例如整季 22 集的剧写成 30），
        # 照单全收就会在多出来的集上造一条不存在的观看记录。
        if not item or item["type"] in ("Video", "LiveChannel", "Directory", "MediaDB",
                                        "Season", "TV"):
            continue
        user_name = users.get(p["user_guid"]) or p["user_guid"][:6]
        show = show_of(item)
        if show is None:
            show = item if item["type"] in ("Movie", "TV") else None
        if show is None:
            continue

        trim = show["trim_id"]
        meta = archive.get(trim) or {}
        title = meta.get("title") or show["title"] or item["title"]
        poster = show["posters"] or meta.get("poster") or ""
        genres = meta.get("genres") or []

        duration = dur_map.get(item["guid"]) or 0
        if duration <= 0 and item["runtime"]:
            duration = int(item["runtime"]) * 60
        position = int(p["ts"] or 0)
        pct = None
        if position > 0 and duration > 0:
            pct = max(0, min(100, round(position * 100 / duration)))

        event_time = int(p["update_time"] or p["create_time"] or 0)
        events.append(
            {
                "user_guid": p["user_guid"],
                "user_name": user_name,
                "show_key": trim or show["guid"],
                "show_title": title,
                "item_guid": item["guid"],
                "item_type": item["type"],
                "season": item["season"],
                "episode": item["episode"],
                "episode_title": item["title"] if item["type"] in ("Episode", "Movie") else "",
                "event_time": event_time,
                "first_time": int(p["create_time"] or 0),
                "watched": int(p["watched"] or 0),
                "position": position,
                "duration": duration,
                "pct": pct,
                "library": lib_map.get(item["guid"], ""),
                "poster": poster,
                "genres": " / ".join(genres[:2]),
                "bulk_group": None,
                "bulk_size": 0,
            }
        )

    _mark_bulk(events)
    stats = {
        "libraries": _libraries(con),
        "users": sorted(set(users.values())),
        "posters": len({e["show_key"] for e in events if e["poster"]}),
        "shows": len({e["show_key"] for e in events}),
    }
    return events, stats


def _mark_bulk(events: list[dict]) -> None:
    """Collapse runs of >=5 plays by the same user inside 60 seconds."""
    events.sort(key=lambda e: (e["user_name"], e["event_time"]))
    i = 0
    n = len(events)
    while i < n:
        j = i + 1
        while (
            j < n
            and events[j]["user_name"] == events[i]["user_name"]
            and events[j]["event_time"] - events[i]["event_time"] <= 60_000
        ):
            j += 1
        if j - i >= 5:
            group = f'{events[i]["user_name"]}:{events[i]["event_time"]}'
            for k in range(i, j):
                events[k]["bulk_group"] = group
                events[k]["bulk_size"] = j - i
        i = j


# ------------------------------------------------------------------- write
def _write(events: list[dict], stats: dict, catalogs: dict[str, dict[str, int]]) -> None:
    con = db.connect()
    try:
        with con:
            con.execute("DELETE FROM watch_event")
            con.executemany(
                "INSERT INTO watch_event(user_guid, user_name, show_key, show_title, item_guid, "
                "item_type, season, episode, episode_title, event_time, first_time, watched, "
                "position, duration, pct, library, poster, genres, bulk_group, bulk_size) "
                "VALUES(:user_guid,:user_name,:show_key,:show_title,:item_guid,:item_type,:season,"
                ":episode,:episode_title,:event_time,:first_time,:watched,:position,:duration,"
                ":pct,:library,:poster,:genres,:bulk_group,:bulk_size)",
                events,
            )
            con.execute("DELETE FROM catalog")
            for scope, entries in catalogs.items():
                con.executemany(
                    "INSERT INTO catalog(scope, name, events) VALUES(?, ?, ?)",
                    [(scope, name, count) for name, count in entries.items()],
                )
            con.execute(
                "INSERT INTO sync_state(id, synced_at, events, shows, posters, error) "
                "VALUES(1, ?, ?, ?, ?, NULL) ON CONFLICT(id) DO UPDATE SET "
                "synced_at=excluded.synced_at, events=excluded.events, "
                "shows=excluded.shows, posters=excluded.posters, error=NULL",
                (int(time.time() * 1000), len(events), stats["shows"], stats["posters"]),
            )
    finally:
        con.close()


def sync_all(force: bool = False) -> dict:
    """Refresh snapshots and rebuild events. Safe to call concurrently."""
    if not _LOCK.acquire(blocking=force):
        return {"skipped": True, "reason": "同步进行中"}
    try:
        _STATE["busy"] = True
        paths = snapshots()
        if "trimmedia.db" not in paths:
            raise FileNotFoundError(f"{config.DB_DIR}/trimmedia.db 不可读")
        con = sqlite3.connect(str(paths["trimmedia.db"]))
        con.row_factory = sqlite3.Row
        ext = sqlite3.connect(str(paths.get("trimmedia_ext.db", paths["trimmedia.db"])))
        try:
            events, stats = build_events(con, ext)
            catalogs = _catalogs(events, stats)
        finally:
            con.close()
            ext.close()
        _write(events, stats, catalogs)
        _STATE["synced_at"] = time.time()
        return {"events": len(events), "shows": stats["shows"], "posters": stats["posters"]}
    except Exception as exc:  # noqa: BLE001 - reported to the UI
        with db.session_scope() as c:
            c.execute(
                "INSERT INTO sync_state(id, synced_at, events, shows, posters, error) "
                "VALUES(1, ?, 0, 0, 0, ?) ON CONFLICT(id) DO UPDATE SET error=excluded.error",
                (int(time.time() * 1000), f"{exc}"),
            )
        traceback.print_exc()
        return {"error": str(exc)}
    finally:
        _STATE["busy"] = False
        _LOCK.release()


def maybe_sync(stale_seconds: int = 0, blocking_seconds: int = 0) -> dict:
    """Refresh when someone is actually looking, instead of only on the timer.

    常态是「先把现有数据发出去，后台补一次，下一次翻页就是新的」——页面不阻塞。
    但如果数据已经陈旧到 SYNC_BLOCKING_SECONDS（例如每天早上第一次打开），
    等零点几秒换「打开就是最新的」更划算，否则第一眼看到的还是昨天的数据。
    """
    if not config.SYNC_ON_VIEW:
        return {"skipped": True, "reason": "SYNC_ON_VIEW 已关闭"}

    stale = stale_seconds or config.SYNC_STALE_SECONDS
    blocking = blocking_seconds or config.SYNC_BLOCKING_SECONDS
    now = time.time()
    age = now - (_STATE["synced_at"] or 0)
    if _STATE["busy"] or age < stale:
        return {"skipped": True, "reason": "数据足够新", "age": int(age)}
    if now - _LAST_ATTEMPT["at"] < 15:
        return {"skipped": True, "reason": "刚刚尝试过", "age": int(age)}
    _LAST_ATTEMPT["at"] = now

    if blocking > 0 and age >= blocking:
        worker = threading.Thread(target=sync_all, name="on-demand-sync", daemon=True)
        worker.start()
        worker.join(timeout=config.SYNC_BLOCKING_WAIT)
        return {"blocked": True, "waited": not worker.is_alive(), "age": int(age)}

    threading.Thread(target=sync_all, name="on-demand-sync", daemon=True).start()
    return {"scheduled": True, "age": int(age)}


def _catalogs(events: list[dict], stats: dict) -> dict[str, dict[str, int]]:
    libs: dict[str, int] = {name: 0 for name in stats["libraries"]}
    accounts: dict[str, int] = {name: 0 for name in stats["users"]}
    dead = 0
    for e in events:
        if e["library"]:
            libs[e["library"]] = libs.get(e["library"], 0) + 1
        else:
            dead += 1
        accounts[e["user_name"]] = accounts.get(e["user_name"], 0) + 1
    if dead:
        libs[config.DEAD_LIBRARY] = dead
    return {"library": libs, "account": accounts}


def _csv_env(name: str) -> list[str]:
    """Comma-separated environment variable as a clean list."""
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def seed_defaults() -> None:
    """Pre-exclude libraries/accounts on first run, but only once.

    Nothing is hard-coded: set ``EXCLUDE_DEFAULT_LIBRARY`` and/or
    ``EXCLUDE_DEFAULT_ACCOUNT`` to a comma-separated list of names to keep out
    of the timeline on a fresh install. Leave them unset to exclude nothing.
    """
    targets = [("library", t) for t in _csv_env("EXCLUDE_DEFAULT_LIBRARY")]
    targets += [("account", t) for t in _csv_env("EXCLUDE_DEFAULT_ACCOUNT")]
    with db.session_scope() as con:
        row = con.execute("SELECT value FROM setting WHERE key = 'seeded'").fetchone()
        if row:
            return
        for scope, target in targets:
            con.execute("INSERT OR IGNORE INTO exclusion(scope, target) VALUES(?, ?)", (scope, target))
        con.execute("INSERT INTO setting(key, value) VALUES('seeded', '1')")


if __name__ == "__main__":  # python -m app.ingest
    db.init()
    seed_defaults()
    print(sync_all(force=True))
    with db.session_scope() as con:
        for row in con.execute(
            "SELECT show_title, COUNT(*) AS n, COUNT(DISTINCT user_name) AS u "
            "FROM watch_event GROUP BY show_key ORDER BY n DESC LIMIT 15"
        ):
            print(f'{row["n"]:5}  {row["u"]:>3} 人  {row["show_title"]}')
