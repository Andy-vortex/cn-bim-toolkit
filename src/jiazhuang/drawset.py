"""出图排图（在 Blender + Bonsai 里运行）：按中国家装约定包建视图、排 A3 图纸、出 PDF。

用法见 scripts/make_demo_set.py。只调用 Bonsai 的公开功能和底层 core 函数，不修改 Bonsai 本身：
- 约定包通过 IfcProject 的 BBIM_Documentation 属性集挂上（样式表、图框目录）；
- 图纸上放图走 core/sheeter（界面上的"放到图纸"操作在后台模式下要选中列表项，绕开它）；
- PDF、PNG 用 Edge 无头打印、截图（printing.py）。
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET

import bpy
import ifcopenshell
import ifcopenshell.api as api
import ifcopenshell.api.drawing  # noqa: F401（assign_product）
import ifcopenshell.util.element as uel
import ifcopenshell.util.unit

import bonsai.tool as tool
import bonsai.core.drawing as core
from bonsai.bim.module.drawing import sheeter

from . import conventions, printing, schedules, symbols
from .printing import log

SVG_NS = "http://www.w3.org/2000/svg"
XLINK = "{http://www.w3.org/1999/xlink}"

# 图框和视图标题的尺寸、字号（约定包 conventions.json 的 titleblock，tools/make_titleblock.py 生成模板读的是同一份）
_TB = conventions.get("titleblock")
_SHEET_W, _SHEET_H = _TB["sheet_mm"]
# A3-CN 图框里可放图的区域（图纸毫米）：图框线（装订边、留边）往里 7mm（上边 5mm），右边到标题栏左边再空 7mm
AREA = (float(_TB["binding_mm"] + 7), float(_TB["margin_mm"] + 5),
        float(_SHEET_W - _TB["margin_mm"] - _TB["width_mm"] - 7), float(_SHEET_H - _TB["margin_mm"] - 7))


def _bonsai_asset(name: str) -> str:
    """本机已安装的 Bonsai 自带的出图资源（default.css、markers.svg 等，GPL-3.0，不随本仓库分发）。"""
    import bonsai
    return os.path.join(os.path.dirname(bonsai.__file__), "bim", "data", "assets", name)


WORK = "work"   # 输出目录里放中间文件的子目录：视图、排版、图纸 SVG，合并样式表，Bonsai 的出图资源
# 中间文件的位置（相对 IFC 所在目录）。Bonsai 0.9 从 IfcProject 的 BBIM_Documentation 读这几个键
# （tool.Drawing.get_default_drawing_path / _sheet_path / _layout_path / get_default_drawing_resource_path），
# 不写就用 Bonsai 偏好设置的缺省值，也就是输出目录顶层的 drawings/、sheets/、layouts/
WORK_PATHS = {
    "DrawingsDir": f"{WORK}/drawings/",
    "SheetsDir": f"{WORK}/sheets/",
    "LayoutsDir": f"{WORK}/layouts/",
    "StylesheetPath": f"{WORK}/cn-combined.css",
    "MarkersPath": f"{WORK}/drawings/assets/markers.svg",
    "SymbolsPath": f"{WORK}/drawings/assets/symbols.svg",
    "PatternsPath": f"{WORK}/drawings/assets/patterns.svg",
    "ShadingStylesPath": f"{WORK}/drawings/assets/shading_styles.json",
}


def _dir_uri(path: str, start: str) -> str:
    """目录写进 IFC 的路径（正斜杠，末尾 /）：相对 start（IFC 所在目录），输出目录连同约定包一起挪走还能用；不在同一个盘
    （Windows 上另一个盘符、网络共享）没有相对路径，写绝对路径（Bonsai 的 resolve_uri 两种都认）。"""
    try:
        p = os.path.relpath(path, start)
    except ValueError:
        p = os.path.abspath(path)
    return p.replace("\\", "/").rstrip("/") + "/"


def configure_project() -> None:
    """把约定包（conventions.pack_dir()）挂到当前 IFC 上（IfcProject 的 BBIM_Documentation）：合并样式表、出图标记、
    图框目录、视图标题，中间文件都放输出目录的 work/。"""
    f = tool.Ifc.get()
    project = f.by_type("IfcProject")[0]
    ifc_dir = os.path.dirname(tool.Ifc.get_path())
    at = lambda p: os.path.join(ifc_dir, *p.split("/"))
    # 合并样式表 = Bonsai 默认样式 + 约定包覆盖规则（后者在后，优先生效）；含 Bonsai 的 GPL 样式，属于输出物
    combined = at(WORK_PATHS["StylesheetPath"])
    os.makedirs(os.path.dirname(combined), exist_ok=True)
    with open(_bonsai_asset("default.css"), encoding="utf-8") as fh:
        base = fh.read()
    with open(conventions.pack_file("cn.css"), encoding="utf-8") as fh:
        ours = fh.read()
    with open(combined, "w", encoding="utf-8") as fh:
        fh.write(base + "\n\n" + ours)
    # 出图标记 = Bonsai 自带的 markers.svg，同 id 的换成约定包 markers-CN.svg 里的（标高符号）；Bonsai 在这个位置有文件时
    # 就不再拷它自带的（svgwriter.prepare_resource_path）
    marks = at(WORK_PATHS["MarkersPath"])
    os.makedirs(os.path.dirname(marks), exist_ok=True)
    with open(_bonsai_asset("markers.svg"), encoding="utf-8") as fh:
        svg = fh.read()
    for mid, m in conventions.markers().items():
        svg, hit = re.subn(r'<marker\b[^>]*\bid="%s"[^>]*>.*?</marker>' % re.escape(mid), lambda _: m, svg, flags=re.S)
        if not hit:
            svg = svg.replace("</svg>", m + "\n</svg>")
    with open(marks, "w", encoding="utf-8") as fh:
        fh.write(svg)
    _write_symbols(at(WORK_PATHS["SymbolsPath"]))
    props = {**WORK_PATHS, "TitleblocksDir": _dir_uri(conventions.pack_file("titleblocks"), ifc_dir)}
    pset = uel.get_pset(project, "BBIM_Documentation")
    pset = f.by_id(pset["id"]) if pset else api.pset.add_pset(f, product=project, name="BBIM_Documentation")
    api.pset.edit_pset(f, pset=pset, properties=props)
    # 视图标题模板：Bonsai 排图纸时读 <LayoutsDir>/assets/view-title.svg（没有才拷自带的：带编号圈、两行排、50mm 宽）。
    # 不放在 titleblocks/ 里，否则 Bonsai 界面会把它当成一种图框列出来
    dst = at(WORK_PATHS["LayoutsDir"] + "assets/view-title.svg")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(conventions.pack_file("view-title-CN.svg"), dst)
    log("pack configured", props)


def _write_symbols(path: str) -> None:
    """出图符号 = Bonsai 自带的 symbols.svg，加上约定包 symbols.json 里按纸面大小画的点位符号和它们的变体（symbols.svg_defs，
    同 id 的替换），写到 path（SymbolsPath）；视图里的 SYMBOL 标注引用它们（_paper_symbols）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(_bonsai_asset("symbols.svg"), encoding="utf-8") as fh:
        svg = fh.read()
    for g in symbols.svg_defs(_pack(conventions.pack_file("symbols.json"))):
        sid = re.match(r'<g id="([^"]+)"', g).group(1)
        svg, hit = re.subn(r'<g\b[^>]*\bid="%s"[^>]*>.*?</g>' % re.escape(sid), lambda _: g, svg, flags=re.S)
        if not hit:
            svg = svg.replace("</svg>", g + "\n</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)


def _ensure_symbols() -> None:
    """出图前确认 SymbolsPath 文件里有约定包的点位符号定义（symbols.svg_defs 的每个 id）。只拿 model.ifc、没带输出目录的
    work/ 重出时，Bonsai 会拷它自带的 symbols.svg（没有这些定义），<use> 找不到定义、点位一个都不画：这时按 configure_project
    的做法补写。"""
    rel = uel.get_pset(tool.Ifc.get().by_type("IfcProject")[0], "BBIM_Documentation", "SymbolsPath")
    if not rel:
        return
    path = tool.Ifc.resolve_uri(rel)
    ids = {re.match(r'<g id="([^"]+)"', g).group(1) for g in symbols.svg_defs(_pack(conventions.pack_file("symbols.json")))}
    try:
        with open(path, encoding="utf-8") as fh:
            have = set(re.findall(r'\bid="([^"]+)"', fh.read()))
    except OSError:
        have = set()
    if not ids <= have:
        _write_symbols(path)
        log("symbols written", rel, len(ids))


OVERVIEW_POINTS = "IfcAlarm, IfcOutlet, IfcSwitchingDevice, IfcWasteTerminal, IfcRailing, IfcAudioVisualAppliance, "


def overview_exclude(den: int, furniture: bool, rest: str) -> str:
    """总图视图的 Exclude 选择器：1:200 及更小不画点位和扶手；有家具时 1:200 只画固定家具（洁具、橱柜），比 1:200 更小的
    家具都不画（JZ_Furniture.Placement，plan2ifc.place_item 写的）。rest 是别的要排除的（接在后面）。"""
    q = (OVERVIEW_POINTS if den >= 200 else "") + rest
    if furniture and den >= 200:
        q += ' + IfcElement, JZ_Furniture.Placement="活动"'
    if furniture and den > 200:
        q += ' + IfcElement, JZ_Furniture.Placement="固定"'
    return q


def add_view(name: str, target_view: str, hint, den: int, extents, margin_paper_mm=30.0, exclude: str | None = None, crop=None):
    """新建一个视图并设好比例、范围、过滤。extents = (x0, y0, x1, y1) 米（相机范围 = extents 四周再加 margin_paper_mm）。
    crop = (x0, y0, x1, y1) 米：模型只画到这个框（分段图的切口），框外留给尺寸、轴号；存进 IFC（视图的 JZ_View 属性集），
    render_view 读它裁模型线，所以从保存的 IFC 重开重出也一样。"""
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
    if crop:
        _set_crop(drawing, crop, cam.location.x, cam.location.y)
    pset = uel.get_pset(drawing, "EPset_Drawing")
    edits = {"HumanScale": f"1:{den}", "Scale": f"1/{den}"}
    if exclude:
        edits["Exclude"] = exclude
    api.pset.edit_pset(f, pset=f.by_id(pset["id"]), properties=edits)
    log("view", name, target_view, f"1:{den}", "size(m)", round(cprops.width, 2), round(cprops.height, 2))
    _paper_symbols(drawing)
    return drawing


def _paper_symbols(drawing) -> int:
    """视图里有 paper_scale 的点位（symbols.json：插座、开关、筒灯、地漏、紧急呼叫、监控）改用 Bonsai 的 SYMBOL 标注画，在任何
    比例下都和 1:paper_scale 时一样大：每个点位一个 SYMBOL（放置和点位相同，EPset_Annotation.Symbol = 符号键，
    IfcRelAssignsToProduct 关联到点位），Bonsai 画成 <use href="#符号键">（symbols.svg_defs）；这些类型再加进视图的 Exclude
    （按类型排除，同一类里别的类型照常画），模型线里不再按实际尺寸画一遍。比例正好是 paper_scale 时不换。
    只换视图本来会画、在裁切框里的点位（切口处的 <use> 由 _clip_model 和模型线一起裁）。符号代表贴墙的点位，要整个画在点位
    所在的房间里、不压墙：按 annotate.paper_pose 在点位放置上再转一个角度、或换成镜像、画到插入点一侧的变体（符号键加后缀，
    symbols.VARIANTS）。返回换了几个。"""
    from mathutils import Matrix
    from . import annotate
    f = tool.Ifc.get()
    den = _scale_den(drawing)
    scales = {k: d for k, d in symbols.paper_scales(conventions.pack_file("symbols.json")).items() if d != den}
    if not scales:
        return 0
    bpy.context.view_layer.update()   # 刚挪过的相机，matrix_world 要求值后才是新位置
    crop, cam = view_crop(drawing), tool.Ifc.get_object(drawing).matrix_world.translation
    used, n = {}, 0   # 符号键 → IFC 类
    for e in sorted(tool.Drawing.get_drawing_elements(drawing), key=lambda e: e.id()):
        t = None if e.is_a("IfcAnnotation") else uel.get_type(e)
        obj = tool.Ifc.get_object(e)
        if getattr(t, "Tag", None) not in scales or obj is None:
            continue
        used[t.Tag] = e.is_a()
        p = obj.matrix_world.translation
        if crop and not (cam.x + crop[0] <= p.x <= cam.x + crop[2] and cam.y + crop[1] <= p.y <= cam.y + crop[3]):
            continue   # 分区、分段图只画裁切框里的点位
        suffix, turn = annotate.paper_pose(f, e, den / scales[t.Tag], den)
        sym = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="SYMBOL", relating_type=None)
        sym.matrix_world = obj.matrix_world @ Matrix.Rotation(math.radians(turn), 4, "Z")
        a = tool.Ifc.get_entity(sym)
        api.pset.edit_pset(f, pset=api.pset.add_pset(f, product=a, name="EPset_Annotation"), properties={"Symbol": t.Tag + suffix})
        api.drawing.assign_product(f, relating_product=e, related_object=a)
        n += 1
    if used:
        pset = uel.get_pset(drawing, "EPset_Drawing")
        excl = " + ".join(f'{c}, query:"type.Tag"="{k}"' for k, c in sorted(used.items()))
        old = pset.get("Exclude")
        api.pset.edit_pset(f, pset=f.by_id(pset["id"]), properties={"Exclude": f"{old} + {excl}" if old else excl})
        log("paper symbols", drawing.Name, n, sorted(used))
    return n


def drawn_symbol_keys(drawing) -> set[str]:
    """视图里实际画出的符号库类型（symbols.json 的键）：按实际尺寸用模型线画的（Bonsai 取到的构件；有裁切框的分区、分段图
    只算放置点在框里的）和按纸面大小用 SYMBOL 画的（符号键去掉变体后缀）。图例的点位行按它列：平面图上不画的（如吸顶的
    监控，在剖切面以上）不列。"""
    bpy.context.view_layer.update()
    crop, cam = view_crop(drawing), tool.Ifc.get_object(drawing).matrix_world.translation
    keys = set()
    for e in tool.Drawing.get_drawing_elements(drawing):
        tag = None if e.is_a("IfcAnnotation") else getattr(uel.get_type(e), "Tag", None)
        obj = tool.Ifc.get_object(e) if tag else None
        if obj is None:
            continue
        p = obj.matrix_world.translation
        if not crop or (cam.x + crop[0] <= p.x <= cam.x + crop[2] and cam.y + crop[1] <= p.y <= cam.y + crop[3]):
            keys.add(tag)
    for a in tool.Drawing.get_group_elements(tool.Drawing.get_drawing_group(drawing)):
        k = a.ObjectType == "SYMBOL" and uel.get_pset(a, "EPset_Annotation", "Symbol")
        if k:
            keys.add(symbols.base_key(k))
    return keys


_VIEW_SIZE: dict[int, tuple] = {}
CROP_PSET = "JZ_View"   # 视图（DRAWING）上的模型裁切框：CropXMin/CropYMin/CropXMax/CropYMax，相对相机中心，项目长度单位
_CROP_KEYS = ("CropXMin", "CropYMin", "CropXMax", "CropYMax")


def _set_crop(drawing, crop, cx, cy) -> None:
    """裁切框写进 IFC（视图的 JZ_View 属性集）。存相对相机中心的值：相机挪了框跟着挪，也不受 Bonsai 加载大坐标时的偏移影响。"""
    f = tool.Ifc.get()
    k = 1.0 / ifcopenshell.util.unit.calculate_unit_scale(f)   # 米 → 项目长度单位
    vals = [(crop[0] - cx) * k, (crop[1] - cy) * k, (crop[2] - cx) * k, (crop[3] - cy) * k]
    pset = uel.get_pset(drawing, CROP_PSET)
    pset = f.by_id(pset["id"]) if pset else api.pset.add_pset(f, product=drawing, name=CROP_PSET)
    api.pset.edit_pset(f, pset=pset, properties={n: f.createIfcLengthMeasure(v) for n, v in zip(_CROP_KEYS, vals)})


def view_crop(drawing):
    """视图的模型裁切框（米，相对相机中心：x0, y0, x1, y1）；没有返回 None。"""
    ps = uel.get_pset(drawing, CROP_PSET) or {}
    if not all(isinstance(ps.get(n), (int, float)) for n in _CROP_KEYS):
        return None
    s = ifcopenshell.util.unit.calculate_unit_scale(tool.Ifc.get())
    return tuple(ps[n] * s for n in _CROP_KEYS)


def _clip_model(svg_path: str, drawing, crop, den: int) -> None:
    """Bonsai 只按相机范围取构件和裁剪（相机外的部分在图纸上由视口裁掉），没有"模型裁切框 ≠ 相机"的设置；
    分段图要把模型裁在切口上、框外还要写尺寸和轴号，所以给 Bonsai 生成的视图 SVG 里的模型线组
    （class 含 target-view- 的那个 g，IfcOpenShell SVG 序列化的结果）加一个 clipPath；按纸面大小画的点位符号（SYMBOL 的
    <use href="#cn_…">）代表模型里的点位，切口处和模型线一样裁掉；别的标注不受影响。
    crop：相对相机中心的裁切框（米）；den：比例分母。相机中心是视图 SVG 的中心：Bonsai 按取整后的渲染分辨率定画幅，
    SVG 的宽高和设定的相机宽高差一点，所以用 SVG 自己的宽高定中心；米到图纸毫米乘 1000/den，横向、竖向视图都对。"""
    x0, y0, x1, y1 = crop
    with open(svg_path, encoding="utf-8") as fh:
        svg = fh.read()
    head = re.search(r"<svg\b[^>]*>", svg)
    sw = head and re.search(r'\bwidth="([\d.]+)mm"', head.group(0))
    sh = head and re.search(r'\bheight="([\d.]+)mm"', head.group(0))
    m = re.search(r'<g\b[^>]*class="[^"]*target-view-[^"]*"[^>]*>', svg)
    if not m or not sw or not sh:
        log("crop: model group or svg size not found", os.path.basename(svg_path))
        return
    s = 1000.0 / den                       # 米 → 图纸毫米（视图 SVG 的单位）
    rx, ry = float(sw.group(1)) / 2 + x0 * s, float(sh.group(1)) / 2 - y1 * s
    cid = f"jz-crop-{drawing.id()}"
    tag = m.group(0)
    clip = (f'<defs><clipPath id="{cid}"><rect x="{rx:.3f}" y="{ry:.3f}" width="{(x1 - x0) * s:.3f}" '
            f'height="{(y1 - y0) * s:.3f}"/></clipPath></defs>')
    # 写在 style 里：排图纸时 Bonsai（sheeter.ensure_drawing_unique_styles）给 id 加前缀，只同步改 style/filter 里的 url(#…)
    svg = svg[:m.start()] + clip + tag[:-1] + f' style="clip-path:url(#{cid})">' + svg[m.end():]
    # 点位符号各包一层不带 transform 的 g 再裁（clip-path 按引用它的元素自己的坐标系算，<use> 带着平移和转角）
    svg = re.sub(r'<use\b[^>]*\bhref="#cn_[^"]*"[^>]*?(?:/>|>\s*</use>)', lambda u: f'<g style="clip-path:url(#{cid})">{u.group(0)}</g>', svg)
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(svg)


def _scale_den(drawing) -> int:
    """视图比例的分母（EPset_Drawing.Scale 写成 "1/den"，add_view 设的）。"""
    num, den = str(uel.get_pset(drawing, "EPset_Drawing", "Scale")).split("/")
    return round(float(den) / float(num))


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
    _ensure_symbols()
    r = bpy.ops.bim.create_drawing(open_viewer=False)
    path = tool.Ifc.resolve_uri(tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing)))
    crop = view_crop(drawing)
    if crop:
        _clip_model(path, drawing, crop, _scale_den(drawing))
    log("drawing", drawing.Name, r, os.path.basename(path))
    return path


def clean_outputs(out_dir: str) -> int:
    """出图前清掉上次留下的视图、图纸和表格中间文件：只删 work/ 下 drawings、drawings/cache、sheets、layouts、schedules
    这几个目录里直接放着的 .svg/.html/.pdf/.png/.xlsx/.css（每次都全部重出；视图、图纸张数和门窗表页数随运行变化，旧文件会和
    本次的 IFC、图纸目录对不上）；assets 子目录（出图资源、视图图名模板）和别的文件不动。返回删了几个。"""
    n = 0
    for sub in ("drawings", "drawings/cache", "sheets", "layouts", "schedules"):
        d = os.path.join(out_dir, WORK, *sub.split("/"))
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.path.splitext(name)[1].lower() in (".svg", ".html", ".pdf", ".png", ".xlsx", ".css"):
                os.remove(p)
                n += 1
    if n:
        log("cleaned old sheet files", n)
    return n


# 标题栏的五个字段 → 图纸（IfcDocumentInformation）的属性；图框模板里写成 {{属性名}}（tools/make_titleblock.py）
SHEET_FIELDS = {
    "图号": "Identification",
    "图名": "Name",
    "工程名称": "Description",
    "比例": "Purpose",        # IfcDocumentInformation 没有比例、日期属性，借用 Purpose、IntendedUse
    "日期": "IntendedUse",
}
TB_TEXT_MM = _TB["width_mm"] - 2 * _TB["pad_mm"]   # 标题栏一格里一行字最宽多少
# 标题栏里会折行的两格：class → (字号, 最多几行)
TB_WRAP = {"tb-proj": (_TB["small_mm"], _TB["project_lines"]), "tb-title": (_TB["big_mm"], _TB["title_lines"])}


def _new_sheet(ident: str, title: str, project_name: str, scale_text: str, date_text: str, titleblock: str = "A3-CN"):
    f = tool.Ifc.get()
    before = {s.id() for s in f.by_type("IfcDocumentInformation")}
    core.add_sheet(tool.Ifc, tool.Drawing, titleblock=titleblock)
    sheet = next(s for s in f.by_type("IfcDocumentInformation") if s.id() not in before and s.Scope == "SHEET")
    values = {"图号": ident, "图名": title, "工程名称": project_name, "比例": scale_text, "日期": date_text}
    api.document.edit_information(f, information=sheet, attributes={SHEET_FIELDS[k]: v for k, v in values.items()})
    return sheet


# 视图标题（约定包 view-title-CN.svg，tools/make_titleblock.py 生成）：图名、比例的字号和间隔
VT_NAME_MM, VT_SCALE_MM, VT_GAP_MM = _TB["big_mm"], _TB["small_mm"], float(_TB["view_title_gap_mm"])


def make_sheet(drawing, ident: str, title: str, project_name: str, scale_text: str, date_text: str,
               reserve_right_mm: float = 0.0):
    """一张 A3-CN 图纸放一个视图，居中；视图标题（图名 + 比例，图名下粗线）居中在视图下方。
    reserve_right_mm：右侧给图例留的宽度。"""
    f = tool.Ifc.get()
    sheet = _new_sheet(ident, title, project_name, scale_text, date_text)
    # 放图（等同于界面上的"放到图纸"）
    drawing_reference = tool.Drawing.get_drawing_document(drawing)
    reference = api.document.add_reference(f, information=sheet)
    # 视图标题取引用的 Name；不写的话 Bonsai 用文件名，而文件名会去掉"～"等字符
    attrs = tool.Drawing.generate_reference_attributes(reference, Identification="1", Name=drawing.Name,
                                                       Location=drawing_reference.Location, Description="DRAWING")
    api.document.edit_reference(f, reference=reference, attributes=attrs)
    sheeter.SheetBuilder().add_drawing(reference, drawing, sheet)
    name_w = schedules.text_mm(drawing.Name, VT_NAME_MM)
    _center_in_layout(sheet, reserve_right_mm, name_w + VT_GAP_MM + schedules.text_mm(scale_text, VT_SCALE_MM))
    out = sheeter.SheetBuilder().build(sheet)
    _fit_titleblock(out["SHEET"])
    _view_title_line(out["SHEET"], name_w)
    log("sheet", ident, title, os.path.basename(out["SHEET"]))
    return sheet, out["SHEET"]


def view_title_bottom(sheet) -> float:
    """图纸上视图标题的下边（图纸 mm，从排版文件读）：总图的图例接在它下面。"""
    layout = tool.Ifc.resolve_uri(tool.Drawing.get_document_uri(sheet, "LAYOUT"))
    img = ET.parse(layout).getroot().find(f".//{{{SVG_NS}}}image[@data-type='view-title']")
    return float(img.get("y")) + float(img.get("height"))


def _view_title_line(sheet_svg: str, length_mm: float) -> None:
    """视图标题图名下的粗线改成图名的宽（GB/T 50001：长度以图名所占长度为准）。只改排好的图纸 SVG：
    在 Bonsai 里重排这张图纸时粗线回到模板的默认长度。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    svg = re.sub(r'(<path\b[^>]*data-jz="vt-line"[^>]*\bd="M 0,[\d.]+ H )[\d.]+', lambda m: f"{m.group(1)}{length_mm:.2f}", svg)
    with open(sheet_svg, "w", encoding="utf-8") as fh:
        fh.write(svg)


def _center_in_layout(sheet, reserve_right_mm, title_w=None):
    """视图在可放图区（右侧扣掉 reserve_right_mm）里居中（往上让 6mm 给视图标题）；title_w（视图标题宽，mm）给了的话，
    视图标题在视图下方居中。改的是图纸的排版文件（layouts），在 Bonsai 里重排图纸时位置不变。"""
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
            if title_w is not None and i.get("data-type") == "view-title":
                i.set("x", str(nx + (w - title_w) / 2))
    tree.write(layout)


# ------------------------------------------------------------------ 表格图纸（Bonsai Schedules）
TABLE_TITLEBLOCK = "A3-CN-TABLE"   # 表格类图纸的图框：和 A3-CN 一样，只是没有指北针（tools/make_titleblock.py）


def make_table_sheet(tables, ident: str, title: str, project_name: str, date_text: str, scale_text: str = "—",
                     origin=(40.0, 24.0), gap_mm: float = 14.0, footnote=()):
    """一张 A3-CN 图纸放一张或几张表格。tables = [(xlsx 路径, 表名), ...]，表格 xlsx 由 schedules.write_table 生成。

    走 Bonsai 的 Schedules：core.add_document(…, "SCHEDULE", uri) 登记为 IFC 文档 → Drawing.create_svg_schedule
    画成 SVG（即 core.build_schedule 去掉"用系统查看器打开"那一步）→ 加文档引用并用 SheetBuilder.add_document
    放到图纸上（即界面上的"把表格放到图纸"，后台模式下绕开要选中列表项的操作）。
    表格从 origin 起自上而下排，放不下就另起一列；一页只有一张表时不写表名（标题栏的图名就是），几张表时写在各表上方。
    footnote（脚注各行，不加"说明"、不编号）写在最后一张表下沿往下 5mm。
    图框用约定包的表格图框（不画指北针），没有就用 A3-CN。返回 (sheet, 图纸 SVG 路径)。"""
    f = tool.Ifc.get()
    tdir = uel.get_pset(f.by_type("IfcProject")[0], "BBIM_Documentation", "TitleblocksDir") or ""
    tb = TABLE_TITLEBLOCK if os.path.exists(tool.Ifc.resolve_uri(os.path.join(tdir, TABLE_TITLEBLOCK + ".svg"))) else "A3-CN"
    sheet = _new_sheet(ident, title, project_name, scale_text, date_text, titleblock=tb)
    ifc_dir = os.path.dirname(tool.Ifc.get_path())
    refs = []
    for k, (xlsx, name) in enumerate(tables, 1):
        uri = os.path.relpath(xlsx, ifc_dir).replace("\\", "/")
        before = {d.id() for d in f.by_type("IfcDocumentInformation")}
        core.add_document(tool.Ifc, tool.Drawing, "SCHEDULE", uri=uri)
        doc = next(d for d in f.by_type("IfcDocumentInformation") if d.id() not in before and d.Scope == "SCHEDULE")
        api.document.edit_information(f, information=doc, attributes={"Identification": f"{ident}-{k}", "Name": name})
        tool.Drawing.create_svg_schedule(doc)
        ref = api.document.add_reference(f, information=sheet)
        attrs = tool.Drawing.generate_reference_attributes(ref, Identification=str(k), Name=name, Description="SCHEDULE",
                                                           Location=tool.Drawing.get_path_with_ext(uri, "svg"))
        api.document.edit_reference(f, reference=ref, attributes=attrs)
        sheeter.SheetBuilder().add_document(ref, doc, sheet)
        refs.append((ref, name))
    titles = _stack_tables(sheet, origin, gap_mm)
    out = sheeter.SheetBuilder().build(sheet)
    _fit_titleblock(out["SHEET"])
    parts = []
    if len(titles) > 1:
        parts.append('<g id="jz-table-titles" font-family="FangSong" fill="#000" stroke="none">')
        for (x, y, w, _h), (_, name) in zip(titles, refs):
            parts.append(f'<text x="{x + w / 2:.2f}" y="{y - 2.5:.2f}" font-size="{schedules.TITLE_FONT_MM}" text-anchor="middle" '
                         f'letter-spacing="1.5">{_xml(name)}</text>')
        parts.append("</g>")
    if footnote and titles:
        x, y, _, h = titles[-1]
        fs = schedules.NOTES_FONT_MM
        parts.append('<g id="jz-footnote" font-family="FangSong" fill="#000" stroke="none">')
        for i, t in enumerate(footnote):
            parts.append(f'<text x="{x:.2f}" y="{y + h + FOOT_GAP_MM + 0.88 * fs + i * 1.5 * fs:.2f}" font-size="{fs}">{_xml(t)}</text>')
        parts.append("</g>")
    if parts:
        _append_svg(out["SHEET"], parts)
    log("sheet", ident, title, os.path.basename(out["SHEET"]), tb)
    return sheet, out["SHEET"]


FOOT_GAP_MM = 5.0   # 表格下沿到脚注字顶


def footnote_mm(n_lines: int) -> float:
    """make_table_sheet 在表下写 n_lines 行脚注占的高度（图纸毫米），表格分页时留出来。"""
    return FOOT_GAP_MM + n_lines * 1.5 * schedules.NOTES_FONT_MM if n_lines else 0.0


def _stack_tables(sheet, origin, gap_mm):
    """把图纸布局里的表格（data-type="schedule"）从 origin 起往下排，去掉 Bonsai 给表格加的视图标题圈。
    返回每张表的 (x, y, 宽, 高)，用来写表名和紧跟在表下的脚注。"""
    layout = tool.Ifc.resolve_uri(tool.Drawing.get_document_uri(sheet, "LAYOUT"))
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK[1:-1])
    tree = ET.parse(layout)
    root = tree.getroot()
    x, y = origin
    col_w = 0.0
    out = []
    for g in root.findall(f"{{{SVG_NS}}}g[@data-type='schedule']"):
        for img in list(g.findall(f"{{{SVG_NS}}}image")):
            if img.get("data-type") == "view-title":
                g.remove(img)
        content = next(i for i in g.findall(f"{{{SVG_NS}}}image") if i.get("data-type") == "content")
        w, h = float(content.get("width")), float(content.get("height"))
        if y + h > AREA[3] and y > origin[1]:   # 这一列放不下，另起一列
            x, y, col_w = x + col_w + gap_mm, origin[1], 0.0
        content.set("x", str(x))
        content.set("y", str(y))
        out.append((x, y, w, h))
        y += h + gap_mm
        col_w = max(col_w, w)
    tree.write(layout)
    return out


_BREAK_AFTER, _BREAK_BEFORE = conventions.line_breaks()   # 自然断点（和房间名折行同一份）
_NO_LINE_START = "）)，,、。；;：:！？》/"
_NO_LINE_END = "（(《"
_TOKEN = re.compile(r"[A-Za-z0-9.#:×~+=_-]+|.")   # 字母数字串（编号、尺寸）不拆开


def wrap_text(s: str, width_mm: float, size: float) -> list[str]:
    """一段字按宽度折成几行（估宽用 schedules.text_mm），不缩字，行数取最少：
    - 断在自然断点（conventions.line_breaks：空格、"/"、右括号、顿号逗号等后面，左括号前面）不会多出一行时，就断在
      自然断点：括号外的优先，同样的取本行能放下的最后一个；
    - 否则断在字间（字母数字串不拆开），行首不放"）""，""/"、行尾不放"（"：带一个字下去；
    - 最后一处断在字间时，最后两行匀开（不留"线）""插座"这种短尾行）。
    结果只由文字、宽度、字号决定：同一条图例在各张图上断法一致，靠各张图的图例同宽。"""
    toks = _TOKEN.findall(str(s))
    n = len(toks)
    w = lambda i, j: schedules.text_mm("".join(toks[i:j]).strip(), size)
    depth = [0]   # depth[p]：第 p 个之前还没合上的括号数
    for t in toks:
        depth.append(max(0, depth[-1] + (t in "（(") - (t in "）)")))
    ok_cut = lambda q: toks[q] not in _NO_LINE_START and toks[q - 1] not in _NO_LINE_END

    def fit(i):
        """从第 i 个起，一行放到第几个（不含）：字间断，行首、行尾不放不该放的标点，行尾的空格算在本行。"""
        j = i + 1
        while j < n and w(i, j + 1) <= width_mm:
            j += 1
        while i + 1 < j < n and not ok_cut(j):
            j -= 1   # 带一个字下去
        while j < n and toks[j] == " ":
            j += 1
        return j

    def count(i):
        k = 0
        while i < n:
            i, k = fit(i), k + 1
        return k

    def natural(p):   # 断在第 p 个之前是自然断点
        return ok_cut(p) and (toks[p - 1] in _BREAK_AFTER or toks[p] in _BREAK_BEFORE)

    total, lines, starts, i, forced = count(0), [], [], 0, False
    while i < n:
        j = fit(i)
        if j < n:
            p = next((p for p in sorted(range(i + 1, j + 1), key=lambda p: (depth[p] > 0, -p))
                      if natural(p) and w(i, p) <= width_mm and len(lines) + 1 + count(p) <= total), None)
            forced, j = (p is None), (j if p is None else p)
        starts.append(i)
        lines.append("".join(toks[i:j]).strip())
        i = j
    if len(lines) >= 2 and forced:   # 最后两行匀开：两行都放得下，取较宽一行最窄的断法
        a = starts[-2]
        cut = [q for q in range(a + 1, n) if ok_cut(q) and toks[q] != " " and w(a, q) <= width_mm and w(q, n) <= width_mm]
        if cut:
            q = min(cut, key=lambda q: (round(max(w(a, q), w(q, n)), 3), round(abs(w(a, q) - w(q, n)), 3), q))
            lines[-2:] = ["".join(toks[a:q]).strip(), "".join(toks[q:]).strip()]
    return lines or [""]


def _xml(s: str) -> str:
    """写进图纸 SVG 的文字：XML 转义，非 ASCII 写成数字字符引用（和 Bonsai 写出的一致）。"""
    return html.escape(s, quote=False).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _fit_text(sheet_svg: str, cls: str, width_mm: float, max_lines: int, size: float) -> None:
    """标题栏里 class 为 cls 的那格字写不下一行时折成几行（tspan，行距 1.3 倍字号，整组在原位置上下居中），不缩字；
    超过 max_lines 行时照样全写出来，记一条 JZLOG warn。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    pitch = 1.3 * size

    def fix(m):
        head, body = m.group(1), html.unescape(m.group(2))   # SheetBuilder 写出的中文是数字字符引用
        lines = wrap_text(body, width_mm, size)
        if len(lines) <= 1:
            return m.group(0)
        if len(lines) > max_lines:
            log("warn titleblock text over", max_lines, "lines", cls, body)
        x = re.search(r'\bx="([^"]*)"', head).group(1)
        dy = [-(len(lines) - 1) / 2 * pitch] + [pitch] * (len(lines) - 1)
        return head + "".join(f'<tspan x="{x}" dy="{d:.2f}">{_xml(t)}</tspan>' for d, t in zip(dy, lines)) + "</text>"
    svg = re.sub(r'(<text\b[^>]*\bclass="[^"]*\b%s\b[^"]*"[^>]*>)([^<]*)</text>' % re.escape(cls), fix, svg)
    with open(sheet_svg, "w", encoding="utf-8") as fh:
        fh.write(svg)


def _fit_titleblock(sheet_svg: str) -> None:
    for cls, (size, n) in TB_WRAP.items():
        _fit_text(sheet_svg, cls, TB_TEXT_MM, n, size)


def _append_svg(sheet_svg: str, parts) -> None:
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = fh.read()
    with open(sheet_svg, "w", encoding="utf-8") as fh:
        fh.write(svg.replace("</svg>", "\n".join(parts) + "\n</svg>"))


def _sheet_page(sheet_svg: str, title: str, css: str) -> str:
    """打印页：图纸 SVG 直接内嵌进 HTML（样式和字体都生效）。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = re.sub(r"^<\?xml[^>]*>\s*", "", fh.read())
    return printing.page(title, svg, css=css)


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def export_png(sheet_svg: str, png_path: str, px_per_mm: float = 5.0) -> str:
    """用 Edge 无头截图出 PNG 预览（A3 整张；px_per_mm=5 约 2100×1485 像素）。"""
    css = "html,body{margin:0;padding:0;background:#fff}svg{width:1587px;height:1123px;display:block}"
    tmp = _write(os.path.splitext(png_path)[0] + ".png.html", _sheet_page(sheet_svg, "", css))
    printing.screenshot(tmp, png_path, (1587, 1123), px_per_mm * 25.4 / 96.0, temp=True)
    return png_path


# ------------------------------------------------------------------ 图例与 PDF
LEGEND_MM = 3.5          # 图例文字字号（标题"图例"5mm）
LEGEND_SYMBOL_MM = 16.0  # 符号列宽，名称列从这里起
LEGEND_GAP_MM = 3.0      # 图纸上找空地放图例时，离已画的东西至少这么远


def _pack(pack_symbols: str) -> dict:
    return symbols.load_pack(pack_symbols)


def _scope(sheet_svg: str) -> str:
    """Bonsai 把视图的样式规则都加上了前缀类 d<id>（sheeter.ensure_drawing_unique_styles），图例里的墙体色块要带上它。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        m = re.search(r'data-type="drawing" data-id="(\d+)"', fh.read())
    return f" d{m.group(1)}" if m else ""


def _legend(pack, keys, den, x, y, width, walls, extra, extra_width, scope, title):
    """图例的 SVG 片段（参数见 add_legend）。返回 (片段, 底边 y, 右边 x)。"""
    from .symbols import HEIGHT_CLASSES, item_polylines
    fs, pitch = LEGEND_MM, 1.35 * LEGEND_MM
    tx = x + LEGEND_SYMBOL_MM   # 名称列
    parts = ['<g id="jz-legend" font-family="FangSong" fill="none" stroke="#000">',
             f'<text x="{x:.2f}" y="{y:.2f}" font-size="5" fill="#000" stroke="none">{_xml(title)}</text>']
    yy, right = y + 3.0, x + schedules.text_mm(title, 5)

    def row(label, sym_h=0.0, h_text=None, w=width):
        """写一行名称（和右侧的默认高度），返回这一行的中线 y。"""
        nonlocal yy, right
        avail = x + w - tx - (schedules.text_mm(h_text, fs) + 2.0 if h_text else 0.0)
        lines = wrap_text(label, avail, fs)
        h = max(sym_h + 3.0, len(lines) * pitch + 2.5, 7.0)
        cy = yy + h / 2
        for i, t in enumerate(lines):
            parts.append(f'<text x="{tx:.2f}" y="{cy + 0.35 * fs + (i - (len(lines) - 1) / 2) * pitch:.2f}" font-size="{fs}" '
                         f'fill="#000" stroke="none">{_xml(t)}</text>')
        right = max(right, tx + max(schedules.text_mm(t, fs) for t in lines))
        if h_text:
            parts.append(f'<text x="{x + w:.2f}" y="{cy + 0.35 * fs:.2f}" font-size="{fs}" text-anchor="end" '
                         f'fill="#000" stroke="none">{h_text}</text>')
            right = max(right, x + w)
        yy += h
        return cy

    for k in keys:
        spec = pack[k]
        s = 1.0 / spec.get("paper_scale", den)   # 实际毫米 → 图纸毫米；有 paper_scale 的点位和图上一样按它的纸面大小
        pts_all = [p for it in spec["items"] for pts in item_polylines(it) for p in pts]
        ys = [py for _, py in pts_all]
        ymid = (max(ys) + min(ys)) / 2
        h_text = f"H{spec['default_height']:g}" if spec["ifc_type"][:-4] in HEIGHT_CLASSES and "default_height" in spec else None
        cy = row(spec["legend"], (max(ys) - min(ys)) * s, h_text)
        for it in spec["items"]:
            for pts in item_polylines(it):
                d = " ".join(f"{'M' if i == 0 else 'L'} {x + 7 + px * s:.3f},{cy - (py - ymid) * s:.3f}" for i, (px, py) in enumerate(pts))
                parts.append(f'<path d="{d}" stroke-width="0.3"/>')
    names = conventions.get("walls")
    for mat in [m_ for m_ in names if m_ in set(walls)]:
        cy = row(names[mat], 4.0)
        cls = "material-" + re.sub(r"[^0-9A-Za-z]", "", mat)   # Bonsai 生成材质类名时去掉连字符
        parts.append(f'<rect x="{x + 1:.2f}" y="{cy - 2:.2f}" width="12" height="4" class="cut {cls}{scope}"/>')
    if extra:
        row(extra, w=extra_width or width)
    parts.append("</g>")
    return parts, yy, right


def add_legend(sheet_svg: str, pack_symbols: str, keys, den: int, x: float, y: float, width: float = 64.0,
               walls=(), extra: str | None = None, title="图例", extra_width: float | None = None) -> float:
    """在排好的图纸 SVG 上加图例（不加外框）。每行"符号 | 名称 | 默认安装高度"，名称 3.5mm，写不下就换行（wrap_text），
    不缩字。x、y：标题"图例"的左下角（mm）。width：图例宽（符号列 16mm + 名称 + 默认高度）；一套图的图例取同一个宽，
    同一条名称在各张图上断法一致。
    - keys：symbols.json 的符号键，符号和图上一样大（有 paper_scale 的点位按它的纸面大小，其余按图纸比例）；插座、开关类
      （symbols.HEIGHT_CLASSES）在右侧写默认高度"H300"，图上只标和它不同的点位（annotate.point_heights，读同一份 symbols.json）；
    - walls：本图画出的墙材质名（annotate.wall_materials），按约定包 conventions.json 的 walls 顺序各一行；色块用和墙
      一样的 CSS 类（cut material-cnbearing 加上本图的 d<id> 前缀类），颜色和填充只在 cn.css 里定义；
    - extra：最后一行只有文字（总图的房间简称对照），宽 extra_width（缺省同 width）。
    返回图例底边 y（mm）；超出可放图区时记 JZLOG warn。三样都没有时不画。"""
    if not (keys or walls or extra):
        return y
    parts, bottom, _ = _legend(_pack(pack_symbols), keys, den, x, y, width, walls, extra, extra_width, _scope(sheet_svg), title)
    _append_svg(sheet_svg, parts)
    if bottom > AREA[3]:
        log("warn legend below drawing area", os.path.basename(sheet_svg), round(bottom, 1))
    return bottom


def place_legend(sheet_svg: str, pack_symbols: str, keys, den: int, near, width: float = 64.0, walls=(),
                 title="图例", extra: str | None = None) -> float | None:
    """没给图例留栏的图纸（只画墙体图例的平面图）：在可放图区里找一块空地放图例，不挪视图、不改比例。
    空地按 Edge 渲染出来的图纸量（_drawn_boxes），图例四周离已画的东西至少 LEGEND_GAP_MM；有几处空地时取离 near
    （标题"图例"左下角的理想位置，即同一套图里图例栏的位置）最近的。放不下或量不出来就不画，记一行 JZLOG。
    其余参数同 add_legend。返回图例底边 y（mm），没画返回 None。"""
    if not (keys or walls or extra):
        return None
    pack, scope, name = _pack(pack_symbols), _scope(sheet_svg), os.path.basename(sheet_svg)
    top = -5.0   # 标题"图例"（5mm 字）的字顶，相对它的基线
    _, bottom, right = _legend(pack, keys, den, 0.0, 0.0, width, walls, extra, None, scope, title)
    boxes = _drawn_boxes(sheet_svg)
    if boxes is None:
        log("legend skipped, sheet not measured", name)
        return None
    spot = _free_spot(boxes, right, bottom - top, (near[0], near[1] + top), LEGEND_GAP_MM)
    if spot is None:
        log("legend skipped, no room", name, f"{right:.0f}x{bottom - top:.0f}mm")
        return None
    x, y = spot[0], spot[1] - top
    parts, bottom, _ = _legend(pack, keys, den, x, y, width, walls, extra, None, scope, title)
    _append_svg(sheet_svg, parts)
    log("legend placed", name, round(x, 1), round(y, 1))
    return bottom


# 量图纸用的页内脚本：每个图形元素（不算图框标题栏、defs 里的定义）在页面上的外框，和各层 clip-path 的矩形取交，
# 换算成图纸毫米写进 <pre id="jz-out">（printing.dom_json 读）
_BOXES_JS = r"""<script>
window.addEventListener('load', () => {
  const svg = document.querySelector('svg'), R = svg.getBoundingClientRect(), k = R.width / __W__;
  const skip = 'defs, clipPath, marker, pattern, symbol, mask, [data-type="titleblock"]';
  const out = [];
  for (const el of svg.querySelectorAll('path, line, polyline, polygon, rect, circle, ellipse, text, use, image')) {
    if (el.closest(skip)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const b = el.getBoundingClientRect();
    if (!b.width && !b.height) continue;
    let r = [b.left, b.top, b.right, b.bottom];
    for (let p = el; p && p !== svg; p = p.parentElement) {
      const ref = 'clip-path:' + (p.getAttribute('clip-path') || '') + ';' + (p.getAttribute('style') || '');
      const m = /clip-path\s*:\s*url\(["']?#([^"')]+)/.exec(ref);
      const c = m && document.getElementById(m[1]);
      const cr = c && c.querySelector('rect');
      if (!cr) continue;
      const t = p.getScreenCTM(), x = +cr.getAttribute('x') || 0, y = +cr.getAttribute('y') || 0;
      const w = +cr.getAttribute('width'), h = +cr.getAttribute('height');
      const q = [[x, y], [x + w, y], [x, y + h], [x + w, y + h]].map(([u, v]) => new DOMPoint(u, v).matrixTransform(t));
      r = [Math.max(r[0], Math.min(...q.map(o => o.x))), Math.max(r[1], Math.min(...q.map(o => o.y))),
           Math.min(r[2], Math.max(...q.map(o => o.x))), Math.min(r[3], Math.max(...q.map(o => o.y)))];
    }
    if (r[2] < r[0] || r[3] < r[1]) continue;
    out.push(r.map((v, i) => +((v - (i % 2 ? R.top : R.left)) / k).toFixed(2)));
  }
  const pre = document.createElement('pre');
  pre.id = 'jz-out';
  pre.textContent = JSON.stringify(out);
  svg.remove();
  document.body.appendChild(pre);
});
</script>"""


def _drawn_boxes(sheet_svg: str):
    """图纸上已经画了东西的地方：[[x0, y0, x1, y1], …]（图纸毫米），用 Edge 渲染图纸 SVG 量出来（字的外框按实际字体）。
    量不出来返回 None。"""
    with open(sheet_svg, encoding="utf-8") as fh:
        svg = re.sub(r"^<\?xml[^>]*>\s*", "", fh.read())
    px = 10   # 每毫米几个像素，量得细一点
    svg = re.sub(r'(<svg\b[^>]*?)\swidth="[^"]*"', rf'\1 width="{_SHEET_W * px}px"', svg, count=1)
    svg = re.sub(r'(<svg\b[^>]*?)\sheight="[^"]*"', rf'\1 height="{_SHEET_H * px}px"', svg, count=1)
    tmp = _write(os.path.splitext(sheet_svg)[0] + ".boxes.html",
                 "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0}svg{display:block}</style></head>"
                 "<body>" + svg + _BOXES_JS.replace("__W__", str(_SHEET_W)) + "</body></html>")
    boxes = printing.dom_json(tmp, (_SHEET_W * px + 100, _SHEET_H * px + 100))
    os.remove(tmp)
    return boxes


def _free_spot(boxes, w, h, near, gap, step=0.5):
    """可放图区里一块 w×h（mm）的空地：四周离 boxes 里的每个框至少 gap。返回左上角 (x, y)，有几处时取离 near 最近的；
    没有返回 None。做法：按 step 把可放图区划成格子，标出被占的格子，用累加和表一次算出每个位置的块里有几格被占。"""
    import numpy as np
    x0, y0, x1, y1 = AREA
    nx, ny = int((x1 - x0) / step), int((y1 - y0) / step)
    kw, kh = int(math.ceil(w / step)), int(math.ceil(h / step))
    if kw > nx or kh > ny:
        return None
    occ = np.zeros((ny, nx), dtype=np.int64)
    for a, b, c, d in boxes:
        i0, i1 = max(0, int((a - gap - x0) / step)), min(nx, int(math.ceil((c + gap - x0) / step)))
        j0, j1 = max(0, int((b - gap - y0) / step)), min(ny, int(math.ceil((d + gap - y0) / step)))
        if i0 < i1 and j0 < j1:
            occ[j0:j1, i0:i1] = 1
    s = np.zeros((ny + 1, nx + 1), dtype=np.int64)
    s[1:, 1:] = occ.cumsum(0).cumsum(1)
    taken = s[kh:, kw:] - s[:-kh, kw:] - s[kh:, :-kw] + s[:-kh, :-kw]   # [j, i]：左上角在第 (i, j) 格的块里被占的格数
    js, iz = np.nonzero(taken == 0)
    if not len(js):
        return None
    xs, ys = x0 + iz * step, y0 + js * step
    k = int(np.argmin((xs - near[0]) ** 2 + (ys - near[1]) ** 2))
    return float(xs[k]), float(ys[k])


def export_pdf(sheet_svg: str, pdf_path: str, title: str = "") -> str:
    """用 Edge 无头打印成 A3 横向 PDF；title（"图号 图名"）是 PDF 的文档标题，阅读器标签页显示它。
    临时打印页（PDF 旁边的 .print.html）打印成功后删掉，失败时留着排查（日志 JZLOG pdf FAILED）。"""
    css = "@page{size:420mm 297mm;margin:0}html,body{margin:0;padding:0}svg{width:420mm;height:297mm;display:block}"
    tmp = _write(os.path.splitext(pdf_path)[0] + ".print.html", _sheet_page(sheet_svg, title, css))
    printing.html_to_pdf(tmp, pdf_path, temp=True)
    return pdf_path
