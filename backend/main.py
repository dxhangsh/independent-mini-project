# -*- coding: utf-8 -*-
"""PicNamer 后端入口。

启动（在 backend/ 目录下）：
    uvicorn main:app --port 8765
浏览器打开 http://127.0.0.1:8765
（端口取 8765：8000 已被本机另一个订阅项目长期占用，见 docs/决策记录.md ADR-006）

手机/局域网安装 PWA 需 HTTPS：运行 `run.ps1 -Lan`（自动生成自签证书，
见 docs/决策记录.md ADR-009）。
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from webapp.routes import router

app = FastAPI(title="PicNamer · AI 商品图片命名助手", version="0.3.0")
app.include_router(router)

# 前端静态资源（原生 HTML/CSS/JS，无构建链）
_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@app.get("/manifest.webmanifest", include_in_schema=False)
def webmanifest():
    """PWA manifest 用正确的 Content-Type 提供（Windows mimetypes 未必认识 .webmanifest）。"""
    return FileResponse(_FRONTEND / "manifest.webmanifest",
                        media_type="application/manifest+json")


if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
