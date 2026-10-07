# -*- coding: utf-8 -*-
"""多目录批量处理：单活动批次 + JSON 持久化 + 断点续跑（ADR-008）。

设计取舍
    - 同一时刻只有一个活动批次：个人工具的常见形态，UI 与心智都最简单；
    - 批次状态在每个目录完成/状态迁移时落盘一次（`out/batch.json`），
      服务重启后可续跑；
    - 「已完成目录不重复处理」：resume 与"同目录集合重新提交"都跳过 done 目录；
    - 图片级进度只更新内存（高频），目录级状态变更才落盘（低频）。
"""
import json
import threading
import uuid
from pathlib import Path

from webapp.runner import run_folder_pipeline


class BatchManager:
    def __init__(self, state, path):
        self.state = state
        self.path = Path(path)
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.worker = None
        self.data = self._load()

    # ---- 持久化 ----

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ---- 对外查询 ----

    def public(self):
        """返回对外快照（深拷贝，避免前端拿到中间态引用）。"""
        with self.lock:
            if not self.data:
                return None
            d = json.loads(json.dumps(self.data, ensure_ascii=False))
            d["exists"] = True
            d["running"] = self.worker is not None and self.worker.is_alive()
            return d

    # ---- 生命周期 ----

    def submit(self, folders, product_word=""):
        """创建批次；已有「同目录集合」的批次则续跑。

        断点续跑与内容刷新的分工（ADR-010）：
          - `resume()`：纯续跑，done 目录一律跳过（服务中断场景）；
          - `submit()`：done 目录先比对**内容签名**（图片名+大小+mtime_ns），目录
            内容有变动则自动重新处理——修掉"改了图还列老文件"的过期快照问题。
        """
        norm = sorted(str(Path(f).resolve()) for f in folders)
        with self.lock:
            if self.data and sorted(x["path"] for x in self.data["folders"]) == norm:
                for fd in self.data["folders"]:
                    if fd["status"] != "done":
                        fd["status"] = "pending"
                    elif _folder_sig(fd["path"]) != fd.get("sig"):
                        # 目录内容变了：旧快照作废，重新处理
                        fd["status"] = "pending"
                        fd["plan_key"] = ""
                        fd["error"] = ""
                        fd["done"] = fd["total"] = 0
                        fd["current"] = ""
            else:
                self.data = {
                    "id": uuid.uuid4().hex[:10],
                    "product_word": product_word or "",
                    "folders": [{"path": str(Path(f).resolve()), "status": "pending",
                                 "plan_key": "", "error": "", "review": 0,
                                 "done": 0, "total": 0, "current": "", "sig": ""}
                                for f in folders],
                }
            self._start_locked()

    def resume(self):
        """续跑：pending/running/error 一律重置为 pending 重新处理，done 跳过。"""
        with self.lock:
            if not self.data:
                raise ValueError("没有可续跑的批次")
            for fd in self.data["folders"]:
                if fd["status"] != "done":
                    fd["status"] = "pending"
                    fd["error"] = ""
            self._start_locked()

    def cancel_run(self):
        """请求停止：当前目录处理完后不再继续下一个。"""
        self.cancel.set()

    def rescan(self, path):
        """人工指定单目录重扫（前端"重新扫描"按钮）：作废旧快照，重新处理。

        批次处理中不允许重扫（worker 循环已启动，中途插入的 pending 不会被
        拾起，MVP 不做动态队列）；批次不存在或目录不在批次中也明确报错。
        """
        rp = str(Path(path).resolve())
        with self.lock:
            if not self.data:
                raise ValueError("还没有任何批次，请先扫描")
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("批次正在处理中，请等它结束再重扫")
            target = next((fd for fd in self.data["folders"] if fd["path"] == rp), None)
            if target is None:
                raise ValueError("该目录不在当前批次中（请先在下方输入框提交它）")
            target["status"] = "pending"
            target["plan_key"] = ""
            target["error"] = ""
            target["done"] = target["total"] = 0
            target["current"] = ""
            self._start_locked()

    def _start_locked(self):
        self.cancel.clear()
        t = threading.Thread(target=self._run, daemon=True)
        self.worker = t
        t.start()

    # ---- worker ----

    def _run(self):
        data = self.data
        for fd in data["folders"]:
            if self.cancel.is_set():
                break
            if fd["status"] != "pending":
                continue
            with self.lock:
                fd["status"] = "running"
                self._save()
            self._process(fd)
            with self.lock:
                self._save()
        self.worker = None

    def _process(self, fd):
        def on_image(i, total, path):
            fd["done"] = i
            fd["total"] = total
            fd["current"] = Path(path).name

        def on_progress(line):
            fd.setdefault("log", [])
            fd["log"].append(line)
            if len(fd["log"]) > 50:
                del fd["log"][:len(fd["log"]) - 50]

        try:
            fd["sig"] = _folder_sig(fd["path"])   # 快照对应的内容签名，供重提交时比对
            d, review, total = run_folder_pipeline(
                self.state.serial, fd["path"],
                product_word=self.data.get("product_word") or None,
                progress=on_progress, on_image=on_image)
            fd["plan_key"] = self.state.save_plan(d)
            fd["review"] = review
            fd["total"] = total
            self.state.allow_root(fd["path"])
            fd["status"] = "done"
        except Exception as e:      # noqa: BLE001  单目录失败不拖垮整批
            fd["status"] = "error"
            fd["error"] = f"{type(e).__name__}: {e}"


def _folder_sig(path):
    """目录内容签名：图片名+大小+mtime_ns 的 JSON（与 scan_folder 同一图片集合口径）。

    变动检测只要"有变化"这一个比特的信息，不需要哈希文件内容——本地盘枚举
    千张图也在毫秒级。
    """
    from engine.pipeline import scan_folder
    out = []
    try:
        for p in scan_folder(path):
            st = p.stat()
            out.append([p.name, st.st_size, st.st_mtime_ns])
    except OSError:
        return ""
    return json.dumps(out, ensure_ascii=False)
