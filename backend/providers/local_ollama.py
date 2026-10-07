# -*- coding: utf-8 -*-
"""本地 Ollama Provider：自「商品图片批量重命名工具」`_gpu_fix/vlm_client.py` 移植。

关键结论（原项目实测得出，勿随意改动）：
  - 必须显式传 think 字段：省略该字段会触发 Ollama 0.34 的偶发连接重置。
  - num_predict 必须给足：qwen3-vl 模板会先生成思考链，配额被思考链吃光时
    response 为空，且没有哪个固定配额是安全的。
  - 空响应有兜底：改用 /api/chat 并预填一条 assistant 消息（JSON 开头），
    模型跳过思考链直接续写 JSON——实测取词 0.5~1.0s、归并 2.5s。
  - 单次请求约有 8%~10% 概率返回瞬时 500，重试 2 次即可完全消除。
  - 视觉编码是耗时主因：缩到 1024px 后视觉编码 token 降约 4 倍，输出不变。
  - 超时必须压到 120s：Ollama 偶发把请求挂死，600s 默认值会让一次卡死白等 10 分钟。

【PicNamer 改动】host 从模块级常量改为实例参数（设置页可配，改完即生效，无需重启进程
改环境变量）；其余协议逻辑逐字保留。
"""
import base64
import io
import json
import time
import urllib.error
import urllib.request

from engine.errors import VlmError

from .base import BaseProvider

DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_NUM_CTX = 8192
DEFAULT_NUM_PREDICT = 512
DEFAULT_RETRIES = 3
DEFAULT_MAX_SIDE = 1024
DEFAULT_KEEP_ALIVE = "30m"
RETRY_BACKOFF = 0.4


class LocalOllamaProvider(BaseProvider):
    name = "local_ollama"

    def __init__(self, host="http://127.0.0.1:11434", model=DEFAULT_MODEL,
                 num_ctx=DEFAULT_NUM_CTX, num_predict=DEFAULT_NUM_PREDICT,
                 max_retries=DEFAULT_RETRIES, timeout=120, max_side=DEFAULT_MAX_SIDE,
                 quality=88, keep_alive=DEFAULT_KEEP_ALIVE):
        self.host = (host or "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = model
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_side = max_side
        self.quality = quality
        self.keep_alive = keep_alive

    # ---- 内部：图片编码 ----

    def _encode(self, image_path):
        """读取并预缩放图片，返回 base64。缩放可把视觉编码 token 数降约 4 倍。"""
        if not self.max_side:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        try:
            from PIL import Image
        except ImportError:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        with Image.open(image_path) as im:
            w, h = im.size
            if max(w, h) > self.max_side:
                s = self.max_side / max(w, h)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=self.quality)
        return base64.b64encode(buf.getvalue()).decode()

    # ---- 对外接口 ----

    def classify(self, image_path, prompt, num_predict=None, prefill=None):
        """对单张图片提问，返回 dict(response, attempts, latency)。"""
        b64 = self._encode(image_path)
        n_pred = num_predict if num_predict is not None else self.num_predict
        t0 = time.time()
        resp, _thinking, attempts, diag = self._run(
            prompt, n_pred, b64, prefill, prefill_only=False)
        if not resp:
            raise VlmError(self._empty_msg(n_pred, prefill, diag))
        return {"response": resp, "attempts": attempts, "latency": time.time() - t0}

    def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
        """纯文本生成（不带图片），返回原始响应字符串。"""
        n_pred = num_predict if num_predict is not None else self.num_predict
        resp, _thinking, _attempts, diag = self._run(
            prompt, n_pred, None, prefill, prefill_only=prefill_only)
        if not resp:
            raise VlmError(self._empty_msg(n_pred, prefill, diag))
        return resp

    def ping(self):
        """检查 Ollama 服务是否可达、模型是否在列。"""
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as r:
                tags = json.loads(r.read()).get("models", [])
            names = [m.get("name", "") for m in tags]
            hit = any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                      for n in names)
            return {"ok": True, "detail": (f"服务可达，模型 {self.model} "
                                           + ("已在列表" if hit else "未拉取（ollama pull）"))}
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def loaded(self):
        """返回当前驻留显存的模型列表。"""
        try:
            with urllib.request.urlopen(f"{self.host}/api/ps", timeout=20) as r:
                return json.loads(r.read()).get("models", [])
        except Exception:
            return []

    # ---- 协议实现（逐字保留） ----

    def _empty_msg(self, n_pred, prefill, diag):
        return (f"空响应: 无协议产出内容 (num_predict={n_pred}, prefill={prefill!r})；"
                + "；".join(diag))

    def _run(self, prompt, n_pred, b64, prefill, prefill_only):
        if prefill_only:
            if not prefill:
                raise ValueError("prefill_only=True 需要非空 prefill")
            steps = ("prefill",)
        elif prefill:
            steps = ("think", "prefill")
        else:
            steps = ("think",)

        attempts = 0
        thinking = ""
        diag = []
        for step in steps:
            if step == "think":
                d, n = self._post("/api/generate", self._think_payload(prompt, n_pred, b64))
                attempts += n
                thinking = (d.get("thinking") or "").strip()
                resp = (d.get("response") or "").strip()
                diag.append(f"think(done_reason={d.get('done_reason')}, "
                            f"eval_count={d.get('eval_count')}, "
                            f"thinking={len(d.get('thinking') or '')}字符)")
            else:
                resp, thinking, n, note = self._prefill_chat(prompt, prefill, b64, n_pred)
                attempts += n
                diag.append(f"prefill({note})")
            if resp:
                return resp, thinking, attempts, diag
        return None, thinking, attempts, diag

    def _post(self, path, payload):
        body = json.dumps(payload).encode()
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.host}{path}", data=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read()), attempt
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(RETRY_BACKOFF * attempt)
        raise VlmError(f"重试 {self.max_retries} 次仍失败: {type(last_err).__name__}: {last_err}")

    def _think_payload(self, prompt, n_pred, b64):
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": n_pred,
            },
        }
        if b64 is not None:
            payload["images"] = [b64]
        return payload

    def _prefill_chat(self, prompt, prefill, b64, n_pred):
        """预填兜底：/api/chat 末尾加一条 assistant 消息，模型直接续写 JSON。"""
        user = {"role": "user", "content": prompt}
        if b64 is not None:
            user["images"] = [b64]
        d, attempts = self._post("/api/chat", {
            "model": self.model,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "messages": [user, {"role": "assistant", "content": prefill}],
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": n_pred,
            },
        })
        msg = d.get("message") or {}
        cont = (msg.get("content") or "").strip()
        thinking = (msg.get("thinking") or "").strip()
        note = (f"done_reason={d.get('done_reason')}, eval_count={d.get('eval_count')}, "
                f"续写={len(cont)}字符")
        return (prefill + cont) if cont else None, thinking, attempts, note
