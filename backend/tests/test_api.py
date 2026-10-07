# -*- coding: utf-8 -*-
"""API 集成测试：假 Provider 驱动「扫描→进度→复核→落盘→回滚→批次」全链路。

方案存储与 journal 指向临时目录，不污染项目 out/。
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import json
import time

import pytest
from fastapi.testclient import TestClient

import webapp.routes as routes
import webapp.state as state_mod
from conftest import FakeProvider, make_image

MERGE_OK = json.dumps({
    "canon": ["充电盒", "耳机"],
    "map": {"蓝牙耳机": "耳机"},
    "split": {},
}, ensure_ascii=False)


def wait_job(api, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job 超时未完成")


# ---- 健康与设置 ----

def test_health(api):
    r = api.get("/api/health").json()
    assert r["ok"] is True
    assert r["provider"] == "fake"
    assert r["ping"]["ok"] is True


def test_settings_roundtrip_masks_key(api, monkeypatch):
    monkeypatch.setattr(routes.state, "settings", dict(state_mod.DEFAULT_SETTINGS))
    r = api.post("/api/settings", json={
        "provider": "cloud_openai",
        "cloud_base_url": "https://api.example.com/v1",
        "cloud_api_key": "sk-secret-1234",
        "cloud_model": "qwen-vl-plus",
    }).json()
    assert r["settings"]["cloud_api_key"].startswith("****")
    assert "sk-secret-1234" not in json.dumps(r)
    # 传掩码不会覆盖真值
    r2 = api.post("/api/settings", json={"cloud_api_key": "****1234"}).json()
    assert r2["ok"]


def test_health_provider_after_save_settings(api, monkeypatch):
    """人工实测发现的回归（2026-10-02）：保存设置后 health.provider 必须仍是真实 provider 名。
    曾因 update_settings 把 SerialProvider 整体 swap 形成嵌套，health 返回 "serial"，
    前端徽标误显示「云端 · …」。"""
    monkeypatch.setattr(routes.state, "settings", dict(state_mod.DEFAULT_SETTINGS))
    r = api.post("/api/settings", json={
        "provider": "local_ollama",
        "ollama_host": "http://127.0.0.1:11434",
        "model": "qwen3-vl:8b",
    }).json()
    assert r["ok"]
    h = api.get("/api/health").json()
    assert h["provider"] == "local_ollama"   # 回归点：修复前为 "serial"
    assert h["model"] == "qwen3-vl:8b"


# ---- 全链路 ----

def test_full_loop_scan_review_apply_rollback(api, tmp_path):
    routes.state.serial.swap(FakeProvider(
        parts_script=[
            {"part": "充电盒", "info": "正面", "conf": 0.9},
            {"part": "蓝牙耳机", "info": "正面", "conf": 0.9},
            {"part": "耳机", "info": "背面", "conf": 0.9},
        ],
        merge_response=MERGE_OK))

    folder = tmp_path / "耳机套装"
    folder.mkdir()
    make_image(folder / "IMG_001.jpg")
    make_image(folder / "IMG_002.jpg")
    make_image(folder / "IMG_003.png")

    # 1) 扫描 → 任务完成
    r = api.post("/api/scan", json={"folder": str(folder)}).json()
    job = wait_job(api, r["job_id"])
    assert job["status"] == "done" and job["total"] == 3
    assert job["done"] == 3

    # 2) 复核数据：归并已把 蓝牙耳机 → 耳机
    plan = api.get(f"/api/plans/{job['plan_key']}").json()
    assert plan["total"] == 3
    finals = {row["src"].split("\\")[-1].split("/")[-1]: row["final"] for row in plan["rows"]}
    assert finals["IMG_002.jpg"] == "耳机-正面.jpg"

    # 3) 复核编辑：跳过第 1 张、改第 2 张名
    rows = [{"final": row["final"], "skip": i == 0} for i, row in enumerate(plan["rows"])]
    rows[1]["final"] = "耳机-正面-编辑.jpg"
    assert api.post(f"/api/plans/{plan_key(plan)}/review", json={"rows": rows}).json()["ok"]

    # 4) 落盘：2 张改名（1 张跳过）
    res = api.post(f"/api/plans/{plan_key(plan)}/apply",
                   json={"accept_review": False}).json()
    assert not res["aborted"]
    assert res["renamed"] == 2 and res["noop"] == 1
    assert (folder / "耳机-正面-编辑.jpg").exists()
    assert (folder / "IMG_001.jpg").exists()          # 跳过的保持原名

    # 5) 批次列表
    batches = api.get("/api/batches").json()["batches"]
    assert len(batches) == 1 and batches[0]["count"] == 2

    # 6) 回滚
    rb = api.post("/api/rollback",
                  json={"plan_id": res["plan_id"]}).json()
    assert rb["restored"] == 2
    assert (folder / "IMG_002.jpg").exists()
    assert not (folder / "耳机-正面-编辑.jpg").exists()


def plan_key(plan):
    # plan dict 没带 key；用 jobs 里最近完成任务的 plan_key
    for job in routes.state.jobs.values():
        if job.status == "done":
            return job.plan_key
    raise AssertionError("找不到已完成任务")


def test_scan_rejects_missing_folder(api):
    r = api.post("/api/scan", json={"folder": "Z:\\不存在的目录"})
    assert r.status_code == 400


def test_thumb_requires_allowed_root(api, tmp_path):
    folder = tmp_path / "相册"
    folder.mkdir()
    img = make_image(folder / "a.jpg")
    # 未扫描过：403
    r = api.get("/api/thumb", params={"path": str(img)})
    assert r.status_code == 403
    # 扫描后放行
    job = wait_job(api, api.post("/api/scan", json={"folder": str(folder)}).json()["job_id"])
    assert job["status"] == "done"
    r = api.get("/api/thumb", params={"path": str(img)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_plan_404(api):
    assert api.get("/api/plans/不存在").status_code == 404


def test_review_row_count_mismatch_rejected(api, tmp_path):
    routes.state.serial.swap(FakeProvider(
        parts_script=[{"part": "耳机", "info": "", "conf": 0.9}],
        merge_response=MERGE_OK))
    folder = tmp_path / "单图"
    folder.mkdir()
    make_image(folder / "a.jpg")
    job = wait_job(api, api.post("/api/scan", json={"folder": str(folder)}).json()["job_id"])
    r = api.post(f"/api/plans/{job['plan_key']}/review",
                 json={"rows": [{"final": "x.jpg", "skip": False},
                                {"final": "y.jpg", "skip": False}]})
    assert r.status_code == 404


# ---- I1 重算（ERR-005：过期冲突标记不应误拦人工改开的名字）----

def _seed_collision_plan(api, tmp_path, b_final="充电盒-正面-02.jpg"):
    """直接注入一个带 stem_collision 的方案：充电盒与充电线两部件共用 stem。"""
    folder = tmp_path / "冲突目录"
    folder.mkdir()
    a = make_image(folder / "a.jpg")
    b = make_image(folder / "b.jpg")
    plan_dict = {
        "folder": str(folder),
        "canon": ["充电盒", "充电线"],
        "issues": [{"kind": "stem_collision", "detail": "不同部件落到同名，1 张待复核"}],
        "rows": [
            {"src": str(a), "raw_part": "充电盒", "part": "充电盒", "info": "正面",
             "kind": "single", "conf": 0.9, "stem": "充电盒-正面",
             "final": "充电盒-正面.jpg", "needs_review": True,
             "exif_ordinal": False, "skip": False},
            {"src": str(b), "raw_part": "充电线", "part": "充电线", "info": "正面",
             "kind": "single", "conf": 0.9, "stem": "充电盒-正面",
             "final": b_final, "needs_review": True,
             "exif_ordinal": False, "skip": False},
        ],
    }
    return folder, routes.state.save_plan(plan_dict)


def test_stale_collision_blocks_until_resolved_or_accepted(api, tmp_path):
    folder, key = _seed_collision_plan(api, tmp_path)
    # 未处理：静默序号仍在 → 阻断（保持 I1 安全语义）
    r1 = api.post(f"/api/plans/{key}/apply", json={"accept_review": False}).json()
    assert r1["aborted"] and r1["blocked"]
    assert (folder / "a.jpg").exists() and (folder / "b.jpg").exists()
    # 显式放行：可以落盘（沿用自动 -02 序号）
    r2 = api.post(f"/api/plans/{key}/apply", json={"accept_review": True}).json()
    assert not r2["aborted"] and r2["accepted_review"] and r2["renamed"] == 2
    assert (folder / "充电盒-正面-02.jpg").exists()


def test_collision_resolved_by_rename_applies_without_accept(api, tmp_path):
    folder, key = _seed_collision_plan(api, tmp_path)
    # 人工把充电线改成语义名 → 重算后不再拦截，无需勾选放行
    plan = api.get(f"/api/plans/{key}").json()
    rows = [{"final": row["final"], "skip": row["skip"]} for row in plan["rows"]]
    rows[1]["final"] = "充电线-正面.jpg"
    api.post(f"/api/plans/{key}/review", json={"rows": rows})
    r = api.post(f"/api/plans/{key}/apply", json={"accept_review": False}).json()
    assert not r["aborted"] and r["renamed"] == 2
    assert (folder / "充电盒-正面.jpg").exists()
    assert (folder / "充电线-正面.jpg").exists()


def test_user_created_duplicate_dst_is_hard_blocked(api, tmp_path):
    folder, key = _seed_collision_plan(api, tmp_path)
    # 人工把两张改成同一个 final → 预检 dst_duplicate 硬拦（不可放行）
    plan = api.get(f"/api/plans/{key}").json()
    rows = [{"final": "同名.jpg", "skip": False} for _ in plan["rows"]]
    api.post(f"/api/plans/{key}/review", json={"rows": rows})
    r = api.post(f"/api/plans/{key}/apply", json={"accept_review": True}).json()
    assert r["aborted"]
    assert {c["reason"] for c in r["conflicts"]} == {"dst_duplicate"}
    assert (folder / "a.jpg").exists() and (folder / "b.jpg").exists()
