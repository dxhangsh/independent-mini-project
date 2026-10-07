# -*- coding: utf-8 -*-
"""云端视觉 API Provider（OpenAI Chat Completions 兼容协议）。

定位：本地 Ollama 不可用时的备选（如演示机没有 GPU）。任何 OpenAI 兼容端点
（OpenAI / 阿里云百炼 / 硅基流动 / DeepSeek 视觉端点等）均可接入。

与本地 Provider 的差异：
  - 没有思考链问题，prefill / prefill_only 参数忽略即可；
  - API Key 属敏感信息：只存本地配置文件（out/config.json，已 gitignore），
    接口回显时打码，绝不写进代码或日志。

安全提示：切换到云端 Provider 意味着图片会离开本机。前端设置页在切换时
必须显著提示这一点，由用户显式确认。
"""
import base64
import io
import json
import time
import urllib.error
import urllib.request

from engine.errors import VlmError

from .base import BaseProvider

DEFAULT_CLOUD_MODEL = "qwen-vl-plus"
DEFAULT_TIMEOUT = 120


class CloudOpenAIProvider(BaseProvider):
    name = "cloud_openai"

    def __init__(self, base_url, api_key, model=DEFAULT_CLOUD_MODEL,
                 num_predict=2048, timeout=DEFAULT_TIMEOUT, max_side=1024, quality=88):
        self.base_url = (base_url or "").rstrip("/")
        if self.base_url and not self.base_url.endswith("/chat/completions"):
            self.base_url += "/chat/completions"
        self.api_key = api_key or ""
        self.model = model
        self.num_predict = num_predict
        self.timeout = timeout
        self.max_side = max_side
        self.quality = quality

    # ---- 图片编码（与本地 Provider 同一预缩放策略） ----

    def _encode_data_uri(self, image_path):
        if not self.max_side:
            with open(image_path, "rb") as f:
                raw = f.read()
            return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
            if max(w, h) > self.max_side:
                s = self.max_side / max(w, h)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=self.quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    # ---- 请求 ----

    def _chat(self, messages, n_pred):
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": n_pred,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            self.base_url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise VlmError(f"云端 API HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise VlmError(f"云端 API 连接失败: {type(e).__name__}: {e}") from e
        try:
            return d["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise VlmError(f"云端 API 响应结构异常: {e}") from e

    # ---- 对外接口 ----

    def classify(self, image_path, prompt, num_predict=None, prefill=None):
        n_pred = num_predict if num_predict is not None else self.num_predict
        t0 = time.time()
        content = self._chat([{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": self._encode_data_uri(image_path)}},
            ],
        }], n_pred)
        return {"response": self._empty(content), "latency": time.time() - t0}

    def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
        n_pred = num_predict if num_predict is not None else self.num_predict
        content = self._chat([{"role": "user", "content": prompt}], n_pred)
        return self._empty(content)

    def ping(self):
        if not self.base_url or not self.api_key:
            return {"ok": False, "detail": "未配置 base_url 或 API Key"}
        try:
            # chat/completions 发一条 1 token 的最小请求验证连通性与鉴权
            self._chat([{"role": "user", "content": "ping"}], 8)
            return {"ok": True, "detail": f"云端 API 可达（{self.model}）"}
        except VlmError as e:
            return {"ok": False, "detail": str(e)}
