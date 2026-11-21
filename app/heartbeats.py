"""播放心跳的落库与真实时长推算。

源库 ``user_activity_log`` 只滚动保留约 28 天，其中 ``/play/record`` 心跳固定
15 秒一条，是唯一能还原「实际播放耗时」的信号——飞牛不记录播放速度，
``watch_event`` 又是状态覆盖式的（每人每集只有一行，重看与中途退出全部丢失）。

所以在每次同步时把新心跳抄进自己的库**永久累积**：今天不抄，这天的真实
时长就永远拿不回来了。心跳没有条目信息（请求路径里没有 guid），只能回答
「谁、什么时候、播了多久」，回答不了「在看什么」。

推算口径：间隔 ≤2 分钟的心跳连成一段会话（一次暂停超过 2 分钟就断开），
会话时长 = 末条 − 首条 + 15 秒；少于 3 条心跳的会话不算（多半是打开了
页面但没有真正播放）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import config, db

logger = logging.getLogger("fnxt.hb")

GAP_MS = 2 * 60 * 1000
HEARTBEAT_MS = 15_000
MIN_SESSION_BEATS = 3
LAST_KEY = "heartbeat_last_sec"

SCHEMA = """
CREATE TABLE IF NOT EXISTS play_heartbeat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name   TEXT NOT NULL,
    hb_time     INTEGER NOT NULL,      -- ms UTC（源库 create_time 秒 ×1000）
    captured_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hb_user_time ON play_heartbeat(user_name, hb_time);
"""


def ensure_schema() -> None:
    with db.session_scope() as con:
        con.executescript(SCHEMA)


def capture(activity_snap: Path | str | None, users: dict[str, str]) -> dict:
    """把快照里还没抄过的心跳写进自己的库。

    以源库秒级 ``create_time`` 为游标（存 setting），天然幂等；已删除账号
    的心跳不收，与观看记录的口径一致。"""
    ensure_schema()
    if not activity_snap or not Path(activity_snap).exists():
        return {"captured": 0}
    last = int(db.get_setting(LAST_KEY) or 0)
    try:
        con = sqlite3.connect(f"file:{Path(activity_snap).as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT user_guid, create_time FROM user_activity_log "
                "WHERE path LIKE '%play/record%' AND create_time > ? ORDER BY create_time",
                (last,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.warning("心跳读取失败：%s", exc)
        return {"captured": 0}

    now = int(time.time() * 1000)
    out = []
    newest = last
    for r in rows:
        sec = r["create_time"] or 0
        name = users.get(r["user_guid"])
        if not name or sec <= 0:
            continue
        newest = max(newest, sec)
        out.append((name, sec * 1000, now))
    if out:
        with db.session_scope() as w:
            w.executemany(
                "INSERT INTO play_heartbeat(user_name, hb_time, captured_at) VALUES(?, ?, ?)",
                out,
            )
            # 游标与数据同一个事务落盘，崩溃也不会重复或漏抄
            w.execute(
                "INSERT INTO setting(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (LAST_KEY, str(newest)),
            )
    return {"captured": len(out)}


def first_captured() -> str:
    """最早一批心跳的落库日期（展示用），没有数据时返回空串。"""
    with db.session_scope() as con:
        row = con.execute("SELECT MIN(captured_at) AS t FROM play_heartbeat").fetchone()
    if not row or not row["t"]:
        return ""
    return datetime.fromtimestamp(row["t"] / 1000, config.TZ).strftime("%Y-%m-%d")


def real_minutes(days: int | None = None) -> dict[str, int]:
    """每人「实际播放分钟数」：心跳会话时长求和。

    与 watchtime 的估算口径（位置上界）是两个数：这个是下界——15 秒粒度下
    暂停不计时、快进少算，但它是真实发生的播放。``days`` 限制回看窗口。"""
    since = 0 if not days else int(time.time() * 1000) - days * config.DAY_MS
    with db.session_scope() as con:
        rows = con.execute(
            "SELECT user_name, hb_time FROM play_heartbeat WHERE hb_time > ? "
            "ORDER BY user_name, hb_time",
            (since,),
        ).fetchall()

    totals: dict[str, float] = defaultdict(float)
    user = None
    start = prev = 0
    beats = 0

    def flush() -> None:
        nonlocal beats
        if user and beats >= MIN_SESSION_BEATS:
            totals[user] += (prev - start) + HEARTBEAT_MS
        beats = 0

    for r in rows:
        t = r["hb_time"]
        if user != r["user_name"] or t - prev > GAP_MS:
            flush()
            user = r["user_name"]
            start = t
        prev = t
        beats += 1
    flush()
    return {u: int(s // 60_000) for u, s in totals.items()}
