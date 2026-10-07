# -*- coding: utf-8 -*-
"""阶段③ 规则后处理：文件名构建、长度控制、同名冲突消解、不变量校验。

移植自「商品图片批量重命名工具」`_rename/naming.py`，逐字保留。

全部是纯函数，不碰模型、不碰磁盘，可直接单测。
"""
import re
from dataclasses import dataclass

from .parts import FALLBACK_WORD

_ILLEGAL = re.compile(r'[\\/:*?"<>|{}\x00-\x1f]')
_MULTI_DASH = re.compile(r"-{2,}")
_WS = re.compile(r"\s+")

# Windows 保留设备名：不区分大小写，且只看扩展名之前的基名（`AUX.jpg` 的基名是 `AUX`）。
# 模型对音频线常输出 `AUX`，落成 `AUX.jpg` 在 Windows 上无法创建。
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CLOCK$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)
# 命中保留名时追加的安全后缀。仅追加，不改写原词，便于人工识别。
_RESERVED_SUFFIX = "-件"

# 泛化词：旧自动流程留下的笼统写法。整目录都落在这几个词上，就是没区分开。
GENERIC_PARTS = {FALLBACK_WORD, "配件", "商品主体", "细节", "其他", "未识别"}


@dataclass
class Issue:
    kind: str
    detail: str


def _is_reserved(name):
    """是否命中 Windows 保留设备名。只看扩展名之前的基名，不区分大小写。"""
    base = name.split(".", 1)[0].upper()
    return base in _WIN_RESERVED


def _avoid_reserved(name):
    """命中保留名就追加后缀使其可用；非保留名原样返回。

    后缀插在**基名之后**而不是整个名字末尾：Windows 只看第一个点之前的基名，
    `AUX.jpg-件` 的基名仍是 `AUX`，照样创建失败；`AUX-件.jpg` 才有效。
    """
    if not name or not _is_reserved(name):
        return name
    base, sep, rest = name.partition(".")
    return base + _RESERVED_SUFFIX + sep + rest


def sanitize_component(s):
    """文件名组件净化。

    顺序：非法字符换连字符 → 去空白 → 压缩连续连字符 → 从尾部剥掉 `.`/空格/连字符
    → 保留设备名兜底。尾部剥离是必须的：Windows 会静默丢弃结尾的 `.` 与空格，
    让"同名冲突守卫"失效。只接受字符串，非字符串一律返回空串。
    """
    if not isinstance(s, str) or not s:
        return ""
    s = _ILLEGAL.sub("-", s)
    s = _WS.sub("", s)
    s = _MULTI_DASH.sub("-", s)
    s = s.strip("-—. ")
    return _avoid_reserved(s)


def _cut_at_member(s, max_len):
    """把多部件名截到 max_len，裁剪落在「、」边界上，不留半个词。

    【移植修正】原项目此处为 `cut = s[:max_len]` + `not s.startswith(cut)` 判断，
    但 `cut` 恒为 `s` 的前缀，边界回退分支实际不可达（死代码），超长多部件名会
    留下半个词。本版改为按成员累积裁剪（与 `parts.sanitize_part` 同思路）；
    仅当首个成员自身超长时才硬切——路径长度上限是硬约束，必须兜住。
    """
    if not max_len or len(s) <= max_len:
        return s
    out = []
    for m in s.split("、"):
        if out and len("、".join(out + [m])) > max_len:
            break
        out.append(m)
    joined = "、".join(out)
    return joined if len(joined) <= max_len else joined[:max_len]


def build_stem(part, info, product_word=None, max_len=None):
    """拼 `[商品词-]部件-信息`。超长时按原规格 §4.4：先丢信息段，再丢商品段。

    截断发生在净化之后，可能重新引入非法尾部，也可能把兜底过的保留名切回去，
    所以末尾要再净化一次；此时安全优先于长度上限。空结果退回兜底词，绝不返回空串。
    """
    pw = sanitize_component(product_word)
    pt = sanitize_component(part)
    nf = sanitize_component(info)
    stem = "-".join(c for c in (pw, pt, nf) if c)
    if max_len and len(stem) > max_len:
        stem = "-".join(c for c in (pw, pt) if c)
    if max_len and len(stem) > max_len:
        stem = _cut_at_member(pt, max_len).rstrip("-") or pt
    stem = _avoid_reserved(stem.strip("-—. "))
    return stem or FALLBACK_WORD


def dedupe_stems(stems):
    """同目录内同名追加两位序号，绝不覆盖。首次出现保留原名。

    必须对"已产出的名字"查重，而不是对"原始名"计数。序号在候选被占用时继续递增。
    查重不区分大小写：Windows 文件名不区分大小写。输出的名字保留各自原始大小写。
    """
    used = set()
    out = []
    for s in stems:
        name = s
        i = 1
        while name.lower() in used:
            i += 1
            name = f"{s}-{i:02d}"
        used.add(name.lower())
        out.append(name)
    return out


def group_same_stems(stems):
    """按小写 stem 分组，返回 `{小写名: [下标...]}`，保序。"""
    groups = {}
    for i, s in enumerate(stems):
        groups.setdefault(s.lower(), []).append(i)
    return groups


def check_invariants(guesses):
    """自动守卫：抓两种劣化——兜底词泛滥、整目录被归并成一个**泛化词**。

    over_merge 只对泛化词报警：一个目录里 6 张图都叫「耳机」是正常的，
    但 29 张图都叫「配件」就是没区分开。
    """
    issues = []
    parts = [g.part for g in guesses]
    n = len(parts)
    if n == 0:
        return issues

    if any(not p for p in parts):
        issues.append(Issue("empty_part", "存在空部件名"))

    fb = sum(1 for p in parts if p == FALLBACK_WORD)
    if fb and fb / n > 0.2:
        issues.append(Issue("fallback_heavy", f"兜底词 {fb}/{n}"))

    uniq = set(parts)
    if n >= 5 and len(uniq) == 1 and next(iter(uniq)) in GENERIC_PARTS:
        issues.append(Issue("over_merge", f"{n} 张图同名泛化词「{next(iter(uniq))}」"))

    return issues
