# -*- coding: utf-8 -*-
"""引擎层：自「商品图片批量重命名工具」项目移植的三段式命名流水线。

出处与致谢：parts / merge / naming / pipeline / apply 五个模块移植自前期 CLI
项目「商品图片批量重命名工具」（本地 Ollama + qwen3-vl，245 个测试用例打磨）。
移植改动均有注释标明：
  - imports 收敛为包内相对导入；VlmError 统一来自 engine.errors；
  - pipeline.run_folder 新增 on_image 进度钩子与 Row.skip 标记（Web 复核需要）；
  - apply.plan_ops 尊重 skip 标记（用户复核时跳过的行不改名）。
提示词与净化/安全逻辑逐字保留——它们是多轮 A/B 实测与真实落盘教训的结晶。
"""
