"""SQLite access for the app's own database (accounts, settings, derived events)."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    passwd      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    last_login  INTEGER
);

CREATE TABLE IF NOT EXISTS session (
    token       TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exclusion (
    scope       TEXT NOT NULL CHECK (scope IN ('library', 'account')),
    target      TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);

CREATE TABLE IF NOT EXISTS catalog (
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,
    events      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, name)
);

CREATE TABLE IF NOT EXISTS watch_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_guid       TEXT,
    user_name       TEXT NOT NULL,
    show_key        TEXT NOT NULL,
    show_title      TEXT NOT NULL,
    item_guid       TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    season          INTEGER,
    episode         INTEGER,
    episode_title   TEXT,
    event_time      INTEGER NOT NULL,   -- ms, UTC
    first_time      INTEGER,
    watched         INTEGER NOT NULL DEFAULT 0,
    position        INTEGER NOT NULL DEFAULT 0,  -- seconds
    duration        INTEGER NOT NULL DEFAULT 0,  -- seconds
    pct             INTEGER,
    library         TEXT,
    poster          TEXT,
    genres          TEXT,
    bulk_group      TEXT,               -- non-null => collapsed into one card
    bulk_size       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_event_time ON watch_event(event_time);
CREATE INDEX IF NOT EXISTS idx_event_user ON watch_event(user_name);
CREATE INDEX IF NOT EXISTS idx_event_show ON watch_event(show_key);

CREATE TABLE IF NOT EXISTS meta_probe (
    show_key     TEXT PRIMARY KEY,      -- trim_id
    title        TEXT NOT NULL,
    kind         TEXT,                  -- TV / Movie
    missing      TEXT,                  -- 逗号分隔的缺失项
    complete     INTEGER NOT NULL DEFAULT 0,
    fetch_status TEXT,                  -- 飞牛自己的「抓过没抓过」
    scraped_at   INTEGER,               -- 飞牛上次更新这个条目的时间
    checked_at   INTEGER,               -- 我们上次盘点的结论来自何时
    ep_missing   INTEGER NOT NULL DEFAULT 0,  -- 单集缺失数（剧照+简介+播出日期）
    ep_total     INTEGER NOT NULL DEFAULT 0,  -- 单集总数
    requested_at INTEGER NOT NULL DEFAULT 0   -- 上次请求重刮的时间（第 1 步用）
);

CREATE TABLE IF NOT EXISTS sync_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    synced_at   INTEGER,
    events      INTEGER NOT NULL DEFAULT 0,
    shows       INTEGER NOT NULL DEFAULT 0,
    posters     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- 日报的投递记录：只留最近若干条，够页面显示「最近发送」就行
CREATE TABLE IF NOT EXISTS digest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_day  TEXT NOT NULL,            -- YYYY-MM-DD，测试消息记当天
    kind        TEXT NOT NULL,            -- generic / serverchan / telegram / ... / test
    ok          INTEGER NOT NULL,
    status      INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    created_at  INTEGER NOT NULL
);
"""


def connect(path=None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or config.APP_DB), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


@contextmanager
def session_scope() -> Iterator[sqlite3.Connection]:
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SNAP_DIR.mkdir(parents=True, exist_ok=True)
    with session_scope() as con:
        con.executescript(SCHEMA)
        # 老库迁移：meta_probe 建表时可能没有这两列
        for col in ("ep_missing INTEGER NOT NULL DEFAULT 0", "ep_total INTEGER NOT NULL DEFAULT 0"):
            try:
                con.execute(f"ALTER TABLE meta_probe ADD COLUMN {col}")
            except Exception:  # noqa: BLE001 - 列已存在
                pass


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: str | None = None) -> str | None:
    with session_scope() as con:
        row = con.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def settings_map(keys: list[str]) -> dict[str, str]:
    """Read several settings in one connection; missing keys are simply absent."""
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    with session_scope() as con:
        rows = con.execute(f"SELECT key, value FROM setting WHERE key IN ({marks})", keys).fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str) -> None:
    with session_scope() as con:
        con.execute(
            "INSERT INTO setting(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# -------------------------------------------------------------- digest log
def log_digest(report_day: str, kind: str, ok: bool, status: int, message: str) -> None:
    with session_scope() as con:
        con.execute(
            "INSERT INTO digest_log(report_day, kind, ok, status, message, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (report_day, kind, 1 if ok else 0, int(status or 0), (message or "")[:500],
             int(time.time() * 1000)),
        )
        # 只留最近 50 条：这是排障用的日志，不是审计账本
        con.execute(
            "DELETE FROM digest_log WHERE id NOT IN (SELECT id FROM digest_log ORDER BY id DESC LIMIT 50)"
        )


def digest_logs(limit: int = 8) -> list[dict]:
    with session_scope() as con:
        rows = con.execute(
            "SELECT * FROM digest_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- exclusions
def exclusions() -> dict[str, set[str]]:
    with session_scope() as con:
        rows = con.execute("SELECT scope, target FROM exclusion").fetchall()
    out: dict[str, set[str]] = {"library": set(), "account": set()}
    for row in rows:
        out.setdefault(row["scope"], set()).add(row["target"])
    return out


def toggle_exclusion(scope: str, target: str) -> None:
    with session_scope() as con:
        cur = con.execute(
            "DELETE FROM exclusion WHERE scope = ? AND target = ?", (scope, target)
        )
        if cur.rowcount == 0:
            con.execute("INSERT INTO exclusion(scope, target) VALUES(?, ?)", (scope, target))


# ---------------------------------------------------------------- catalogs
def catalogs() -> dict[str, list[dict]]:
    with session_scope() as con:
        rows = con.execute("SELECT scope, name, events FROM catalog ORDER BY events DESC, name").fetchall()
    out: dict[str, list[dict]] = {"account": [], "library": []}
    for row in rows:
        out.setdefault(row["scope"], []).append({"name": row["name"], "events": row["events"]})
    return out


def sync_state() -> dict | None:
    with session_scope() as con:
        row = con.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
    return dict(row) if row else None
