# -*- coding: utf-8 -*-
"""试用反馈驱动的修复与功能测试：缩略图 ETag 即时刷新、CSV 映射表导出、
目录内容变动自动重扫、快照时间、放大预览参数。"""
import time

import pytest

import webapp.routes as routes
from conftest import FakeProvider, make_image
from webapp.batch import BatchManager

MERGE_OK = '{"canon": ["耳机"], "map": {}, "split": {}}'


@pytest.fixture
def batch(api, tmp_path, monkeypatch):
    m = BatchManager(routes.state, tmp_path / "batch.json")
    monkeypatch.setattr(routes, "batch_manager", m)
    return m


def wait_batch_done(api, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = api.get("/api/batch").json()
        if b.get("exists") and not b.get("running"):
            return b
        time.sleep(0.1)
    raise AssertionError("批次超时未完成")


def _scan_one(api, tmp_path, name="相册", n=2):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    for i in range(n):
        make_image(folder / f"IMG_{i:03d}.jpg", color=(60 * i + 30, 100, 150))
    job_id = api.post("/api/scan", json={"folder": str(folder)}).json()["job_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        job = api.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            assert job["status"] == "done", job["error"]
            return folder, job["plan_key"]
        time.sleep(0.05)
    raise AssertionError("扫描超时")


# ---- ERR-009：文件变动后缩略图必须即时刷新 ----

def test_thumb_etag_revalidates_on_change(api, tmp_path):
    folder, _ = _scan_one(api, tmp_path, n=1)
    img = folder / "IMG_000.jpg"

    r1 = api.get("/api/thumb", params={"path": str(img)})
    assert r1.status_code == 200
    etag1 = r1.headers["etag"]
    assert r1.headers["cache-control"] == "no-cache"

    # 内容未变：带 If-None-Match 再验证 → 304（省流量）
    r2 = api.get("/api/thumb", params={"path": str(img)},
                 headers={"If-None-Match": etag1})
    assert r2.status_code == 304

    # 同名路径替换成不同内容的图：ETag 必须变化 → 返回 200 新图（修复"还是老图"）
    make_image(img, size=(80, 60), color=(10, 200, 90))
    r3 = api.get("/api/thumb", params={"path": str(img)},
                 headers={"If-None-Match": etag1})
    assert r3.status_code == 200
    assert r3.headers["etag"] != etag1


def test_thumb_side_param_for_lightbox(api, tmp_path):
    folder, _ = _scan_one(api, tmp_path, n=1)
    img = folder / "IMG_000.jpg"
    r = api.get("/api/thumb", params={"path": str(img), "side": 1600})
    assert r.status_code == 200
    r2 = api.get("/api/thumb", params={"path": str(img), "side": 999})
    assert r2.status_code == 200        # 非法 side 回落 512，不报错


# ---- 快照时间（目录改了页面没变的困惑） ----

def test_plan_carries_created_timestamp(api, tmp_path):
    _, key = _scan_one(api, tmp_path)
    plan = api.get(f"/api/plans/{key}").json()
    assert plan.get("created") and "T" in plan["created"]


# ---- CSV 映射表导出（试用者 3 建议） ----

def test_csv_export(api, tmp_path):
    folder, key = _scan_one(api, tmp_path, n=3)
    r = api.get(f"/api/plans/{key}/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")
    text = r.content.decode("utf-8-sig")     # 去 BOM
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines[0] == "原文件名,新文件名,AI 部件,信息词,跳过,待复核"
    assert len(lines) == 4                    # 表头 + 3 张图
    assert "IMG_000.jpg" in text


def test_csv_export_404(api):
    assert api.get("/api/plans/不存在/export.csv").status_code == 404


# ---- ADR-010：目录内容变动 → 重提交自动重新处理 ----

def test_batch_resubmit_refreshes_changed_folder(api, batch, tmp_path):
    d1 = tmp_path / "甲"
    d1.mkdir()
    make_image(d1 / "A.jpg")
    d2 = tmp_path / "乙"
    d2.mkdir()
    make_image(d2 / "B.jpg")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    b1 = wait_batch_done(api)
    key_d1_old = b1["folders"][0]["plan_key"]
    key_d2_old = b1["folders"][1]["plan_key"]

    # 甲目录加了图：重提交同一目录集合 → 甲自动重扫，乙不动
    make_image(d1 / "C_new.jpg")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    b2 = wait_batch_done(api)
    assert b2["folders"][0]["plan_key"] != key_d1_old        # 甲重新处理了
    assert b2["folders"][0]["total"] == 2                    # 新图被扫到
    assert b2["folders"][1]["plan_key"] == key_d2_old        # 乙没重跑

    # 甲目录删图：同样触发重扫
    (d1 / "C_new.jpg").unlink()
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    b3 = wait_batch_done(api)
    assert b3["folders"][0]["plan_key"] not in (key_d1_old, b2["folders"][0]["plan_key"])
    assert b3["folders"][0]["total"] == 1


def test_batch_resume_still_skips_done(api, batch, tmp_path):
    """resume 语义不变：纯续跑跳过 done（即使目录内容变了也不重扫）。"""
    d = tmp_path / "甲"
    d.mkdir()
    make_image(d / "A.jpg")
    api.post("/api/batch", json={"folders": [str(d)]})
    b1 = wait_batch_done(api)
    make_image(d / "NEW.jpg")                    # 内容变了
    api.post("/api/batch/resume")                # resume 不比对签名
    b2 = wait_batch_done(api)
    assert b2["folders"][0]["plan_key"] == b1["folders"][0]["plan_key"]
    assert b2["folders"][0]["total"] == 1        # 还是旧快照（明确语义）


# ---- 单目录重扫（目录变动的就近刷新入口） ----

def test_batch_rescan_single_folder(api, batch, tmp_path):
    d1 = tmp_path / "甲"
    d1.mkdir()
    make_image(d1 / "A.jpg")
    d2 = tmp_path / "乙"
    d2.mkdir()
    make_image(d2 / "B.jpg")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    b1 = wait_batch_done(api)
    key_d1_old = b1["folders"][0]["plan_key"]

    make_image(d1 / "NEW.jpg")                   # 甲内容变了
    r = api.post("/api/batch/rescan", json={"path": str(d1)})
    assert r.status_code == 200
    b2 = wait_batch_done(api)
    assert b2["folders"][0]["plan_key"] != key_d1_old
    assert b2["folders"][0]["total"] == 2        # 新图被扫到
    assert b2["folders"][1]["plan_key"] == b1["folders"][1]["plan_key"]  # 乙不动


def test_batch_rescan_errors(api, batch, tmp_path):
    # 还没有批次
    r0 = api.post("/api/batch/rescan", json={"path": str(tmp_path)})
    assert r0.status_code == 400
    # 有批次但目录不在其中
    d = tmp_path / "甲"
    d.mkdir()
    make_image(d / "A.jpg")
    api.post("/api/batch", json={"folders": [str(d)]})
    wait_batch_done(api)
    other = tmp_path / "丙"
    other.mkdir()
    r1 = api.post("/api/batch/rescan", json={"path": str(other)})
    assert r1.status_code == 400
    assert "不在当前批次" in r1.json()["detail"]
