# -*- coding: utf-8 -*-
"""阶段③ 命名规则单测：净化、拼名、去重、保留名、不变量。"""
from engine.naming import (Issue, build_stem, check_invariants, dedupe_stems,
                           group_same_stems, sanitize_component)
from engine.parts import PartGuess, FALLBACK_WORD


# ---- sanitize_component ----

def test_sanitize_replaces_illegal_chars():
    assert sanitize_component('a/b\\c:d*e?f"g<h>i|j{k}') == "a-b-c-d-e-f-g-h-i-j-k"


def test_sanitize_strips_trailing_dots_and_spaces():
    # Windows 会静默丢弃结尾的点与空格，必须剥掉，否则同名守卫失效
    assert sanitize_component("USB-C.") == "USB-C"
    assert sanitize_component("USB-C ") == "USB-C"


def test_sanitize_reserved_device_name():
    # AUX 是 Windows 保留设备名，落成 AUX.jpg 无法创建
    assert sanitize_component("AUX") == "AUX-件"
    assert sanitize_component("aux.jpg") == "aux-件.jpg"   # 只看第一个点前的基名


def test_sanitize_rejects_non_string():
    assert sanitize_component(["包装", "配件"]) == ""
    assert sanitize_component(None) == ""
    assert sanitize_component("") == ""


# ---- build_stem ----

def test_build_stem_full():
    assert build_stem("充电盒", "正面", "韶音") == "韶音-充电盒-正面"


def test_build_stem_drops_info_then_product_when_too_long():
    long_part = "充" * 20
    stem = build_stem(long_part, "正面", "商品词", max_len=25)
    assert stem == f"商品词-{long_part}"          # 先丢信息段
    stem2 = build_stem(long_part, "正面", "商品词很长很长", max_len=25)
    assert stem2 == long_part                     # 再丢商品段


def test_build_stem_truncates_at_member_boundary():
    part = "包装盒、说明书、挂绳、读卡器"
    stem = build_stem(part, "", None, max_len=12)
    # 移植修正后的行为：裁剪落在「、」边界，只保留完整成员（挂绳整体放得下就保留）
    assert stem == "包装盒、说明书、挂绳"
    assert len(stem) <= 12


def test_build_stem_hard_cuts_single_overlong_word():
    # 首个成员自身超长：路径上限是硬约束，必须硬切
    stem = build_stem("超" * 20, "", None, max_len=6)
    assert stem == "超" * 6


def test_build_stem_never_empty():
    assert build_stem("///", "///", "///") == FALLBACK_WORD


# ---- dedupe / group ----

def test_dedupe_stems_appends_ordinal_case_insensitive():
    out = dedupe_stems(["USB", "usb", "USB"])
    assert out[0] == "USB"
    assert out[1].lower() == "usb-02"
    assert out[2].lower() == "usb-03"


def test_dedupe_avoids_collision_with_existing_ordinal():
    # 原始列表里就有 a-02：a 的第二次出现不能撞成 a-02，要跳到 a-03
    out = dedupe_stems(["a", "a-02", "a"])
    assert out == ["a", "a-02", "a-03"]


def test_group_same_stems_case_insensitive():
    g = group_same_stems(["USB", "usb", "其他"])
    assert g["usb"] == [0, 1]


# ---- check_invariants ----

def test_invariants_fallback_heavy():
    gs = [PartGuess(part=FALLBACK_WORD) for _ in range(3)]
    gs.append(PartGuess(part="耳机"))
    kinds = [i.kind for i in check_invariants(gs)]
    assert "fallback_heavy" in kinds


def test_invariants_over_merge_generic_word():
    gs = [PartGuess(part="配件") for _ in range(5)]
    kinds = [i.kind for i in check_invariants(gs)]
    assert "over_merge" in kinds


def test_invariants_all_same_real_part_is_fine():
    gs = [PartGuess(part="耳机") for _ in range(6)]
    assert check_invariants(gs) == []
