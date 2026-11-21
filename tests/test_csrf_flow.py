"""CSRF 中间件的端到端流程测试（不发网络请求，走 Starlette TestClient）。

重点验证两件事：

1. 没有 csrf 令牌的 POST 一律 403；带了从页面里拿到的令牌就放行；
2. 中间件里 `await request.form()` 读过表单之后，handler 仍能拿到表单字段
   （BaseHTTPMiddleware 的 body 回放），这是整套同步器令牌方案成立的前提。

运行：``.venv/Scripts/python.exe tests/test_csrf_flow.py``
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("PREVIEW_AUTOLOGIN", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402
from tests._freshdb import fresh_db  # noqa: E402

_STATE: dict = {}


def _shared_client() -> TestClient:
    """整个文件共用一个已完成初始化 + 登录的客户端（含 lifespan 之外的初始化）。"""
    if "client" not in _STATE:
        fresh_db("fnxt-csrf-")  # 运行时重指，隔离其它测试文件的 config 改动
        c = TestClient(main.app, follow_redirects=False)
        r = c.post("/setup", data={"username": "admin", "password": "secret6"})
        assert r.status_code == 303, "初始化管理员失败"
        _STATE["client"] = c
    return _STATE["client"]


_CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]{32})"')


def test_post_without_csrf_is_rejected():
    c = _shared_client()
    r = c.post("/settings/refresh", data={})
    assert r.status_code == 403, "没有令牌的 POST 必须被拒绝"


def test_post_with_page_token_passes_and_form_reaches_handler():
    c = _shared_client()
    page = c.get("/settings")
    assert page.status_code == 200
    m = _CSRF_RE.search(page.text)
    assert m, "设置页的表单里必须埋有 csrf 令牌"
    token = m.group(1)

    r = c.post("/settings/refresh", data={"csrf": token})
    assert r.status_code == 303, "带令牌的 POST 放行（表单在中间件读过后 handler 仍可用）"

    # 带业务字段的表单：字段必须原样到达 handler
    r = c.post("/settings/toggle", data={"scope": "library", "target": "测试库", "csrf": token})
    assert r.status_code == 303
    excl = db.exclusions()
    assert "测试库" in excl["library"], "handler 拿到了表单字段（body 回放正常）"


def test_wrong_token_is_rejected():
    c = _shared_client()
    r = c.post("/settings/refresh", data={"csrf": "f" * 32})
    assert r.status_code == 403


def test_every_post_route_carries_the_csrf_dependency():
    """新增 POST 路由忘记挂 csrf_check 的话，这里直接红。"""
    from fastapi.routing import APIRoute

    exempt = {"/login", "/setup", "/api/refresh"}
    for route in main.app.routes:
        if isinstance(route, APIRoute) and "POST" in route.methods:
            has_dep = any(
                getattr(dep, "dependency", None) is main.csrf_check
                or getattr(dep, "call", None) is main.csrf_check
                for dep in route.dependencies
            )
            assert route.path in exempt or has_dep, f"{route.path} 缺少 csrf_check 依赖"


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