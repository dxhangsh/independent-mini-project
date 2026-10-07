# -*- coding: utf-8 -*-
"""API 路由。MVP 端点一览：

  GET  /api/health                 健康检查（当前 Provider + 连通性）
  GET  /api/settings               查看设置（Key 掩码）
  POST /api/settings               更新设置（切换 Provider / 模型 / 云端 Key）
  POST /api/scan                   创建命名任务（后台线程跑流水线）
  GET  /api/jobs/{job_id}          轮询任务进度
  GET  /api/plans/{plan_key}       取复核数据
  POST /api/plans/{plan_key}/review  提交复核编辑（改名/跳过）
  POST /api/plans/{plan_key}/apply   预检并落盘
  GET  /api/batches                历史批次列表（journal 聚合）
  POST /api/rollback               按批次号回滚
  GET  /api/thumb?path=...         缩略图（仅白名单目录）
"""
import csv
import io
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from engine import apply as apply_mod
from engine.naming import Issue
from engine.pipeline import heic_supported
from webapp.batch import BatchManager
from webapp.runner import run_folder_pipeline
from webapp.state import AppState, OUT_DIR, JOURNAL_PATH
from webapp.thumbs import make_thumb_or_placeholder

router = APIRouter(prefix="/api")
state = AppState()
batch_manager = BatchManager(state, OUT_DIR / "batch.json")


# ---- 请求/响应模型 ----

class ScanReq(BaseModel):
    folder: str = Field(min_length=1)
    product_word: str = ""


class ReviewReq(BaseModel):
    rows: list                       # [{final: str, skip: bool}, ...]


class ApplyReq(BaseModel):
    accept_review: bool = False


class RollbackReq(BaseModel):
    plan_id: str = Field(min_length=1)


class SettingsReq(BaseModel):
    provider: str | None = None
    ollama_host: str | None = None
    model: str | None = None
    num_ctx: int | None = None
    max_side: int | None = None
    cloud_base_url: str | None = None
    cloud_api_key: str | None = None
    cloud_model: str | None = None
    default_product_word: str | None = None


class BatchReq(BaseModel):
    folders: list[str] = Field(min_length=1)
    product_word: str = ""


class RescanReq(BaseModel):
    path: str = Field(min_length=1)


# ---- 健康与设置 ----

@router.get("/health")
def health():
    ping = state.serial.ping()
    return {
        "ok": True,
        "provider": state.serial.provider.name,
        "model": (state.settings.get("model") if state.settings.get("provider") == "local_ollama"
                  else state.settings.get("cloud_model")),
        "ping": ping,
        "heic": heic_supported(),
    }


@router.get("/settings")
def get_settings():
    return state.masked_settings()


@router.post("/settings")
def post_settings(req: SettingsReq):
    s = state.update_settings(req.model_dump(exclude_none=True))
    return {"ok": True, "settings": state.masked_settings(), "provider": s["provider"]}


# ---- 扫描与任务 ----

def _require_service():
    """任务启动前预检 AI 服务（2.4）：不通就明确拒绝，不让用户等一堆兜底词。"""
    ping = state.serial.ping()
    if not ping.get("ok"):
        raise HTTPException(
            503, f"AI 服务未连接：{ping.get('detail', '')}。"
                 f"请启动 Ollama 并拉取模型，或在「设置」页切换到云端 API 并测试连接。")
    return ping


def _run_job(job):
    """后台线程：跑完整目录流水线。进度经 job 可见，异常不逃逸。"""
    def on_image(done, total, path):
        job.done = done
        job.total = total
        job.current = Path(path).name

    try:
        job.stage = "AI 逐图识别中"
        d, review, total = run_folder_pipeline(
            state.serial, job.folder,
            product_word=job.product_word or None,
            progress=job.log, on_image=on_image)
        job.stage = "生成完成"
        job.plan_key = state.save_plan(d)
        state.allow_root(job.folder)
        job.status = "done"
        job.log(f"完成：{total} 张建议名，待复核 {review} 张")
    except Exception as e:      # noqa: BLE001  任务线程吞异常，状态上报
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.log(f"[x] 任务失败: {job.error}")


@router.post("/scan")
def scan(req: ScanReq):
    folder = Path(req.folder)
    if not folder.is_dir():
        raise HTTPException(400, f"目录不存在或不是目录：{req.folder}")
    _require_service()
    job = state.new_job(str(folder))
    job.product_word = req.product_word.strip()
    job.stage = "排队中"
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return {"job_id": job.id}


# ---- 多目录批量（ADR-008：单活动批次 + 断点续跑） ----

@router.post("/batch")
def batch_submit(req: BatchReq):
    invalid = [f for f in req.folders if not Path(f).is_dir()]
    if invalid:
        raise HTTPException(400, f"以下目录不存在或不是目录：{'；'.join(invalid)}")
    _require_service()
    batch_manager.submit(req.folders, req.product_word.strip())
    return batch_manager.public()


@router.get("/batch")
def batch_get():
    d = batch_manager.public()
    return d if d is not None else {"exists": False}


@router.post("/batch/resume")
def batch_resume():
    _require_service()
    try:
        batch_manager.resume()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return batch_manager.public()


@router.post("/batch/cancel")
def batch_cancel():
    batch_manager.cancel_run()
    return batch_manager.public()


@router.post("/batch/rescan")
def batch_rescan(req: RescanReq):
    """人工重扫单个目录（目录内容变动后即时刷新的就近入口，ADR-010）。"""
    try:
        batch_manager.rescan(req.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return batch_manager.public()


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = state.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


# ---- 复核与落盘 ----

@router.get("/plans/{plan_key}")
def get_plan(plan_key: str):
    d = state.get_plan(plan_key)
    if d is None:
        raise HTTPException(404, "方案不存在")
    review = sum(1 for r in d["rows"] if r.get("needs_review"))
    skipped = sum(1 for r in d["rows"] if r.get("skip"))
    return {**d, "review_count": review, "skip_count": skipped, "total": len(d["rows"])}


@router.post("/plans/{plan_key}/review")
def review_plan(plan_key: str, req: ReviewReq):
    d = state.update_plan_review(plan_key, req.rows)
    if d is None:
        raise HTTPException(404, "方案不存在或行数不匹配")
    return {"ok": True}


@router.post("/plans/{plan_key}/apply")
def apply_plan(plan_key: str, req: ApplyReq):
    d = state.get_plan(plan_key)
    if d is None:
        raise HTTPException(404, "方案不存在")
    plan = _dict_to_plan(d)
    # I1 冲突标记重算：方案里存的 stem_collision 是流水线当时算的。复核阶段人工
    # 改名后冲突可能已解开，不能拿旧标记继续拦；反过来人工编辑也可能制造新的重复
    # （由 apply_plan 的预检 dst_duplicate 兜底）。判定口径：同一个 stem 上有多个
    # 不同部件、且这些行的 final 仍是流水线的自动产物（stem.ext / stem-NN.ext），
    # 说明"静默序号"仍在，需要人工改开或显式放行；只要有一行被人工改名，即视为已解。
    stored_collision = any(it.kind == "stem_collision" for it in plan.issues)
    plan.issues = [it for it in plan.issues if it.kind != "stem_collision"]
    if stored_collision and _silent_numbering_remains(plan):
        plan.issues.append(Issue(
            "stem_collision", "仍有不同部件共用同名（自动序号未人工确认），请改名或放行"))
    res = apply_mod.apply_plan(plan, JOURNAL_PATH, accept_review=req.accept_review)
    return {
        "plan_id": res.plan_id,
        "renamed": res.renamed, "noop": res.noop,
        "rolled_back": res.rolled_back, "aborted": res.aborted,
        "accepted_review": res.accepted_review,
        "blocked": [{"kind": b.kind, "detail": b.detail} for b in res.blocked],
        "conflicts": [{"src": c.src, "dst": c.dst, "reason": c.reason}
                      for c in res.conflicts],
        "errors": res.errors,
    }


def _dict_to_plan(d):
    from webapp.state import dict_to_plan
    return dict_to_plan(d)


_AUTO_ORDINAL = re.compile(r"-\d{2}$")


def _is_auto_final(row):
    """该行的 final 是否仍是流水线的自动产物：`stem.ext` 或 `stem-NN.ext`。

    人工改名（哪怕只改一个字）都会使 final 偏离这两种形态，即视为已人工确认。
    """
    final = row.final
    ext = Path(final).suffix.lower()
    stem_part = final[: len(final) - len(ext)] if ext else final
    s = row.stem.lower()
    st = stem_part.lower()
    if not st.startswith(s):
        return False
    base = st[len(s):]
    return base == "" or bool(_AUTO_ORDINAL.fullmatch(base))


def _silent_numbering_remains(plan):
    """重算 I1：同一 stem 上有多个不同部件、且全部仍挂着自动名 → 静默序号仍在。"""
    by_stem = {}
    for r in plan.rows:
        if getattr(r, "skip", False) or not r.final:
            continue
        by_stem.setdefault(r.stem.lower(), []).append(r)
    for rows in by_stem.values():
        if len({r.part.lower() for r in rows}) > 1 and all(_is_auto_final(r) for r in rows):
            return True
    return False


# ---- 批次与回滚 ----

@router.get("/batches")
def batches():
    try:
        entries = apply_mod.read_journal(JOURNAL_PATH)
    except (OSError, FileNotFoundError):
        return {"batches": []}
    agg = {}
    for e in entries:
        if e.get("op") != "rename":
            continue
        pid = e.get("plan_id", "")
        a = agg.setdefault(pid, {"plan_id": pid, "count": 0, "ts": e.get("ts", ""),
                                 "folder": str(Path(e.get("src", "?")).parent)})
        a["count"] += 1
    out = sorted(agg.values(), key=lambda x: x["ts"], reverse=True)
    return {"batches": out}


@router.post("/rollback")
def rollback(req: RollbackReq):
    try:
        res = apply_mod.rollback(req.plan_id, JOURNAL_PATH)
    except FileNotFoundError:
        raise HTTPException(404, "journal 不存在：还没有任何落盘记录")
    return {
        "plan_id": res.plan_id, "restored": res.restored, "already": res.already,
        "mismatched": res.mismatched, "missing": res.missing,
    }


# ---- 缩略图 ----

@router.get("/thumb")
def thumb(request: Request, path: str, side: int = 512):
    """缩略图/预览图（仅白名单目录）。

    缓存策略（ERR-009）：ETag 取自文件 mtime_ns+size，浏览器每次带 If-None-Match
    再验证——文件一变 ETag 即变，彻底杜绝"同名路径换图后仍显示旧图"；
    服务端内存缓存按 (路径, mtime_ns, size, side) 失效，重验证成本极低。
    """
    if not state.path_allowed(path):
        raise HTTPException(403, "路径不在已扫描目录内")
    if side not in (256, 512, 1024, 1600):
        side = 512
    try:
        st = Path(path).stat()
        etag = f'"t{st.st_mtime_ns:x}s{st.st_size}"'
    except OSError:
        etag = '"placeholder"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    data = make_thumb_or_placeholder(path, side)
    return Response(content=data, media_type="image/jpeg", headers=headers)


# ---- 映射表导出（试用者 3 建议） ----

@router.get("/plans/{plan_key}/export.csv")
def export_csv(plan_key: str):
    d = state.get_plan(plan_key)
    if d is None:
        raise HTTPException(404, "方案不存在")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["原文件名", "新文件名", "AI 部件", "信息词", "跳过", "待复核"])
    for r in d.get("rows", []):
        w.writerow([Path(r["src"]).name, r.get("final", ""), r.get("part", ""),
                    r.get("info", ""), "是" if r.get("skip") else "",
                    "是" if r.get("needs_review") else ""])
    # \ufeff 前缀：Excel 直接打开 UTF-8 CSV 不乱码
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=picnamer-{plan_key}.csv"})
