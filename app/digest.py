"""每日观影日报：取数 → 排版 → 投递。

三件事刻意分开：

1. **取数** `build(day)` 只读派生表与快照，返回一个纯字典型的数据模型（不含 date 对象），
   页面和 webhook 用的是同一份，不会出现「页面上一套、推送上另一套」。
2. **排版** 先把内容拆成 `block` 列表，再分别渲染成 Markdown / 纯文本 / Telegram HTML。
   同一份 block 渲染三种格式，改文案只改一处。
3. **投递** 只依赖标准库 `urllib`，不引入新依赖；失败写进 `digest_log`，自动推送失败会退避
   15 分钟再试，不会每 30 秒锤一次 webhook；错过当天窗口（超过 12 小时）就直接跳过，
   避免半夜开机时突然收到早上九点的日报。

时长的口径和 `/stats`、`/recap` 一致：每集取「播放位置的最大值」再求和，是估算，不是
实际播放耗时。家庭总时长按集去重（两个人同看一集只算一次），所以它可能小于成员时长相加。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import io
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

try:  # Pillow 在 requirements 里；老容器没重建时也不该让整个应用起不来
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

try:
    from . import card
except ImportError:  # pragma: no cover
    card = None  # type: ignore[assignment]

from . import additions, config, db, ingest, queries

# --------------------------------------------------------------- 配置
KINDS = (
    ("generic", "通用 JSON"),
    ("serverchan", "Server 酱"),
    ("telegram", "Telegram Bot"),
    ("wechat", "企业微信群机器人"),
    ("dingtalk", "钉钉自定义机器人"),
    ("feishu", "飞书自定义机器人"),
)
KIND_LABELS = dict(KINDS)

DEFAULTS = {
    "digest_on": "0",
    "digest_at": "09:00",
    "digest_mode": "yesterday",   # yesterday = 早上发昨天的日报；today = 晚上发当天的日报
    "digest_kind": "generic",
    "digest_url": "",
    "digest_chat": "",            # Telegram 的 chat_id
    "digest_link": "",            # 可选：看板的对外地址，也用来拼图片链接
    "digest_skip_empty": "1",
    "digest_form": "both",        # text / image / both
}

LATE_HOURS = 12       # 超过这么久才启动就不再补发
RETRY_MINUTES = 15    # 投递失败后隔多久再试
TIMEOUT = 10
WECHAT_BYTES = 4000   # 企业微信 markdown 上限 4096 字节，留点余量

_LOCK = threading.RLock()  # 定时和「立即推送」可能同时发生，只让一个真的发出去


# --------------------------------------------------------------- 设置
def _parse_at(value: str) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (AttributeError, ValueError):
        pass
    return 9, 0


def settings() -> dict:
    stored = db.settings_map(list(DEFAULTS))
    conf = {
        key: (stored[key] if stored.get(key) is not None else default)
        for key, default in DEFAULTS.items()
    }
    conf["on"] = conf["digest_on"] == "1"
    conf["at"] = "%02d:%02d" % _parse_at(conf["digest_at"])
    conf["mode"] = conf["digest_mode"] if conf["digest_mode"] in ("yesterday", "today") else "yesterday"
    conf["kind"] = conf["digest_kind"] if conf["digest_kind"] in KIND_LABELS else "generic"
    conf["url"] = (conf["digest_url"] or "").strip()
    conf["chat"] = (conf["digest_chat"] or "").strip()
    conf["link"] = (conf["digest_link"] or "").strip().rstrip("/")
    conf["skip_empty"] = conf["digest_skip_empty"] == "1"
    conf["form"] = conf["digest_form"] if conf["digest_form"] in ("text", "both", "image") else "both"
    return conf


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "on", "true", "yes")


def save(form: dict) -> dict:
    """表单原样传进来，这里负责清洗后再落库。"""
    hour, minute = _parse_at(form.get("at", DEFAULTS["digest_at"]))
    mode = form.get("mode", "yesterday")
    kind = form.get("kind", "generic")
    db.set_setting("digest_on", "1" if _truthy(form.get("on")) else "0")
    db.set_setting("digest_at", "%02d:%02d" % (hour, minute))
    db.set_setting("digest_mode", mode if mode in ("yesterday", "today") else "yesterday")
    db.set_setting("digest_kind", kind if kind in KIND_LABELS else "generic")
    db.set_setting("digest_url", (form.get("url") or "").strip())
    db.set_setting("digest_chat", (form.get("chat") or "").strip())
    db.set_setting("digest_link", (form.get("link") or "").strip())
    db.set_setting("digest_skip_empty", "1" if _truthy(form.get("skip_empty")) else "0")
    pushed = form.get("form", "both")
    db.set_setting("digest_form", pushed if pushed in ("text", "both", "image") else "both")
    return settings()


# --------------------------------------------------------------- 取数
def parse_day(raw: str) -> date | None:
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def default_day(now: datetime | None = None) -> date:
    """Preview / manual push default: the day the next scheduled push would cover."""
    now = now or datetime.now(config.TZ)
    return now.date() - timedelta(days=1) if settings()["mode"] == "yesterday" else now.date()


def _ep_code(season, episode) -> str:
    if season and episode:
        return f"S{int(season):02d}E{int(episode):02d}"
    if episode:
        return f"E{int(episode):02d}"
    return ""


def _ep_range(eps: list[tuple[int, int]]) -> str:
    """S01E09–E14 for a contiguous run, otherwise say how many."""
    if not eps:
        return ""
    if len(eps) == 1:
        return _ep_code(*eps[0])
    first, last = eps[0], eps[-1]
    if first[0] == last[0] and len(eps) > 1 and last[1] - first[1] + 1 == len(eps):
        return f"{_ep_code(*first)}–E{int(last[1]):02d}" if last[1] else _ep_code(*first)
    if first[0] == last[0]:
        return f"{_ep_code(*first)} 等 {len(eps)} 集"
    return f"{_ep_code(*first)} 起 {len(eps)} 集"


def build(day: date) -> dict:
    events = queries.allowed_events()
    evs = [e for e in events if queries.day_of(e["event_time"]) == day]
    prev_day = day - timedelta(days=1)
    prev = [e for e in events if queries.day_of(e["event_time"]) == prev_day]

    people: dict[str, dict] = {}
    shows: dict[str, dict] = {}
    titles: dict[str, str] = {}

    for e in evs:
        titles[e["show_key"]] = e["show_title"]

        p = people.setdefault(
            e["user_name"],
            {"name": e["user_name"], "events": 0, "items": set(), "pos": {},
             "shows": defaultdict(int), "eps": defaultdict(list), "last": 0},
        )
        p["events"] += 1
        p["items"].add(e["item_guid"])
        p["pos"][e["item_guid"]] = max(p["pos"].get(e["item_guid"], 0), e["position"] or 0)
        p["shows"][e["show_key"]] += 1
        p["eps"][e["show_key"]].append((e["season"] or 0, e["episode"] or 0))
        p["last"] = max(p["last"], e["event_time"])

        s = shows.setdefault(
            e["show_key"],
            {"key": e["show_key"], "show": e["show_title"], "events": 0,
             "items": set(), "people": set(), "pos": {},
             "latest": "", "last": 0, "poster": e["poster"] or "",
             "library": e["library"] or "未归属"},
        )
        s["events"] += 1
        s["items"].add(e["item_guid"])
        s["people"].add(e["user_name"])
        s["pos"][e["item_guid"]] = max(s["pos"].get(e["item_guid"], 0), e["position"] or 0)
        if e["event_time"] >= s["last"]:
            s["last"] = e["event_time"]
            s["latest"] = queries.episode_label(e)

    # 「看到第几集」只能是库里真实存在的集：观看记录里那些挂不上 Season 树的
    # 集号（孤儿条目、被借用的 trim_id）不能拿来当进度，否则卡片会自相矛盾。
    catalog = queries.library_episodes([e["show_key"] for e in evs])
    seen: dict[str, tuple[int, int]] = {}
    for e in events:
        hit = catalog.get(e["show_key"])
        if not hit or e["item_guid"] not in hit["guids"]:
            continue
        pair = (e["season"] or 0, e["episode"] or 0)
        if pair > seen.get(e["show_key"], (0, 0)):
            seen[e["show_key"]] = pair

    people_rows = []
    for p in people.values():
        top_key = max(p["shows"].items(), key=lambda kv: kv[1])[0] if p["shows"] else ""
        people_rows.append(
            {
                "name": p["name"],
                "color": queries.color_of(p["name"]),
                "initial": queries.initial(p["name"]),
                "events": p["events"],
                "episodes": len(p["items"]),
                "shows_n": len(p["shows"]),
                "minutes": sum(p["pos"].values()) // 60,
                "top_show": titles.get(top_key, ""),
                "top_key": top_key,
                "top_eps": _ep_range(sorted(set(p["eps"].get(top_key, [])))) if top_key else "",
                "last_label": queries.clock(p["last"]),
            }
        )

    by_minutes = any(p["minutes"] > 0 for p in people_rows)
    if by_minutes:
        people_rows.sort(key=lambda p: (-p["minutes"], -p["episodes"], p["name"]))
    else:
        # 全是「标记已看」、没有播放位置时，时长排名没有任何信息量，退回按条数
        people_rows.sort(key=lambda p: (-p["events"], -p["episodes"], p["name"]))

    peak = max(((p["minutes"] if by_minutes else p["events"]) for p in people_rows), default=0)
    for p in people_rows:
        value = p["minutes"] if by_minutes else p["events"]
        p["pct"] = round(value * 100 / peak) if peak else 0
        p["value_label"] = _hm(p["minutes"]) if by_minutes else f'{p["events"]} 条'

    show_rows = []
    for s in shows.values():
        show_rows.append(
            {
                "key": s["key"],
                "show": s["show"],
                "events": s["events"],
                "episodes": len(s["items"]),
                "people": len(s["people"]),
                "people_names": sorted(s["people"]),
                "minutes": sum(s["pos"].values()) // 60,
                "last_watched": s["latest"],       # 当天最后一条记录看的集
                "seen": queries.episode_code(*seen.get(s["key"], (0, 0))),
                "available": "",                   # 库里更新到第几集，稍后查快照补
                "last_label": queries.clock(s["last"]),
                "poster": s["poster"],
                "library": s["library"],
                "hue": queries.hue_of(s["show"]),
                "initial": queries.initial(s["show"]),
            }
        )
    show_rows.sort(key=lambda s: (-s["episodes"], -s["minutes"], s["show"]))
    for s in show_rows:
        hit = catalog.get(s["key"])
        if hit:
            s["available"] = queries.episode_code(hit["season"], hit["episode"])

    prev_minutes = queries.minutes_of(prev)
    minutes = queries.minutes_of(evs)
    delta_events = delta_minutes = None
    if len(prev) >= 3:  # 基数太小的时候，百分比变化只是噪音
        delta_events = len(evs) - len(prev)
        delta_minutes = minutes - prev_minutes

    # 新增入库来自源库快照，不经过 watch_event，所以排除规则要在这里再过一遍
    add = additions.summary(day)
    hidden_libs = db.exclusions()["library"]
    if hidden_libs and add["shows"]:
        kept = [g for g in add["shows"] if (g["library"] or "") not in hidden_libs]
        if len(kept) != len(add["shows"]):
            add = {
                "groups": len(kept),
                "episodes": sum(g["n"] for g in kept if g["kind"] == "剧集"),
                "movies": sum(1 for g in kept if g["kind"] == "电影"),
                "total": sum(g["n"] for g in kept),
                "shows": kept,
            }

    return {
        "day": day.isoformat(),
        "label": f"{day.month} 月 {day.day} 日 {queries.WEEKDAYS[day.weekday()]}",
        "weekday": queries.WEEKDAYS[day.weekday()],
        "totals": {
            "events": len(evs),
            "episodes": len({e["item_guid"] for e in evs}),
            "shows": len(shows),
            "people": len(people),
            "minutes": minutes,
            "delta_events": delta_events,
            "delta_minutes": delta_minutes,
        },
        "prev": {"events": len(prev), "minutes": prev_minutes,
                 "label": f"{prev_day.month} 月 {prev_day.day} 日"},
        "by_minutes": by_minutes,
        "people": people_rows,
        "shows": show_rows,
        "additions": add,
    }


# --------------------------------------------------------------- 排版
def _hm(minutes: int) -> str:
    if minutes <= 0:
        return "0 分"
    if minutes < 60:
        return f"{minutes} 分"
    hour, minute = divmod(minutes, 60)
    return f"{hour} 小时 {minute} 分" if minute else f"{hour} 小时"


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else f"{value}"


def blocks(report: dict, link: str = "") -> list[dict]:
    """Ordered content blocks; the three renderers below all start from this."""
    t = report["totals"]
    add = report["additions"]
    out: list[dict] = [{"t": "h", "text": f'📺 观影日报 · {report["label"]}'}]

    if not t["events"] and not add["groups"]:
        out.append({"t": "p", "text": "这一天没有观看记录，也没有新内容入库。"})
    else:
        head = (f'全天 {t["events"]} 条 · {_hm(t["minutes"])} · '
                f'{t["people"]} 人 · {t["shows"]} 部剧')
        if t["delta_events"]:
            head += f'（较前一天 {_signed(t["delta_events"])} 条）'
        elif t["delta_minutes"]:
            head += f'（较前一天 {_signed(t["delta_minutes"])} 分）'
        out.append({"t": "p", "text": head})

    if report["people"]:
        out.append({"t": "h", "text": "⏱ 时长排名" if report["by_minutes"] else "👥 成员观看"})
        for i, p in enumerate(report["people"][:8], 1):
            rank = _hm(p["minutes"]) if report["by_minutes"] else f'{p["events"]} 条'
            tail = f' · {p["episodes"]} 集'
            if p["top_show"]:
                tail += f' · 《{p["top_show"]}》{p["top_eps"]}'
            out.append({"t": "line", "text": f'{i}. {p["name"]} — {rank}{tail}'})
        if len(report["people"]) > 8:
            out.append({"t": "line", "text": f'… 另有 {len(report["people"]) - 8} 人'})

    if report["shows"]:
        out.append({"t": "h", "text": "🎬 剧集"})
        for s in report["shows"][:6]:
            bits = [f'{s["episodes"]} 集', f'{s["people"]} 人']
            if s["minutes"]:
                bits.append(_hm(s["minutes"]))
            if s["seen"]:
                bits.append(f'看到 {s["seen"]}')
            if s["available"] and s["available"] != s["seen"]:
                bits.append(f'更新至 {s["available"]}')
            out.append({"t": "line", "text": f'《{s["show"]}》 ' + " · ".join(bits)})
        if len(report["shows"]) > 6:
            out.append({"t": "line", "text": f'… 另有 {len(report["shows"]) - 6} 部'})

    if add["groups"]:
        count = []
        if add["episodes"]:
            count.append(f'{add["episodes"]} 集')
        if add["movies"]:
            count.append(f'{add["movies"]} 部电影')
        out.append({"t": "h", "text": f'🆕 新增入库 · {add["groups"]} 部（' + " · ".join(count) + "）"})
        for g in add["shows"][:8]:
            out.append({"t": "line", "text": g["label"]})
        if add["groups"] > 8:
            out.append({"t": "line", "text": f'… 另有 {add["groups"] - 8} 部'})

    if link:
        out.append({"t": "link", "text": "查看当天看板", "url": f'{link}/digest?day={report["day"]}'})
    return out


def render(report: dict, link: str = "") -> dict:
    bl = blocks(report, link)
    return {
        "title": f'观影日报 · {report["label"]}',
        "markdown": _markdown(bl),
        "text": _plain(bl),
        "html": _html_text(bl),
    }


def _markdown(bl: list[dict]) -> str:
    lines: list[str] = []
    for b in bl:
        if b["t"] == "h":
            lines.extend(["", f'**{b["text"]}**'])
        elif b["t"] == "link":
            lines.extend(["", f'[{b["text"]}]({b["url"]})'])
        else:
            lines.append(b["text"])
    return "\n".join(lines).strip()


def _plain(bl: list[dict]) -> str:
    lines: list[str] = []
    for b in bl:
        if b["t"] == "h":
            lines.extend(["", b["text"]])
        elif b["t"] == "link":
            lines.extend(["", f'{b["text"]}：{b["url"]}'])
        else:
            lines.append(b["text"])
    return "\n".join(lines).strip()


def _html_text(bl: list[dict]) -> str:
    lines: list[str] = []
    for b in bl:
        if b["t"] == "h":
            lines.extend(["", f'<b>{_html.escape(b["text"])}</b>'])
        elif b["t"] == "link":
            lines.extend(["", f'<a href="{_html.escape(b["url"], quote=True)}">{_html.escape(b["text"])}</a>'])
        else:
            lines.append(_html.escape(b["text"]))
    return "\n".join(lines).strip()


def _clip(text: str, limit: int) -> str:
    """Byte-aware truncation for platforms with a hard message-size cap."""
    if len(text.encode("utf-8")) <= limit:
        return text
    budget = limit - 32
    out: list[str] = []
    size = 0
    for line in text.splitlines():
        cost = len(line.encode("utf-8")) + 1
        if size + cost > budget:
            break
        out.append(line)
        size += cost
    return "\n".join(out) + "\n…（内容过长，已截断）"


# --------------------------------------------------------------- 投递
def _post(url: str, body: dict, check_body: bool = True) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "fn-media-ext-digest/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(2000).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "message": detail or str(exc)}
    except Exception as exc:  # noqa: BLE001 - 网络错误要显示给人看
        return {"ok": False, "status": 0, "message": f"{type(exc).__name__}: {exc}"}
    ok = 200 <= status < 300 and (not check_body or _api_ok(raw))
    return {"ok": ok, "status": status, "message": raw[:400] or "ok"}


def _api_ok(raw: str) -> bool:
    """HTTP 200 不等于成功：企业微信/钉钉/飞书/Server酱 都把错误放在 JSON 里。"""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return True
    if not isinstance(doc, dict):
        return True
    # 各家用的字段名不同，认第一个出现的那个；不要既看 errcode 又看 code，
    # 有的网关会同时带上两个含义不同的字段
    for key in ("errcode", "code", "StatusCode"):
        if key in doc:
            return doc[key] in (0, "0", None)
    return doc.get("ok") is not False


_CARD_CACHE: dict[str, tuple[float, bytes]] = {}
CARD_TTL = 300
WECOM_IMAGE_BYTES = 1_400_000  # base64 后仍小于企业微信的 2MB 上限


def card_available() -> bool:
    return bool(card) and card.available()


def card_png(day: date, fresh: bool = False) -> bytes | None:
    """Render (and briefly cache) the share card. None when PIL or a CJK font is missing."""
    if card is None:
        return None
    key = day.isoformat()
    hit = _CARD_CACHE.get(key)
    if not fresh and hit and time.time() - hit[0] < CARD_TTL:
        return hit[1]
    try:
        png = card.render(build(day))
    except Exception:  # noqa: BLE001 - 渲染失败要退回文字，不能拖垮推送
        import traceback
        traceback.print_exc()
        png = None
    if png:
        _CARD_CACHE[key] = (time.time(), png)
        while len(_CARD_CACHE) > 8:
            _CARD_CACHE.pop(next(iter(_CARD_CACHE)))
    return png


def _secret() -> str:
    secret = db.get_setting("digest_secret")
    if not secret:
        secret = secrets.token_hex(16)
        db.set_setting("digest_secret", secret)
    return secret


def card_sig(day: date) -> str:
    return hmac.new(_secret().encode(), f"card:{day.isoformat()}".encode(), hashlib.sha256).hexdigest()[:32]


def card_sig_ok(day: date, sig: str) -> bool:
    return bool(sig) and hmac.compare_digest(card_sig(day), sig)


def card_url(conf: dict, day: date) -> str:
    """Public, signed card URL. Empty when no outward dashboard address is configured."""
    if not conf["link"]:
        return ""
    return f'{conf["link"]}/card/{day.isoformat()}.png?sig={card_sig(day)}'


def _shrink(png: bytes, limit: int) -> bytes:
    """平台有硬上限时先转 JPEG，再退一步缩放。"""
    if len(png) <= limit:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGB")
    for quality in (90, 84, 78, 72, 66):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= limit:
            return buf.getvalue()
    w, h = im.size
    im = im.resize((int(w * 0.8), int(h * 0.8)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()


def _wecom_image(png: bytes) -> dict:
    data = _shrink(png, WECOM_IMAGE_BYTES)
    return {
        "msgtype": "image",
        "image": {
            "base64": base64.b64encode(data).decode(),
            "md5": hashlib.md5(data).hexdigest(),
        },
    }


def _merge(results: list[dict]) -> dict:
    """一条日报可能拆成文字 + 图片两个请求，成功与否要合起来看。"""
    if len(results) == 1:
        return {k: results[0][k] for k in ("ok", "status", "message")}
    ok = all(r["ok"] for r in results)
    failed = next((r for r in results if not r["ok"]), results[-1])
    message = " | ".join(f'{r["label"]}: {r["message"]}' for r in results)
    return {"ok": ok, "status": failed["status"], "message": message}


def deliver(conf: dict, title: str, md: str, text: str, html_body: str,
            report: dict | None = None) -> dict:
    url = conf["url"]
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "status": 0, "message": "Webhook 地址要以 http:// 或 https:// 开头"}

    kind = conf["kind"]
    form = conf["form"]
    day = parse_day(report["day"]) if report else None
    png = card_png(day, fresh=True) if (day and form in ("both", "image")) else None
    img_url = card_url(conf, day) if day else ""

    # 钉钉 / 飞书 / Server 酱只能发图片链接；没有「看板地址」就退回文字
    needs_url = kind in ("dingtalk", "feishu", "serverchan")
    can_embed = bool(png and img_url) if needs_url else bool(png)
    wants_text = form in ("both", "text") or (form == "image" and not can_embed)

    if kind == "wechat":
        results = []
        if wants_text or not png:
            results.append({**_post(url, {
                "msgtype": "markdown",
                "markdown": {"content": _clip(md, WECHAT_BYTES)},
            }, True), "label": "文字"})
        if png:
            results.append({**_post(url, _wecom_image(png), True), "label": "图片"})
        return _merge(results)

    if kind == "dingtalk":
        body_md = md if (wants_text or not can_embed) else f"**{title}**"
        if can_embed:
            body_md += f"\n\n![观影日报]({img_url})"
        return _post(url, {"msgtype": "markdown",
                           "markdown": {"title": title, "text": _clip(body_md, 18000)}}, True)

    if kind == "feishu":
        body_text = text if (wants_text or not can_embed) else title
        if can_embed:
            body_text += f"\n\n图片：{img_url}"
        return _post(url, {"msg_type": "text",
                           "content": {"text": _clip(body_text, 18000)}}, True)

    if kind == "serverchan":
        desp = md if (wants_text or not can_embed) else f"**{title}**"
        if can_embed:
            desp += f"\n\n![观影日报]({img_url})"
        return _post(url, {"title": title, "desp": desp}, True)

    if kind == "telegram":
        if not conf["chat"]:
            return {"ok": False, "status": 0, "message": "Telegram 需要填写聊天 ID"}
        body = {
            "chat_id": conf["chat"],
            "text": html_body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if png and img_url:
            body["text"] = html_body + f'\n\n<a href="{_html.escape(img_url, quote=True)}">查看日报卡片</a>'
        return _post(url, body, True)

    # 通用 JSON：结构化数据与图片一起给，接收方自己决定怎么用
    body = {
        "title": title,
        "markdown": md,
        "text": text,
        "day": report["day"] if report else "",
        "report": report or {},
    }
    if png:
        body["image_base64"] = base64.b64encode(png).decode()
        body["image_mime"] = "image/png"
    if img_url:
        body["card_url"] = img_url
    return _post(url, body, False)


def send_report(day: date, reason: str = "manual") -> dict:
    """Build the report for `day` and push it. Manual sends ignore 「空数据跳过」."""
    with _LOCK:
        return _send(day, reason)


def _send(day: date, reason: str) -> dict:
    conf = settings()
    report = build(day)
    text = render(report, conf["link"])
    empty = not report["totals"]["events"] and not report["additions"]["groups"]

    if empty and conf["skip_empty"] and reason == "auto":
        db.set_setting("digest_last", day.isoformat())
        db.set_setting("digest_tries", "0")
        db.log_digest(day.isoformat(), conf["kind"], True, 0, "当天没有观看记录与新入库，按设置跳过")
        return {
            "ok": True, "skipped": True, "status": 0,
            "message": "当天没有观看记录与新入库，按设置跳过",
            "day": day.isoformat(), "title": text["title"],
        }

    result = deliver(conf, text["title"], text["markdown"], text["text"], text["html"], report)
    db.log_digest(day.isoformat(), conf["kind"], result["ok"], result["status"], result["message"])
    if result["ok"]:
        db.set_setting("digest_last", day.isoformat())
        db.set_setting("digest_tries", "0")
        db.set_setting("digest_next_try", "0")
    else:
        tries = int(db.get_setting("digest_tries") or 0) + 1
        db.set_setting("digest_tries", str(tries))
        db.set_setting("digest_next_try", str(int(time.time() * 1000) + RETRY_MINUTES * 60_000))
    return {**result, "skipped": False, "day": day.isoformat(), "title": text["title"]}


def send_test() -> dict:
    conf = settings()
    now = datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M")
    if conf["on"]:
        when = f"每天 {conf['at']}" + ("（发前一天）" if conf["mode"] == "yesterday" else "（发当天）")
    else:
        when = "未启用"
    title = "观影日报 · 测试消息"
    md = f"**{title}**\n\n收到这条消息，说明 webhook 配好了。\n自动推送：{when}\n发送时间：{now}"
    text = f"{title}\n\n收到这条消息，说明 webhook 配好了。\n自动推送：{when}\n发送时间：{now}"
    html_body = f"<b>{title}</b>\n\n收到这条消息，说明 webhook 配好了。\n发送时间：{now}"
    # 带上最近一天的卡片，这样「测试」也能验证图片通道（base64 / 签名链接）
    report = build(default_day())
    result = deliver(conf, title, md, text, html_body, report)
    db.log_digest(datetime.now(config.TZ).date().isoformat(), "test", result["ok"],
                  result["status"], result["message"])
    return {**result, "title": title}


# --------------------------------------------------------------- 定时
def tick() -> dict | None:
    """Called every 30s from the app's lifespan loop; sends at most once per report day."""
    conf = settings()
    if not conf["on"] or not conf["url"]:
        return None

    now = datetime.now(config.TZ)
    hour, minute = _parse_at(conf["at"])
    sched = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < sched:
        return None

    day = sched.date() - timedelta(days=1) if conf["mode"] == "yesterday" else sched.date()
    if db.get_setting("digest_last") == day.isoformat():
        return None

    if now - sched > timedelta(hours=LATE_HOURS):
        # NAS 白天关机、半夜才启动：这时候补发「昨天的日报」只会让人困惑
        db.set_setting("digest_last", day.isoformat())
        db.log_digest(day.isoformat(), conf["kind"], True, 0,
                      f"超过 {LATE_HOURS} 小时才启动，已跳过")
        return None

    if not _LOCK.acquire(blocking=False):
        return None
    try:
        next_try = float(db.get_setting("digest_next_try") or 0)
        if next_try and now.timestamp() * 1000 < next_try:
            return None
        ingest.maybe_sync()  # 推之前先确保派生表不是隔夜的
        return _send(day, reason="auto")
    finally:
        _LOCK.release()