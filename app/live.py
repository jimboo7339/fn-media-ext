"""Live view: who is watching right now.

The media server has no "online users" API, so this reads the closest thing it
does expose: the playback progress heartbeats in ``item_user_play``, the request
log in ``trimactivity.user_activity_log`` and the login tokens in
``user_token``. Everything is opened read-only and, when that is not possible
(read-only mount without a usable WAL sidecar), the newest snapshot is used
instead and the page says so.
"""
from __future__ import annotations

import sqlite3
import time

from . import config, db, ingest, queries

WATCH_WINDOW = 5 * 60  # 5 分钟内还有进度上报就算“正在看”
ACTIVE_WINDOW = 30 * 60  # 30 分钟内有请求就算“活跃”
DEVICE_WINDOW = 12 * 3600  # 12 小时内登录过的设备还列出来
_TTL = 3.0  # 同一次刷新里的重复请求共用结果

_CACHE: dict = {"at": 0.0, "data": None}

CLIENTS = (
    ("web", "网页"),
    ("andorid", "Android"),
    ("android", "Android"),
    ("ios", "iOS"),
    ("apple", "iOS"),
    ("tv", "电视端"),
    ("kodi", "Kodi"),
)


def _open(name: str, probe: str):
    """Open ``name`` read-only; prefer the live database, fall back to a snapshot."""
    for path, mode in (
        (config.DB_DIR / name, "live"),
        (config.SNAP_DIR / name, "snapshot"),
    ):
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            con.execute(f"SELECT 1 FROM {probe} LIMIT 1").fetchone()
            return con, mode
        except sqlite3.Error:
            try:
                con.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
    return None, "none"


def _client_name(app_name: str, device: str) -> str:
    blob = f"{app_name} {device}".lower()
    for needle, label in CLIENTS:
        if needle in blob:
            return label
    return (app_name or device or "未知客户端")[:24]


def _mask_ip(ip: str) -> str:
    parts = (ip or "").split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["*"])
    return ip or ""


def _ago(seconds: int) -> str:
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def snapshot(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    data = _build(now)
    _CACHE.update(at=now, data=data)
    return data


def _build(now: float) -> dict:
    now_ms = int(now * 1000)
    empty = {
        "mode": "none",
        "as_of": now_ms,
        "watching": [],
        "recent": [],
        "devices": [],
        "active": [],
        "counts": {"watching": 0, "active": 0, "today": 0, "devices": 0},
        "last_seen": None,
        "error": "源数据库不可读，检查 TRIM_DB_DIR 挂载",
    }

    con, mode = _open("trimmedia.db", "item_user_play")
    if con is None:
        return empty

    try:
        users = ingest._users(con)
        lib_sql = (
            "       (SELECT d.title FROM item_ancestor a JOIN item d ON d.guid = a.ancestor_guid "
            "        WHERE a.item_guid = p.item_guid AND d.type = 'MediaDB' LIMIT 1) AS library, "
        )
        rows = con.execute(
            "SELECT p.item_guid, p.user_guid, p.ts, p.watched, p.resolution, p.bitrate, "
            "       p.media_guid, p.update_time, " + lib_sql +
            "       i.title AS item_title, i.type AS item_type, i.trim_id, "
            "       i.season_number, i.episode_number, i.runtime, i.posters AS item_poster "
            "FROM item_user_play p LEFT JOIN item i ON i.guid = p.item_guid "
            "WHERE p.visible = 1 ORDER BY p.update_time DESC LIMIT 120"
        ).fetchall()

        durs = _durations(con, [r["item_guid"] for r in rows])
        today_start = int(_local_midnight(now) * 1000)
        today_rows = con.execute(
            "SELECT p.user_guid, " + lib_sql +
            "       i.title AS item_title, i.type AS item_type "
            "FROM item_user_play p LEFT JOIN item i ON i.guid = p.item_guid "
            "WHERE p.visible = 1 AND p.update_time >= ?",
            (today_start,),
        ).fetchall()
    finally:
        con.close()

    ext, _mode = _open("trimmedia_ext.db", "item_archive")
    try:
        archive = ingest._archive_meta(ext) if ext is not None else {}
    finally:
        if ext is not None:
            ext.close()

    excl = db.exclusions()

    def hidden(row) -> bool:
        """Skip what the timeline skips: 排除的账号/媒体库，以及解析不到条目的行。"""
        name = users.get(row["user_guid"]) or ""
        if name in excl["account"] or (row["library"] or config.DEAD_LIBRARY) in excl["library"]:
            return True
        return not row["item_title"] and not row["item_type"]

    today = sum(1 for r in today_rows if not hidden(r))

    watching: list[dict] = []
    recent: list[dict] = []
    last_seen = None

    for row in rows:
        if hidden(row):
            continue
        upd = row["update_time"] or 0
        seconds = upd / 1000
        ago = max(0, int(now - seconds))
        last_seen = last_seen or upd

        trim_id = (row["trim_id"] or "").strip()
        meta = archive.get(trim_id) or {}
        item_type = row["item_type"] or ""
        show = meta.get("title") or row["item_title"] or "未知内容"
        episode = ""
        if item_type == "Episode":
            season = row["season_number"] or 0
            number = row["episode_number"] or 0
            if season or number:
                episode = f"S{season:02d}E{number:02d}"

        duration = durs.get(row["item_guid"]) or (row["runtime"] or 0) * 60
        ts = row["ts"] or 0
        pct = round(100 * ts / duration) if duration and ts else None
        name = users.get(row["user_guid"]) or row["user_guid"][:8]
        poster = meta.get("poster") or (row["item_poster"] or "").split(",")[0].strip().lstrip("/")

        entry = {
            "key": trim_id,
            "user": name,
            "color": queries.color_of(name),
            "show": show,
            "episode": episode,
            "episode_title": row["item_title"] or "",
            "poster": poster,
            "hue": queries.hue_of(show),
            "initial": queries.initial(show),
            "pct": pct,
            "position": _clock(ts),
            "duration": _clock(duration) if duration else "",
            "resolution": (row["resolution"] or "").upper(),
            "mbps": round((row["bitrate"] or 0) / 1_000_000, 1) if row["bitrate"] else 0,
            "watched": bool(row["watched"]),
            "ago": _ago(ago),
            "seconds_ago": ago,
            "is_movie": item_type == "Movie",
        }
        if ago <= WATCH_WINDOW and ts > 0 and not row["watched"]:
            watching.append(entry)
        if len(recent) < 12:
            recent.append(entry)

    devices, active = _activity(now, users)

    lag = 0
    if mode == "snapshot":
        try:
            lag = max(0, int((now - (config.SNAP_DIR / "trimmedia.db").stat().st_mtime) / 60))
        except OSError:
            lag = 0

    return {
        "mode": mode,
        "as_of": now_ms,
        "stamp": _stamp(now_ms),
        "last_seen_text": _stamp(last_seen) if last_seen else "",
        "snapshot_lag": lag,
        "watching": watching[:8],
        "recent": recent,
        "devices": devices,
        "active": active,
        "counts": {
            "watching": len(watching),
            "active": len({a["user"] for a in active} | {w["user"] for w in watching}),
            "today": today,
            "devices": len(devices),
        },
        "last_seen": last_seen,
        "error": "",
    }


def _activity(now: float, users: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Devices that logged in recently and users that made requests recently."""
    con, _mode = _open("trimactivity.db", "user_token")
    if con is None:
        return [], []
    try:
        devices = []
        for row in con.execute(
            "SELECT user_guid, app_name, device, ip, MAX(update_time) AS last "
            "FROM user_token WHERE update_time > ? "
            "GROUP BY user_guid, app_name, device ORDER BY last DESC LIMIT 20",
            (int(now - DEVICE_WINDOW),),
        ):
            devices.append(
                {
                    "user": users.get(row["user_guid"]) or row["user_guid"][:8],
                    "client": _client_name(row["app_name"], row["device"]),
                    "device": row["device"] or "",
                    "ip": _mask_ip(row["ip"]),
                    "ago": _ago(max(0, int(now - row["last"]))),
                    "seconds_ago": max(0, int(now - row["last"])),
                }
            )
        active = []
        try:
            for row in con.execute(
                "SELECT user_guid, MAX(update_time) AS last, COUNT(*) AS n "
                "FROM user_activity_log WHERE update_time > ? "
                "AND path NOT LIKE '%sys/img%' GROUP BY user_guid ORDER BY last DESC",
                (int(now - ACTIVE_WINDOW),),
            ):
                name = users.get(row["user_guid"]) or row["user_guid"][:8]
                active.append(
                    {
                        "user": name,
                        "color": queries.color_of(name),
                        "n": row["n"],
                        "ago": _ago(max(0, int(now - row["last"]))),
                    }
                )
        except sqlite3.Error:
            pass
        return devices, active
    finally:
        con.close()


def _durations(con: sqlite3.Connection, item_guids: list[str]) -> dict[str, int]:
    guids = [g for g in dict.fromkeys(item_guids) if g]
    if not guids:
        return {}
    marks = ",".join("?" * len(guids))
    try:
        rows = con.execute(
            "SELECT m.item_guid AS g, MIN(s.duration) AS d FROM item_media m "
            "JOIN media_stream s ON s.media_guid = m.guid "
            f"WHERE s.codec_type = 'video' AND s.duration > 0 AND m.item_guid IN ({marks}) "
            "GROUP BY m.item_guid",
            guids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["g"]: int(row["d"] or 0) for row in rows}


def _local_midnight(now: float) -> float:
    from datetime import datetime

    local = datetime.fromtimestamp(now, config.TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _stamp(ms: int) -> str:
    from datetime import datetime

    local = datetime.fromtimestamp(ms / 1000, config.TZ)
    today = datetime.now(config.TZ).date()
    if local.date() == today:
        return local.strftime("今天 %H:%M:%S")
    return local.strftime("%m-%d %H:%M")


def _clock(seconds: int) -> str:
    if not seconds:
        return "00:00"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"
