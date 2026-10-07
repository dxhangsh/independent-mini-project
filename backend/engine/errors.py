# -*- coding: utf-8 -*-
"""引擎层公共异常。

原项目里 `VlmError` 定义在 `_gpu_fix/vlm_client.py`，且因命名空间包被双份加载，
`pipeline._is_vlm_error` 被迫扫描 sys.modules 兜底。PicNamer 是单包结构，异常类
只有一份，直接集中定义、各处 import 同一个类即可。
"""


class VlmError(RuntimeError):
    """模型调用失败：网络故障、重试耗尽、空响应等。"""
