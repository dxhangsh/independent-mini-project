# -*- coding: utf-8 -*-
"""pytest 公共夹具：把 backend/ 加入 sys.path，提供假 Provider 与临时图片目录。"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class FakeProvider:
    """脚本化假 Provider：classify/generate 按预置脚本返回，不碰网络与 GPU。"""

    name = "fake"

    def __init__(self, parts_script=None, merge_response=None):
        self.parts_script = parts_script or []
        self.merge_response = merge_response or ""
        self.classify_calls = 0
        self.generate_calls = 0

    def classify(self, image_path, prompt, num_predict=None, prefill=None):
        idx = min(self.classify_calls, len(self.parts_script) - 1)
        self.classify_calls += 1
        return {"response": json.dumps(self.parts_script[idx], ensure_ascii=False)}

    def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
        self.generate_calls += 1
        return self.merge_response

    def ping(self):
        return {"ok": True, "detail": "fake provider"}


def make_image(path, size=(64, 48), color=(180, 60, 60)):
    """生成一张真实 PNG（流水线与缩略图都会用 PIL 解码）。"""
    from PIL import Image
    im = Image.new("RGB", size, color)
    im.save(path)
    return Path(path)


DEFAULT_MERGE_OK = json.dumps({
    "canon": ["充电盒", "耳机"],
    "map": {"蓝牙耳机": "耳机"},
    "split": {},
}, ensure_ascii=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient + 假 Provider + 临时 journal/plans/config 目录（全模块共享）。"""
    from fastapi.testclient import TestClient

    import webapp.routes as routes
    import webapp.state as state_mod
    from main import app

    monkeypatch.setattr(routes, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(state_mod, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(routes.state, "allowed_roots", set())
    routes.state.serial.swap(FakeProvider(
        parts_script=[
            {"part": "充电盒", "info": "正面", "conf": 0.9},
            {"part": "蓝牙耳机", "info": "正面", "conf": 0.9},
            {"part": "耳机", "info": "背面", "conf": 0.9},
        ],
        merge_response=DEFAULT_MERGE_OK))
    return TestClient(app)


@pytest.fixture
def api(client):
    return client


@pytest.fixture
def image_dir(tmp_path):
    """建一个含 3 张图的临时目录，返回目录 Path。"""
    d = tmp_path / "耳机套装"
    d.mkdir()
    make_image(d / "IMG_001.jpg", color=(200, 80, 80))
    make_image(d / "IMG_002.jpg", color=(80, 200, 80))
    make_image(d / "IMG_003.png", color=(80, 80, 200))
    return d
