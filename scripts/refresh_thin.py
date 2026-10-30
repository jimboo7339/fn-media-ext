#!/usr/bin/env python3
"""触发飞牛重刮「待补全清单」里的条目。

原理：飞牛影视有单条目刷新接口（网页版「重新刮削」按钮就是调它）：

    POST /v/api/v1/item/refresh
    {"item_guid": "<item 表主键>", "refresh_mode": 1}

refresh_mode: 1 = 仅刷新缺失数据（推荐），0 = 替换所有数据。

本脚本只做三件事，**不写飞牛的任何数据库**：

1. 从快照里把「待补全条目」映射到**活库副本**的 item.guid；
2. 挑一个管理员的活跃登录令牌（来自飞牛自己的 user_token 表）；
3. 逐个调用刷新接口，并把请求时间记回 app.db 的 meta_probe.requested_at。

用法（在 NAS 的项目目录）：

    # 只看待发送的内容，不发请求
    docker exec fn-media-ext python /app/refresh.py --dry-run

    # 真正发送（仅补缺失）
    docker exec fn-media-ext python /app/refresh.py

    # 替换所有数据（更强的刷新，一般不需要）
    docker exec fn-media-ext python /app/refresh.py --mode 0

需要环境变量（或 --host/--token 参数）：
    TRIM_API_BASE   飞牛影视的地址，如 http://192.168.100.10:8005
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
for _cand in (BASE_DIR, BASE_DIR.parent):
    if (_cand / "app").is_dir():
        sys.path.insert(0, str(_cand))
        break

from app import config, db  # noqa: E402
from app.meta import LABELS, _labels  # noqa: E402

API_PATH = "/v/api/v1/item/refresh"


def _snapshot_conn():
    path = config.SNAP_DIR / "trimmedia.db"
    if not path.exists():
        print(f"找不到源库快照：{path}")
        sys.exit(1)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def load_token(explicit: str | None) -> tuple[str, str]:
    """挑一个管理员的活跃令牌。返回 (token, username)。"""
    m = _snapshot_conn()
    act = sqlite3.connect(f"file:{config.SNAP_DIR / 'trimactivity.db'}?mode=ro", uri=True)
    act.row_factory = sqlite3.Row
    try:
        admins = {r["guid"]: r["username"] for r in
                  m.execute("SELECT guid, username FROM user WHERE is_admin=1 AND status=1")}
        rows = act.execute(
            "SELECT token, user_guid, app_name, update_time FROM user_token "
            "WHERE status=1 ORDER BY update_time DESC"
        ).fetchall()
        for r in rows:
            u = admins.get(r["user_guid"])
            if u:
                return r["token"], u
        print("没有找到「管理员 + 活跃令牌」的组合。")
        print("可用的管理员：", ", ".join(admins.values()))
        print("活跃令牌属于：", ", ".join(sorted({str(r["user_guid"])[:8] for r in rows})) or "无")
        sys.exit(1)
    finally:
        m.close()
        act.close()


def pick_targets(only_stale_hours: float) -> list[dict]:
    """待补全、且最近没请求过重刮的条目。"""
    with db.session_scope() as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM meta_probe WHERE complete=0 ORDER BY title"
        )]
    cutoff = time.time() * 1000 - only_stale_hours * 3600_000
    return [r for r in rows if not r["requested_at"] or r["requested_at"] < cutoff]


def map_to_live_guids(con, show_key: str) -> list[str]:
    """trim_id -> 活库副本的 item.guid。死库副本刷了也没意义。"""
    if show_key.startswith("guid:"):
        return [show_key[5:]]
    rows = con.execute(
        "SELECT i.guid FROM item i JOIN item_ancestor a ON a.item_guid = i.guid "
        "JOIN item d ON d.guid = a.ancestor_guid AND d.type='MediaDB' "
        "WHERE i.trim_id = ? AND i.type IN ('TV','Movie')",
        (show_key,),
    ).fetchall()
    return [r["guid"] for r in rows]


def call_refresh(base: str, token: str, guid: str, mode: int, timeout: float = 15) -> tuple[bool, str]:
    url = base.rstrip("/") + API_PATH
    body = json.dumps({"item_guid": guid, "refresh_mode": mode}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Cookie": f"token={token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status} {resp.read(120).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.read(120).decode('utf-8', 'replace')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os_env("TRIM_API_BASE", "http://127.0.0.1:8005"),
                    help="飞牛影视地址，默认取 TRIM_API_BASE 或 http://127.0.0.1:8005")
    ap.add_argument("--token", default="", help="登录令牌；不给就从快照里挑管理员的活跃令牌")
    ap.add_argument("--mode", type=int, default=1, help="refresh_mode：1=仅补缺失（默认），0=替换全部")
    ap.add_argument("--dry-run", action="store_true", help="只列出将发送的内容，不发请求")
    ap.add_argument("--stale-days", type=float, default=7.0,
                    help="距上次请求超过这个天数才再次请求（默认 7 天）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几部（0=不限制）")
    args = ap.parse_args()

    token, username = ("", "")
    if not args.dry_run:
        token, username = (args.token, "手动指定") if args.token else load_token(None)
        print(f"使用 {username!r} 的登录令牌调用 {args.host}{API_PATH}\n")

    con = _snapshot_conn()
    targets = pick_targets(args.stale_days)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("待补全清单里没有需要处理的条目（都请求过了，或清单为空）。")
        return 0

    print(f"待处理 {len(targets)} 部（mode={args.mode}）\n")
    results = []
    for t in targets:
        guids = map_to_live_guids(con, t["show_key"])
        labels = "、".join(_labels(t["missing"] or "", t["ep_total"] and
                                   {"eps": t["ep_total"]}))
        if not guids:
            print(f"  跳过 {t['title']!r}：找不到活库副本（只存在于已删除的媒体库）")
            results.append((t, None, "无活库副本"))
            continue
        results.append((t, guids, ""))
        for g in guids:
            if args.dry_run:
                print(f"  [dry] {t['title']!r} guid={g}")
            else:
                ok, msg = call_refresh(args.host, token, g, args.mode)
                print(f"  {'✓' if ok else '✗'} {t['title']!r} guid={g[:12]}… {msg}")
                if not ok:
                    break

    if args.dry_run:
        print("\n（dry-run，未发送。去掉 --dry-run 执行。）")
        return 0

    # 记录请求时间；失败的也记（避免反复打一个坏接口），下次过期后会重试
    now = int(time.time() * 1000)
    with db.session_scope() as con:
        for t, guids, err in results:
            if guids:
                con.execute("UPDATE meta_probe SET requested_at=? WHERE show_key=?",
                            (now, t["show_key"]))
    print(f"\n完成：{sum(1 for _, g, _ in results if g)} 部已请求重刮，"
          f"{sum(1 for _, g, e in results if not g)} 部跳过。结果过几分钟到 24 小时内出现在飞牛里，"
          f"我们的待补全清单会随快照更新自动重新盘点。")
    return 0


def os_env(name: str, default: str) -> str:
    import os
    return os.getenv(name, default)


if __name__ == "__main__":
    sys.exit(main())