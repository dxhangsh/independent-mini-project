# -*- coding: utf-8 -*-
"""阶段二测试：多目录批量、断点续跑、服务预检、云端 Provider 契约、设置扩展。"""
import json
import time

import pytest

import webapp.routes as routes
from webapp.batch import BatchManager
from webapp.state import DEFAULT_SETTINGS
from conftest import FakeProvider, make_image

MERGE_OK = json.dumps({
    "canon": ["耳机", "充电盒"],
    "map": {"蓝牙耳机": "耳机"},
    "split": {},
}, ensure_ascii=False)


@pytest.fixture
def batch(api, tmp_path, monkeypatch):
    """把批量管理器指向临时持久化文件。"""
    m = BatchManager(routes.state, tmp_path / "batch.json")
    monkeypatch.setattr(routes, "batch_manager", m)
    return m


def make_folder(tmp_path, name, n=3):
    d = tmp_path / name
    d.mkdir()
    for i in range(n):
        make_image(d / f"IMG_{i:03d}.jpg")
    return d


def wait_batch_done(api, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = api.get("/api/batch").json()
        if b.get("exists") and not b.get("running"):
            return b
        time.sleep(0.1)
    raise AssertionError("批次超时未完成")


# ---- 2.1/2.2 批量与进度 ----

def test_batch_two_folders_completes(api, batch, tmp_path):
    d1 = make_folder(tmp_path, "甲")
    d2 = make_folder(tmp_path, "乙")
    r = api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    assert r.status_code == 200
    b = wait_batch_done(api)
    assert [f["status"] for f in b["folders"]] == ["done", "done"]
    assert all(f["total"] == 3 for f in b["folders"])
    # 两个方案都可取到
    plans = [api.get(f"/api/plans/{f['plan_key']}").json() for f in b["folders"]]
    assert plans[0]["total"] == 3 and plans[1]["total"] == 3


def test_batch_resume_after_crash_skips_done(api, batch, tmp_path):
    d1 = make_folder(tmp_path, "甲")
    d2 = make_folder(tmp_path, "乙")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    b = wait_batch_done(api)
    # 模拟"服务在处理乙目录前崩溃"：乙重置为 pending，甲保持 done
    batch.data["folders"][1]["status"] = "pending"
    batch.data["folders"][1]["plan_key"] = ""
    batch._save()
    # 重新加载管理器（模拟重启后从磁盘恢复）
    routes.batch_manager = BatchManager(routes.state, batch.path)
    api.post("/api/batch/resume")
    b2 = wait_batch_done(api)
    # 甲没有被重跑（plan_key 不变），乙完成
    assert b2["folders"][0]["plan_key"] == b["folders"][0]["plan_key"]
    assert b2["folders"][0]["status"] == "done"
    assert b2["folders"][1]["status"] == "done"


def test_batch_resubmit_same_folders_skips_done(api, batch, tmp_path):
    d1 = make_folder(tmp_path, "甲")
    d2 = make_folder(tmp_path, "乙")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    wait_batch_done(api)
    keys_before = {f["path"]: f["plan_key"] for f in api.get("/api/batch").json()["folders"]}
    # 同目录集合重新提交：done 目录应保留，不重跑
    api.post("/api/batch", json={"folders": [str(d2), str(d1)]})
    b = wait_batch_done(api)
    keys_after = {f["path"]: f["plan_key"] for f in b["folders"]}
    assert keys_before == keys_after


def test_batch_cancel_stops_between_folders(api, batch, tmp_path):
    d1 = make_folder(tmp_path, "甲")
    d2 = make_folder(tmp_path, "乙")
    api.post("/api/batch", json={"folders": [str(d1), str(d2)]})
    # 立刻请求停止：至少一个目录完成后批次停止（不保证停在哪个边界）
    api.post("/api/batch/cancel")
    b = wait_batch_done(api)
    assert not b["running"]
    statuses = [f["status"] for f in b["folders"]]
    assert "running" not in statuses


# ---- 2.4 服务预检 ----

class DeadProvider(FakeProvider):
    def ping(self):
        return {"ok": False, "detail": "connection refused"}


def test_scan_rejected_when_service_down(api, tmp_path):
    d = make_folder(tmp_path, "甲", n=1)
    routes.state.serial.swap(DeadProvider([], MERGE_OK))
    r = api.post("/api/scan", json={"folder": str(d)})
    assert r.status_code == 503
    assert "AI 服务未连接" in r.json()["detail"]


def test_batch_rejected_when_service_down(api, tmp_path):
    d = make_folder(tmp_path, "甲", n=1)
    routes.state.serial.swap(DeadProvider([], MERGE_OK))
    r = api.post("/api/batch", json={"folders": [str(d)]})
    assert r.status_code == 503


def test_batch_invalid_folder_400(api, batch, tmp_path):
    d = make_folder(tmp_path, "甲", n=1)
    r = api.post("/api/batch", json={"folders": [str(d), "Z:\\不存在"]})
    assert r.status_code == 400
    assert "Z:\\不存在" in r.json()["detail"]


# ---- 2.3 设置扩展：默认商品词 ----

def test_settings_default_product_word(api, monkeypatch):
    monkeypatch.setattr(routes.state, "settings", dict(DEFAULT_SETTINGS))
    r = api.post("/api/settings", json={"default_product_word": "韶音OpenFit2"}).json()
    assert r["settings"]["default_product_word"] == "韶音OpenFit2"
    g = api.get("/api/settings").json()
    assert g["default_product_word"] == "韶音OpenFit2"


# ---- 2.3 云端 Provider 契约：切换不改代码 ----

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def test_cloud_provider_classify_request_shape(api, tmp_path, monkeypatch):
    from providers.cloud_openai import CloudOpenAIProvider
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"choices": [{"message": {"content": '{"part":"耳机"}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = CloudOpenAIProvider(base_url="https://api.example.com/v1",
                            api_key="sk-test-1234", model="qwen-vl-plus")
    img = make_image(tmp_path / "x.jpg")
    r = p.classify(str(img), "提示词", num_predict=1024)
    assert r["response"] == '{"part":"耳机"}'
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"].get("Authorization") == "Bearer sk-test-1234"
    body = captured["body"]
    assert body["model"] == "qwen-vl-plus" and body["temperature"] == 0
    assert body["max_tokens"] == 1024
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text" and content[0]["text"] == "提示词"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_cloud_provider_generate_and_ping(api, monkeypatch):
    from providers.cloud_openai import CloudOpenAIProvider
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(
                            {"choices": [{"message": {"content": "OK"}}]}))
    p = CloudOpenAIProvider(base_url="https://api.example.com/v1", api_key="k")
    assert p.generate("hi") == "OK"
    assert p.ping()["ok"] is True
    # 未配置时 ping 直接失败，不发请求
    p2 = CloudOpenAIProvider(base_url="", api_key="")
    assert p2.ping()["ok"] is False
