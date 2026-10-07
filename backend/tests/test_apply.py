# -*- coding: utf-8 -*-
"""落盘安全单测：预检零写入、改名环、journal 写前日志、异常自动回滚、哈希校验回滚。"""
import json

import pytest

from engine.apply import (apply_plan, new_plan_id, plan_ops, preflight,
                          read_journal, rollback)
from engine.pipeline import FolderPlan, Row


def build_plan(folder, rows_spec):
    """rows_spec: [(src_name, final_or_None, skip)]，文件由测试自行落盘。"""
    plan = FolderPlan(folder=str(folder))
    for src, final, skip in rows_spec:
        plan.rows.append(Row(
            src=str(folder / src), raw_part="", part="", info="", kind="single",
            conf=0.9, stem="", final=final or "", skip=skip))
    return plan


def touch(folder, name, content=b"x"):
    p = folder / name
    p.write_bytes(content)
    return p


# ---- plan_ops 的 skip 语义 ----

def test_plan_ops_respects_skip(tmp_path):
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "b.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "新-a.jpg", False),
                                 ("b.jpg", "新-b.jpg", True)])
    ops = plan_ops(plan)
    assert [o.noop for o in ops] == [False, True]


def test_plan_ops_empty_final_is_noop(tmp_path):
    touch(tmp_path, "a.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "", False)])
    assert plan_ops(plan)[0].noop is True


# ---- 预检 ----

def test_preflight_rejects_existing_dst(tmp_path):
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "占用.jpg", b"other")
    plan = build_plan(tmp_path, [("a.jpg", "占用.jpg", False)])
    conflicts = preflight(plan)
    assert [c.reason for c in conflicts] == ["dst_exists"]


def test_preflight_detects_swap_cycle(tmp_path):
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "b.jpg")
    # 互换名字：排序无解，必须整目录拒绝
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False),
                                 ("b.jpg", "a.jpg", False)])
    conflicts = preflight(plan)
    assert {c.reason for c in conflicts} == {"rename_cycle"}


def test_preflight_allows_overlap_resolved_by_order(tmp_path):
    # a 改成 b，b 改成 c：排序后可解，不算冲突
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "b.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False),
                                 ("b.jpg", "c.jpg", False)])
    assert preflight(plan) == []


def test_preflight_case_insensitive(tmp_path):
    # Windows 文件名不区分大小写：A.JPG 与 a.jpg 是同一个文件
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "B.JPG", b"other")
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False)])
    assert [c.reason for c in preflight(plan)] == ["dst_exists"]


# ---- 落盘与 journal ----

def test_apply_success_and_journal(tmp_path, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "充电盒-正面.jpg", False)])
    res = apply_plan(plan, journal, plan_id="p-test-1")
    assert not res.aborted and res.renamed == 1
    assert (tmp_path / "充电盒-正面.jpg").exists()
    entries = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines()]
    assert entries[0]["op"] == "rename" and entries[0]["result"] == "pending"
    assert entries[0]["hash"].startswith("sha256:")


def test_apply_blocked_by_collision_issue(tmp_path):
    from engine.naming import Issue
    touch(tmp_path, "a.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "充电盒-正面.jpg", False)])
    plan.issues.append(Issue("stem_collision", "不同部件落到同名"))
    res = apply_plan(plan, tmp_path / "j.jsonl", plan_id="p-1")
    assert res.aborted and res.blocked
    assert (tmp_path / "a.jpg").exists()                     # 零写入
    assert not (tmp_path / "充电盒-正面.jpg").exists()


def test_apply_accept_review_passes_blocking(tmp_path):
    from engine.naming import Issue
    touch(tmp_path, "a.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "充电盒-正面.jpg", False)])
    plan.issues.append(Issue("stem_collision", "人工已核对"))
    res = apply_plan(plan, tmp_path / "j.jsonl", plan_id="p-2", accept_review=True)
    assert not res.aborted and res.accepted_review and res.renamed == 1


def test_apply_midway_failure_auto_rolls_back(tmp_path, monkeypatch, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "b.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "x1.jpg", False),
                                 ("b.jpg", "x2.jpg", False)])
    real_rename = apply_plan.__globals__["os"].rename

    calls = {"n": 0}
    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:               # 第二条改名时注入失败
            raise OSError("disk error")
        return real_rename(src, dst)

    monkeypatch.setattr("engine.apply._rename", flaky)
    res = apply_plan(plan, journal, plan_id="p-3")
    assert res.aborted and res.renamed == 0 and res.rolled_back == 1
    assert (tmp_path / "a.jpg").exists() and (tmp_path / "b.jpg").exists()


# ---- 回滚 ----

def test_rollback_restores_names(tmp_path, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg", b"hello")
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False)])
    apply_plan(plan, journal, plan_id="p-4")
    assert (tmp_path / "b.jpg").exists()

    res = rollback("p-4", journal)
    assert res.restored == 1 and (tmp_path / "a.jpg").exists()
    # journal 里补了 rollback 记录
    entries = read_journal(journal)
    assert entries[-1]["op"] == "rollback" and entries[-1]["result"] == "ok"


def test_rollback_rejects_modified_content(tmp_path, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg", b"hello")
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False)])
    apply_plan(plan, journal, plan_id="p-5")
    (tmp_path / "b.jpg").write_bytes(b"tampered")       # 外部改过内容
    res = rollback("p-5", journal)
    assert res.restored == 0 and len(res.mismatched) == 1
    assert (tmp_path / "b.jpg").exists()                # 不动被改过的文件


def test_rollback_idempotent(tmp_path, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "b.jpg", False)])
    apply_plan(plan, journal, plan_id="p-6")
    assert rollback("p-6", journal).restored == 1
    again = rollback("p-6", journal)                     # 第二次：已是原名
    assert again.restored == 0 and again.already == 1


def test_new_plan_id_unique_within_same_second():
    ids = {new_plan_id() for _ in range(20)}
    assert len(ids) == 20


# ---- 批内同名（复核编辑可能引入，ERR-005）----

def test_preflight_flags_duplicate_dst_in_batch(tmp_path):
    touch(tmp_path, "a.jpg")
    touch(tmp_path, "b.jpg")
    plan = build_plan(tmp_path, [("a.jpg", "同名.jpg", False),
                                 ("b.jpg", "同名.jpg", False)])
    assert {c.reason for c in preflight(plan)} == {"dst_duplicate"}


def test_preflight_case_only_rename_is_not_duplicate(tmp_path):
    touch(tmp_path, "A.JPG")
    plan = build_plan(tmp_path, [("A.JPG", "a.jpg", False)])
    assert preflight(plan) == []          # 仅大小写不同，不产生新名字


def test_apply_duplicate_dst_aborts_zero_write(tmp_path, tmp_path_factory):
    journal = tmp_path_factory.mktemp("out") / "journal.jsonl"
    touch(tmp_path, "a.jpg", b"aa")
    touch(tmp_path, "b.jpg", b"bb")
    plan = build_plan(tmp_path, [("a.jpg", "同名.jpg", False),
                                 ("b.jpg", "同名.jpg", False)])
    res = apply_plan(plan, journal, plan_id="p-7")
    assert res.aborted and res.renamed == 0
    assert {c.reason for c in res.conflicts} == {"dst_duplicate"}
    assert (tmp_path / "a.jpg").exists() and (tmp_path / "b.jpg").exists()
