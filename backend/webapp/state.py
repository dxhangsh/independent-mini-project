# -*- coding: utf-8 -*-
"""Web 层状态：设置、Provider 单例、任务注册表、方案存储、路径白名单。

MVP 设计取舍（与阶段一范围对齐）：
  - 任务/方案存内存 + JSON 落盘（out/plans/），重启后方案仍可查（journal 在磁盘，
    回滚不依赖内存态）；
  - 模型调用全局串行（SerialProvider），替代原项目的独立网关进程；
  - 缩略图只对「本会话扫描过的目录」（allowed_roots）放行，防止任意路径读图。
"""
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]        # backend/
PROJECT_ROOT = BASE_DIR.parent                        # independent-mini-project/
OUT_DIR = Path(__file__).resolve().parents[2] / "out"
CONFIG_PATH = OUT_DIR / "config.json"
JOURNAL_PATH = OUT_DIR / "journal.jsonl"
PLANS_DIR = OUT_DIR / "plans"

DEFAULT_SETTINGS = {
    "provider": "local_ollama",        # local_ollama | cloud_openai
    "ollama_host": "http://127.0.0.1:11434",
    "model": "qwen3-vl:8b",
    "num_ctx": 8192,
    "max_side": 1024,
    "cloud_base_url": "",
    "cloud_api_key": "",
    "cloud_model": "qwen-vl-plus",
    "default_product_word": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return s


def save_settings(s):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def build_serial_provider(settings):
    """按设置构建串行 Provider。import 放函数内，避免循环依赖并便于测试替换。"""
    from providers.local_ollama import LocalOllamaProvider
    from providers.cloud_openai import CloudOpenAIProvider
    from providers.serial import SerialProvider

    if settings.get("provider") == "cloud_openai":
        inner = CloudOpenAIProvider(
            base_url=settings.get("cloud_base_url", ""),
            api_key=settings.get("cloud_api_key", ""),
            model=settings.get("cloud_model") or "qwen-vl-plus",
            max_side=int(settings.get("max_side") or 1024))
    else:
        inner = LocalOllamaProvider(
            host=settings.get("ollama_host") or "http://127.0.0.1:11434",
            model=settings.get("model") or "qwen3-vl:8b",
            num_ctx=int(settings.get("num_ctx") or 8192),
            max_side=int(settings.get("max_side") or 1024))
    return SerialProvider(inner)


@dataclass
class Job:
    id: str
    folder: str
    status: str = "running"            # running | done | error
    stage: str = "启动中"
    done: int = 0
    total: int = 0
    current: str = ""
    messages: list = field(default_factory=list)
    plan_key: str = ""
    error: str = ""
    product_word: str = ""
    created: float = field(default_factory=time.time)

    def log(self, line):
        self.messages.append(line)
        if len(self.messages) > 200:   # 只留最近 200 条，防长任务撑爆内存
            del self.messages[:len(self.messages) - 200]

    def to_dict(self):
        return {
            "id": self.id, "folder": self.folder, "status": self.status,
            "stage": self.stage, "done": self.done, "total": self.total,
            "current": self.current, "messages": self.messages,
            "plan_key": self.plan_key, "error": self.error,
        }


# ---- FolderPlan <-> dict 互转（方案要存 JSON、供前端复核编辑） ----

def plan_to_dict(plan):
    return {
        "folder": plan.folder,
        "canon": list(plan.canon),
        "issues": [{"kind": it.kind, "detail": it.detail} for it in plan.issues],
        "rows": [{
            "src": r.src, "raw_part": r.raw_part, "part": r.part, "info": r.info,
            "kind": r.kind, "conf": r.conf, "stem": r.stem, "final": r.final,
            "needs_review": r.needs_review, "exif_ordinal": r.exif_ordinal,
            "skip": r.skip,
        } for r in plan.rows],
    }


def dict_to_plan(d):
    from engine.pipeline import FolderPlan, Row
    from engine.naming import Issue

    plan = FolderPlan(folder=d.get("folder", ""))
    plan.canon = list(d.get("canon", []))
    plan.issues = [Issue(it.get("kind", ""), it.get("detail", ""))
                   for it in d.get("issues", [])]
    for r in d.get("rows", []):
        plan.rows.append(Row(
            src=r["src"], raw_part=r.get("raw_part", ""), part=r.get("part", ""),
            info=r.get("info", ""), kind=r.get("kind", "single"),
            conf=r.get("conf", 0.0), stem=r.get("stem", ""),
            final=r.get("final", ""), needs_review=r.get("needs_review", False),
            exif_ordinal=r.get("exif_ordinal", False), skip=r.get("skip", False)))
    return plan


class AppState:
    """进程级单例：FastAPI 依赖注入的根对象。"""

    def __init__(self):
        self.settings = load_settings()
        self.serial = build_serial_provider(self.settings)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.plans = {}                    # plan_key -> plan dict
        self.plans_lock = threading.Lock()
        self.allowed_roots = set()         # 已扫描目录的白名单（缩略图放行依据）
        self.roots_lock = threading.Lock()

    # ---- settings ----

    def update_settings(self, patch: dict):
        """应用设置补丁并原子换 Provider。api_key 为空/占位时保留旧值。"""
        s = dict(self.settings)
        for k in DEFAULT_SETTINGS:
            if k in patch and patch[k] is not None:
                v = patch[k]
                if k == "cloud_api_key" and (not v or str(v).startswith("****")):
                    continue               # 前端回显的是掩码，别把掩码存回去
                s[k] = v
        self.settings = s
        save_settings(s)
        # 人工实测发现的回归（2026-10-02）：build_serial_provider 已返回 SerialProvider 包装，
        # 若整体 swap 会形成「Serial 套 Serial」，health 的 .provider.name 读成 "serial"，
        # 前端徽标误显示「云端」。这里只 swap 内部 provider，保持 serial.provider 始终是真实 Provider。
        self.serial.swap(build_serial_provider(s).provider)
        return s

    def masked_settings(self):
        s = dict(self.settings)
        key = s.get("cloud_api_key") or ""
        if key:
            s["cloud_api_key"] = "****" + key[-4:] if len(key) > 4 else "****"
        return s

    # ---- jobs ----

    def new_job(self, folder):
        job = Job(id=uuid.uuid4().hex[:12], folder=folder)
        with self.jobs_lock:
            self.jobs[job.id] = job
        return job

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    # ---- plans ----

    def save_plan(self, plan_dict):
        key = uuid.uuid4().hex[:12]
        # 快照生成时间：复核页展示，避免"目录已改、页面还是旧快照"的困惑（试用者反馈）
        plan_dict.setdefault("created",
                             datetime.now().astimezone().isoformat(timespec="seconds"))
        with self.plans_lock:
            self.plans[key] = plan_dict
        try:
            PLANS_DIR.mkdir(parents=True, exist_ok=True)
            (PLANS_DIR / f"{key}.json").write_text(
                json.dumps(plan_dict, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass                          # 落盘失败不阻断流程（内存里仍可用）
        return key

    def get_plan(self, key):
        with self.plans_lock:
            if key in self.plans:
                return self.plans[key]
        # 重启后从磁盘找
        try:
            p = PLANS_DIR / f"{key}.json"
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                with self.plans_lock:
                    self.plans[key] = d
                return d
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def update_plan_review(self, key, rows_patch):
        """rows_patch: [{final, skip}, ...] 按下标全量覆盖。"""
        with self.plans_lock:
            d = self.plans.get(key)
        if d is None:
            d = self.get_plan(key)
        if d is None:
            return None
        rows = d.get("rows", [])
        if len(rows_patch) != len(rows):
            return None
        for row, patch in zip(rows, rows_patch):
            row["final"] = str(patch.get("final", row.get("final", "")))
            row["skip"] = bool(patch.get("skip", False))
        with self.plans_lock:
            self.plans[key] = d
        try:
            (PLANS_DIR / f"{key}.json").write_text(
                json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return d

    # ---- path safety ----

    def allow_root(self, folder):
        with self.roots_lock:
            self.allowed_roots.add(str(Path(folder).resolve()).lower())

    def path_allowed(self, path):
        """缩略图放行判定：路径必须落在本会话扫描过的目录白名单内。"""
        try:
            rp = str(Path(path).resolve()).lower()
        except OSError:
            return False
        with self.roots_lock:
            return any(rp.startswith(root.rstrip("\\/") + "\\")
                       or rp.startswith(root.rstrip("\\/") + "/")
                       for root in self.allowed_roots)
