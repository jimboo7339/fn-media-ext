"""UI 2.0 双版本渲染的回归测试。

UI 1.0 与 UI 2.0 并存：cookie ``fnxt.ui=2`` 走 templates2，缺省走 templates。
两边必须都能渲染全部页面，且 ``?ui=2`` 能设置 cookie 并把参数从地址里拿掉。

运行：``.venv/Scripts/python.exe tests/test_ui2.py``
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("PREVIEW_AUTOLOGIN", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests._freshdb import fresh_db  # noqa: E402

PAGES = ["/", "/live", "/matrix", "/dropouts", "/stats", "/recap", "/digest",
         "/libraries", "/settings", "/search?q=x"]


def _shared_client() -> TestClient:
    if "client" not in _STATE:
        fresh_db("fnxt-ui2-")  # 运行时再重指，避免与其它测试文件的模块级改动互相污染
        c = TestClient(main.app, follow_redirects=False)
        c.post("/setup", data={"username": "admin", "password": "secret6"})
        _STATE["client"] = c
    return _STATE["client"]


_STATE: dict = {}


def test_ui1_renders_every_page_by_default():
    c = _shared_client()
    for p in PAGES:
        r = c.get(p)
        assert r.status_code == 200, f"UI1 {p} -> {r.status_code}"
        assert "ui2.css" not in r.text, "UI1 页面不得引用 2.0 的样式"


def test_ui2_renders_every_page_with_cookie():
    c = _shared_client()
    c.cookies.set("fnxt.ui", "2")
    for p in PAGES:
        r = c.get(p)
        assert r.status_code == 200, f"UI2 {p} -> {r.status_code}"
        assert "/static/ui2.css" in r.text, f"UI2 {p} 应引用 ui2.css"
        assert "/static/app.css" not in r.text, f"UI2 {p} 不应引用 1.0 样式"


def test_ui_query_switch_sets_cookie_and_strips_param():
    c = _shared_client()
    r = c.get("/stats?ui=2", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/stats", "ui 参数被拿掉、其余参数保留"
    assert "fnxt.ui=2" in r.headers.get("set-cookie", "")
    r = c.get("/stats?days=30&ui=1", follow_redirects=False)
    assert r.headers["location"] == "/stats?days=30", "其它查询参数原样保留"


def test_ui2_login_page_is_standalone():
    # 未登录 + UI2 cookie：登录页本体应走 2.0（render 按 cookie 双路）
    c2 = TestClient(main.app)
    c2.cookies.set("fnxt.ui", "2")
    r = c2.get("/login")
    assert r.status_code == 200
    assert "/static/ui2.css" in r.text
    assert 'class="gate"' in r.text


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