# -*- coding: utf-8 -*-
"""Provider 抽象基类。"""
from engine.errors import VlmError


class BaseProvider:
    """模型调用提供方接口。子类必须实现 classify / generate / ping。"""

    name = "base"

    def classify(self, image_path, prompt, num_predict=None, prefill=None):
        """对单张图片提问，返回 dict(response=str, ...)。失败抛 VlmError。"""
        raise NotImplementedError

    def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
        """纯文本生成，返回响应字符串。失败抛 VlmError。"""
        raise NotImplementedError

    def ping(self):
        """健康检查。返回 {"ok": bool, "detail": str}，不抛异常。"""
        return {"ok": False, "detail": "not implemented"}

    @staticmethod
    def _empty(resp):
        """空响应契约：拿不到内容一律抛 VlmError，绝不返回空串。"""
        if not resp or not str(resp).strip():
            raise VlmError("空响应: 模型未产出内容")
        return resp
