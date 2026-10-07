# -*- coding: utf-8 -*-
"""编排：目录扫描 -> 阶段① 取词 -> 阶段② 归并 -> 阶段③ 后处理 -> FolderPlan。

移植自「商品图片批量重命名工具」`_rename/pipeline.py`，改动处均有注释标明：
  - imports 收敛为包内相对导入，`VlmError` 统一来自 `engine.errors`；
  - 【PicNamer 新增】`run_folder` 增加 `on_image` 进度钩子（Web 端实时进度）；
  - 【PicNamer 新增】`Row.skip` 标记（复核阶段用户选择跳过的行，落盘时不动）。

容错边界（单个目录的失败不允许拖垮整批）
    - 阶段① 单图异常：退回顾为兜底词并标待复核，继续跑下一张；失败张数记 `guess_failed`。
    - 阶段② 归并抛 `VlmError`：退回恒等词表并记 `merge_failed`，继续跑完本目录。
    - `Vocab.degraded` 为真：追加 `vocab_degraded`，让"同义合并落空"浮出到 issue。
    - 缺 `pillow-heif`：跳过 `.heic/.heif` 并记一条汇总 `heic_unsupported`。
    - 目录不存在：记 `folder_not_found`，不静默返回空 plan。
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import VlmError
from .merge import Vocab, apply_map, consolidate
from .naming import (Issue, build_stem, check_invariants, dedupe_stems,
                     group_same_stems)
from .parts import PartGuess, guess_parts

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
HEIC_EXTS = {".heic", ".heif"}
STEM_BUDGET = 180          # 单段文件名上限，给目录路径与扩展名留余量
REVIEW_RATIO = 0.2
# EXIF DateTimeOriginal / DateTime。EXIF 里的拍摄时间，用于 I2 的同名消歧排序。
EXIF_TAGS = (36867, 306)

MAX_PATH_BUDGET = 260      # Windows 默认 MAX_PATH 上限
MIN_STEM_BUDGET = 60       # 再挤也不把名字压得比这更短，否则失去区分度
EXT_SLACK = 6              # 最长扩展名 `.jpeg` 5 字符 + 1


@dataclass
class Row:
    src: str
    raw_part: str
    part: str
    info: str
    kind: str
    conf: float
    stem: str = ""
    final: str = ""
    needs_review: bool = False
    exif_ordinal: bool = False   # 本行按 EXIF 时间补了序号（I2 自动消解）
    skip: bool = False           # 【PicNamer 新增】用户复核时选择跳过（落盘不改名）


@dataclass
class FolderPlan:
    folder: str
    rows: list = field(default_factory=list)
    canon: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def review_count(self):
        return sum(1 for r in self.rows if r.needs_review)


def heic_supported():
    """本机是否具备 HEIC 解码能力（pillow-heif 是否可用）。"""
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


def stem_budget_for(folder):
    """按实际路径长度推导单段文件名上限（公式移植自原 CLI）。

    固定 180 在目录名很长时会与根路径叠加越过 Windows 260 上限，落盘必然失败。
    """
    room = MAX_PATH_BUDGET - len(str(folder)) - 1 - EXT_SLACK
    return max(MIN_STEM_BUDGET, min(STEM_BUDGET, room))


def image_time(path):
    """取图片拍摄时间（epoch 秒）用于同名消歧排序；取不到返回 None。

    回退链：EXIF DateTimeOriginal -> EXIF DateTime -> 文件修改时间 -> None。
    解码失败一律退回下一级，绝不抛异常拖垮整目录。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            exif = im.getexif()
            raw = next((exif.get(t) for t in EXIF_TAGS if exif.get(t)), None)
        if raw:
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:      # noqa: BLE001  解码/解析失败都退回 mtime
        pass
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _one_part(assigned, idxs):
    """同名组内是否只有一个部件名（大小写不敏感）。

    是 -> I2 的「同一部件多张图、信息词区分不开」，按 EXIF 时间补序号自动消解；
    否 -> 不同部件撞名（I1 真冲突），必须人看。
    """
    return len({assigned[i].part.lower() for i in idxs}) == 1


def scan_folder(folder):
    """列出目录下的图片文件，含隐藏属性文件；不递归子目录。

    【PicNamer 改动】扩展名集合加入 `.webp`（原库未出现，Web 场景常见）。
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir(), key=lambda x: x.name.lower())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS]


def run_folder(client, folder, product_word=None, progress=print, stem_budget=None,
               on_image=None):
    """跑完一个目录，返回 FolderPlan。不落盘，只产出建议名。

    【PicNamer 新增】`on_image(idx, total, path)`：每处理完一张图回调一次，
    供 Web 端展示实时进度；传 None 则无开销。
    """
    folder = Path(folder)
    if stem_budget is None:
        stem_budget = stem_budget_for(folder)
    plan = FolderPlan(folder=str(folder))
    if not folder.is_dir():
        plan.issues.append(Issue("folder_not_found", "目录不存在或不是目录"))
        return plan
    files = scan_folder(folder)
    if not files:
        return plan

    if not heic_supported():
        heic_files = [p for p in files if p.suffix.lower() in HEIC_EXTS]
        if heic_files:
            detail = f"跳过 {len(heic_files)} 个 HEIC 文件：未安装 pillow-heif"
            plan.issues.append(Issue("heic_unsupported", detail))
            progress(f"    [!] {detail}")
        files = [p for p in files if p.suffix.lower() not in HEIC_EXTS]
        if not files:
            return plan

    guesses = []
    failed = 0
    total = len(files)
    for i, p in enumerate(files):
        try:
            g = guess_parts(client, p, folder.name)
        except Exception as e:
            failed += 1
            progress(f"    [!] {p.name} 取词失败: {type(e).__name__}: {e}")
            g = PartGuess(needs_review=True).normalized()
        guesses.append(g)
        if on_image:
            on_image(i + 1, total, str(p))

    if failed:
        # 失败张数必须单独浮出来，否则会淹没在大量待复核里。
        plan.issues.append(Issue("guess_failed", f"{failed}/{len(files)} 张取词失败"))

    try:
        vocab = consolidate(client, folder.name, guesses)
    except Exception as e:
        if not isinstance(e, VlmError):
            raise
        # 归并失败不能让整批崩掉：退回恒等词表并标记，交由人工复核。
        progress(f"    [!] 归并失败: {type(e).__name__}: {e}")
        vocab = Vocab(canon=list(dict.fromkeys(g.part for g in guesses)), degraded=True)
        plan.issues.append(Issue("merge_failed", f"{type(e).__name__}: {e}"))

    plan.canon = list(vocab.canon)
    if vocab.degraded:
        plan.issues.append(Issue(
            "vocab_degraded", "归并词表退化（恒等词表），同义合并未生效"))

    assigned = apply_map(guesses, vocab)

    plan.issues.extend(check_invariants(assigned))
    stems = [build_stem(a.part, a.info, product_word, max_len=stem_budget) for a in assigned]

    # 与 dedupe_stems 同一口径：Windows 文件名不区分大小写。
    groups = group_same_stems(stems)
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    same = {k: v for k, v in dup.items() if _one_part(assigned, v)}
    # 真冲突（I1）：不同部件落到同一个 stem，落盘前会被整目录阻断。
    collided = {i for k, v in dup.items() if k not in same for i in v}
    auto = {i for v in same.values() for i in v}
    if collided:
        plan.issues.append(Issue(
            "stem_collision",
            f"不同部件落到同名，{len(collided)} 张待复核（落盘将整目录阻断）"))
    if auto:
        plan.issues.append(Issue(
            "exif_ordinal", f"同部件同信息词 {len(auto)} 张，按 EXIF 时间补序号"))

    # I2：同一部件多张图、信息词也区分不开时，按拍摄时间排先后补序号。
    times = {i: image_time(files[i]) for i in auto}
    order = []
    for key, idxs in groups.items():
        if key in same:
            idxs = sorted(idxs, key=lambda i: (times[i] is None, times[i] or 0.0, i))
        order.extend(idxs)

    named = dedupe_stems([stems[i] for i in order])
    finals = [""] * len(stems)
    for pos, i in enumerate(order):
        finals[i] = named[pos]
    ordinal = {i for i in auto if finals[i].lower() != stems[i].lower()}

    for i, (src, g, a, stem, fin) in enumerate(zip(files, guesses, assigned, stems, finals)):
        plan.rows.append(Row(
            src=str(src), raw_part=g.part, part=a.part, info=a.info, kind=a.kind,
            conf=a.conf, stem=stem, final=fin + src.suffix.lower(),
            needs_review=a.needs_review or i in collided,
            exif_ordinal=i in ordinal,
        ))

    if plan.rows and plan.review_count / len(plan.rows) > REVIEW_RATIO:
        plan.issues.append(Issue(
            "review_heavy", f"待复核 {plan.review_count}/{len(plan.rows)}"))
    return plan
