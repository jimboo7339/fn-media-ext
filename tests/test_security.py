"""批次 1 的安全基线测试：CSRF 令牌、密钥存取、海报路径约束、会话续期。

运行：``.venv/Scripts/python.exe tests/test_security.py``
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, config, db, posters  # noqa: E402


def _use_tmp_db():
    """把 app.db 指到临时目录，测试互不污染、也不碰真实数据。"""
    tmp = tempfile.mkdtemp(prefix="fnxt-test-")
    config.DATA_DIR = Path(tmp)
    config.APP_DB = Path(tmp) / "app.db"
    db.init()
    return Path(tmp)


def test_secret_setting_is_stable_across_calls():
    _use_tmp_db()
    first = db.secret_setting("app_secret")
    assert first
    assert db.secret_setting("app_secret") == first, "重复调用必须拿到同一个密钥"


def test_csrf_token_derives_from_session_token():
    _use_tmp_db()
    t1 = auth.csrf_token("session-token-1")
    t2 = auth.csrf_token("session-token-2")
    assert t1 and len(t1) == 32
    assert t1 != t2, "不同会话的令牌必须不同"
    assert auth.csrf_token("") == ""


def test_csrf_token_stable_and_independent_of_db_secret_regenerations():
    _use_tmp_db()
    token_a = auth.csrf_token("sid")
    token_b = auth.csrf_token("sid")
    assert token_a == token_b


def test_posters_rejects_paths_outside_root():
    tmp = Path(tempfile.mkdtemp(prefix="fnxt-img-"))
    config.IMG_ROOT = tmp / "img"
    (config.IMG_ROOT / "a").mkdir(parents=True)
    inside = config.IMG_ROOT / "a" / "inner.webp"
    inside.write_bytes(b"x")
    outside = tmp / "outside.webp"
    outside.write_bytes(b"x")

    # 直接路径命中
    assert posters.resolve("a/inner.webp") == inside.resolve()
    # .. 已在入口被拒
    assert posters.resolve("../outside.webp") is None
    # 模拟「索引把根目录之外的文件收了进来」：即使索引里有，也必须被 _safe 拦下
    posters._INDEX["outside.webp"] = outside
    assert posters.resolve("outside.webp") is None, "越界路径不得通过索引兜底返回"
    posters._INDEX.pop("outside.webp", None)


def test_session_sliding_renewal():
    _use_tmp_db()
    assert auth.create_account("alice", "secret6")
    account = auth.check_login("alice", "secret6")
    token = auth.new_session(account["id"])
    # 正常会话：剩余时间超过一半，不该续期
    got = auth.account_by_token(token)
    assert got and got["username"] == "alice"
    with db.session_scope() as con:
        row = con.execute("SELECT expires_at FROM session WHERE token = ?", (token,)).fetchone()
    # 手动把有效期压到一半以内，再访问一次应触发续期
    now = int(time.time())
    with db.session_scope() as con:
        con.execute("UPDATE session SET expires_at = ? WHERE token = ?",
                    ((now + 5 * 86_400) * 1000, token))
    got = auth.account_by_token(token)
    assert got, "半衰期内的会话仍然有效"
    with db.session_scope() as con:
        row = con.execute("SELECT expires_at FROM session WHERE token = ?", (token,)).fetchone()
    assert row["expires_at"] >= (now + auth.SESSION_SECONDS - 60) * 1000, "过期时间被拉回满格"


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