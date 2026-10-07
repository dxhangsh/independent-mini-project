# -*- coding: utf-8 -*-
"""阶段① 逐图取词：让视觉模型说出画面里的部件名。

移植自「商品图片批量重命名工具」`_rename/parts.py`，提示词与解析逻辑逐字保留。

设计要点
    - 部件词是开放词表，不预设封闭集合（原规格 §5）。
    - 本阶段只对单图负责，不做跨图区分；"两个同类部件要不要加限定词"交给阶段②。
    - 模型输出必须能容错解析：它常把 JSON 包在 ```json 围栏里，或前后带解释文字。
"""
import re
from dataclasses import dataclass, field

KINDS = ("single", "multi", "overview", "showcase", "scene")
MAX_WORD_LEN = 12
# 多部件名（`、` 拼接）的整体上限。单个词仍按 MAX_WORD_LEN 掐，这里只限制成员个数，
# 裁剪落在「、」边界上，绝不把末位部件切一半（见 sanitize_part）。
MAX_PART_LEN = 60
FALLBACK_WORD = "配件或细节"
CONF_THRESHOLD = 0.5
# 取词提示词会诱导模型产出长思考链（实测 4900+ 字符），配额不足会吃光额度、
# JSON 未输出即被截断，整张丢失。3072 覆盖绝大多数，跑飞的那些由 PARTS_PREFILL 兜底。
NUM_PREDICT = 3072
# 兜底：空响应时改用 /api/chat 预填这个 JSON 开头，模型跳过思考链直接续写。
PARTS_PREFILL = '{"part":"'

# 含全部控制字符（\x00-\x1f 覆盖 \r\n\t）：控制字符留在文件名里会导致落盘失败。
# 花括号也在此列：模型偶发输出**未闭合**的 JSON，整段兜底当词名时 `{` 会漏进文件名。
_ILLEGAL = re.compile(r'[\\/:*?"<>|{}\x00-\x1f]')
_WS = re.compile(r"[\s\u3000]+")


def sanitize_word(s, max_len=MAX_WORD_LEN):
    """去掉非法字符与空白，掐到 max_len。空输入返回空串。

    只接受字符串。非字符串一律返回空串——`str()` 兜底会把 `["包装","配件"]`
    变成 `"['包装','配件']"`，看着像合法部件名，实则污染文件名。
    """
    if not isinstance(s, str) or not s:
        return ""
    s = _ILLEGAL.sub("", s)
    s = _WS.sub("", s)
    s = s.strip("-—")
    if max_len and len(s) > max_len:
        s = s[:max_len]
    return s


def sanitize_part(s, max_len=MAX_PART_LEN):
    """部件名净化：逐词掐长后按「、」边界整体裁剪。

    为什么不能直接 `sanitize_word`：multi 场景的 part 是多个词用「、」拼起来的，
    整体按 12 字掐会把末位部件切一半。宁可少列一个成员，也不留半个词。
    """
    if not isinstance(s, str) or not s:
        return ""
    words = dedupe_words([w for w in (sanitize_word(x) for x in s.split("、")) if w])
    out = []
    for w in words:
        if out and max_len and len("、".join(out + [w])) > max_len:
            break
        out.append(w)
    return "、".join(out)


def word_list(x):
    """把模型可能给的任意形态收敛成干净词列表（只认字符串与字符串序列）。"""
    if isinstance(x, str):
        x = [x]
    if not isinstance(x, (list, tuple)):
        return []
    return [w for w in (sanitize_word(v) for v in x) if w]


def part_list(x):
    """同 `word_list`，但按**部件名**口径净化：多部件名不整体掐到 12 字。

    归并阶段的 canon / map / split 里存的都是部件名，可能是「、」拼接的多部件名；
    若按单字上限掐，阶段① 修好的长名会在阶段② 被重新切碎。
    """
    if isinstance(x, str):
        x = [x]
    if not isinstance(x, (list, tuple)):
        return []
    return [w for w in (sanitize_part(v) for v in x) if w]


def dedupe_words(words):
    """保序去重（模型会把同一部件重复写两遍，带进文件名是噪声）。"""
    return list(dict.fromkeys(words))


def collapse_repeat(s):
    """把模型偶发重复的整串塌回一半：`正面正面` -> `正面`。

    按**精确对半相等**塌缩，不做模糊匹配，避免误伤正常词。
    """
    n = len(s)
    while n and n % 2 == 0 and s[:n // 2] == s[n // 2:]:
        s = s[:n // 2]
        n = len(s)
    return s


@dataclass
class PartGuess:
    part: str = ""
    info: str = ""
    kind: str = "single"
    members: list = field(default_factory=list)
    conf: float = 0.0
    needs_review: bool = False

    def normalized(self):
        """归一化：非法字符、kind 合法性、空部件名兜底、低置信标记。

        part 只接受字符串：模型在 multi 场景常把 part 输出成数组，这种输出会被
        "修复"成「、」拼接并标复核——修复过就说明模型没按契约输出。
        """
        kind = self.kind if self.kind in KINDS else "single"
        repaired = not isinstance(self.part, str)
        part = sanitize_part(self.part)
        members = dedupe_words(word_list(self.members))
        if repaired:
            members = dedupe_words(members + word_list(self.part))
        if not part and members:
            part = sanitize_part("、".join(members))
        try:
            conf = float(self.conf)
        except (TypeError, ValueError):
            conf = 0.0
        needs = bool(self.needs_review) or repaired or not part or conf < CONF_THRESHOLD
        if not part:
            part = FALLBACK_WORD
        return PartGuess(part=part, info=collapse_repeat(sanitize_word(self.info)), kind=kind,
                         members=members, conf=conf, needs_review=needs)


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")
# 未闭合 JSON 的救援：模型偶发在 `part` 值之后被截断（缺右花括号）。右引号可选：
# 截断点可能落在值末尾的引号之前，要求闭合引号会匹配不到。
_PART_FIELD = re.compile(r'"part"\s*:\s*"([^"]*)"?')


def parse_part_guess(raw):
    """把模型输出解析成 PartGuess。任何解析失败都走兜底并标记待复核，绝不抛异常。"""
    if not raw or not str(raw).strip():
        return PartGuess(needs_review=True).normalized()

    m = _JSON_BLOCK.search(str(raw))
    if not m:
        pm = _PART_FIELD.search(str(raw))
        if pm:
            return PartGuess(part=pm.group(1), needs_review=True).normalized()
        return PartGuess(part=sanitize_part(raw), needs_review=True).normalized()

    import json
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return PartGuess(needs_review=True).normalized()
    if not isinstance(d, dict):
        return PartGuess(needs_review=True).normalized()

    members = d.get("members") or []
    if not isinstance(members, list):
        members = [members]
    return PartGuess(
        part=d.get("part") or "",
        info=d.get("info") or "",
        kind=d.get("kind") or "single",
        members=list(members),
        conf=d.get("conf", 0.0),
    ).normalized()


# 提示词经 2026-09-27 韶音 39 图 + 充电宝配件 9 图等多轮 A/B 实测打磨（详见原项目
# out/ab_c.json 与提示词演进注释），逐字保留：
#   v2 加入「按实物形态判断、勿依赖画面文字」与「三种盒子须分清」（错误 6 胜 1 负）；
#   v3 补壳体类判据与 快递包装/收纳盒/收纳袋 词（4 改对 1 回退）。
_PARTS_PROMPT = (
    "这是一件已购商品的实拍图，商品目录名是「{folder}」。\n"
    "请判断画面里是什么部件，只输出一个 JSON，不要任何解释：\n"
    '{{"part":"部件名","info":"视角或页面或序号","kind":"single|multi|overview|showcase|scene",'
    '"members":[],"conf":0.9}}\n'
    "规则：\n"
    "1) part 用短名词，写画面里实际是什么物体，例如 耳机、充电盒、保护套、充电线、包装盒、"
    "快递纸箱、快递包装、说明书、保修卡、收纳盒、墨盒仓、钢化膜、电源适配器。\n"
    "2) 按实物的形状、材质、开合方式判断，不要依据画面里的印刷文字、快递面单、条形码内容命名"
    "——面单或包装上印着「耳机」「保护套」「充电宝」只是商品名称，不代表画面里就是这个物体。\n"
    "3) 盒子与包装必须分清：\n"
    "   - 充电盒：有铰链盖、内腔可装耳机或设备的硬质小盒，椭圆或圆角方形，可握在手中；\n"
    "   - 包装盒：商品的外包装纸盒，有品牌印刷或塑封膜，更扁平更大；\n"
    "   - 快递纸箱：牛皮纸色瓦楞纸箱，贴有快递面单，用于运输；\n"
    "   - 快递包装：塑料快递袋或气泡袋（银色/白色软塑料袋，贴面单），用于运输。\n"
    "4) 壳体类必须分清：\n"
    "   - 保护套：包裹整机的软壳或硬壳（透明硅胶壳、布套、皮质套），无内腔、无铰链，形状贴合被包物；\n"
    "   - 充电线：线缆本体，有线身与插头；\n"
    "   - 夹子：有夹持或卡扣结构的小件；\n"
    "   - 收纳盒/收纳袋：布面或网面、有拉链或开口的袋子或盒子，用于收纳。\n"
    "5) 纸质文档类写 说明书 / 用户指南 / 保修卡，不要写成盒子。\n"
    "6) 画面是整机本身时，part 写商品品类名，例如 耳机、打印机、平板、路由器。\n"
    "7) 多个部件同框：kind=multi，part 用「、」连接各部件，members 列出它们。\n"
    "8) 所有部件整齐陈列：kind=overview。美化摆拍或宣传图：kind=showcase。真实使用场景：kind=scene。\n"
    "9) 严禁使用 商品主体、配件或细节 这类笼统词——除非你确实无法判断。\n"
    "10) info 只填 正面/背面/侧面/接口面/内部/顶部/底部/打开/封面/展开/细节/页01 这类词，没有就填空串。\n"
    "11) conf 是 0 到 1 的置信度；仅凭外形难以区分（如各种壳体、盒、袋）时，如实给低于 0.5 的置信度。\n"
)


def build_parts_prompt(folder):
    return _PARTS_PROMPT.format(folder=folder)


def guess_parts(client, image_path, folder):
    """调模型对单图取词。client 需实现 classify(path, prompt) -> {"response": str}。"""
    r = client.classify(str(image_path), build_parts_prompt(folder),
                        num_predict=NUM_PREDICT, prefill=PARTS_PREFILL)
    return parse_part_guess(r["response"])
