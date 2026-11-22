"""表格（门窗表、图纸目录）：写成 Bonsai Schedules 能读的 xlsx（出图用），并配同名 CSS；另写一份交付用的 xlsx。
只用 openpyxl，不用 bpy。字号等数值在约定包 conventions.json 的 schedule 一节。

Bonsai 的 Schedules（bonsai/bim/module/drawing/scheduler.py）把 xlsx 画成 SVG：
- 列宽按 Excel 的"字符数"换算成毫米（1 字符 ≈ 1.852mm），行高按磅（pt）换算；
- 字号 = 单元格字号 / --font-size-pt × --font-size-px（这两个变量写在同名 CSS 里）；
- 自动换行用 textwrap.wrap(width=列宽 // (字号 × --font-width), break_long_words=False)：按字符数、只在 ASCII 空格处断。
  默认 --font-width 0.45 是按西文估的，中文要设成 1.0；行高要按换行后的行数自己算好，Bonsai 不会撑高行；
- 合并单元格的宽度算法有问题（按首列宽乘列数），所以不用合并单元格，表名另外写在表上方。
同名 CSS 的样式会随表格嵌进整张图纸，写选择器时只针对表格自己的类（text.schedule、rect.border），
否则会改掉图框和图面的字体。

出图用的 xlsx 单元格里是"已经断好行、行与行之间用一个空格隔开"的文字（write_table）；交付用的 xlsx 是原文
（write_delivery：表头加粗、细边框、列宽按内容、冻结表头），两份分开写。
"""
from __future__ import annotations

import os
import re

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import conventions

_SC = conventions.get("schedule")
FONT_PT = float(_SC["font_pt"])     # 正文字号（pt），对应图纸上 FONT_MM
FONT_MM = float(_SC["font_mm"])     # 字身高（mm）：汉字约 3.2mm，数字约 2.5mm
ROW_PAD_MM = float(_SC["row_pad_mm"])   # 单元格上下留白合计
TITLE_FONT_MM = float(_SC["title_font_mm"])
NOTES_FONT_MM = float(_SC["notes_font_mm"])
CHAR_MM = 7 * 0.75 * 25.4 / 72     # Excel 列宽 1 个字符 = 1.852mm（Bonsai 的换算）
PT_MM = 25.4 / 72
_BREAK = tuple(_SC["breaks"])
_NBSP = " "                    # textwrap 只把 ASCII 空白当断点，原文里的空格换成它就不会在那里断开
_CODE_RUN = re.compile(r"[A-Za-z0-9甲乙丙×.\-]+")   # 编号、尺寸这类不宜拆开的串

CSS = """/* 由 jiazhuang.schedules 生成：Bonsai Schedules 的同名样式表 */
:root {{ --font-size-pt: {pt}; --font-size-px: {px}; --font-width: 1.0; }}
text.schedule, tspan.schedule {{ font-family: 'FangSong', '仿宋', 'STFangsong', 'Noto Serif CJK SC', serif; fill: #000; stroke: none; }}
rect.border {{ stroke: #000; stroke-width: 0.25; }}
"""


def text_mm(s: str, font_mm: float = FONT_MM) -> float:
    """估算一行字的宽度（mm）：汉字、全角符号按 1 个字身，ASCII 按 0.55。"""
    return sum((0.55 if ord(ch) < 128 else 1.0) * font_mm for ch in str(s))


def _split_long(tok: str, k: int) -> list[str]:
    """超过 k 个字又没有可断标点的串强制切开（每段 ≤ k 个字）：先找空格、"/""）"后面，再找编号串的边界，
    都没有就硬切在 k 处。"""
    out = []
    while len(tok) > k:
        cut = max((tok.rfind(ch, 0, k) + 1 for ch in (_NBSP, "/", "）", ")")), default=0)
        if cut < 2:
            runs = [m.span() for m in _CODE_RUN.finditer(tok)]
            inside = lambda i: any(a < i < b for a, b in runs)
            cut = next((i for i in range(k, 1, -1) if not inside(i)), k)
        out.append(tok[:cut])
        tok = tok[cut:]
    return out + ([tok] if tok else [])


def wrap_lines(s: str, k: int) -> list[str]:
    """把一段文字断成每行不超过 k 个字（按字符数，和 Bonsai 的 textwrap 一致）：只断在约定包 breaks 里的标点后面，
    原文的空格不断（"原图编号 FM乙1121"不拆开，空格换成不断开空格）；没有可断标点的长串强制切开，不横向出格。"""
    toks, cur = [], ""
    for ch in re.sub(r"[ \t\n\r\x0b\x0c ]+", _NBSP, str(s).strip()):
        cur += ch
        if ch in _BREAK or ch == "-":   # textwrap 也会断在连字符后面：这里切得一样细，保证 Bonsai 不并行
            toks.append(cur)
            cur = ""
    if cur:
        toks.append(cur)
    lines, line = [], ""
    for tok in toks:
        for piece in _split_long(tok, k):
            if line and len(line) + len(piece) > k:
                lines.append(line)
                line = ""
            if not line:
                piece = piece.lstrip(_NBSP)   # 行首不留空格（连续空格已合并成一个，前一行行尾不会同时去掉空格）
            line += piece
    if line:
        lines.append(line)
    return lines or [""]


def _wrapped(s: str, width_mm: float) -> tuple[str, int]:
    """出图用：按 Bonsai 的规则（每行 宽度 // 字身 个字、只在 ASCII 空格处断）先断好行，行与行之间用一个空格隔开，
    行内没有 ASCII 空格。每一行加上下一行（至少下一行的第一段）都超过 k 个字，所以 Bonsai 贪心排行时不会把两行
    并成一行（不会出现行内空格、编号被拆到两行），行数和这里算的一样，按这个行数定行高不会挤出格子。
    返回 (写进单元格的文字, 行数)。"""
    k = max(1, int(width_mm // FONT_MM))
    # 行尾的原文空格（不断开空格）去掉一个：少 1 个字，加上行间的 1 个空格，Bonsai 那边仍然并不上
    lines = [ln[:-1] if ln.endswith(_NBSP) and len(ln) > 1 else ln for ln in wrap_lines(s, k)]
    return " ".join(lines), len(lines)


def layout(head: list, rows: list, max_width_mm: dict | None = None, min_width_mm: float = 10.0):
    """列宽（mm）、需要换行的列、每行高度（mm，含表头）、处理过换行的行。write_table 和分页共用。"""
    max_width_mm = max_width_mm or {}
    widths, wrap_cols = [], set()
    for c in range(len(head)):
        need = max([text_mm(head[c])] + [text_mm(r[c]) for r in rows if r[c] not in (None, "")]) + 3.0
        if c in max_width_mm and need > max_width_mm[c]:
            # 换行列：宽度取"k 个汉字 + 3mm"，Bonsai 按 宽度 // 字身 断成 k 字一行，两边各留 1.5mm
            k = max(2, int((max_width_mm[c] - 3.0) // FONT_MM))
            widths.append(k * FONT_MM + 3.0)
            wrap_cols.add(c)
        else:
            widths.append(max(min_width_mm, need))
    heights, cooked = [FONT_MM + ROW_PAD_MM], [list(head)]
    for r in rows:
        nl, rr = 1, list(r)
        for c in wrap_cols:
            if isinstance(r[c], str) and r[c]:
                rr[c], n = _wrapped(r[c], widths[c])
                nl = max(nl, n)
        heights.append(nl * FONT_MM + ROW_PAD_MM)
        cooked.append(rr)
    return widths, wrap_cols, heights, cooked


def paginate(head: list, rows: list, max_height_mm: float, max_width_mm: dict | None = None) -> list[list]:
    """行太多时按实际行高分成几页（每页都带表头），每页表格高不超过 max_height_mm。"""
    hs = layout(head, rows, max_width_mm)[2]
    pages, cur, h = [], [], hs[0] + 2.0
    for r, rh in zip(rows, hs[1:]):
        if cur and h + rh > max_height_mm:
            pages.append(cur)
            cur, h = [], hs[0] + 2.0
        cur.append(r)
        h += rh
    pages.append(cur)
    return pages


def write_table(path: str, head: list, rows: list, max_width_mm: dict | None = None, align: dict | None = None,
                min_width_mm: float = 10.0) -> dict:
    """出图用：写 Bonsai Schedules 读的 xlsx + 同名 CSS。max_width_mm = {列号: 最大宽度}，超过的列自动换行；
    align = {列号: "left"/"center"}。返回 {"width_mm", "height_mm"}（Bonsai 画出来的表格尺寸，含 1mm 外边距）。"""
    align = align or {}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    widths, wrap_cols, heights, cooked = layout(head, rows, max_width_mm, min_width_mm)
    wb = openpyxl.Workbook()
    ws = wb.active
    grey = PatternFill(fill_type="solid", fgColor="FFEFEFEF", bgColor="FFEFEFEF")
    for i, (r, h_mm) in enumerate(zip(cooked, heights), 1):
        ws.append(r)
        ws.row_dimensions[i].height = h_mm / PT_MM
        for c, cell in enumerate(ws[i]):
            cell.font = Font(size=FONT_PT, bold=False)
            h = "center" if i == 1 else align.get(c, "center")
            cell.alignment = Alignment(horizontal=h, vertical="center", wrap_text=(c in wrap_cols and i > 1))
            if i == 1:
                cell.fill = grey
    for c, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(c + 1)].width = w / CHAR_MM
    wb.save(path)
    with open(os.path.splitext(path)[0] + ".css", "w", encoding="utf-8") as fh:
        fh.write(CSS.format(pt=FONT_PT, px=FONT_MM))
    return {"width_mm": sum(widths) + 2.0, "height_mm": sum(heights) + 2.0, "widths_mm": widths}


def _xl_width(s) -> float:
    """Excel 列宽（字符数）：汉字、全角按 2，ASCII 按 1.1。"""
    return sum(2.0 if ord(ch) > 127 else 1.1 for ch in str(s))


def write_delivery(path: str, title: str, head: list, rows: list, footnote=(), subtitle: str = "",
                   sheet_name: str | None = None, max_chars: float = 48.0, align: dict | None = None) -> str:
    """交付用的 xlsx（数据原文，没有为出图插入的断行空格）：第 1 行表名（+ 工程名），第 2 行表头（加粗、灰底），
    全部单元格细边框，列宽按内容（最宽 max_chars 个字符宽，超过的自动换行），冻结表头；空一行写脚注（和图纸上的一样）。
    返回路径。"""
    align = align or {}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (sheet_name or title)[:31]
    thin = Side(style="thin", color="FF000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.append([title + (f"　工程：{subtitle}" if subtitle else "")])
    ws.cell(1, 1).font = Font(size=14, bold=True)
    ws.row_dimensions[1].height = 24
    ws.append(list(head))
    grey = PatternFill(fill_type="solid", fgColor="FFEFEFEF", bgColor="FFEFEFEF")
    for c in range(1, len(head) + 1):
        cell = ws.cell(2, c)
        cell.font = Font(size=10.5, bold=True)
        cell.fill = grey
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    widths = [_xl_width(h) + 2 for h in head]
    # 对齐按列定：给了 align 的照给的；否则这一列有长文字（>8 字）就左对齐，其余居中
    col_align = [align.get(c, "left" if any(isinstance(r[c], str) and len(r[c]) > 8 for r in rows) else "center")
                 for c in range(len(head))]
    for r in rows:
        # 纯数字的文字（如窗台高"900"）存成数，便于统计
        ws.append([int(v) if isinstance(v, str) and v.isdigit() else v for v in r])
        i = ws.max_row
        for c, v in enumerate(r, 1):
            cell = ws.cell(i, c)
            cell.font = Font(size=10.5)
            cell.border = border
            cell.alignment = Alignment(horizontal=col_align[c - 1], vertical="center", wrap_text=True)
            widths[c - 1] = max(widths[c - 1], min(_xl_width(v) + 2, max_chars))
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = round(w, 1)
    ws.freeze_panes = "A3"
    if footnote:
        ws.append([])
        for t in footnote:
            ws.append([t])
            ws.cell(ws.max_row, 1).alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
    wb.save(path)
    return path
