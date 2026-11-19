"""生成中文 A3 图框（横式，420x297，左侧装订边 25mm，其余 5mm；右侧竖向标题栏）。

输出文件里所有非 ASCII 字符都写成 XML 数字字符引用（&#xxxx;），因为 Bonsai 读图框模板时用
平台默认编码打开文件，这样无论机器是 GBK 还是 UTF-8 都不会乱码。
图框字段用 Bonsai 的 mustache 模板，数据来自图纸的 IfcDocumentInformation：
  {{Description}} 工程名称 / {{Name}} 图名 / {{Identification}} 图号 / {{Purpose}} 比例 / {{IntendedUse}} 日期
线宽参照 GB/T 50001-2017（A3 取 b=0.7：图框线 b，标题栏外框 0.7b，分格线 0.35b），待复核。
用法：python tools/make_titleblock.py packs/cn/drawing/titleblocks/A3-CN.svg
"""
import sys


def ent(s: str) -> str:
    return "".join(c if ord(c) < 128 else f"&#{ord(c)};" for c in s)


W, H = 420, 297
L, M = 25, 5                      # 装订边、其余边距
FX0, FY0, FX1, FY1 = L, M, W - M, H - M
TB = 40                           # 标题栏宽
TX0 = FX1 - TB
FRAME, OUTER, GRID = 0.7, 0.5, 0.25

out = []
a = out.append
a('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
a(f'<svg xmlns="http://www.w3.org/2000/svg" id="root" version="1.1" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">')
a('<style>text{font-family:FangSong,"STFangsong","Noto Serif CJK SC",serif;fill:#000;stroke:none}'
  '.lab{font-size:3.2px}.val{font-size:4.2px}.valsm{font-size:3.3px}.big{font-size:5.2px;font-weight:bold}.note{font-size:2.9px}</style>')
a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fff" stroke="none"/>')
a(f'<rect x="{FX0}" y="{FY0}" width="{FX1 - FX0}" height="{FY1 - FY0}" fill="none" stroke="#000" stroke-width="{FRAME}"/>')
a(f'<rect x="{TX0}" y="{FY0}" width="{TB}" height="{FY1 - FY0}" fill="none" stroke="#000" stroke-width="{OUTER}"/>')


def hline(y, w=GRID):
    a(f'<line x1="{TX0}" y1="{y}" x2="{FX1}" y2="{y}" stroke="#000" stroke-width="{w}"/>')


def text(x, y, s, cls, anchor="middle"):
    a(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{s}</text>')


cx = TX0 + TB / 2
y = FY0
# 1. 项目标识
text(cx, y + 9, ent("cn-bim-toolkit"), "big")
text(cx, y + 16, ent("开源工具链生成"), "lab")
y += 22; hline(y, OUTER)
# 2-6. 字段：(标签, 模板, 值行高)
fields = [("工程名称", "{{Description}}", 18), ("图名", "{{Name}}", 18), ("图号", "{{Identification}}", 10),
          ("比例", "{{Purpose}}", 10), ("日期", "{{IntendedUse}}", 10)]
for label, tpl, vh in fields:
    text(TX0 + 2, y + 5, ent(label), "lab", "start"); y += 7; hline(y)
    text(cx, y + vh / 2 + 1.5, tpl, "valsm" if tpl == "{{Description}}" else "val"); y += vh; hline(y)
# 7. 签字栏
for label in ("设计", "校对", "审核"):
    text(TX0 + 2, y + 7.5, ent(label), "lab", "start")
    a(f'<line x1="{TX0 + 12}" y1="{y}" x2="{TX0 + 12}" y2="{y + 12}" stroke="#000" stroke-width="{GRID}"/>')
    y += 12; hline(y)
hline(y, OUTER)
# 8. 备注
note = ["说明：", "1. 本图由 AI 生成，", "仅供方案沟通。", "2. 须经设计师、电工", "复核，并现场复尺", "后方可施工。",
        "3. 尺寸单位：毫米；", "标高单位：米。"]
for i, line in enumerate(note):
    text(TX0 + 2, y + 6 + i * 4.6, ent(line), "note", "start")
# 9. 指北针占位（Bonsai 会按项目的网格北旋转 data-type="grid-north" 组）
a(f'<g transform="translate({TX0 + TB / 2},{FY1 - 22})"><g data-type="grid-north">'
  '<circle cx="0" cy="0" r="8" fill="none" stroke="#000" stroke-width="0.25"/>'
  '<path d="M 0,-8 L 2.8,6 L 0,4 L -2.8,6 Z" fill="#000" stroke="none"/>'
  f'<text x="0" y="-10" class="lab" text-anchor="middle">N</text></g></g>')
a('</svg>')

with open(sys.argv[1], "w", encoding="ascii") as fh:
    fh.write("\n".join(out))
print("wrote", sys.argv[1])
