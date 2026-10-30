"""FastAPI entrypoint: timeline, matrix, settings and poster proxying."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, digest, ingest, live, meta, posters, queries, quiet


async def _periodic_sync() -> None:
    interval = max(60, config.SNAPSHOT_MINUTES * 60)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(ingest.sync_all)
        except Exception:  # noqa: BLE001 - keep the loop alive
            pass


async def _digest_loop() -> None:
    """每 30 秒看一眼是不是到了该发日报的时间；真正的判断在 digest.tick 里。"""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(digest.tick)
        except Exception:  # noqa: BLE001 - 推送失败不能拖垮应用
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    ingest.seed_defaults()
    if config.PREVIEW_AUTOLOGIN and auth.account_count() == 0:
        auth.create_account("admin", "admin123")
    await asyncio.to_thread(ingest.sync_all)
    await asyncio.to_thread(posters.build_index)
    sync_task = asyncio.create_task(_periodic_sync())
    digest_task = asyncio.create_task(_digest_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        digest_task.cancel()


app = FastAPI(title="观影时间线", lifespan=lifespan, docs_url=None, redoc_url=None)
quiet.install()  # 吞掉「非 HTTP 请求」的重复告警，按周期汇总成一条
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
tpl = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
tpl.env.globals.update(color_of=queries.color_of, initial=queries.initial, hue_of=queries.hue_of)


def _fmt_time(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, config.TZ).strftime("%m-%d %H:%M")


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
    ctx = {
        "request": request,
        "page": page,
        "account": request.state.account,
        "nav_q": "",
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
    return tpl.TemplateResponse(
        request, "timeline.html", shell(request, "timeline", nav_q=queries.nav_qs(state), **ctx)
    )


@app.get("/matrix")
def matrix(request: Request, days: str = "30", u: str = "", l: str = ""):
    state = queries.filter_state(_days(days), u, l)
    ctx = queries.matrix_context(state)
    return tpl.TemplateResponse(
        request, "matrix.html", shell(request, "matrix", nav_q=queries.nav_qs(state), **ctx)
    )


@app.get("/live")
def live_page(request: Request):
    return tpl.TemplateResponse(
        request, "live.html", shell(request, "live", live=live.snapshot())
    )


@app.get("/partials/live")
def live_partial(request: Request):
    """HTML fragment polled by the live page; same view model as the full page."""
    return tpl.TemplateResponse(
        request, "_live.html", shell(request, "live", live=live.snapshot())
    )


# -------------------------------------------------------------- detail pages
def _missing(what: str) -> Response:
    return Response(f"没有{what}的观看记录", status_code=404, media_type="text/plain; charset=utf-8")


@app.get("/show/{key}")
def show_page(request: Request, key: str):
    ctx = queries.show_context(key)
    if ctx is None:
        return _missing("这部影片")
    return tpl.TemplateResponse(request, "show.html", shell(request, "show", **ctx))


@app.get("/user/{name}")
def user_page(request: Request, name: str):
    ctx = queries.user_context(name)
    if ctx is None:
        return _missing("这个账号")
    # 侧栏列表在这里既是导航也是"你在看谁"的指示，所以详情页也要带上
    ctx.update(queries.default_filters("/"))
    return tpl.TemplateResponse(request, "user.html", shell(request, "user", **ctx))


# ------------------------------------------------------------------ analysis
@app.get("/search")
def search_page(request: Request, q: str = ""):
    ctx = queries.search_context(q)
    return tpl.TemplateResponse(request, "search.html", shell(request, "search", **ctx))


@app.get("/recap")
def recap_page(request: Request, mode: str = "month", p: str = ""):
    ctx = queries.recap_context(mode, p)
    return tpl.TemplateResponse(request, "recap.html", shell(request, "recap", **ctx))


@app.get("/dropouts")
def dropouts_page(request: Request, d: str = "30"):
    days = int(d) if d in ("7", "30", "90", "180") else 30
    ctx = queries.dropout_context(days)
    return tpl.TemplateResponse(request, "dropouts.html", shell(request, "dropouts", **ctx))


@app.get("/stats")
def stats_page(request: Request):
    ctx = queries.stats_context()
    return tpl.TemplateResponse(request, "stats.html", shell(request, "stats", **ctx))


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
    return tpl.TemplateResponse(
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


def _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form) -> None:
    digest.save(
        {"on": on, "at": at, "mode": mode, "kind": kind, "url": url,
         "chat": chat, "link": link, "skip_empty": skip_empty, "form": form}
    )


@app.post("/digest/save")
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
):
    _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form)
    return RedirectResponse("/digest?saved=1", 303)


@app.post("/digest/send")
def digest_send(day: str = Form("")):
    target = digest.parse_day(day) or digest.default_day()
    ingest.maybe_sync()  # 点「立即推送」时不想拿到十分钟前的数据
    result = digest.send_report(target)
    flag = "ok" if result["ok"] else "err"
    note = "推送成功" if result["ok"] else f'推送失败：{result["message"]}'
    return RedirectResponse(
        f'/digest?day={target}&sent={flag}&note={quote(note[:200])}', 303
    )


@app.post("/digest/test")
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
):
    # 「保存并发送测试」：先存下来，测的就是你刚填的这个地址
    _digest_form(on, at, mode, kind, url, chat, link, skip_empty, form)
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
    return tpl.TemplateResponse(request, "libraries.html", shell(request, "libraries", **ctx))


@app.get("/settings")
def settings(request: Request, done: int = 0, sync: str = "", pw: str = ""):
    excl = db.exclusions()
    cats = db.catalogs()
    state = db.sync_state() or {}
    events = queries.load_events()
    shows = {e["show_key"]: e["poster"] for e in events}
    poster_stats = posters.stats([{"show_key": k, "poster": v} for k, v in shows.items()])
    return tpl.TemplateResponse(
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
    return tpl.TemplateResponse(
        request, "login.html", {"request": request, "next": next, "err": err, "pw": pw}
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    account = auth.check_login(username, password)
    if not account:
        return tpl.TemplateResponse(
            request,
            "login.html",
            {"request": request, "next": next, "err": 1, "pw": 0},
            status_code=401,
        )
    token = auth.new_session(account["id"])
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, 303)
    resp.set_cookie("sid", token, max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax")
    return resp


@app.get("/setup")
def setup_page(request: Request):
    return tpl.TemplateResponse(request, "setup.html", {"request": request, "err": 0})


@app.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.account_count() or len(password) < 6 or not auth.create_account(username, password):
        return tpl.TemplateResponse(request, "setup.html", {"request": request, "err": 1}, status_code=400)
    account = auth.check_login(username, password)
    token = auth.new_session(account["id"])
    resp = RedirectResponse("/", 303)
    resp.set_cookie("sid", token, max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.destroy_session(request.cookies.get("sid"))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("sid")
    return resp


# ----------------------------------------------------------------- settings
@app.post("/settings/toggle")
def toggle_exclusion(scope: str = Form(...), target: str = Form(...)):
    if scope in ("library", "account") and target:
        db.toggle_exclusion(scope, target)
    return RedirectResponse("/settings?done=1", 303)


@app.post("/settings/password")
def change_password(request: Request, old: str = Form(...), new: str = Form(...)):
    ok = auth.change_password(request.state.account["id"], old, new)
    if not ok:
        return RedirectResponse("/settings?pw=err", 303)
    resp = RedirectResponse("/login?pw=1", 303)
    resp.delete_cookie("sid")
    return resp


@app.post("/settings/refresh")
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
    state = db.sync_state() or {}
    body = {"ok": True, "synced_at": state.get("synced_at"), "events": state.get("events")}
    body.update(quiet.stats())
    return JSONResponse(body)


@app.post("/api/refresh")
def api_refresh():
    return JSONResponse(ingest.sync_all(force=True))


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    # 浏览器会自动请求它，没有图标就回 204，别在控制台留 404
    return Response(status_code=204)
