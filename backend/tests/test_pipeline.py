# -*- coding: utf-8 -*-
"""流水线编排集成测试：假 Provider 驱动 run_folder 全流程（不碰网络与 GPU）。"""
import json

from conftest import FakeProvider
from engine.pipeline import run_folder


def wired(provider):
    """FakeProvider → 引擎需要的 client 接口（classify/generate 已同名兼容）。"""
    return provider


MERGE_OK = json.dumps({
    "canon": ["充电盒", "耳机"],
    "map": {"蓝牙耳机": "耳机", "充电盒": "充电盒"},
    "split": {},
}, ensure_ascii=False)


def test_basic_flow_names_files(image_dir):
    fp = FakeProvider(
        parts_script=[
            {"part": "充电盒", "info": "正面", "conf": 0.9},
            {"part": "蓝牙耳机", "info": "正面", "conf": 0.9},   # 归并后 → 耳机
            {"part": "耳机", "info": "背面", "conf": 0.9},
        ],
        merge_response=MERGE_OK)
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None)

    assert [r.final for r in plan.rows] == [
        "充电盒-正面.jpg", "耳机-正面.jpg", "耳机-背面.png"]   # 扩展名保留原名
    assert not plan.review_count
    kinds = {i.kind for i in plan.issues}
    assert "stem_collision" not in kinds


def test_on_image_progress_hook(image_dir):
    seen = []

    fp = FakeProvider(
        parts_script=[{"part": "耳机", "info": "", "conf": 0.9}] * 3,
        merge_response=MERGE_OK)
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None,
                      on_image=lambda i, total, path: seen.append((i, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert len(plan.rows) == 3


def test_same_part_same_info_gets_exif_ordinal(image_dir):
    fp = FakeProvider(
        parts_script=[{"part": "耳机", "info": "正面", "conf": 0.9}] * 3,
        merge_response=MERGE_OK)
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None)

    finals = [r.final.lower() for r in plan.rows]
    assert len(set(finals)) == 3                       # 决不重名
    assert any(r.exif_ordinal for r in plan.rows)
    assert any(i.kind == "exif_ordinal" for i in plan.issues)


def test_tiny_stem_budget_forces_collision_and_blocks_apply(image_dir, tmp_path):
    from engine.apply import apply_plan
    # stem_budget=1：不同部件都截成同一个字 → I1 真冲突
    fp = FakeProvider(
        parts_script=[
            {"part": "充电盒", "info": "正面", "conf": 0.9},
            {"part": "充电线", "info": "正面", "conf": 0.9},
        ],
        merge_response=json.dumps({"canon": ["充电盒", "充电线"], "map": {},
                                   "split": {}}, ensure_ascii=False))
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None, stem_budget=1)
    assert any(i.kind == "stem_collision" for i in plan.issues)
    assert all(r.needs_review for r in plan.rows)

    res = apply_plan(plan, tmp_path / "j.jsonl", plan_id="p-x")
    assert res.aborted and res.blocked                  # 落盘层整目录阻断


def test_merge_failure_degrades_gracefully(image_dir):
    class BoomProvider(FakeProvider):
        def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
            raise RuntimeError("network down")

    from engine.errors import VlmError
    class VlmBoomProvider(FakeProvider):
        def generate(self, prompt, num_predict=None, prefill=None, prefill_only=False):
            raise VlmError("空响应")

    fp = VlmBoomProvider(
        parts_script=[{"part": "耳机", "info": "", "conf": 0.9}] * 3)
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None)
    kinds = {i.kind for i in plan.issues}
    assert "merge_failed" in kinds and "vocab_degraded" in kinds
    assert len(plan.rows) == 3                          # 目录没有崩，行仍在


def test_single_image_failure_does_not_kill_folder(image_dir):
    calls = {"n": 0}
    class FlakyProvider(FakeProvider):
        def classify(self, image_path, prompt, num_predict=None, prefill=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return super().classify(image_path, prompt, num_predict, prefill)

    fp = FlakyProvider(
        parts_script=[{"part": "耳机", "info": "", "conf": 0.9}],
        merge_response=MERGE_OK)
    plan = run_folder(wired(fp), image_dir, progress=lambda *_: None)
    assert len(plan.rows) == 3
    assert any(i.kind == "guess_failed" for i in plan.issues)
    assert plan.rows[0].needs_review                    # 失败那张标待复核


def test_folder_not_found_is_explicit(tmp_path):
    plan = run_folder(wired(FakeProvider()), tmp_path / "不存在", progress=lambda *_: None)
    assert any(i.kind == "folder_not_found" for i in plan.issues)


def test_empty_folder_returns_empty_plan(tmp_path):
    d = tmp_path / "空目录"
    d.mkdir()
    plan = run_folder(wired(FakeProvider()), d, progress=lambda *_: None)
    assert plan.rows == [] and plan.issues == []
