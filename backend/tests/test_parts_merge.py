# -*- coding: utf-8 -*-
"""阶段① / 阶段② 解析与净化单测：模型输出的容错是质量生命线。"""
import json

from engine.merge import Vocab, apply_map, parse_vocab
from engine.parts import (PartGuess, build_parts_prompt, collapse_repeat,
                          parse_part_guess, sanitize_part, sanitize_word)


# ---- 词净化 ----

def test_sanitize_word_strips_illegal_and_space():
    assert sanitize_word('耳 机:1') == "耳机1"


def test_sanitize_part_cuts_at_member_boundary():
    out = sanitize_part("包装盒、说明书、挂绳、读卡器")
    # 整体上限 60：全部成员都放得下
    assert out == "包装盒、说明书、挂绳、读卡器"


def test_sanitize_part_dedupes_members():
    assert sanitize_part("充电盒、充电盒") == "充电盒"


def test_collapse_repeat_exact_half():
    assert collapse_repeat("正面正面") == "正面"
    assert collapse_repeat("正面正面正面正面") == "正面"
    assert collapse_repeat("充电盒正面") == "充电盒正面"   # 非精确对半不塌缩


# ---- parse_part_guess 容错 ----

def test_parse_plain_json():
    g = parse_part_guess(json.dumps({"part": "充电盒", "info": "正面", "conf": 0.9},
                                    ensure_ascii=False))
    assert g.part == "充电盒" and g.info == "正面" and not g.needs_review


def test_parse_json_in_code_fence():
    raw = "```json\n{\"part\": \"耳机\", \"info\": \"\", \"conf\": 0.9}\n```"
    assert parse_part_guess(raw).part == "耳机"


def test_parse_truncated_json_salvages_part_field():
    raw = '{"part":"收纳盒、收纳盒'
    g = parse_part_guess(raw)
    assert g.part == "收纳盒" and g.needs_review


def test_parse_no_json_falls_back_sanitized():
    g = parse_part_guess("这好像是一个包装盒")
    assert g.needs_review
    assert g.part != ""                       # 有兜底词，绝不空


def test_parse_empty_response_is_review():
    g = parse_part_guess("")
    assert g.needs_review and g.part == "配件或细节"


def test_parse_part_as_list_gets_repaired():
    # 模型把 part 输出成数组：修复成「、」拼接并标记待复核
    g = parse_part_guess('{"part": ["包装", "配件"], "conf": 0.9}')
    assert g.part == "包装、配件" and g.needs_review


def test_parse_low_confidence_flags_review():
    g = parse_part_guess('{"part": "保护套", "conf": 0.3}')
    assert g.needs_review


def test_parse_empty_part_falls_back():
    g = parse_part_guess('{"part": "", "conf": 0.9}')
    assert g.part == "配件或细节" and g.needs_review


# ---- parse_vocab 容错 ----

def test_parse_vocab_happy_path():
    raw = json.dumps({
        "canon": ["耳机", "充电盒"],
        "map": {"蓝牙耳机": "耳机", "耳机主体": "耳机", "充电盒": "充电盒"},
        "split": {},
    }, ensure_ascii=False)
    v = parse_vocab(raw, ["蓝牙耳机", "充电盒"])
    assert not v.degraded
    assert v.map["蓝牙耳机"] == "耳机"


def test_parse_vocab_rejects_map_value_outside_canon():
    raw = json.dumps({"canon": ["耳机"], "map": {"a": "不存在的词"}}, ensure_ascii=False)
    v = parse_vocab(raw, ["a"])
    assert "a" in v.canon                     # 兜底并入 canon
    assert "a" not in v.map


def test_parse_vocab_split_requires_two_members():
    raw = json.dumps({"canon": ["充电线"], "split": {"充电线": ["USB-C"]}}, ensure_ascii=False)
    v = parse_vocab(raw, ["充电线"])
    assert "充电线" not in v.split            # 只有 1 个成员说明没真拆


def test_parse_vocab_degraded_on_broken_json():
    v = parse_vocab("前缀解释 {\"canon\": [\"耳", ["耳机", "充电盒"])
    assert v.degraded
    assert set(v.canon) == {"耳机", "充电盒"}  # 退回恒等词表（含兜底）


def test_parse_vocab_salvages_canon_from_truncated_output():
    raw = '{"canon": ["耳机", "充电盒"], "map": {"蓝牙'
    v = parse_vocab(raw, ["数据线"])
    assert v.degraded
    assert "耳机" in v.canon and "数据线" in v.canon


# ---- apply_map ----

def test_apply_map_merges_and_flags_split():
    vocab = Vocab(canon=["耳机", "充电线A", "充电线B"],
                  map={"蓝牙耳机": "耳机"},
                  split={"充电线": ["充电线A", "充电线B"]})
    gs = [PartGuess(part="蓝牙耳机"), PartGuess(part="充电线")]
    out = apply_map(gs, vocab)
    assert out[0].part == "耳机" and not out[0].needs_review
    assert out[1].part == "充电线" and out[1].needs_review   # split 命中必须人看


def test_apply_map_unknown_word_flags_review():
    vocab = Vocab(canon=["耳机"], map={})
    out = apply_map([PartGuess(part="神秘零件")], vocab)
    assert out[0].needs_review


# ---- 提示词 ----

def test_parts_prompt_contains_folder_name():
    p = build_parts_prompt("韶音耳机")
    assert "韶音耳机" in p
    assert '"part"' in p                      # JSON schema 在场
