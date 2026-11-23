"""自动标注（在 Blender + Bonsai 里运行）：从 IFC 几何算出国内家装平面图的常用标注。

- 三道尺寸（国标三道）：由里到外依次是 洞口/墙垛（细部）、轴线（开间/进深）、总尺寸（外墙外皮外包）；四个方向各算一次，
  与外一道完全相同的就省掉；第一道离图样 10mm、各道间距 7mm（GB/T 50001-2017 第 11.3 节）。有 IfcGrid 的模型走
  block_plan + grid_annotations（细部按门窗构件沿外墙投影算，墙段在洞口处断开、没有 IfcOpeningElement 的识别模型也适用；
  分段图内部切口一侧量到切口轴，画折断线 cut_marks）；示例户型这类有洞口的模型走 three_chains，口径相同；
- 楼面标高：Bonsai 的 PLAN_LEVEL 标注，标高符号用约定包的（level_mark）；组合平面图的分区线和各段图号（zone_key）；
- 尺寸用 Bonsai 的 DIMENSION 标注画；写不下数字的小段（约小于 300）数字改用 TEXT 错开放（第二排、旁边或尺寸线另一侧，
  不缩字，排不开的用引出线，dim_chain_smart）；
- 门窗编号：Bonsai 的文字标注关联到门窗构件，内容写 {{Tag}}，出图时由 Bonsai 换成构件的编号（opening_tags），
  紧贴自己的洞口、和洞口在墙的同一侧、比离别的门窗都近，避开墙、门窗、开启范围、点位符号和已经写上的文字；
  不压家具设备和点位符号；旁边放不下的画引出线，字仍写在洞口两侧不隔墙的地方、不写在别的门前，引线落在洞口中点（_leader_tag）；
- 房间名 + 净面积：放在房间里离标注点最近、不压墙、门扇、家具设备和别的字的空地上（space_labels）；小房间放不下时
  从房外引出（_label_leader）；总图写简称，1:150 上写不下全名的小房间也写简称，图例里的简称对照按实际写过的生成（abbr_legend）；
- 水电点位只标和图例默认高度不同的安装高度（H400），紧贴自己的符号（point_heights）；顶面图每个房间标吊顶名称和标高
  （同 level_mark）；按纸面大小画的点位符号挑转角和变体，整个画在点位所在的房间里、不压墙（paper_pose），房间名和门窗编号
  把它们当硬障碍（paper_shapes）。

所有偏移量按"图纸上的毫米"给出，再按比例换算成实际尺寸（米）。字号从约定包 cn.css 读（含汉字的不小于 3.5mm，只有数字、
字母的小字用 num 档），尺寸排布参数从约定包
conventions.json 读（conventions.py）。改了 Blender 曲线/网格的标注（尺寸、轴号圈和引线、尺寸界线、折断线、标高线）
都用 Bonsai 的 Update Representation 写回 IFC（commit），保存后的 IFC 重新打开能原样重出图。
"""
from __future__ import annotations

import importlib
import itertools
import json
import math
import re

import bpy
import numpy as np
import ifcopenshell
import ifcopenshell.api as api
import ifcopenshell.geom
import ifcopenshell.util.element as uel
import ifcopenshell.util.selector as usel
import ifcopenshell.util.unit
import shapely
import shapely.affinity
import shapely.ops
from shapely.geometry import LineString, Polygon, box as _box
from mathutils import Matrix, Vector

import bonsai.tool as tool
import bonsai.core.drawing as core

from . import conventions, openings, symbols
from .printing import log
from .schedules import text_mm

for _m in ("pset", "drawing"):
    importlib.import_module("ifcopenshell.api." + _m)

_GS = ifcopenshell.geom.settings()
_GS.set("use-world-coords", True)
EPS = 0.02  # 米
WALL_TOUCH_M = 0.05   # 引线、编号到洞口的连线压墙不超过这么长（米）不算穿墙：线端碰到墙面、洞口的墙面

# 字号（图纸毫米，字号即字高）：从约定包 cn.css 读，不在代码里再写一份。size 是 Bonsai 的字号档的键（1.8 small、2.5 regular、
# 3.5 large、5.0 header、7.0 title；实际多大看 cn.css），另加 NUM：只有数字、字母的小字（GB/T 50001：汉字 ≥3.5mm，数字字母 ≥2.5mm）
_CSS = conventions.css_font_mm()
NUM = "num"
FONT_MM = {1.8: _CSS["small"], 2.5: _CSS["regular"], 3.5: _CSS["large"], 5.0: _CSS["header"], 7.0: _CSS["title"], NUM: _CSS["num"]}
DIM_FONT_MM = _CSS["text"]   # 尺寸数字（cn.css 的 text 默认字号）
LEVEL_FONT_MM = _CSS["PLANLEVEL"]   # 楼面标高的数字（Bonsai 的 PLAN_LEVEL 类）；cn.css 里和 small 写在同一条规则
LEVEL_SYMBOL_MM = conventions.marker_mm("plan-level-marker")   # 标高符号（约定包 markers-CN.svg）的宽、高
CJK = re.compile(r"[㐀-鿿]")


def small_size(literal):
    """最小一档字号：含汉字的用 small，只有数字、字母的用 NUM。"""
    return 1.8 if CJK.search(str(literal)) else NUM
DIM_BASE_MM = 1.0      # Bonsai 把尺寸数字的基线放在尺寸线上方 1mm
ROW_STEP_MM = 3.1      # 小段数字抬高一排的距离
DIM_OTHER_MM = 3.8     # 尺寸线另一侧那排（GB/T 50001 上下错开注写）数字的基线离尺寸线：数字顶离线约 1.3mm，躲开起止符
DIM_NEAR_MM = 5.0      # 小段数字离本段中点最好不超过这么远（图纸毫米），再远就读成别的段的数字了
DIM_INK = 0.82         # 尺寸数字（仿宋）实际墨迹的宽约为估的字宽（schedules.text_mm，每个数字 0.55 个字号）的这么多倍（Edge 实测）
DIM_LEADER_MM = 12.0   # 小段数字用引出线（GB/T 50001）时，引线全长不超过这么长（图纸毫米）


def _row_off(r):
    """小段数字第 r 排的基线离尺寸线多远（图纸毫米，朝写数字的一侧为正）：0 Bonsai 写数字的那排，1 再抬高一排，
    -1 尺寸线另一侧。"""
    return -DIM_OTHER_MM if r < 0 else DIM_BASE_MM + r * ROW_STEP_MM


def _dim_points(vals):
    """一道尺寸的各点（米）：排序，不到 20mm 的点合并。"""
    vs = []
    for v in sorted(vals):
        if not vs or v - vs[-1] > 0.02:
            vs.append(v)
    return vs


def _dim_room(a, b, den):
    """a～b（米）这一段里写得下自己的数字（数字宽 + 两头各 0.5mm，至少 5.2mm）：写不下的是"小段"，数字另放（dim_chain_smart）。"""
    P = lambda mm: mm * den / 1000.0
    return (b - a) >= max(P(text_mm(str(int(round((b - a) * 1000.0))), DIM_FONT_MM)) + 2 * P(0.5), P(5.2))
# 尺寸排布（约定包 conventions.json 的 dimensions，图纸毫米）：第一道尺寸离图样最外轮廓、平行尺寸线间距
# （GB/T 50001-2017 第 11.3 节，最内一道不宜小于 10mm，间距宜为 7～10mm）；最外一道到轴号圈；轴号圈半径；
# 尺寸界线离开图样轮廓、超出最外一道尺寸线的长度；角上数字最多伸出建筑边多少（相邻一侧第一道尺寸的数字从 10-1-2.9≈6mm 处开始）
_DIMS = conventions.get("dimensions")
CHAIN_MM = tuple(float(v) for v in _DIMS["chain_mm"])
BUBBLE_GAP_MM = float(_DIMS["bubble_gap_mm"])
BUBBLE_R_MM = float(_DIMS["bubble_r_mm"])
EXT_GAP_MM = float(_DIMS["ext_gap_mm"])
CORNER_MM = float(_DIMS["corner_mm"])
SIDES = ("bottom", "top", "left", "right")
REACH = float(_DIMS["reach_m"])   # 轴线端点离某一侧的外墙面（或切口）不超过这么多（米）就算伸到了这一侧


# ---------------------------------------------------------------- 视图信息、文字占位登记
_DEN: dict[int, int] = {}
_OCC: dict[int, list] = {}      # 视图 id → 已写文字的外框（米），编号避让用
_GEO: dict[int, "openings.PlanGeometry"] = {}
_OUTSIDE: dict[int, list] = {}  # 视图 id → 放在外墙外侧的编号外框（米），尺寸数字要避开
_LEVEL: dict[int, list] = {}    # 视图 id → 楼面标高的外框（米），房间名不排在它正上方、正下方
_GROUPS: dict[int, list] = {}   # 视图 id → [(房间轮廓, 房名组的外框)]（米）：引出的房名不贴着别的房名组写
_RESERVE: dict[int, list] = {}  # 视图 id → [(房名组的外框, 房间轮廓)]（米）：小房间、交通空间预留的房名位置，只对门窗编号生效（space_labels reserve）
_WIRES: dict[int, list] = {}   # 视图 id → 开关连线（米，线两侧各 WIRE_CLEAR_MM 的范围）：后放的字不压它（_obstacles）


def _den_of(drawing) -> int:
    k = drawing.id()
    if k not in _DEN:
        hs = (uel.get_pset(drawing, "EPset_Drawing") or {}).get("HumanScale") or "1:100"
        try:
            _DEN[k] = int(float(str(hs).split(":")[-1]))
        except ValueError:
            _DEN[k] = 100
    return _DEN[k]


def _is_rcp(drawing) -> bool:
    return (uel.get_pset(drawing, "EPset_Drawing") or {}).get("TargetView") == "REFLECTED_PLAN_VIEW"


def plan_geometry(f) -> "openings.PlanGeometry":
    """平面几何缓存（每个 IFC 文件算一次）。"""
    k = id(f)
    if k not in _GEO:
        _GEO[k] = openings.PlanGeometry(f)
    return _GEO[k]


def view_element_ids(drawing) -> set[int]:
    """视图画出的构件：Bonsai 的 tool.Drawing.get_drawing_elements（EPset_Drawing 的 Include/Exclude，含 Bonsai 界面存的
    JSON 写法，再和相机的范围、深度取交集），去掉标注；再加上按纸面大小用 SYMBOL 画的点位（paper_symbols，它们的类型
    在 Exclude 里）。视图要先建好（drawset.add_view 设好了相机）。"""
    bpy.context.view_layer.update()   # 刚挪过的相机，matrix_world 要求值后才是新位置
    return {e.id() for e in tool.Drawing.get_drawing_elements(drawing) if not e.is_a("IfcAnnotation")} | set(paper_symbols(drawing))


def paper_symbols(drawing) -> dict[int, tuple[float, int, int]]:
    """本视图按纸面大小用 SYMBOL 标注画的点位（drawset.add_view 建的，IfcRelAssignsToProduct 关联到点位）：
    {点位 id: (符号相对 symbols.json 实际尺寸的放大倍数 den / paper_scale, 比例分母 den, 视图 id)}（1:150 时放大 3 倍；视图 id 给
    paper_pose 取这张图上排开后的摆法）。"""
    den = _den_of(drawing)
    paper = symbols.paper_scales(conventions.pack_file("symbols.json"))
    out = {}
    for a in tool.Drawing.get_group_elements(tool.Drawing.get_drawing_group(drawing)):
        d = a.ObjectType == "SYMBOL" and paper.get(symbols.base_key(uel.get_pset(a, "EPset_Annotation", "Symbol") or ""))
        for r in (a.HasAssignments or ()) if d else ():
            if r.is_a("IfcRelAssignsToProduct"):
                out[r.RelatingProduct.id()] = (den / d, den, drawing.id())
    return out


def _outline_fp(geo, el):
    """算建筑外包用的平面轮廓（米）：墙、柱、凸窗用构件投影；普通门窗只算墙厚范围里的洞口（门框、门扇比墙面多出的一点不算，
    三道尺寸量的是墙）。"""
    if (el.is_a("IfcDoor") or el.is_a("IfcWindow")) and not openings._is_bay(el):
        fr = geo.frame(el)
        if fr is not None:
            return openings.oriented_box(fr["c"][0], fr["c"][1], fr["u"][0], fr["u"][1], fr["w"] / 2, fr["t"] / 2)
    return geo.fp(el)


def elements_extent(f, query, classes=("IfcWall", "IfcColumn", "IfcDoor", "IfcWindow")):
    """选择器选中的构件（默认只看墙、柱、门窗）在平面上的外包（米，_outline_fp）；分区图的尺寸按它画，不会压到伸出去的墙垛。"""
    geo = plan_geometry(f)
    bs = [p.bounds for e in usel.filter_elements(f, query) if e.is_a() in classes for p in [_outline_fp(geo, e)] if p is not None]
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


_oriented_box = openings.oriented_box


def _text_box(literal, size, den, x, y, align="center", rot=0.0):
    """文字外框（米）。literal 可以有 "\\n"：Bonsai 把它写成几行，行距 1em，(x, y) 按第一行对齐，后面的行往下排。"""
    fs = FONT_MM.get(size, DIM_FONT_MM)
    lines = str(literal).split("\n")
    w = max(text_mm(t, fs) for t in lines) * den / 1000.0
    h = fs * 0.9 * den / 1000.0
    k = (len(lines) - 1) * fs * den / 1000.0   # 第一行中心到最后一行中心
    a = math.radians(rot)
    ux, uy = math.cos(a), math.sin(a)
    if align.startswith("bottom"):   # 基线在 (x, y)，字往"上"长
        x, y = x - uy * h / 2, y + ux * h / 2
    if align.endswith("left"):       # 字从 (x, y) 往右写
        x, y = x + ux * w / 2, y + uy * w / 2
    elif align.endswith("right"):
        x, y = x - ux * w / 2, y - uy * w / 2
    return _oriented_box(x + uy * k / 2, y - ux * k / 2, ux, uy, w / 2, h / 2 + k / 2)


def bbox(el):
    """构件世界坐标包围框（米）：(x0, y0, z0, x1, y1, z1)。"""
    sh = ifcopenshell.geom.create_shape(_GS, el)
    v = sh.geometry.verts
    xs, ys, zs = v[0::3], v[1::3], v[2::3]
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


def _paper(mm, den):
    return mm * den / 1000.0


# ---------------------------------------------------------------- Bonsai 标注对象
def dim_chain(drawing, pts, z=0.0, object_type="DIMENSION"):
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type=object_type, relating_type=None)
    obj.matrix_world = Matrix.Identity(4)
    sp = obj.data.splines[0]
    if len(sp.points) < len(pts):
        sp.points.add(len(pts) - len(sp.points))
    for p, (x, y) in zip(sp.points, pts):
        p.co = (x, y, z, 1.0)
    commit(obj)
    return obj


def commit(obj):
    """把改过的 Blender 曲线/网格写回 IFC 表示：Bonsai 的"Update Representation"（bim.update_representation，
    经 tool.Geometry.run_geometry_update_representation 调用）。core.add_annotation 建的标注在 IFC 里只有默认几何；
    出 SVG 时 Bonsai 用的是 Blender 对象，所以图面是对的，但不写回的话 IFC 里的尺寸、轴号圈全是原点处的默认线，
    重新打开 IFC 或在 Bonsai 里重出图就坏了（IFC 是唯一数据源）。
    Bonsai 写回 PLAN_LEVEL/FALL/SECTION_LEVEL 时固定用 Model/Annotation/MODEL_VIEW 上下文（模型里没有就报错），
    这里先用 Bonsai 自己的 create_annotation_context 补上。"""
    el = tool.Ifc.get_entity(obj)
    if el is not None and tool.Drawing.is_annotation_object_type(el, ("PLAN_LEVEL", "FALL", "SECTION_LEVEL")) \
            and tool.Drawing.get_annotation_context("MODEL_VIEW") is None:
        tool.Drawing.create_annotation_context("MODEL_VIEW")
    tool.Geometry.run_geometry_update_representation(obj)


def polyline(drawing, pts, z=0.0, object_type="LINEWORK", classes=None):
    """一条线（Bonsai 的 LINEWORK / BREAKLINE 标注是网格对象：写顶点和边），点为米。
    classes：加到 SVG 上的样式类（Bonsai 读 EPset_Annotation.Classes），样式写在约定包的 cn.css 里。"""
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type=object_type, relating_type=None)
    obj.matrix_world = Matrix.Identity(4)
    me = obj.data
    me.clear_geometry()
    me.from_pydata([(x, y, z) for x, y in pts], [(i, i + 1) for i in range(len(pts) - 1)], [])
    me.update()
    commit(obj)
    if classes:
        f = tool.Ifc.get()
        ps = api.pset.add_pset(f, product=tool.Ifc.get_entity(obj), name="EPset_Annotation")
        api.pset.edit_pset(f, pset=ps, properties={"Classes": classes})
    return obj


def text(drawing, x, y, literal, size=2.5, z=0.0, align="center", rot=0.0, register=True, box_literal=None):
    """size：字号档（见 FONT_MM，实际大小由 cn.css 决定）。literal 里的 "\\n" 是换行。
    rot：逆时针转角（度），竖向文字用 90（从下往上读）。register：把文字外框登记到本视图的占位表，后放的编号会避开它；
    box_literal：估外框用的文字（内容是 {{Tag}} 这类变量时传实际文字）。"""
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="TEXT", relating_type=None)
    obj.location = (x, y, z)
    if rot:
        obj.rotation_euler = (0.0, 0.0, math.radians(rot))
    lit = tool.Drawing.get_text_literal(obj)
    lit.Literal = literal
    if hasattr(lit, "BoxAlignment"):
        lit.BoxAlignment = align
    _apply_size(obj, size)
    if register:
        _OCC.setdefault(drawing.id(), []).append(_text_box(box_literal or literal, size, _den_of(drawing), x, y, align, rot))
    return obj


def _apply_size(obj, size):
    """字号档写进 IFC 的 EPset_Annotation.Classes（Bonsai 出图时变成文字的样式类）：Bonsai 的五档用它的 edit_text_font_size，
    NUM 不是 Bonsai 的档，直接写类名（cn.css 的 text.num）。"""
    if size == NUM:
        f, el = tool.Ifc.get(), tool.Ifc.get_entity(obj)
        ps = tool.Pset.get_element_pset(el, "EPset_Annotation") or api.pset.add_pset(f, product=el, name="EPset_Annotation")
        api.pset.edit_pset(f, pset=ps, properties={"Classes": NUM})
    elif size != 2.5:
        tool.Drawing.edit_text_font_size(obj, size)


def _no_text(obj):
    """尺寸标注只画线和起止符、不写数字（Bonsai 的 BBIM_Dimension.ShowDescriptionOnly，且不给 Description）。"""
    f = tool.Ifc.get()
    el = tool.Ifc.get_entity(obj)
    ps = api.pset.add_pset(f, product=el, name="BBIM_Dimension")
    api.pset.edit_pset(f, pset=ps, properties={"ShowDescriptionOnly": True})


def dim_chain_smart(drawing, vals, fixed, horizontal, den, z=0.0, leaders=(), neighbor=(), limits=None, blocked=None, facing=(),
                    facing_ext=None):
    """画一道连续尺寸（_dim_plan 排好数字，_dim_draw 画出来），返回画了几段 DIMENSION 对象。参数见 _dim_plan。"""
    plan = _dim_plan(drawing, vals, fixed, horizontal, den, leaders, neighbor, limits, blocked, facing, facing_ext)
    return _dim_draw(drawing, plan, z) if plan else 0


def _dim_plan(drawing, vals, fixed, horizontal, den, leaders=(), neighbor=(), limits=None, blocked=None, facing=(), facing_ext=None):
    """排一道连续尺寸的数字（还不画）。vals：沿尺寸线的各点（米），fixed：尺寸线另一坐标；horizontal：尺寸线是否水平。

    写得下数字的段交给 Bonsai 的 DIMENSION 原样标（数字在线上方/左侧，国内习惯）；写不下的段（_dim_room，约小于 300，
    随比例变）用"只画线不写字"的 DIMENSION，数字另用 TEXT 错开：先试抬高一排居中，再试放进相邻段里紧贴尺寸界线，再在本段
    中点两边 DIM_NEAR_MM 以内沿线挪；都不行时写在尺寸线另一侧（GB/T 50001 的上下错开注写，_row_off(-1)）的本段附近；
    再不行按数字的实际墨迹宽（DIM_INK）量，先在本段正上方居中、再在本段附近找；还不行用引出线（GB/T 50001）：数字写在
    相邻段以内的空位上，引线从数字一端引出、折到本段中点的尺寸线上（_dim_leader）；引不出去时把挡路的一个数字挪开（它也
    可以引出）；都不行时写在离本段最近的空位上（记日志 dim text far）。第一排不能压起止符（各点）和
    轴号引线（leaders），第二排不能压引线和相邻一道尺寸的起止符（neighbor），另一侧那排不能压引线、那一侧相邻一道尺寸的
    数字（facing，沿线的 (起, 止)；最外一道是轴号圈的引线）和已写的字、引线；另一侧离相邻一道太近（facing_ext：那一道沿线
    的 (起, 止)，数字会落进它的数字带、横着读成那一道的数字）时，这一段范围里不用另一侧那排。同一排相邻两个数字尽量隔 1mm
    （免得连读），至少 0.5mm。尺寸数字一律用同一个字号（不缩字）；都放不下时取压得最少的位置并记日志（dim text crowded /
    dim text far）。
    同一排数字的先后会互相挤占：从左到右放，从右到左、短的段先放这两种先后要是各项都不比它差（挤的、离本段中点超过
    DIM_NEAR_MM 的个数，最远的距离）就取更好的；同值的数字不上下叠放（放不下才放宽）。
    返回排法 dict（spans：第 0、1 排数字占的沿线范围，相邻一道的另一侧那排要避开；lead：用引出线的段 → 引线折线，沿线、
    离线两个坐标，米）；不到两个点返回 None。"""
    P = lambda mm: mm * den / 1000.0
    blocked = blocked or {}
    vs = _dim_points(vals)
    if len(vs) < 2:
        return None
    segs = list(zip(vs[:-1], vs[1:]))
    texts = [str(int(round((b - a) * 1000.0))) for a, b in segs]
    W = [P(text_mm(t, DIM_FONT_MM)) for t in texts]
    m = P(0.5)
    rcp = _is_rcp(drawing)   # 顶面图视图是镜像的，自己放的文字方向对不上，全部交给 Bonsai
    room = [_dim_room(a, b, den) for a, b in segs]
    # Bonsai 把数字居中写；居中位置压到轴号引线的（总尺寸常见）也改成自己放
    clear = [not any((a + b) / 2 - W[i] / 2 - P(0.3) < q < (a + b) / 2 + W[i] / 2 + P(0.3) for q in leaders)
             and not any((a + b) / 2 - W[i] / 2 < q1 and (a + b) / 2 + W[i] / 2 > q0 for q0, q1 in blocked.get(0, ()))
             for i, (a, b) in enumerate(segs)]
    fits = [rcp or (room[i] and clear[i]) for i in range(len(segs))]
    occ = {0: [((a + b) / 2 - W[k] / 2, (a + b) / 2 + W[k] / 2) for k, (a, b) in enumerate(segs) if fits[k]], 1: [], -1: []}
    # 另一侧那排：已写的字、引线（室外的房名、编号）挡住的沿线范围；相邻一道的数字和轴号圈的引线也算。都多让 0.7mm（加上
    # ok 里的 0.3mm，左右隔开 1mm：上下错开的两排数字不读成一组）
    o0, o1 = P(_row_off(-1) - 0.2), P(_row_off(-1) + 2.9)
    band = []
    for o in _OCC.get(drawing.id(), []):
        x0, y0, x1, y1 = o.bounds
        if horizontal and y0 < fixed + o1 and y1 > fixed + o0:
            band.append((x0, x1))
        elif not horizontal and x0 < fixed - o0 and x1 > fixed - o1:
            band.append((y0, y1))
    near_other = [(q0 - P(0.7), q1 + P(0.7)) for q0, q1 in list(facing) + band]
    forb = {0: list(vs) + list(leaders) + list(blocked.get(0, ())), 1: list(leaders) + list(neighbor) + list(blocked.get(1, ())),
            -1: list(vs) + list(leaders) + list(blocked.get(-1, ())) + near_other}   # 起止符的短线两侧各伸出约 2mm，另一侧那排也不压
    if facing_ext:   # 另一侧离相邻一道太近：那一道的范围里（两头多 1mm）不用另一侧那排
        forb[-1].append((facing_ext[0] - P(1.0), facing_ext[1] + P(1.0)))
    lead = set(leaders)
    lo_lim, hi_lim = limits or (vs[0] - P(12), vs[-1] + P(12))
    gaps = (P(1.0), P(0.5))   # 同一排相邻两个数字的间隙：先按 1mm（免得 "100" "1000" 连读），放不下再按 0.5mm

    def ok(row, x0, x1, gap, occ, same=(), pm=0.3, ends=()):
        """pm：离轴号引线至少多远（图纸毫米；离起止符总是 0.3mm）。ends：不算压线的本段两头的起止符（_dim_plan 的 spot）。"""
        if x0 < lo_lim or x1 > hi_lim:
            return False
        if any((x0 < q[1] + P(0.3) and x1 > q[0] - P(0.3)) if isinstance(q, tuple)
               else (q not in ends and x0 - P(pm if q in lead else 0.3) < q < x1 + P(pm if q in lead else 0.3)) for q in forb[row]):
            return False
        if any(x0 < q1 and x1 > q0 for q0, q1 in same):   # 同值的数字在另一排正上方或正下方：读不出各是哪段的
            return False
        return not any(x0 < q1 + gap and x1 > q0 - gap for q0, q1 in occ[row])

    def candidates(a, b, w, mid, long_enough):
        st = P(0.4)
        cands = [(0, mid)]   # 本段正中：离两端起止符各 0.3mm 以上就写在这里（小段的小字常常放得下）
        if long_enough:   # 段够长、只是居中会压引线：先在本段里沿线挪
            for s in range(1, int((b - a) / st) + 1):
                cands += [(0, c) for c in (mid + s * st, mid - s * st) if a + m + w / 2 <= c <= b - m - w / 2]
        cands += [(1, mid), (0, b + m + w / 2), (0, a - m - w / 2)]
        for s in range(1, 32):
            cands += [(1, mid + s * st), (1, mid - s * st)]
        cands += [(0, b + m + w / 2 + s * st) for s in range(1, 40)] + [(0, a - m - w / 2 - s * st) for s in range(1, 40)]
        return cands

    def near_cands(a, b, w, mid, long_enough):
        """离本段中点 DIM_NEAR_MM 以内的位置（三排，含尺寸线另一侧），由近到远。"""
        st = P(0.4)
        other = [(-1, mid)] + [(-1, c) for s in range(1, 32) for c in (mid + s * st, mid - s * st)]
        return sorted((rc for rc in candidates(a, b, w, mid, long_enough) + other if abs(rc[1] - mid) <= P(DIM_NEAR_MM) + 1e-9),
                      key=lambda rc: round(abs(rc[1] - mid) / st))

    def crowd_cost(r, c, w, mid, occ):
        x0, x1 = c - w / 2, c + w / 2
        cost = sum(max(0.0, min(x1, q1) - max(x0, q0)) for q0, q1 in occ[r]) * 10
        cost += sum(1 for q in forb[r] if not isinstance(q, tuple) and x0 < q < x1) * P(1.0)
        cost += sum(max(0.0, min(x1, q[1]) - max(x0, q[0])) for q in forb[r] if isinstance(q, tuple)) * 10   # 轴号圈、引线、室外编号
        return cost + abs(c - mid) * 0.01

    def near_first(a, b, w, mid, long_enough):
        """先试离本段中点 DIM_NEAR_MM 以内的位置（三排，由近到远），再试原来那些远的位置、另一侧远的位置。"""
        st = P(0.4)
        far = [rc for rc in candidates(a, b, w, mid, long_enough) if abs(rc[1] - mid) > P(DIM_NEAR_MM) + 1e-9]
        far += [(-1, c) for s in range(1, 32) for c in (mid + s * st, mid - s * st) if abs(c - mid) > P(DIM_NEAR_MM) + 1e-9]
        return near_cands(a, b, w, mid, long_enough) + far

    def place(order, cands):
        """按 order 的先后一段段放写不下的小段数字（每段取 cands 给的候选里第一个放得下的）：返回 {段: (排, 中心, 字号, 宽, 挤)}。"""
        oc = {r: list(v) for r, v in occ.items()}
        put = {}
        for k in order:
            a, b = segs[k]
            mid = (a + b) / 2
            pick = None
            for nostack in (True, False):
                for size, gap in ((2.5, g_) for g_ in gaps):   # 不缩字：放不下的交给 relocate、引出线
                    w = P(text_mm(texts[k], FONT_MM[size]))
                    same = lambda r: [(c_ - w_ / 2, c_ + w_ / 2) for kk, (r_, c_, _, w_, _) in put.items()
                                      if nostack and r_ != r and texts[kk] == texts[k]]
                    pick = next(((r, c) for r, c in cands(a, b, w, mid, room[k]) if ok(r, c - w / 2, c + w / 2, gap, oc, same(r))), None)
                    if pick:
                        break
                if pick:
                    break
            crowded = pick is None
            if crowded:   # 实在放不下：取和别的数字重叠最少的位置
                pick = min(candidates(a, b, w, mid, room[k]), key=lambda rc: crowd_cost(rc[0], rc[1], w, mid, oc))
            r, c = pick
            oc[r].append((c - w / 2, c + w / 2))
            put[k] = (r, c, size, w, crowded)
        return put

    off = lambda put, k: abs(put[k][1] - (segs[k][0] + segs[k][1]) / 2) * 1000.0 / den

    def score(put):
        """挤的、退成小字的、离本段中点超过 DIM_NEAR_MM 的个数，最远的离多远，同一排挨得不到 1mm 的对数，总共离多远：越小越好。"""
        offs = [off(put, k) for k in put]
        close = 0
        for row in occ:
            bs = sorted([(c - w / 2, c + w / 2) for r, c, _, w, _ in put.values() if r == row] + list(occ[row]))
            close += sum(1 for (_, a1), (b0, _) in zip(bs, bs[1:]) if b0 - a1 < P(1.0))
        return (sum(v[4] for v in put.values()), sum(v[2] != 2.5 for v in put.values()), sum(o > DIM_NEAR_MM for o in offs),
                round(max(offs, default=0.0), 1), close, round(sum(offs), 1))

    bad_ = lambda put, k: put[k][4] or put[k][2] != 2.5 or (not room[k] and off(put, k) > DIM_NEAR_MM)

    def line_occ(paths, skip=None):
        """引线（_dim_leader 的折线）在各排数字带里占的沿线范围：{排: [(起, 止)]}。"""
        out = {r: [] for r in occ}
        h = P(0.9 * FONT_MM[2.5])
        for kk, p_ in paths.items():
            if kk == skip:
                continue
            ln = LineString(p_)
            for r in out:
                seg = ln.intersection(_box(-1e9, P(_row_off(r)), 1e9, P(_row_off(r)) + h))
                if not seg.is_empty:
                    x0_, _, x1_, _ = seg.bounds
                    out[r].append((x0_ - P(0.3), x1_ + P(0.3)))
        return out

    ADJ = {0: (1,), 1: (0,), -1: ()}   # 上下相邻的排（第 -1 排在尺寸线另一侧，和第 0 排不相邻）

    def stacked(r, x0, x1, oc):
        """数字和上下相邻一排的数字左右重叠超过 1mm（叠成两层，读成一组）。"""
        return any(min(x1, q1) - max(x0, q0) > P(1.0) for rr in ADJ[r] if rr in oc for q0, q1 in oc[rr])

    def spot(put, k, sizes, edge=False, ink=False, paths=None, nostack=False):
        """k 在本段附近（near_cands）的位置，别的数字都不动：(排, 中心, 字号, 宽) 或 None。先按离轴号引线 0.3mm 找，找不到再
        在第一排以外不留余量、和同排的数字只隔 0.3mm（估的字宽本来比实际宽约一成，两根引线之间的段也写得下）。edge=True：
        在本段正上方（三排之一）居中写，按数字的实际墨迹宽（DIM_INK）量，离轴号引线、别的起止符和数字仍留 0.3mm；段比墨迹宽
        两头各多 0.08mm 以上时，本段两头的起止符短线（不是轴号引线）落在数字两端以外，不算压线。ink=True：同样按墨迹宽、
        留 0.3mm，在本段附近找。这两种返回的宽是墨迹宽（后放的数字按它避让）。paths：已画的数字引线，不压；nostack=True：
        不和上下相邻一排的数字（还要挪的不算）叠成两层（stacked）。"""
        a, b = segs[k]
        mid = (a + b) / 2
        oc = {r: list(v) for r, v in occ.items()}
        os_ = {r: list(v) for r, v in occ.items()}
        for kk, (r_, c_, _, w_, _) in put.items():
            if kk != k:
                oc[r_].append((c_ - w_ / 2, c_ + w_ / 2))
                if not bad_(put, kk) or kk in (paths or {}):
                    os_[r_].append((c_ - w_ / 2, c_ + w_ / 2))
        for r_, v_ in line_occ(paths or {}, k).items():
            oc[r_] += v_
        for size, pm, gap in ((s_, m_, g_) for s_ in sizes for m_ in (0.3, 0.0) for g_ in (gaps if m_ else gaps + (P(0.3),))):
            w = P(text_mm(texts[k], FONT_MM[size]))
            same = lambda r: [(c_ - w_ / 2, c_ + w_ / 2) for kk, (r_, c_, _, w_, _) in put.items() if kk != k and r_ != r and texts[kk] == texts[k]]
            if edge or ink:
                if not pm:
                    continue
                wi = DIM_INK * w
                ends = {a, b} - lead if edge and b - a >= wi + P(0.15) else ()
                pick = next(((r, c) for r, c in near_cands(a, b, w, mid, room[k]) if (c == mid or not edge)
                             and ok(r, c - wi / 2, c + wi / 2, gap, oc, same(r), pm, ends)
                             and not (nostack and stacked(r, c - wi / 2, c + wi / 2, os_))), None)
                if pick:
                    return pick[0], pick[1], size, wi
                continue
            pick = next(((r, c) for r, c in near_cands(a, b, w, mid, room[k]) if (pm or r)
                         and ok(r, c - w / 2, c + w / 2, gap, oc, same(r), pm)), None)
            if pick:
                return pick[0], pick[1], size, w
        return None

    def relocate(put):
        """离本段远的小段数字、挤的、退成小字的挪到本段附近（正常字号；挤的才退到小字）：先只挪它自己；挪不动时再试把挡着它的
        一个数字在那个数字自己的本段附近挪一挪；再不行才在另一侧居中、两头贴着本段的起止符短线写。这几个数字谁先挪试四种先后
        （最远的先、最近的先、从左到右、从右到左），取各项最好的。"""
        bad = [k for k in put if bad_(put, k)]
        if not bad:
            return put
        tries = [move(dict(put), o) for o in (sorted(bad, key=lambda k: -off(put, k)), sorted(bad, key=lambda k: off(put, k)), sorted(bad),
                                               sorted(bad, reverse=True))]
        return min(tries, key=score)

    def swap(put, k, sizes):
        """把挡着 k 的一个数字 j（离 k 近的先试）在 j 自己的本段附近挪开，再放 k。成了返回 True。"""
        mid = (segs[k][0] + segs[k][1]) / 2
        for j in sorted((j for j in put if j != k and not bad_(put, j)), key=lambda j: abs(put[j][1] - mid)):
            if abs(put[j][1] - mid) > P(2 * DIM_NEAR_MM + 8.0):
                break
            trial = {kk: v for kk, v in put.items() if kk != j}
            s = spot(trial, k, sizes)
            if s is None:
                continue
            trial[k], trial[j] = s + (False,), put[j]
            sj = spot(trial, j, (put[j][2],))
            if sj:
                trial[j] = sj + (False,)
                put.update(trial)
                return True
        return False

    def move(put, order):
        for k in order:
            if not bad_(put, k):
                continue
            sizes = (2.5,)
            s = spot(put, k, sizes)
            if s:
                put[k] = s + (False,)
            elif not swap(put, k, sizes):
                s = spot(put, k, sizes, edge=True, nostack=True) or spot(put, k, sizes, ink=True, nostack=True)
                if s:
                    put[k] = s + (False,)
        return put

    def _dim_leader(k, put, ldr, hug=False):
        """k 的数字用引出线写：数字在三排之一、不越过相邻段（前一段的起点到后一段的终点以内），不压
        起止符、轴号引线和别的数字；引线从数字靠本段的一端顺着数字引出 LEADER_RUN_MM 以上，再按 45°/60°/90° 折到本段中点的
        尺寸线上（_leader_path），不穿过别的数字和别的引线，全长不超过 DIM_LEADER_MM。取"引线长 + 排的代价"最小的：
        ((排, 中心, 字号, 宽, False), 引线折线)，折线是 (沿线, 离线) 坐标（米，离线朝第 0 排一侧为正）；没有返回 None。
        ldr：别的数字的引线。引线的折点离同一排别的数字 0.6mm 以上，免得读成"600—900"；和上下相邻一排的
        数字叠成两层的加代价。垂直于尺寸线的一段不贴着轴号引线（1mm 以内）走，hug=True（实在没有别的写法）时才可以。"""
        a, b = segs[k]
        mid = (a + b) / 2
        w = P(text_mm(texts[k], FONT_MM[2.5]))
        h = P(0.9 * FONT_MM[2.5])
        # 引线从相邻段里引出；相邻段比数字还窄（墙垛、洞口边的小段）时可以再隔一段
        i0 = k - 1 - (k >= 2 and segs[k - 1][1] - segs[k - 1][0] < w)
        i1 = k + 1 + (k + 2 < len(segs) and segs[k + 1][1] - segs[k + 1][0] < w)
        lo = segs[i0][0] if i0 >= 0 else lo_lim
        hi = segs[i1][1] if i1 < len(segs) else hi_lim
        oc = {r: list(v) for r, v in occ.items()}
        os_ = {r: list(v) for r, v in occ.items()}
        rects = [_box(q0, P(_row_off(0)), q1, P(_row_off(0)) + h) for q0, q1 in occ[0]]   # Bonsai 写的数字
        for kk, (r_, c_, _, w_, _) in put.items():
            if kk != k:
                oc[r_].append((c_ - w_ / 2, c_ + w_ / 2))
                if not bad_(put, kk) or kk in ldr:
                    os_[r_].append((c_ - w_ / 2, c_ + w_ / 2))
                rects.append(_box(c_ - w_ / 2, P(_row_off(r_)), c_ + w_ / 2, P(_row_off(r_)) + h))
        rects = [r_.buffer(-P(0.1)) for r_ in rects]
        lines = [LineString(p_) for kk, p_ in ldr.items() if kk != k]
        for r_, v_ in line_occ(ldr, k).items():
            oc[r_] += v_
        best, st = None, P(0.4)
        for r, pen in ((0, 0.0), (1, P(0.5)), (-1, P(2.0))):
            if r not in occ:
                continue
            # 引出的字（TEXT_LEADER，字按"中线"对齐在折线第一点）：墨迹和同一排的数字（底边在这一排的基线）齐平（Edge 实测）
            t = P(_row_off(r) + 0.22 * FONT_MM[2.5])
            for sgn in (1, -1):   # 数字在本段右（上）边、左（下）边
                for s in range(200):
                    e = mid + sgn * (P(LEADER_RUN_MM) + s * st)   # 数字靠本段的一端
                    c = e + sgn * w / 2
                    if not lo <= e <= hi:   # 引线不越过相邻段（数字从相邻段里引出）
                        break
                    if not ok(r, c - w / 2, c + w / 2, gaps[0], oc, (), 0.3):
                        continue
                    a_ = abs(e - mid)   # 字端到落点沿线的距离
                    if a_ > P(DIM_LEADER_MM):
                        break
                    hs = [a_ - abs(t) / math.tan(math.radians(ang)) for ang in LEADER_ANGLES] + [a_]   # 先顺着字引出的长：45°、60°、直角
                    found = False
                    for h_ in hs:
                        if h_ < P(LEADER_RUN_MM) - 1e-9:
                            continue
                        path = [(e, t), (e - sgn * h_, t), (mid, 0.0)]
                        line = LineString(path)
                        if line.length > P(DIM_LEADER_MM) or any(line.intersects(r_) for r_ in rects) \
                                or any(line.intersects(l_) for l_ in lines):
                            continue
                        # 引线引出的一头：折点往外 0.6mm 以内同一排没有别的数字（横段不贴着别的数字，读不成"600—900"；字框是估的，墨迹还要再空约 0.5mm）
                        z0, z1 = (path[1][0] - P(0.6), e) if sgn > 0 else (e, path[1][0] + P(0.6))
                        if any(q0 < z1 and q1 > z0 for q0, q1 in oc[r]):
                            continue
                        # 直角折下的一段贴着轴号引线（1mm 以内）走：和轴线读成一根线
                        right = h_ == hs[-1]
                        if right and not hug and any(abs(mid - q) < P(1.0) for q in lead):
                            continue
                        # 代价：引线长；和上下相邻一排叠成两层的加 3mm；折成直角的（先顺着尺寸线再垂直落下，像个钩）加 2mm
                        cost = line.length + pen + (P(3.0) if stacked(r, c - w / 2, c + w / 2, os_) else 0.0) + (P(2.0) if right else 0.0)
                        if best is None or cost < best[0]:
                            best = (cost, (r, c, 2.5, w, False), [tuple(map(float, p_)) for p_ in path])
                        found = not right
                        break
                    if found:
                        break   # 再远只会更长
        return best and best[1:]

    todo = [k for k in range(len(segs)) if not fits[k]]
    orders = [todo, todo[::-1], sorted(todo, key=lambda k: (segs[k][1] - segs[k][0], k))]
    # 原来的排法（第一、二排）：从左到右放；从右到左、短的段先放这两种先后要是各项都不比它差（挤的、小字、离得远的个数，最远的
    # 距离，挨得近的对数），取更好的
    best = place(todo, candidates)
    if len(todo) > 1:
        base = score(best)
        for alt in (place(o, candidates) for o in orders[1:]):
            sc = score(alt)
            if all(a_ <= b_ for a_, b_ in zip(sc[:5], base[:5])) and sc < score(best):
                best = alt
    # 再把离本段远的、挤的、退成小字的挪到本段附近：三排里离中点 DIM_NEAR_MM 以内，含尺寸线另一侧（GB/T 50001 的上下错开注写）。
    # 先只挪这些（别的数字不动）；再按三种先后、每个数字都先试本段附近重排，只要原来在本段附近的数字一个都没被挤远、各项更好就换
    best = relocate(best)
    near = lambda put: {k for k in put if not put[k][4] and off(put, k) <= DIM_NEAR_MM}
    keep = near(best)
    for alt in (relocate(place(o, near_first)) for o in (orders if len(todo) > 1 and len(keep) < len(todo) else ())):
        if keep <= near(alt) and score(alt)[:3] < score(best)[:3]:   # 只为少几个挤的、小字、远的才重排
            best = alt
    # 还是挤的、离本段远的：用引出线（GB/T 50001），离本段远的先引；引不出去时把挡路的一个数字挪开（离得近的先试），
    # 它也可以引出
    ldr = {}

    def settle(put, lk, k, near=True, hug=False, stack_first=False):
        """k 依次找：本段附近（near=True 时）、本段正上方或附近按墨迹宽（不叠两层）、引出线、按墨迹宽叠两层也行、
        （hug=True 时）引线贴着轴号引线；stack_first=True：叠两层的先于引出线（挪别的数字时，k 占了引出线的空地别的数字就
        没处放）：((排, 中心, 字号, 宽, False), 引线折线或 None) 或 None。"""
        s = (near and spot(put, k, (2.5,), paths=lk)) or spot(put, k, (2.5,), edge=True, paths=lk, nostack=True) \
            or spot(put, k, (2.5,), ink=True, paths=lk, nostack=True)
        if s:
            return s + (False,), None
        stacked_ = lambda: spot(put, k, (2.5,), edge=True, paths=lk) or spot(put, k, (2.5,), ink=True, paths=lk)
        s = stack_first and stacked_()
        if s:
            return s + (False,), None
        got = _dim_leader(k, put, lk)
        if got:
            return got
        s = stacked_()
        if s:
            return s + (False,), None
        return _dim_leader(k, put, lk, hug=True) if hug else None

    def fix(k, hug):
        """k 自己找（settle）；不行把挡路的一个数字 j（离 k 近的先试）挪开，k、j 都放得下就换。成了返回 True。"""
        nonlocal best, ldr
        got = settle(best, ldr, k, near=False, hug=hug)
        if got:
            best[k] = got[0]
            if got[1]:
                ldr[k] = got[1]
            return True
        mid = (segs[k][0] + segs[k][1]) / 2
        for j in sorted((j for j in best if j != k and (j in ldr or not bad_(best, j))), key=lambda j: abs(best[j][1] - mid)):
            if abs(best[j][1] - mid) > P(2 * DIM_NEAR_MM + 8.0):
                break
            for sf in (False, True):
                trial = {kk: v for kk, v in best.items() if kk != j}
                tl = {kk: v for kk, v in ldr.items() if kk != j}
                gk = settle(trial, tl, k, stack_first=sf)
                if gk is None:
                    continue
                trial[k] = gk[0]
                if gk[1]:
                    tl[k] = gk[1]
                gj = settle(trial, tl, j, hug=hug)
                if gj is None:
                    continue
                trial[j] = gj[0]
                if gj[1]:
                    tl[j] = gj[1]
                best, ldr = trial, tl
                return True
        return False

    def nearest(k):
        """都放不下：三排里离本段中点最近的空位（不压别的数字、起止符、轴号引线和数字引线）。"""
        a, b = segs[k]
        mid = (a + b) / 2
        w = P(text_mm(texts[k], FONT_MM[2.5]))
        oc = {r: list(v) for r, v in occ.items()}
        for kk, (r_, c_, _, w_, _) in best.items():
            if kk != k:
                oc[r_].append((c_ - w_ / 2, c_ + w_ / 2))
        for r_, v_ in line_occ(ldr, k).items():
            oc[r_] += v_
        cands = [(abs(c - mid), r, c) for r in occ for s in range(64) for c in (mid + s * P(0.4), mid - s * P(0.4))
                 if ok(r, c - w / 2, c + w / 2, gaps[1], oc)]
        if cands and min(cands)[0] < abs(best[k][1] - mid) - 1e-9:
            _, r, c = min(cands)
            best[k] = (r, c, 2.5, w, False)

    bad2 = lambda k: bad_(best, k) and k not in ldr
    for _ in range(2):   # 挪过别的数字以后再来一遍
        for k in sorted((k for k in best if bad2(k)), key=lambda k: -off(best, k)):
            if bad2(k) and not fix(k, False):
                fix(k, True)   # 实在没有别的写法：引线可以贴着轴号引线
    for k in [k for k in best if bad2(k)]:   # 别的都排好以后，还放不下的写在离本段最近的空位上
        nearest(k)
    spans = list(occ[0]) + [(c - w / 2, c + w / 2) for r, c, _, w, _ in best.values() if r >= 0]
    return dict(vs=vs, segs=segs, texts=texts, fits=fits, W=W, rcp=rcp, fixed=fixed, horizontal=horizontal, den=den, todo=todo,
                put=best, spans=spans, lead=ldr)


def _dim_draw(drawing, plan, z=0.0):
    """按 _dim_plan 的排法画：连续的写得下（写不下）数字的段合成一个 DIMENSION，小段数字写 TEXT。返回 DIMENSION 对象数。"""
    vs, segs, texts, fits, W, den = plan["vs"], plan["segs"], plan["texts"], plan["fits"], plan["W"], plan["den"]
    fixed, horizontal = plan["fixed"], plan["horizontal"]
    P = lambda mm: mm * den / 1000.0
    pt = (lambda v: (v, fixed)) if horizontal else (lambda v: (fixed, v))
    n_obj, i = 0, 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and fits[j + 1] == fits[i]:
            j += 1
        obj = dim_chain(drawing, [pt(vs[k]) for k in range(i, j + 2)], z)
        if not fits[i]:
            _no_text(obj)
        n_obj += 1
        i = j + 1
    for k, (a, b) in enumerate(segs):   # Bonsai 自己写的数字也登记进本视图的占位表
        if fits[k] and not plan["rcp"]:
            mid = (a + b) / 2
            _OCC.setdefault(drawing.id(), []).append(
                _text_box(texts[k], 2.5, den, mid, fixed + P(DIM_BASE_MM), "bottom-middle") if horizontal
                else _text_box(texts[k], 2.5, den, fixed - P(DIM_BASE_MM), mid, "bottom-middle", 90.0))
    lead = plan.get("lead", {})
    for k in plan["todo"]:
        r, c, size, w, crowded = plan["put"][k]
        mid = (segs[k][0] + segs[k][1]) / 2
        if k in lead:   # 引出线（Bonsai 的 TEXT_LEADER，数字在折线的第一点，圆点落在本段中点的尺寸线上）
            pts = [(s, fixed + t) for s, t in lead[k]] if horizontal else [(fixed - t, s) for s, t in lead[k]]
            first, rot = c > mid, 0.0 if horizontal else 90.0
            _leader(drawing, pts, texts[k], size, first, z, rot=rot)
            _OCC.setdefault(drawing.id(), []).append(
                _text_box(texts[k], size, den, pts[0][0], pts[0][1], "middle-left" if first else "middle-right", rot))
            log("dim text leader", texts[k], round(mid, 3))
            continue
        if crowded:
            log("dim text crowded", texts[k], round(mid, 3))
        elif size != 2.5:
            log("dim text small", texts[k], round(mid, 3))
        elif abs(c - mid) > P(DIM_NEAR_MM) and not _dim_room(*segs[k], den):
            log("dim text far", texts[k], round(mid, 3))
        off = P(_row_off(r))
        # 登记进占位表：后放的文字（门窗编号、标高、接图说明）会避开这些小段数字
        if horizontal:
            text(drawing, c, fixed + off, texts[k], size=size, z=z, align="bottom-middle")
        else:
            text(drawing, fixed - off, c, texts[k], size=size, z=z, align="bottom-middle", rot=90.0)
    return n_obj


def _blocked_rows(drawing, side, fixed, den):
    """放在外墙外侧的门窗编号、房名挡住了这道尺寸哪几排数字的哪些位置：{排: [(起, 止), ...]}（沿尺寸线坐标，米；
    排见 _row_off）。"""
    P = lambda mm: mm * den / 1000.0
    out = {0: [], 1: [], -1: []}
    for b in _OUTSIDE.get(drawing.id(), []):
        x0, y0, x1, y1 = b.bounds
        for r in out:
            lo_off, hi_off = P(_row_off(r) - 0.2), P(_row_off(r) + 2.9)
            if side in ("bottom", "top"):     # 数字在线上方（+y）
                if y0 < fixed + hi_off and y1 > fixed + lo_off:
                    out[r].append((x0, x1))
            elif x0 < fixed - lo_off and x1 > fixed - hi_off:   # 竖线：数字在线左侧（-x）
                out[r].append((y0, y1))
    return out


def _draw_side_chains(drawing, chains, side, edge, den, z, leaders=(), limits=None, outer_row1_forbid=()):
    """一侧的几道尺寸由里到外排（CHAIN_MM 间距）。数字在水平线上方、竖线左侧，所以底边、右边的数字朝里，
    顶边、左边的数字朝外；第二排文字要避开的"相邻一道"据此取里面一道或外面一道。尺寸线另一侧那排数字朝着另一边的
    相邻一道（顶、左两侧朝里，底、右两侧朝外；底、右两侧最外一道朝着轴号圈的引线），要避开那一道的数字，所以那一道先排
    （排好以后仍按由里到外的顺序画）；相邻一道离得不够远（空不出一个字高）时，在那一道的范围里不用另一侧那排。底、右两侧
    返回最外一道离建筑边的距离。"""
    P = lambda mm: mm * den / 1000.0
    bx0, by0, bx1, by1 = edge
    horizontal = side in ("bottom", "top")
    text_out = side in ("top", "left")
    offs = [CHAIN_MM[0] + i * CHAIN_MM[1] for i in range(len(chains))]
    plans = [None] * len(chains)
    # 另一侧那排（字框离线 DIM_OTHER_MM + 0.2）和朝着的相邻一道的第一排数字（字框离那道线 DIM_BASE_MM + 字高）之间空不出一个
    # 字高：数字落进那一道的数字带，横着读成那一道的数字（"2400 900 3300"），这一段范围里不用另一侧那排
    near_face = CHAIN_MM[1] - (DIM_BASE_MM + 0.9 * DIM_FONT_MM) - (DIM_OTHER_MM + 0.2) < DIM_FONT_MM
    for i in (range(len(chains)) if text_out else reversed(range(len(chains)))):
        fixed = {"bottom": by0 - P(offs[i]), "top": by1 + P(offs[i]), "left": bx0 - P(offs[i]), "right": bx1 + P(offs[i])}[side]
        j = i + 1 if text_out else i - 1
        nb = list(chains[j]) if 0 <= j < len(chains) else (list(outer_row1_forbid) if text_out else [])
        k = i - 1 if text_out else i + 1   # 另一侧那排朝着的一道
        face = ((plans[k] or {}).get("spans", []) if 0 <= k < len(chains) else ([] if text_out else list(outer_row1_forbid)))
        fext = (min(chains[k]), max(chains[k])) if near_face and 0 <= k < len(chains) and len(chains[k]) >= 2 else None
        blk = _blocked_rows(drawing, side, fixed, den)
        plans[i] = _dim_plan(drawing, chains[i], fixed, horizontal, den, leaders=leaders, neighbor=nb, limits=limits, blocked=blk, facing=face,
                             facing_ext=fext)
    for p in plans:
        if p:
            _dim_draw(drawing, p, z)
    return offs[-1] if offs else 0.0


def _dedupe(chains):
    """与外一道完全相同的省掉（保留外面那道）。"""
    kept = []
    for i, c in enumerate(chains):
        if any(len(c) == len(o) and all(abs(a - b) < 1e-3 for a, b in zip(c, o)) for o in chains[i + 1:]):
            continue
        kept.append(c)
    return kept


# ---------------------------------------------------------------- 三道尺寸
def _side_specs(X0, Y0, X1, Y1):
    # (名字, 沿边的轴, 边线位置, 向外的符号)
    return [("S", "x", Y0, -1), ("N", "x", Y1, +1), ("W", "y", X0, -1), ("E", "y", X1, +1)]


def _ext_lines(drawing, side, pts, edge, den, z=0.0):
    """尺寸界线：没有轴号引线可依的尺寸端点（外墙面、切口轴），从离开图样轮廓 EXT_GAP_MM 处画到超出
    用到它的最外一道尺寸线 EXT_GAP_MM。pts = {沿边坐标: 用到它的最外一道的序号}。界线遇到已写的尺寸数字等文字时断开
    （GB/T 50001：尺寸数字不得被图线穿过），所以要在各道尺寸画完之后画。"""
    P = lambda mm: mm * den / 1000.0
    bx0, by0, bx1, by1 = edge
    occ = _OCC.get(drawing.id(), [])
    for v, i in pts.items():
        a, b = P(EXT_GAP_MM), P(CHAIN_MM[0] + i * CHAIN_MM[1] + EXT_GAP_MM)
        seg = {"bottom": [(v, by0 - a), (v, by0 - b)], "top": [(v, by1 + a), (v, by1 + b)],
               "left": [(bx0 - a, v), (bx0 - b, v)], "right": [(bx1 + a, v), (bx1 + b, v)]}[side]
        p0, p1 = np.array(seg[0]), np.array(seg[1])
        L = float(np.linalg.norm(p1 - p0))
        d = (p1 - p0) / L
        line = LineString(seg)
        cuts = []
        for o in occ:
            hit = o.buffer(P(0.4), join_style="mitre").intersection(line)
            if not hit.is_empty:
                ts = [float(np.dot(np.array(c) - p0, d)) for g in getattr(hit, "geoms", [hit]) for c in g.coords]
                cuts.append((min(ts), max(ts)))
        t = 0.0
        for c0, c1 in sorted(cuts) + [(L, L)]:
            if c0 - t > P(0.5):
                polyline(drawing, [tuple(p0 + d * t), tuple(p0 + d * c0)], z)
            t = max(t, c1)


def _end_points(chains, axis_pos=()):
    """各道尺寸的首末点里，不是轴线（没有轴号引线）的点 → {坐标: 用到它的最外一道序号}。"""
    out = {}
    for i, c in enumerate(chains):
        for v in (c[0], c[-1]):
            if not any(abs(v - q) < 0.02 for q in axis_pos):
                k = next((q for q in out if abs(q - v) < 0.02), v)
                out[k] = max(out.get(k, -1), i)
    return out


def three_chains(f, drawing, den, sides=("S", "N", "W", "E")):
    """没有 IfcGrid 的模型（示例户型）：口径和 grid_annotations 相同——第三道总尺寸量外墙外皮外包，第二道轴线
    （与这一侧外墙相接的垂直墙中线），第一道细部两端与总尺寸一致、中间点为轴线和洞口边；首末端点画尺寸界线。"""
    walls = [(w, bbox(w)) for w in f.by_type("IfcWall")]
    X0 = min(b[0] for _, b in walls); Y0 = min(b[1] for _, b in walls)
    X1 = max(b[3] for _, b in walls); Y1 = max(b[4] for _, b in walls)
    holes = [bbox(o) for o in f.by_type("IfcOpeningElement")]
    made = 0
    for name, axis, edge, outward in _side_specs(X0, Y0, X1, Y1):
        if name not in sides:
            continue
        lo, hi = (X0, X1) if axis == "x" else (Y0, Y1)
        # 这一侧的外墙带（贴着边线的墙的厚度）
        band = 0.0
        for _, b in walls:
            across = (b[1], b[4]) if axis == "x" else (b[0], b[3])
            along_len = (b[3] - b[0]) if axis == "x" else (b[4] - b[1])
            if along_len > (b[4] - b[1] if axis == "x" else b[3] - b[0]) and min(abs(across[0] - edge), abs(across[1] - edge)) < EPS:
                band = max(band, across[1] - across[0])
        if band == 0:
            continue
        inner = edge - outward * band  # 外墙内皮
        lo_band, hi_band = sorted((edge, inner))
        # 轴线（开间/进深）：与这一侧外墙相接、且垂直于这一侧的墙（含两端外墙）的中线
        axes = set()
        for _, b in walls:
            across = (b[1], b[4]) if axis == "x" else (b[0], b[3])
            along = (b[0], b[3]) if axis == "x" else (b[1], b[4])
            perpendicular = (along[1] - along[0]) < (across[1] - across[0])
            touches = across[0] - EPS <= hi_band and across[1] + EPS >= lo_band
            if perpendicular and touches:
                axes.add(round((along[0] + along[1]) / 2, 4))
        # 细部：两端（外包）+ 轴线 + 洞口两边
        detail = {lo, hi} | axes
        for b in holes:
            across = (b[1], b[4]) if axis == "x" else (b[0], b[3])
            along = (b[0], b[3]) if axis == "x" else (b[1], b[4])
            if across[0] < hi_band - EPS and across[1] > lo_band + EPS and (along[1] - along[0]) > (across[1] - across[0]) - EPS:
                detail.update(along)
        chains = [sorted(detail), sorted(axes), [lo, hi]]
        kept = _dedupe([c for c in chains if len(c) >= 2])   # 与外一道相同就不画
        side = {"S": "bottom", "N": "top", "W": "left", "E": "right"}[name]
        _draw_side_chains(drawing, kept, side, (X0, Y0, X1, Y1), den, 0.0, limits=(lo, hi))
        _ext_lines(drawing, side, _end_points(kept), (X0, Y0, X1, Y1), den)
        made += len(kept)
    return made


# ---------------------------------------------------------------- 房间名：放在房间里的空地上
LABEL_GAP_MM = 0.6   # 房间名离墙、门窗和门扇扫过的扇形、家具设备和点位符号的轮廓、别的字至少这么远（图纸毫米）
TIGHT_GAP_MM = 0.4   # 放不下时收紧到这么近（墙线 0.5mm 粗、字形比估的字框略高，再近字就贴线了）
ZERO_GAP_MM = 0.15   # 贴着线放时离别的字（门窗编号等）仍留这么多（图纸毫米）
ZERO_COVER_MM2 = 0.05   # 贴着线放时压家具设备、点位符号的面积不到这么多（图纸 mm²，线宽之内，看不出压线）算不压
WALL_GAP_MM = (0.2, 0.6, 0.0)   # 收紧、贴着线放时离墙仍留这么多：左右、墙在字上面、墙在字下面（墙线 0.5mm 粗，一半压进房间；
                                # Bonsai 居中写的字比估的字框偏上约 0.12 个字高，字顶离上面的墙要多留，字底可以贴着下面的墙）
SAME_LINE_MM = 3.0   # 房间名和左右的别的字（门窗编号等）排在同一行时至少隔这么远，免得连读成一串（图纸毫米）
ROW_MM = 3.5         # 房间名组和上下相邻的别的字（门窗编号等，左右有重叠）至少隔一行，免得读成房名的一行（图纸毫米）
LEVEL_ROW_MM = 4.0   # 房间名和楼面标高上下相邻时至少隔这么远（空一行），免得标高读成房间名的一行（图纸毫米）
LINE_GAP_MM = 1.5    # 名字和面积两行之间
WRAP_PEN_MM = 3.0    # 名字每多折一行的代价：一行的写法只比它远不到 3mm 就不折
ROW_PEN_MM = 4.0     # 名字和面积并排写成一行的代价（窄长的房间，如阳台）
STEP_MM = 0.5        # 找位置的步长
SPILL_MM = 2.5       # 房间实在放不下时，字最多出房间多少（图纸毫米）
LABEL_LEADER_MM = (2.0, 20.0)   # 小房间名从房外引出：字中心离房间里的落点的范围（图纸毫米）
LEADER_WALL_M = 0.35            # 房名的引线最多压这么长的墙（米，一道墙厚：只穿过本房间的墙）
NAMED_PEN_MM = 10.0             # 引出的房名写在别的有名字的房间里（不是走道、未标注的空间）：宁可远这么多（图纸毫米）
SMALL_ROOM_M2 = 5.5   # 只写名字的小房间（卫生间、管井等，space_labels）；门的编号尽量写在门外（走道一侧），房间里留给房名（opening_tags）
# _label 的档位（见 _label）；EARLY 之前的几档放不下时（房间里每种写法都要压线），单行的名字先试从房外引出（_label_leader），
# 再往后退
TIER_CLEAN, TIER_NEAR, TIER_LIGHT, TIER_TIGHT, TIER_ZERO, TIER_COVER, TIER_TOUCH, TIER_SPILL = range(8)
EARLY = TIER_COVER


_BREAK_AFTER, _BREAK_BEFORE = conventions.line_breaks()   # 自然断点（和图例、标题栏折行同一份）


def _wrap_name(name, n, short=False):
    """名字折成 n 行（"上\\n下"），折不了返回 None：断在自然断点（空格、"/"、右括号等后面，左括号前面），取最宽一行
    最窄的断法；折两行又没有这样的断点时，4 个字以上的从中间断（字母数字串不拆）。后面各行至少两个字（不留孤字）；
    第一行也至少两个字，short=True（总图简称，如"居(非)1"）时第一行可以只有一个字。"""
    s = str(name)
    core = lambda t: len(re.sub(r"[\s()（）/、，,]", "", t))
    cuts = sorted({i + 1 for i, c in enumerate(s) if c in _BREAK_AFTER} | {i for i, c in enumerate(s) if c in _BREAK_BEFORE})
    if not cuts and n == 2 and len(s) >= 4:
        cuts = [i for i in range(2, len(s) - 1) if not (s[i - 1].isalnum() and s[i].isalnum() and s[i - 1].isascii() and s[i].isascii())]
    best = None
    for cs in itertools.combinations(cuts, n - 1):
        lines = [s[a:b].strip() for a, b in zip((0,) + cs, cs + (len(s),))]
        if core(lines[0]) >= (1 if short else 2) and all(core(t) >= 2 for t in lines[1:]):
            w = max(text_mm(t, 1.0) for t in lines)
            if best is None or w < best[0]:
                best = (w, "\n".join(lines))
    return best and best[1]


class _Rot(list):
    """转 90° 写的一组字（_stack 的 row="rot"）：放的时候文字转 90°，从下往上读。"""


def _stack(items, den, row=False):
    """几项字 [(文字, 字号档)] 排成一组：居中、自上而下（row=True 时左右排成一行，中线对齐；row="rot" 时只有一项，整行
    转 90° 从下往上读，返回 _Rot）→ [(文字, 字号档, 锚点 x, 锚点 y, 外框 (x0, y0, x1, y1))]，都相对整组中心（米）。
    一项里有 "\\n" 时 Bonsai 写成几行（行距 1em），锚点是第一行的中心。"""
    P = lambda mm: mm * den / 1000.0
    dims = []
    for lit, size in items:
        fs = FONT_MM[size]
        ls = lit.split("\n")
        dims.append((P(max(text_mm(t, fs) for t in ls)), P(0.9 * fs + (len(ls) - 1) * fs), P(fs)))
    gap, out = P(LINE_GAP_MM), []
    if row == "rot":
        (lit, size), (w, h, _) = items[0], dims[0]
        return _Rot([(lit, size, 0.0, 0.0, (-h / 2, -w / 2, h / 2, w / 2))])
    if row:
        left = -(sum(w for w, _, _ in dims) + gap * (len(items) - 1)) / 2
        for (lit, size), (w, h, fs) in zip(items, dims):
            out.append((lit, size, left + w / 2, h / 2 - 0.45 * fs, (left, -h / 2, left + w, h / 2)))
            left += w + gap
        return out
    top = (sum(h for _, h, _ in dims) + gap * (len(items) - 1)) / 2
    for (lit, size), (w, h, fs) in zip(items, dims):
        out.append((lit, size, 0.0, top - 0.45 * fs, (-w / 2, top - h, w / 2, top)))
        top -= h + gap
    return out


def _rects(st):
    """一组字要占的矩形：每项的外框，加上相邻两项之间的空隙（按窄的那项连起来，两项之间不能隔着线）。"""
    rs = [r for *_, r in st]

    def span(lo1, hi1, lo2, hi2):   # 两段的重叠部分；不重叠时取两段之间的空隙
        lo, hi = max(lo1, lo2), min(hi1, hi2)
        return (lo, hi) if lo < hi else (hi, lo)
    gaps = [(span(a[0], a[2], b[0], b[2]), span(a[1], a[3], b[1], b[3])) for a, b in zip(rs, rs[1:])]
    return rs + [(x0, y0, x1, y1) for (x0, x1), (y0, y1) in gaps]


def _free_region(region, obstacles, gap):
    """region 里离边界和 obstacles 都有 gap 的地方。"""
    inner = region.buffer(-gap, join_style="mitre")
    if inner.is_empty or not obstacles:
        return inner
    return inner.difference(shapely.union_all([o.buffer(gap, join_style="mitre") for o in obstacles]))


def _same_line(o, d):
    """已写的横排字（外框是横放的矩形）左右各加宽 d：后写的字和它排在同一行时隔得开，上下照旧。别的（竖排字、引线）原样。"""
    x0, y0, x1, y1 = o.bounds
    w, h = x1 - x0, y1 - y0
    return _box(x0 - d, y0, x1 + d, y1) if w > h and o.area >= 0.9 * w * h else o


def _grow(polys, ax, dn, up):
    """图形左右各外扩 ax、往下外扩 dn、往上外扩 up（米；和矩形的闵可夫斯基和，用平移拼出来）。"""
    if not polys or max(ax, dn, up) <= 0:
        return list(polys)
    return [shapely.union_all([shapely.affinity.translate(p, dx, dy) for dx in (-ax, 0.0, ax) for dy in (-dn, 0.0, up)]) for p in polys]


def _row_box(o, d, r, hmin):
    """_same_line 再加上下：横排字（高不小于 hmin，不是引线）上下各让出 r（左右有重叠时隔一行），左右各让出 d。竖排字（宽不小于
    hmin，如竖写的门编号）左右也让出 d：横写的房名紧贴在它旁边，会和它连读成一串。"""
    x0, y0, x1, y1 = o.bounds
    w, h = x1 - x0, y1 - y0
    if w > h and h >= hmin and o.area >= 0.9 * w * h:
        return _box(x0 - d, y0, x1 + d, y1).union(_box(x0, y0 - r, x1, y1 + r))
    if h > w >= hmin and o.area >= 0.9 * w * h:
        return _box(x0 - d, y0, x1 + d, y1)
    return o


def _anchors(free, rects, cx, cy, step):
    """rects（相对锚点的矩形）整组放得进 free 的锚点，由近到远一批批给出 (x, y, 离 (cx, cy) 的距离) 三个数组。
    锚点取以 (cx, cy) 为原点、step 为间距的网格（shapely 向量化判断包含）。"""
    if free.is_empty:
        return
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    fx0, fy0, fx1, fy1 = free.bounds
    i = np.arange(math.ceil((fx0 - x0 - cx) / step), math.floor((fx1 - x1 - cx) / step) + 1)
    j = np.arange(math.ceil((fy0 - y0 - cy) / step), math.floor((fy1 - y1 - cy) / step) + 1)
    if not len(i) or not len(j):
        return
    I, J = np.meshgrid(i, j)
    ax, ay = cx + I.ravel() * step, cy + J.ravel() * step
    d = np.hypot(ax - cx, ay - cy)
    order = np.argsort(d, kind="stable")
    shapely.prepare(free)
    for k in range(0, len(order), 2048):
        idx = order[k:k + 2048]
        ok = np.ones(len(idx), dtype=bool)
        for r in rects:
            ok &= shapely.contains(free, shapely.box(ax[idx] + r[0], ay[idx] + r[1], ax[idx] + r[2], ay[idx] + r[3]))
        if ok.any():
            yield ax[idx][ok], ay[idx][ok], d[idx][ok]


def _nearest_fit(free, rects, cx, cy, step):
    """整组放得进 free、离 (cx, cy) 最近的锚点 (x, y, 距离)；没有返回 None。"""
    for xs, ys, ds in _anchors(free, rects, cx, cy, step):
        return float(xs[0]), float(ys[0]), float(ds[0])
    return None


def _least_cover(free, rects, cx, cy, step, cover, unit):
    """整组放得进 free 的锚点里压图形最少的：cover = [(图形, 权重)]，按"压的面积 × 权重"之和（以 unit 面积计，取一位小数）
    比，一样多取近的：(x, y, 距离, 压的量)；没有返回 None。"""
    best = None
    for xs, ys, ds in _anchors(free, rects, cx, cy, step):
        boxes = [shapely.box(xs + r[0], ys + r[1], xs + r[2], ys + r[3]) for r in rects]
        hit = np.round(sum(wt * shapely.area(shapely.intersection(b, g)) for g, wt in cover for b in boxes) / unit, 1)
        k = int(np.lexsort((ds, hit))[0])
        cand = (float(hit[k]), float(ds[k]), float(xs[k]), float(ys[k]))
        if best is None or cand < best:
            best = cand
    return best and (best[2], best[3], best[1], best[0])


_NAMED: dict[int, object] = {}


def _public(geo, sp):
    """公共交通空间：JZ_Space.Public，或用途是走道、通道这类交通空间（约定包 openings.circulation_words，openings.CIRCULATION）。"""
    return bool(uel.get_psets(sp).get("JZ_Space", {}).get("Public")) or any(w in geo.usage(sp) for w in openings.CIRCULATION)


def _named(geo, sp):
    """有名字的房间：有用途（不是"未标注"），不是公共交通空间（_public）。"""
    return geo.usage(sp) not in ("", openings.UNNAMED_ROOM) and not _public(geo, sp)


def _area_m2(geo, sp):
    """房间净面积（㎡）：JZ_Space.NetArea_m2，没有就按平面轮廓算。"""
    fp = geo.fp(sp)
    return uel.get_psets(sp).get("JZ_Space", {}).get("NetArea_m2") or (fp.area if fp is not None else 0.0)


def _named_rooms(geo):
    """有名字的房间（_named）的合并轮廓（米），每个 IFC 文件算一次。"""
    k = id(geo)
    if k not in _NAMED:
        ps = [geo.fp(s) for s in geo.spaces if geo.fp(s) is not None and _named(geo, s)]
        _NAMED[k] = shapely.union_all(ps) if ps else shapely.Polygon()
        shapely.prepare(_NAMED[k])
    return _NAMED[k]


_ROOMS: dict[int, tuple] = {}


def _rooms(geo):
    """各房间的轮廓（米）和 STRtree，每个 IFC 文件算一次。"""
    k = id(geo)
    if k not in _ROOMS:
        rs = [geo.fp(s) for s in geo.spaces if geo.fp(s) is not None]
        _ROOMS[k] = (rs, shapely.STRtree(rs))
    return _ROOMS[k]


def _label_obstacles(f, drawing):
    """房间名要避开的图形（米）：(墙、门窗洞口和平开门扫过的扇形、按纸面大小画的点位符号（paper_shapes，排在最后）, STRtree,
    本视图画出的家具、洁具、设备和按实际尺寸画的点位的轮廓 [(轮廓, 是否大件)], STRtree, 墙的合并轮廓, 第一项里墙有几个（后面
    的是门窗、开启范围和纸面符号）, 平面几何, 第一项里纸面符号有几个)。"""
    hard, _, _ = _obstacles(f, drawing)
    syms = paper_shapes(f, drawing)
    hard = hard + syms
    soft = soft_shapes(f, drawing, paper=False)
    geo = plan_geometry(f)
    return (hard, shapely.STRtree(hard), soft, (shapely.STRtree([p for p, _ in soft]) if soft else None), geo.wall_union,
            len(geo.wall_polys), geo, len(syms))


def _label(drawing, den, region, center, variants, obs, z=0.0, skip_tiny=False, what="", line_gap=False, start=0, stop=None, clip=None,
           dry=False):
    """在 region（房间轮廓，米）里写一组字：variants = [(字列表 [(文字, 字号档)], 代价 mm, 并排)]（_variants）。
    取"离 center 的距离 + 代价"最小的写法和位置，按档位由严到宽（按纸面大小画的点位符号 paper_shapes 和墙同一级，哪一档都不压）：
      TIER_CLEAN  整组字不压墙、门窗和门扇、家具设备和点位符号、本视图已写的字（都离开 LABEL_GAP_MM；line_gap=True 时
                  和左右同一行的字隔开 SAME_LINE_MM、和上下相邻的横排字（门窗编号等）隔开一行 ROW_MM，见 _row_box；
                  和楼面标高上下隔开 LEVEL_ROW_MM）；
      TIER_NEAR   同上，上下相邻的字只隔开 LABEL_GAP_MM（line_gap=True 时才有这一档）；
      TIER_LIGHT  可以压大件家具的轮廓（床、沙发、楼梯，记日志 room label over furniture）；line_gap=True 时先找和上下相邻
                  的横排字隔一行的位置；
      TIER_TIGHT  什么都不压，空隙收紧到 TIGHT_GAP_MM（离墙仍留 WALL_GAP_MM；记日志 room label tight）；
      TIER_ZERO   什么都不压，贴着门窗、家具设备的轮廓放（空隙 0，压的面积不到 ZERO_COVER_MM2 算不压；离墙仍留 WALL_GAP_MM，
                  离别的字 ZERO_GAP_MM；room label tight）；
      TIER_COVER  只避开墙、门窗、门扇和字，取压家具设备最少的位置（room label over furniture）；
      TIER_TOUCH  同上，贴着墙放（不留空隙，room label touching；水电图不用这一档）；
      TIER_SPILL  房间实在太小：只避开别的字和纸面符号，可以出房间 SPILL_MM（不出 clip：分区、分段图不越过切口），取压墙、门扇
                  和家具最少的位置（水电图上门洞、门扇差不多按压墙算；room label spills）；
    连这也不行才写在 center（room label crowded）。只试 start ≤ 档位 < stop 的几档（stop=None 试到底）；
    这几档都放不下时返回 None（调用方先试别的写法，如从房外引出 _label_leader，再从 stop 接着试）。
    skip_tiny（总图）：房间连一个字都放不下时不写（记日志 room label skipped），返回 False；写上了返回 True。
    dry=True：只找位置、不写字（space_labels reserve），返回整组字的外框（这几档都放不下返回 None）。"""
    P = lambda mm: mm * den / 1000.0
    gap, step = P(LABEL_GAP_MM), P(STEP_MM)
    cx, cy = center
    stacks = [(_stack(items, den, row), P(pen)) for items, pen, row in variants]
    last = TIER_SPILL + 1 if stop is None else stop
    pick = None
    if region is not None and not region.is_empty:
        hard, hard_tree, soft, soft_tree, walls, nwall, _, nsym = obs
        idx = hard_tree.query(region)
        near_h = [hard[i] for i in idx]
        near_w, near_o = [hard[i] for i in idx if i < nwall], [hard[i] for i in idx if i >= nwall]   # 墙；门窗、开启范围和纸面符号
        wx, wa, wb = (P(v) for v in WALL_GAP_MM)   # 墙往下外扩 wa（挡住它下面的字的字顶），往上外扩 wb
        w_tight = _grow(near_w, 0.0, max(0.0, wa - P(TIGHT_GAP_MM)), 0.0)   # 再外扩 TIGHT_GAP_MM 后字顶离上面的墙够 wa
        w_zero = _grow(near_w, wx, wa, wb)
        near_s = [soft[i] for i in soft_tree.query(region)] if soft_tree is not None else []
        occ = [o for o in _OCC.get(drawing.id(), []) if o.intersects(region.buffer(P(max(SAME_LINE_MM, ROW_MM))))]
        occ_x = [_same_line(o, P(SAME_LINE_MM - LABEL_GAP_MM)) for o in occ] if line_gap else occ
        occ_r = [_row_box(o, P(SAME_LINE_MM - LABEL_GAP_MM), P(ROW_MM - LABEL_GAP_MM), P(2.0)) for o in occ] if line_gap else occ
        if line_gap:   # 楼面标高上下各让出一行
            lv = [_box(b[0], b[1] - P(LEVEL_ROW_MM), b[2], b[3] + P(LEVEL_ROW_MM)) for b in (o.bounds for o in _LEVEL.get(drawing.id(), []))
                  if b[0] < region.bounds[2] + P(SAME_LINE_MM) and b[2] > region.bounds[0] - P(SAME_LINE_MM)]
            occ_x, occ_r = occ_x + lv, occ_r + lv
        small_s = [p for p, light in near_s if not light]
        all_s = [p for p, _ in near_s]
        occ_t = [o.buffer(P(ZERO_GAP_MM), join_style="mitre") for o in occ]
        # 档位：(离障碍的空隙, 几组障碍（依次试，压大件家具的那档先找和上下的横排字隔一行的位置）, 家具设备怎么算)：None 当障碍
        # （已在障碍里或不算）；"least" 取压得最少的位置；"zero" 只要压的面积不到 ZERO_COVER_MM2 的位置（线宽之内，看不出压线）
        light = [near_h + small_s + occ_r, near_h + small_s + occ_x] if line_gap else [near_h + small_s + occ_x]
        tiers = [(gap, [near_h + all_s + occ_r], None), (gap, [near_h + all_s + occ_x], None), (gap, light, None),
                 (P(TIGHT_GAP_MM), [w_tight + near_o + all_s + occ], None),
                 (0.0, [w_zero + near_o + occ_t], "zero"),
                 (gap, [near_h + occ], "least"), (0.0, [near_h + occ], "least")]
        for tier in range(start, min(last, len(tiers))):
            g, obs_list, soft_as = tiers[tier]
            if (tier == TIER_NEAR and not line_gap) or (tier == TIER_LIGHT and len(small_s) == len(near_s)) \
                    or (tier == TIER_COVER and not near_s) or (tier == TIER_TOUCH and PAPER_SHIFT):   # 水电图不贴墙写（字墨会压到墙）
                continue   # 和上一档一样
            cover = [(shapely.union_all(all_s), 1.0)] if soft_as and near_s else None
            for ob in obs_list:
                free, best = _free_region(region, ob, g), None
                for st, pen in stacks:
                    rx = _rects(st)
                    if cover is None:
                        if best is not None and best[0][1] <= pen:
                            break
                        hit = _nearest_fit(free, rx, cx, cy, step)
                        key = hit and (0.0, hit[2] + pen)
                    else:   # 压家具设备的面积（图纸 mm²）少的优先，再比距离
                        hit = _least_cover(free, rx, cx, cy, step, cover, P(1.0) ** 2)
                        key = hit and (hit[3], hit[2] + pen)
                    if hit and (best is None or key < best[0]) and not (soft_as == "zero" and key[0] >= ZERO_COVER_MM2):
                        best = (key, hit[0], hit[1], st)
                if best:
                    break
            if best:
                pick = (best[1], best[2], best[3], tier)
                break
        if pick is None and last <= TIER_SPILL:
            return None
        if pick is None and skip_tiny:
            lit, size, *_ = stacks[0][0][0]
            fs = P(FONT_MM[size])
            if not _nearest_fit(region, [(-fs / 2, -0.45 * fs, fs / 2, 0.45 * fs)], cx, cy, step):
                log("room label skipped (room too small)", what, drawing.Name)
                return False
        if pick is None:   # 出房间一点、只避开字和纸面符号，压墙（权重 10）、门扇和家具设备（权重 1）最少
            wide = region.buffer(P(SPILL_MM * (2 if PAPER_SHIFT else 1)), join_style="mitre")   # 水电图房里挤满符号：可以出得远一点
            wide = wide if clip is None else wide.intersection(clip)
            drawn = [hard[i] for i in hard_tree.query(wide)]
            drawn += [soft[i][0] for i in soft_tree.query(wide)] if soft_tree is not None else []
            syms = [hard[i] for i in hard_tree.query(wide) if i >= len(hard) - nsym]
            free = _free_region(wide, [o for o in _OCC.get(drawing.id(), []) if o.intersects(wide)] + syms, P(0.3))
            cover = [(walls.intersection(wide), 10.0), (shapely.union_all(drawn) if drawn else shapely.Polygon(), 1.0)]
            if PAPER_SHIFT:   # 水电图：房里挤满了纸面符号，常常要出房间；门洞和门扇也按压墙算（不然字会写在门上）
                cover.append((_door_zones(tool.Ifc.get()).intersection(wide), 9.0))
            hits = [(h[3], h[2] + pen, h[0], h[1], st) for st, pen in stacks
                    for h in [_least_cover(free, _rects(st), cx, cy, step, cover, P(1.0) ** 2)] if h]
            if hits:
                _, _, hx, hy, hst = min(hits, key=lambda t: t[:2])
                pick = (hx, hy, hst, TIER_SPILL)
    elif stop is not None:
        return None
    if pick is None:
        pick = (cx, cy, stacks[0][0], None)
    x, y, st, _ = pick
    rs = [r for *_, r in st]
    if dry:   # 只找位置（space_labels reserve）：返回整组字的外框
        return _box(x + min(r[0] for r in rs), y + min(r[1] for r in rs), x + max(r[2] for r in rs), y + max(r[3] for r in rs))
    if pick[3] != TIER_CLEAN:
        log("room label " + {TIER_NEAR: "next to text", TIER_LIGHT: "over furniture", TIER_TIGHT: "tight", TIER_ZERO: "tight",
                             TIER_COVER: "over furniture", TIER_TOUCH: "touching", TIER_SPILL: "spills"}.get(pick[3], "crowded"),
            what, drawing.Name)
    for lit, size, dx, dy, _ in st:
        text(drawing, x + dx, y + dy, lit, size=size, z=z, rot=90.0 if isinstance(st, _Rot) else 0.0)
    _GROUPS.setdefault(drawing.id(), []).append(
        (region, _box(x + min(r[0] for r in rs), y + min(r[1] for r in rs), x + max(r[2] for r in rs), y + max(r[3] for r in rs))))
    return True


def _label_leader(drawing, den, region, center, lit, size, obs, allowed, z=0.0, what="", gap=LABEL_GAP_MM, rmax=LABEL_LEADER_MM[1],
                  column=True):
    """小房间（房间里每种写法都要压线）：名字写在房外附近的空地上，用 Bonsai 的 TEXT_LEADER 引一条折线到房间里（_leader_path：
    从字端顺着字的方向引出，再 45°/60°/90° 折向落点；圆点落在房间里不压家具设备和点位的空地上）。字不压墙、门窗和开启范围、
    家具设备和点位符号、已写的字（都离开 gap），在 allowed（楼里、视图范围里）之内，字中心离房间里的目标点 LABEL_LEADER_MM[0]～rmax；
    离别的房名组（_GROUPS，不管写在哪间房里、走道里还是室外，含先引出的房名）至少一行 ROW_MM，上下不到两行时不和它左对齐、
    右对齐或居中对齐（column="center" 时只管中线错开半个字，column=False 时不管对齐），免得读成那组房名的一行；
    引线不穿过自己的字和别的字、家具设备和门窗开启范围，压墙不超过 LEADER_WALL_M（只穿过本房间的一道墙），不穿过第三间房，
    在室外不贴着轴线（2mm 以内）竖着、横着走。
    字优先写在走道、未标注的空间和室外（allowed 含室外时，space_labels 的 _outdoor）：写进别的有名字的房间时距离加
    NAMED_PEN_MM，取"距离 + 代价"最小的。
    放上了返回 True（记日志 room label leader）。"""
    P = lambda mm: mm * den / 1000.0
    if region is None or region.is_empty:
        return False
    hard, hard_tree, soft, soft_tree, _, nwall, geo, nsym = obs
    tw, th = P(text_mm(lit, FONT_MM[size])), P(0.9 * FONT_MM[size])
    near_s = [soft[i][0] for i in soft_tree.query(region)] if soft_tree is not None else []
    near_s += [hard[i] for i in hard_tree.query(region) if i >= len(hard) - nsym]   # 圆点也不落在纸面符号上
    occ = _OCC.setdefault(drawing.id(), [])
    inner = _free_region(region, near_s + [o for o in occ if o.intersects(region)], P(LABEL_GAP_MM))
    if inner.is_empty:
        inner = region
    tgt = shapely.ops.nearest_points(inner, shapely.Point(*center))[0]
    dots = inner.buffer(-P(0.4))   # 圆点（直径 0.8mm）整个落在空地上
    dots = inner if dots.is_empty else dots
    zone = _box(tgt.x - P(rmax), tgt.y - P(rmax), tgt.x + P(rmax), tgt.y + P(rmax))
    if allowed is not None:
        zone = zone.intersection(allowed)
    if zone.is_empty:
        return False
    ob = [hard[i] for i in hard_tree.query(zone)] + [region] + [o for o in occ if o.intersects(zone)] \
        + ([soft[i][0] for i in soft_tree.query(zone)] if soft_tree is not None else [])
    groups = []
    for reg, gb in _GROUPS.get(drawing.id(), []):   # 别的房名组（不管写在哪间房里、走道里还是室外）：隔一行
        if reg is not region and gb.intersects(zone.buffer(P(2 * ROW_MM))):
            ob.append(gb.buffer(P(ROW_MM - gap), join_style="mitre"))
            groups.append(gb.bounds)
    free = _free_region(zone, ob, P(gap))
    named = _named_rooms(geo)
    hw, hh = tw / 2, th / 2
    best = None

    def aligned(x, y):
        """字和别的房名组上下不到两行，又左对齐、右对齐或居中对齐（差不到 1mm）：读成那组的一行。column="center"：只管居中
        （中线左右错开不到半个字，读成那组正下方、正上方的一行）。"""
        for gx0, gy0, gx1, gy1 in groups if column else ():
            if max(gy0 - (y + hh), (y - hh) - gy1) >= P(2 * ROW_MM):
                continue
            if column == "center":
                if abs(x - (gx0 + gx1) / 2) < P(0.5 * FONT_MM[size]):
                    return True
            elif min(abs(x - hw - gx0), abs(x + hw - gx1), abs(x - (gx0 + gx1) / 2)) < P(1.0):
                return True
        return False
    db = dots.bounds
    rooms, rtree = _rooms(geo)
    gx, gy = grid_axes(geo.f)

    def along_axis(path):
        """室外的竖段、横段离轴线不到 2mm（顺着轴号引线、尺寸界线走，读成一根线）。"""
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            seg = LineString([(x0, y0), (x1, y1)])
            if geo.building.contains(seg):
                continue
            if abs(x1 - x0) < 1e-6 and any(abs(x0 - a[1]) < P(2.0) for a in gx):
                return True
            if abs(y1 - y0) < 1e-6 and any(abs(y0 - a[1]) < P(2.0) for a in gy):
                return True
        return False

    def passes(line, a, b):
        """引线穿过第三间房（不是字所在、也不是落点所在的房间）的里面。"""
        for i in rtree.query(line):
            r = rooms[i]
            if not r.contains(shapely.Point(*a)) and not r.contains(shapely.Point(*b)) \
                    and line.intersection(r.buffer(-P(0.3))).length > 0:
                return True
        return False
    for xs, ys, ds in _anchors(free, [(-hw, -hh, hw, hh)], tgt.x, tgt.y, P(STEP_MM)):
        for x, y, d in zip(xs, ys, ds):
            if d > P(rmax) or (best is not None and d >= best[0]):
                break
            if d < P(LABEL_LEADER_MM[0]):
                continue
            cost = d + (P(NAMED_PEN_MM) if named.contains(shapely.Point(float(x), float(y))) else 0.0)
            if (best is not None and cost >= best[0]) or aligned(float(x), float(y)):
                continue
            for first in ((tgt.x < x, tgt.x >= x)):   # 先试朝着房间的那一端
                p0 = np.array([x - hw, y] if first else [x + hw, y], dtype=float)
                out = np.array([-1.0, 0.0] if first else [1.0, 0.0])
                # 落点：房间空地上在字端以外 LEADER_RUN_MM 的部分里，离字端外侧最近的点（引线不回头穿过字）
                e = p0[0] + out[0] * P(LEADER_RUN_MM + 0.05)
                hp = _box(db[0] - 1.0, db[1] - 1.0, e, db[3] + 1.0) if first else _box(e, db[1] - 1.0, db[2] + 1.0, db[3] + 1.0)
                cand = dots.intersection(hp) if hp.bounds[0] < hp.bounds[2] else shapely.Polygon()
                if cand.is_empty:
                    continue
                q = shapely.ops.nearest_points(cand, shapely.Point(*(p0 + out * P(LEADER_RUN_MM + 0.5))))[0]
                path = _leader_path(p0, (q.x, q.y), out, den)
                if path is None:
                    continue
                line = LineString(path)
                band = line.buffer(P(0.3))
                if any(o.intersects(band) for o in occ):
                    continue
                if any(i >= nwall and hard[i].intersects(line) for i in hard_tree.query(line)):
                    continue   # 穿过门窗、门扇
                if soft_tree is not None and any(soft[i][0].intersects(line) for i in soft_tree.query(line)):
                    continue
                if geo.crosses_wall(line, LEADER_WALL_M):
                    continue
                if passes(line, (x, y), path[-1]) or along_axis(path):
                    continue   # 穿过第三间房；在室外顺着轴线走
                best = (cost, float(x), float(y), path, first)
                break
        else:
            continue
        break
    if best is None:
        return False
    _, x, y, path, first = best
    _leader(drawing, path, lit, size, first, z)
    occ.append(_box(x - hw, y - hh, x + hw, y + hh))
    pt = shapely.Point(x, y)   # 登记成字所在那间房（室外为 None）的房名组：后引出的房名也离它一行
    _GROUPS.setdefault(drawing.id(), []).append((next((rooms[i] for i in rtree.query(pt) if rooms[i].contains(pt)), None), occ[-1]))
    if not geo.building.contains(shapely.Point(x, y)):   # 写在室外的字：尺寸数字要避开
        _OUTSIDE.setdefault(drawing.id(), []).append(occ[-1])
    log("room label leader", what, drawing.Name)
    return True


def _variants(items, short=False):
    """一组字的几种写法 [(字列表, 代价, 并排)]，按代价从小到大：上下排；有面积的名字和面积并排一行（代价 ROW_PEN_MM）；
    第一项（名字）折两行、三行（每多一行代价 WRAP_PEN_MM；short 见 _wrap_name）；只有名字、两到四个汉字的，还可以一字一行
    竖着写（窄长的房间、井道；代价比同样行数的折行多一个 WRAP_PEN_MM）。"""
    name, size = items[0]
    out = [(items, 0.0, False)]
    if len(items) > 1:
        out.append((items, ROW_PEN_MM, True))
    for n in (2, 3):
        w = _wrap_name(name, n, short)
        if w:
            out.append(([(w, size)] + items[1:], WRAP_PEN_MM * (n - 1), False))
    if len(items) == 1 and _char_column(name):
        out.append(([("\n".join(name), size)], WRAP_PEN_MM * len(name), False))
    return sorted(out, key=lambda v: v[1])


def _char_column(name):
    """名字能一字一行竖着写：两到四个汉字。"""
    return 2 <= len(name) <= 4 and all(CJK.match(c) for c in name)


def _place_labels(drawing, den, jobs, obs, allowed, z=0.0, line_gap=False, clip=None):
    """一视图的房间名分两遍写：先把房间里有不压线的写法的（_label 的前几档，到 EARLY 为止，含贴着线的写法）都写上；剩下的
    （房间里每种写法都要压线）再一个个来，名字和面积两行的在前（引出的字要避开它们）：只有名字一行的先试从房外引出
    （_label_leader；附近 20mm 内没有空地时，空隙收紧到 0.3mm、放宽到 25mm 再试，再不行放宽到 50mm，先找不在别的房名组正下方、正上方居中的位置，最后不管对齐）；不行再按 _label 的后几档写（只有名字一行、又不能一字一行竖写的，出房间之前先试
    整行转 90° 写）。有简称的（alt，1:150 的小卫生间等）全名写不下时：第一遍在房里写简称（只要和别的字隔一行的位置）；
    第二遍先引出全名，引不出去再在房里写简称（贴着线、压家具也行），最后要出房间时也写简称（记日志 room label abbreviated）。
    jobs = [(区域, 标注点, 写法, 字列表, 用途, skip_tiny, 写上后的回调, 简称 (写法, 字列表, 写上后的回调) 或 None)]；
    clip：字不越过的范围（分区、分段图的裁切框）。返回写了几个。"""
    def abbreviated(alt, what):
        log("room label abbreviated", what, drawing.Name)
        alt[2]()

    n, rest = 0, []
    for job in jobs:
        region, c, var, items, what, tiny, done, alt = job
        r = _label(drawing, den, region, c, var, obs, z=z, skip_tiny=tiny, what=what, line_gap=line_gap, stop=EARLY, clip=clip)
        if r is None and alt is not None and _label(drawing, den, region, c, alt[0], obs, z=z, what=what, line_gap=line_gap,
                                                    stop=TIER_NEAR, clip=clip):
            abbreviated(alt, what)
            n += 1
        elif r is None:
            rest.append(job)
        elif r:
            done()
            n += 1
    for region, c, var, items, what, tiny, done, alt in sorted(rest, key=lambda j: len(j[3]) == 1):
        one = len(items) == 1
        lead = lambda g, r, col: one and _label_leader(drawing, den, region, c, items[0][0], items[0][1], obs, allowed, z=z, what=what,
                                                       gap=g, rmax=r, column=col)
        lab = lambda v, **kw: _label(drawing, den, region, c, v, obs, z=z, what=what, line_gap=line_gap, clip=clip, **kw)
        ok = lead(LABEL_GAP_MM, LABEL_LEADER_MM[1], True) or lead(0.3, 25.0, True) or None
        # 远一点引出，只要不在别的房名组正下方、正上方居中（中线错开半个字以上）；再不行不管对齐
        if ok is None and not lead(0.3, 50.0, "center") and not lead(0.3, 50.0, False):
            if alt is not None and lab(alt[0], start=TIER_NEAR, stop=TIER_SPILL):   # 简称：贴着线、压家具也行
                abbreviated(alt, what)
                n += 1
                continue
            ok = lab(var, skip_tiny=tiny, start=EARLY, stop=TIER_SPILL)   # 压家具、贴着墙写
            if ok is None and one and not _char_column(items[0][0]):
                # 只有名字一行、又不能一字一行竖写的（如总图上的"居(非)1"）：整行转 90° 在房里写（轻压床的外框比出房间压墙、门扇好）
                ok = lab([(items, 0.0, "rot")], start=TIER_TIGHT, stop=TIER_SPILL)
            if ok is None and alt is not None:   # 出房间一点：有简称的写简称（字少，压线少）
                lab(alt[0], start=TIER_SPILL)
                abbreviated(alt, what)
                n += 1
                continue
            if ok is None:   # 出房间一点
                ok = lab(var, skip_tiny=tiny, start=TIER_SPILL)
        else:
            ok = True
        if ok:
            done()
            n += 1
    return n


def room_labels(f, drawing, den, with_area=True, size=3.5):
    """没有 JZ_Space 的模型（示例户型）：每个 IfcSpace 写名字 +（包围框）面积，放法同 space_labels，中心取包围框中心。"""
    geo = plan_geometry(f)
    obs = _label_obstacles(f, drawing)
    jobs = []
    for sp in f.by_type("IfcSpace"):
        b = bbox(sp)
        name = sp.LongName or sp.Name
        items = [(name, size)] + ([(f"{(b[3] - b[0]) * (b[4] - b[1]):.2f}㎡", 2.5)] if with_area else [])
        jobs.append((geo.fp(sp), ((b[0] + b[3]) / 2, (b[1] + b[4]) / 2), _variants(items), items, name, False, lambda: None, None))
    return _place_labels(drawing, den, jobs, obs, geo.building.buffer(-_paper(0.4, den)), line_gap=True)


# ---------------------------------------------------------------- 点位高度、吊顶


H_GAPS_MM = (1.0, 2.0, 3.0, 4.0)   # H 值的字框离自己的符号（图纸毫米），由近到远
H_LEADER_MM = (4.0, 16.0)          # 紧贴不下时，引出的 H 值离符号中心的范围（图纸毫米）
MERGE_MM = 12.0                    # point_heights(merge=True)：同一面墙上同类同高的点位沿墙相距不到这么远（图纸毫米）只标一个
H_LEADER_FAR_MM = 30.0             # point_heights(merge=True)：引出也放不下时，再放宽到这么远（引线连着，不再要求离自己的符号比离别的近）


def point_heights(f, drawing, den, defaults=None, bbox_m=None, z=0.0, classes=symbols.HEIGHT_CLASSES, merge=False):
    """点位的安装高度（底边距本层楼面，毫米）写成 H300。defaults（符号键 → 默认高度，symbols.default_heights，和图例读
    同一份）给了时只标和类型默认高度相差 ≥1mm 的点位，默认高度写在图例里。只标本视图画出的点位（bbox_m：米，
    分区图的裁切框）。位置按图纸毫米找（_height_spots）：紧贴自己的符号，字框离符号 1～4mm，沿墙方向（符号两侧）优先，
    其次室内一侧、斜角；不压墙、门窗和开启范围、家具洁具和点位符号、已写的字，离自己的符号比离别的点位符号近 1mm 以上。
    都放不下时字写在附近同一房间的空地上，用一条短引出线（Bonsai 的 TEXT_LEADER）指向符号（记日志 height leader）。
    merge：同一面墙上挨着的同类点位（同一 IFC 类、高度相同、沿墙相距不到 MERGE_MM 图纸毫米，一个挨一个连成串）只标第一个；
    引出也放不下时，再放宽到 H_LEADER_FAR_MM，最后一档字可以压淡显的家具、洁具（仍不压墙、门窗、点位符号和字）
    （水电图用；默认 False，现有各图不变）。返回标了几个。"""
    import ifcopenshell.util.placement as up
    P = lambda mm: mm * den / 1000.0
    s = ifcopenshell.util.unit.calculate_unit_scale(f)   # 项目单位 → 米
    geo = plan_geometry(f)
    ids = view_element_ids(drawing)
    big = paper_symbols(drawing)
    tree = _drawn_shapes(f, drawing)
    if merge:   # 水电图：给水点、强弱电箱、端子箱不在 PlanGeometry.SOFT 里，按纸面大小画的符号都要避开
        tree = shapely.STRtree(list(tree.geometries) + paper_shapes(f, drawing))
    _hard = []

    def hard_tree():   # 只有墙、门窗和开启范围、接线、点位符号（不含家具洁具）：水电图上 H 值最后一档用
        if not _hard:
            _hard.append(shapely.STRtree(_obstacles(f, drawing)[0] + paper_shapes(f, drawing)))
        return _hard[0]
    occ = _OCC.setdefault(drawing.id(), [])
    allowed = _box(*bbox_m).buffer(-P(CUT_GAP_MM), join_style="mitre") if bbox_m else None   # 不贴着切口折断线
    def outline(e):   # 符号外框；没有几何、也没有二维符号的点位按放置点算 1mm 见方
        o = _soft_outline(geo, e, big.get(e.id()))
        if o is None:
            m = up.get_local_placement(e.ObjectPlacement)
            o = shapely.Point(m[0][3] * s, m[1][3] * s).buffer(P(0.5), cap_style="square")
        return o
    pts = {e.id(): outline(e) for cls in classes for e in f.by_type(cls) if e.id() in ids}
    fs = FONT_MM[NUM]
    n, skip = 0, (_merged_heights(f, pts, classes, defaults, s, P) if merge else set())
    for cls in classes:
        for e in f.by_type(cls):
            if e.id() not in pts:
                continue
            m = up.get_local_placement(e.ObjectPlacement)
            x, y = m[0][3] * s, m[1][3] * s
            st = uel.get_container(e)
            h = (m[2][3] - (up.get_local_placement(st.ObjectPlacement)[2][3] if st and st.ObjectPlacement else 0.0)) * s * 1000
            t = uel.get_type(e)
            d = (defaults or {}).get(t.Tag if t else None)
            if d is not None and abs(h - d) < 1.0:
                continue
            if bbox_m and not (bbox_m[0] <= x <= bbox_m[2] and bbox_m[1] <= y <= bbox_m[3]):
                continue
            if e.id() in skip:
                continue
            lit = f"H{int(round(h))}"
            sym = pts[e.id()]
            others = [o for k, o in pts.items() if k != e.id()]
            cell = _cell_of(f, sym)
            tw, th = P(text_mm(lit, fs)), P(fs * 0.9)

            def ok(p, sym=sym, others=others, cell=cell, tw=tw, th=th, tr=tree):
                """字框不出视图、和符号在同一块不隔墙的地方，不压图形（tr）和字，离自己的符号比离别的点位近 1mm 以上。"""
                b = _box(p[0] - tw / 2 - P(0.3), p[1] - th / 2 - P(0.3), p[0] + tw / 2 + P(0.3), p[1] + th / 2 + P(0.3))
                if (allowed is not None and not allowed.contains(b)) or (cell is not None and not cell.contains(b)) \
                        or len(tr.query(b, predicate="intersects")) or any(o.intersects(b) for o in occ):
                    return False
                d_own = sym.distance(b)
                return all(o.distance(b) > d_own + P(1.0) for o in others)
            u, nv = np.array([m[0][0], m[1][0]]), np.array([m[0][1], m[1][1]])   # 局部 x 沿墙，+y 指向室内
            sym_other = lambda sym=sym: [g for g in paper_shapes(f, drawing) if g is not sym]   # 水电图：引线不穿过别的点位符号（含地漏、排水点）
            spot = next((p for _, p in _height_spots(sym, u, nv, tw, th, den) if ok(p)), None)
            if spot is not None:
                text(drawing, spot[0], spot[1], lit, size=NUM, z=z)
            elif cell is None or not (_height_leader(f, drawing, den, sym, lit, tw, z, ok, avoid=sym_other() if merge else ()) or merge and (
                    _height_leader(f, drawing, den, sym, lit, tw, z, lambda p: ok(p, others=()), rmax=H_LEADER_FAR_MM, avoid=sym_other())
                    # 水电图：还放不下时字可以压淡显的家具、洁具（单独计数），仍不压墙、门窗、点位符号和字，引线连着
                    or _height_leader(f, drawing, den, sym, lit, tw, z, lambda p: ok(p, others=(), tr=hard_tree()), rmax=H_LEADER_FAR_MM,
                                      avoid=sym_other()))):
                log("height crowded", lit, e.Name)
                c = np.array(sym.centroid.coords[0]) + nv * P(4.0)
                ring = [tuple(c + P(r) * np.array([math.cos(a), math.sin(a)])) for r in range(2, 21 if merge else 13)
                        for a in np.arange(0, 2 * math.pi, math.pi / 8)] if merge else []   # 水电图：至少不压别的字，不出这个点位所在的房间
                ring_all = [p for p in ring if cell is None or cell.contains(shapely.Point(p))]
                ring = [p for p in ring if (cell is None or cell.contains(shapely.Point(p)))
                        and (allowed is None or not merge or allowed.contains(_text_box(lit, NUM, den, *p)))]   # 不贴着切口
                if merge:   # 先找不压墙、门窗、符号和字的（可以压家具），再找不压墙、门窗和字的；离符号远的加一条引线（不穿墙、别的符号）
                    def leader_of(p):
                        q = shapely.ops.nearest_points(sym, shapely.Point(*p))[0]
                        if q.distance(shapely.Point(*p)) <= tw / 2 + P(1.5):
                            return []
                        left = q.x < p[0]
                        return [(float(p[0] - tw / 2), float(p[1])) if left else (float(p[0] + tw / 2), float(p[1])), (q.x, q.y)]

                    def clear(p, tr):
                        if _free_spot(drawing, den, lit, NUM, [p], tr, strict=True) is None:
                            return False
                        ln = leader_of(p)
                        return not ln or not (geo.crosses_wall(LineString(ln), WALL_TOUCH_M)
                                              or any(o is not sym and o.intersects(LineString(ln)) for o in paper_shapes(f, drawing)))
                    p = next((p for tr in (hard_tree(), _obstacles(f, drawing)[1]) for p in ring if clear(p, tr)), None) \
                        or _free_spot(drawing, den, lit, NUM, ring, hard_tree(), strict=True) \
                        or _free_spot(drawing, den, lit, NUM, ring, _obstacles(f, drawing)[1], strict=True) \
                        or min(ring or ring_all or [tuple(c)], key=lambda q: (len(hard_tree().query(_text_box(lit, NUM, den, *q), predicate="intersects")),
                                                                  math.dist(q, tuple(c))))   # 都不行：压的东西最少的
                    ln = leader_of(p)
                    if ln:
                        _leader(drawing, ln, lit, NUM, ln[0][0] < p[0], z)
                        occ.append(_text_box(lit, NUM, den, *p))
                    else:
                        text(drawing, *p, lit, size=NUM, z=z)
                else:
                    text(drawing, *_free_spot(drawing, den, lit, NUM, [tuple(c)] + ring, tree), lit, size=NUM, z=z)
            n += 1
    return n


def _merged_heights(f, pts, classes, defaults, s, P) -> set:
    """point_heights(merge=True)：同一面墙上挨着的同类点位（同一 IFC 类、高度相同、沿墙相距不到 MERGE_MM，一个挨一个连成串）
    算一组，组里要标高度的只标第一个（按构件 id）。返回不用标的点位 id。"""
    import ifcopenshell.util.placement as up
    info = []
    for cls in classes:
        for e in f.by_type(cls):
            if e.id() not in pts:
                continue
            m = up.get_local_placement(e.ObjectPlacement)
            st = uel.get_container(e)
            h = (m[2][3] - (up.get_local_placement(st.ObjectPlacement)[2][3] if st and st.ObjectPlacement else 0.0)) * s * 1000
            t = uel.get_type(e)
            d = (defaults or {}).get(t.Tag if t else None)
            info.append((e.id(), cls, h, np.array([m[0][3] * s, m[1][3] * s]), np.array([m[0][0], m[1][0]]), d is None or abs(h - d) >= 1.0))
    parent = {i[0]: i[0] for i in info}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for a, b in itertools.combinations(info, 2):
        d = b[3] - a[3]
        if a[1] == b[1] and abs(a[2] - b[2]) < 1.0 and abs(float(a[4] @ b[4])) > 0.99 \
                and abs(float(d @ np.array([-a[4][1], a[4][0]]))) < P(0.5) and abs(float(d @ a[4])) < P(MERGE_MM):
            parent[root(a[0])] = root(b[0])
    first = {}
    for k, *_, need in sorted(info, key=lambda i: i[0]):
        if need:
            first.setdefault(root(k), k)
    return {k for k, *_, need in info if need and first[root(k)] != k}


def _soft_outline(geo, e, paper=None):
    """家具、洁具、点位在平面图上实际占的地方（米）：三维 Body 的投影并上类型的二维符号（_symbol_outline）；按纸面大小用
    SYMBOL 画的点位（paper = (放大倍数, 比例分母, 视图 id)，见 paper_symbols）是图上符号的笔画（_paper_outline）。没有返回 None。"""
    if paper:
        return _paper_outline(e, *paper)
    p = geo.fp(e)
    sym = _symbol_outline(e, geo.S)
    if sym is not None:
        p = sym if p is None else p.union(sym)
    return p


def _height_spots(sym, u, n, tw, th, den):
    """H 值的候选字中心（米），按代价从小到大：[(代价, (x, y))]。字横写，字框离符号外框 H_GAPS_MM：沿墙方向（符号的
    两侧）代价最小，字往室内挪到不压墙为止（每 0.5mm 加 0.25）；其次室内一侧（加 1.5），再次四个斜角（加 2.5）。"""
    P = lambda mm: mm * den / 1000.0
    x0, y0, x1, y1 = sym.bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    wall_x = abs(u[0]) >= abs(u[1])                     # 墙沿 x 方向
    into = (0.0, 1.0 if n[1] > 0 else -1.0) if wall_x else (1.0 if n[0] > 0 else -1.0, 0.0)
    out = []
    for g in H_GAPS_MM:
        dx, dy = (x1 - x0) / 2 + P(g) + tw / 2, (y1 - y0) / 2 + P(g) + th / 2
        along = [(cx + dx, cy), (cx - dx, cy)] if wall_x else [(cx, cy + dy), (cx, cy - dy)]
        for k in range(9):
            out += [(g + 0.25 * k, (px + into[0] * P(0.5 * k), py + into[1] * P(0.5 * k))) for px, py in along]
        out.append((g + 1.5, (cx + into[0] * dx, cy + into[1] * dy)))
        out += [(g + 2.5, (cx + sx * dx, cy + sy * dy)) for sx in (1, -1) for sy in (1, -1)]
    return sorted(out, key=lambda t: t[0])


def _height_leader(f, drawing, den, sym, lit, tw, z, ok, rmax=H_LEADER_MM[1], avoid=()):
    """H 值紧贴不下：字放在符号周围 H_LEADER_MM 内的空地上（ok 同 point_heights：和符号在同一块不隔墙的地方），
    引线不穿墙、不穿过已写的字，由近到远取第一个；用 TEXT_LEADER 从字的一端引到符号外框。返回是否放上了。"""
    P = lambda mm: mm * den / 1000.0
    geo = plan_geometry(f)
    c = np.array(sym.centroid.coords[0])
    occ = _OCC.setdefault(drawing.id(), [])
    for r in np.arange(H_LEADER_MM[0], rmax, 0.5):
        for a in np.arange(0.0, 2 * math.pi, math.pi / 12):
            p = c + np.array([math.cos(a), math.sin(a)]) * P(r)
            if not ok(p):
                continue
            left = c[0] < p[0]
            p0 = (float(p[0] - tw / 2), float(p[1])) if left else (float(p[0] + tw / 2), float(p[1]))
            out = (-1.0, 0.0) if left else (1.0, 0.0)
            q = shapely.ops.nearest_points(sym, shapely.Point(p0[0] + out[0] * P(LEADER_RUN_MM + 0.5), p0[1]))[0]
            path = _leader_path(p0, (q.x, q.y), out, den)   # 引线从字端顺着字引出再折过去，不回头穿过字
            if path is None:
                continue
            line = LineString(path)
            if geo.crosses_wall(line, WALL_TOUCH_M):
                continue
            if any(o.intersects(line.buffer(P(0.3))) for o in occ) or any(o.intersects(line) for o in avoid):   # avoid：别的点位符号
                continue
            _leader(drawing, path, lit, NUM, left, z)
            occ.append(_text_box(lit, NUM, den, *p))
            log("height leader", lit)
            return True
    return False


def _drawn_shapes(f, drawing):
    """本视图里字不该压的图形（米）：墙、门窗和开启范围，家具、洁具、灯具、点位符号。"""
    hard, _, _ = _obstacles(f, drawing)
    return shapely.STRtree(hard + [p for p, _ in soft_shapes(f, drawing)])


def _free_spot(drawing, den, lit, size, spots, tree, strict=False):
    """spots（米）里第一个放得下 lit 的位置：不压 tree 里的图形、不压本视图已写的字；都不行取第一个（strict 时返回 None）。"""
    occ = _OCC.get(drawing.id(), [])
    for p in spots:
        b = _text_box(lit, size, den, *p)
        if not len(tree.query(b, predicate="intersects")) and not any(o.intersects(b) for o in occ):
            return p
    return None if strict or not spots else spots[0]


WIRE_CLASS = "jz-wire"   # 开关连线的样式类（约定包 cn-mep.css）
WIRE_BULGE = (0.18, -0.18, 0.3, -0.3, 0.45, -0.45, 0.6, -0.6)   # 弧线拱高 / 弦长，由先到后试
WIRE_CLEAR_MM = 0.4      # 弧线离字、离别的符号至少这么远；后放的字也离弧线这么远（图纸毫米）


def _arc(a, b, bulge, n=32):
    """a 到 b 的圆弧，拱高 = bulge × 弦长（正负是拱向弦的左、右侧），n 段折线（米）。"""
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    c = float(np.linalg.norm(b - a))
    if c < 1e-9 or not bulge:
        return [tuple(a), tuple(b)]
    s = bulge * c
    r = (c * c / 4 + s * s) / (2 * abs(s))
    mid, nrm = (a + b) / 2, np.array([-(b - a)[1], (b - a)[0]]) / c
    o, top = mid + nrm * (s - math.copysign(r, s)), mid + nrm * s
    ang = lambda p: math.atan2(p[1] - o[1], p[0] - o[0])
    a0, sweep = ang(a), (ang(b) - ang(a)) % (2 * math.pi)
    if (ang(top) - a0) % (2 * math.pi) > sweep:   # 拱顶不在逆时针那一段上：顺时针走
        sweep -= 2 * math.pi
    return [tuple(o + r * np.array([math.cos(a0 + sweep * i / n), math.sin(a0 + sweep * i / n)])) for i in range(n + 1)]


def wires(f, drawing, den, z=0.0):
    """开关连线：每个开关到它控制的每盏灯（IFC 端口连接，ifcopenshell.util.system.get_connected_to）画一条圆弧（Bonsai 的
    LINEWORK，样式类 WIRE_CLASS，关联到开关，Name 写"开关 Tag→灯 Tag"）。两头都是本视图按纸面大小画的符号才画；弧线从符号
    笔画外截断（端点停在符号外框上，不穿过两头的符号）。拱高按 WIRE_BULGE 依次试，取压字、压别的符号、和已画的弧线相交最少的
    （一样少取先试的）；画好的弧线登记进 _WIRES，后放的字（房间名、楼面标高）不压它。返回 [(开关 Tag, 灯 Tag, 压字, 压符号)]。"""
    import ifcopenshell.util.system as usys
    import ifcopenshell.api.drawing  # noqa: F401（assign_product）
    P = lambda mm: mm * den / 1000.0
    big = paper_symbols(drawing)
    outl = {i: o for i, v in big.items() for o in [_paper_outline(f.by_id(i), *v)] if o is not None}
    # 弧线从符号笔画外框的形心连（不用插入点：画到插入点一侧的变体、沿墙挪开画的符号，插入点不在笔画里），再截在外框上
    xy = lambda e: (lambda c: (c.x, c.y))(outl[e.id()].centroid)
    occ = _OCC.setdefault(drawing.id(), [])
    drawn = _WIRES.setdefault(drawing.id(), [])
    lines, out = [], []
    for sw in sorted((e for e in f.by_type("IfcSwitchingDevice") if e.id() in outl), key=lambda e: (e.Tag or "", e.id())):
        for lt in sorted((e for e in usys.get_connected_to(sw) if e.id() in outl), key=lambda e: (e.Tag or "", e.id())):
            ends = outl[sw.id()].union(outl[lt.id()]).buffer(P(0.1))
            others = [o for i, o in outl.items() if i not in (sw.id(), lt.id())]
            best = None
            for i, b in enumerate(WIRE_BULGE):
                seg = LineString(_arc(xy(sw), xy(lt), b)).difference(ends)
                parts = [g for g in getattr(seg, "geoms", [seg]) if g.length > 0]
                if not parts:
                    continue
                o1, o2 = outl[sw.id()].buffer(P(0.2)), outl[lt.id()].buffer(P(0.2))
                g = max(parts, key=lambda g: (g.intersects(o1) and g.intersects(o2), g.length))   # 两头分别搭在两个符号上的那一段
                band = g.buffer(P(WIRE_CLEAR_MM))
                sc = (not (g.intersects(o1) and g.intersects(o2)), sum(o.intersects(band) for o in occ), sum(o.intersects(band) for o in others),
                      sum(ln.crosses(g) for ln in lines), i)   # 先要两头都搭在符号上
                if best is None or sc < best[0]:
                    best = (sc, g)
            if best is None:
                log("wire skipped", sw.Tag, lt.Tag)
                continue
            (_, n_txt, n_sym, _, _), g = best
            obj = polyline(drawing, list(g.coords), z, classes=WIRE_CLASS)
            a = tool.Ifc.get_entity(obj)
            a.Name = f"{sw.Tag}→{lt.Tag}"
            api.drawing.assign_product(f, relating_product=sw, related_object=a)
            lines.append(g)
            drawn.append(g.buffer(P(WIRE_CLEAR_MM)))
            out.append((sw.Tag, lt.Tag, n_txt, n_sym))
            if n_txt or n_sym:
                log("wire crowded", sw.Tag, lt.Tag, "text", n_txt, "symbols", n_sym)
    return out


def wall_materials(f, drawing, bbox_m=None) -> set[str]:
    """本视图画出的墙用到的材质名（图例的墙体行用）：按视图的 Include/Exclude 筛（总图排除了拆除的墙），
    bbox_m（米，分区图的裁切框）给了时只算和它有重叠的墙。"""
    geo = plan_geometry(f)
    ids = view_element_ids(drawing)
    clip = _box(*bbox_m) if bbox_m else None
    out = set()
    for w in f.by_type("IfcWall"):
        if w.id() not in ids:
            continue
        if clip is not None:
            p = geo.fp(w)
            if p is None or p.intersection(clip).area < 1e-4:
                continue
        mat = uel.get_material(w, should_skip_usage=True)
        if mat is not None and getattr(mat, "Name", None):
            out.add(mat.Name)
    return out


def ceiling_notes(f, drawing, den, storey_height_m=2.8):
    """顶面图：每个房间写吊顶名称（没吊顶的写"原顶刷白"）和吊顶标高（和楼面标高同一个 level_mark：Bonsai 的 PLAN_LEVEL，
    米、三位小数，按 GB/T 50001 正数不写"+"）。两者作为一组（标高在上、名称在下，隔 LINE_GAP_MM），和房间名同一套避让：
    不压墙、门窗、灯具和别的字，离开 LABEL_GAP_MM（放不下收紧到 TIGHT_GAP_MM），从房间中心起找最近的空地，灯具不会夹在
    名称和标高中间。整组放不下时才分开写（名称 _label，从房间中心下方 6mm 起找；标高 level_mark 在房间里另找空地）。"""
    P = lambda mm: mm * den / 1000.0
    coverings = [(c, bbox(c)) for c in f.by_type("IfcCovering") if c.PredefinedType == "CEILING"]
    geo = plan_geometry(f)
    obs = _label_obstacles(f, drawing)
    hard, hard_tree, soft, soft_tree, *_ = obs
    n = 0
    for sp in f.by_type("IfcSpace"):
        b = bbox(sp)
        cx, cy = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
        hit = None
        for c, cb in coverings:
            if cb[0] - EPS <= cx <= cb[3] + EPS and cb[1] - EPS <= cy <= cb[4] + EPS:
                hit = (c, cb)
        if hit:
            label, z = hit[0].Name, hit[1][2]
        else:
            label, z = "原顶刷白", storey_height_m
        region = geo.fp(sp)
        lay = _level_layout([z], "stack", 1, den)
        W, H, lrects = lay["W"], lay["H"], lay["rects"]
        fs = FONT_MM[2.5]
        nw, nh = P(text_mm(label, fs)), P(0.9 * fs)
        tot = H + P(LINE_GAP_MM) + nh
        ly, ny = tot / 2 - H, -tot / 2 + nh / 2   # 标高字组左下角、名称中心（相对整组中心）
        rects = [(r[0] - W / 2, r[1] + ly, r[2] - W / 2, r[3] + ly) for r in lrects] + [(-nw / 2, ny - nh / 2, nw / 2, ny + nh / 2)] \
            + [(-min(W, nw) / 2, ny + nh / 2, min(W, nw) / 2, ly)]   # 两者之间的空隙也要空着（不夹着灯）
        spot = None
        if region is not None and not region.is_empty:
            ob = [hard[i] for i in hard_tree.query(region)] + ([soft[i][0] for i in soft_tree.query(region)] if soft_tree is not None else []) \
                + [o for o in _OCC.get(drawing.id(), []) if o.intersects(region)]
            for g in (LABEL_GAP_MM, TIGHT_GAP_MM):
                spot = _nearest_fit(_free_region(region, ob, P(g)), rects, cx, cy, P(STEP_MM))
                if spot:
                    break
        if spot:
            x, y, _ = spot
            text(drawing, x, y + ny, label, size=2.5, z=z)
            level_mark(f, drawing, den, [z], z=z, place=(x - W / 2, y + ly))
        else:
            log("ceiling note split", label, drawing.Name)
            _label(drawing, den, region, (cx, cy - _paper(6, den)), _variants([(label, 2.5)]), obs, z=z, what=label)
            level_mark(f, drawing, den, [z], z=z, bbox_m=(b[0], b[1], b[3], b[4]), avoid_labels=False)
        n += 1
    return n


# ---------------------------------------------------------------- 按 JZ_Space 的标注点写房间名
# 小比例总图用的简称；项目可以传自己的表（abbr=…）覆盖或补充
ABBR = {"主卧": "卧", "次卧": "卧", "卧室": "卧", "书房": "书", "客厅": "客", "餐厅": "餐", "客厅/餐厅": "客餐", "餐厅/客厅": "客餐",
        "起居室": "起", "厨房": "厨", "主卫": "卫", "公卫": "卫", "卫生间": "卫", "阳台": "阳台", "储藏": "储", "衣帽间": "衣",
        "玄关": "玄", "前室": "前室", "通道": "通道", "走道": "走道", "楼梯": "梯", "电梯": "梯", "消防电梯": "消梯", "无障碍电梯": "梯"}
# 中比例图上嫌长的名字换成短一点的
SHORT = {"无障碍电梯": "电梯", "消防电梯": "消防梯"}


_ABBR_USED: dict[int, dict] = {}   # 视图 id → {简称: {"names": [名字…], "n": 次数, "beds": 带床位数}}，总图图例的简称对照用


def _outdoor(f, den, plan):
    """引出的房名可以写的室外（米，plan 是 block_plan 的安排）：画了尺寸或轴号的上、左两侧（尺寸数字朝外写，留边够宽），
    离楼 0.6mm 以外、第一道尺寸线以里 1.5mm；下、右两侧的这一带留给小段尺寸数字，不写。不压室外的轴号引线（轴线两侧 1mm）。
    没有返回 None。"""
    P = lambda mm: mm * den / 1000.0
    geo = plan_geometry(f)
    x0, y0, x1, y1 = plan["env"]
    d = P(CHAIN_MM[0] - 1.5)
    sd = plan["sides"]
    bands = ([_box(x0 - d, y1, x1, y1 + d)] if sd["top"]["chains"] or sd["top"]["sel"] else []) \
        + ([_box(x0 - d, y0, x0, y1 + d)] if sd["left"]["chains"] or sd["left"]["sel"] else [])
    if not bands:
        return None
    out = shapely.union_all(bands).difference(geo.building.buffer(P(0.6), join_style="mitre"))
    gx, gy = grid_axes(f)
    axes = [_box(a[1] - P(1.0), y0 - d, a[1] + P(1.0), y1 + d) for a in gx] + [_box(x0 - d, a[1] - P(1.0), x1 + d, a[1] + P(1.0)) for a in gy]
    if axes:
        out = out.difference(shapely.union_all(axes))
    return None if out.is_empty else out


def space_labels(f, drawing, den, mode="full", bbox_m=None, z=0.0, abbr=None, short=None, zone=None, strict=False, plan=None,
                 reserve=False):
    """mode="full"：用途 +（床位）+ 面积（1:100～1:150）；mode="abbr"：简称 +（床位数）（1:200 以下的总图）。
    reserve=True（在门窗编号之前调用，参数和后面真正写房名时相同）：只给只写名字的小房间、交通空间（走道、通道）在房里找好
    房名的位置（_label 的前几档，写不下全名的试简称），登记进本视图的预留表（_RESERVE），不写字；窗的编号先避开预留的位置
    （opening_tags），房名后写时就能写在房里。返回预留了几个。
    字放在房间里离标注点最近、整组放得下的空地上（_label：不压墙、门窗和门扇、家具设备、点位符号和已写的字；名字放不下
    一行时折两行、竖写）；小房间里实在放不下的从房外引出（_label_leader），见 _place_labels；1:150 上只写名字的小房间写不下
    全名时，有简称的（如卫生间写"卫"）先在房里写简称，和总图一样进图例的简称对照（abbr_legend）。标注点取 JZ_Space.LabelX/Y_mm
    （方案阶段用 polylabel 算好的房间内部点），没有就取包围框中心。管井等小房间（不到 5.5㎡）只写名字；分段、分区图上每个
    有名字的房间都写，总图上管井和不到 1.6㎡ 的不写（分段、分区图上有），房间本身放不下简称的也不写。
    abbr、short：项目自己的简称表，和默认表合并（项目的优先）。zone：只写这个分区（JZ_Zone.Block）的房间；
    strict=True（自动分段）时别的段的房间一律不写（房名只在标注点所在的那一段写一次）。plan（block_plan 的安排）给了时，
    引出的房名也可以写在上、左两侧的室外（_outdoor）。"""
    ABBR_ = dict(ABBR, **(abbr or {}))
    SHORT_ = dict(SHORT, **(short or {}))
    geo = plan_geometry(f)
    obs = _label_obstacles(f, drawing)
    clip = _box(*bbox_m) if bbox_m else None
    clip_in = clip.buffer(-_paper(CUT_GAP_MM, den), join_style="mitre") if clip is not None else None   # 不贴着切口折断线
    allowed = geo.building.buffer(-_paper(0.4, den))
    if clip_in is not None:
        allowed = allowed.intersection(clip_in)
    if mode == "full" and plan is not None:   # 引出的房名也可以写在室外（_outdoor）
        out = _outdoor(f, den, plan)
        if out is not None:
            allowed = allowed.union(out)
    used = _ABBR_USED.setdefault(drawing.id(), {})

    def record(ab, usage, beds):
        """写上简称后登记（图例的简称对照按实际写过的生成）。"""
        def done():
            u = used.setdefault(ab, {"names": [], "n": 0, "beds": False})
            full = SHORT_.get(usage, usage)   # "客厅/餐厅""餐厅/客厅"算同一个名字
            u["names"] += [] if any(set(full.split("/")) == set(nm.split("/")) for nm in u["names"]) else [full]
            u["n"] += 1
            u["beds"] = u["beds"] or bool(beds)
        return done
    jobs, small = [], 0
    for sp in f.by_type("IfcSpace"):
        ps = uel.get_psets(sp).get("JZ_Space", {})
        usage = ps.get("Usage") or sp.LongName or sp.Name
        area = _area_m2(geo, sp)
        beds = ps.get("BedCount") or 0
        if "LabelX_mm" in ps:
            x, y = ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0
        else:
            b = bbox(sp); x, y = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
        if bbox_m and not (bbox_m[0] <= x <= bbox_m[2] and bbox_m[1] <= y <= bbox_m[3]):
            continue
        if zone is not None and uel.get_psets(sp).get("JZ_Zone", {}).get("Block") != zone:
            if strict:
                continue
            # 别的分区的房间：大部分落在本图范围里（墙是本分区画的）才写，免得在空白处写字
            fp = plan_geometry(f).fp(sp)
            if not (bbox_m and fp is not None and fp.area > 0 and fp.intersection(_box(*bbox_m)).area / fp.area >= 0.6):
                continue
        if usage in ("", "未标注"):   # 没有名字的空间不写字
            continue
        fp = geo.fp(sp)
        region = fp.intersection(clip_in) if fp is not None and clip_in is not None else fp
        if mode == "abbr":
            if usage == "管井" or area < 1.6:   # 总图上井道、极小的房间不写（分段、分区图上有）
                small += 1
                continue
            ab = ABBR_.get(usage, usage[:2])
            items = [(ab + (str(beds) if beds else ""), 1.8)]
            jobs.append((region, (x, y), _variants(items, short=True), items, usage, True, record(ab, usage, beds), None))
        else:
            name = SHORT_.get(usage, usage) + (f" {beds}床" if beds else "")
            alt = None
            if area < SMALL_ROOM_M2 or ps.get("Public"):   # 小房间、公共空间：只写名字，用最小的汉字
                items = [(name, 1.8)]
                ab = ABBR_.get(usage)
                if den >= 150 and ab and ab != name and not beds:   # 1:150 上写不下全名时写简称
                    alt = (_variants([(ab, 1.8)], short=True), [(ab, 1.8)], record(ab, usage, beds))
            else:
                items = [(name, 2.5), (f"{area:.2f}㎡", NUM)]
            jobs.append((region, (x, y), _variants(items), items, usage, False, lambda: None, alt))
    if reserve:   # 先给只写名字的小房间、交通空间的房名找好位置（还不写），窗的编号避开（opening_tags）
        res = _RESERVE.setdefault(drawing.id(), [])
        for region, c, var, items, what, tiny, done, alt in jobs:
            if len(items) != 1:
                continue
            b = _label(drawing, den, region, c, var, obs, line_gap=True, stop=EARLY, clip=clip_in, dry=True)
            if b is None and alt is not None:
                b = _label(drawing, den, region, c, alt[0], obs, line_gap=True, stop=TIER_NEAR, clip=clip_in, dry=True)
            if b is not None:
                res.append((b, region))
        return len(res)
    if small:
        log("room labels left to detail plans (shafts, rooms < 1.6㎡)", small, drawing.Name)
    return _place_labels(drawing, den, jobs, obs, allowed, z=z, line_gap=mode == "full", clip=clip_in)


def abbr_legend(drawing) -> str:
    """本视图（总图）实际写过的简称对照，图例用："卧=主卧、次卧 卫=卫生间 …"：用得多的在前，名字取分段、分区图上写的
    （SHORT 换过的）；简称就是名字的不列；写过床位数的末尾加一句。没有简称时返回空串。"""
    used = _ABBR_USED.get(drawing.id(), {})
    parts = [f"{a}={'、'.join(u['names'])}" for a, u in sorted(used.items(), key=lambda kv: -kv[1]["n"]) if u["names"] != [a]]
    if any(u["beds"] for u in used.values()):
        parts.append("简称后的数字为床位数")
    return " ".join(parts)


# ---------------------------------------------------------------- 轴网：轴号圈 + 轴线尺寸 + 总尺寸（读 IfcGrid）
def grid_axes(f):
    """IfcGrid → ([(轴号, x米, 端, (y起, y止))], [(轴号, y米, 端, (x起, x止))])。
    端 = 原图轴号放在哪一侧（JZ_Grid.BubbleEnds，没有就两侧都算）；(起, 止) = 轴线两个端点在另一方向上的范围
    （原图常是局部轴线，某一侧画不画这根轴的轴号要看它伸没伸到这一侧）。"""
    import ifcopenshell.util.placement as up
    import ifcopenshell.util.unit as uu
    gs = f.by_type("IfcGrid")
    if not gs:
        return [], []
    g = gs[0]
    sc = uu.calculate_unit_scale(f)
    m = up.get_local_placement(g.ObjectPlacement)
    ends = json.loads(uel.get_psets(g).get("JZ_Grid", {}).get("BubbleEnds", "{}") or "{}")

    def rng(a, k):
        pts = a.AxisCurve.Points
        vs = [(p.Coordinates[k] + m[k][3]) * sc for p in (pts[0], pts[-1])]
        return (min(vs), max(vs))
    X = [(a.AxisTag, (a.AxisCurve.Points[0].Coordinates[0] + m[0][3]) * sc, ends.get("x", {}).get(a.AxisTag, ["bottom", "top"]), rng(a, 1))
         for a in g.UAxes]
    Y = [(a.AxisTag, (a.AxisCurve.Points[0].Coordinates[1] + m[1][3]) * sc, ends.get("y", {}).get(a.AxisTag, ["left", "right"]), rng(a, 0))
         for a in g.VAxes]
    return sorted(X, key=lambda t: t[1]), sorted(Y, key=lambda t: t[1])


def _spread(vals, gap):
    """一维去重叠：保持顺序，相邻至少 gap；重叠的一串整体以原位置均值为中心排开。"""
    groups = []
    for v in vals:
        groups.append([v])
        while len(groups) > 1:
            a, b = groups[-2], groups[-1]
            ea = sum(a) / len(a) + gap * (len(a) - 1) / 2
            sb = sum(b) / len(b) - gap * (len(b) - 1) / 2
            if sb - ea < gap:
                groups[-2:] = [a + b]
            else:
                break
    out = []
    for g in groups:
        c = sum(g) / len(g)
        out += [c - gap * (len(g) - 1) / 2 + i * gap for i in range(len(g))]
    return out


def bubble(drawing, x, y, label, den, z=0.0, r_mm=4.0):
    r = _paper(r_mm, den)
    polyline(drawing, [(x + r * math.cos(2 * math.pi * i / 32), y + r * math.sin(2 * math.pi * i / 32)) for i in range(33)], z)
    text(drawing, x, y, label, size=2.5, z=z)


def facade_openings(f, edge, side, lo, hi, oblique=None):
    """这一侧外墙上看得见的门窗洞口沿边方向的起止（米）：按位置取（洞口和本图范围 [lo, hi] 有重叠、在本图裁切框里），
    不按分区归属取——跨在切口上的洞口两张图都标；朝向这一侧、往外看不被楼体挡住的；凸窗取外轮廓在这一侧的投影。
    oblique：传一个 set 进来，收集斜墙、弧墙上进不了细部尺寸的外墙门窗（记日志用）。"""
    geo = plan_geometry(f)
    bx0, by0, bx1, by1 = edge
    k = 0 if side in ("bottom", "top") else 1
    out = []
    for el in geo.openings:
        fr = geo.frame(el)
        if not fr or fr.get("out") is None:
            continue
        cx, cy = fr["c"]
        across_ok = (by0 - 1.0 <= cy <= by1 + 1.0) if k == 0 else (bx0 - 1.0 <= cx <= bx1 + 1.0)
        if fr["bay"]:
            b = fr["fp"].bounds
            a0, a1 = (b[0], b[2]) if k == 0 else (b[1], b[3])
        else:
            a0, a1 = sorted((float(fr["span"][0][k]), float(fr["span"][1][k])))
        if not across_ok or a1 < lo + 0.02 or a0 > hi - 0.02:
            continue
        if fr.get("side") is None:
            if oblique is not None:
                oblique.add(el.Name or str(el.id()))
            continue
        if fr.get("side") != side or not fr.get("visible"):
            continue
        out.append((a0, a1))
    return out


def side_margin_mm(n_chains, bubbles):
    """一侧要给尺寸和轴号留的宽度（图纸毫米）：n_chains 道尺寸 + 轴号圈（有的话）。"""
    off = CHAIN_MM[0] + (n_chains - 1) * CHAIN_MM[1] if n_chains else 0.0
    if bubbles:
        return off + BUBBLE_GAP_MM + 2 * BUBBLE_R_MM + 0.5
    return off + (4.0 if n_chains else 0.0)   # 顶边、左边的数字写在线外侧


def envelope(f, crop=None, classes=("IfcWall", "IfcColumn", "IfcDoor", "IfcWindow")):
    """墙、柱、门窗在平面上的外包（米，_outline_fp）；给 crop 时只算落在裁切框里的部分。"""
    geo = plan_geometry(f)
    clip = _box(*crop) if crop else None
    bs = []
    for cls in classes:
        for el in f.by_type(cls):
            p = _outline_fp(geo, el)
            if p is None:
                continue
            if clip is not None:
                if not p.intersects(clip):
                    continue
                p = p.intersection(clip)
                if p.is_empty or p.area < 1e-6:
                    continue
            bs.append(p.bounds)
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def side_kind(f, crop, side):
    """裁切框这条边是外墙面（"ext"）还是内部切口（"cut"）：边线外侧紧挨着（50mm）还有楼（墙、房间、门窗）就是切口。
    变形缝两侧的楼块是分开的，缝边上算外墙面。"""
    geo = plan_geometry(f)
    x0, y0, x1, y1 = crop
    d = 0.05
    line = {"left": LineString([(x0 - d, y0), (x0 - d, y1)]), "right": LineString([(x1 + d, y0), (x1 + d, y1)]),
            "bottom": LineString([(x0, y0 - d), (x1, y0 - d)]), "top": LineString([(x0, y1 + d), (x1, y1 + d)])}[side]
    return "cut" if line.intersection(geo.raw).length > 0.3 else "ext"


def block_plan(f, crop, kinds=None, cut_pos=None):
    """分区图/分段图四边的尺寸和轴号，出图前先算好（据此定留边和比例），画的时候照着画（grid_annotations）。

    crop：本图裁切框（米）；kinds：{边: "ext"|"cut"}（不给就按 side_kind 判断）；cut_pos：{边: 切口轴位置}
    （自动分段时给，尺寸量到切口轴；不给就量到裁切框边）。每一侧：
      - 轴号：只画伸到这一侧的轴（轴线端点到这一侧外墙面/切口不超过 REACH，另一方向的范围和本图有交集），
        沿边位置在本图尺寸范围内（不因容差拉进邻段的轴）；这一侧是整栋楼的最外边时还按原图轴号放在哪一侧（JZ_Grid）；
      - 第一道 细部：两端与第三道相同，中间点为本侧画了轴号的轴和看得见的外墙洞口边（按位置取）；只在外墙面一侧画；
      - 第二道 轴线：只用本侧画了轴号的轴（≥2 根）；
      - 第三道 总尺寸：外墙外皮外包（内部切口一侧量到切口轴），x 向画在下边、y 向画在左边（那边是切口而对边是外墙时画在对边）；
        某一侧只有一根轴也照画；与外一道相同的省掉；
      - 首末端点不是轴线的（外墙面、切口轴）画尺寸界线。"""
    geo = plan_geometry(f)
    kinds = dict(kinds or {})
    for s in SIDES:
        kinds.setdefault(s, side_kind(f, crop, s))
    cut_pos = cut_pos or {}
    env = envelope(f, crop) or tuple(crop)
    bx0, by0, bx1, by1 = env
    if kinds["left"] == "cut":
        bx0 = crop[0]
    if kinds["right"] == "cut":
        bx1 = crop[2]
    if kinds["bottom"] == "cut":
        by0 = crop[1]
    if kinds["top"] == "cut":
        by1 = crop[3]
    env = (bx0, by0, bx1, by1)
    face = {"bottom": by0, "top": by1, "left": bx0, "right": bx1}
    end = {s: (cut_pos.get(s, face[s]) if kinds[s] == "cut" else face[s]) for s in SIDES}
    G = envelope(f) or env
    extreme = {"bottom": abs(by0 - G[1]) < 0.5, "top": abs(by1 - G[3]) < 0.5, "left": abs(bx0 - G[0]) < 0.5, "right": abs(bx1 - G[2]) < 0.5}
    total_x = "bottom" if kinds["bottom"] == "ext" or kinds["top"] != "ext" else "top"
    total_y = "left" if kinds["left"] == "ext" or kinds["right"] != "ext" else "right"
    X, Y = grid_axes(f)
    oblique = set()
    sides = {}
    for side in SIDES:
        is_x = side in ("bottom", "top")
        lo, hi = (end["left"], end["right"]) if is_x else (end["bottom"], end["top"])
        olo, ohi = (by0, by1) if is_x else (bx0, bx1)
        fc = face[side]
        sel = []
        for a in (X if is_x else Y):
            if not (lo - 0.02 <= a[1] <= hi + 0.02):
                continue
            s0, s1 = a[3]
            reach = s0 <= fc + REACH if side in ("bottom", "left") else s1 >= fc - REACH
            if not reach or s1 < olo - 0.02 or s0 > ohi + 0.02:
                continue
            if kinds[side] == "ext" and extreme[side] and side not in a[2]:
                continue
            sel.append(a)
        chains = []
        if kinds[side] == "ext":
            ops = facade_openings(f, env, side, lo, hi, oblique)
            if ops:
                pts = {lo, hi} | {a[1] for a in sel}
                for a0, a1 in ops:
                    pts |= {v for v in (a0, a1) if lo + 0.02 < v < hi - 0.02}
                chains.append(sorted(pts))
        if len(sel) >= 2:
            chains.append([a[1] for a in sel])
        if side in (total_x, total_y) and hi - lo > 0.05:
            chains.append([lo, hi])
        chains = _dedupe([c for c in chains if len(c) >= 2])
        sides[side] = {"sel": sel, "chains": chains, "lo": lo, "hi": hi, "ext": _end_points(chains, [a[1] for a in sel]),
                       "margin": side_margin_mm(len(chains), bool(sel))}
    if oblique:
        log("openings on oblique walls, not in detail chains", sorted(oblique))
    # 外开的平开门（入户门常见）门扇扫到外轮廓以外时，那一侧的尺寸、轴号排到门扇以外：外包并上门扇（比例也按它算）；
    # 尺寸量的仍是墙（各道的端点、轴线在上面已经定了）
    sw = _swings_out(f, crop, kinds)
    if sw is not None:
        sx0, sy0, sx1, sy1 = sw.bounds
        env = (min(env[0], sx0), min(env[1], sy0), max(env[2], sx1), max(env[3], sy1))
    return {"crop": tuple(crop), "env": env, "kinds": kinds, "sides": sides}


def _swings_out(f, crop, kinds):
    """平开门扇扫到建筑外轮廓以外的部分（米）；没有返回 None。只算本图的：裁切框外扩到外墙面一侧以外（门扇在框外），切口一侧不外扩。"""
    geo = plan_geometry(f)
    ext = lambda side: 3.0 if kinds[side] == "ext" else 0.0
    clip = _box(crop[0] - ext("left"), crop[1] - ext("bottom"), crop[2] + ext("right"), crop[3] + ext("top"))
    # 外轮廓只按墙和房间算：geo.building 还并了门窗构件，开到 90° 的门扇也在里面
    base = shapely.ops.unary_union(list(geo.wall_polys) + [p for p in (geo.fp(s) for s in geo.spaces) if p is not None])
    base = base.buffer(0.15, join_style="mitre").buffer(-0.15, join_style="mitre")
    parts = []
    for el in geo.openings:
        fr = geo.frame(el) if el.is_a("IfcDoor") else None
        if fr is None or fr["bay"]:
            continue
        for z in _opening_zone(fr)[1:]:
            g = z.difference(base).intersection(clip)
            if g.area > 0.01:
                parts.append(g)
    return shapely.ops.unary_union(parts) if parts else None


def grid_annotations(f, drawing, den, edge, z=0.0, mode="block", spans=None, plan=None):
    """外墙尺寸和轴号。edge = 建筑（或本分区）外包（米，x0 y0 x1 y1）。

    mode="block"（分区图、分段图、户型图）：按 block_plan 算好的安排画（plan 不给就按 edge 现算，四边都按外墙），
      每一侧由里到外 第一道 细部、第二道 轴线、第三道 总尺寸，规则见 block_plan；轴号画在最外，引线连到建筑边；
      首末端点没有轴号的画尺寸界线。门窗按构件位置和宽度沿外墙投影，识别模型的墙在洞口处断开也能算。
    mode="overall"（总图）：spans = [(x0, x1), ...] 各单元范围，下边标各单元首末轴 + 总尺寸，左边标总尺寸；
      只画这些轴的轴号；只有首末两根轴时轴线这一道和总尺寸重复，省掉。"""
    X, Y = grid_axes(f)
    if not X and not Y and mode != "block":
        return 0
    P = lambda mm: _paper(mm, den)
    if mode == "block":
        plan = plan or block_plan(f, edge)
        edge = plan["env"]
    bx0, by0, bx1, by1 = edge
    R = BUBBLE_R_MM
    tol = 0.35
    xs = [a for a in X if bx0 - tol <= a[1] <= bx1 + tol]
    ys = [a for a in Y if by0 - tol <= a[1] <= by1 + tol]
    n = 0

    def bubble_pos(axis_list):
        axis_list = sorted(axis_list, key=lambda a: a[1])
        return axis_list, _spread([a[1] for a in axis_list], P(2 * R + 0.8))

    def put(axis_list, side, bubble_mm):
        """画轴号：挤在一起的轴号沿边错开（保持顺序、整体居中），引线在圈前 1.2mm 处折过去。"""
        nonlocal n
        axis_list, bpos = bubble_pos(axis_list)
        k = P(bubble_mm - R - 1.2)   # 折点
        for (t, v, *_), b in zip(axis_list, bpos):
            if side == "bottom":
                polyline(drawing, [(v, by0 - P(1.5)), (v, by0 - k), (b, by0 - P(bubble_mm - R))], z); bubble(drawing, b, by0 - P(bubble_mm), t, den, z, R)
            elif side == "top":
                polyline(drawing, [(v, by1 + P(1.5)), (v, by1 + k), (b, by1 + P(bubble_mm - R))], z); bubble(drawing, b, by1 + P(bubble_mm), t, den, z, R)
            elif side == "left":
                polyline(drawing, [(bx0 - P(1.5), v), (bx0 - k, v), (bx0 - P(bubble_mm - R), b)], z); bubble(drawing, bx0 - P(bubble_mm), b, t, den, z, R)
            else:
                polyline(drawing, [(bx1 + P(1.5), v), (bx1 + k, v), (bx1 + P(bubble_mm - R), b)], z); bubble(drawing, bx1 + P(bubble_mm), b, t, den, z, R)
            n += 1

    def bubble_forbid(axis_list):
        """最外一道尺寸第二排文字要避开的范围：引线折过去的那一截和轴号圈。"""
        axis_list, bpos = bubble_pos(axis_list)
        return [(min(v, b - P(R)) - P(0.5), max(v, b + P(R)) + P(0.5)) for (_, v, *_), b in zip(axis_list, bpos)]

    if mode == "overall":
        keep = []
        for s0, s1 in spans or [(bx0, bx1)]:
            inside = [a for a in xs if s0 - tol <= a[1] <= s1 + tol]
            if inside:
                keep += [inside[0], inside[-1]]
        keep = sorted({a[1]: a for a in keep}.values(), key=lambda a: a[1])
        if len(keep) >= 2:
            chains = ([[a[1] for a in keep]] if len(keep) > 2 else []) + [[keep[0][1], keep[-1][1]]]
            last = _draw_side_chains(drawing, chains, "bottom", edge, den, z, leaders=[a[1] for a in keep], limits=(bx0, bx1))
            n += len(chains)
            put(keep, "bottom", last + BUBBLE_GAP_MM + R)
        if len(ys) >= 2:
            last = _draw_side_chains(drawing, [[ys[0][1], ys[-1][1]]], "left", edge, den, z, leaders=[ys[0][1], ys[-1][1]],
                                     limits=(by0, by1), outer_row1_forbid=bubble_forbid([ys[0], ys[-1]]))
            n += 1
            put([ys[0], ys[-1]], "left", last + BUBBLE_GAP_MM + R)
        return n

    sides = plan["sides"]
    perp = {"bottom": ("left", "right"), "top": ("left", "right"), "left": ("bottom", "top"), "right": ("bottom", "top")}
    for side in SIDES:
        sp = sides[side]
        sel, chains = sp["sel"], sp["chains"]
        is_x = side in ("bottom", "top")
        e0, e1 = (bx0, bx1) if is_x else (by0, by1)
        # 数字可以伸出建筑边：相邻一侧有尺寸或轴号时最多 CORNER_MM（不压那一侧的数字，YH8），没有时 12mm
        busy = [bool(sides[s]["chains"] or sides[s]["sel"]) for s in perp[side]]
        limits = (e0 - P(CORNER_MM if busy[0] else 12.0), e1 + P(CORNER_MM if busy[1] else 12.0))
        last = 0.0
        if chains:
            last = _draw_side_chains(drawing, chains, side, edge, den, z, leaders=[a[1] for a in sel], limits=limits,
                                     outer_row1_forbid=bubble_forbid(sel))
            _ext_lines(drawing, side, sp["ext"], edge, den, z)
        n += len(chains)
        put(sel, side, last + BUBBLE_GAP_MM + R)
    return n


def _breakline(drawing, den, a, b, z=0.0):
    """折断线 a→b（米）。Bonsai 的 BREAKLINE 在每条边的中点画折断符号；一条切口只要一个符号，放在线上不压字、
    离中点最近的地方：符号那一小段画 BREAKLINE，两头接细实线（LINEWORK）。"""
    P = lambda mm: mm * den / 1000.0
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    L = float(np.linalg.norm(b - a))
    d = (b - a) / L
    half = P(3.0)                                   # 折断符号那一段的半长
    occ = _OCC.get(drawing.id(), [])

    def quiet(t):
        c = a + d * t
        box = _oriented_box(c[0], c[1], d[0], d[1], half + P(0.5), P(2.5))
        return not any(box.intersects(o) for o in occ)
    ts = sorted(np.arange(half, L - half, P(1.0)), key=lambda t: abs(t - L / 2))
    t = next((float(t) for t in ts if quiet(t)), L / 2)
    p0, p1 = a + d * (t - half), a + d * (t + half)
    if t - half > 1e-6:
        polyline(drawing, [tuple(a), tuple(p0)], z)
    polyline(drawing, [tuple(p0), tuple(p1)], z, object_type="BREAKLINE")
    if L - t - half > 1e-6:
        polyline(drawing, [tuple(p1), tuple(b)], z)


def cut_marks(drawing, den, plan, notes, z=0.0):
    """内部切口：裁切框边上画折断线（_breakline，符号避开已写的字），下端外侧注"接 A-03"之类。notes = {边: 文字}。"""
    P = lambda mm: mm * den / 1000.0
    bx0, by0, bx1, by1 = plan["env"]
    made = 0
    for side, lit in notes.items():
        if plan["kinds"].get(side) != "cut":
            continue
        if side in ("left", "right"):
            x = bx0 if side == "left" else bx1
            _breakline(drawing, den, (x, by0 - P(3.0)), (x, by1 + P(3.0)), z)
            if lit:
                sgn = 1 if side == "right" else -1
                tx = x + sgn * (P(6.0) + P(text_mm(lit, FONT_MM[2.5])) / 2)
                text(drawing, tx, by0 - P(14.0), lit, size=2.5, z=z, align="bottom-middle")
        else:
            y = by0 if side == "bottom" else by1
            _breakline(drawing, den, (bx0 - P(3.0), y), (bx1 + P(3.0), y), z)
            if lit:
                sgn = 1 if side == "top" else -1
                text(drawing, bx0 - P(14.0), y + sgn * P(6.0), lit, size=2.5, z=z, rot=90.0, align="bottom-middle")
        made += 1
    return made


def zone_key(drawing, den, edge, cuts, labels, z=0.0):
    """组合示意（总图上）：各分段的分区线（点划线，约定包 cn.css 的 jz-zoneline）+ 每段上方注图号和段名。
    分区线遇到已写的字（房间名等）断开，不压字。cuts：分区线的 x（米）；labels：[(x中点, 图号, 段名)]。"""
    P = lambda mm: mm * den / 1000.0
    bx0, by0, bx1, by1 = edge
    occ = _OCC.get(drawing.id(), [])
    for x in cuts:
        lo, hi = by0 - P(2.0), by1 + P(9.0)
        band = _box(x - P(0.8), lo, x + P(0.8), hi)
        holes = sorted((o.bounds[1] - P(0.5), o.bounds[3] + P(0.5)) for o in occ if o.intersects(band))
        y = lo
        for h0, h1 in holes + [(hi, hi)]:
            if h0 - y > P(1.0):
                polyline(drawing, [(x, y), (x, h0)], z, classes="jz-zoneline")
            y = max(y, h1)
    for x, ident, name in labels:   # 段名在下（最小的汉字），图号在上，中间空 0.9mm
        text(drawing, x, by1 + P(1.6 + 0.9 * FONT_MM[1.8] + 0.9), ident, size=3.5, z=z, align="bottom-middle")
        text(drawing, x, by1 + P(1.6), name, size=1.8, z=z, align="bottom-middle")
    return len(cuts)


def fmt_level(v: float) -> str:
    """标高（米，三位小数）：0 写 ±0.000。"""
    return "±0.000" if abs(v) < 5e-4 else f"{v:.3f}"


STACK_MAX = 4   # 多层标高叠注最多几行；层数更多、层高又相同时先用一行"最低～最高（层高 h）"的写法
LEVEL_KEEP_MM = (10.0, 25.0, 30.0)  # 标高先避开房间名标注点所在的竖条：左右各 10、上 25、下 30mm（图纸毫米）
LEVEL_GAP_MM = 11.0   # 竖条里放不下时，至少离开标注点这么远（上下、左右；房间名和面积两行约 ±4.5mm，中间再空一行多）


def level_style(f, views, levels, z=0.0):
    """一套图的楼面标高只用一种写法：在每张图上试放（level_mark dry=True），取每张都放得下的第一种。views = [(视图, 比例分母,
    裁切框)]。层数不超过 STACK_MAX 时依次试 stack、line、wrap，否则 line、wrap、stack；都放不下时用 wrap（各层层高不同、
    没有一行写法时 level_mark 自己退回 stack）。"""
    order = ("stack", "line", "wrap") if len(set(levels)) <= STACK_MAX else ("line", "wrap", "stack")
    return next((st for st in order if all(level_mark(f, v, den, levels, z=z, bbox_m=bb, style=st, dry=True) for v, den, bb in views)),
                "wrap")


def _level_layout(levels, kind, cols, den):
    """楼面标高一种写法的字和占位（米，相对字组左下角）：dict(txt 数字, up 叠注的各层（从低到高，加括号）, rows 行数, W 宽,
    H 高, L 标高线离底边, at "（层高 h）"的位置 (dx, dy) 或 None, note "（层高 h）"（各层等高时才有）, line 一行写法
    "最低～最高"（各层等高时才有）, rects 占的矩形, colw 叠注每列宽, lh 行距)。kind：stack / line / wrap（见 level_mark）；
    cols：叠注分几列。标高线比字长 1mm，符号挂在线右端下面。wrap："（层高 h）"写在线下：符号左边放得下就紧贴线下，否则写在
    符号下面。"""
    P = lambda mm: mm * den / 1000.0
    levels = sorted({round(float(v), 3) for v in levels}) or [0.0]
    upper = [f"({fmt_level(v)})" for v in levels[1:]]
    steps = sorted({round(b - a, 3) for a, b in zip(levels[:-1], levels[1:])})
    line = f"{fmt_level(levels[0])}～{fmt_level(levels[-1])}" if len(steps) == 1 else None
    note = f"（层高 {steps[0]:.3f}）" if line else None
    fs, fc = LEVEL_FONT_MM, FONT_MM[1.8]   # 数字；叠注的各层、"（层高 h）"（cn.css 里两者同字号）
    sym_w, sym_h = LEVEL_SYMBOL_MM          # 标高符号画在标高线右端下面（Bonsai 的 marker-end）
    lh, colw = P(max(fs, fc) * 1.2), P(text_mm("(00.000)", fc) + 1.5)
    gap = 1.0                # 一行写法里数字和"（层高 h）"之间（图纸毫米）
    sym_lift = sym_h + 0.4   # 标高线以下：标高符号（图纸毫米）
    txt, up = (fmt_level(levels[0]), upper) if kind == "stack" else (line, [])
    rows = math.ceil(len(up) / cols) if up else 0
    tw = text_mm(txt, fs) + (gap + text_mm(note, fc) if kind == "line" else 0.0)
    Wm = max([tw + 1.0, sym_w])
    nw = text_mm(note, fc) if kind == "wrap" else 0.0
    beside = nw + 1.0 + sym_w <= Wm
    Wm = max(Wm, nw)
    lift = sym_lift if kind != "wrap" else (max(sym_lift, 1.0 + 0.9 * fc + 0.2) if beside else sym_lift + 0.6 + 0.9 * fc + 0.2)
    W, L = max([P(Wm)] + ([cols * colw] if up else [])), P(lift)
    H = L + P(1.0) + rows * lh + P(fs)     # 标高线上 1mm 起往上：数字（和叠注的各层）
    rects = [(0.0, L, W, H), (W - P(sym_w), L - P(sym_lift), W, L)]
    at = None
    if kind == "line":
        at = (P(text_mm(txt, fs) + gap), L + P(1.0))
    elif kind == "wrap":
        at = (0.0, L - P(1.0 + 0.9 * fc)) if beside else (0.0, P(0.1))
        rects.append((0.0, at[1] - P(0.1), P(nw), at[1] + P(0.9 * fc + 0.1)))
    return dict(txt=txt, up=up, rows=rows, W=W, H=H, L=L, at=at, note=note, line=line, rects=rects, colw=colw, lh=lh)


def level_mark(f, drawing, den, levels, z=0.0, bbox_m=None, style="stack", dry=False, avoid_labels=True, place=None):
    """楼面标高：Bonsai 的 PLAN_LEVEL 标注，标高符号（GB/T 50001 的等腰直角三角形，约定包 markers-CN.svg）挂在标高线右端
    下面，数字写在线上。只显示说明文字（BBIM_Dimension.ShowDescriptionOnly，说明 = 最低一层的标高；不用它按 z 自动算的值，
    那个是毫米、不带三位小数）。数字、叠注的各层、"（层高 h）"同一字号（cn.css 的 PLANLEVEL，和 small 一样大）；
    "（层高 h）"是汉字，单独一段字。一个标准层代表多层时有下面几种写法，同一套图只用一种（level_style）：
      style="stack"：GB/T 50001 的多层标高单列叠注，本层在最下，往上依次是更高的各层（加括号）；
      style="line"：一行"最低～最高（层高 h）"，"（层高 h）"接在数字后面（各层等高时才能用，否则仍按 stack）；
      style="wrap"：同 line，一行放不下时数字写在线上，"（层高 h）"写在线下（标高符号左边放得下就在左边，否则在符号下面）。
    位置在本图范围里找一块空地：先交通/公共空间，再按面积从大到小（字组中心落在该房间里，字组可以跨过没有墙的房间
    分界），不出楼、不贴着切口、不压墙、门窗、开启范围、家具和点位符号、已写的文字（门窗编号先放，标高后放）；
    avoid_labels=True 时先避开各房间名标注点所在的竖条（LEVEL_KEEP_MM），免得标高正好落在房名、面积的中轴线上，读成
    房名的第三行；放不下就至少离开标注点 LEVEL_GAP_MM（和房名之间空一行多），再放不下才不管它。出图时（dry=False）各种写法都放不下，才压大件家具（床、桌、沙发，取压得最少的，记日志
    level mark over furniture）。dry=True 只试不画，返回放不放得下（一种写法的尺寸见 _level_layout）；place=(x, y)：不找位，
    直接以它为字组左下角写（顶面图的吊顶标高和名称一起找位，ceiling_notes）。
    否则返回写了几段字（放不下记日志、返回 0）。"""
    P = lambda mm: mm * den / 1000.0
    first = _level_layout(levels, "stack", 1, den)
    upper, line, note, colw, lh = first["up"], first["line"], first["note"], first["colw"], first["lh"]
    geo = plan_geometry(f)
    occ = _OCC.setdefault(drawing.id(), [])
    clip = _box(*bbox_m).buffer(-P(CUT_GAP_MM), join_style="mitre") if bbox_m else None

    def prio(s):
        u = geo.usage(s)
        return (0 if _public(geo, s) else 1, -geo.fp(s).area)
    rooms = []
    for s in sorted((s for s in geo.spaces if geo.fp(s) is not None and geo.usage(s) not in ("管井", "未标注")), key=prio):
        r = geo.fp(s) if clip is None else geo.fp(s).intersection(clip)
        if not r.is_empty:
            rooms.append(r)
    # 整组字放在楼里的空地上（可以跨过没有墙的房间分界，如起居和走道之间）：不压墙、门窗和开启范围、家具洁具和点位符号，
    # 离已写的字 0.6mm 以上
    inside = geo.building.buffer(-P(0.8))
    if clip is not None:
        inside = inside.intersection(clip)
    hard, hard_tree, _ = _obstacles(f, drawing)
    soft = soft_shapes(f, drawing)
    base_block = [hard[i] for i in hard_tree.query(inside)] + [o.buffer(P(0.6), join_style="mitre") for o in occ]
    free = inside.difference(shapely.union_all(base_block + [p for p, _ in soft]))
    frees = [free]
    if avoid_labels:   # 房间名标注点（JZ_Space.LabelX/Y_mm，没有就取包围框中心）所在的竖条
        kx, ku, kd = (P(v) for v in LEVEL_KEEP_MM)
        keep, near = [], []
        for s in geo.spaces:
            ps = uel.get_psets(s).get("JZ_Space", {})
            if "LabelX_mm" in ps:
                ax, ay = ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0
            elif geo.fp(s) is not None:
                b = geo.fp(s).bounds
                ax, ay = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            else:
                continue
            keep.append(_box(ax - kx, ay - kd, ax + kx, ay + ku))
            near.append(_box(ax - P(LEVEL_GAP_MM), ay - P(LEVEL_GAP_MM), ax + P(LEVEL_GAP_MM), ay + P(LEVEL_GAP_MM)))
        if keep:
            frees[:0] = [free.difference(shapely.union_all(keep)), free.difference(shapely.union_all(near))]
    light = [p for p, lt in soft if lt]   # 大件家具（床、桌、沙发、楼梯）：实在放不下时可以压，取压得最少的

    def find(W, H, rects, over=False):
        """按房间优先级找：字组（rects，相对左下角）整组在空地上、中心落在这个房间里、离房间中心近的位置先试（网格 1mm）。
        先避开房间名的竖条，放不下再不管它；over=True：可以压大件家具，取压的面积最少的（一样少取优先级高、离中心近的）。
        返回左下角 (x, y)。"""
        if over:
            fr_ = inside.difference(shapely.union_all(base_block + [p for p, lt in soft if not lt]))
            cover = shapely.union_all(light) if light else shapely.Polygon()
            best = None
            for k, r in enumerate(rooms):
                x0, y0, x1, y1 = r.bounds
                c = r.representative_point()
                for xs, ys, ds in _anchors(fr_.intersection(_box(x0 - W, y0 - H, x1 + W, y1 + H)), rects, c.x - W / 2, c.y - H / 2, P(1.0)):
                    ok = shapely.contains_xy(r, xs + W / 2, ys + H / 2)
                    if not ok.any():
                        continue
                    xs, ys, ds = xs[ok], ys[ok], ds[ok]
                    hit = sum(shapely.area(shapely.intersection(shapely.box(xs + q[0], ys + q[1], xs + q[2], ys + q[3]), cover)) for q in rects)
                    j = int(np.lexsort((ds, hit))[0])
                    cand = (round(float(hit[j]) / P(1.0) ** 2, 1), k, float(ds[j]), float(xs[j]), float(ys[j]))
                    if best is None or cand < best:
                        best = cand
            return best and (best[3], best[4])
        for fr_ in frees:
            for r in rooms:
                x0, y0, x1, y1 = r.bounds
                c = r.representative_point()
                for xs, ys, _ in _anchors(fr_.intersection(_box(x0 - W, y0 - H, x1 + W, y1 + H)), rects, c.x - W / 2, c.y - H / 2, P(1.0)):
                    ok = np.flatnonzero(shapely.contains_xy(r, xs + W / 2, ys + H / 2))
                    if len(ok):
                        return float(xs[ok[0]]), float(ys[ok[0]])
        return None

    if upper and style in ("line", "wrap") and line:   # wrap：一行放不下时"（层高 …）"写到标高线下面，写法不变
        tries = [(style, 1)] + ([("wrap", 1)] if style == "line" and not dry else [])
    else:   # 单列叠注；层高不等时单列放不下才分两三列（这时没有一行的写法）
        tries = [("stack", 1)] + ([("stack", 2), ("stack", 3)] if upper and not line and not dry else [])

    def layout(kind, cols):
        d = _level_layout(levels, kind, cols, den)
        return d["txt"], d["up"], d["rows"], d["W"], d["H"], d["L"], d["at"], d["rects"]

    spot, over = None, False
    if place is not None:
        kind, cols = tries[0]
        txt, up, rows, W, H, L, at, rects = layout(kind, cols)
        spot = place
    for over in (() if place is not None else (False,) if dry else (False, True)):   # 出图时都放不下才压大件家具
        for kind, cols in tries:
            txt, up, rows, W, H, L, at, rects = layout(kind, cols)
            spot = find(W, H, rects, over)
            if spot:
                break
        if spot:
            break
    if dry:
        return spot is not None
    if not spot:
        log("level mark skipped (no free spot)", drawing.Name, style)
        return 0
    if over:
        log("level mark over furniture", drawing.Name, kind)
    x, y = spot
    yl = y + L                                                  # 标高线
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="PLAN_LEVEL", relating_type=None)
    obj.matrix_world = Matrix.Identity(4)
    sp = obj.data.splines[0]
    for p, (px, py) in zip(sp.points, [(x, yl), (x + W, yl)]):
        p.co = (px, py, z, 1.0)
    commit(obj)
    el = tool.Ifc.get_entity(obj)
    el.Description = txt
    _no_text(obj)
    occ.append(_box(x, y, x + W, y + H))
    _LEVEL.setdefault(drawing.id(), []).append(occ[-1])
    for i, lit in enumerate(up):
        c, r = divmod(i, rows)
        text(drawing, x + c * colw, yl + P(1.0) + (r + 1) * lh, lit, size=1.8, z=z, align="bottom-left", register=False)
    if at is not None:
        text(drawing, x + at[0], y + at[1], note, size=1.8, z=z, align="bottom-left", register=False)
    return 1 + len(up) + (kind != "stack")


# ---------------------------------------------------------------- 门窗编号
def _opening_zone(fr):
    """门窗本身占的地方（墙厚范围内的洞口）+ 平开门扫过的地方，米（openings.opening_zones，家具校验用的同一份）；凸窗是它的轮廓。"""
    if fr["bay"]:
        return [fr["fp"]]
    el = fr["el"]
    return openings.opening_zones(fr["c"], fr["u"], fr["n"], fr["w"], fr["t"], (el.OperationType or "") if el.is_a("IfcDoor") else None)


def _obstacles(f, drawing):
    """放字要避开的硬障碍：墙 + 每樘门窗的洞口和平开门开启范围（+ 开关连线图上已画的接线）。返回 (列表, STRtree, 门窗 frame 列表)。"""
    geo = plan_geometry(f)
    frames = [fr for fr in (geo.frame(el) for el in geo.openings) if fr is not None]
    hard = list(geo.wall_polys) + [zz for fr in frames for zz in _opening_zone(fr)] + _WIRES.get(drawing.id(), [])
    return hard, shapely.STRtree(hard), frames


_SYM_OUTLINE: dict[int, object] = {}
SOFT_LIGHT = ("IfcFurniture", "IfcFurnishingElement", "IfcStair", "IfcStairFlight", "IfcRailing")   # 大件轮廓：字压上去还看得清


def soft_shapes(f, drawing, paper=True):
    """本视图里家具、洁具、点位等"软障碍"在平面图上实际占的地方（米）：三维 Body 的投影，再并上类型的二维符号
    （Plan/Body，约定包 symbols.json 生成；呼叫按钮、插座的符号比盒子大得多）。返回 [(轮廓, 是否大件)]。
    paper=False 时不含按纸面大小用 SYMBOL 画的点位（房间名、门窗编号把它们当硬障碍，见 paper_shapes）。"""
    geo = plan_geometry(f)
    ids = view_element_ids(drawing)
    big = paper_symbols(drawing)
    out = []
    for cls in openings.PlanGeometry.SOFT:
        for e in f.by_type(cls):
            if e.id() not in ids or (not paper and e.id() in big):
                continue
            p = _soft_outline(geo, e, big.get(e.id()))
            if p is not None:
                out.append((p, cls in SOFT_LIGHT))
    return out


def paper_shapes(f, drawing):
    """本视图按纸面大小用 SYMBOL 画的点位符号的笔画（米，_paper_outline）。房间名、门窗编号不压它们（和墙同一级的硬障碍：
    1:100、1:150 上符号比实物大得多，压上去字和符号都读不清）。"""
    return [p for e_id, v in paper_symbols(drawing).items() for p in [_paper_outline(f.by_id(e_id), *v)] if p is not None]


def _symbol_outline(e, scale):
    """构件类型的二维符号（Plan 上下文的 IfcPolyline）按构件放置变换后的外框（米）；没有返回 None。"""
    if e.id() in _SYM_OUTLINE:
        return _SYM_OUTLINE[e.id()]
    import ifcopenshell.util.placement as up
    t = uel.get_type(e)
    pts = [p.Coordinates for m in ((t.RepresentationMaps or []) if t else ()) for r in [m.MappedRepresentation]
           if getattr(r.ContextOfItems, "ContextType", "") == "Plan" for it in r.Items if it.is_a("IfcPolyline") for p in it.Points]
    res = None
    if pts:
        m = up.get_local_placement(e.ObjectPlacement)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        corners = [(x, y) for x in (min(xs), max(xs)) for y in (min(ys), max(ys))]
        res = shapely.MultiPoint([((m[0][0] * x + m[0][1] * y + m[0][3]) * scale, (m[1][0] * x + m[1][1] * y + m[1][3]) * scale)
                                  for x, y in corners]).convex_hull
    _SYM_OUTLINE[e.id()] = res
    return res


_PAPER_OUTLINE: dict[tuple, object] = {}
PAPER_INK_MM = 0.2   # 按纸面大小画的点位符号：笔画两侧各算这么宽（线宽 0.3mm 的一半再多一点，图纸毫米）
PAPER_TURNS = (0.0, 90.0, 270.0, 180.0)   # 按纸面大小画的点位符号在点位放置上再转的角度，由先到后试（paper_pose）
PAPER_CLEAN_MM = 0.05   # 笔画压墙、出房间不到这么长（图纸毫米）算不压
_POSE: dict[tuple, tuple] = {}
_SYMBOL_PACK: list = []


def _symbol_pack() -> dict:
    """约定包 symbols.json 的符号（第一次用时读，之后不再读）。"""
    if not _SYMBOL_PACK:
        _SYMBOL_PACK.append(symbols.load_pack(conventions.pack_file("symbols.json")))
    return _SYMBOL_PACK[0]


PAPER_SHIFT = False   # 水电图（make_plan_set --mep）打开：同一面墙上的符号沿墙排开（layout_paper），挪开的加引线
SHIFT_CLASS = "jz-shift"   # 挪开画的符号指回实际位置的引线（约定包 cn-mep.css）
LAYOUT_INK_MM = 0.15     # 排开时符号笔画两侧各算这么宽、圈起来的连里面（图纸毫米；和回归里"符号压符号"的量法同一个）
LAYOUT_GAP_MM = 0.3      # 排开后相邻两个符号之间至少空这么多（图纸毫米）
LAYOUT_MAX_MM = 8.0      # 一个符号最多挪这么远（图纸毫米；1:100 是 0.8m，约两个符号宽：再远读不出是哪一个点位，引线也太长）
_VIEW_POSE: dict[tuple, tuple] = {}   # (视图 id, 点位 id) → 排开后的摆法（带第三项沿墙挪开的距离）


def _paper_lines(f, e, k, pose):
    """点位 e 的纸面符号按 pose（符号键后缀, 再转的角度[, 沿墙挪开的距离]）画出来的折线（米，世界坐标）：符号变体
    （symbols.variant_polylines）转 pose 的角度、放大 k 倍，沿点位局部 x（沿墙）挪开（项目长度单位），再按点位的放置变换。
    点位类型不在符号库里返回 []。"""
    import ifcopenshell.util.placement as up
    spec = _symbol_pack().get(getattr(uel.get_type(e), "Tag", None))
    if spec is None:
        return []
    s = plan_geometry(f).S
    m = up.get_local_placement(e.ObjectPlacement)
    a = math.radians(pose[1])
    ca, sa = math.cos(a), math.sin(a)
    dx = pose[2] if len(pose) > 2 else 0.0

    def tf(x, y):
        x, y = (ca * x - sa * y) * k + dx, (sa * x + ca * y) * k
        return (m[0][0] * x + m[0][1] * y + m[0][3]) * s, (m[1][0] * x + m[1][1] * y + m[1][3]) * s
    return [[tf(*p) for p in pts] for pts in symbols.variant_polylines(spec, pose[0]) if len(pts) > 1]


def paper_pose(f, e, k, den, view=None):
    """按纸面大小画的点位符号怎么摆：(符号键后缀, 在点位放置上再转的角度[, 沿墙挪开的距离])。drawset._paper_symbols 按它建
    SYMBOL，_paper_outline 按它算笔画。符号代表贴墙的点位，要整个画在点位所在的房间里、不压墙：依次试转角 PAPER_TURNS 和各变体
    （symbols.variants：原样、左右镜像、画到插入点一侧），取第一个笔画不压墙、不出房间、不压平开门扫过的扇形的（都不到
    PAPER_CLEAN_MM）；都不行取压墙和出房间的笔画最短的（压墙的那段两样都算），一样短的取压门扇少的。原样就干净的不动。
    转向（转角不为 0）只在不转向的各变体都压墙或出房间时才用：不转向时符号背贴着点位所在的墙，转过去圆圈离墙远，读不出
    点位在哪道墙上；不转向的变体里有不压墙、不出房间的，就在这些里取压门扇少的。
    带字母的符号（K、TV、TD、LEB、L、R、P）摆好后转到字会倒着读的，后缀再加 _u（字母正过来，symbols.upright）。
    view（视图 id）：这张图上排开过（layout_paper）的用排开后的摆法（第三项是沿墙挪开的距离，项目长度单位）。
    房间：点位放置往局部 +y（朝室内）0.1m 那一点所在的房间（PlanGeometry.space_at），找不到只看墙。"""
    import ifcopenshell.util.placement as up
    if view is not None and (view, e.id()) in _VIEW_POSE:
        return _VIEW_POSE[(view, e.id())]
    key = (id(f), e.id(), k, den)
    if key in _POSE:
        return _POSE[key]
    geo = plan_geometry(f)
    spec = _symbol_pack().get(getattr(uel.get_type(e), "Tag", None))
    best = ("", 0.0)
    if spec is not None and geo.wall_tree is not None:
        m = up.get_local_placement(e.ObjectPlacement)
        ny = math.hypot(m[0][1], m[1][1]) or 1.0
        p = shapely.Point((m[0][3] + m[0][1] / ny * 100.0) * geo.S, (m[1][3] + m[1][1] / ny * 100.0) * geo.S)   # 往室内 100mm
        sp = geo.space_at(p)
        room = geo.fp(sp) if sp is not None else None
        swings = _swings(f)
        mm = 1000.0 / den   # 米 → 图纸毫米

        def measure(pose):   # (压墙, 出房间, 压门扇)，图纸毫米
            ink = shapely.MultiLineString(_paper_lines(f, e, k, pose))
            near = [geo.wall_polys[j] for j in geo.wall_tree.query(ink)]
            in_wall = shapely.union_all([w.intersection(ink) for w in near]).length * mm if near else 0.0
            out = (ink.length - ink.intersection(room).length) * mm if room is not None else 0.0
            return in_wall, out, ink.intersection(swings).length * mm
        score = None
        for i, pose in enumerate((suf, turn) for turn in PAPER_TURNS for suf in symbols.variants(spec)):
            if pose[1] != PAPER_TURNS[0] and score is not None and score[0] == 0.0:
                break   # 不转向就有不压墙、不出房间的：不转向
            in_wall, out, door = measure(pose)
            if max(in_wall, out, door) < PAPER_CLEAN_MM:
                best, score = pose, None
                break
            sc = (round(in_wall + out, 1), round(door, 1), i)
            if score is None or sc < score:
                score, best = sc, pose
    if spec is not None:   # 带字母的符号转到字会倒着读时换字母正过来的变体（_u，字母外框不变，上面算的压线照样成立）
        m = up.get_local_placement(e.ObjectPlacement)
        if symbols.upright(spec, math.degrees(math.atan2(m[1][0], m[0][0])) + best[1]):
            best = (best[0] + "_u",) + tuple(best[1:])
    _POSE[key] = best
    return best


def _isotonic(t):
    """t 的单调不减最小二乘拟合（相邻违序的合并取平均，pool adjacent violators）。"""
    blocks = []   # [和, 个数]
    for v in t:
        blocks.append([v, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s_, n_ = blocks.pop()
            blocks[-1][0] += s_
            blocks[-1][1] += n_
    return [b[0] / b[1] for b in blocks for _ in range(b[1])]


def symbol_region(f, e, k, pose, den, ink_mm=LAYOUT_INK_MM):
    """符号画出来占的地方（米）：笔画两侧各 ink_mm（图纸毫米），圈起来的连里面。回归的"符号压符号"按同一个量法。"""
    lines = _paper_lines(f, e, k, pose)
    if not lines:
        return None
    u = shapely.unary_union([LineString(p) for p in lines])
    return shapely.unary_union([u.buffer(ink_mm * den / 1000.0), *shapely.get_parts(shapely.polygonize(shapely.get_parts(u)))])


def layout_paper(f, view, items, den):
    """水电图上同一面墙上的点位符号沿墙排开（只在 PAPER_SHIFT 时由 drawset._paper_symbols 调用）。items = [(点位, 放大倍数)]。
    贴墙的点位（插入点在所在房间的一条边上、没有转向的）按房间和墙线（方向、位置相同，门洞两边的算一条）分组，沿墙排序；
    每个符号沿墙占的一段（符号占的地方在墙方向的投影，加 LAYOUT_GAP_MM 间隙）互不重叠、总挪动量（平方和）最小：先按这条墙线
    前面的障碍——地面上的点位（地漏、排水点，不挪）的符号、灯、平开门的洞口和开启范围、墙和房外——把墙线切成几段空段，每个
    符号归到放得下它、离它最近的空段（一段放不下时把两头的挪到别的空段），段内按单调最小二乘（_isotonic）求位置、再夹到
    空段里。挪动量超过 LAYOUT_MAX_MM 的那一段不按这样排；这些符号再一个一个按平面上的实际形状沿墙试（每 0.25mm，不超过
    LAYOUT_MAX_MM），取最近的一处不压别的符号、障碍、不出房间的；还不行的保持原样（记日志 layout kept）。只沿所在的墙线
    一维挪，不换墙。点位坐标不动：结果是这张图上的摆法（_VIEW_POSE，第三项是沿墙挪开的距离），drawset 给挪开的画引线。
    返回 (挪开的个数, 排不开的 [(房间名, [点位 Tag], 原因)])。"""
    import ifcopenshell.util.placement as up
    geo = plan_geometry(f)
    S = geo.S
    P = lambda mm: mm * den / 1000.0
    base, info = {}, {}
    for e, k in items:
        pose = paper_pose(f, e, k, den)
        reg = symbol_region(f, e, k, pose, den)
        if reg is None:
            continue
        m = up.get_local_placement(e.ObjectPlacement)
        p = np.array([m[0][3] * S, m[1][3] * S])
        u = np.array([m[0][0], m[1][0]]) / (math.hypot(m[0][0], m[1][0]) or 1.0)
        n = np.array([m[0][1], m[1][1]]) / (math.hypot(m[0][1], m[1][1]) or 1.0)
        sp = geo.space_at(shapely.Point(*(p + n * 0.1)))
        room = geo.fp(sp) if sp is not None else None
        edge = None
        if room is not None and not pose[1]:
            ring = list(room.exterior.coords)
            for i in range(len(ring) - 1):
                a, b = np.array(ring[i]), np.array(ring[i + 1])
                d = b - a
                L = float(np.hypot(*d))
                if L < 1e-6 or abs(d[0] * u[1] - d[1] * u[0]) / L > 0.01:   # 不和墙平行
                    continue
                if LineString([a, b]).distance(shapely.Point(*p)) < 0.002:
                    edge = (sp.id(), i, a, b)
                    break
        base[e.id()] = (e, k, pose, reg)
        info[e.id()] = (p, u, n, edge)
    fixed = [base[i][3] for i, v in info.items() if v[3] is None]   # 不挪的：地面点位、灯、转向的
    doors = [z_ for fr in (geo.frame(el) for el in geo.openings) if fr is not None and fr["el"].is_a("IfcDoor") for z_ in _opening_zone(fr)]
    walls_in = shapely.union_all(list(geo.wall_polys or [])).buffer(-(P(LAYOUT_INK_MM) + 0.001))   # 墙往里缩外扩的那一圈：笔画贴着墙面不算压，伸进墙 1mm 就算
    obst = shapely.union_all(fixed + doors + [walls_in])   # 不挪的符号、门洞和门扇、墙
    groups = {}   # 同一间房、同一条墙线（方向、离原点的距离按 5mm 取整）上的点位一组：门洞两边的也在一组
    for i, (p, u, n, edge) in info.items():
        if edge is not None:
            groups.setdefault((edge[0], round(math.degrees(math.atan2(u[1], u[0]))) % 360, round(float(np.dot(p, n)) * 200)), []).append(i)
    moved, kept, shift = 0, [], {}
    paper = lambda m_: m_ * 1000.0 / den   # 米 → 图纸毫米
    pts_of = lambda g: [c for h in getattr(g, "geoms", [g]) if not h.is_empty
                        for c in (h.exterior.coords if h.geom_type == "Polygon" else h.coords)]
    for (sid, _, _), ids in sorted(groups.items()):
        u, n = info[ids[0]][1], info[ids[0]][2]
        along = lambda g: [float(np.dot(np.array(c), u)) for c in pts_of(g)]
        boxes = {i: (min(along(base[i][3])), max(along(base[i][3]))) for i in ids}   # 每个符号沿墙占的一段
        reach = P(LAYOUT_MAX_MM) + max(b_ - a_ for a_, b_ in boxes.values())
        span = (min(a_ for a_, _ in boxes.values()) - reach, max(b_ for _, b_ in boxes.values()) + reach)
        p0 = info[ids[0]][0]
        at = lambda s_, dn: p0 + u * (s_ - float(np.dot(p0, u))) + n * dn
        depth = max(float(np.dot(np.array(c) - p0, n)) for i in ids for c in pts_of(base[i][3]))
        band = shapely.Polygon([at(span[0], 0.0001), at(span[1], 0.0001), at(span[1], depth), at(span[0], depth)])   # 墙前符号占的那一条
        room = geo.fp(f.by_id(sid))
        hit = shapely.union_all([obst.intersection(band), band.difference(room.buffer(P(LAYOUT_INK_MM)))])   # 障碍、墙和房外
        cuts = [(min(xs), max(xs)) for g in getattr(hit, "geoms", [hit]) if not g.is_empty and g.area > 1e-12 for xs in [along(g)]]
        free = [(a_ + P(0.2), b_ - P(0.2)) for a_, b_ in _subtract_spans(span, cuts) if b_ - a_ > P(0.4)]   # 笔画离墙、障碍再留 0.2mm
        by_free = {}
        for i in ids:   # 归到放得下它、离它最近的空段（都放不下就归最近的）
            c, wi = sum(boxes[i]) / 2, boxes[i][1] - boxes[i][0]
            dist = lambda j: 0.0 if free[j][0] <= c <= free[j][1] else min(abs(c - free[j][0]), abs(c - free[j][1]))
            fit = [j for j in range(len(free)) if free[j][1] - free[j][0] >= wi]
            j = min(fit or range(len(free)), key=dist, default=None)
            if j is not None:
                by_free.setdefault(j, []).append(i)
        wid = lambda i: boxes[i][1] - boxes[i][0] + P(LAYOUT_GAP_MM)
        for _ in range(len(ids)):   # 一段放不下这几个：把两头的一个挪到别的放得下它的空段（离它最近的），直到都放得下或挪不动
            over = [j for j, mem in sorted(by_free.items()) if sum(wid(i) for i in mem) - P(LAYOUT_GAP_MM) > free[j][1] - free[j][0]]
            if not over:
                break
            mem = sorted(by_free[over[0]], key=lambda i: sum(boxes[i]))
            cand = [(min(abs(sum(boxes[i]) / 2 - free[j2][0]), abs(sum(boxes[i]) / 2 - free[j2][1])), i, j2) for i in {mem[0], mem[-1]}
                    for j2 in range(len(free)) if j2 != over[0] and free[j2][1] - free[j2][0] - sum(wid(x) for x in by_free.get(j2, []))
                    >= wid(i) - P(LAYOUT_GAP_MM)]
            if not cand:
                break
            _, i, j2 = min(cand)
            by_free[over[0]].remove(i)
            by_free.setdefault(j2, []).append(i)
        for j, mem in sorted((j, m_) for j, m_ in by_free.items() if m_):
            lo, hi = free[j]
            mem.sort(key=lambda i: (sum(boxes[i]), i))
            gap = P(LAYOUT_GAP_MM)
            w = [boxes[i][1] - boxes[i][0] + gap for i in mem]
            C = [sum(w[:q]) for q in range(len(mem))]
            tags = [base[i][0].Tag or str(i) for i in mem]
            if C[-1] + w[-1] - gap > hi - lo + 1e-9:
                if len(mem) > 1 or boxes[mem[0]][0] < lo or boxes[mem[0]][1] > hi:
                    kept.append((sp_name(f, sid), tags, f"这段墙 {paper(hi - lo):.1f}mm，放不下 {paper(C[-1] + w[-1] - gap):.1f}mm", mem, u))
                continue
            z = _isotonic([boxes[i][0] - C[q] for q, i in enumerate(mem)])
            z = [min(max(v, lo), hi - C[-1] - (w[-1] - gap)) for v in z]
            d = [z[q] + C[q] - boxes[i][0] for q, i in enumerate(mem)]
            if max(abs(x) for x in d) > P(LAYOUT_MAX_MM):
                kept.append((sp_name(f, sid), tags, f"要挪 {paper(max(abs(x) for x in d)):.1f}mm，超过 {LAYOUT_MAX_MM:g}mm", mem, u))
                continue
            for q, i in enumerate(mem):
                if abs(d[q]) > P(0.25):   # 挪不到 0.25mm 的不挪（原来的位置已经不压墙、不出房间）
                    shift[i] = (d[q], u)
    # 第二遍：上面按沿墙投影排（门扇的扇形、地面点位按它们在这条墙前面的投影整段让开，偏保守）排不开的，一个一个按平面上
    # 实际的形状试：沿墙往两边每 0.25mm 挪，取最近的一处不压别的符号（已排好的位置）、障碍，不出房间的
    placed = lambda i: shapely.affinity.translate(base[i][3], *(shift[i][1] * shift[i][0])) if i in shift else base[i][3]
    still = []
    for room_name, tags, why, mem, u in kept:
        room = None
        for i in mem:
            sp = geo.space_at(shapely.Point(*(info[i][0] + info[i][2] * 0.1)))
            room = geo.fp(sp).buffer(P(LAYOUT_INK_MM)) if sp is not None else None
            others = shapely.union_all([obst] + [placed(j) for j in base if j != i])
            bad = lambda g: (g.intersection(others).area * paper(1.0) ** 2 > 0.05
                             or (room is not None and g.difference(room).area * paper(1.0) ** 2 > 0.05))
            if not bad(base[i][3]):
                continue
            steps = sorted((s_ * 0.25 for s_ in range(-int(LAYOUT_MAX_MM * 4), int(LAYOUT_MAX_MM * 4) + 1) if s_), key=lambda v: (abs(v), v))
            hit = next((P(v) for v in steps if not bad(shapely.affinity.translate(base[i][3], *(u * P(v))))), None)
            if hit is None:
                still.append((room_name, [base[i][0].Tag or str(i)], why))
            else:
                shift[i] = (hit, u)
    for i, (dd, u) in shift.items():
        pose = base[i][2]
        _VIEW_POSE[(view, i)] = (pose[0], pose[1], dd * float(np.dot(info[i][1], u)) / S)   # 沿点位自己的局部 x（项目长度单位）
        moved += 1
    for r in still:
        log("layout kept", *r)
    return moved, still


def sp_name(f, sid):
    sp = f.by_id(sid)
    return sp.LongName or sp.Name or str(sid)


def _subtract_spans(span, cuts):
    segs = [tuple(span)]
    for c0, c1 in sorted(cuts):
        segs = [s for a, b in segs for s in ((a, min(b, c0)), (max(a, c1), b)) if s[1] > s[0]]
    return segs


def shift_leader(f, drawing, e, k, pose, z=0.0):
    """沿墙挪开画的符号（paper_pose 第三项不为 0）：从点位实际位置画一条细线到符号笔画上最近的一点（LINEWORK，样式类
    SHIFT_CLASS，关联到点位，Name"引线 Tag"），登记进 _WIRES（后放的字不压它）。没挪开返回 None。"""
    import ifcopenshell.util.placement as up
    if len(pose) < 3 or not pose[2]:
        return None
    s = plan_geometry(f).S
    m = up.get_local_placement(e.ObjectPlacement)
    p0 = shapely.Point(m[0][3] * s, m[1][3] * s)
    ink = shapely.MultiLineString(_paper_lines(f, e, k, pose))
    p1 = shapely.ops.nearest_points(ink, p0)[0]
    obj = polyline(drawing, [(p0.x, p0.y), (p1.x, p1.y)], z, classes=SHIFT_CLASS)
    a = tool.Ifc.get_entity(obj)
    a.Name = f"引线 {e.Tag or e.id()}"
    api.drawing.assign_product(f, relating_product=e, related_object=a)
    _WIRES.setdefault(drawing.id(), []).append(LineString([(p0.x, p0.y), (p1.x, p1.y)]).buffer(_den_of(drawing) / 1000.0 * WIRE_CLEAR_MM))
    return obj


_DOOR_ZONES: dict[int, object] = {}


def _door_zones(f):
    """门窗洞口和平开门扫过的扇形（_opening_zone 全部，米）合在一起，每个 IFC 文件算一次。"""
    k = id(f)
    if k not in _DOOR_ZONES:
        geo = plan_geometry(f)
        zs = [z_ for fr in (geo.frame(el) for el in geo.openings) if fr is not None for z_ in _opening_zone(fr)]
        _DOOR_ZONES[k] = shapely.union_all(zs) if zs else shapely.Polygon()
    return _DOOR_ZONES[k]


_SWINGS: dict[int, object] = {}


def _swings(f):
    """平开门扫过的扇形（_opening_zone 里洞口以外的部分，米）合在一起，每个 IFC 文件算一次。"""
    k = id(f)
    if k not in _SWINGS:
        geo = plan_geometry(f)
        zs = [z_ for fr in (geo.frame(el) for el in geo.openings) if fr is not None for z_ in _opening_zone(fr)[1:]]
        _SWINGS[k] = shapely.union_all(zs) if zs else shapely.Polygon()
    return _SWINGS[k]


def _paper_outline(e, k, den, view=None):
    """按纸面大小用 SYMBOL 画的点位在平面上实际的笔画（米）：符号按 paper_pose 摆（镜像、画到一侧、再转的角度），绕放置点
    放大 k 倍、按构件放置变换，两侧各加 PAPER_INK_MM；圈起来的地方（圆、方框）连里面一起算。不用外框：开关的斜线、插座的
    半圆旁边的空地还能写字。view：视图 id（这张图上排开过的按排开后的摆法）。点位类型不在符号库里返回 None。"""
    key = (e.id(), k, den, view)
    if key in _PAPER_OUTLINE:
        return _PAPER_OUTLINE[key]
    f = tool.Ifc.get()
    lines = _paper_lines(f, e, k, paper_pose(f, e, k, den, view))
    res = None
    if lines:
        ink = shapely.unary_union([LineString(pts) for pts in lines])
        rings = shapely.get_parts(shapely.polygonize(shapely.get_parts(ink)))
        res = shapely.unary_union([ink.buffer(PAPER_INK_MM * den / 1000.0), *rings])
    _PAPER_OUTLINE[key] = res
    return res


# 门窗编号的位置约束（图纸毫米）：编号中心离本洞口不超过 TAG_NEAR_MM（放不下才放宽到 TAG_FAR_MM）；离别的门窗至少比离
# 本洞口远 TAG_CLEAR_MM（免得读成别的门窗的编号）；从洞口到编号的连线不穿墙（编号和洞口在同一侧、同一个房间）
TAG_NEAR_MM, TAG_FAR_MM, TAG_CLEAR_MM = 8.0, 12.0, 1.0
LABEL_KEEP_MM = 2.0   # 编号尽量不压房间名标注点周围这么远（图纸毫米）
CUT_GAP_MM = 0.8      # 字离分区、分段图的裁切框（内部切口的折断线画在框上）至少这么远（图纸毫米）
SMALL_ROOM_PEN = 8.0  # 门的编号写进这种小房间的代价（比写在门的另一侧的 5 分大，比垂直于墙写的 10 分小）
DIM_REACH_MM = 8.0    # 下、右两侧第一道尺寸的小段，数字可能写到本段中点两边这么远（图纸毫米），室外的编号不进这一带
OUT_TAG_MM = 5.5     # 上、左两侧室外的编号（贴着外墙面）不超出视图范围（裁切框）外扩这么多（图纸毫米）：1:100 的常规字号也写得下一行


def _dim_band(plan, den):
    """下、右两侧（尺寸数字朝里写）第一道尺寸里写不下数字的小段附近，数字两排占的地方（米，block_plan 的安排）：室外的门窗编号
    不写进去，免得小段数字没处写、被挤到别的段上。没有返回 None。"""
    P = lambda mm: mm * den / 1000.0
    bx0, by0, bx1, by1 = plan["env"]
    lo, hi = P(DIM_BASE_MM - 0.3), P(DIM_BASE_MM + ROW_STEP_MM + 3.0)
    out = []
    for side in ("bottom", "right"):
        chains = plan["sides"][side]["chains"]
        if not chains:
            continue
        vs = _dim_points(chains[0])
        fixed = by0 - P(CHAIN_MM[0]) if side == "bottom" else bx1 + P(CHAIN_MM[0])
        for a, b in zip(vs, vs[1:]):
            if _dim_room(a, b, den):
                continue
            m, r = (a + b) / 2, P(DIM_REACH_MM)
            out.append(_box(m - r, fixed + lo, m + r, fixed + hi) if side == "bottom" else _box(fixed - hi, m - r, fixed - lo, m + r))
    return shapely.union_all(out) if out else None


def opening_tags(f, drawing, den, z=0.0, size=None, max_den=150, bbox_m=None, plan=None):
    """门窗编号：在每樘门窗旁边放一个 Bonsai 文字标注，关联到门窗构件（IfcRelAssignsToProduct），内容是 {{Tag}}，
    出图时 Bonsai 换成构件的编号（Tag 由 openings.assign_marks 写好）。洞口中心在本图范围里的门窗都标（跨切口的两张图都标）。
    位置：沿墙放、和墙平行（竖墙上的字转 90°），门放在不开启的一侧、窗放在室内一侧；必须满足：
      - 不压墙、门窗、开启范围、已写的文字，在建筑轮廓和视图范围内（离裁切框 CUT_GAP_MM 以上）；
      - 编号中心离本洞口不超过 TAG_NEAR_MM，比离任何别的门窗都近（至少近 TAG_CLEAR_MM）；
      - 从洞口到编号的连线不穿墙（不隔墙写到别的房间、井道里）；
      - 不压家具、洁具和点位符号（按纸面大小画的点位符号 paper_shapes 和墙同一级，引出的字也不压）。
    满足的位置里离洞口近的优先（门洞两侧都试），房间名的标注点能避就避；门的编号尽量不写进只写名字的小房间（SMALL_ROOM_M2，
    卫生间、管井等，房间里留给房名）；点位符号按纸面大小画的图上，纸面上小的房间外墙上的窗，编号先试室外；小房间、走道预留的
    房名位置（space_labels reserve），窗的编号在那间房里先不和它同行、相邻行，近处、室外都放不下才不管它（下、右两侧外墙上的
    窗除了纸面上小的房间的，不管它）。
    放不下时依次：外墙门窗贴外墙面放在室外一侧（plan 给了时不进下、右两侧小段尺寸数字的两排，_dim_band；记日志 tag outside）；
    距离放宽到 TAG_FAR_MM（记日志 tag far）；画引出线指向洞口（_leader_tag，字和引线也不压家具设备；旁边本来就没有合格位置的
    排在最前、马上画，免得洞口两侧的空地先被别的编号占满；只是被挤掉或只能压家具设备的，等别的编号放好再画）；引出线也找不到
    干净的位置时，才写在近处压家具设备最少的地方（记日志 tag over furniture）；不压纸面符号实在放不下时，写在近处压符号最少的
    地方（记日志 tag over symbol）。
    字号：1:100 及更大的图用 regular；1:150 用最小一档（含汉字的编号 small，只有字母数字的 NUM）。
    比例小于 1:max_den 的图不标。返回标了几樘。"""
    if den > max_den:
        return 0
    size = size or (2.5 if den <= 100 else 1.8)
    tag_size = lambda tag: small_size(tag) if size == 1.8 else size   # 1:150 的小字：含汉字的编号 small，其余 NUM
    P = lambda mm: mm * den / 1000.0
    geo = plan_geometry(f)
    ids = view_element_ids(drawing)
    hard0, hard_tree0, frames = _obstacles(f, drawing)
    syms = paper_shapes(f, drawing)   # 纸面符号：和墙同一级
    sym_tree = shapely.STRtree(syms) if syms else None
    hard = hard0 + syms
    hard_tree = shapely.STRtree(hard)
    soft = soft_shapes(f, drawing, paper=False)
    soft_tree = shapely.STRtree([p for p, _ in soft]) if soft else None
    # 洞口外包框（构件几何的外包，全部门窗含电梯层门）：编号离谁近按它算
    obox = {o.id(): _box(*geo.fp(o).bounds) for o in geo.openings if geo.fp(o) is not None}
    oids = list(obox)
    otree = shapely.STRtree([obox[i] for i in oids])
    allowed = geo.building.buffer(-P(0.4))
    if bbox_m:
        allowed = allowed.intersection(_box(*bbox_m).buffer(-P(CUT_GAP_MM), join_style="mitre"))
    shapely.prepare(allowed)
    bld_in = geo.building.buffer(-P(0.1))   # 室外放编号时不能碰到建筑
    shapely.prepare(bld_in)
    # 室外的编号不超出视图范围外扩 4mm；上、左两侧（尺寸数字朝外写）放宽到 OUT_TAG_MM（贴着外墙面写一行字：0.6mm 空隙 + 1:100
    # 的常规字高 + 余量；第一道尺寸在 10mm 外）。下、右两侧的数字朝里写，编号贴近了会和数字排成一列
    box_out = _box(*bbox_m).buffer(P(4), join_style="mitre") if bbox_m else None
    box_wide = _box(*bbox_m).buffer(P(OUT_TAG_MM), join_style="mitre") if bbox_m else None
    gx, gy = grid_axes(f)
    axis_x, axis_y = [a[1] for a in gx], [a[1] for a in gy]
    occ = _OCC.setdefault(drawing.id(), [])
    # 小房间、交通空间预留的房名位置（space_labels reserve）：在那间房里，编号和它不排在同一行（左右隔 SAME_LINE_MM）、上下隔一行
    # （ROW_MM；隔着墙的室外、别的房间不管）。窗的编号在近处、室外都先按不占它找（use_res），找不到再照常放
    # （预留只对编号生效）
    res = []
    for b, reg in _RESERVE.get(drawing.id(), []):
        x0, y0, x1, y1 = b.bounds
        cross = shapely.union_all([_box(x0 - P(SAME_LINE_MM), y0, x1 + P(SAME_LINE_MM), y1), _box(x0, y0 - P(ROW_MM), x1, y1 + P(ROW_MM))])
        res.append(cross.intersection(reg) if reg is not None and not reg.is_empty else cross)
    res = [r for r in res if not r.is_empty]
    res_tree = shapely.STRtree(res) if res else None

    def hits_res(b):
        return res_tree is not None and len(res_tree.query(b, predicate="intersects")) > 0
    fronts = {fr["el"].id(): _front_zone(fr) for fr in frames if fr["el"].is_a("IfcDoor") and fr["el"].Tag}
    # 内部切口（建筑伸出裁切框的那几条边，折断线画在边上、两头各伸出 3mm）：室外的编号也不压它
    cut_band = None
    if bbox_m:
        bx = _box(*bbox_m)
        cuts = []
        for a, b in zip(bx.exterior.coords[:-1], bx.exterior.coords[1:]):
            a, b = np.array(a), np.array(b)
            d = (b - a) / np.linalg.norm(b - a)
            if LineString([a, b]).intersects(geo.building.buffer(-0.05)):
                cuts.append(LineString([a - d * P(3.5), b + d * P(3.5)]).buffer(P(CUT_GAP_MM), cap_style="flat"))
        cut_band = shapely.union_all(cuts) if cuts else None
    if plan is not None:   # 下、右两侧小段尺寸数字的两排：室外的编号不写进去
        band = _dim_band(plan, den)
        if band is not None:
            cut_band = band if cut_band is None else cut_band.union(band)
    # 只写名字的小房间（不是走道等交通空间）：门的编号尽量写在门外，也不占门外那间房的房名（标注点周围一个房名组大小）
    small, groups = [], []
    for sp in geo.spaces:
        fp, u, ps = geo.fp(sp), geo.usage(sp), uel.get_psets(sp).get("JZ_Space", {})
        if fp is None or u in ("", openings.UNNAMED_ROOM):
            continue
        if _named(geo, sp) and _area_m2(geo, sp) < SMALL_ROOM_M2:
            small.append(fp)
        elif "LabelX_mm" in ps:   # 房名组约占：名字宽 + 1mm，两行高
            hw, hh = P(text_mm(u, FONT_MM[2.5]) / 2 + 0.5), P(4.5)
            ax, ay = ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0
            groups.append(_box(ax - hw, ay - hh, ax + hw, ay + hh))
    small_zone = shapely.union_all(small) if small else None
    group_zone = shapely.union_all(groups) if groups else None
    for z_ in (small_zone, group_zone):
        if z_ is not None:
            shapely.prepare(z_)
    small_doors = {fr["el"].id() for fr in frames if fr["el"].is_a("IfcDoor") and small_zone is not None
                   and any(small_zone.contains(shapely.Point(*(fr["c"] + fr["n"] * s_ * (fr["t"] / 2 + 0.1)))) for s_ in (1, -1))}
    # 点位符号按纸面大小画的视图（paper_symbols）：纸面上小的房间（不到 1:100 时 SMALL_ROOM_M2 那么大，1:150 时约 12㎡）里多半被
    # 符号占满，房间里留给房名——这种房间外墙上的窗，编号先试室外（search_outside），放不下照旧
    yield_wins = set()
    if paper_symbols(drawing):
        lim = SMALL_ROOM_M2 * max(1.0, den / 100.0) ** 2
        rooms = [geo.fp(sp) for sp in geo.spaces if geo.fp(sp) is not None and geo.usage(sp) not in ("", openings.UNNAMED_ROOM)
                 and _area_m2(geo, sp) < lim]
        yield_wins = {fr["el"].id() for fr in frames if fr["el"].is_a("IfcWindow") and fr.get("out") is not None
                      and any(r.contains(shapely.Point(*(fr["c"] - fr["out"] * (fr["t"] / 2 + 0.1)))) for r in rooms)}

    def room_pen(el, b):
        """小房间的门：编号写进小房间里罚 SMALL_ROOM_PEN；写在门外压着那间房的房名组（估的）也罚 SMALL_ROOM_PEN。"""
        if el.id() not in small_doors:
            return 0.0
        if small_zone.intersects(b.centroid) or (group_zone is not None and group_zone.intersects(b)):
            return SMALL_ROOM_PEN
        return 0.0
    tagged = []   # [(编号, 字框)]：引出的编号不和同号的编号上下叠放

    def hits_text(b):
        b = b.buffer(P(0.6), join_style="mitre")   # 和别的文字至少隔开 0.6mm
        x0, y0, x1, y1 = b.bounds
        return any(o.bounds[0] < x1 and o.bounds[2] > x0 and o.bounds[1] < y1 and o.bounds[3] > y0 and b.intersects(o) for o in occ)

    def legal(el, own_zone, cpos, dmax):
        """硬约束：离本洞口 ≤ dmax；离别的门窗都比离本洞口远 TAG_CLEAR_MM；从洞口到编号中心不穿墙。"""
        q = shapely.Point(float(cpos[0]), float(cpos[1]))
        d_own = obox[el.id()].distance(q)
        if d_own > dmax:
            return False
        r = d_own + P(TAG_CLEAR_MM)
        if any(oids[i] != el.id() and obox[oids[i]].distance(q) < r for i in otree.query(q.buffer(r))):
            return False
        line = LineString([shapely.ops.nearest_points(own_zone, q)[0], q])
        return not geo.crosses_wall(line, WALL_TOUCH_M)

    # 房间名的标注点（JZ_Space.LabelX/Y_mm，房间名后放）周围 LABEL_KEEP_MM 能避就避：阳台、小卫生间里编号先占了中间，
    # 房间名就没地方写了
    lab = [shapely.Point(ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0).buffer(P(LABEL_KEEP_MM))
           for ps in (uel.get_psets(sp).get("JZ_Space", {}) for sp in geo.spaces) if "LabelX_mm" in ps]
    lab_zone = shapely.union_all(lab) if lab else None

    def soft_hits(b):
        return () if soft_tree is None else soft_tree.query(b, predicate="intersects")

    def soft_pen(b):
        pen = 20 if lab_zone is not None and b.intersects(lab_zone) else 0
        return pen + sum(25 if soft[i][1] else 60 for i in soft_hits(b))

    def other_pen(el, b):
        """引出的字离别的门窗：2mm 以内加 LEADER_NEAR_OTHER；比离自己的洞口还近加 LEADER_CLOSER（强代价，不是硬约束）。"""
        d_own = obox[el.id()].distance(b)
        near = [obox[oids[i]].distance(b) for i in otree.query(b.buffer(max(d_own, P(2.0)))) if oids[i] != el.id()]
        return (LEADER_NEAR_OTHER if any(d < P(2.0) for d in near) else 0.0) + (LEADER_CLOSER if any(d < d_own for d in near) else 0.0)

    deg = lambda d: math.degrees(math.atan2(d[1], d[0]))
    step = P(0.8)

    def prep(fr):
        """一樘门窗的候选位置：(分数, 字中心, 阅读方向)，按分数从低到高。"""
        el = fr["el"]
        u = fr["u"] if (fr["u"][0] > 1e-6 or (abs(fr["u"][0]) <= 1e-6 and fr["u"][1] > 0)) else -fr["u"]   # 字从左往右、从下往上读
        n = fr["n"]
        v = n if (n[0] > 1e-6 or (abs(n[0]) <= 1e-6 and n[1] > 0)) else -n                              # 垂直于墙写时的阅读方向
        fs = FONT_MM[tag_size(el.Tag)]
        tw, th = P(text_mm(el.Tag, fs)), P(fs * 0.9)
        # 优先一侧：门放不开启的一侧（局部 -y）；窗放室内一侧；凸窗放室内（frame 的 n 已经朝里）
        if fr["bay"]:
            pref = 1
        elif el.is_a("IfcDoor"):
            pref = -1
        else:
            pref = -1 if fr.get("out") is not None and float(np.dot(fr["out"], n)) > 0 else 1
        ks = range(0, int((fr["w"] / 2 + tw / 2 + P(TAG_FAR_MM)) / step) + 1)   # 沿墙最多挪到字中心离洞口端 TAG_FAR_MM
        cands = []
        for sgn in (pref, -pref):
            side_pen = 0 if sgn == pref else 5
            for j in range(40):                    # 和墙平行写，离墙由近到远（门扇一侧要越过开启范围才放得下）
                off_n = fr["t"] / 2 + P(0.8) + th / 2 + j * (th / 2 + P(0.4))
                if off_n - fr["t"] / 2 > P(TAG_FAR_MM) + th / 2:
                    break
                for k in ks:
                    for s in ((1, -1) if k else (1,)):
                        cands.append((k * 0.8 + 1.6 * j + side_pen, fr["c"] + n * sgn * off_n + u * (s * k * step), u))
            # 门挨着门、窄走道里和墙平行放不下时，门的编号可以垂直于墙写（罚 10 分：平行能放就平行放）；
            # 窗不这样写（垂直于本墙的字和旁边的墙平行，容易读成那道墙上的窗），放不下就放室外或引出
            for j in range(40 if el.is_a("IfcDoor") else 0):
                off_n = fr["t"] / 2 + P(0.8) + tw / 2 + j * P(0.8)
                if off_n - fr["t"] / 2 > P(TAG_FAR_MM) + tw / 2:
                    break
                for k in range(0, int((fr["w"] / 2 + th / 2 + P(TAG_FAR_MM)) / step) + 1):
                    for s in ((1, -1) if k else (1,)):
                        cands.append((10 + k * 0.8 + 1.6 * j + side_pen, fr["c"] + n * sgn * off_n + u * (s * k * step), v))
        cands.sort(key=lambda t: t[0])
        return el, u, n, tw, th, _opening_zone(fr)[0], ks, cands

    first = {}   # 门窗 id → 不管已写的字时最好的位置（字框），先画的引出线编号尽量别占

    def room(pp):
        """先不管已写的字，近处（分数 ≤12）有几个合格位置（抽样，最多数到 20）：位置少的先放，免得被别的编号占掉。"""
        el, u, n, tw, th, own, ks, cands = pp
        cnt = 0
        for score, cpos, d in cands[::3]:
            if score > 12 or cnt >= 20:
                break
            b = _oriented_box(cpos[0], cpos[1], d[0], d[1], tw / 2 + P(0.4), th / 2 + P(0.3))
            if allowed.contains(b) and not len(hard_tree.query(b, predicate="intersects")) and legal(el, own, cpos, P(TAG_NEAR_MM)):
                first.setdefault(el.id(), b)
                cnt += 1
        return cnt

    def search(pp, dmax, soft_ok=False, over_sym=False, use_res=False):
        """离洞口 dmax 以内的合格位置里"分数 + 代价"最低的：(代价, 字中心, 字框, 方向)，没有返回 None。
        soft_ok=False 时压家具、洁具的位置不要（代价只剩房间名标注点）；True 时计代价（大件 25、其余 60）。纸面符号不压；
        over_sym=True（实在放不下时）可以压，每压一个计 60。use_res=True：不占预留的房名位置（hits_res）。"""
        el, u, n, tw, th, own, ks, cands = pp
        best = None
        for score, cpos, d in cands:
            if best is not None and best[0] <= score:
                break                                  # 后面的候选分数只会更高
            b = _oriented_box(cpos[0], cpos[1], d[0], d[1], tw / 2 + P(0.4), th / 2 + P(0.3))
            if not allowed.contains(b) or len((hard_tree0 if over_sym else hard_tree).query(b, predicate="intersects")) or hits_text(b) \
                    or (use_res and hits_res(b)):
                continue
            if not soft_ok and len(soft_hits(b)):
                continue
            pen = score + soft_pen(b) + room_pen(el, b)
            if over_sym and sym_tree is not None:
                pen += 60 * len(sym_tree.query(b, predicate="intersects"))
            if best is not None and pen >= best[0]:
                continue
            if not legal(el, own, cpos, dmax):
                continue
            best = (pen, cpos, b, d)
            if pen == score:
                break
        return best

    def search_outside(fr, pp, dmax, perp=False, use_res=False):
        """外墙门窗贴着外墙面放在室外一侧（凸窗放在凸窗外沿外），离第一道尺寸还有距离；不压室外的轴号引线。
        perp=True（小房间外墙上的窗，和墙平行写不下时，如窄天井里）：垂直于墙写，字的一端贴着墙面。use_res 同 search。"""
        el, u, n, tw, th, own, ks, cands = pp
        out_sgn = 1 if float(np.dot(fr["out"], n)) > 0 else -1
        d = (n if (n[0] > 1e-6 or (abs(n[0]) <= 1e-6 and n[1] > 0)) else -n) if perp else u   # 字的方向（从左往右、从下往上读）
        off_n = (fr.get("depth", 0.0) if fr["bay"] else fr["t"] / 2) + P(0.6) + (tw if perp else th) / 2
        for k in ks:
            for s in ((1, -1) if k else (1,)):
                cpos = fr["c"] + n * out_sgn * off_n + u * (s * k * step)
                b = _oriented_box(cpos[0], cpos[1], d[0], d[1], tw / 2 + P(0.3), th / 2 + P(0.2))
                bx = b.bounds
                # 室外的轴号引线和墙垂直：沿墙方向（u，不是字的方向——perp 时字垂直于墙）量字骑不骑在引线上
                on_leader = any(bx[0] - P(0.3) < a < bx[2] + P(0.3) for a in axis_x) if abs(u[0]) > abs(u[1]) \
                    else any(bx[1] - P(0.3) < a < bx[3] + P(0.3) for a in axis_y)
                bo = box_wide if fr.get("side") in ("top", "left") else box_out
                if (bo is None or bo.contains(b)) and not bld_in.intersects(b) and not on_leader \
                        and (cut_band is None or not cut_band.intersects(b)) \
                        and not len(hard_tree.query(b, predicate="intersects")) and not len(soft_hits(b)) and not hits_text(b) \
                        and not (use_res and hits_res(b)) and legal(el, own, cpos, dmax):
                    return (0, cpos, b, d)
        return None

    def put(el, best):
        _, cpos, b, d = best
        obj = text(drawing, float(cpos[0]), float(cpos[1]), "{{Tag}}", size=tag_size(el.Tag), z=z, rot=deg(d), register=False)
        api.drawing.assign_product(tool.Ifc.get(), relating_product=el, related_object=tool.Ifc.get_entity(obj))
        occ.append(b)
        tagged.append((el.Tag, b))

    def outside_zone(fr):
        """外墙门窗的引出字也可以写在室外：离开建筑 0.6mm、视图范围外扩 4mm 以内，不压内部切口和室外的轴号引线（轴线两侧 1mm）。"""
        if fr.get("out") is None:
            return None
        r, c = P(LEADER_NEAR_MM), fr["c"]
        zone = _box(c[0] - r, c[1] - r, c[0] + r, c[1] + r).difference(geo.building.buffer(P(0.6), join_style="mitre"))
        if box_out is not None:
            zone = zone.intersection(box_out)
        if cut_band is not None:
            zone = zone.difference(cut_band)
        axes = [_box(a - P(1.0), c[1] - r, a + P(1.0), c[1] + r) for a in axis_x if abs(a - c[0]) < r] \
            + [_box(c[0] - r, a - P(1.0), c[0] + r, a + P(1.0)) for a in axis_y if abs(a - c[1]) < r]
        if axes:
            zone = zone.difference(shapely.union_all(axes))
        return None if zone.is_empty else zone

    def leader(fr, el, tw, th, keep=(), clean=False):
        """引出线编号（_leader_tag）。keep：还没放的门窗最好的编号位置，引出的字或引线压上去加代价 LEADER_KEEP（先画的引出线
        不占别的门窗旁边的位置）；clean=True 只要字不压家具设备、不挨着别的门窗的位置。返回是否放上了。"""
        pen = lambda b, line: soft_pen(b) + room_pen(el, b) + LEADER_KEEP * sum(1 for k in keep if k.intersects(b) or k.intersects(line))
        ext = outside_zone(fr)
        b = _leader_tag(f, drawing, den, fr, el, tw, th, tag_size(el.Tag), z, allowed, hard, hard_tree, occ,
                        lambda b: other_pen(el, b), pen, fronts=[g for k, g in fronts.items() if k != el.id()], clean=clean, outside=ext,
                        closer=lambda b: other_pen(el, b) >= LEADER_CLOSER, same=[bx for t, bx in tagged if t == el.Tag], sym_tree=sym_tree)
        if b and ext is not None and not bld_in.intersects(b):   # 写在室外的引出字：尺寸数字要避开
            _OUTSIDE.setdefault(drawing.id(), []).append(b)
        if b:
            tagged.append((el.Tag, b))
        return bool(b)

    def over_symbol(el, pp):
        """不压纸面符号实在放不下：写在近处压符号最少的地方（记日志 tag over symbol）；还不行不写（tag skipped）。返回写了几个。"""
        fb = search(pp, P(TAG_FAR_MM), soft_ok=True, over_sym=True)
        if fb is None:
            log("tag skipped (no room)", el.Tag, el.Name)
            return 0
        put(el, fb)
        log("tag over symbol", el.Tag, el.Name)
        return 1

    placed = 0
    todo = [fr for fr in frames if fr["el"].id() in ids and fr["el"].Tag and fr["el"].id() in obox
            and (not bbox_m or (bbox_m[0] <= fr["c"][0] <= bbox_m[2] and bbox_m[1] <= fr["c"][1] <= bbox_m[3]))]
    todo = sorted(((room(pp), fr, pp) for fr, pp in ((fr, prep(fr)) for fr in todo)),
                  key=lambda t: (t[0], round(float(t[1]["c"][1]), 1), float(t[1]["c"][0])))
    later = []
    for i, (cnt, fr, pp) in enumerate(todo):
        el, tw, th = pp[0], pp[3], pp[4]
        out = fr.get("out") is not None
        # 窗的编号先不占预留的房名位置（门的编号本来就尽量写在小房间外，room_pen；挤到门外会占了别的房间的房名）。下、右两侧的
        # 窗（纸面上小的房间除外）不管它：挤到室外会和朝里写的尺寸数字排在一起
        ur = bool(res) and el.is_a("IfcWindow") and (fr.get("side") not in ("bottom", "right") or el.id() in yield_wins)
        best, how, outside = search(pp, P(TAG_NEAR_MM), use_res=ur), None, False
        # 下、右两侧的尺寸数字朝里写，编号放室外会和尺寸数字排在一起，只在室内放不下时才放；上、左两侧室内只能压房间名
        # 标注点等时也放室外
        if out and (best is None or (best[0] > 20 and fr.get("side") in ("top", "left")) or el.id() in yield_wins):
            found = search_outside(fr, pp, P(TAG_NEAR_MM), use_res=ur)
            if found is None and best is None and el.id() in yield_wins:   # 小房间的窗：室内、室外平行都写不下，垂直于墙写在窗外
                found = search_outside(fr, pp, P(TAG_NEAR_MM), perp=True, use_res=ur)
            # 窗外的编号沿墙偏出窗宽一半以上（大半贴着实墙，看不出是哪樘窗的）：房里写得下就写在房里（预留的房名位置也不管，
            # 房名后写时另找地方）
            if found and el.is_a("IfcWindow") and abs(float(np.dot(found[1] - fr["c"], pp[1]))) > fr["w"] / 2:
                inside = best if best is not None else (search(pp, P(TAG_NEAR_MM)) if ur else None)
                if inside is not None:
                    found, best = None, inside
                    log("tag inside (outside off the window)", el.Tag, el.Name)
            if found:
                best, how, outside = found, ("outside" if best is None else None), True
        if best is None and ur:   # 不占预留的房名位置时近处、室外都放不下：照常找（房名后写时另找地方）
            best = search(pp, P(TAG_NEAR_MM))
            if best is None and out:
                found = search_outside(fr, pp, P(TAG_NEAR_MM)) or (el.id() in yield_wins and search_outside(fr, pp, P(TAG_NEAR_MM), perp=True))
                if found:
                    best, how, outside = found, "outside", True
        if best is None:
            best, how = search(pp, P(TAG_FAR_MM)), "far"
        if best is None and out:
            best, how, outside = search_outside(fr, pp, P(TAG_FAR_MM)), "far outside", True
        if best is None:
            # 旁边放不下，画引出线：洞口旁边本来就没有合格位置的（cnt=0，排在最前面）马上画，免得洞口两侧的空地先被别的编号
            # 占满、只好隔墙写；只是被已写的字挤掉、或只能压家具设备的，等别的编号都放好了再画（引出的字不占别的门窗旁边的位置）
            if cnt == 0:
                ok = leader(fr, el, tw, th, keep=[first[t[1]["el"].id()].buffer(P(0.6), join_style="mitre")
                                                  for t in todo[i + 1:] if t[1]["el"].id() in first])
                placed += ok or over_symbol(el, pp)
            else:
                later.append((fr, pp))
            continue
        if how:
            log("tag " + how, el.Tag, el.Name)
        if outside:   # 尺寸数字要避开室外的编号
            _OUTSIDE.setdefault(drawing.id(), []).append(best[2])
        put(el, best)
        placed += 1
    for fr, pp in later:
        el, tw, th = pp[0], pp[3], pp[4]
        fb = search(pp, P(TAG_NEAR_MM), soft_ok=True) or search(pp, P(TAG_FAR_MM), soft_ok=True)
        if fb is not None:
            if leader(fr, el, tw, th, clean=True):
                placed += 1
                continue
            put(el, fb)
            log("tag over furniture", el.Tag, el.Name)
            placed += 1
        elif leader(fr, el, tw, th):
            placed += 1
        else:
            placed += over_symbol(el, pp)
    return placed


def _front_zone(fr):
    """门的"门前"（米）：开启范围（_opening_zone 里洞口以外的部分）+ 门洞两侧各一块通行区（宽同门洞，进深 FRONT_DEPTH_M）。
    引出的编号字不写在别的门的门前，免得读成那樘门的编号。"""
    c, u, n, w, t = fr["c"], fr["u"], fr["n"], fr["w"], fr["t"]
    dep = FRONT_DEPTH_M
    parts = _opening_zone(fr)[1:] + [_oriented_box(*(c + n * s * (t / 2 + dep / 2)), u[0], u[1], w / 2, dep / 2) for s in (1, -1)]
    return shapely.union_all(parts)


# 引出线编号的找位（图纸毫米）：先在洞口周围 LEADER_NEAR_MM 内找；找不到时在走道一侧放到 LEADER_FAR_MM，宁可引线长一点
LEADER_NEAR_MM, LEADER_FAR_MM = 25.0, 50.0
# 代价（按图纸毫米的距离计）：字离别的门窗 2mm 以内（有引线指着，比不引线的编号好认，罚得轻）；字离别的门窗比离自己的
# 洞口还近（强代价：宁可引线长 20mm；不做硬约束，1:150 核心筒里门挨着门，硬性要求会无处可放）；字悬在空地中间（上下都
# 还能挪 1mm，没贴着墙或别的图形，后面的字就不好放了）
LEADER_NEAR_OTHER, LEADER_CLOSER, LEADER_FLOAT = 4.0, 20.0, 4.0
LEADER_FRONT = 30.0  # 门挨着门、只能写在别的门的门前时（宁可引线长 30mm）
LEADER_KEEP = 40.0   # 先画的引出线编号（字或引线）占了后面的门窗最好的编号位置：宁可引线长 40mm
FRONT_DEPTH_M = 0.6  # 门前通行区（_front_zone）的进深（米）
TAG_LEADER_MM = 12.0  # 门窗编号的引线（折线全长，图纸毫米）不超过这么长；都放不下才放宽（记日志 tag leader long）


def _leader_tag(f, drawing, den, fr, el, tw, th, size, z, allowed, hard, hard_tree, occ, other_pen, pen_of, fronts=(), clean=False,
                outside=None, closer=None, same=(), sym_tree=None):
    """引出线编号（旁边放不下时）：编号写在附近的空地上，用 Bonsai 的 TEXT_LEADER 从字的一端引一条折线到洞口中点（_leader_path：
    顺着字的方向引出，再 45°/60°/90° 折向洞口，不回头穿过自己的字）。
    硬性要求：字在洞口两侧不隔墙的地方（_sides：不写进隔着墙的别的房间、井道里），不压墙、门窗和开启范围、已写的字
    （编号、尺寸数字、别的引线；房间名后放，会避开编号和引线），不写在别的门的门前（fronts，_front_zone），不和同号的编号
    （same）上下叠放（左右有重叠时上下隔一行 ROW_MM），引线不穿墙、不穿过已写的字和纸面符号（sym_tree），引线全长不超过 TAG_LEADER_MM；窗的编号
    离别的门窗比离自己的洞口近的位置不要（closer；门先找这样的位置，没有才把离别的门窗近计代价）。满足的位置在 0.5mm 网格上由近到远找，依次：
      1. 洞口周围 LEADER_NEAR_MM 内，不压家具和点位符号、不压房间名的标注点、离别的门窗不近（other_pen 为 0），最近的；
      2. 同样范围，压家具、标注点或离别的门窗近的计代价，取 代价 + 离洞口的距离 最小的（字贴着墙或别的图形放的优先，
         把整块空地留给后面的字；字或引线占了还没放的门窗最好的编号位置的，代价 LEADER_KEEP，见 pen_of）；
      3. 只在走道（公共交通空间）一侧找，放宽到 LEADER_FAR_MM；
      4. 竖着写（窄长的前室、走道，和竖向的门窗编号一样从下往上读）；
      5. 字和墙、别的字之间的空隙收紧到 0.1～0.3mm（窄小的井道门口）。
    clean=True（旁边只能压家具设备的编号）只试 1、3、4 的"干净"位置（引线可以放宽到不限长），找不到返回 None，由调用方
    写在近处。都放不下时依次放宽：引线不限长（画出来超过 TAG_LEADER_MM 的记日志 tag leader long）；门挨着门时（1:150 的
    核心筒）别的门的门前也可以写，计代价 LEADER_FRONT（记日志 tag leader in front of a door）；窗可以离别的门窗更近（计代价
    LEADER_CLOSER，记日志 tag leader closer to another opening）；可以和同号的编号上下相邻（记日志 tag leader stacked）。
    还不行才只避开墙、门窗和字，可能隔墙（记日志 tag leader crowded）。outside：外墙门窗室外一侧可以写字的地方
    （opening_tags 的 outside_zone），和洞口两侧一起找。返回字框（米），放不上返回 None。"""
    P = lambda mm: mm * den / 1000.0
    geo = plan_geometry(f)
    own = _opening_zone(fr)[0]
    c = fr["c"]
    sides = _sides(f, fr)
    near_reg = shapely.union_all([p for p, _ in sides] + ([outside] if outside is not None else [])) if sides or outside is not None else None
    allow = allowed if outside is None else allowed.union(outside)
    pub = [p for p, is_pub in sides if is_pub]
    far_reg = shapely.union_all(pub) if pub else near_reg
    same = [s.bounds for s in same]

    def stacked(b):
        """和同号的编号上下叠放：左右有重叠（竖写的字：上下有重叠），中间隔不到一行 ROW_MM。"""
        x0, y0, x1, y1 = b.bounds
        k = 1 if (x1 - x0) < (y1 - y0) else 0   # 竖写的字沿 y
        lo, hi = (x0, x1) if k == 0 else (y0, y1)
        a0, a1 = (y0, y1) if k == 0 else (x0, x1)
        for s in same:
            slo, shi = (s[0], s[2]) if k == 0 else (s[1], s[3])
            sa0, sa1 = (s[1], s[3]) if k == 0 else (s[0], s[2])
            if min(hi, shi) > max(lo, slo) and max(sa0 - a1, a0 - sa1) < P(ROW_MM):
                return True
        return False

    def ends(cpos, vert):
        """引线：p0 在字靠洞口的一端（横写：左端或右端；竖写：下端或上端），落点 p1 是洞口中点、在字那一侧的墙面上
        （圆点落在门洞里，不落在门边墙角被墙的涂黑盖住；凸窗取凸窗轮廓上离字端外侧最近的点）。返回 (first, 折线)，
        first=True：p0 是字的起点（字从 p0 往右、往上写）；洞口中点不在字端以外（_leader_path 折不出来）时折线为 None。"""
        k = 1 if vert else 0
        first = c[k] < cpos[k]
        out = np.zeros(2)
        out[k] = -1.0 if first else 1.0
        p0 = np.array(cpos, dtype=float) + out * (tw / 2)
        if fr["bay"]:
            q = shapely.ops.nearest_points(own, shapely.Point(*(p0 + out * P(LEADER_RUN_MM + 0.5))))[0]
            p1 = (q.x, q.y)
        else:
            s = 1.0 if float(np.dot(p0 - c, fr["n"])) >= 0 else -1.0
            q = c + fr["n"] * s * (fr["t"] / 2)
            p1 = (float(q[0]), float(q[1]))
        return first, _leader_path(p0, p1, out, den)

    def through(line, sym=True):
        """引线穿墙，或穿过已写的字（别的编号、尺寸数字）和别的引线，或（sym=True 时）穿过按纸面大小画的点位符号（sym_tree：
        笔画连同圈起来的地方，圆点也不落在上面）。"""
        if geo.crosses_wall(line, WALL_TOUCH_M):
            return True
        if sym and sym_tree is not None and len(sym_tree.query(line.buffer(P(0.1)), predicate="intersects")):
            return True
        band = line.buffer(P(0.3))
        return any(o.intersects(band) for o in occ)

    def search(rmax, region, strict, vert=False, pad=(0.4, 0.3), text_gap=0.6, cross=False, front="hard", long=False, closer_hard=False,
               stack=False, sym=True):
        """region 里离洞口 rmax（图纸毫米）以内的空地：字框（pad 外扩）不压墙、门窗和开启范围，离已写的字 text_gap 以上；
        别的门的门前（fronts）：front="hard" 不写，"cost" 写了加代价 LEADER_FRONT，None 不管。strict：第一个合格的（不压
        家具等、离别的门窗不近）就是；否则取 代价 + 距离 最小的。cross=True：引线可以穿墙、穿字。long=False：引线不超过
        TAG_LEADER_MM。closer_hard：离别的门窗比离自己近的位置不要。stack=False：不和同号的编号上下叠放。sym=False：引线
        可以穿过点位符号。"""
        if region is None:
            return None
        zone = region.intersection(allow).intersection(_box(c[0] - P(rmax), c[1] - P(rmax), c[0] + P(rmax), c[1] + P(rmax)))
        if zone.is_empty:
            return None
        obs = [hard[i] for i in hard_tree.query(zone)] + [o.buffer(P(text_gap), join_style="mitre") for o in occ if o.intersects(zone)]
        if front == "hard":
            obs += [g for g in fronts if g.intersects(zone)]
        near_fronts = [g for g in fronts if g.intersects(zone)] if front == "cost" else []
        free = zone.difference(shapely.union_all(obs)) if obs else zone
        shapely.prepare(free)
        hw, hh = (th / 2, tw / 2) if vert else (tw / 2, th / 2)
        hw, hh = hw + P(pad[0] if not vert else pad[1]), hh + P(pad[1] if not vert else pad[0])
        across = np.array([1.0, 0.0]) if vert else np.array([0.0, 1.0])   # 垂直于字的方向：两边都还能挪 1mm 的算悬空

        def fits(p):
            return free.contains(_box(p[0] - hw, p[1] - hh, p[0] + hw, p[1] + hh))
        best = None
        grid = P(0.25 if pad[1] <= 0.1 else 0.5)   # 收紧的几轮网格也加密，窄处差 0.1mm 也放得进
        for xs, ys, ds in _anchors(free, [(-hw, -hh, hw, hh)], float(c[0]), float(c[1]), grid):
            for x, y, d in zip(xs, ys, ds):
                r = d * 1000.0 / den
                if r > rmax or (best is not None and (strict or best[0] <= r)):
                    return best
                cpos = np.array([x, y])
                path = ends(cpos, vert)[1]
                if path is None:
                    continue
                line = LineString(path)
                if not long and line.length > P(TAG_LEADER_MM):
                    continue
                if not cross and through(line, sym):
                    continue
                b = _box(x - hw, y - hh, x + hw, y + hh)
                if (closer_hard and closer is not None and closer(b)) or (not stack and stacked(b)):
                    continue
                pen = pen_of(b, line) + other_pen(b) + (LEADER_FRONT if any(g.intersects(b) for g in near_fronts) else 0.0)
                if strict and pen:
                    continue
                if not strict and fits(cpos + across * P(1.0)) and fits(cpos - across * P(1.0)):
                    pen += LEADER_FLOAT
                if best is None or pen + r < best[0]:
                    best = (pen + r, cpos, b, vert)
                    if strict:
                        return best
        return best

    def attempt(front, **kw):
        """一轮找位：先找引线不穿过点位符号的，这一轮里没有才让引线穿过符号（记日志 tag leader over symbol；不为躲符号
        降到更宽的一轮）。"""
        got = attempt1(front, **kw)
        if got is None and sym_tree is not None:
            got = attempt1(front, sym=False, **kw)
            if got:
                log("tag leader over symbol", el.Tag, el.Name)
        return got

    def attempt1(front, **kw):
        """一轮找位（kw 传给 search）。front=False：不写在别的门前；True：门挨着门（1:150 的核心筒）时别的门的门前也可以写，
        计代价 LEADER_FRONT。"""
        if clean:
            return search(LEADER_NEAR_MM, near_reg, True, **kw) or search(LEADER_FAR_MM, far_reg, True, **kw) \
                or search(LEADER_NEAR_MM, near_reg, True, vert=True, **kw)
        if not front:
            return search(LEADER_NEAR_MM, near_reg, True, **kw) or search(LEADER_NEAR_MM, near_reg, False, **kw) \
                or search(LEADER_FAR_MM, far_reg, False, **kw) or search(LEADER_NEAR_MM, near_reg, False, vert=True, **kw) \
                or search(LEADER_NEAR_MM, near_reg, False, pad=(0.15, 0.1), text_gap=0.3, **kw)
        return search(LEADER_NEAR_MM, near_reg, False, front="cost", **kw) or search(LEADER_FAR_MM, far_reg, False, front="cost", **kw) \
            or search(LEADER_NEAR_MM, near_reg, False, vert=True, front="cost", **kw) \
            or search(LEADER_NEAR_MM, near_reg, False, pad=(0.15, 0.1), text_gap=0.3, front="cost", **kw) \
            or search(LEADER_NEAR_MM, near_reg, False, pad=(0.15, 0.1), text_gap=0.15, front="cost", **kw)

    win = el.is_a("IfcWindow")
    # 门：先找离别的门窗不比离自己近的位置（不写在别的门前、引线不超长），没有再照下面放宽（离别的门窗近只计代价）
    best = None if win else attempt(False, long=False, closer_hard=True, stack=False)
    # 依次放宽：引线不限长 → 别的门前 → 窗离别的门窗更近 → 和同号的编号上下相邻
    for front, long, relax, stack in ((False, False, False, False), (False, True, False, False), (True, False, False, False),
                                      (True, True, False, False), (True, True, True, False), (True, True, True, True)):
        if best or ((front or relax) and clean):
            break
        if relax and not stack and not win:
            continue
        best = attempt(front, long=long, closer_hard=win and not relax, stack=stack)
        if best:
            for w, on in (("tag leader in front of a door", front), ("tag leader closer to another opening", relax and not stack),
                          ("tag leader stacked", stack)):
                if on:
                    log(w, el.Tag, el.Name)
            break
    if best is None and clean:
        return None
    if best is None:   # 实在没有：只避开墙、门窗和字，引线可以穿墙，可以写在别的门前
        best = search(LEADER_NEAR_MM, allowed, False, cross=True, front=None, long=True, stack=True) \
            or search(LEADER_FAR_MM, allowed, False, cross=True, front=None, long=True, stack=True) \
            or search(LEADER_FAR_MM, allowed, False, pad=(0.15, 0.1), text_gap=0.3, cross=True, front=None, long=True, stack=True)
        if best:
            log("tag leader crowded", el.Tag, el.Name)
    if not best:
        return None
    _, cpos, b, vert = best
    first, path = ends(cpos, vert)
    if LineString(path).length > P(TAG_LEADER_MM):
        log("tag leader long", el.Tag, el.Name, round(LineString(path).length * 1000.0 / den, 1))
    obj = _leader(drawing, path, "{{Tag}}", size, first, z, rot=90.0 if vert else 0.0)
    api.drawing.assign_product(tool.Ifc.get(), relating_product=el, related_object=tool.Ifc.get_entity(obj))
    occ.append(b)
    log("tag leader", el.Tag, el.Name, "vertical" if vert else "")
    return b


LEADER_RUN_MM = 1.0       # 引线先顺着字的方向从字端引出至少这么长（横写的字水平、竖写的字竖直），再折向落点（图纸毫米）
LEADER_ANGLES = (45.0, 60.0)   # 折出的斜段和水平段的夹角，由先到后试，都不行用 90°（GB/T 50001：30°、45°、60°、90°）


def _leader_path(p0, q, out, den):
    """引线的折线（米）：从字端 p0 顺着 out（离开字的方向，单位向量）先引出一段，再按 LEADER_ANGLES 折向落点 q，都不行就
    引到和 q 齐平再 90° 折过去；q 差不多就在 out 方向上时是一条直线。q 在字端以外不到 LEADER_RUN_MM（在字的后面、正上方或
    正下方，引线会回头穿过自己的字）返回 None。返回 [p0, (折点,) q]。"""
    P = lambda mm: mm * den / 1000.0
    p0, q, out = np.asarray(p0, float), np.asarray(q, float), np.asarray(out, float)
    a, b = float(np.dot(q - p0, out)), float(np.dot(q - p0, (-out[1], out[0])))
    if a < P(LEADER_RUN_MM):
        return None
    if abs(b) < P(0.15):
        return [tuple(p0), tuple(q)]
    h = next((h for h in (a - abs(b) / math.tan(math.radians(t)) for t in LEADER_ANGLES) if h >= P(LEADER_RUN_MM)), a)
    return [tuple(p0), tuple(p0 + out * h), tuple(q)]


def _leader(drawing, pts_xy, literal, size, first, z=0.0, rot=0.0):
    """Bonsai 的 TEXT_LEADER：字在折线的第一点 p0（first=True 时 p0 是字的起点，否则是终点），线沿 pts_xy（米，_leader_path）
    画到最后一点，线端用约定包的 leader-marker（markers-CN.svg）。rot：字的转角（度，逆时针；竖写用 90，和竖向的 TEXT 编号
    一样从下往上读）——Bonsai 按对象的旋转定字的方向，所以对象转 rot，线的点按对象坐标写。引线登记进本视图的占位表，后放的字
    （房间名、尺寸数字）不压它。返回标注对象。"""
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="TEXT_LEADER", relating_type=None)
    obj.matrix_world = Matrix.Rotation(math.radians(rot), 4, "Z")
    inv = obj.matrix_world.inverted()
    sp = obj.data.splines[0]
    bez = bool(sp.bezier_points)
    pts = sp.bezier_points if bez else sp.points
    if len(pts) < len(pts_xy):
        pts.add(len(pts_xy) - len(pts))
    for p, xy in zip(pts, pts_xy):
        px, py, pz = inv @ Vector((xy[0], xy[1], z))
        p.co = (px, py, pz) if bez else (px, py, pz, 1.0)
        if bez:
            p.handle_left_type = p.handle_right_type = "VECTOR"
    commit(obj)
    lit = tool.Drawing.get_text_literal(obj)
    lit.Literal = literal
    if hasattr(lit, "BoxAlignment"):
        lit.BoxAlignment = "middle-left" if first else "middle-right"
    _apply_size(obj, size)
    _OCC.setdefault(drawing.id(), []).append(LineString(pts_xy).buffer(_paper(0.3, _den_of(drawing))))
    return obj


_CELLS: dict[int, tuple] = {}


def _cells(f):
    """楼里互相不隔墙的一块块地方（米）：建筑轮廓减去墙（外扩 3cm，合上墙段之间的细缝）和每樘门窗的洞口。同一块里的
    两处之间没有墙（开敞相连的客厅、餐厅、过道是一块）。返回 (各块, STRtree)，每个 IFC 文件算一次。"""
    k = id(f)
    if k not in _CELLS:
        geo = plan_geometry(f)
        frames = [fr for fr in (geo.frame(el) for el in geo.openings) if fr is not None]
        cut = shapely.union_all([p.buffer(0.03, join_style="mitre") for p in geo.wall_polys] + [_opening_zone(fr)[0] for fr in frames])
        rest = geo.building.difference(cut)
        parts = [g for g in getattr(rest, "geoms", [rest]) if g.area > 0.05]
        _CELLS[k] = (parts, shapely.STRtree(parts))
    return _CELLS[k]


def _cell_of(f, shape):
    """和 shape（米）重叠最多的那一块（_cells），一块都不挨着返回 None。点位符号贴着墙面，用它找符号所在的房间。"""
    parts, tree = _cells(f)
    near = [parts[i] for i in tree.query(shape, predicate="intersects")]
    return max(near, key=lambda g: g.intersection(shape).area) if near else None


def _cell_at(f, p):
    """点 p（米）所在的那一块（_cells），不在楼里返回 None。"""
    parts, tree = _cells(f)
    q = shapely.Point(float(p[0]), float(p[1]))
    hit = [parts[i] for i in tree.query(q, predicate="intersects")]
    return hit[0] if hit else None


def _sides(f, fr):
    """门窗两侧的地方（_cells 里洞口两边各探 10cm 落在的那块）：[(区域, 是否走道等公共交通空间)]；室外一侧没有。"""
    geo = plan_geometry(f)
    pts = [fr["c"] + fr["n"] * 0.1] if fr["bay"] else [fr["c"] + fr["n"] * s * (fr["t"] / 2 + 0.1) for s in (1, -1)]
    out = []
    for p in pts:
        cell = _cell_at(f, p)
        if cell is None or any(cell is g for g, _ in out):
            continue
        sp = geo.space_at(shapely.Point(float(p[0]), float(p[1])))
        out.append((cell, sp is not None and _public(geo, sp)))
    return out
