# -*- coding: utf-8 -*-
"""串行包装：所有模型调用全局排队。

背景（原项目实测教训）：模型服务须单进程独占 GPU，多进程/多线程并发抢占会导致
生成质量漂移。原项目用独立网关进程统一串行；PicNamer 是单进程 Web 应用，
进程内 `threading.Lock` 即可达到同一效果，且省去一个常驻服务。

改 Provider 类型（本地 ↔ 云端）时通过 `swap()` 原子替换内部实例，排队锁保持不变。
"""
import threading


class SerialProvider:
    name = "serial"

    def __init__(self, provider):
        self._lock = threading.Lock()
        self._provider = provider

    @property
    def provider(self):
        return self._provider

    def swap(self, provider):
        """替换底层 Provider（设置页切换时调用）。"""
        with self._lock:
            old = self._provider
            self._provider = provider
            return old

    def classify(self, image_path, prompt, num_predict=None, prefill=None):
        with self._lock:
            return self._provider.classify(image_path, prompt,
                                           num_predict=num_predict, prefill=prefill)

    def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
        with self._lock:
            return self._provider.generate(prompt, num_predict=num_predict,
                                           prefill=prefill, prefill_only=prefill_only)

    def ping(self):
        # ping 不排队：健康检查不该被长任务卡住
        return self._provider.ping()
