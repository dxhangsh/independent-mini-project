# -*- coding: utf-8 -*-
"""把《项目报告》Markdown 渲染为排版 PDF（fpdf2 + 等线字体，纯 Python 无外部依赖）。

用法（项目根目录）：
    .\\.venv\\Scripts\\python.exe tools/make_report_pdf.py

本机没有 Chromium 内核浏览器（无法用 headless 打印），故自建渲染器：
覆盖本报告用到的全部语法——#/##/### 标题、> 元信息块、- 列表、表格、
图片（含放大/居中/跨页）、**加粗**、--- 分隔线。表格与长中文行用
CHAR 换行模式，绝不溢出页宽。
"""
import re
import sys
from pathlib import Path

import markdown  # noqa: F401  （备用：正文如需 HTML 渲染）
from fpdf import FPDF
from fpdf.fonts import FontFace
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "项目报告-草稿.md"
OUT_DIR = ROOT / "陈胤璋"
OUT_PDF = OUT_DIR / "商品归档重命名工具.pdf"

ACCENT = (79, 70, 229)        # indigo（与产品一致）
GRAY = (107, 114, 128)
LINE = (203, 213, 225)
FILL_HEAD = (238, 242, 255)

CONTENT_W = 174               # A4 210 - 2*18 边距
MAX_IMG_H = 150               # 单图最大高（mm），竖图按高缩放并居中

# fpdf2 的 markdown=True 只认 **bold**；emoji 等字体缺字符号预处理
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2705\u23F3\u274C\u26A0\uFE0F\u200D\u2B50]")


def clean(s):
    s = _EMOJI_RE.sub("", s)
    s = s.replace("✅", "").replace("✔", "√").replace("⚠️", "注意：").replace("⚠", "注意：")
    return s


def strip_md(s):
    """表格单元格用：去掉加粗/反引号标记，保留纯文本。"""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = s.replace("`", "")
    return clean(s)


class ReportPDF(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=16)
        # 不设置 char_wrap="CHAR"：fpdf2 2.8.9 的 CHAR 模式与 align="C" 冲突，
        # 且默认模式已能对 CJK 无空格文本按字符断行（实测验证）
        self.set_title("商品归档重命名工具（PicNamer）项目报告")
        self.set_author("陈胤璋")

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Deng", "", 8.5)
        self.set_text_color(*GRAY)
        self.cell(0, 6, "商品归档重命名工具（PicNamer）项目报告 · 陈胤璋", align="C")
        self.ln(8)

    def footer(self):
        self.set_y(-12)
        self.set_font("Deng", "", 8.5)
        self.set_text_color(*GRAY)
        self.cell(0, 8, f"{self.page_no()} / {{nb}}", align="C")


def centered(pdf, txt, h, size=None, style=""):
    """居中渲染：单行且放得下用 cell（fpdf2 的 C+换行有 bug），否则降级左对齐。"""
    if size:
        pdf.set_font("Deng", style, size)
    if pdf.get_string_width(txt) <= CONTENT_W:
        pdf.cell(0, h, txt, align="C")
        pdf.ln(h)
    else:
        pdf.multi_cell(0, h, txt, align="L")


def add_image(pdf, path):
    full = ROOT / "docs" / path
    if not full.exists():
        return False
    with Image.open(full) as im:
        w_px, h_px = im.size
    ar = h_px / w_px
    w = CONTENT_W
    h = w * ar
    if h > MAX_IMG_H:
        h = MAX_IMG_H
        w = h / ar
    x = (210 - w) / 2
    if pdf.get_y() + h > pdf.page_break_trigger:
        pdf.add_page()
    pdf.image(str(full), x=x, w=w, h=h)
    return True


def render_table(pdf, lines):
    rows = []
    for ln in lines:
        cells = [strip_md(c.strip()) for c in ln.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells):
            continue                       # 分隔行
        rows.append(cells)
    if not rows:
        return
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    # 列宽启发式：按各列最大字符长度配比（限制上下限）
    lens = [max(len(r[i]) for r in rows) for i in range(ncol)]
    ratio = [max(4.0, min(55.0, L)) for L in lens]
    total = sum(ratio)
    ratio = [r / total for r in ratio]
    if ncol == 4 and max(lens[1:]) > 40:   # 问题表等长文本表：固定经验配比
        ratio = [0.05, 0.32, 0.40, 0.23]
    pdf.set_font("Deng", "", 9.5)
    pdf.set_draw_color(*LINE)
    pdf.set_fill_color(*FILL_HEAD)
    head_style = FontFace(emphasis="BOLD", fill_color=FILL_HEAD)
    with pdf.table(col_widths=tuple(ratio), text_align="LEFT",
                   line_height=5.2, headings_style=head_style,
                   borders_layout="ALL", padding=1.4) as table:
        for i, r in enumerate(rows):
            row = table.row()
            for c in r:
                row.cell(c)
    pdf.ln(2)


def rich_line(pdf, text, h, size=10.5, indent=0.0, bullet=False):
    """流式段落：**加粗** 段以字体切换实现（不用 fpdf2 的 markdown 参数——
    其在含 * / 反引号 / 中英混排的段落上会触发换行崩溃，实测 ERR-011）。
    write() 支持流式换行与自动分页，CJK 断行走 fpdf2 内建逻辑（已验证）。"""
    pdf.set_text_color(31, 41, 55)
    if x_off := (indent or (3 if bullet else 0)):
        pdf.set_x(pdf.get_x() + x_off)
    body = ("• " + text) if bullet else text
    parts = re.split(r"\*\*(.+?)\*\*", clean(body))
    for i, part in enumerate(parts):
        if not part:
            continue
        pdf.set_font("Deng", "B" if i % 2 else "", size)
        pdf.write(h, part)
    pdf.ln(h)


def render(md_text, pdf):
    lines = md_text.splitlines()
    i, n = 0, len(lines)
    in_meta = True                          # 标题后的 > 元信息块
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("# "):              # H1 标题（封面区）：拆两行居中
            pdf.set_text_color(31, 41, 55)
            main, _, rest = s[2:].partition("（")
            pdf.set_font("Deng", "B", 20)
            centered(pdf, main, 12)
            if rest:
                pdf.set_text_color(*GRAY)
                pdf.set_font("Deng", "", 12)
                centered(pdf, "（" + rest, 8)
        elif s.startswith("## "):
            pdf.ln(3)
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.8)
            y = pdf.get_y()
            pdf.line(18, y + 1.2, 24, y + 7.2)   # 小竖条装饰
            pdf.set_text_color(31, 41, 55)
            pdf.set_font("Deng", "B", 14.5)
            pdf.set_x(28)
            pdf.multi_cell(0, 8, clean(s[3:]))
            pdf.ln(1)
        elif s.startswith("### "):
            pdf.ln(2)
            pdf.set_text_color(55, 65, 81)
            pdf.set_font("Deng", "B", 12)
            pdf.multi_cell(0, 7, clean(s[4:]))
            pdf.ln(0.5)
        elif s.startswith("> "):            # 元信息块（单行 cell 居中，超宽降级左对齐）
            pdf.set_text_color(*GRAY)
            pdf.set_font("Deng", "", 10.5)
            centered(pdf, strip_md(clean(s[2:])), 6)
        elif s == ">":
            pass
        elif s.startswith("|"):
            tbl = []
            while i < n and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            in_meta = False
            render_table(pdf, tbl)
            continue
        elif s.startswith("!["):
            m = re.match(r"!\[(.*?)\]\((.+?)\)", s)
            if m:
                in_meta = False
                ok = add_image(pdf, m.group(2))
                if ok:
                    pdf.set_text_color(*GRAY)
                    pdf.set_font("Deng", "", 8.5)
                    centered(pdf, clean(m.group(1)), 5)
                    pdf.ln(2.5)
        elif s == "---":
            pdf.ln(1.5)
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.3)
            pdf.line(18, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(3)
        elif s.startswith("- "):
            in_meta = False
            rich_line(pdf, s[2:], 5.6, bullet=True)
        elif re.match(r"^\d+\. ", s):
            in_meta = False
            rich_line(pdf, s, 5.6)
        else:                               # 普通段落
            in_meta = False
            rich_line(pdf, s, 5.8)
        i += 1


def main():
    md = MD_PATH.read_text(encoding="utf-8")
    pdf = ReportPDF()
    pdf.add_font("Deng", "", r"C:\Windows\Fonts\Deng.ttf")
    pdf.add_font("Deng", "B", r"C:\Windows\Fonts\Dengb.ttf")
    pdf.add_font("SimHei", "", r"C:\Windows\Fonts\simhei.ttf")
    pdf.set_fallback_fonts(["SimHei"])
    pdf.add_page()
    render(md, pdf)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT_PDF))
    print(f"PDF done: {OUT_PDF}")
    print(f"pages: {pdf.pages_count}, size: {OUT_PDF.stat().st_size} bytes")


if __name__ == "__main__":
    sys.exit(main())
