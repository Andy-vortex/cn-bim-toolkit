"""自动标注（在 Blender + Bonsai 里运行）：从 IFC 几何算出国内家装平面图的常用标注。

- 三道尺寸（国标三道）：由里到外依次是 洞口/墙垛（细部）、开间/隔墙定位、总尺寸；四个方向各算一次，
  与外一道完全相同的就省掉；
- 房间名 + 净面积（取 IfcSpace 的平面包围框，当前只适用于矩形房间）；
- 水电点位旁标注安装高度 H=xxx；
- 顶面图：每个房间标吊顶名称和标高（米，三位小数），没有吊顶的写"原顶"。

当前版本假设墙体正交（横平竖直），用构件的世界坐标包围框做计算；斜墙、弧墙以后用 shapely 做通用版。
所有偏移量按"图纸上的毫米"给出，再按比例换算成实际尺寸。
"""
from __future__ import annotations

import json
import math

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as uel
from mathutils import Matrix

import bonsai.tool as tool
import bonsai.core.drawing as core

_GS = ifcopenshell.geom.settings()
_GS.set("use-world-coords", True)
EPS = 0.02  # 米


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
    return obj


def polyline(drawing, pts, z=0.0):
    """一条线（Bonsai 的 LINEWORK 标注是网格对象：写顶点和边），点为米。"""
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="LINEWORK", relating_type=None)
    obj.matrix_world = Matrix.Identity(4)
    me = obj.data
    me.clear_geometry()
    me.from_pydata([(x, y, z) for x, y in pts], [(i, i + 1) for i in range(len(pts) - 1)], [])
    me.update()
    return obj


def text(drawing, x, y, literal, size=2.5, z=0.0, align="center"):
    """size 用 Bonsai 的字号档：1.8 small / 2.5 regular / 3.5 large / 5.0 header / 7.0 title（实际大小由 cn.css 决定）。"""
    obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type="TEXT", relating_type=None)
    obj.location = (x, y, z)
    lit = tool.Drawing.get_text_literal(obj)
    lit.Literal = literal
    if hasattr(lit, "BoxAlignment"):
        lit.BoxAlignment = align
    if size != 2.5:
        tool.Drawing.edit_text_font_size(obj, size)
    return obj


# ---------------------------------------------------------------- 三道尺寸
def _side_specs(X0, Y0, X1, Y1):
    # (名字, 沿边的轴, 边线位置, 向外的符号)
    return [("S", "x", Y0, -1), ("N", "x", Y1, +1), ("W", "y", X0, -1), ("E", "y", X1, +1)]


def three_chains(f, drawing, den, sides=("S", "N", "W", "E")):
    walls = [(w, bbox(w)) for w in f.by_type("IfcWall")]
    X0 = min(b[0] for _, b in walls); Y0 = min(b[1] for _, b in walls)
    X1 = max(b[3] for _, b in walls); Y1 = max(b[4] for _, b in walls)
    openings = [bbox(o) for o in f.by_type("IfcOpeningElement")]
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
        # 细部：洞口两边
        detail = {lo, hi}
        for b in openings:
            across = (b[1], b[4]) if axis == "x" else (b[0], b[3])
            along = (b[0], b[3]) if axis == "x" else (b[1], b[4])
            if across[0] < hi_band - EPS and across[1] > lo_band + EPS and (along[1] - along[0]) > (across[1] - across[0]) - EPS:
                detail.update(along)
        # 轴线（开间/进深）：与这一侧外墙相接、且垂直于这一侧的墙（含两端外墙）的中线
        axes = set()
        for _, b in walls:
            across = (b[1], b[4]) if axis == "x" else (b[0], b[3])
            along = (b[0], b[3]) if axis == "x" else (b[1], b[4])
            perpendicular = (along[1] - along[0]) < (across[1] - across[0])
            touches = across[0] - EPS <= hi_band and across[1] + EPS >= lo_band
            if perpendicular and touches:
                axes.add(round((along[0] + along[1]) / 2, 4))
        chains = [sorted(detail), sorted(axes), [lo, hi]]
        chains = [c for c in chains if len(c) >= 2]
        # 去重：与外一道相同就不画
        kept = []
        for i, c in enumerate(chains):
            if any(len(c) == len(o) and all(abs(a - b) < 1e-4 for a, b in zip(c, o)) for o in chains[i + 1:]):
                continue
            kept.append(c)
        off = _paper(10, den)
        for c in kept:
            pos = edge + outward * off
            pts = [(v, pos) if axis == "x" else (pos, v) for v in c]
            dim_chain(drawing, pts)
            made += 1
            off += _paper(8, den)
    return made


# ---------------------------------------------------------------- 房间名、点位高度、吊顶
def room_labels(f, drawing, den, with_area=True, size=3.5):
    n = 0
    for sp in f.by_type("IfcSpace"):
        b = bbox(sp)
        cx, cy = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
        name = sp.LongName or sp.Name
        if with_area:
            area = (b[3] - b[0]) * (b[4] - b[1])
            text(drawing, cx, cy + _paper(3.0, den), name, size=size)
            text(drawing, cx, cy - _paper(3.5, den), f"{area:.2f}㎡", size=2.5)
        else:
            text(drawing, cx, cy, name, size=size)
        n += 1
    return n


def point_heights(f, drawing, den, classes=("IfcOutlet", "IfcSwitchingDevice")):
    """在每个点位符号外侧写 H=安装高度（毫米）。"""
    import ifcopenshell.util.placement as up
    n = 0
    for cls in classes:
        for e in f.by_type(cls):
            m = up.get_local_placement(e.ObjectPlacement)  # 项目单位（毫米）
            x, y, z = m[0][3], m[1][3], m[2][3]
            ny_x, ny_y = m[0][1], m[1][1]  # 局部 +y（指向室内）
            d = 520.0
            text(drawing, (x + ny_x * d) / 1000.0, (y + ny_y * d) / 1000.0, f"H{int(round(z))}", size=1.8)
            n += 1
    return n


def ceiling_notes(f, drawing, den, storey_height_m=2.8):
    coverings = [(c, bbox(c)) for c in f.by_type("IfcCovering") if c.PredefinedType == "CEILING"]
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
        text(drawing, cx, cy - _paper(6, den), label, size=2.5, z=z)
        text(drawing, cx, cy - _paper(11, den), f"标高 +{z:.3f}", size=2.5, z=z)
        n += 1
    return n


# ---------------------------------------------------------------- 按 JZ_Space 的标注点写房间名
# 小比例总图用的简称；项目可以传自己的表（abbr=…）覆盖或补充
ABBR = {"主卧": "卧", "次卧": "卧", "卧室": "卧", "书房": "书", "客厅": "客", "餐厅": "餐", "客厅/餐厅": "客餐", "餐厅/客厅": "客餐",
        "起居室": "起", "厨房": "厨", "主卫": "卫", "公卫": "卫", "卫生间": "卫", "阳台": "阳台", "储藏": "储", "衣帽间": "衣",
        "玄关": "玄", "前室": "前室", "通道": "通道", "走道": "走道", "楼梯": "梯", "电梯": "梯", "消防电梯": "消梯", "无障碍电梯": "梯"}
# 中比例图上嫌长的名字换成短一点的
SHORT = {"无障碍电梯": "电梯", "消防电梯": "消防梯"}


def space_labels(f, drawing, den, mode="full", bbox_m=None, z=0.0, abbr=None, short=None):
    """mode="full"：用途 +（床位）+ 面积（1:100～1:150）；mode="abbr"：简称 +（床位数）（1:200 以下的总图）。
    标注点取 JZ_Space.LabelX/Y_mm（方案阶段用 polylabel 算好的房间内部点），没有就取包围框中心。
    abbr、short：项目自己的简称表，和默认表合并（项目的优先）。"""
    ABBR_ = dict(ABBR, **(abbr or {}))
    SHORT_ = dict(SHORT, **(short or {}))
    n = 0
    for sp in f.by_type("IfcSpace"):
        ps = uel.get_psets(sp).get("JZ_Space", {})
        usage = ps.get("Usage") or sp.LongName or sp.Name
        area = ps.get("NetArea_m2") or 0.0
        beds = ps.get("BedCount") or 0
        if "LabelX_mm" in ps:
            x, y = ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0
        else:
            b = bbox(sp); x, y = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
        if bbox_m and not (bbox_m[0] <= x <= bbox_m[2] and bbox_m[1] <= y <= bbox_m[3]):
            continue
        if usage in ("管井", "未标注") or area < 1.6:   # 管井、没标名的空间不写字
            continue
        if mode == "abbr":
            text(drawing, x, y, ABBR_.get(usage, usage[:2]) + (str(beds) if beds else ""), size=1.8, z=z)
        else:
            name = SHORT_.get(usage, usage) + (f" {beds}床" if beds else "")
            if area < 5.5 or ps.get("Public"):   # 小房间、公共交通空间：只写名字，字小一号
                text(drawing, x, y, name, size=1.8, z=z)
            else:
                text(drawing, x, y + _paper(2.2, den), name, size=2.5, z=z)
                text(drawing, x, y - _paper(2.6, den), f"{area:.2f}㎡", size=1.8, z=z)
        n += 1
    return n


# ---------------------------------------------------------------- 轴网：轴号圈 + 轴线尺寸 + 总尺寸（读 IfcGrid）
def grid_axes(f):
    """IfcGrid → ([(轴号, x米, 端)], [(轴号, y米, 端)])；端 = 原图轴号放在哪一侧（JZ_Grid.BubbleEnds）。"""
    import ifcopenshell.util.placement as up
    import ifcopenshell.util.unit as uu
    gs = f.by_type("IfcGrid")
    if not gs:
        return [], []
    g = gs[0]
    sc = uu.calculate_unit_scale(f)
    m = up.get_local_placement(g.ObjectPlacement)
    ends = json.loads(uel.get_psets(g).get("JZ_Grid", {}).get("BubbleEnds", "{}") or "{}")
    X = [(a.AxisTag, (a.AxisCurve.Points[0].Coordinates[0] + m[0][3]) * sc, ends.get("x", {}).get(a.AxisTag, ["bottom", "top"])) for a in g.UAxes]
    Y = [(a.AxisTag, (a.AxisCurve.Points[0].Coordinates[1] + m[1][3]) * sc, ends.get("y", {}).get(a.AxisTag, ["left", "right"])) for a in g.VAxes]
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


def grid_annotations(f, drawing, den, edge, z=0.0, mode="block", spans=None):
    """edge = 建筑外包（米，x0 y0 x1 y1）。
    mode="block"：下/左两道（轴线尺寸、总尺寸），上/右一道轴线尺寸；轴号按原图放在哪一侧就画在哪一侧，用引线连到建筑边。
    mode="overall"：spans = [(x0, x1), ...] 各单元范围，下边标各单元首末轴 + 总尺寸，左边标总尺寸；只画这些轴的轴号。"""
    X, Y = grid_axes(f)
    if not X:
        return 0
    P = lambda mm: _paper(mm, den)
    bx0, by0, bx1, by1 = edge
    R = 4.0
    tol = 0.35
    xs = [a for a in X if bx0 - tol <= a[1] <= bx1 + tol]
    ys = [a for a in Y if by0 - tol <= a[1] <= by1 + tol]
    n = 0

    def put(axis_list, side, chain_mm, bubble_mm):
        """画轴号：挤在一起的轴号沿边错开（保持顺序、整体居中），引线在圈前 2mm 处折过去。"""
        nonlocal n
        axis_list = sorted(axis_list, key=lambda a: a[1])
        bpos = _spread([a[1] for a in axis_list], P(2 * R + 0.8))
        k = P(bubble_mm - R - 2.0)   # 折点
        for (t, v, e), b in zip(axis_list, bpos):
            if side == "bottom":
                polyline(drawing, [(v, by0 - P(1.5)), (v, by0 - k), (b, by0 - P(bubble_mm - R))], z); bubble(drawing, b, by0 - P(bubble_mm), t, den, z, R)
            elif side == "top":
                polyline(drawing, [(v, by1 + P(1.5)), (v, by1 + k), (b, by1 + P(bubble_mm - R))], z); bubble(drawing, b, by1 + P(bubble_mm), t, den, z, R)
            elif side == "left":
                polyline(drawing, [(bx0 - P(1.5), v), (bx0 - k, v), (bx0 - P(bubble_mm - R), b)], z); bubble(drawing, bx0 - P(bubble_mm), b, t, den, z, R)
            else:
                polyline(drawing, [(bx1 + P(1.5), v), (bx1 + k, v), (bx1 + P(bubble_mm - R), b)], z); bubble(drawing, bx1 + P(bubble_mm), b, t, den, z, R)
            n += 1

    if mode == "overall":
        keep = []
        for s0, s1 in spans or [(bx0, bx1)]:
            inside = [a for a in xs if s0 - tol <= a[1] <= s1 + tol]
            if inside:
                keep += [inside[0], inside[-1]]
        keep = sorted({a[1]: a for a in keep}.values(), key=lambda a: a[1])
        if len(keep) >= 2:
            if len(keep) > 2:   # 只有首末两根轴时，这一道和总尺寸重复，省掉
                dim_chain(drawing, [(a[1], by0 - P(6)) for a in keep], z)
            dim_chain(drawing, [(keep[0][1], by0 - P(12)), (keep[-1][1], by0 - P(12))], z); n += 2
            put(keep, "bottom", 6, 12 + 5 + R)
        if len(ys) >= 2:
            dim_chain(drawing, [(bx0 - P(6), ys[0][1]), (bx0 - P(6), ys[-1][1])], z); n += 1
            put([ys[0], ys[-1]], "left", 6, 6 + 5 + R)
        return n

    for side, axis_list, is_x in (("bottom", xs, True), ("top", xs, True), ("left", ys, False), ("right", ys, False)):
        sel = [a for a in axis_list if side in a[2]]
        two = side in ("bottom", "left")
        if len(sel) >= 2:
            if is_x:
                yy = by0 - P(6) if side == "bottom" else by1 + P(6)
                dim_chain(drawing, [(a[1], yy) for a in sel], z)
            else:
                xx = bx0 - P(6) if side == "left" else bx1 + P(6)
                dim_chain(drawing, [(xx, a[1]) for a in sel], z)
            n += 1
        if two and len(axis_list) >= 2:
            lo, hi = axis_list[0][1], axis_list[-1][1]
            if is_x:
                dim_chain(drawing, [(lo, by0 - P(12)), (hi, by0 - P(12))], z)
            else:
                dim_chain(drawing, [(bx0 - P(12), lo), (bx0 - P(12), hi)], z)
            n += 1
        put(sel, side, 6, (12 if two else 6) + 5 + R)
    return n
