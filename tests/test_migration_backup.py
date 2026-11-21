"""迁移链与冷备份的回归测试。

运行：``.venv/Scripts/python.exe tests/test_migration_backup.py``
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402


def _use_tmp_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="fnxt-mig-"))
    config.DATA_DIR = tmp
    config.APP_DB = tmp / "app.db"
    db.init()
    return tmp


def test_fresh_init_sets_schema_version_and_indexes():
    _use_tmp_db()
    con = db.connect()
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION, "新库应当直接建到最新版本"
        idx = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_event_user_time" in idx
        assert "idx_event_show_time" in idx
    finally:
        con.close()


def test_legacy_db_upgrades_idempotently():
    _use_tmp_db()
    # 模拟老库：user_version 归零（列其实已存在，迁移必须幂等地跳过）
    con = db.connect()
    try:
        con.execute("PRAGMA user_version = 0")
    finally:
        con.close()
    db.init()  # 不应抛异常
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        con.close()


def test_backup_recent_creates_and_rotates():
    tmp = _use_tmp_db()
    with db.session_scope() as con:
        con.execute("INSERT INTO setting(key, value) VALUES('k', 'v')")
    dest = db.backup_recent()
    assert dest.exists(), "备份文件已生成"
    check = db.connect(dest)
    try:
        assert check.execute("SELECT COUNT(*) FROM setting WHERE key='k'").fetchone()[0] == 1
    finally:
        check.close()
    # 同一分钟内重复调用不覆盖
    assert db.backup_recent() == dest

    # 造 9 份假旧备份，验证只保留最近 7 份
    backups = tmp / "backups"
    for i in range(9):
        (backups / f"app-2020010{i}-0000.db").write_bytes(b"x")
    db.backup_recent()
    left = sorted(p.name for p in backups.glob("app-*.db"))
    assert len(left) == 7, f"轮转后应剩 7 份，实际 {len(left)}"
    assert not any(n < "app-20200103" for n in left), "最旧的三份被删除"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print("  ok  ", fn.__name__)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("  FAIL", fn.__name__, "->", exc)
    print(f"{ok}/{len(fns)} passed")