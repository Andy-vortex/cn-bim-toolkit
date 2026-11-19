"""出图排图（在 Blender + Bonsai 里运行）：按中国家装约定包建视图、排 A3 图纸、出 PDF。

用法见 scripts/make_demo_set.py。只调用 Bonsai 的公开功能和底层 core 函数，不修改 Bonsai 本身：
- 约定包通过 IfcProject 的 BBIM_Documentation 属性集挂上（样式表、图框目录）；
- 图纸上放图走 core/sheeter（界面上的"放到图纸"操作在后台模式下要选中列表项，绕开它）；
- PDF 用 Edge/Chrome 无头打印（不依赖 Inkscape）。
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import bpy
import ifcopenshell
import ifcopenshell.api as api
import ifcopenshell.util.element as uel

import bonsai.tool as tool
import bonsai.core.drawing as core
from bonsai.bim.module.drawing import sheeter

SVG_NS = "http://www.w3.org/2000/svg"
XLINK = "{http://www.w3.org/1999/xlink}"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# A3-CN 图框里可放图的区域（图纸毫米）：装订边 25 + 5 边距，右侧标题栏 40 宽
AREA = (32.0, 10.0, 368.0, 285.0)


def log(*a):
    print("JZLOG", *a, flush=True)


def _bonsai_default_css() -> str:
    """本机已安装的 Bonsai 自带的默认样式表（GPL-3.0，不随本仓库分发）。"""
    import bonsai
    return os.path.join(os.path.dirname(bonsai.__file__), "bim", "data", "assets", "default.css")


def configure_project(pack_dir: str) -> None:
    f = tool.Ifc.get()
    project = f.by_type("IfcProject")[0]
    ifc_dir = os.path.dirname(tool.Ifc.get_path())
    rel = lambda p: os.path.relpath(p, ifc_dir).replace("\\", "/")
    # 合并样式表 = Bonsai 默认样式 + 约定包覆盖规则（后者在后，优先生效）；写在 IFC 旁边，属于输出物
    combined = os.path.join(ifc_dir, "cn-combined.css")
    with open(_bonsai_default_css(), encoding="utf-8") as fh:
        base = fh.read()
    with open(os.path.join(pack_dir, "cn.css"), encoding="utf-8") as fh:
        ours = fh.read()
    with open(combined, "w", encoding="utf-8") as fh:
        fh.write(base + "\n\n" + ours)
    props = {
        "StylesheetPath": rel(combined),
        "TitleblocksDir": rel(os.path.join(pack_dir, "titleblocks")) + "/",
    }
    pset = uel.get_pset(project, "BBIM_Documentation")
    pset = f.by_id(pset["id"]) if pset else api.pset.add_pset(f, product=project, name="BBIM_Documentation")
    api.pset.edit_pset(f, pset=pset, properties=props)
    log("pack configured", props)


def add_view(name: str, target_view: str, hint, den: int, extents, margin_paper_mm=30.0,
             exclude: str | None = None, include: str | None = None):
    """新建一个视图并设好比例、范围、过滤。extents = (x0, y0, x1, y1) 米。"""
    f = tool.Ifc.get()
    props = tool.Drawing.get_document_props()
    props.target_view = target_view
    props.location_hint = str(hint)
    before = {d.id() for d in f.by_type("IfcAnnotation") if d.ObjectType == "DRAWING"}
    bpy.ops.bim.add_drawing()
    drawing = next(d for d in f.by_type("IfcAnnotation") if d.ObjectType == "DRAWING" and d.id() not in before)
    core.update_drawing_name(tool.Ifc, tool.Drawing, drawing=drawing, name=name)
    bpy.ops.bim.activate_drawing(drawing=drawing.id(), should_view_from_camera=False)
    cam = bpy.context.scene.camera
    cprops = tool.Drawing.get_camera_props(cam.data)
    if den in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
        cprops.diagram_scale = f"1:{den}|1/{den}"
    else:  # 1:150、1:300 等不在 Bonsai 下拉列表里的比例走"自定义"
        cprops.diagram_scale = "CUSTOM"
        cprops.custom_scale_numerator = "1"
        cprops.custom_scale_denominator = str(den)
    m = margin_paper_mm * den / 1000.0
    x0, y0, x1, y1 = extents
    cprops.width = (x1 - x0) + 2 * m
    cprops.height = (y1 - y0) + 2 * m
    cam.location.x, cam.location.y = (x0 + x1) / 2, (y0 + y1) / 2
    _VIEW_SIZE[drawing.id()] = (cprops.width, cprops.height, cam.location.x, cam.location.y)
    pset = uel.get_pset(drawing, "EPset_Drawing")
    edits = {"HumanScale": f"1:{den}", "Scale": f"1/{den}"}
    if exclude:
        edits["Exclude"] = exclude
    if include:   # ifcopenshell 选择器语法，如 'IfcElement, JZ_Zone.Block="K1"'；Bonsai 再和相机范围取交集
        edits["Include"] = include
    api.pset.edit_pset(f, pset=f.by_id(pset["id"]), properties=edits)
    log("view", name, target_view, f"1:{den}", "size(m)", round(cprops.width, 2), round(cprops.height, 2))
    return drawing


_VIEW_SIZE: dict[int, tuple] = {}


def render_view(drawing) -> str:
    bpy.ops.bim.activate_drawing(drawing=drawing.id(), should_view_from_camera=False)
    cam = bpy.context.scene.camera
    cprops = tool.Drawing.get_camera_props(cam.data)
    if drawing.id() in _VIEW_SIZE:  # 激活可能从 IFC 读回旧尺寸，这里按 add_view 时的设定再写一次
        w, h, cx, cy = _VIEW_SIZE[drawing.id()]
        if abs(cprops.width - w) > 1e-6 or abs(cprops.height - h) > 1e-6:
            cprops.width, cprops.height = w, h
        cam.location.x, cam.location.y = cx, cy
    # 渲染分辨率决定投影（隐藏线）部分的画幅比例；不同步的话投影会按正方形画幅算，整体错位
    rx, ry = cprops.update_camera_resolution()
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = rx, ry
    bpy.context.scene.render.resolution_percentage = 100
    r = bpy.ops.bim.create_drawing(open_viewer=False)
    path = tool.Ifc.resolve_uri(tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing)))
    log("drawing", drawing.Name, r, os.path.basename(path))
    return path


def make_sheet(drawing, ident: str, title: str, project_name: str, scale_text: str, date_text: str,
               reserve_right_mm: float = 0.0):
    """一张 A3-CN 图纸放一个视图，居中。reserve_right_mm：右侧给图例留的宽度。"""
    f = tool.Ifc.get()
    before = {s.id() for s in f.by_type("IfcDocumentInformation")}
    core.add_sheet(tool.Ifc, tool.Drawing, titleblock="A3-CN")
    sheet = next(s for s in f.by_type("IfcDocumentInformation") if s.id() not in before and s.Scope == "SHEET")
    api.document.edit_information(f, information=sheet, attributes={
        "Identification": ident, "Name": title, "Description": project_name,
        "Purpose": scale_text, "IntendedUse": date_text})
    # 放图（等同于界面上的"放到图纸"）
    drawing_reference = tool.Drawing.get_drawing_document(drawing)
    reference = api.document.add_reference(f, information=sheet)
    attrs = tool.Drawing.generate_reference_attributes(reference, Identification="1",
                                                       Location=drawing_reference.Location, Description="DRAWING")
    api.document.edit_reference(f, reference=reference, attributes=attrs)
    sheeter.SheetBuilder().add_drawing(reference, drawing, sheet)
    _center_in_layout(sheet, reserve_right_mm)
    out = sheeter.SheetBuilder().build(sheet)
    log("sheet", ident, title, os.path.basename(out["SHEET"]))
    return sheet, out["SHEET"]


def _center_in_layout(sheet, reserve_right_mm):
    layout = tool.Ifc.resolve_uri(tool.Drawing.get_document_uri(sheet, "LAYOUT"))
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK[1:-1])
    tree = ET.parse(layout)
    root = tree.getroot()
    x0, y0, x1, y1 = AREA
    x1 -= reserve_right_mm
    for g in root.findall(f"{{{SVG_NS}}}g[@data-type='drawing']"):
        imgs = g.findall(f"{{{SVG_NS}}}image")
        fg = next(i for i in imgs if i.get("data-type") == "foreground")
        w, h = float(fg.get("width")), float(fg.get("height"))
        nx, ny = x0 + (x1 - x0 - w) / 2, y0 + (y1 - y0 - h) / 2 - 6
        dx, dy = nx - float(fg.get("x")), ny - float(fg.get("y"))
        for i in imgs:
            i.set("x", str(float(i.get("x")) + dx))
            i.set("y", str(float(i.get("y")) + dy))
    tree.write(layout)


# ------------------------------------------------------------------ 图例与 PDF
def add_legend(sheet_svg: str, pack_symbols: str, keys, den: int, x: float, y: float, title="图例"):
    """在最终图纸 SVG 上加图例（符号按图纸比例画，旁边写名称和默认安装高度）。"""
    from .symbols import item_polylines
    with open(pack_symbols, encoding="utf-8") as fh:
        pack = json.load(fh)["types"]
    s = 1.0 / den  # 实际毫米 -> 图纸毫米
    parts = [f'<g id="jz-legend" font-family="FangSong" fill="none" stroke="#000">',
             f'<text x="{x}" y="{y}" font-size="5" fill="#000" stroke="none">{html.escape(title)}</text>']
    yy = y + 6
    for k in keys:
        spec = pack[k]
        ys = [py for it in spec["items"] for pts in item_polylines(it) for _, py in pts]
        row = max(12.0, (max(ys) - min(ys)) * s + 5.0)
        cx, cy = x + 7, yy + row / 2
        for it in spec["items"]:
            for pts in item_polylines(it):
                ymid = (max(ys) + min(ys)) / 2
                d = " ".join(f"{'M' if i == 0 else 'L'} {cx + px * s:.3f},{cy - (py - ymid) * s:.3f}" for i, (px, py) in enumerate(pts))
                parts.append(f'<path d="{d}" stroke-width="0.3"/>')
        h = spec.get("default_height", 0)
        label = html.escape(spec["legend"])
        parts.append(f'<text x="{x + 16}" y="{cy + 1.2}" font-size="3.4" fill="#000" stroke="none">{label}</text>')
        if spec["ifc_type"] in ("IfcOutletType", "IfcSwitchingDeviceType"):
            parts.append(f'<text x="{x + 16}" y="{cy + 5.4}" font-size="2.8" fill="#444" stroke="none">底边距地 {h}</text>')
        yy += row
    parts.append(f'<rect x="{x - 3}" y="{y - 7}" width="64" height="{yy - y + 9}" stroke-width="0.25"/>')
    parts.append("</g>")
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    svg = svg.replace("</svg>", "\n".join(parts) + "\n</svg>")
    with open(sheet_svg, "w", encoding="utf-8") as fh:
        fh.write(svg)


def export_pdf(sheet_svg: str, pdf_path: str) -> str:
    """用 Edge 无头打印成 A3 横向 PDF。SVG 直接内嵌进 HTML，样式和字体都能生效。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    svg = re.sub(r"^<\?xml[^>]*>\s*", "", svg)
    page = ('<!doctype html><html><head><meta charset="utf-8"><style>@page{size:420mm 297mm;margin:0}'
            'html,body{margin:0;padding:0}svg{width:420mm;height:297mm;display:block}</style></head><body>'
            + svg + "</body></html>")
    html_path = os.path.splitext(pdf_path)[0] + ".print.html"
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    url = "file:///" + html_path.replace("\\", "/")
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}", url], timeout=180, capture_output=True)
    log("pdf", os.path.basename(pdf_path), os.path.exists(pdf_path) and os.path.getsize(pdf_path))
    return pdf_path


def add_notes(sheet_svg: str, lines, x: float, y: float, size=3.2, title=None):
    """在最终图纸 SVG 上加几行说明文字（图纸毫米坐标）。"""
    parts = ['<g id="jz-notes" font-family="FangSong" fill="#000" stroke="none">']
    yy = y
    if title:
        parts.append(f'<text x="{x}" y="{yy}" font-size="{size + 1.2}">{html.escape(title)}</text>')
        yy += size + 3
    for ln in lines:
        parts.append(f'<text x="{x}" y="{yy}" font-size="{size}">{html.escape(ln)}</text>')
        yy += size + 1.8
    parts.append("</g>")
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    with open(sheet_svg, "w", encoding="utf-8") as fh:
        fh.write(svg.replace("</svg>", "\n".join(parts) + "\n</svg>"))
