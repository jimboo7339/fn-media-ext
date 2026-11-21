"""FastAPI entrypoint: timeline, matrix, settings and poster proxying."""
from __future__ import annotations

import asyncio
import hmac as _hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, digest, ingest, live, meta, posters, queries, quiet

logger = logging.getLogger("fnxt")


def _refuse_multi_worker() -> None:
    """单例后台任务（快照同步、日报推送）的锁都在进程内，多 worker 会重复推送。"""
    raw = os.getenv("WEB_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={raw}：本应用不支持多进程部署（同步与日报是单例任务）。"
            "请保持单 worker；要横向扩展时把同步与日报拆到独立容器。"
        )


async def _periodic_sync() -> None:
    interval = max(60, config.SNAPSHOT_MINUTES * 60)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(ingest.sync_all)
        except Exception:  # noqa: BLE001 - 保活循环，但必须留下痕迹
            logger.exception("定时同步失败")


async def _digest_loop() -> None:
    """每 30 秒看一眼是不是到了该发日报的时间；真正的判断在 digest.tick 里。"""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(digest.tick)
        except Exception:  # noqa: BLE001 - 推送失败不能拖垮应用
            logger.exception("日报定时推送失败")


async def _backup_loop() -> None:
    """app.db 每天冷备一次（VACUUM INTO），只留最近 7 份。"""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await asyncio.to_thread(db.backup_recent)
        except Exception:  # noqa: BLE001 - 备份失败不影响服务
            logger.exception("app.db 备份失败")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _refuse_multi_worker()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    db.init()
    ingest.seed_defaults()
    if config.PREVIEW_AUTOLOGIN and auth.account_count() == 0:
        auth.create_account("admin", "admin123")
    await asyncio.to_thread(ingest.sync_all)
    await asyncio.to_thread(posters.build_index)
    sync_task = asyncio.create_task(_periodic_sync())
    digest_task = asyncio.create_task(_digest_loop())
    backup_task = asyncio.create_task(_backup_loop())
    try:
        yield
    finally:
        for task in (sync_task, digest_task, backup_task):
            task.cancel()


app = FastAPI(title="观影时间线", lifespan=lifespan, docs_url=None, redoc_url=None)
quiet.install()  # 吞掉「非 HTTP 请求」的重复告警；是否再打周期汇总由 QUIET_SUMMARY 决定
# JSON / HTML 响应体普遍只有几 KB，但它对 /static 的 43KB CSS 和模板页都有收益
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
tpl = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
tpl2 = Jinja2Templates(directory=str(config.BASE_DIR / "templates2"))
for _env in (tpl.env, tpl2.env):
    _env.globals.update(color_of=queries.color_of, initial=queries.initial, hue_of=queries.hue_of)


def _engine(request: Request) -> Jinja2Templates:
    """UI 版本选择：cookie fnxt.ui=2 走 2.0，缺省永远是经典版（1.0 一行未改）。"""
    return tpl2 if request.cookies.get("fnxt.ui") == "2" else tpl


def render(request: Request, name: str, ctx: dict, **kwargs):
    """按当前 UI 版本挑模板引擎渲染；两个版本共用同一份数据上下文。"""
    return _engine(request).TemplateResponse(request, name, ctx, **kwargs)


def _fmt_time(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%m-%d %H:%M")


async def csrf_check(request: Request) -> None:
    """表单 POST 的 CSRF 防护（同步器令牌），以依赖的形式挂在每个 POST 路由上。

    不做成 BaseHTTPMiddleware：中间件里 `await request.form()` 读完的表单不会
    回放给下游，handler 拿到的就是空表单（实测 422）。依赖与 handler 共享同一个
    Request，body 只解析一次。令牌由会话 token 派生（auth.csrf_token），模板在
    渲染表单时埋进隐藏字段；tests/test_csrf_flow.py 强制每个 POST 路由都挂它。"""
    if config.PREVIEW_AUTOLOGIN:
        return
    sid = request.cookies.get("sid") or ""
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - 解析不出表单一律拒绝
        form = {}
    supplied = str(form.get("csrf") or "")
    expected = auth.csrf_token(sid)
    if not sid or not supplied or not _hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="csrf token missing or invalid")


@app.middleware("http")
async def ui_switch(request: Request, call_next):
    """?ui=2 / ?ui=1 随时切换 UI 版本：写一年期 cookie，再把参数从地址里拿掉。

    注册在两个中间件之后 = 最先执行，登录页上也能切。"""
    if request.method == "GET" and request.query_params.get("ui") in ("1", "2"):
        params = dict(request.query_params)
        ui = params.pop("ui")
        rest = urlencode(params)
        target = request.url.path + (f"?{rest}" if rest else "")
        resp = RedirectResponse(target, 303)
        resp.set_cookie("fnxt.ui", ui, max_age=365 * 86_400, samesite="lax")
        return resp
    return await call_next(request)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    account = auth.account_by_token(request.cookies.get("sid"))
    if account is None and config.PREVIEW_AUTOLOGIN:
        account = auth.first_account()
    request.state.account = account

    public = (path.startswith(("/static", "/card/"))
              or path in ("/login", "/setup", "/health", "/favicon.ico"))
    if account is None:
        if not auth.account_count():
            if path not in ("/setup", "/health") and not path.startswith("/static"):
                return RedirectResponse("/setup", 303)
        elif path == "/setup":
            return RedirectResponse("/login", 303)
        elif not public:
            return RedirectResponse("/login?next=" + quote(path), 303)
    elif path in ("/login", "/setup"):
        return RedirectResponse("/", 303)
    return await call_next(request)


@app.middleware("http")
async def freshness(request: Request, call_next):
    """页面访问时按需补同步。静态资源、健康检查和实时在线的轮询片段不触发。"""
    path = request.url.path
    if (
        request.method == "GET"
        and path not in ("/health",)
        and not path.startswith(("/static", "/img", "/partials", "/favicon"))
    ):
        try:
            await asyncio.to_thread(ingest.maybe_sync)
        except Exception:  # noqa: BLE001 - 补同步失败不能影响页面
            pass
    return await call_next(request)


def shell(request: Request, page: str, **extra) -> dict:
    state = db.sync_state() or {}
    sid = request.cookies.get("sid") or ""
    ctx = {
        "request": request,
        "page": page,
        "account": request.state.account,
        "nav_q": "",
        "csrf": auth.csrf_token(sid),
        "version": config.APP_VERSION,
        "synced": _fmt_time(state.get("synced_at")),
        "sync_error": state.get("error"),
    }
    ctx.update(extra)
    return ctx


# --------------------------------------------------------------------- pages
def _days(raw: str) -> int:
    """Only 7 / 30 / 90 / 0(all) are meaningful; anything else falls back to 30."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 30
    return value if value in queries.RANGES else 30


@app.get("/")
def timeline(request: Request, days: str = "30", u: str = "", l: str = ""):
    state = queries.filter_state(_days(days), u, l)
    ctx = queries.timeline_context(state, "/")
    return render(
        request, "timeline.html", shell(request, "timeline", nav_q=queries.nav_qs(state), **ctx)
    )


@app.get("/matrix")
def matrix(request: Request, days: str = "30", u: str = "", l: str = ""):
    state = queries.filter_state(_days(days), u, l)
    ctx = queries.matrix_context(state)
    return render(
        request, "matrix.html", shell(request, "matrix", nav_q=queries.nav_qs(state), **ctx)
    )


@app.get("/live")
def live_page(request: Request):
    return render(
        request, "live.html", shell(request, "live", live=live.snapshot())
    )


@app.get("/partials/live")
def live_partial(request: Request):
    """HTML fragment polled by the live page; same view model as the full page."""
    return render(
        request, "_live.html", shell(request, "live", live=live.snapshot())
    )


# -------------------------------------------------------------- detail pages
def _missing(request: Request, what: str) -> Response:
    """详情页 404 渲染成带导航的页面：从时间线点进来的人不应该被留在死胡同里。"""
    return render(
        request, "missing.html", {"request": request, "what": what}, status_code=404
    )


@app.get("/show/{key}")
def show_page(request: Request, key: str):
    ctx = queries.show_context(key)
    if ctx is None:
        return _missing(request, "这部影片")
    return render(request, "show.html", shell(request, "show", **ctx))


@app.get("/user/{name}")
def user_page(request: Request, name: str):
    ctx = queries.user_context(name)
    if ctx is None:
        return _missing(request, "这个账号")
    # 侧栏列表在这里既是导航也是"你在看谁"的指示，所以详情页也要带上
    ctx.update(queries.default_filters("/"))
    return render(request, "user.html", shell(request, "user", **ctx))


# ------------------------------------------------------------------ analysis
@app.get("/search")
def search_page(request: Request, q: str = ""):
    ctx = queries.search_context(q)
    return render(request, "search.html", shell(request, "search", **ctx))


@app.get("/recap")
def recap_page(request: Request, mode: str = "month", p: str = ""):
    ctx = queries.recap_context(mode, p)
    return render(request, "recap.html", shell(request, "recap", **ctx))


@app.get("/dropouts")
def dropouts_page(request: Request, d: str = "30"):
    days = int(d) if d in ("7", "30", "90", "180") else 30
    ctx = queries.dropout_context(days)
    return render(request, "dropouts.html", shell(request, "dropouts", **ctx))


@app.get("/stats")
def stats_page(request: Request):
    ctx = queries.stats_context()
    return render(request, "stats.html", shell(request, "stats", **ctx))


# ------------------------------------------------------------------- digest
def _digest_logs() -> list[dict]:
    rows = db.digest_logs()
    for row in rows:
        row["time"] = datetime.fromtimestamp(row["created_at"] / 1000, config.TZ).strftime("%m-%d %H:%M")
        row["kind_label"] = ("测试" if row["kind"] == "test"
                             else digest.KIND_LABELS.get(row["kind"], row["kind"]))
    return rows


@app.get("/digest")
def digest_page(request: Request, day: str = "", sent: str = "", note: str = "", saved: int = 0):
    conf = digest.settings()
    today = datetime.now(config.TZ).date()
    target = digest.parse_day(day) or digest.default_day()
    report = digest.build(target)
    return render(
        request,
        "digest.html",
        shell(
            request,
            "digest",
            conf=conf,
            kinds=digest.KINDS,
            report=report,
            preview=digest.render(report, conf["link"]),
            card_ok=digest.card_available(),
            day=target,
            today=today,
            prev_day=(target - timedelta(days=1)).isoformat(),
            next_day=(target + timedelta(days=1)).isoformat(),
            next_allowed=target < today,
            logs=_digest_logs(),
            sent=sent,
            note=note,
            saved=saved,
        ),
    )


def _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form,
                 sign_token, webhook_key, token) -> None:
    digest.save(
        {"on": on, "at": at, "mode": mode, "kind": kind, "url": url,
         "chat": chat, "link": link, "skip_empty": skip_empty, "form": form,
         "sign_token": sign_token, "webhook_key": webhook_key, "token": token}
    )


@app.post("/digest/save", dependencies=[Depends(csrf_check)])
def digest_save(
    on: str = Form(""),
    at: str = Form("09:00"),
    mode: str = Form("yesterday"),
    kind: str = Form("generic"),
    url: str = Form(""),
    chat: str = Form(""),
    link: str = Form(""),
    skip_empty: str = Form(""),
    form: str = Form("both"),
    sign_token: str = Form(""),
    webhook_key: str = Form(""),
    token: str = Form(""),
):
    _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form,
                 sign_token, webhook_key, token)
    return RedirectResponse("/digest?saved=1", 303)


@app.post("/digest/send", dependencies=[Depends(csrf_check)])
def digest_send(day: str = Form("")):
    target = digest.parse_day(day) or digest.default_day()
    ingest.maybe_sync()  # 点「立即推送」时不想拿到十分钟前的数据
    result = digest.send_report(target)
    flag = "ok" if result["ok"] else "err"
    note = "推送成功" if result["ok"] else f'推送失败：{result["message"]}'
    return RedirectResponse(
        f'/digest?day={target}&sent={flag}&note={quote(note[:200])}', 303
    )


@app.post("/digest/test", dependencies=[Depends(csrf_check)])
def digest_test(
    on: str = Form(""),
    at: str = Form("09:00"),
    mode: str = Form("yesterday"),
    kind: str = Form("generic"),
    url: str = Form(""),
    chat: str = Form(""),
    link: str = Form(""),
    skip_empty: str = Form(""),
    form: str = Form("both"),
    sign_token: str = Form(""),
    webhook_key: str = Form(""),
    token: str = Form(""),
):
    # 「保存并发送测试」：先存下来，测的就是你刚填的这个地址
    _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form,
                 sign_token, webhook_key, token)
    result = digest.send_test()
    flag = "ok" if result["ok"] else "err"
    note = "测试消息已发出" if result["ok"] else f'测试失败：{result["message"]}'
    return RedirectResponse(f'/digest?sent={flag}&note={quote(note[:200])}', 303)


@app.get("/card/{day}.png")
def card_image(request: Request, day: str, sig: str = ""):
    """日报卡片。带签名可以公开访问（钉钉/飞书的图片链接），否则要已登录。"""
    target = digest.parse_day(day)
    if target is None:
        return Response(status_code=404)
    if sig:
        if not digest.card_sig_ok(target, sig):
            return Response(status_code=403)
    elif request.state.account is None:
        return Response(status_code=403)
    png = digest.card_png(target)
    if not png:
        return Response(status_code=404)
    cache = "public, max-age=300" if sig else "private, max-age=60"
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": cache})


@app.get("/libraries")
def libraries_page(request: Request):
    ctx = queries.library_context()
    ctx.update(meta.scan())   # 刮削完整度盘点（源库没变就复用上次结论）
    return render(request, "libraries.html", shell(request, "libraries", **ctx))


@app.get("/settings")
def settings(request: Request, done: int = 0, sync: str = "", pw: str = ""):
    excl = db.exclusions()
    cats = db.catalogs()
    state = db.sync_state() or {}
    events = queries.load_events()
    shows = {e["show_key"]: e["poster"] for e in events}
    poster_stats = posters.stats([{"show_key": k, "poster": v} for k, v in shows.items()])
    return render(
        request,
        "settings.html",
        shell(
            request,
            "settings",
            libraries=[
                {**c, "excluded": c["name"] in excl["library"]}
                for c in cats["library"]
                if c["events"] or c["name"] in excl["library"]
            ],
            accounts=[
                {**c, "excluded": c["name"] in excl["account"]}
                for c in cats["account"]
                if c["events"] or c["name"] in excl["account"]
            ],
            sync_state=state,
            sync_time=_fmt_time(state.get("synced_at")),
            poster_stats=poster_stats,
            db_dir=str(config.DB_DIR),
            img_root=str(config.IMG_ROOT),
            data_dir=str(config.DATA_DIR),
            done=done,
            sync_result=sync,
            pw_changed=pw,
        ),
    )


# --------------------------------------------------------------------- auth
@app.get("/login")
def login_page(request: Request, next: str = "/", err: int = 0, pw: int = 0):
    return render(request, "login.html", {"request": request, "next": next, "err": err, "pw": pw})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    account = auth.check_login(username, password)
    if not account:
        return render(
            request,
            "login.html",
            {"request": request, "next": next, "err": 1, "pw": 0},
            status_code=401,
        )
    token = auth.new_session(account["id"])
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, 303)
    resp.set_cookie("sid", token, max_age=auth.SESSION_SECONDS, httponly=True,
                    samesite="lax", secure=config.COOKIE_SECURE)
    return resp


@app.get("/setup")
def setup_page(request: Request):
    return render(request, "setup.html", {"request": request, "err": 0})


@app.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.account_count() or len(password) < 6 or not auth.create_account(username, password):
        return render(request, "setup.html", {"request": request, "err": 1}, status_code=400)
    account = auth.check_login(username, password)
    token = auth.new_session(account["id"])
    resp = RedirectResponse("/", 303)
    resp.set_cookie("sid", token, max_age=auth.SESSION_SECONDS, httponly=True,
                    samesite="lax", secure=config.COOKIE_SECURE)
    return resp


@app.post("/logout", dependencies=[Depends(csrf_check)])
def logout(request: Request):
    auth.destroy_session(request.cookies.get("sid"))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("sid")
    return resp


# ----------------------------------------------------------------- settings
@app.post("/settings/toggle", dependencies=[Depends(csrf_check)])
def toggle_exclusion(scope: str = Form(...), target: str = Form(...)):
    if scope in ("library", "account") and target:
        db.toggle_exclusion(scope, target)
    return RedirectResponse("/settings?done=1", 303)


@app.post("/settings/password", dependencies=[Depends(csrf_check)])
def change_password(request: Request, old: str = Form(...), new: str = Form(...)):
    ok = auth.change_password(request.state.account["id"], old, new)
    if not ok:
        return RedirectResponse("/settings?pw=err", 303)
    resp = RedirectResponse("/login?pw=1", 303)
    resp.delete_cookie("sid")
    return resp


@app.post("/settings/refresh", dependencies=[Depends(csrf_check)])
def refresh_now():
    result = ingest.sync_all(force=True)
    flag = "err" if "error" in result else "ok"
    return RedirectResponse(f"/settings?sync={flag}", 303)


# --------------------------------------------------------------------- misc
MEDIA_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".avif": "image/avif",
}


@app.get("/img/{path:path}")
def image(path: str):
    hit = posters.resolve(path)
    if not hit:
        return Response(status_code=404)
    media_type = MEDIA_TYPES.get(hit.suffix.lower(), "application/octet-stream")
    return FileResponse(
        hit, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"}
    )


@app.get("/health")
def health():
    body: dict = {"ok": True, "version": config.APP_VERSION}
    try:
        state = db.sync_state() or {}
        body["synced_at"] = state.get("synced_at")
        body["events"] = state.get("events")
        body["sync_error"] = state.get("error") or None
    except Exception as exc:  # noqa: BLE001 - 健康检查必须如实反映数据库故障
        body["ok"] = False
        body["error"] = f"app.db unavailable: {exc}"
        return JSONResponse(body, status_code=500)
    body.update(quiet.stats())
    return JSONResponse(body)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """浏览器默认会直接要 /favicon.ico；模板里虽然声明了 SVG 图标，这里也补一个。"""
    return FileResponse(config.BASE_DIR / "static" / "favicon.ico")


@app.post("/api/refresh")
def api_refresh():
    """运维端点：只触发一次快照同步，效果温和，靠会话 cookie 鉴权即可，豁免 CSRF。"""
    return JSONResponse(ingest.sync_all(force=True))


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # SW 脚本本体必须每次拿最新的，不能让浏览器或它自己的缓存拖住升级
    resp = FileResponse(
        config.BASE_DIR / "static" / "sw.js", media_type="application/javascript"
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest", include_in_schema=False)
def webmanifest():
    return FileResponse(
        config.BASE_DIR / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    # 浏览器会自动请求它，没有图标就回 204，别在控制台留 404
    return Response(status_code=204)
