# -*- coding: utf-8 -*-
"""生成 PWA 图标：靛蓝→青色渐变圆角方块 + 白色价签图形。

运行一次即可（产物已入库）；改设计后重跑：
    ..\\.venv\\Scripts\\python.exe tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "frontend" / "icons"
SIZE = 512
TOP = (99, 102, 241)      # indigo-500
BOTTOM = (34, 211, 238)   # cyan-400


def gradient_bg(size, rounded=True, radius_ratio=0.22):
    """渐变背景；rounded=True 时裁圆角（普通图标），False 时满幅（maskable/苹果）。"""
    g = Image.linear_gradient("L").resize((size, size))     # 上 0 → 下 255
    top = Image.new("RGB", (size, size), TOP)
    bot = Image.new("RGB", (size, size), BOTTOM)
    grad = Image.composite(bot, top, g).convert("RGBA")
    if not rounded:
        return grad
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1],
                        radius=int(size * radius_ratio), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def draw_tag(canvas, scale=1.0, center=None):
    """画一个白色价签：圆角矩形 + 左端圆孔，旋转 -25°。"""
    size = canvas.size[0]
    cx, cy = center or (size * 0.54, size * 0.52)
    tw, th = int(size * 0.56 * scale), int(size * 0.30 * scale)
    tag = Image.new("RGBA", (tw + 80, th + 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(tag)
    d.rounded_rectangle([40, 40, 40 + tw, 40 + th], radius=int(th * 0.3), fill=(255, 255, 255, 255))
    # 左端圆孔（画透明擦除）
    r = int(th * 0.22)
    hx, hy = 40 + int(tw * 0.16), 40 + th // 2
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(0, 0, 0, 0))
    tag = tag.rotate(-25, expand=True, resample=Image.BICUBIC)
    px, py = int(cx - tag.size[0] / 2), int(cy - tag.size[1] / 2)
    canvas.alpha_composite(tag, (px, py))
    return canvas


def save(img, name, px):
    OUT.mkdir(parents=True, exist_ok=True)
    img.resize((px, px), Image.LANCZOS).save(OUT / name, "PNG")
    print("icon:", name)


def main():
    # 普通图标（圆角渐变 + 价签）
    base = draw_tag(gradient_bg(SIZE))
    save(base, "icon-512.png", 512)
    save(base, "icon-192.png", 192)
    save(base, "favicon-32.png", 32)
    # maskable：满幅背景，图形缩进安全区（60%）
    mask = draw_tag(gradient_bg(SIZE, rounded=False), scale=0.72)
    save(mask, "icon-512-maskable.png", 512)
    # apple-touch-icon：满幅（苹果自带圆角遮罩）
    apple = draw_tag(gradient_bg(SIZE, rounded=False), scale=0.86)
    save(apple, "apple-touch-icon-180.png", 180)


if __name__ == "__main__":
    main()
