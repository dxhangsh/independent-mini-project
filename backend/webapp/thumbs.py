# -*- coding: utf-8 -*-
"""缩略图生成：PIL 统一转 JPEG（heic 依赖 pillow-heif 解码）。

带一个小缓存（按路径+mtime+size+side 失效，文件一变即自动失效——ERR-009）。
生成失败返回 None，由路由层回退为占位图——单张坏图绝不允许拖垮复核页。
"""
import io
import threading
from pathlib import Path

from PIL import Image

_CACHE = {}            # key -> (mtime_ns, size, jpeg_bytes)
_CACHE_CAP = 300
_LOCK = threading.Lock()

THUMB_SIDE = 512
ALLOWED_SIDES = (256, 512, 1024, 1600)


def make_thumb(path, side=THUMB_SIDE):
    """返回 JPEG bytes；无法解码返回 None。side 控制最长边（供放大预览复用同一端点）。"""
    if side not in ALLOWED_SIDES:
        side = THUMB_SIDE
    path = Path(path)
    try:
        st = path.stat()
        key = (str(path).lower(), side)
    except OSError:
        return None
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) > side:
                s = side / max(w, h)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            data = buf.getvalue()
    except Exception:      # noqa: BLE001  解码失败（含 heic 缺依赖）→ 占位
        return None
    with _LOCK:
        if len(_CACHE) >= _CACHE_CAP:
            _CACHE.clear()             # 简单粗暴：满了就清空（MVP 够用）
        _CACHE[key] = (st.st_mtime_ns, st.st_size, data)
    return data


# 1x1 灰色 PNG 的占位图（hex），解码失败时回退
_PLACEHOLDER = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")


def make_thumb_or_placeholder(path, side=THUMB_SIDE):
    data = make_thumb(path, side)
    return data if data is not None else _PLACEHOLDER
