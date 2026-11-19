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


def load_pack(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["types"]


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
