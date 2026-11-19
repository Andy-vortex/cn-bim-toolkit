"""规范检查引擎：读 IFC + 规则包（packs/cn/rules/*.json）→ 逐条结论 + HTML/PDF 报告。

在 Blender 里用 Bonsai 自带的 ifcopenshell 和 shapely 运行（不用 bpy）：
  blender -b --python scripts/check_ifc.py -- <ifc> <规则.json> <输出目录>

规则包是纯数据：每条规则写明 `type`（下面 CHECKS 里的通用检查类型）和 `params`（用途关键字、阈值、说法）。
引擎本身不认识任何具体标准，条文号和概括都在规则包里。

结论只有六种：通过 / 不通过 / 待确认（有数据但缺关键一项，如窗能否开启）/ 信息不足（模型里没有判定所需的属性）/
不涉及（本模型没有这类房间或构件）/ 人工复核（管理、运营类要求，图上判不了）。
检查尽量只用 IFC 的通用信息（空间轮廓、构件类别和预定义类型、Pset_*Common、构件位置），
本项目自定义的 JZ_* 属性只作补充（房间用途、分区、照护类型、一个标准层代表的多个标高）。
所有结论只做提示，不替代建筑、结构、消防等专业审查。
"""
from __future__ import annotations

import datetime
import html
import json
import math
import os
import subprocess

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as ue
import ifcopenshell.util.placement as up
import ifcopenshell.util.unit as uu
import shapely
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
STATUSES = ("不通过", "待确认", "信息不足", "人工复核", "通过", "不涉及")
COLORS = {"通过": "#2e7d32", "不通过": "#c62828", "待确认": "#ef6c00", "信息不足": "#6a1b9a", "不涉及": "#757575", "人工复核": "#1565c0"}

_GS = ifcopenshell.geom.settings()
_GS.set("use-world-coords", True)


# ------------------------------------------------------------------ 读模型
class Model:
    def __init__(self, path):
        self.path = path
        f = self.f = ifcopenshell.open(path)
        self.S = uu.calculate_unit_scale(f) * 1000.0   # 项目单位 → 毫米
        self.scheme = {}
        for b in f.by_type("IfcBuilding"):
            ps = ue.get_psets(b)
            self.scheme.update(ps.get("JZ_Facility", {}))
            self.scheme.update(ps.get("JZ_Scheme", {}))
        self.scheme.pop("id", None)
        self.storeys = []
        for st in f.by_type("IfcBuildingStorey"):
            ps = ue.get_psets(st).get("JZ_Storey", {})
            lv = [float(x) for x in str(ps["Levels_m"]).split(",")] if ps.get("Levels_m") else [(st.Elevation or 0.0) * self.S / 1000.0]
            self.storeys.append({"e": st, "sid": st.id(), "name": st.Name, "levels": lv, "basement": bool(ps.get("IsBasement", min(lv) < 0))})
        self.spaces, by_id = [], {}
        for sp in f.by_type("IfcSpace"):
            ps = ue.get_psets(sp)
            jz = ps.get("JZ_Space", {})
            st = ue.get_aggregate(sp)
            s = {"e": sp, "id": sp.Name, "usage": jz.get("Usage") or sp.LongName or sp.Name or "",
                 "area": ps.get("Qto_SpaceBaseQuantities", {}).get("NetFloorArea") or jz.get("NetArea_m2"),
                 "poly": self._space_polygon(sp), "storey": st.id() if st is not None else None, "unit": jz.get("Unit", ""),
                 "zone": jz.get("Zone") or jz.get("Core", ""), "care": jz.get("CareType", ""), "elements": []}
            self.spaces.append(s)
            by_id[sp.id()] = s
        for cls in ("IfcFurniture", "IfcAlarm", "IfcRailing", "IfcOutlet", "IfcAudioVisualAppliance", "IfcSanitaryTerminal"):
            for e in f.by_type(cls):
                c = ue.get_container(e)
                s = by_id.get(c.id()) if c is not None and c.is_a("IfcSpace") else self._nearest_space(self.xy(e))
                if s:
                    s["elements"].append(e)
        self.windows = []
        for w in f.by_type("IfcWindow"):
            ps = ue.get_psets(w)
            op = ps.get("JZ_Opening", {}).get("Operable")
            self.windows.append({"e": w, "fp": self.footprint(w), "external": ps.get("Pset_WindowCommon", {}).get("IsExternal"),
                                 "operable": {"是": True, "否": False}.get(op, op if isinstance(op, bool) else None)})
        self._wall_fp = None
        # 门：几何外包 + 两侧空间（从门中心沿墙法线两边各探 250mm，落在哪个空间里；只看外包会在墙角误判）
        self.doors = []
        for d in f.by_type("IfcDoor"):
            fp = self.footprint(d)
            near = []
            if fp is not None:
                m = up.get_local_placement(d.ObjectPlacement)
                ny = (m[0][1], m[1][1])                      # 门的局部 y = 墙的法线方向
                c = fp.centroid
                for sgn in (-1, 1):
                    p = Point(c.x + sgn * ny[0] * 250, c.y + sgn * ny[1] * 250)
                    hit = [s for s in self.spaces if s["poly"] is not None and s["poly"].buffer(5).contains(p)]
                    if hit:
                        near.append(min(hit, key=lambda s: s["poly"].area))
            self.doors.append({"e": d, "fp": fp, "w": (d.OverallWidth or 0) * self.S, "op": d.OperationType or "", "spaces": near})

    def xy(self, e):
        m = up.get_local_placement(e.ObjectPlacement)
        return m[0][3] * self.S, m[1][3] * self.S

    def footprint(self, e):
        try:
            sh = ifcopenshell.geom.create_shape(_GS, e)
        except Exception:
            return None
        v = sh.geometry.verts
        return MultiPoint([(v[i] * 1000.0, v[i + 1] * 1000.0) for i in range(0, len(v), 3)]).convex_hull

    def _space_polygon(self, sp):
        m = up.get_local_placement(sp.ObjectPlacement)
        for rep in (sp.Representation.Representations if sp.Representation else []):
            for it in rep.Items:
                if it.is_a("IfcExtrudedAreaSolid") and it.SweptArea.is_a("IfcArbitraryClosedProfileDef") \
                        and it.SweptArea.OuterCurve.is_a("IfcPolyline"):
                    lx, ly = (it.Position.Location.Coordinates[:2] if it.Position else (0.0, 0.0))
                    pts = []
                    for p in it.SweptArea.OuterCurve.Points:
                        x, y = p.Coordinates[0] + lx, p.Coordinates[1] + ly
                        pts.append(((m[0][0] * x + m[0][1] * y + m[0][3]) * self.S, (m[1][0] * x + m[1][1] * y + m[1][3]) * self.S))
                    return Polygon(pts).buffer(0)
        return self.footprint(sp)

    def _nearest_space(self, xy, tol=150.0):
        p = Point(xy)
        cands = [(s["poly"].distance(p), k) for k, s in enumerate(self.spaces) if s["poly"] is not None]
        if not cands:
            return None
        d, k = min(cands)
        return self.spaces[k] if d < tol else None

    def walls_fp(self):
        """现存的墙（拆除状态的不算）。"""
        if self._wall_fp is None:
            self._wall_fp = [(w, self.footprint(w)) for w in self.f.by_type("IfcWall")
                             if ue.get_psets(w).get("Pset_WallCommon", {}).get("Status") != "DEMOLISH"]
        return self._wall_fp


def kind(e):
    """构件归类：bed / call / grab / socket / socket_splash / camera / sanitary / other。"""
    t = ue.get_type(e)
    tag = getattr(t, "Tag", None) if t is not None else None
    pt = ue.get_predefined_type(e)
    name = (e.Name or "") + ((t.Name or "") if t is not None else "")
    if e.is_a("IfcFurniture") and (pt == "BED" or "床" in name):
        return "bed"
    if e.is_a("IfcAlarm") or "呼叫" in name:
        return "call"
    if (e.is_a("IfcRailing") and pt in ("HANDRAIL", "GRABRAIL")) or "扶手" in name:
        return "grab"
    if e.is_a("IfcOutlet"):
        return "socket_splash" if (tag == "cn_socket_splash" or "防溅" in name or "防水" in name) else "socket"
    if (e.is_a("IfcAudioVisualAppliance") and pt == "CAMERA") or "监控" in name:
        return "camera"
    if e.is_a("IfcSanitaryTerminal"):
        return "sanitary"
    return "other"


def _has(usage, words):
    return any(w in (usage or "") for w in words)


def _lbl(s):
    return f'{s["id"]} {s["usage"]}' + (f'（{s["unit"]}）' if s["unit"] else "")


def _beds(s):
    return [e for e in s["elements"] if kind(e) == "bed"]


def _zones(M):
    out = {}
    for s in M.spaces:
        if s["zone"]:
            out.setdefault(s["zone"], []).append(s)
    return out


def _is_care(s, p):
    """房间是不是"护理型"：看 JZ_Space.CareType；没写时按规则参数 default_care。"""
    if s.get("care"):
        return s["care"] == p.get("care_value", "护理型")
    return bool(p.get("default_care", False))


def _none(p, what):
    return [("不涉及", "-", p.get("none_text", f"模型里没有{what}"))]


# ------------------------------------------------------------------ 通用检查类型
def storey_max_elevation(p, M):
    """有相关用房的楼层，楼面标高（一个标准层代表多层时取最高）不超过上限。"""
    items = []
    for st in M.storeys:
        used = [s for s in M.spaces if s["storey"] == st["sid"] and _has(s["usage"], p["usages"]) and not _has(s["usage"], p.get("exempt_usage", []))]
        if not used:
            continue
        top = max(st["levels"])
        ok = top <= p["max_floor_elevation_m"]
        items.append(("通过" if ok else "不通过", st["name"],
                      f"本层相关用房 {len(used)} 间；代表的楼面标高 {'、'.join(f'{v:.3f}' for v in st['levels'])} m，"
                      f"最高 {top:.3f} m {'≤' if ok else '>'} {p['max_floor_elevation_m']} m"))
    return items or _none(p, "相关用房")


def no_usage_in_basement(p, M):
    items = []
    for st in M.storeys:
        rooms = [s for s in M.spaces if s["storey"] == st["sid"] and _has(s["usage"], p["usages"])]
        if rooms:
            items.append(("不通过" if st["basement"] else "通过", st["name"],
                          f"{'地下/半地下层' if st['basement'] else '地上层（最低标高 %.3f m）' % min(st['levels'])}，相关房间 {len(rooms)} 间"))
    return items or _none(p, "相关房间")


def exterior_window(p, M):
    """房间要有外窗；窗能否开启看 JZ_Opening.Operable，不知道时报待确认。"""
    gap, items = p.get("max_window_gap_mm", 300), []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        ext = [w for w in M.windows if w["external"] and w["fp"] is not None and w["fp"].distance(s["poly"]) <= gap]
        if not ext:
            items.append(("不通过", _lbl(s), "没有找到外窗"))
        elif not p.get("need_operable", True) or any(w["operable"] is True for w in ext):
            items.append(("通过", _lbl(s), f"外窗 {len(ext)} 扇" + ("，其中有可开启扇" if p.get("need_operable", True) else "")))
        elif all(w["operable"] is False for w in ext):
            items.append(("不通过", _lbl(s), f"外窗 {len(ext)} 扇，均为固定扇"))
        else:
            items.append(("待确认", _lbl(s), f"有外窗 {len(ext)} 扇（{'、'.join(w['e'].Name for w in ext)}），图上看不出能否开启"))
    return items or _none(p, "相关房间")


def max_beds_per_room(p, M):
    items = []
    for s in M.spaces:
        if _has(s["usage"], p["usages"]):
            n, care = len(_beds(s)), _is_care(s, p)
            mx = p["max_beds_care"] if care else p["max_beds_other"]
            items.append(("通过" if n <= mx else "不通过", _lbl(s), f"{n} 床（{p.get('care_value', '护理型') if care else '其他'}，上限 {mx} 床）"))
    return items or _none(p, "相关房间")


def room_min_area(p, M):
    """某类房间的使用面积不小于 min_m2。"""
    items = []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        a = s["area"] if s["area"] is not None else (s["poly"].area / 1e6 if s["poly"] is not None else None)
        if a is None:
            items.append(("信息不足", _lbl(s), "没有使用面积"))
        else:
            items.append(("通过" if a >= p["min_m2"] else "不通过", _lbl(s), f"使用面积 {a:.2f} ㎡（≥{p['min_m2']:.2f}）"))
    return items or _none(p, "相关房间")


def area_per_bed(p, M):
    mn, items = p["min_area_per_bed_m2"], []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        n = len(_beds(s))
        if not n:
            items.append(("信息不足", _lbl(s), "房间里没有床，无法按床位核算"))
        elif s["area"] is None:
            items.append(("信息不足", _lbl(s), "没有使用面积"))
        else:
            per = s["area"] / n
            items.append(("通过" if per >= mn else "不通过", _lbl(s), f"使用面积 {s['area']:.2f} ㎡ / {n} 床 = {per:.2f} ㎡/床（≥{mn:.2f}）"))
    return items or _none(p, "相关房间")


def room_area_by_beds(p, M):
    """单人间、双人间的最小使用面积；多人间按每床面积。"""
    items = []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        n, a = len(_beds(s)), s["area"] or 0.0
        if n == 1:
            items.append(("通过" if a >= p["single_m2"] else "不通过", _lbl(s), f"单人间 {a:.2f} ㎡（≥{p['single_m2']:.2f}）"))
        elif n == 2:
            items.append(("通过" if a >= p["double_m2"] else "不通过", _lbl(s), f"双人间 {a:.2f} ㎡（≥{p['double_m2']:.2f}）"))
        elif n > 2:
            per = p.get("multi_m2_per_bed", 6.0)
            items.append(("通过" if a >= per * n else "不通过", _lbl(s), f"{n} 人间 {a:.2f} ㎡（按每床 {per} ㎡）"))
        else:
            items.append(("信息不足", _lbl(s), "房间里没有床"))
    return items or _none(p, "相关房间")


def bedroom_access(p, M):
    """有床的房间：留得下轮椅回转圆；从门口出发，主通道宽度能走到每张床边；相邻床的间距。"""
    half, items = p["main_passage_mm"] / 2.0, []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        beds = [(b, M.footprint(b)) for b in _beds(s)]
        beds = [(b, fp) for b, fp in beds if fp is not None]
        if not beds:
            items.append(("信息不足", _lbl(s), "房间里没有床"))
            continue
        free = s["poly"].difference(unary_union([fp for _, fp in beds]))
        probs = []
        if free.buffer(-p["turning_diameter_mm"] / 2.0).is_empty:
            probs.append("放不下直径 %.1fm 的轮椅回转圆" % (p["turning_diameter_mm"] / 1000))
        doors = [d for d in M.doors if any(x is s for x in d["spaces"]) and d["fp"] is not None]
        eroded = free.buffer(-half)
        comps = list(eroded.geoms) if eroded.geom_type == "MultiPolygon" else ([eroded] if not eroded.is_empty else [])
        entry = [c for c in comps for d in doors if c.distance(d["fp"]) <= half + 400]
        if not doors:
            probs.append("没找到门")
        elif not entry:
            probs.append("门口进不去 %.2fm 宽的通道" % (p["main_passage_mm"] / 1000))
        else:
            for b, fp in beds:
                if not any(c.distance(fp) <= half + 60 for c in entry):
                    probs.append(f"{b.Name} 床边到不了 {p['main_passage_mm'] / 1000:.2f}m 宽的主通道")
        for i in range(len(beds)):
            for j in range(i + 1, len(beds)):
                g = beds[i][1].distance(beds[j][1])
                if g < p["bed_gap_mm"] - 1:
                    probs.append(f"{beds[i][0].Name} 与 {beds[j][0].Name} 间距 {g:.0f}mm（≥{p['bed_gap_mm']}）")
        items.append(("不通过" if probs else "通过", _lbl(s), "；".join(probs) if probs else
                      f"{len(beds)} 床：回转空间、{p['main_passage_mm']}mm 主通道、床间距均满足"))
    return items or _none(p, "相关房间")


def device_in_room(p, M):
    """某类房间里要有某种设施（device 取值见 kind()）。模型里一个都没有时报信息不足（require_in_model）。"""
    dev, name = p["device"], p.get("device_name", p["device"])
    if p.get("require_in_model") and not any(kind(e) == dev for s in M.spaces for e in s["elements"]):
        return [("信息不足", "全部", f"模型里没有{name}")]
    items = []
    for s in M.spaces:
        if _has(s["usage"], p["usages"]):
            n = sum(1 for e in s["elements"] if kind(e) == dev)
            items.append(("通过" if n >= p.get("min_count", 1) else "不通过", _lbl(s), f"{name} {n} 个"))
    return items or _none(p, "相关房间")


def call_near_bed(p, M):
    """每张床附近（≤ max_distance_bed_to_call_mm）要有呼叫装置；usages 里的房间也要有。"""
    dmax, items = p["max_distance_bed_to_call_mm"], []
    for s in M.spaces:
        calls = [e for e in s["elements"] if kind(e) == "call"]
        for b in _beds(s):
            fp = M.footprint(b)
            d = min([fp.distance(Point(M.xy(c))) for c in calls] + [1e9]) if fp is not None else 1e9
            items.append(("通过" if d <= dmax else "不通过", f"{_lbl(s)} · {b.Name}",
                          f"最近的呼叫装置离床 {d:.0f} mm（≤{dmax}）" if d < 1e9 else "房间里没有呼叫装置"))
        if _has(s["usage"], p.get("usages", [])):
            items.append(("通过" if calls else "不通过", _lbl(s), f"呼叫装置 {len(calls)} 个"))
    return items or _none(p, "床位")


def wet_room_sockets(p, M):
    """湿区（卫生间等）的插座都要是防溅型。"""
    items = []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        ks = [kind(e) for e in s["elements"] if e.is_a("IfcOutlet")]
        if not ks:
            items.append(("不涉及", _lbl(s), "没有插座"))
        else:
            bad = ks.count("socket")
            items.append(("不通过" if bad else "通过", _lbl(s), f"插座 {len(ks)} 个，其中普通插座 {bad} 个"))
    return items or _none(p, "相关房间")


def fire_separation_info(p, M):
    """某类房间要防火分隔：墙门有 FireRating 属性才能判，否则信息不足。"""
    rooms = [s for s in M.spaces if _has(s["usage"], p["usages"])]
    if not rooms:
        return _none(p, "、".join(p["usages"]))
    items = []
    for s in rooms:
        rated = [w for w, fp in M.walls_fp() if fp is not None and fp.distance(s["poly"]) < 50
                 and ue.get_psets(w).get("Pset_WallCommon", {}).get("FireRating")]
        items.append(("信息不足" if not rated else "待确认", _lbl(s), "围护墙、门没有耐火等级属性" if not rated else "已有耐火属性，需对照要求"))
    return items


def wall_combustibility_info(p, M):
    """某类房间的隔墙要不燃、砌到板底：墙有 Pset_WallCommon.Combustible 才能判。"""
    items, walls = [], M.walls_fp()
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        around = [w for w, fp in walls if fp is not None and fp.distance(s["poly"]) < 50]
        known = [w for w in around if ue.get_psets(w).get("Pset_WallCommon", {}).get("Combustible") is not None]
        if len(known) < len(around) or not around:
            mats = sorted({(getattr(ue.get_material(w), "Category", None) or "?") for w in around})
            items.append(("信息不足", _lbl(s), f"周边墙 {len(around)} 段，{len(around) - len(known)} 段没有“是否可燃”和“砌到板底”属性"
                                             f"（材料类别：{'、'.join(mats)}）"))
        else:
            bad = [w for w in known if ue.get_psets(w)["Pset_WallCommon"]["Combustible"]]
            items.append(("不通过" if bad else "待确认", _lbl(s), f"可燃墙 {len(bad)} 段；是否砌到板底需另行确认"))
    return items or _none(p, "相关房间")


def covering_material_info(p, M):
    cov = [c for c in M.f.by_type("IfcCovering") if c.PredefinedType in ("CEILING", "ROOFING")] + list(M.f.by_type("IfcRoof"))
    if not cov:
        return [("不涉及", "全部", "模型里没有屋面和吊顶构件")]
    return [("信息不足", "全部", f"屋面/吊顶构件 {len(cov)} 个，芯材属性待补")]


def forbidden_usage(p, M):
    bad = [s for s in M.spaces if _has(s["usage"], p["words"])]
    if bad:
        return [("不通过", _lbl(s), f"房间用途含“{'/'.join(p['words'])}”") for s in bad]
    return [("通过", "全部", p.get("ok_text", "没有这类用途的房间"))]


def zone_required_rooms(p, M):
    """每个分区（JZ_Space.Zone）都要有 required 里列的各类用房；in_toilet 里的设施按设在卫生间内考虑（待确认）。"""
    label, items = p.get("zone_label", "分区"), []
    for k, sp in sorted(_zones(M).items()):
        if p.get("only_if") and not any(_has(s["usage"], p["only_if"]) for s in sp):
            continue
        miss = [name for name, words in p["required"].items() if not any(_has(s["usage"], words) for s in sp)]
        if miss:
            items.append(("不通过", f"{label} {k}", "缺：" + "、".join(miss)))
        elif p.get("in_toilet") and any("卫生间" in s["usage"] for s in sp):
            items.append(("待确认", f"{label} {k}", f"必设用房齐全；{'、'.join(p['in_toilet'])}按设在卫生间内考虑，洁具未建模，待确认"))
        else:
            items.append(("通过", f"{label} {k}", "必设用房齐全"))
    return items or _none(p, label)


def zone_max_beds(p, M):
    label, items = p.get("zone_label", "分区"), []
    for k, sp in sorted(_zones(M).items()):
        n = sum(len(_beds(s)) for s in sp)
        if n:
            items.append(("通过" if n <= p["max_beds"] else "不通过", f"{label} {k}", f"{n} 床（上限 {p['max_beds']}）"))
    return items or _none(p, "床位")


def zone_area_per_bed(p, M):
    """分区内某类用房（如起居厅）的合计面积 ≥ 每床指标 × 床位数。"""
    label, items = p.get("zone_label", "分区"), []
    for k, sp in sorted(_zones(M).items()):
        n = sum(len(_beds(s)) for s in sp)
        if not n:
            continue
        a = sum(s["area"] or 0 for s in sp if _has(s["usage"], p["usages"]))
        items.append(("通过" if a >= p["min_m2_per_bed"] * n else "不通过", f"{label} {k}",
                      f"{'/'.join(p['usages'])} {a:.2f} ㎡ / {n} 床 = {a / n:.2f} ㎡/床（≥{p['min_m2_per_bed']:.2f}）"))
    return items or _none(p, "床位")


def zone_dining_area(p, M):
    """餐厅座位按床位比例、每座面积；和起居厅合用时再加起居的每床面积。"""
    label, items = p.get("zone_label", "分区"), []
    shared_word = p.get("shared_with", "起居")
    for k, sp in sorted(_zones(M).items()):
        n = sum(len(_beds(s)) for s in sp)
        if not n:
            continue
        dining = [s for s in sp if _has(s["usage"], p["usages"])]
        shared = any(shared_word in s["usage"] for s in dining)
        seats = math.ceil(n * p["seat_ratio"])
        need = seats * p["m2_per_seat"] + (p.get("shared_m2_per_bed", 0) * n if shared else 0)
        a = sum(s["area"] or 0 for s in dining)
        items.append(("通过" if a >= need else "不通过", f"{label} {k}",
                      f"就餐面积 {a:.2f} ㎡，需要 {need:.2f} ㎡（{seats} 座 × {p['m2_per_seat']} ㎡"
                      + (f" + {shared_word} {p.get('shared_m2_per_bed', 0)} ㎡/床 × {n}，因合用" if shared else "") + "）"))
    return items or _none(p, "床位")


def ensuite_required(p, M):
    """房间要有门直通的卫生间；护理型"应设"，其他"宜设"（未设不算不通过）。"""
    items = []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]):
            continue
        wc = [x for d in M.doors if any(y is s for y in d["spaces"]) for x in d["spaces"] if x is not s and "卫生间" in x["usage"]]
        if wc:
            items.append(("通过", _lbl(s), f"有门直通卫生间 {wc[0]['id']}"))
        elif _is_care(s, p):
            items.append(("不通过", _lbl(s), f"没有相邻的专用卫生间（{p.get('care_value', '护理型')}：应设）"))
        else:
            items.append(("通过", _lbl(s), "专用卫生间是“宜设”（建议项），本间未设，用公用卫生间"))
    return items or _none(p, "相关房间")


def zone_public_wc(p, M):
    """分区内没有专用卫生间的床位，按每 beds_per_wc 床 1 个坐便器核算公用卫生间（每间按 1 个估算）。"""
    label, items = p.get("zone_label", "分区"), []
    rw = p.get("room_usages", ["卧室", "居室"])
    for k, sp in sorted(_zones(M).items()):
        rooms = [s for s in sp if _has(s["usage"], rw)]
        if not rooms:
            continue
        ensuite = {id(s) for s in rooms for d in M.doors if any(y is s for y in d["spaces"]) and any("卫生间" in x["usage"] for x in d["spaces"] if x is not s)}
        beds = sum(len(_beds(s)) for s in rooms if id(s) not in ensuite)
        public_wc = [s for s in sp if "卫生间" in s["usage"] and not any(
            any(y is s for y in d["spaces"]) and any(x in rooms for x in d["spaces"]) for d in M.doors)]
        need = math.ceil(beds / p["beds_per_wc"]) if beds else 0
        st = "不通过" if len(public_wc) < need or not public_wc else "待确认"
        items.append((st, f"{label} {k}", f"没有专用卫生间的床位 {beds} 张，需坐便器 ≥{need} 个；公用卫生间 {len(public_wc)} 间"
                                          f"（按每间 1 个坐便器估算，无障碍厕位待洁具建模后确认）"))
    return items or _none(p, label)


def corridor_width(p, M):
    """走廊净宽给区间：下限是等效矩形短边（门洞凹口会拉低），上限是最大内切圆直径（最宽处）。"""
    items = []
    for s in M.spaces:
        if not _has(s["usage"], p["usages"]) or s["poly"] is None:
            continue
        A, P = s["poly"].area, s["poly"].length
        lo = (P - math.sqrt(max(P * P - 16 * A, 0))) / 4
        hi = 2 * shapely.maximum_inscribed_circle(s["poly"], 5).length
        if lo >= p["min_mm"] - 1:
            st = "通过"
        elif hi < p.get("hard_min_mm", p["min_mm"]) - 1:
            st = "不通过"
        else:
            st = "待确认"
        req = f"要求 ≥{p['min_mm'] / 1000:.2f}" + (f"，确有困难时 ≥{p['hard_min_mm'] / 1000:.2f}" if p.get("hard_min_mm") else "")
        items.append((st, _lbl(s), f"净宽在 {lo / 1000:.2f}～{hi / 1000:.2f} m 之间（墙面到墙面，未扣扶手）；{req}"))
    return items or _none(p, "走廊")


def door_clear_width(p, M):
    """门的开启净宽（按洞口估算）。usages 里的房间的门按 min_mm；room_usages 的房间门按 room_min_mm（护理型按 care_room_min_mm）；
    entrance 指定的两类空间之间的门（如单元入户门）也按 min_mm。"""
    items = []
    rw = p.get("room_usages", [])
    ent = p.get("entrance", [])
    for d in M.doors:
        names = [x["usage"] for x in d["spaces"]]
        entrance = len(ent) == 2 and any(_has(u, ent[0]) for u in names) and any(_has(u, ent[1]) for u in names)
        if not (any(_has(u, p["usages"]) for u in names) or entrance):
            continue
        w, op = d["w"], d["op"]
        if op.startswith("SLIDING"):
            clear, how = (w / 2 - 50 if w >= 1500 else w - p["frame_deduction_mm"]), "推拉门"
        elif op.startswith("DOUBLE"):
            clear, how = (w - p["frame_deduction_mm"]) / 2, "双扇门（单扇）"
        else:
            clear, how = w - p["frame_deduction_mm"], "平开门"
        to_room = rw and any(_has(u, rw) for u in names) and not any("卫生间" in u for u in names)
        care = to_room and any(_is_care(x, p) for x in d["spaces"] if _has(x["usage"], rw))
        need = p.get("care_room_min_mm", p["min_mm"]) if care else (p.get("room_min_mm", p["min_mm"]) if to_room else p["min_mm"])
        what = "房间门（护理型）" if care else ("房间门" if to_room else "门")
        items.append(("通过" if clear >= need - 1 else "不通过", f"{d['e'].Name}（{'/'.join(sorted(set(names)))}）",
                      f"{what}：洞口 {w:.0f}，{how}开启净宽约 {clear:.0f} mm（≥{need}）"))
    return items or _none(p, "相关的门")


CHECKS = {f.__name__: f for f in (
    storey_max_elevation, no_usage_in_basement, exterior_window, max_beds_per_room, room_min_area, area_per_bed, room_area_by_beds,
    bedroom_access, device_in_room, call_near_bed, wet_room_sockets, fire_separation_info, wall_combustibility_info,
    covering_material_info, forbidden_usage, zone_required_rooms, zone_max_beds, zone_area_per_bed, zone_dining_area,
    ensuite_required, zone_public_wc, corridor_width, door_clear_width)}


def run(ifc_path: str, rules_path: str) -> dict:
    with open(rules_path, encoding="utf-8") as fh:
        rules = json.load(fh)
    M = Model(ifc_path)
    out = []
    for rule in rules["rules"]:
        fn = CHECKS.get(rule.get("type", ""))
        if rule["check"] == "manual" or fn is None:
            items = [("人工复核", "-", rule.get("note", "管理、运营或外部条件类要求，图上判不了"))]
        else:
            items = fn(rule.get("params", {}), M)
        worst = min((STATUSES.index(st) for st, _, _ in items), default=STATUSES.index("不涉及"))
        out.append({"id": rule["id"], "clause": rule["clause"], "summary": rule["summary"], "check": rule["check"],
                    "status": STATUSES[worst], "items": [{"status": a, "target": b, "detail": c} for a, b, c in items]})
    facts = {"ifc": os.path.basename(ifc_path), "rules": os.path.basename(rules_path), "standard": rules["standard"],
             "title": rules["title"], "effective": rules.get("effective", ""), "related": rules.get("related", []),
             "source": rules.get("source", ""), "spaces": len(M.spaces), "beds": sum(len(_beds(s)) for s in M.spaces),
             "storeys": [{"name": s["name"], "levels": s["levels"]} for s in M.storeys], "scheme": M.scheme,
             "date": datetime.date.today().isoformat()}
    return {"facts": facts, "results": out}


# ------------------------------------------------------------------ 报告
def to_html(res: dict, path: str, project: str, extra_notes=()):
    F, R = res["facts"], res["results"]
    cnt = {st: sum(1 for r in R if r["status"] == st) for st in STATUSES}
    chip = lambda st: f'<span class="chip" style="background:{COLORS[st]}">{st}</span>'
    e = html.escape
    rows = "".join(f'<tr><td>{e(r["clause"])}</td><td>{e(r["summary"])}</td><td>{ {"auto": "自动", "semi": "半自动", "manual": "人工"}[r["check"]] }</td>'
                   f'<td>{chip(r["status"])}</td><td class="n">{sum(1 for i in r["items"] if i["status"] == "通过")}/{len(r["items"])}</td></tr>' for r in R)
    det = []
    for r in R:
        if r["check"] == "manual":
            continue
        bad = [i for i in r["items"] if i["status"] not in ("通过", "不涉及")]
        good = [i for i in r["items"] if i["status"] in ("通过", "不涉及")]
        det.append(f'<h3>{e(r["clause"])} {chip(r["status"])} <small>{e(r["summary"])}</small></h3>')
        if bad:
            det.append("<table class='d'>" + "".join(f'<tr><td>{chip(i["status"])}</td><td>{e(i["target"])}</td><td>{e(i["detail"])}</td></tr>' for i in bad) + "</table>")
        if good:
            det.append(f'<p class="ok">{"通过" if any(i["status"] == "通过" for i in good) else "不涉及"} {len(good)} 项：'
                       + "；".join(e(f'{i["target"]}（{i["detail"]}）') for i in good[:60]) + (" ……" if len(good) > 60 else "") + "</p>")
    manual = "".join(f'<li>{e(r["clause"])} {e(r["summary"])}' + (f'<br><small>{e(r["items"][0]["detail"])}</small>' if r["items"] else "") + "</li>"
                     for r in R if r["status"] == "人工复核")
    sch = "".join(f"<li>{e(k)}：{e(str(v))}</li>" for k, v in F.get("scheme", {}).items())
    lv = "；".join(f'{s["name"]}：{"、".join(f"{v:.3f}" for v in s["levels"])} m' for s in F["storeys"])
    notes = "".join(f"<li>{e(n)}</li>" for n in extra_notes)
    eff = f"（实施日期 {e(F['effective'])}）" if F.get("effective") else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(F['standard'])} 检查报告</title><style>
@page {{ size: A4; margin: 14mm 13mm; }}
body {{ font-family: 'Microsoft YaHei', 'SimSun', sans-serif; font-size: 10.5pt; color: #222; }}
h1 {{ font-size: 17pt; margin: 0 0 4px; }} h2 {{ font-size: 13pt; border-bottom: 2px solid #333; padding-bottom: 2px; margin-top: 18px; }}
h3 {{ font-size: 11pt; margin: 12px 0 4px; }} h3 small {{ font-weight: normal; color: #555; }}
.chip {{ color: #fff; border-radius: 3px; padding: 0 5px; font-size: 9pt; white-space: nowrap; }}
table {{ border-collapse: collapse; width: 100%; }} td, th {{ border: 1px solid #bbb; padding: 3px 5px; vertical-align: top; }}
th {{ background: #f0f0f0; }} td.n {{ text-align: center; white-space: nowrap; }} table.d td:first-child {{ width: 52px; }}
.sum span {{ margin-right: 12px; }} .ok {{ color: #444; font-size: 9pt; }} .warn {{ background: #fff8e1; border: 1px solid #f0c36d; padding: 6px 10px; }}
ul {{ margin: 4px 0; padding-left: 20px; }}
</style></head><body>
<h1>{e(F['standard'])}《{e(F['title'])}》自动检查报告</h1>
<div>{e(project)} ｜ 模型 {e(F['ifc'])} ｜ 规则 {e(F['rules'])}{eff}｜ {e(F['date'])}</div>
<p class="warn">本报告由程序根据 IFC 模型自动生成，只做设计自查提示，<b>不替代建筑、结构、消防等专业审查</b>；条文概括为规则包作者自拟，原文以标准文本为准。{('<br>条文来源：' + e(F['source'])) if F.get('source') else ''}</p>
<h2>一、结论汇总</h2>
<p class="sum">{''.join(f'<span>{chip(st)} {cnt[st]} 条</span>' for st in STATUSES)}</p>
<table><tr><th>条文</th><th>要求（概括）</th><th>方式</th><th>结论</th><th>通过/检查项</th></tr>{rows}</table>
<h2>二、模型概况</h2>
<ul><li>空间 {F['spaces']} 个，床位 {F['beds']} 张</li><li>楼层与标高：{e(lv)}</li>{sch}{notes}</ul>
<h2>三、逐条明细</h2>{''.join(det)}
<h2>四、人工复核清单</h2><ul>{manual}</ul>
<h2>五、相关标准</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in F['related'])}</ul>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


def to_pdf(html_path: str, pdf_path: str):
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={os.path.abspath(pdf_path)}", url],
                   timeout=180, capture_output=True)
    return os.path.exists(pdf_path) and os.path.getsize(pdf_path)
