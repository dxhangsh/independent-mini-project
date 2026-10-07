# -*- coding: utf-8 -*-
"""Provider 层：把「模型怎么调」从「命名怎么算」里解耦出来。

引擎（engine/）只依赖一个鸭子类型接口：
    classify(image_path, prompt, num_predict=None, prefill=None) -> {"response": str}
    generate(prompt, num_predict=None, prefill=None, prefill_only=False) -> str
    ping() -> dict        # 健康检查，返回 {"ok": bool, "detail": str}

Provider 不需要完整实现 prefill 语义（云端 API 没有思考链问题，忽略即可），
但必须保证：拿不到内容一律抛 `VlmError`，绝不返回空串——静默空响应会退回兜底词
并记成一次"成功"命名，污染结果。
"""
from .base import BaseProvider
from .cloud_openai import CloudOpenAIProvider
from .local_ollama import LocalOllamaProvider
from .serial import SerialProvider

__all__ = ["BaseProvider", "LocalOllamaProvider", "CloudOpenAIProvider", "SerialProvider"]
