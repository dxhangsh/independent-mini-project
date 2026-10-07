# -*- coding: utf-8 -*-
"""落盘：把 FolderPlan 的建议名真正写到磁盘，带 journal 与逆序回滚。

移植自「商品图片批量重命名工具」`_rename/apply.py`，逐字保留，两处改动：
  - 【PicNamer 新增】`plan_ops` 尊重 `Row.skip`：用户复核时跳过的行按 no-op 处理，
    final 为空的行同样按 no-op 处理（防止手工编辑把名字清空后落出 `.jpg`）。
  - 【PicNamer 新增】预检新增 `dst_duplicate`：批内两个操作改到同一个目标名
    （复核编辑可能引入；Windows 的 os.rename 会静默覆盖，必须零写入拦下）。

三条安全约束
    1. **预检零写入**：目标名被本批之外的既有文件占用、或两个名字互为占用成环、
       或批内互相同名时整目录拒绝，不做部分落盘。目标名只是被本批另一个
       **将要改名**的文件占用时不拒绝（resolve_order 会先腾空）。
    2. **写前日志**：每改一条**之前**先 append 一行 journal 并 fsync。
    3. **不覆盖**：Windows 的 `os.rename` 会静默覆盖目标文件，改名前再确认一次。
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

JOURNAL_NAME = "journal.jsonl"
HASH_ALGO = "sha256"
_CHUNK = 1 << 20

# I1 落盘阻断：只放真冲突——不同部件落到同一个文件名。人工在复核页确认后，
# 可用 accept_review=True 显式放行。
BLOCKING_ISSUE_KINDS = frozenset({"stem_collision"})


@dataclass
class Op:
    src: Path
    dst: Path
    noop: bool


@dataclass
class Conflict:
    src: str
    dst: str
    reason: str


@dataclass
class ApplyResult:
    plan_id: str
    renamed: int = 0
    noop: int = 0
    rolled_back: int = 0
    aborted: bool = False
    blocked: list = field(default_factory=list)
    accepted_review: bool = False
    conflicts: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class RollbackResult:
    plan_id: str
    restored: int = 0
    already: int = 0
    mismatched: list = field(default_factory=list)
    missing: list = field(default_factory=list)


def file_hash(path, algo=HASH_ALGO):
    """内容哈希，带算法前缀。用于回滚时校验文件没被外部改过。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return f"{algo}:{h.hexdigest()}"


def new_plan_id(now=None):
    """批次号。带随机后缀，避免同一秒内两次运行撞号导致回滚串批。"""
    now = now or datetime.now()
    return f"p-{now:%Y%m%d-%H%M%S}-{os.urandom(2).hex()}"


def _ts():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def plan_ops(plan):
    """把 FolderPlan 摊成逐条改名操作。`Row.final` 是 basename，与 `Row.src` 同目录。

    【PicNamer 新增】skip 行与 final 为空的行按 no-op 处理。
    """
    ops = []
    for r in plan.rows:
        src = Path(r.src)
        if getattr(r, "skip", False) or not r.final:
            ops.append(Op(src=src, dst=src, noop=True))
            continue
        dst = src.with_name(r.final)
        ops.append(Op(src=src, dst=dst, noop=src.name == dst.name))
    return ops


def resolve_order(ops):
    """把改名操作排成可安全执行的顺序：目标名被本批另一个**将要改名**的文件占用时，
    先让占用者腾空，再让后来者落位。

    返回 `(ordered, cycles)`：`cycles` 非空表示存在改名环，排序无解，调用方必须
    整目录拒绝（零写入）。
    """
    n = len(ops)
    src_idx = {}
    for i, o in enumerate(ops):
        if not o.noop:
            src_idx[str(o.src).lower()] = i
    deps = [set() for _ in range(n)]
    for i, o in enumerate(ops):
        if o.noop:
            continue
        j = src_idx.get(str(o.dst).lower())
        if j is not None and j != i:
            deps[i].add(j)                 # i 必须排在 j 之后：j 先腾出目标名
    ordered, done = [], set()
    ready = [i for i in range(n) if not deps[i]]
    while ready:
        ready.sort()
        i = ready.pop(0)
        ordered.append(i)
        done.add(i)
        for k in range(n):
            if i in deps[k]:
                deps[k].discard(i)
                if not deps[k] and k not in done and k not in ready:
                    ready.append(k)
    cycles = [i for i in range(n) if i not in done]
    return [ops[i] for i in ordered], cycles


def _preflight_ops(ops, cycles):
    """预检主体：返回**无法靠排序解决**的冲突。"""
    out = []
    if cycles:
        for i in cycles:
            o = ops[i]
            out.append(Conflict(str(o.src), str(o.dst), "rename_cycle"))
        return out
    # 【PicNamer 新增】批内同名：两个未跳过的操作改到同一个目标名（大小写不敏感）。
    # 流水线产出的 finals 保证唯一，但复核阶段的人工编辑可能制造重复；Windows 的
    # os.rename 会静默覆盖，靠"写前逐条确认"只能拦住执行到第二条时的情况并触发
    # 整批回滚——不如预检直接零写入拦下，提示也清晰。
    # 仅大小写不同的改名（A.jpg -> a.jpg）不算：它不产生新名字。
    by_dst = {}
    for o in ops:
        if o.noop or str(o.dst).lower() == str(o.src).lower():
            continue
        by_dst.setdefault(str(o.dst).lower(), []).append(o)
    for group in by_dst.values():
        if len(group) > 1:
            for o in group:
                out.append(Conflict(str(o.src), str(o.dst), "dst_duplicate"))
    by_src = {str(o.src).lower(): o for o in ops}
    for o in ops:
        if o.noop or not o.dst.exists():
            continue
        if str(o.dst).lower() == str(o.src).lower():
            continue                        # 仅大小写不同：同一个文件，合法改名
        other = by_src.get(str(o.dst).lower())
        if other is not None and not other.noop and other is not o:
            continue                        # 占用者本批会挪走，排序可解
        out.append(Conflict(str(o.src), str(o.dst), "dst_exists"))
    return out


def preflight(plan):
    """零写入预检，返回**无法靠排序解决**的冲突；空列表表示可安全落盘。

    拒绝三种物理风险：
      - `dst_exists`：目标名被本批之外的既有文件占用，或占用者是 no-op（不会挪走）；
      - `rename_cycle`：两个名字互为占用形成环（如两名互换），排序无解；
      - `dst_duplicate`：批内两个操作改到同一个目标名（复核编辑可能引入）。
    """
    ops = plan_ops(plan)
    _, cycles = resolve_order(ops)
    return _preflight_ops(ops, cycles)


def _rename(src, dst):
    """薄封装：测试用它注入中途失败，验证自动回滚。"""
    os.rename(src, dst)


def _append_journal(journal_path, entry):
    journal_path = Path(journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_journal(journal_path):
    """按行读 journal。文件不存在时抛 FileNotFoundError（调用方要能区分"没这批次"）。"""
    with open(journal_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _undo(done, journal_path, plan_id):
    """逆序把已改的还原回去，逐条记 journal。返回成功还原条数。"""
    n = 0
    for src, dst in reversed(done):
        try:
            _rename(dst, src)
        except OSError as e:
            _append_journal(journal_path, {
                "ts": _ts(), "op": "rollback", "plan_id": plan_id,
                "src": str(dst), "dst": str(src), "result": f"error: {e}",
            })
            continue
        _append_journal(journal_path, {
            "ts": _ts(), "op": "rollback", "plan_id": plan_id,
            "src": str(dst), "dst": str(src), "result": "ok",
        })
        n += 1
    return n


def blocking_issues(plan):
    """I1 落盘阻断：返回该目录的阻断性 issue（空列表表示可落盘）。"""
    return [it for it in getattr(plan, "issues", [])
            if getattr(it, "kind", None) in BLOCKING_ISSUE_KINDS]


def apply_plan(plan, journal_path, plan_id=None, progress=print, accept_review=False):
    """执行落盘。I1 未通过或预检不过则整目录拒绝（零写入）；中途异常自动逆序回滚。

    `accept_review=True` 表示人工已看过复核页并接受残留冲突，此时跳过 I1 阻断
    直接落盘。**预检仍然强制**——它拦的是会覆盖或搬错文件的物理风险。
    """
    plan_id = plan_id or new_plan_id()
    res = ApplyResult(plan_id=plan_id)

    blocked = blocking_issues(plan)
    if blocked:
        res.blocked = blocked
        if not accept_review:
            res.aborted = True
            progress(f"    [!] I1 未通过，整目录阻断落盘（零写入）：{len(blocked)} 项")
            for it in blocked:
                progress(f"        [{it.kind}] {it.detail}")
            return res
        res.accepted_review = True
        progress(f"    [!] 人工放行：忽略 {len(blocked)} 项 I1 阻断继续落盘")
        for it in blocked:
            progress(f"        [{it.kind}] {it.detail}")

    conflicts = preflight(plan)
    if conflicts:
        res.aborted = True
        res.conflicts = conflicts
        progress(f"    [!] 预检未通过，整目录拒绝落盘（零写入）：{len(conflicts)} 条冲突")
        for c in conflicts:
            progress(f"        {Path(c.src).name} -> {Path(c.dst).name}: {c.reason}")
        return res

    ops = plan_ops(plan)
    ordered, _ = resolve_order(ops)      # 预检已排除环，此处必有解
    done = []
    for o in ordered:
        if o.noop:
            res.noop += 1
            continue
        _append_journal(journal_path, {
            "ts": _ts(), "op": "rename", "plan_id": plan_id,
            "src": str(o.src), "dst": str(o.dst),
            "hash": file_hash(o.src), "size": o.src.stat().st_size,
            "result": "pending",
        })
        try:
            if o.dst.exists() and str(o.dst).lower() != str(o.src).lower():
                raise FileExistsError(f"目标已存在: {o.dst.name}")
            _rename(o.src, o.dst)
        except Exception as e:
            res.errors.append(f"{o.src.name}: {type(e).__name__}: {e}")
            progress(f"    [!] 改名失败，自动逆序回滚 {len(done)} 条：{e}")
            res.rolled_back = _undo(done, journal_path, plan_id)
            res.renamed = 0        # 已全部还原，本批次净改名为 0
            res.aborted = True
            return res
        done.append((o.src, o.dst))
        res.renamed += 1
    return res


def rollback(plan_id, journal_path, progress=print):
    """按 plan_id 逆序还原。还原前逐条校验内容哈希，不一致就跳过并报出来。"""
    res = RollbackResult(plan_id=plan_id)
    entries = [e for e in read_journal(journal_path)
               if e.get("op") == "rename" and e.get("plan_id") == plan_id]

    for e in reversed(entries):
        src, dst = Path(e["src"]), Path(e["dst"])
        if not dst.exists():
            if src.exists():
                res.already += 1          # 没改成，或已回滚过
            else:
                res.missing.append(str(dst))
            continue
        want = e.get("hash")
        if want and file_hash(dst) != want:
            res.mismatched.append(f"{dst.name}: 内容与落盘时不一致，拒绝还原")
            continue
        if src.exists():
            res.mismatched.append(f"{dst.name} -> {src.name}: 原名已被占用，拒绝还原")
            continue
        _rename(dst, src)
        _append_journal(journal_path, {
            "ts": _ts(), "op": "rollback", "plan_id": plan_id,
            "src": str(dst), "dst": str(src), "result": "ok",
        })
        res.restored += 1

    if res.mismatched or res.missing:
        progress(f"    [!] 回滚未完成：{len(res.mismatched)} 条拒绝、{len(res.missing)} 条丢失")
    return res
