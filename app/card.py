"""把一天的日报画成一张 1080 宽的 PNG，供 webhook 推送。

为什么是 Pillow 而不是「HTML 截图」：容器里没有浏览器，Pillow + 一个中文字体是唯一
能在 NAS 上稳定出图的组合。字体不进仓库，Dockerfile 装 `fonts-wqy-microhei`；本地
开发用系统字体（Windows 的微软雅黑）。都找不到时 `render()` 返回 None，推送自动退回
纯文字——宁可不发图，也不要发一张全是方块的图。

版式是「暗色卡片 + 三张海报 + 排行榜」，不是把海报铺满背景：海报是竖图，拉满 1080
会糊，而聊天窗口里真正要看清的是日期、时长和名次。
"""
from __future__ import annotations

import colorsys
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import posters

WIDTH = 1080
PAD = 56
GAP = 34

BG = "#0e1014"
PANEL = "#161a22"
PANEL2 = "#1e2430"
LINE = "#2a3040"
TEXT = "#e8ebf2"
DIM = "#8b93a7"
FAINT = "#5f677a"
ACCENT = "#5b9cf8"
ACCENT2 = "#4fc3d9"
TRACK = "#232936"

MEMBER_ROWS = 5
SHOW_CARDS = 3
ADD_ROWS = 4

# 字体：先看环境变量，再按平台常见的路径找。第一个能用的就是正文，
# 粗体找不到时用同一套字体加描边模拟。
FONT_REGULAR = (
    os.getenv("CARD_FONT", ""),
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)
FONT_BOLD = (
    os.getenv("CARD_FONT_BOLD", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def _first(paths) -> str:
    for path in paths:
        if path and Path(path).is_file():
            return path
    return ""


@lru_cache(maxsize=1)
def _files() -> tuple[str, str]:
    regular = _first(FONT_REGULAR)
    override = os.getenv("CARD_FONT", "")
    if override and regular == override:
        # 显式指定了字体时，粗体也只能来自同一套（或它自己的粗体），
        # 不能拿系统里另一个字体的粗体去配，字形会打架
        bold = _first((os.getenv("CARD_FONT_BOLD", ""), regular)) or regular
    else:
        bold = _first(FONT_BOLD)
    if not regular:
        regular = bold
    if not bold:
        bold = regular
    return regular, bold


def available() -> bool:
    return bool(_files()[0])


@lru_cache(maxsize=128)
def _font(size: int, bold: bool = False):
    regular, bold_path = _files()
    path = bold_path if bold else regular
    if not path:
        return None
    try:
        return ImageFont.truetype(path, size, index=0)
    except OSError:
        return None


# ------------------------------------------------------------------ helpers
def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    ca, cb = _rgb(a), _rgb(b)
    return tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))  # type: ignore[return-value]


def _hm(minutes: int) -> str:
    if minutes <= 0:
        return "0 分"
    if minutes < 60:
        return f"{minutes} 分"
    hour, minute = divmod(minutes, 60)
    return f"{hour} 小时 {minute} 分" if minute else f"{hour} 小时"


def _text(draw, xy, text, size, color, bold=False, anchor="la"):
    font = _font(size, bold)
    if font is None:
        return
    # 没有真正的粗体文件时，用 1px 描边模拟，比纯细体在暗底上清楚
    stroke = 0 if (bold and _files()[1] != _files()[0]) else (1 if bold else 0)
    draw.text(xy, text, font=font, fill=color, anchor=anchor,
              stroke_width=stroke, stroke_fill=color)


def _width(draw, text, size, bold=False) -> float:
    font = _font(size, bold)
    return draw.textlength(text, font=font) if font else 0


def _fit(draw, text, size, max_w, bold=False) -> str:
    if _width(draw, text, size, bold) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _width(draw, text[:mid] + "…", size, bold) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo else "…"


def _panel(draw, box, radius=16, fill=PANEL, outline=LINE):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def _bar(img, box, pct, radius=6):
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(box, radius=radius, fill=TRACK)
    x1, y1, x2, y2 = box
    w = int((x2 - x1) * max(0, min(100, pct)) / 100)
    if w < radius * 2 or y2 <= y1:
        return
    strip = Image.new("RGB", (w, 1))
    sd = ImageDraw.Draw(strip)
    for i in range(w):
        sd.point((i, 0), fill=_mix(ACCENT, ACCENT2, i / max(1, w - 1)))
    strip = strip.resize((w, y2 - y1))
    mask = Image.new("L", (w, y2 - y1), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, y2 - y1 - 1], radius=radius, fill=255)
    img.paste(strip, (x1, y1), mask)


def _hue_color(name: str, sat=0.42, val=0.42) -> tuple[int, int, int]:
    hue = (sum(ord(c) for c in name) * 37 % 360) / 360
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return round(r * 255), round(g * 255), round(b * 255)


def _cover(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    iw, ih = im.size
    scale = max(tw / iw, th / ih)
    im = im.resize((max(tw, round(iw * scale)), max(th, round(ih * scale))), Image.Resampling.LANCZOS)
    left = max(0, (im.width - tw) // 2)
    top = max(0, (im.height - th) // 2)
    return im.crop((left, top, left + tw, top + th))


def _poster(path: str, name: str, size: tuple[int, int], radius: int) -> tuple[Image.Image, Image.Image]:
    """Returns (image, mask) so the caller can paste a rounded poster."""
    im = None
    hit = posters.resolve(path) if path else None
    if hit:
        try:
            im = Image.open(hit)
            im.load()
            im = _cover(im.convert("RGB"), size)
        except Exception:  # noqa: BLE001 - 坏图退化成色块，不能影响整张卡片
            im = None
    if im is None:
        im = Image.new("RGB", size, _hue_color(name))
        draw = ImageDraw.Draw(im)
        _text(draw, (size[0] / 2, size[1] / 2 - 2), name[:1] or "?",
              max(14, round(size[1] * 0.42)), "#ffffff", bold=True, anchor="mm")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return im, mask


# ------------------------------------------------------------------ layout
def _height(rep: dict) -> int:
    total = rep["totals"]
    add = rep["additions"]
    if not total["events"] and not add["groups"]:
        return 560
    h = 40 + 100 + 36 + 132 + 36
    if rep["people"]:
        h += 44 + min(len(rep["people"]), MEMBER_ROWS) * 56 + 32
    if rep["shows"]:
        n = min(len(rep["shows"]), SHOW_CARDS)
        h += 44 + (400 + 32 if n == 1 else 400 + 14 + 24 + 8 + 19 + 6 + 18 + 32)
    if add["groups"]:
        h += 44 + min(len(add["shows"]), ADD_ROWS) * 76 + 32
    return h + 56


def render(report: dict) -> bytes | None:
    if not available():
        return None

    total = report["totals"]
    add = report["additions"]
    height = _height(report)
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)

    # 顶部品牌条
    d.rectangle([0, 0, WIDTH // 2, 4], fill=ACCENT)
    d.rectangle([WIDTH // 2, 0, WIDTH, 4], fill=ACCENT2)

    # 标题
    d.rounded_rectangle([PAD, 42, PAD + 56, 98], radius=16, fill=ACCENT)
    _text(d, (PAD + 28, 70), "时", 28, "#ffffff", bold=True, anchor="mm")
    _text(d, (PAD + 76, 40), "观影日报", 34, TEXT, bold=True)
    sub = f'{report["label"]}'
    if total["events"]:
        sub += f' · {total["people"]} 人 · {total["shows"]} 部剧'
    _text(d, (PAD + 76, 84), sub, 21, DIM)

    if not total["events"] and not add["groups"]:
        _text(d, (PAD, 220), "这一天没有观看记录，", 34, TEXT, bold=True)
        _text(d, (PAD, 270), "也没有新内容入库。", 34, TEXT, bold=True)
        _text(d, (PAD, height - 72), "观影时间线 · 只读飞牛影视数据", 18, FAINT)
        return _png(img)

    # KPI：数值和单位排在同一基线，读起来是「4 小时 52 分」而不是上下两截
    y = 152
    labels = [
        ("观看记录", str(total["events"]), "条"),
        ("观看时长", _hm(total["minutes"]), ""),
        ("参与成员", str(total["people"]), "人"),
        ("新增入库", str(add["groups"]), f'部 · {add["episodes"]} 集'),
    ]
    box_w = (WIDTH - PAD * 2 - GAP * 3) / 4
    for i, (label, value, unit) in enumerate(labels):
        x = PAD + i * (box_w + GAP)
        _panel(d, [x, y, x + box_w, y + 132], radius=18)
        _text(d, (x + 22, y + 20), label, 19, DIM)
        avail = box_w - 44
        size = 40
        for size in (40, 36, 32, 30, 28, 26):
            need = _width(d, value, size, True) + ((8 + _width(d, unit, 18)) if unit else 0)
            if need <= avail:
                break
        baseline = y + 96
        _text(d, (x + 22, baseline), value, size, TEXT, bold=True, anchor="ls")
        if unit:
            _text(d, (x + 22 + _width(d, value, size, True) + 8, baseline), unit, 18, FAINT, anchor="ls")

    y += 132 + 36

    # 成员排名
    if report["people"]:
        title = "观影时长排名" if report["by_minutes"] else "成员观看"
        _text(d, (PAD, y), title, 26, TEXT, bold=True)
        _text(d, (WIDTH - PAD, y + 6), "按估算时长" if report["by_minutes"] else "按记录数",
              18, FAINT, anchor="ra")
        y += 44
        for i, p in enumerate(report["people"][:MEMBER_ROWS], 1):
            _text(d, (PAD + 6, y + 15), f"{i:02d}", 20, FAINT)
            _text(d, (PAD + 46, y + 10), _fit(d, p["name"], 24, 200), 24, TEXT)
            _bar(img, [PAD + 260, y + 20, PAD + 690, y + 32], p["pct"])
            _text(d, (WIDTH - PAD, y + 10), p["value_label"], 22, DIM, anchor="ra")
            y += 56
        y += 32

    # 剧集海报
    if report["shows"]:
        _text(d, (PAD, y), "剧集", 26, TEXT, bold=True)
        _text(d, (WIDTH - PAD, y + 6), f'共 {len(report["shows"])} 部', 18, FAINT, anchor="ra")
        y += 44
        card_w, poster_h = 300, 400
        n = min(len(report["shows"]), SHOW_CARDS)
        if n == 1:
            # 只有一部剧时不做海报网格：一张竖图 + 右侧信息，把整行填满
            s = report["shows"][0]
            im, mask = _poster(s["poster"], s["show"], (card_w, poster_h), 14)
            img.paste(im, (PAD, y), mask)
            tx = PAD + card_w + 32
            ty = y + 96  # 海报高 400，信息块垂直居中更好看
            _text(d, (tx, ty), _fit(d, s["show"], 34, WIDTH - PAD - tx, True),
                  34, TEXT, bold=True)
            line = f'{s["episodes"]} 集 · {s["people"]} 人'
            if s["minutes"]:
                line += f' · {_hm(s["minutes"])}'
            _text(d, (tx, ty + 58), _fit(d, line, 22, WIDTH - PAD - tx), 22, DIM)
            progress = []
            if s["seen"]:
                progress.append(f'看到 {s["seen"]}')
            if s["available"] and s["available"] != s["seen"]:
                progress.append(f'更新至 {s["available"]}')
            if progress:
                _text(d, (tx, ty + 98), _fit(d, " · ".join(progress), 19, WIDTH - PAD - tx),
                      19, FAINT)
            names = s.get("people_names") or []
            if names:
                who = "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
                line = f"{who} 一起看" if len(names) > 1 else f"{who} 在看"
                if s["library"]:
                    line += f' · {s["library"]}'
                _text(d, (tx, ty + 136), _fit(d, line, 19, WIDTH - PAD - tx), 19, DIM)
            elif s["library"]:
                _text(d, (tx, ty + 136), s["library"], 19, DIM)
            y += poster_h + 32
        else:
            start = (WIDTH - (n * card_w + (n - 1) * GAP)) // 2
            for i, s in enumerate(report["shows"][:SHOW_CARDS]):
                x = start + i * (card_w + GAP)
                im, mask = _poster(s["poster"], s["show"], (card_w, poster_h), 14)
                img.paste(im, (x, y), mask)
                _text(d, (x, y + poster_h + 14), _fit(d, s["show"], 24, card_w, True),
                      24, TEXT, bold=True)
                line = f'{s["episodes"]} 集 · {s["people"]} 人'
                if s["minutes"]:
                    line += f' · {_hm(s["minutes"])}'
                _text(d, (x, y + poster_h + 48), _fit(d, line, 18, card_w), 18, DIM)
                progress = []
                if s["seen"]:
                    progress.append(f'看到 {s["seen"]}')
                if s["available"] and s["available"] != s["seen"]:
                    progress.append(f'更新至 {s["available"]}')
                if progress:
                    _text(d, (x, y + poster_h + 72),
                          _fit(d, " · ".join(progress), 18, card_w), 18, FAINT)
            y += poster_h + 14 + 24 + 8 + 19 + 6 + 18 + 32

    # 新增入库
    if add["groups"]:
        _text(d, (PAD, y), "新增入库", 26, TEXT, bold=True)
        _text(d, (WIDTH - PAD, y + 6), f'{add["groups"]} 部 · {add["episodes"]} 集',
              18, FAINT, anchor="ra")
        y += 44
        for g in add["shows"][:ADD_ROWS]:
            im, mask = _poster(g["poster"], g["show"], (52, 72), 8)
            img.paste(im, (PAD, y + 2), mask)
            _text(d, (PAD + 68, y + 6), _fit(d, g["show"], 22, 560, True), 22, TEXT, bold=True)
            _text(d, (PAD + 68, y + 36), g["detail"], 18, DIM)
            if g.get("library"):
                _text(d, (WIDTH - PAD, y + 12), _fit(d, g["library"], 17, 240),
                      17, FAINT, anchor="ra")
            y += 76
        if add["groups"] > ADD_ROWS:
            _text(d, (PAD + 68, y + 2), f'… 另有 {add["groups"] - ADD_ROWS} 部', 18, FAINT)
        y += 32

    _text(d, (PAD, height - 56), "观影时间线 · 只读飞牛影视数据", 17, FAINT)
    _text(d, (WIDTH - PAD, height - 56), f'{total["events"]} 条记录 · {_hm(total["minutes"])}',
          17, FAINT, anchor="ra")
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()