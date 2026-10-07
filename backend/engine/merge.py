# -*- coding: utf-8 -*-
"""阶段② 文件夹级归并：把逐图取到的候选词整理成同目录唯一的规范词表。

移植自「商品图片批量重命名工具」`_rename/merge.py`，逐字保留。

为什么要单独一段
    阶段① 只看单图，同一个部件会被叫成不同名字（蓝牙耳机 / 耳机 / 耳机主体），
    不同部件也可能被叫成同一个名字。本阶段用纯文本调用（不带图）做同义合并、
    唯一性校验、同名不同物的拆分标记。

为什么用同一个模型而不是另起小模型
    换模型会触发显存换入换出，单图耗时从 2s 劣化到 70s。用 qwen3-vl:8b 跑纯文本即可。
"""
import json
import re
from dataclasses import dataclass, field

from .parts import PartGuess, part_list, sanitize_part

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")
# 从被截断的归并输出里抢救 canon 数组（整体 JSON 解析失败时的部分可用结果）。
_CANON_ARRAY = re.compile(r'"canon"\s*:\s*(\[[^\]]*\])')


@dataclass
class Vocab:
    canon: list = field(default_factory=list)
    map: dict = field(default_factory=dict)
    split: dict = field(default_factory=dict)
    # 模型有输出、但没能解析出可用 JSON 对象：词表退回恒等，同义合并这一职责落空。
    degraded: bool = False


def _salvage_canon(raw):
    """从被截断的归并输出里抢救 canon 数组。只抢救 canon：map/split 依赖完整结构。"""
    m = _CANON_ARRAY.search(str(raw))
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return list(dict.fromkeys(part_list(arr)))


def parse_vocab(raw, fallback_parts):
    """解析归并结果。任何异常都退回"恒等词表"，保证流水线不中断。

    校验规则：
      - canon 去重并保序
      - map 的值必须落在 canon 内，否则丢弃该条
      - split 的值必须至少 2 个，否则丢弃该条（1 个说明模型没真拆）
      - fallback_parts（阶段① 的原始词）兜底并入 canon，避免有图无词
    """
    base = list(dict.fromkeys(part_list(fallback_parts)))
    if not raw:
        return Vocab(canon=base)

    m = _JSON_BLOCK.search(str(raw))
    d = None
    if m:
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            d = None
    if not isinstance(d, dict):
        canon = _salvage_canon(raw)
        for w in base:
            if w not in canon:
                canon.append(w)
        return Vocab(canon=canon or base, degraded=True)

    canon = list(dict.fromkeys(part_list(d.get("canon"))))

    mp = {}
    raw_map = d.get("map")
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            k2, v2 = sanitize_part(k), sanitize_part(v)
            if k2 and v2 and v2 in canon:
                mp[k2] = v2

    sp = {}
    raw_split = d.get("split")
    if isinstance(raw_split, dict):
        for k, v in raw_split.items():
            k2 = sanitize_part(k)
            if not k2 or k2 not in canon:
                continue
            vals = part_list(v)
            if len(vals) >= 2:
                sp[k2] = vals

    for w in base:
        if w not in canon and w not in mp:
            canon.append(w)
    return Vocab(canon=canon, map=mp, split=sp)


_MERGE_PROMPT = (
    "下面是同一个商品目录「{folder}」里出现过的初步部件命名（已按名字去重，附带出现次数）。\n"
    "请整理成一份规范词表，只输出一个 JSON，不要任何解释：\n"
    '{{"canon":["规范名1","规范名2"],'
    '"map":{{"原始名":"规范名"}},'
    '"split":{{"规范名":["限定名1","限定名2"]}}}}\n'
    "规则：\n"
    "1) 同一个部件的不同叫法必须合并，例如 蓝牙耳机/耳机主体 -> 耳机，合并关系写进 map。\n"
    "2) canon 内每个名字必须唯一，且都是短名词。\n"
    "3) 只有当你确信同一个名字其实对应两个不同实物时，才写进 split 并给出带限定的名字，"
    "例如 充电线 -> [USB-C充电线, 闪电充电线]。没有就留空对象。\n"
    "4) map 的键是**去重后**的原始名：同一个名字只写一条，绝不重复键。"
    "每个不同的原始名都要覆盖到（包括与规范名相同的）。\n"
    "\n初步命名（每行一个，格式：部件名 | kind=类型 [| members=成员] | 出现N次）：\n{listing}\n"
)


def build_merge_prompt(folder, guesses):
    """把逐图候选词整理成**去重**词表喂给归并模型。

    必须去重：实测把含大量重复名的逐图列表喂进去时，模型会在 JSON 对象里写出重复键
    并陷入退化重复循环，吃光任意配额后被截断，整份词表退回恒等词表。
    """
    counts, kinds, mem = {}, {}, {}
    for g in guesses:
        counts[g.part] = counts.get(g.part, 0) + 1
        bucket = kinds.setdefault(g.part, [])
        if g.kind not in bucket:
            bucket.append(g.kind)
        if g.members:
            mb = mem.setdefault(g.part, [])
            for m in g.members:
                if m not in mb:
                    mb.append(m)
    lines = []
    for part, n in counts.items():
        extra = (" | members=" + "、".join(mem[part])) if part in mem else ""
        lines.append(f"{part} | kind={'/'.join(kinds[part])}{extra} | 出现{n}次")
    return _MERGE_PROMPT.format(folder=folder, listing="\n".join(lines))


def apply_map(guesses, vocab):
    """把 map 应用到每张图，产出规范部件名。

    三种需要人工复核的情形：
      - 命中 split：同名其实对应不同实物，限定词要人定
      - 规范名不在 canon：阶段② 没覆盖到这个词
      - 阶段① 已经标了待复核：不能在这里丢掉
    """
    out = []
    for g in guesses:
        needs = g.needs_review
        if g.part in vocab.split:
            name = g.part
            needs = True
        else:
            name = vocab.map.get(g.part, g.part)
            if name not in vocab.canon:
                name = g.part
                needs = True
        out.append(PartGuess(part=name, info=g.info, kind=g.kind,
                             members=list(g.members), conf=g.conf,
                             needs_review=needs))
    return out


# 归并只走预填（prefill_only）：归并思考链无界，3072/8192 实测全被吃光，预填 2.5s
# 就给出逐字相同的词表。预填也接不上就抛 VlmError，由流水线记 merge_failed。
MERGE_PREFILL = '{"canon":["'
MERGE_NUM_PREDICT = 2048


def consolidate(client, folder, guesses):
    """调模型做文件夹级归并。client 需实现 generate(prompt, prefill=...) -> str。

    空响应时 generate 抛 VlmError——**不要在这里吞掉它**：静默退回恒等词表会让
    同目录出现重名部件却不被察觉。让异常上抛，由流水线按目录记为待复核。
    """
    raw = client.generate(build_merge_prompt(folder, guesses),
                          num_predict=MERGE_NUM_PREDICT,
                          prefill=MERGE_PREFILL, prefill_only=True)
    return parse_vocab(raw, [g.part for g in guesses])
