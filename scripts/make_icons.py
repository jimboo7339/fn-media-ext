"""生成应用图标：static/favicon.ico 与 static/apple-touch-icon.png。

标记与 static/favicon.svg、templates/_macros.html 里的 mark() 保持一致：
24 画布上的「播放三角 + 三个事件节点的时间轴」。

改标记时三处一起改，然后重跑：

    python scripts/make_icons.py

只依赖 Pillow（应用本来就要装它）。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "static"

# 与 app.css 的 --accent / --accent-2 / --accent-3（深色主题）一致
ACCENT = ((0x6D, 0x8C, 0xFF), (0xA7, 0x8B, 0xFA), (0x22, 0xD3, 0xEE))
ANGLE = 140.0          # 与 CSS linear-gradient(140deg, ...) 对齐
MID_STOP = 0.55

# ---- 标记几何（24 画布坐标，与 SVG 里的数字一一对应）----
AXIS_Y = 20.4
AXIS_X0, AXIS_X1, AXIS_W = 4.6, 19.4, 1.8
AXIS_ALPHA = 0.38
DOTS = ((7.2, 0.50), (12.0, 0.75), (16.8, 1.00))
DOT_R = 1.55
TRI = ((8.4, 3.4), (20.0, 10.7), (8.4, 18.0))

# 把 24 画布的标记摆进 64 画布的图标
MARK_SCALE = 2.2
MARK_X, MARK_Y = 5.6, 4.8
CANVAS = 64.0


def gradient(size: int) -> Image.Image:
    """沿 140deg 的三段线性渐变（低分辨率算完再放大，线性渐变放大无损）。"""
    n = 64
    rad = math.radians(ANGLE)
    dx, dy = math.sin(rad), -math.cos(rad)
    corners = [x * dx + y * dy for x in (0, 1) for y in (0, 1)]
    lo, hi = min(corners), max(corners)

    small = Image.new("RGB", (n, n))
    px = small.load()
    for j in range(n):
        for i in range(n):
            t = ((i / (n - 1)) * dx + (j / (n - 1)) * dy - lo) / (hi - lo)
            t = min(1.0, max(0.0, t))
            if t <= MID_STOP:
                f, a, b = t / MID_STOP, ACCENT[0], ACCENT[1]
            else:
                f, a, b = (t - MID_STOP) / (1 - MID_STOP), ACCENT[1], ACCENT[2]
            px[i, j] = tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
    return small.resize((size, size), Image.BICUBIC)


def render(size: int, *, rounded: bool, ss: int = 4) -> Image.Image:
    full = size * ss
    unit = full / CANVAS

    def point(x: float, y: float) -> tuple[float, float]:
        return ((MARK_X + x * MARK_SCALE) * unit, (MARK_Y + y * MARK_SCALE) * unit)

    def span(v: float) -> float:
        return v * MARK_SCALE * unit

    tile = gradient(full).convert("RGBA")
    if rounded:
        mask = Image.new("L", (full, full), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, full - 1, full - 1], radius=round(full * 0.30), fill=255
        )
        cut = Image.new("RGBA", (full, full), (0, 0, 0, 0))
        cut.paste(tile, (0, 0), mask)
        tile = cut

    def layer(mask: Image.Image, alpha: float) -> Image.Image:
        out = Image.new("RGBA", (full, full), (255, 255, 255, 0))
        out.putalpha(mask.point(lambda v: round(v * alpha)))
        return out

    # 时间轴：圆头粗线
    axis = Image.new("L", (full, full), 0)
    draw = ImageDraw.Draw(axis)
    ax0, ay = point(AXIS_X0, AXIS_Y)
    ax1, _ = point(AXIS_X1, AXIS_Y)
    half = span(AXIS_W) / 2
    draw.line([ax0, ay, ax1, ay], fill=255, width=max(1, round(span(AXIS_W))))
    for cx in (ax0, ax1):
        draw.ellipse([cx - half, ay - half, cx + half, ay + half], fill=255)
    tile = Image.alpha_composite(tile, layer(axis, AXIS_ALPHA))

    # 事件节点
    for cx, alpha in DOTS:
        px_, py_ = point(cx, AXIS_Y)
        r = span(DOT_R)
        dot = Image.new("L", (full, full), 0)
        ImageDraw.Draw(dot).ellipse([px_ - r, py_ - r, px_ + r, py_ + r], fill=255)
        tile = Image.alpha_composite(tile, layer(dot, alpha))

    # 播放三角
    tri = Image.new("L", (full, full), 0)
    ImageDraw.Draw(tri).polygon([point(*p) for p in TRI], fill=255)
    tile = Image.alpha_composite(tile, layer(tri, 1.0))

    return tile.resize((size, size), Image.LANCZOS)


def main() -> int:
    ico = render(256, rounded=True)
    ico.save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    # iOS 会自己切圆角，这里给全出血的方图
    render(180, rounded=False).save(STATIC / "apple-touch-icon.png")

    for name in ("favicon.ico", "apple-touch-icon.png"):
        p = STATIC / name
        print(f"{p.relative_to(STATIC.parent)}  {p.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
