"""示例户型（合成数据，用于开发和测试约定包；不是任何真实房子）。

外轮廓 6800 x 4000（毫米，x 向东、y 向北）：左边客厅，右上卫生间，右下玄关。
- 外墙 200 厚，材质 cn-bearing（按承重墙画法：实心涂黑）；
- 隔墙 100 厚，材质 cn-partition（按新砌隔墙画法：斜线填充）。
门窗用 ifcopenshell 的参数化门窗（注意：门窗接口用项目单位毫米，墙接口用米）。
水电点位、灯具用 symbols.py 的类型，图上自动出国标习惯符号；家具和洁具按示例布置意图（packs/cn/furniture/example-intent.json）摆。
recognition() 把同一套墙、门窗、房间写成识别结果的格式（dxfplan 的输出），家具布置（furnish）和 make_plan_set 能直接用：
  python -m jiazhuang.demo_apartment <识别.json>
"""
from __future__ import annotations

import importlib
import json
import math
import os

import numpy as np

try:   # 建 IFC（build）在 Blender 里跑；recognition() 只要 shapely，.venv 里也能跑
    import ifcopenshell
    import ifcopenshell.api as api
    from . import symbols as sym
    for _m in ("project", "root", "unit", "context", "aggregate", "geometry", "spatial", "feature", "material", "pset"):
        importlib.import_module("ifcopenshell.api." + _m)
except ImportError:
    ifcopenshell = None

M = 0.001
H = 2800  # 层高（结构顶）毫米
# 墙（轴线坐标，毫米）：名称、起点、终点、厚、材质
WALLS = (("外墙-南", (0, 100), (6800, 100), 200, "cn-bearing"),
         ("外墙-北", (6800, 3900), (0, 3900), 200, "cn-bearing"),
         ("外墙-东", (6700, 200), (6700, 3800), 200, "cn-bearing"),
         ("外墙-西", (100, 3800), (100, 200), 200, "cn-bearing"),
         ("隔墙-客厅/卫生间", (4600, 200), (4600, 3800), 100, "cn-partition"),
         ("隔墙-卫生间/玄关", (4650, 2000), (6600, 2000), 100, "cn-partition"))
# 门：所在墙、洞口起点离墙起点、宽、名称、开启方式（门扇开向墙方向的左侧）；窗：所在墙、起点、宽、高、窗台、名称
DOORS = (("外墙-南", 5200, 900, "入户门 M1", "SINGLE_SWING_LEFT"),
         ("隔墙-客厅/卫生间", 800, 900, "客厅门 M2", "SINGLE_SWING_RIGHT"),
         ("隔墙-卫生间/玄关", 300, 700, "卫生间门 M3", "SINGLE_SWING_LEFT"))
WINDOWS = (("外墙-西", 900, 1800, 1500, 900, "客厅窗 C1"),
           ("外墙-北", 900, 900, 600, 1500, "卫生间高窗 C2"))
SPACES = (("客厅", 200, 200, 4550, 3800), ("卫生间", 4650, 2050, 6600, 3800), ("玄关", 4650, 200, 6600, 1950))


class Builder:
    def __init__(self, pack_symbols: str, project_name="示例户型"):
        f = self.f = api.project.create_file(version="IFC4")
        self.project = api.root.create_entity(f, ifc_class="IfcProject", name=project_name)
        api.unit.assign_unit(f)  # 毫米
        model = api.context.add_context(f, context_type="Model")
        self.body = api.context.add_context(f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model)
        plan = api.context.add_context(f, context_type="Plan")
        self.plan_body = api.context.add_context(f, context_type="Plan", context_identifier="Body", target_view="PLAN_VIEW", parent=plan)
        api.context.add_context(f, context_type="Plan", context_identifier="Annotation", target_view="PLAN_VIEW", parent=plan)
        site = api.root.create_entity(f, ifc_class="IfcSite", name="场地")
        bldg = api.root.create_entity(f, ifc_class="IfcBuilding", name="示例楼")
        self.storey = api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="1F")
        api.aggregate.assign_object(f, relating_object=self.project, products=[site])
        api.aggregate.assign_object(f, relating_object=site, products=[bldg])
        api.aggregate.assign_object(f, relating_object=bldg, products=[self.storey])
        for p in (site, bldg, self.storey):
            api.geometry.edit_object_placement(f, product=p)
        self.mat = {
            "cn-bearing": api.material.add_material(f, name="cn-bearing", category="concrete"),
            "cn-partition": api.material.add_material(f, name="cn-partition", category="block"),
        }
        self.types = sym.create_types(f, sym.load_pack(pack_symbols))
        self.walls = {}

    # ---------- helpers ----------
    def place(self, product, x, y, z=0.0, rot_deg=0.0):
        a = math.radians(rot_deg)
        m = np.array([[math.cos(a), -math.sin(a), 0, x * M], [math.sin(a), math.cos(a), 0, y * M],
                      [0, 0, 1, z * M], [0, 0, 0, 1]], dtype=float)
        api.geometry.edit_object_placement(self.f, product=product, matrix=m)

    def contain(self, p):
        api.spatial.assign_container(self.f, relating_structure=self.storey, products=[p])

    def wall(self, name, a, b, t, material):
        f = self.f
        w = api.root.create_entity(f, ifc_class="IfcWall", name=name)
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        rep = api.geometry.add_wall_representation(f, context=self.body, length=L * M, height=H * M,
                                                   thickness=t * M, offset=-t / 2 * M)
        api.geometry.assign_representation(f, product=w, representation=rep)
        self.place(w, a[0], a[1], 0, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])))
        api.material.assign_material(f, products=[w], material=self.mat[material])
        self.contain(w)
        self.walls[name] = (w, a, b, t)
        return w

    def _frame_on_wall(self, wall_name, dist):
        """沿墙轴线 dist 处：返回 (轴线点, 方向单位向量, 法向单位向量, 墙厚)。法向 = 方向逆时针转 90°。"""
        w, a, b, t = self.walls[wall_name]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        return (a[0] + ux * dist, a[1] + uy * dist), (ux, uy), (-uy, ux), t

    def _opening(self, wall_name, dist, width, height, sill):
        f = self.f
        w = self.walls[wall_name][0]
        (px, py), (ux, uy), _, t = self._frame_on_wall(wall_name, dist)
        op = api.root.create_entity(f, ifc_class="IfcOpeningElement", name="洞口")
        rep = api.geometry.add_wall_representation(f, context=self.body, length=width * M, height=height * M,
                                                   thickness=(t + 40) * M, offset=-(t + 40) / 2 * M)
        api.geometry.assign_representation(f, product=op, representation=rep)
        self.place(op, px, py, sill, math.degrees(math.atan2(uy, ux)))
        api.feature.add_feature(f, feature=op, element=w)
        return op

    def door(self, wall_name, dist, width, name, op_type="SINGLE_SWING_LEFT", height=2100):
        f = self.f
        op = self._opening(wall_name, dist, width, height, 0)
        (px, py), (ux, uy), (nx, ny), t = self._frame_on_wall(wall_name, dist)
        d = api.root.create_entity(f, ifc_class="IfcDoor", name=name)
        d.PredefinedType, d.OperationType = "DOOR", op_type
        d.OverallWidth, d.OverallHeight = width, height
        lining = {"LiningDepth": t, "LiningThickness": 50.0, "LiningOffset": 0.0}
        for ctx in (self.body, self.plan_body):  # 门窗接口：项目单位（毫米）
            rep = api.geometry.add_door_representation(f, context=ctx, overall_height=height, overall_width=width,
                                                       operation_type=op_type, lining_properties=lining)
            api.geometry.assign_representation(f, product=d, representation=rep)
        # 门的局部原点放在墙的一个面上（轴线往法向反方向退半个墙厚），门体沿 +y 穿过墙厚
        self.place(d, px - nx * t / 2, py - ny * t / 2, 0, math.degrees(math.atan2(uy, ux)))
        api.feature.add_filling(f, opening=op, element=d)
        self.contain(d)
        return d

    def window(self, wall_name, dist, width, height, sill, name):
        f = self.f
        op = self._opening(wall_name, dist, width, height, sill)
        (px, py), (ux, uy), (nx, ny), t = self._frame_on_wall(wall_name, dist)
        win = api.root.create_entity(f, ifc_class="IfcWindow", name=name)
        win.PredefinedType, win.PartitioningType = "WINDOW", "SINGLE_PANEL"
        win.OverallWidth, win.OverallHeight = width, height
        lining = {"LiningDepth": 80.0, "LiningThickness": 50.0, "LiningOffset": (t - 80) / 2}
        for ctx in (self.body, self.plan_body):
            rep = api.geometry.add_window_representation(f, context=ctx, overall_height=height, overall_width=width,
                                                         partition_type="SINGLE_PANEL", lining_properties=lining)
            api.geometry.assign_representation(f, product=win, representation=rep)
        self.place(win, px - nx * t / 2, py - ny * t / 2, sill, math.degrees(math.atan2(uy, ux)))
        api.feature.add_filling(f, opening=op, element=win)
        self.contain(win)
        pset = api.pset.add_pset(f, product=win, name="Pset_WindowCommon")
        api.pset.edit_pset(f, pset=pset, properties={"IsExternal": wall_name.startswith("外墙")})
        return win

    def box(self, ifc_class, name, x, y, z, dx, dy, dz, predefined=None, rot=0.0):
        f = self.f
        e = api.root.create_entity(f, ifc_class=ifc_class, name=name, predefined_type=predefined)
        v = [(0, 0, 0), (dx, 0, 0), (dx, dy, 0), (0, dy, 0), (0, 0, dz), (dx, 0, dz), (dx, dy, dz), (0, dy, dz)]
        v = [tuple(c * M for c in p) for p in v]
        faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        rep = api.geometry.add_mesh_representation(f, context=self.body, vertices=[v], faces=[faces])
        api.geometry.assign_representation(f, product=e, representation=rep)
        self.place(e, x, y, z, rot)
        self.contain(e)
        return e

    def space(self, name, x0, y0, x1, y1, h=H):
        f = self.f
        sp = api.root.create_entity(f, ifc_class="IfcSpace", name=name)
        sp.LongName = name
        rep = api.geometry.add_wall_representation(f, context=self.body, length=(x1 - x0) * M, height=h * M,
                                                   thickness=(y1 - y0) * M)
        api.geometry.assign_representation(f, product=sp, representation=rep)
        self.place(sp, x0, y0)
        api.aggregate.assign_object(f, relating_object=self.storey, products=[sp])
        return sp

    def point(self, key, name, x, y, z=None, rot=0.0):
        spec_h = self.types[key]
        if z is None:
            z = 0.0
        return sym.place_occurrence(self.f, self.types, key, name, x, y, z, rot, container=self.storey)


def build(out_path: str, pack_symbols: str) -> str:
    b = Builder(pack_symbols)
    for w in WALLS:
        b.wall(*w)
    for d in DOORS:
        b.door(*d)
    for w in WINDOWS:
        b.window(*w)
    for s in SPACES:
        b.space(*s)
    # 楼板、吊顶
    b.box("IfcSlab", "楼板", 0, 0, -120, 6800, 4000, 120, "FLOOR")
    c1 = b.box("IfcCovering", "石膏板边吊", 200, 200, 2600, 4350, 3600, 20, "CEILING")
    c2 = b.box("IfcCovering", "铝扣板吊顶", 4650, 2050, 2400, 1950, 1750, 10, "CEILING")
    # 家具、洁具：只有一处定义，就是示例布置意图（和单独的家具布置流程同一份）
    from . import furnish, plan2ifc
    with open(os.path.join(furnish.PACK, "example-intent.json"), encoding="utf-8") as fh:
        items, errors = furnish.solve(json.load(fh), recognition())
    if errors:
        raise ValueError(f"示例布置意图摆不了：{errors[0]['msg']}")
    cache = {}
    for it in items:
        plan2ifc.place_item(b.f, cache, it, it["x"], it["y"], it["z"], b.storey)
    # 地漏（局部 +y 指向室内）
    b.point("cn_floor_drain", "地漏", 5600, 2600, 0)
    # 插座、开关
    b.point("cn_socket", "插座 S1 电视", 1700, 200, 300, 0)
    b.point("cn_socket", "插座 S2 电视", 2700, 200, 300, 0)
    b.point("cn_socket", "插座 S3 沙发左", 450, 3800, 300, 180)
    b.point("cn_socket", "插座 S4 沙发右", 3400, 3800, 300, 180)
    b.point("cn_socket", "插座 S5", 4550, 2800, 300, 90)         # 隔墙客厅侧
    b.point("cn_socket_splash", "防溅插座 S6 台盆", 4650, 2700, 1500, -90)
    b.point("cn_socket_splash", "防溅插座 S7 坐便", 6600, 3300, 400, 90)
    b.point("cn_switch_2", "双联开关 K1 客厅", 4550, 1850, 1300, 90)
    b.point("cn_switch_1", "单联开关 K2 卫生间", 5800, 1950, 1300, 180)
    b.point("cn_switch_1", "单联开关 K3 玄关", 6600, 450, 1300, 90)
    # 灯具
    b.point("cn_light_ceiling", "吸顶灯 L1", 2375, 2000, 2580)
    b.point("cn_downlight", "筒灯 L2", 5300, 2950, 2390)
    b.point("cn_downlight", "筒灯 L3", 6000, 2950, 2390)
    b.point("cn_downlight", "筒灯 L4", 5600, 1000, 2740)
    b.f.write(out_path)
    return out_path


def recognition() -> dict:
    """同一套墙、门窗、房间写成识别结果（dxfplan 的格式）：墙在洞口处断开，门窗记洞口中心、墙的方向和开启一侧，
    轴网取外墙和隔墙的轴线。"""
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union
    axis = {w[0]: w for w in WALLS}
    rooms = [{"id": f"R{i}", "name": n, "names": [n], "area_m2": round((x1 - x0) * (y1 - y0) / 1e6, 2),
              "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], "unit": "U1", "unit_type": "", "public": False,
              "labeled": True, "windows": [], "ext_windows": [], "doors": []} for i, (n, x0, y0, x1, y1) in enumerate(SPACES, 1)]
    ops, cuts = [], {}
    for kind, items in (("door", DOORS), ("window", WINDOWS)):
        for it in items:
            wall, dist, width, name = it[0], it[1], it[2], (it[3] if kind == "door" else it[5])
            _, (ax, ay), (bx, by), t, _ = axis[wall]
            L = math.hypot(bx - ax, by - ay)
            u = ((bx - ax) / L, (by - ay) / L)
            p0, p1 = (ax + u[0] * dist, ay + u[1] * dist), (ax + u[0] * (dist + width), ay + u[1] * (dist + width))
            cut = LineString([p0, p1]).buffer(t / 2 + 1, cap_style="flat")
            cuts.setdefault(wall, []).append(cut)
            near = [r for r in rooms if box(*r["polygon"][0], *r["polygon"][2]).intersects(cut.buffer(10))]
            oid = name.split()[-1]
            for r in near:
                r["doors" if kind == "door" else "windows"].append(oid)
                if kind == "window" and wall.startswith("外墙"):
                    r["ext_windows"].append(oid)
            ops.append({"id": oid, "kind": kind, "block": "", "x": (p0[0] + p1[0]) / 2, "y": (p0[1] + p1[1]) / 2,
                        "rot": round(math.degrees(math.atan2(u[1], u[0])), 3) % 360.0, "width": float(width),
                        "depth": float(t) if kind == "window" else 0.0, "label": "", "label_raw": "", "exterior": wall.startswith("外墙"),
                        "rooms": [r["id"] for r in near], "polygon": [], "sx": 1, "sy": 1, "wall_t": float(t),
                        "leafs": 1 if kind == "door" else -1, "swing": 1,
                        "hinge_end": (-1 if it[4] == "SINGLE_SWING_LEFT" else 1) if kind == "door" else 0,
                        **({} if kind == "door" else {"sill": float(it[4])})})
    walls = []
    for name, a, b, t, _ in WALLS:
        g = LineString([a, b]).buffer(t / 2, cap_style="flat").difference(unary_union(cuts.get(name, [])))
        for p in getattr(g, "geoms", [g]):
            walls.append({"id": f"W{len(walls) + 1}", "thickness": float(t), "length": round(p.length / 2 - t, 1),
                          "polygon": [[round(x, 1), round(y, 1)] for x, y in p.exterior.coords], "exterior": name.startswith("外墙"),
                          "keep": True, "reason": name})
    return {"source": "demo_apartment", "region": [0, 0, 6800, 4000], "footprint": [[[0, 0], [6800, 0], [6800, 4000], [0, 4000], [0, 0]]],
            "footprint_area_m2": 27.2, "declared_area_m2": None, "levels": [0.0],
            "units": [{"id": "U1", "type": "", "area_m2": round(sum(r["area_m2"] for r in rooms), 2), "rooms": [r["id"] for r in rooms]}],
            "rooms": rooms, "walls": walls, "openings": ops,
            "axes": {"x": [{"label": str(i), "pos": float(x), "span": [100, 3900], "ends": ["bottom", "top"]} for i, x in enumerate((100, 4600, 6700), 1)],
                     "y": [{"label": k, "pos": float(y), "span": [100, 6700], "ends": ["left", "right"]} for k, y in (("A", 100), ("B", 3900))]}}


if __name__ == "__main__":
    import argparse
    from .printing import done
    ap = argparse.ArgumentParser(prog="python -m jiazhuang.demo_apartment", allow_abbrev=False,
                                 description="把示例户型写成识别结果 JSON（dxfplan 的格式），给家具布置和 make_plan_set 用。")
    ap.add_argument("out", metavar="识别.json")
    out = ap.parse_args().out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(recognition(), fh, ensure_ascii=False, indent=1)
    done(out)
