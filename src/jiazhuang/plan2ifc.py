"""识别结果（dxfplan）+ 方案（scheme：房间用途、分区、床和设施点位、拆改后的几何）→ IFC4。
在 Blender 里用 Bonsai 自带的 ifcopenshell 运行（不用 bpy）。没有方案时用 scheme.from_recognition() 原样建模。

- 坐标：图纸坐标（mm）减去原点偏移 (OX, OY) 再写入；偏移量记在 IfcSite 的 JZ_Source 属性集里，可以追溯回原图。
- 墙、房间（IfcSpace）、凸窗：多边形拉伸（IfcArbitraryClosedProfileDef + IfcExtrudedAreaSolid，项目单位毫米）。
- 普通门窗：ifcopenshell 参数化门窗（项目单位毫米），不挖洞——识别出的墙段本来就在洞口处断开。
- 床和设施：约定包的符号类型（symbols.json），实例放进所在的 IfcSpace。
- 一个标准层代表多层时：楼层放在第一个标高上，全部标高写在 JZ_Storey.Levels_m 里，规范检查按最高标高判。
"""
from __future__ import annotations

import importlib
import json
import math
import os

import numpy as np
import ifcopenshell
import ifcopenshell.api as api

from . import symbols as sym

for _m in ("project", "root", "unit", "context", "aggregate", "geometry", "spatial", "material", "pset", "type"):
    importlib.import_module("ifcopenshell.api." + _m)

M = 0.001
STOREY_H = 3000.0
SPACE_H = 2800.0
DOOR_H = 2100.0
WIN_SILL, WIN_H = 900.0, 1500.0


def _matrix(x, y, z, rot_deg):
    a = math.radians(rot_deg)
    return np.array([[math.cos(a), -math.sin(a), 0, x * M], [math.sin(a), math.cos(a), 0, y * M],
                     [0, 0, 1, z * M], [0, 0, 0, 1]], dtype=float)


def _ring(pts, ox, oy):
    """去掉闭合点和重复点，转成逆时针，减偏移。"""
    out = []
    for x, y in pts:
        p = (round(x - ox, 1), round(y - oy, 1))
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    area2 = sum(out[i][0] * out[(i + 1) % len(out)][1] - out[(i + 1) % len(out)][0] * out[i][1] for i in range(len(out)))
    return out if area2 > 0 else out[::-1]


def _prism(f, ctx, ring, z0, h):
    cps = [f.createIfcCartesianPoint((float(x), float(y))) for x, y in ring]
    curve = f.createIfcPolyline(cps + [cps[0]])
    prof = f.createIfcArbitraryClosedProfileDef("AREA", None, curve)
    pos = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, float(z0))), None, None)
    solid = f.createIfcExtrudedAreaSolid(prof, pos, f.createIfcDirection((0.0, 0.0, 1.0)), float(h))
    return f.createIfcShapeRepresentation(ctx, "Body", "SweptSolid", [solid])


def _curve2d(f, ctx, rings):
    curves = [f.createIfcPolyline([f.createIfcCartesianPoint((float(x), float(y))) for x, y in list(r) + [r[0]]]) for r in rings]
    return f.createIfcShapeRepresentation(ctx, ctx.ContextIdentifier, "Curve2D", curves)


def _pset(f, product, name, props):
    p = api.pset.add_pset(f, product=product, name=name)
    api.pset.edit_pset(f, pset=p, properties=props)


def build(rec: dict, sch: dict, pack_symbols: str, out_path: str, project_name: str, building_name: str = "楼栋") -> dict:
    rec = sch.get("after") or rec          # 有拆改时用拆改后的几何
    f = api.project.create_file(version="IFC4")
    project = api.root.create_entity(f, ifc_class="IfcProject", name=project_name)
    api.unit.assign_unit(f)  # 毫米
    model = api.context.add_context(f, context_type="Model")
    body = api.context.add_context(f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model)
    plan = api.context.add_context(f, context_type="Plan")
    plan_body = api.context.add_context(f, context_type="Plan", context_identifier="Body", target_view="PLAN_VIEW", parent=plan)
    api.context.add_context(f, context_type="Plan", context_identifier="Annotation", target_view="PLAN_VIEW", parent=plan)

    levels = sch.get("levels") or rec.get("levels") or [0.0]
    z0 = levels[0] * 1000.0
    xs = [p[0] for g in rec["footprint"] for p in g]; ys = [p[1] for g in rec["footprint"] for p in g]
    OX, OY = math.floor(min(xs) / 1000.0) * 1000.0 - 2000.0, math.floor(min(ys) / 1000.0) * 1000.0 - 2000.0

    site = api.root.create_entity(f, ifc_class="IfcSite", name="场地")
    bldg = api.root.create_entity(f, ifc_class="IfcBuilding", name=building_name)
    storey = api.root.create_entity(f, ifc_class="IfcBuildingStorey", name=f"标准层（{len(levels)} 层共用）")
    storey.Elevation = z0
    api.aggregate.assign_object(f, relating_object=project, products=[site])
    api.aggregate.assign_object(f, relating_object=site, products=[bldg])
    api.aggregate.assign_object(f, relating_object=bldg, products=[storey])
    api.geometry.edit_object_placement(f, product=site)
    api.geometry.edit_object_placement(f, product=bldg)
    api.geometry.edit_object_placement(f, product=storey, matrix=_matrix(0, 0, z0, 0))
    _pset(f, site, "JZ_Source", {"Drawing": os.path.basename(rec["source"]), "OffsetX_mm": OX, "OffsetY_mm": OY,
                                 "Region": ",".join(str(round(v)) for v in rec["region"]),
                                 "DeclaredFloorArea_m2": float(rec.get("declared_area_m2") or 0.0),
                                 "FootprintArea_m2": float(rec["footprint_area_m2"])})
    # 方案的设计假设原样写进楼栋的 JZ_Scheme（键值都转成文字），检查报告会列出来
    a = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in (sch.get("assumptions") or {}).items()}
    if a:
        _pset(f, bldg, "JZ_Scheme", a)
    _pset(f, storey, "JZ_Storey", {"Levels_m": ",".join(f"{v:.3f}" for v in levels), "TopLevel_m": float(max(levels)),
                                   "BottomLevel_m": float(min(levels)), "IsBasement": min(levels) < 0, "StoreyCount": len(levels)})

    def contain(p, where=storey):
        api.spatial.assign_container(f, relating_structure=where, products=[p])

    # 分区范围：按方案里各分区（zone，旧方案里叫 core）的房间外包；构件按 x 归到最近的分区，记在 JZ_Zone.Block，出分图时按它过滤
    polys = {r["id"]: r["polygon"] for r in rec["rooms"]}
    spans = {}
    for r in sch["rooms"]:
        zid = r.get("zone") or r.get("core")
        if zid and r["id"] in polys:
            xs_ = [q[0] for q in polys[r["id"]]]
            lo, hi = spans.get(zid, (1e18, -1e18))
            spans[zid] = (min(lo, min(xs_)), max(hi, max(xs_)))

    def zone(p, x_mm):
        if spans:
            k = min(spans, key=lambda k: 0 if spans[k][0] <= x_mm <= spans[k][1] else min(abs(x_mm - spans[k][0]), abs(x_mm - spans[k][1])))
            _pset(f, p, "JZ_Zone", {"Block": k})

    mats = {k: api.material.add_material(f, name=k, category=c) for k, c in
            (("cn-bearing", "concrete"), ("cn-existing", "block"), ("cn-partition", "block"), ("cn-demolish", "demolish"))}

    # 墙
    n_wall = 0
    for w in rec["walls"]:
        ring = _ring(w["polygon"], OX, OY)
        if len(ring) < 3:
            continue
        e = api.root.create_entity(f, ifc_class="IfcWall", name=w["id"])
        api.geometry.assign_representation(f, product=e, representation=_prism(f, body, ring, 0, STOREY_H))
        api.geometry.edit_object_placement(f, product=e, matrix=_matrix(0, 0, z0, 0))
        bearing = w["exterior"] or w["thickness"] >= 180
        new = w.get("status") == "新砌"
        api.material.assign_material(f, products=[e], material=mats["cn-partition" if new else ("cn-bearing" if bearing else "cn-existing")])
        contain(e)
        _pset(f, e, "Pset_WallCommon", {"IsExternal": bool(w["exterior"]), "Status": "NEW" if new else "EXISTING"})
        _pset(f, e, "JZ_Wall", {"Thickness_mm": float(w["thickness"]), "Status": w.get("status", "保留"), "SuspectedLoadBearing": bool(bearing and not new),
                                "Reason": w["reason"]})
        zone(e, sum(q[0] for q in ring) / len(ring) + OX)
        n_wall += 1

    # 拆除的墙块：单独建墙，Status=DEMOLISH，材质 cn-demolish（出图画红色虚线；总图排除）
    for k_, d in enumerate(sch.get("demolish", []), 1):
        ring = _ring(d["polygon"], OX, OY)
        if len(ring) < 3:
            continue
        e = api.root.create_entity(f, ifc_class="IfcWall", name=f"拆除{k_:02d}")
        api.geometry.assign_representation(f, product=e, representation=_prism(f, body, ring, 0, STOREY_H))
        api.geometry.edit_object_placement(f, product=e, matrix=_matrix(0, 0, z0, 0))
        api.material.assign_material(f, products=[e], material=mats["cn-demolish"])
        contain(e)
        _pset(f, e, "Pset_WallCommon", {"IsExternal": False, "Status": "DEMOLISH"})
        _pset(f, e, "JZ_Wall", {"Thickness_mm": float(d.get("thickness", 0)), "Status": "拆除", "Reason": d["reason"], "From": d["from"]})
        zone(e, sum(q[0] for q in ring) / len(ring) + OX)

    # 房间
    use = {x["id"]: x for x in sch["rooms"]}
    spaces = {}
    for r in rec["rooms"]:
        u = use[r["id"]]
        ring = _ring(r["polygon"], OX, OY)
        if len(ring) < 3:
            continue
        sp = api.root.create_entity(f, ifc_class="IfcSpace", name=r["id"])
        sp.LongName = u["usage"]
        sp.ObjectType = u["usage"]
        api.geometry.assign_representation(f, product=sp, representation=_prism(f, body, ring, 0, SPACE_H))
        api.geometry.edit_object_placement(f, product=sp, matrix=_matrix(0, 0, z0, 0))
        api.aggregate.assign_object(f, relating_object=storey, products=[sp])
        lx, ly = u.get("label") or ring[0]
        props = {"Usage": u["usage"], "OriginalName": u.get("orig_name", ""), "Unit": u.get("unit", ""), "UnitType": u.get("unit_type", ""),
                 "Zone": u.get("zone") or u.get("core", ""), "Public": bool(u.get("public")), "NetArea_m2": float(u["area_m2"]),
                 "Note": u.get("note", ""), "LabelX_mm": float(lx - OX), "LabelY_mm": float(ly - OY)}
        if u.get("beds"):
            props["BedCount"] = int(u["beds"])
        if u.get("care_type"):
            props["CareType"] = u["care_type"]
        _pset(f, sp, "JZ_Space", props)
        zone(sp, sum(q[0] for q in ring) / len(ring) + OX)
        q = api.pset.add_qto(f, product=sp, name="Qto_SpaceBaseQuantities")
        api.pset.edit_qto(f, qto=q, properties={"NetFloorArea": float(u["area_m2"])})
        spaces[r["id"]] = sp

    # 门窗
    n_door = n_win = 0
    for o in rec["openings"]:
        rooms = ",".join(o["rooms"])
        if o["polygon"]:  # 凸窗、散线窗
            ring = _ring(o["polygon"], OX, OY)
            e = api.root.create_entity(f, ifc_class="IfcWindow", name=o["id"])
            e.ObjectType = "BAY"
            e.PredefinedType = "WINDOW"
            e.OverallWidth, e.OverallHeight = float(o["width"]), 1900.0
            api.geometry.assign_representation(f, product=e, representation=_prism(f, body, ring, 450, 1900))
            api.geometry.assign_representation(f, product=e, representation=_curve2d(f, plan_body, [ring]))
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(0, 0, z0, 0))
            contain(e)
            _pset(f, e, "Pset_WindowCommon", {"IsExternal": True})
            _pset(f, e, "JZ_Opening", {"Kind": "凸窗/散线窗", "Operable": "未知", "Rooms": rooms, "Source": o["block"]})
            zone(e, o["x"])
            n_win += 1
            continue
        w, t = float(o["width"]), float(o["wall_t"] or 200.0)
        if o["kind"] == "door":
            # 局部 +y = 开启一侧（ifcopenshell 门的约定：门框占 y∈[0,t]，门扇向 +y 开；LEFT 铰链在 x=0，RIGHT 在 x=宽）
            s_ = o.get("swing", 1) or 1
            rot = o["rot"] + (180.0 if s_ < 0 else 0.0)
            leafs = o.get("leafs", 1)
            op = ("SLIDING_TO_LEFT" if leafs == 0 else "DOUBLE_DOOR_SINGLE_SWING" if leafs >= 2
                  else "SINGLE_SWING_LEFT" if o.get("hinge_end", -1) * s_ < 0 else "SINGLE_SWING_RIGHT")
        else:
            rot = o["rot"] + (180.0 if o["sy"] < 0 else 0.0)
        ux, uy = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        nx, ny = -uy, ux
        cx, cy = o["x"] - OX, o["y"] - OY
        ox_, oy_ = cx - ux * w / 2 - nx * t / 2, cy - uy * w / 2 - ny * t / 2
        if o["kind"] == "door":
            e = api.root.create_entity(f, ifc_class="IfcDoor", name=o["id"])
            lining = {"LiningDepth": t, "LiningThickness": 50.0, "LiningOffset": 0.0}
            try:
                reps = [api.geometry.add_door_representation(f, context=ctx, overall_height=DOOR_H, overall_width=w,
                                                             operation_type=op, lining_properties=lining) for ctx in (body, plan_body)]
            except Exception as ex:   # 个别开启方式要额外参数，退回单扇平开
                print("JZLOG door rep fallback", o["id"], op, ex)
                op = "SINGLE_SWING_LEFT"
                reps = [api.geometry.add_door_representation(f, context=ctx, overall_height=DOOR_H, overall_width=w,
                                                             operation_type=op, lining_properties=lining) for ctx in (body, plan_body)]
            e.PredefinedType, e.OperationType = "DOOR", op
            e.OverallWidth, e.OverallHeight = w, DOOR_H
            for rep in reps:
                api.geometry.assign_representation(f, product=e, representation=rep)
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(ox_, oy_, z0, rot))
            contain(e)
            _pset(f, e, "Pset_DoorCommon", {"IsExternal": bool(o["exterior"])})
            _pset(f, e, "JZ_Opening", {"Kind": "门", "Label": o["label"], "Rooms": rooms, "Source": o["block"], "ClearWidthEst_mm": w - 100.0})
            zone(e, o["x"])
            n_door += 1
        else:
            e = api.root.create_entity(f, ifc_class="IfcWindow", name=o["id"])
            e.PredefinedType, e.PartitioningType = "WINDOW", "SINGLE_PANEL"
            e.OverallWidth, e.OverallHeight = w, WIN_H
            lining = {"LiningDepth": 80.0, "LiningThickness": 50.0, "LiningOffset": max((t - 80.0) / 2, 0.0)}
            for ctx in (body, plan_body):
                rep = api.geometry.add_window_representation(f, context=ctx, overall_height=WIN_H, overall_width=w,
                                                             partition_type="SINGLE_PANEL", lining_properties=lining)
                api.geometry.assign_representation(f, product=e, representation=rep)
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(ox_, oy_, z0 + WIN_SILL, rot))
            contain(e)
            _pset(f, e, "Pset_WindowCommon", {"IsExternal": bool(o["exterior"])})
            _pset(f, e, "JZ_Opening", {"Kind": "窗", "Label": o["label"], "Operable": "未知", "Rooms": rooms, "Source": o["block"]})
            zone(e, o["x"])
            n_win += 1

    # 轴网（IfcGrid）：轴号错开放在哪一端，记在 JZ_Grid.BubbleEnds（IfcGridAxis 不能挂属性集）
    axes = rec.get("axes") or {"x": [], "y": []}
    if axes["x"] or axes["y"]:
        grid = api.root.create_entity(f, ifc_class="IfcGrid", name="轴网")
        P2 = lambda x, y: f.createIfcCartesianPoint((float(x), float(y)))
        grid.UAxes = [f.createIfcGridAxis(a["label"], f.createIfcPolyline([P2(a["pos"] - OX, a["span"][0] - OY), P2(a["pos"] - OX, a["span"][1] - OY)]), True)
                      for a in axes["x"]]
        grid.VAxes = [f.createIfcGridAxis(a["label"], f.createIfcPolyline([P2(a["span"][0] - OX, a["pos"] - OY), P2(a["span"][1] - OX, a["pos"] - OY)]), True)
                      for a in axes["y"]]
        api.geometry.edit_object_placement(f, product=grid, matrix=_matrix(0, 0, z0, 0))
        contain(grid)
        _pset(f, grid, "JZ_Grid", {"BubbleEnds": json.dumps({k: {a["label"]: a["ends"] for a in axes[k]} for k in ("x", "y")}, ensure_ascii=False),
                                   "Source": "原图 DOTE 层轴线 + _AXISO 轴号"})

    # 床和设施
    types = sym.create_types(f, sym.load_pack(pack_symbols))
    n_item = 0
    for it in sch["items"]:
        occ = sym.place_occurrence(f, types, it["key"], it["name"], it["x"] - OX, it["y"] - OY, z0 + it["z"], it["rot"],
                                   container=spaces.get(it["room"], storey))
        zone(occ, it["x"])
        n_item += 1

    f.write(out_path)
    return {"ifc": out_path, "offset": (OX, OY), "z0": z0, "walls": n_wall, "spaces": len(spaces), "doors": n_door,
            "windows": n_win, "items": n_item, "axes": (len(axes["x"]), len(axes["y"])),
            "extents_m": ((min(xs) - OX) / 1000, (min(ys) - OY) / 1000, (max(xs) - OX) / 1000, (max(ys) - OY) / 1000)}
