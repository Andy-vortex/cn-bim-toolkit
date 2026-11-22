"""中国家装符号库：把 packs/cn/drawing/symbols.json 里的二维符号做成 IFC 类型（IfcXxxType）。

每个类型带两种表达：
  - Model/Body/MODEL_VIEW：简单的盒子，给三维渲染和碰撞用；
  - Plan/Body/<视图>：二维符号。Bonsai 出平面（或顶面）图时会优先用 Plan/Body，
    于是图上画的是国标习惯的符号，而不是盒子的投影。
构件实例通过 ifcopenshell.api.type.assign_type 关联类型，自动继承这些表达。

注意单位：geometry.add_mesh_representation 用 SI（米）；我们手工构造的二维曲线用项目单位（毫米）。
"""
from __future__ import annotations

import importlib
import json
import math

import ifcopenshell
import ifcopenshell.api as api

for _m in ("root", "context", "geometry", "type", "spatial"):
    importlib.import_module("ifcopenshell.api." + _m)

M = 0.001  # mm -> m
# 图上标安装高度（H300）的点位：图例写类型的默认高度，图上只标和它不同的（drawset.add_legend、annotate.point_heights）
HEIGHT_CLASSES = ("IfcOutlet", "IfcSwitchingDevice")


def load_pack(path: str) -> dict:
    """符号库（symbols.json 的 types）。符号键不能以变体后缀（VARIANTS：_m、_r、_l）结尾：出图时变体靠后缀认（base_key），
    这样的键会被误拆成别的符号，读到就报错。"""
    with open(path, encoding="utf-8") as fh:
        types = json.load(fh)["types"]
    bad = sorted(k for k in types if base_key(k) != k)
    if bad:
        raise ValueError(f"符号键不能以 {'、'.join(s for s in VARIANTS if s)} 结尾（出图时是变体后缀）：{'、'.join(bad)}（{path}）")
    return types


def default_heights(path: str) -> dict[str, float]:
    """符号键 → 默认安装高度（毫米，symbols.json 的 default_height）：图例和 point_heights 读的是同一份。"""
    return {k: s["default_height"] for k, s in load_pack(path).items() if "default_height" in s}


def paper_scales(path: str) -> dict[str, float]:
    """符号键 → paper_scale（symbols.json）：这些点位符号在任何比例下都按 1:paper_scale 时的纸面大小画。"""
    return {k: s["paper_scale"] for k, s in load_pack(path).items() if s.get("paper_scale")}


# 按纸面大小画的点位符号的变体（符号键后缀）：原样、左右镜像（开关的斜线朝另一侧）、整个画到插入点一侧（贴着墙角的点位，
# 居中画会压进相邻的墙）。Bonsai 的 SYMBOL 只认转角、不认镜像，所以镜像和挪到一侧都做成另一个定义（drawset._paper_symbols 选）
VARIANTS = ("", "_m", "_r", "_l", "_m_r", "_m_l")


def base_key(key: str) -> str:
    """符号变体的键（cn_switch_1_m）→ 符号库的键（cn_switch_1）。"""
    for suf in sorted(VARIANTS, key=len, reverse=True):
        if suf and key.endswith(suf):
            return key[:-len(suf)]
    return key


def variant_polylines(spec: dict, suffix: str = "") -> list[list[tuple[float, float]]]:
    """符号变体的折线（实际毫米，局部坐标）：_m 左右镜像（x → -x）；_r、_l 整个平移到插入点的 +x、-x 一侧（最左、最右的笔画
    正好过插入点）。"""
    pls = [pts for it in spec["items"] for pts in item_polylines(it)]
    if suffix.startswith("_m"):
        pls = [[(-x, y) for x, y in pl] for pl in pls]
    if suffix.endswith(("_r", "_l")):
        xs = [x for pl in pls for x, _ in pl]
        dx = -min(xs) if suffix.endswith("_r") else -max(xs)
        pls = [[(x + dx, y) for x, y in pl] for pl in pls]
    return pls


def variants(spec: dict) -> list[str]:
    """这个符号有哪些变体（后缀）：左右对称的符号（镜像后每段线都还在）不要镜像的那几个。"""
    def segs(pls):
        r = lambda p: (round(p[0], 1) + 0.0, round(p[1], 1) + 0.0)
        return sorted(tuple(sorted((r(a), r(b)))) for pl in pls for a, b in zip(pl, pl[1:]))
    sym = segs(variant_polylines(spec)) == segs(variant_polylines(spec, "_m"))
    return [s for s in VARIANTS if not (sym and s.startswith("_m"))]


def svg_defs(pack: dict) -> list[str]:
    """有 paper_scale 的符号做成 Bonsai symbols.svg 的条目 <g id="符号键">（纸面毫米，y 朝下），出图时的 SYMBOL 标注
    （drawset.add_view）用 <use> 引用；每个符号连同它的变体（VARIANTS：镜像、画到插入点一侧）各一条。和 Bonsai 的模型线一样
    每段一条线，线型由约定包 cn.css 里同名的 IFC 类（.IfcOutlet 等）决定：和 1:paper_scale 的模型线、图例是同一份图形。"""
    out = []
    for key, spec in pack.items():
        d = spec.get("paper_scale")
        if not d:
            continue
        cls = spec["ifc_type"][:-4]
        for suf in variants(spec):
            segs = [(a, b) for pts in variant_polylines(spec, suf) for a, b in zip(pts, pts[1:])]
            path = " ".join(f"M {a[0] / d:.4f},{-a[1] / d:.4f} L {b[0] / d:.4f},{-b[1] / d:.4f}" for a, b in segs)
            out.append(f'<g id="{key}{suf}" class="{cls}"><path class="{cls}" d="{path}"/></g>')
    return out


def _arc_pts(cx, cy, r, a0, a1, step_deg=10.0):
    n = max(2, int(abs(a1 - a0) / step_deg))
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def _ellipse_pts(cx, cy, rx, ry, n=36):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def item_polylines(item: dict) -> list[list[tuple[float, float]]]:
    """把 json 里的一条图元转成若干条折线（毫米）。"""
    if "polyline" in item:
        return [[tuple(p) for p in item["polyline"]]]
    if "circle" in item:
        cx, cy, r = item["circle"]
        return [_arc_pts(cx, cy, r, 0, 360)]
    if "arc" in item:
        return [_arc_pts(*item["arc"])]
    if "ellipse" in item:
        return [_ellipse_pts(*item["ellipse"])]
    raise ValueError(f"unknown symbol item {item}")


def get_or_add_context(f, context_type, identifier, target_view):
    for c in f.by_type("IfcGeometricRepresentationSubContext"):
        if c.ContextType == context_type and c.ContextIdentifier == identifier and c.TargetView == target_view:
            return c
    parent = next(c for c in f.by_type("IfcGeometricRepresentationContext", include_subtypes=False)
                  if c.ContextType == context_type)
    return api.context.add_context(f, context_type=context_type, context_identifier=identifier,
                                   target_view=target_view, parent=parent)


def _box_rep(f, body_ctx, spec):
    dx, dy, dz = spec["body"]
    if spec.get("body_centered"):
        ox, oy, oz = -dx / 2, -dy / 2, 0
    elif "body_offset" in spec:
        ox, oy, oz = spec["body_offset"]
    else:
        ox, oy, oz = -dx / 2, 0, 0
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz), (ox, oy + dy, oz + dz)]
    v = [tuple(c * M for c in p) for p in v]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return api.geometry.add_mesh_representation(f, context=body_ctx, vertices=[v], faces=[faces])


def _symbol_rep(f, ctx, spec):
    curves = []
    for it in spec["items"]:
        for pts in item_polylines(it):
            curves.append(f.createIfcPolyline([f.createIfcCartesianPoint([float(x), float(y)]) for x, y in pts]))
    return f.createIfcShapeRepresentation(ctx, ctx.ContextIdentifier, "Curve2D", curves)


def create_types(f, pack: dict) -> dict[str, ifcopenshell.entity_instance]:
    """为符号库里每一项建一个 IFC 类型；返回 {符号键: 类型实体}。"""
    body = get_or_add_context(f, "Model", "Body", "MODEL_VIEW")
    types = {}
    for key, spec in pack.items():
        t = api.root.create_entity(f, ifc_class=spec["ifc_type"], name=spec["name"], predefined_type=spec["predefined"])
        t.Description = spec.get("legend")
        t.Tag = key
        api.geometry.assign_representation(f, product=t, representation=_box_rep(f, body, spec))
        for view in spec.get("views", ["PLAN_VIEW"]):
            ctx = get_or_add_context(f, "Plan", "Body", view)
            api.geometry.assign_representation(f, product=t, representation=_symbol_rep(f, ctx, spec))
        types[key] = t
    return types


def place_occurrence(f, types, key, name, x, y, z=None, rot_deg=0.0, container=None):
    """放一个构件实例：(x, y, z) 毫米，rot_deg 让符号局部 +y 指向室内。"""
    import numpy as np
    t = types[key]
    occ_class = t.is_a()[:-4]  # IfcOutletType -> IfcOutlet
    e = api.root.create_entity(f, ifc_class=occ_class, name=name, predefined_type=t.PredefinedType)
    api.type.assign_type(f, related_objects=[e], relating_type=t)
    if z is None:
        z = 0.0
    a = math.radians(rot_deg)
    m = np.array([[math.cos(a), -math.sin(a), 0, x * M], [math.sin(a), math.cos(a), 0, y * M],
                  [0, 0, 1, z * M], [0, 0, 0, 1]], dtype=float)
    api.geometry.edit_object_placement(f, product=e, matrix=m)
    if container is not None:
        api.spatial.assign_container(f, relating_structure=container, products=[e])
    return e
