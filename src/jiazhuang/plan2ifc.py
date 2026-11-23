"""识别结果（dxfplan）+ 方案（scheme：房间用途、分区、床和设施点位、拆改后的几何）→ IFC4。
在 Blender 里用 Bonsai 自带的 ifcopenshell 运行（不用 bpy）。没有方案时用 scheme.from_recognition() 原样建模。

- 坐标：图纸坐标（mm）减去原点偏移 (OX, OY) 再写入；偏移量记在 IfcSite 的 JZ_Source 属性集里，可以追溯回原图。
- 墙、房间（IfcSpace）、凸窗：多边形拉伸（IfcArbitraryClosedProfileDef + IfcExtrudedAreaSolid，项目单位毫米）。
- 普通门窗：ifcopenshell 参数化门窗（项目单位毫米），不挖洞——识别出的墙段本来就在洞口处断开。
  识别结果没有门窗高度：原图编号（如 FM乙1121、TC1818）的宽度位与洞口宽相差 ≤1dm 的才采用，写进 JZ_Opening.Label
  （openings 编号时优先用它做 Tag），高度取编号后两位；否则用默认值。来源记在 JZ_Opening.HeightSource（"原图编号"/"默认值"），
  门窗表据此注明"待实测"；对不上的原图编号不采用，备注写进 JZ_Opening.LabelNote。防火门类代号（FM/FD/FH）记防火等级。
  默认高度、窗台高取约定包 conventions.json；识别结果带窗台高（sill）的用它，记 JZ_Opening.SillSource="识别结果"，否则记"默认值"。
- 床和设施：约定包的符号类型（symbols.json），实例放进所在的 IfcSpace。
- 家具（方案 items 里键在 furniture.json 里的，place_item）：lib:<类型名> 运行时用 project.append_asset 从 Bonsai 自带的家具库
  引进类型，同一类型只引一次；库的插入点各不相同，按类型的三维外包和 furniture.json 写的靠墙边归一到"靠墙边中点、局部 +y
  指向室内"（furnish.lib_frame）。cn_<类型> 由 furngen 按参数生成，同参数只建一次。
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
import ifcopenshell.geom

from . import conventions
from . import furngen
from . import furnish
from . import markcode
from . import openings
from . import scheme
from .printing import log
from . import symbols as sym

for _m in ("project", "root", "unit", "context", "aggregate", "geometry", "spatial", "material", "pset", "type"):
    importlib.import_module("ifcopenshell.api." + _m)

M = 0.001
STOREY_H = 3000.0
SPACE_H = 2800.0
# 识别结果没有的门窗高度、窗台高：用约定包 conventions.json 的默认值（openings 统计门窗表时用的是同一份）
_OPC = conventions.get("openings")
DOOR_H = float(_OPC["default_height_mm"]["door"])
WIN_H = float(_OPC["default_height_mm"]["window"])
BAY_H = float(_OPC["default_height_mm"]["bay"])
WIN_SILL = float(_OPC["default_sill_mm"]["window"])
BAY_SILL = float(_OPC["default_sill_mm"]["bay"])


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


def lib_bbox(occ) -> tuple:
    """构件在自身局部坐标里的三维外包（毫米）：(x0, y0, z0, x1, y1, z1)。库类型的插入点不统一，靠它归一。"""
    sh = ifcopenshell.geom.create_shape(ifcopenshell.geom.settings(), occ)
    v = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3) / M
    return tuple(float(a) for a in (*v.min(0), *v.max(0)))


def place_item(f, cache, it, x, y, z, container):
    """方案里一件家具（键 lib:<库类型名> 或 cn_<参数化类型>）摆到 (x, y)（靠墙边中点，已减过偏移）：
    库类型运行时从 Bonsai 家具库引进、按外包归一；参数化类型由 furngen 按参数生成。同一类型同参数只建一次（cache 里记着）。
    实例写 JZ_Furniture.Placement（固定/活动）：小比例总图只画洁具和固定家具。"""
    key, spec = it["key"], furnish.TYPES[it["key"]]
    if key.startswith("lib:"):
        if "lib" not in cache:
            cache["lib"] = ifcopenshell.open(furnish.library_path())
        if key not in cache:
            src = next((t for t in cache["lib"].by_type("IfcTypeProduct") if t.Name == key[4:]), None)
            if src is None:
                raise KeyError(f"家具库里没有类型 {key[4:]}（{furnish.library_path()}）")
            cache[key] = [api.project.append_asset(f, library=cache["lib"], element=src), None]
        ck = key
    else:
        params = furnish.resolve(key, it.get("params"))
        ck = key + json.dumps(params, sort_keys=True)
        if ck not in cache:
            g = furnish.geom(key, params)
            name = f"{spec['name']} {g['width']:.0f}×{g['depth']:.0f}" + (f" 转角{params['return']:.0f}" if params.get("return") else "")
            cache[ck] = [furngen.create_type(f, spec, params, name), "cn"]
    t = cache[ck][0]
    occ = api.root.create_entity(f, ifc_class=t.is_a()[:-4], name=it["name"])
    api.type.assign_type(f, related_objects=[occ], relating_type=t)
    extra, (bx, by) = 0.0, (0.0, 0.0)
    if key.startswith("lib:"):
        if cache[ck][1] is None:
            cache[ck][1] = lib_bbox(occ)
        extra, (bx, by) = furnish.lib_frame(cache[ck][1], spec["back"])
    a = math.radians(it["rot"] + extra)
    api.geometry.edit_object_placement(f, product=occ, matrix=_matrix(x - (math.cos(a) * bx - math.sin(a) * by),
                                                                       y - (math.sin(a) * bx + math.cos(a) * by), z, it["rot"] + extra))
    api.spatial.assign_container(f, relating_structure=container, products=[occ])
    fixed = spec.get("fixed") or t.is_a("IfcSanitaryTerminalType")
    _pset(f, occ, "JZ_Furniture", {"Key": key, "Placement": "固定" if fixed else "活动"})
    return occ


def _orig_label(o: dict, width: float, width0: float | None = None):
    """识别结果里的原图编号 → (采用的编号, 备注)。width0 是拆改前的识别宽度：拆改改了洞口宽度的，原图编号说的是
    改前的洞口，一律不沿用（哪怕只差 1dm、编号还"对得上"）；宽度位与洞口宽相差 1dm 以上的不采用，备注写明待核。"""
    lab, raw = o.get("label") or "", o.get("label_raw") or o.get("label") or ""
    if not raw:
        return "", ""
    if width0 is not None and abs(width0 - width) > 1.0:
        return "", f"原图编号 {raw} 为拆改前的洞口，未沿用"
    if lab and markcode.fits(lab, width):
        return lab, ""
    if markcode.parse(raw) is None:
        return "", f"原图编号文字 {raw} 不是完整编号，未采用（待核）"
    return "", f"原图编号 {raw} 与识别尺寸不符，未采用（待核）"


def _label_height(label: str, width: float):
    """采用的原图编号（代号 + 宽高各两位分米，如 FM乙1121、C1815、TC1818）→ 编号里的高度（毫米）；宽度对不上返回 None。"""
    p = markcode.parse(label)
    return float(p["h"]) if p and markcode.fits(label, width) else None


def build(rec: dict, sch: dict, pack_symbols: str, out_path: str, project_name: str, building_name: str = "楼栋") -> dict:
    """rec 传拆改前的识别结果（原图）；方案有拆改（sch["after"]）时几何用拆改后的，原图编号按拆改前的洞口宽判断。"""
    width0 = {o["id"]: o["width"] for o in rec["openings"]}   # 拆改前的识别宽度（拆改改了宽度的，原图编号不沿用）
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

    # 分区范围：按方案里各分区（zone，旧方案里叫 core）的房间外包；构件按 x 归到最近的分区，记在 JZ_Zone.Block，出分图时按它过滤。
    # 出图自动切的分段（zones 里 auto=True）不是设计分区：只写 JZ_Zone.Block，归属规则和 scheme.split_zones 相同（scheme.zone_at）
    auto = [z for z in (sch.get("zones") or []) if z.get("auto")]
    polys = {r["id"]: r["polygon"] for r in rec["rooms"]}
    spans = {}
    for r in sch["rooms"]:
        zid = r.get("zone") or r.get("core")
        if zid and r["id"] in polys:
            xs_ = [q[0] for q in polys[r["id"]]]
            lo, hi = spans.get(zid, (1e18, -1e18))
            spans[zid] = (min(lo, min(xs_)), max(hi, max(xs_)))

    def zone(p, x_mm):
        if auto:
            _pset(f, p, "JZ_Zone", {"Block": scheme.zone_at(auto, x_mm)})
        elif spans:
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
        lx, ly = u.get("label") or scheme.label_point(r["polygon"])   # 图纸坐标（ring 已减过偏移，不能拿来比）
        props = {"Usage": u["usage"], "OriginalName": u.get("orig_name", ""), "Unit": u.get("unit", ""), "UnitType": u.get("unit_type", ""),
                 "Zone": u.get("zone") or u.get("core", ""), "Public": bool(u.get("public")), "NetArea_m2": float(u["area_m2"]),
                 "Note": u.get("note", ""), "LabelX_mm": float(lx - OX), "LabelY_mm": float(ly - OY)}
        if u.get("beds"):
            props["BedCount"] = int(u["beds"])
        if u.get("care_type"):
            props["CareType"] = u["care_type"]
        _pset(f, sp, "JZ_Space", props)
        zone(sp, lx if auto else sum(q[0] for q in ring) / len(ring) + OX)   # 自动分段：房间按标注点归段（只在这一段写房名）
        q = api.pset.add_qto(f, product=sp, name="Qto_SpaceBaseQuantities")
        api.pset.edit_qto(f, qto=q, properties={"NetFloorArea": float(u["area_m2"])})
        spaces[r["id"]] = sp

    # 门窗
    n_door = n_win = 0
    for o in rec["openings"]:
        rooms = ",".join(o["rooms"])
        w = float(o["width"])
        lab, lab_note = _orig_label(o, w, width0.get(o["id"]))
        lab_h = _label_height(lab, w)   # 原图编号里带的高度；没有就用默认值，门窗表里注明待实测
        hsrc = "原图编号" if lab_h else "默认值"
        jz_lab = dict({"Label": lab}, **({"LabelNote": lab_note} if lab_note else {}))
        if o["polygon"]:  # 凸窗、散线窗
            ring = _ring(o["polygon"], OX, OY)
            bay_h = lab_h or BAY_H
            e = api.root.create_entity(f, ifc_class="IfcWindow", name=o["id"])
            e.ObjectType = "BAY"
            e.PredefinedType = "WINDOW"
            e.OverallWidth, e.OverallHeight = w, bay_h
            api.geometry.assign_representation(f, product=e, representation=_prism(f, body, ring, BAY_SILL, bay_h))
            api.geometry.assign_representation(f, product=e, representation=_curve2d(f, plan_body, [ring]))
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(0, 0, z0, 0))
            contain(e)
            _pset(f, e, "Pset_WindowCommon", {"IsExternal": True})
            _pset(f, e, "JZ_Opening", dict({"Kind": "凸窗/散线窗", "Operable": "未知", "Rooms": rooms, "Source": o["block"],
                                            "HeightSource": hsrc, "SillSource": "默认值"}, **jz_lab))
            zone(e, o["x"])
            n_win += 1
            continue
        t = float(o["wall_t"] or 200.0)
        if o["kind"] == "door":
            # 局部 +y = 开启一侧（ifcopenshell 门的约定：门框占 y∈[0,t]，门扇向 +y 开；LEFT 铰链在 x=0，RIGHT 在 x=宽）
            rot, op = openings.rec_door(o)
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
                reps = [api.geometry.add_door_representation(f, context=ctx, overall_height=lab_h or DOOR_H, overall_width=w,
                                                             operation_type=op, lining_properties=lining) for ctx in (body, plan_body)]
            except Exception as ex:   # 个别开启方式要额外参数，退回单扇平开
                log("door rep fallback", o["id"], op, ex)
                op = "SINGLE_SWING_LEFT"
                reps = [api.geometry.add_door_representation(f, context=ctx, overall_height=lab_h or DOOR_H, overall_width=w,
                                                             operation_type=op, lining_properties=lining) for ctx in (body, plan_body)]
            e.PredefinedType, e.OperationType = "DOOR", op
            e.OverallWidth, e.OverallHeight = w, lab_h or DOOR_H
            for rep in reps:
                api.geometry.assign_representation(f, product=e, representation=rep)
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(ox_, oy_, z0, rot))
            contain(e)
            fire = markcode.fire_rating(lab)   # 防火门类代号 FM/FD/FH → 甲/乙/丙级（没写等级为"待定"）
            _pset(f, e, "Pset_DoorCommon", dict({"IsExternal": bool(o["exterior"])}, **({"FireRating": fire} if fire else {})))
            _pset(f, e, "JZ_Opening", dict({"Kind": "门", "Rooms": rooms, "Source": o["block"], "ClearWidthEst_mm": w - 100.0,
                                            "HeightSource": hsrc}, **jz_lab, **({"Entrance": True} if o.get("entrance") else {})))
            zone(e, o["x"])
            n_door += 1
        else:
            e = api.root.create_entity(f, ifc_class="IfcWindow", name=o["id"])
            e.PredefinedType, e.PartitioningType = "WINDOW", "SINGLE_PANEL"
            e.OverallWidth, e.OverallHeight = w, lab_h or WIN_H
            lining = {"LiningDepth": 80.0, "LiningThickness": 50.0, "LiningOffset": max((t - 80.0) / 2, 0.0)}
            for ctx in (body, plan_body):
                rep = api.geometry.add_window_representation(f, context=ctx, overall_height=lab_h or WIN_H, overall_width=w,
                                                             partition_type="SINGLE_PANEL", lining_properties=lining)
                api.geometry.assign_representation(f, product=e, representation=rep)
            sill = float(o["sill"]) if o.get("sill") is not None else WIN_SILL
            api.geometry.edit_object_placement(f, product=e, matrix=_matrix(ox_, oy_, z0 + sill, rot))
            contain(e)
            _pset(f, e, "Pset_WindowCommon", {"IsExternal": bool(o["exterior"])})
            _pset(f, e, "JZ_Opening", dict({"Kind": "窗", "Operable": "未知", "Rooms": rooms, "Source": o["block"],
                                            "HeightSource": hsrc, "SillSource": "识别结果" if o.get("sill") is not None else "默认值"}, **jz_lab))
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

    # 床和设施：约定包符号的类型；家具（furniture.json 的键）见 place_item
    pack = sym.load_pack(pack_symbols)
    types = sym.create_types(f, pack)
    furn = {}
    placed = []
    for it in sch["items"]:
        where = spaces.get(it["room"], storey)
        if it["key"] in furnish.TYPES:
            occ = place_item(f, furn, it, it["x"] - OX, it["y"] - OY, z0 + it["z"], where)
        else:
            occ = sym.place_occurrence(f, types, it["key"], it["name"], it["x"] - OX, it["y"] - OY, z0 + it["z"], it["rot"], container=where)
            if it.get("plan_id"):   # 布点器的点位：plan_id 写进 Tag，回路、控制关系和方案对得上
                occ.Tag = it["plan_id"]
            placed.append((it, occ))
        zone(occ, it["x"])
    links = _mep_links(f, placed, {r["id"]: r.get("unit") or "" for r in rec["rooms"]}, pack)

    f.write(out_path)
    return {"ifc": out_path, "offset": (OX, OY), "z0": z0, "walls": n_wall, "spaces": len(spaces), "doors": n_door,
            "windows": n_win, "items": len(sch["items"]), "axes": (len(axes["x"]), len(axes["y"])),
            "extents_m": ((min(xs) - OX) / 1000, (min(ys) - OY) / 1000, (max(xs) - OX) / 1000, (max(ys) - OY) / 1000), **links}


def _mep_links(f, placed, unit_of: dict, pack: dict) -> dict:
    """水电关系：带 circuit 的点位按（户, 回路号）归到 IfcDistributionCircuit；符号库写了 system 的给排水点归到冷水、热水、排水
    系统（IfcDistributionSystem，全楼层各一个）；开关和它控制的灯（controls）用端口连起来：开关每控一盏灯一个 SOURCE 端口，
    灯每个控它的开关一个 SINK 端口（IfcRelConnectsPorts，connect_port 的 direction 要传 SOURCE）。"""
    groups = {}
    for it, occ in placed:
        if it.get("circuit"):
            groups.setdefault(("IfcDistributionCircuit", it["circuit"], unit_of.get(it["room"], "")), []).append(occ)
        if s := pack[it["key"]].get("system"):
            groups.setdefault(("IfcDistributionSystem", s["name"], s["type"]), []).append(occ)
    if not groups and not any(it.get("controls") for it, _ in placed):
        return {}
    importlib.import_module("ifcopenshell.api.system")
    from . import mep
    for (cls, name, extra), occs in groups.items():
        s = api.system.add_system(f, ifc_class=cls)
        if cls == "IfcDistributionCircuit":   # extra 是户
            c = mep.CIRCUITS.get(name.rstrip("0123456789"), {})
            attrs = {"Name": name, "Description": extra or None, "LongName": c.get("name"), "PredefinedType": c.get("system", "NOTDEFINED")}
        else:
            attrs = {"Name": name, "PredefinedType": extra}
        api.system.edit_system(f, system=s, attributes=attrs)
        api.system.assign_system(f, products=occs, system=s)
    lights = {it["plan_id"]: occ for it, occ in placed if it.get("plan_id")}
    n = 0
    for it, occ in placed:
        for lid in it.get("controls") or []:
            if lid not in lights:
                log("controls: no light", it.get("plan_id"), lid)
                continue
            ports = []
            for e, way in ((occ, "SOURCE"), (lights[lid], "SINK")):
                p = api.system.add_port(f, element=e)
                p.Name, p.FlowDirection, p.PredefinedType, p.SystemType = f"{it['plan_id']}→{lid}", way, "CABLE", "LIGHTING"
                ports.append(p)
            api.system.connect_port(f, port1=ports[0], port2=ports[1], direction="SOURCE")
            n += 1
    log("mep", "systems", len(groups), "links", n)
    return {"systems": len(groups), "links": n}
