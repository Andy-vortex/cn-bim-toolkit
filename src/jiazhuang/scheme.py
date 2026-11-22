"""方案（scheme）数据格式和最简单的方案：把识别结果原样当作方案（现状平面）。

方案是一个 JSON 字典，plan2ifc 和出图流程按下面的键读取（都可以缺省）：
  rooms:   [{id, usage, orig_name, unit, unit_type, zone, public, area_m2, label:[x,y], beds?, care_type?, note?, type?}]
           id 对应识别结果里的房间；usage 是新用途（出图、检查都按它）；zone 是分区编号（出分图、按分区检查）；
           label 是房名标注点（不给就取 label_point）；type 覆盖家具布置用的房间类型（不给按房名查 room_types.json）
  zones:   [{id, ...}]         分区列表（旧方案里叫 cores）；出图时自动切的分段带 auto=True（见 split_zones），只用于出图，
                               不写进 rooms[].zone
  items:   [{key, name, x, y, z, rot, room, params?, plan_id?}]   约定包符号（symbols.json 的键）的实例：床、插座、呼叫按钮……；
           键是 lib:<类型名> 或 cn_<类型> 的是家具（furniture.json；furnish 布置，params 是 cn_ 类型的尺寸参数，plan_id 是布置意图里的 id）；room 是房间 id
  after:   拆改后的识别结果（walls/rooms/openings 都换成改后的几何）；没有拆改就不给
  demolish:[{polygon, reason, from, thickness}]  被拆掉的墙块
  changes: [{type, target, detail, zone}]        拆改清单
  assumptions: {键: 文字}      设计假设，写进 IFC 的 JZ_Scheme
  stats:   {键: 数字或文字}    出图说明里列出来
  levels:  [标高 m, …]        一个标准层代表多层时的全部标高
坐标都是图纸毫米。只依赖 shapely。
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import polylabel


def label_point(polygon) -> list:
    """房间的标注点（图纸毫米）：多边形内部离边最远的点（polylabel）；方案没给 rooms[].label 时也用它。"""
    P = Polygon(polygon)
    lab = polylabel(P, tolerance=50) if P.is_valid and not P.is_empty else P.representative_point()
    return [round(lab.x), round(lab.y)]


def from_recognition(rec: dict) -> dict:
    """识别结果原样当方案：用途 = 图上的房间名，不分区，不加设施。"""
    rooms = []
    for r in rec["rooms"]:
        rooms.append({"id": r["id"], "orig_name": r["name"], "usage": r["name"], "unit": r.get("unit", ""), "unit_type": r.get("unit_type", ""),
                      "zone": "", "public": bool(r.get("public")), "area_m2": r["area_m2"], "beds": 0, "note": "",
                      "label": label_point(r["polygon"])})
    named = [x for x in rooms if x["usage"] != "未标注"]
    return {"source": rec.get("source", ""), "assumptions": {}, "levels": rec.get("levels") or [0.0], "zones": [],
            "rooms": rooms, "items": [], "stats": {"房间": len(named), "户": len(rec.get("units", []))}}


def zone_at(zones: list[dict], x_mm: float) -> str:
    """自动分段的归属规则（split_zones 和 plan2ifc 共用）：x 落在哪一段的 [x_range[0], x_range[1]) 里就归哪段，
    最后一段含右端；落在所有段外面的归最近的一段。房间用标注点（polylabel）的 x，墙用顶点平均 x，门窗用洞口中心 x。"""
    for i, z in enumerate(zones):
        lo, hi = z["x_range"]
        if lo <= x_mm < hi or (i == len(zones) - 1 and x_mm == hi):
            return z["id"]
    return min(zones, key=lambda z: min(abs(x_mm - z["x_range"][0]), abs(x_mm - z["x_range"][1])))["id"]


CUT_EXT_MM = 500.0   # 分段图在内部切口一侧比切口轴多画出去的长度


def _cut_candidates(rec: dict, x0: float, x1: float) -> list[dict]:
    """可以下刀的位置。① 楼面分成几块、中间隔着一条缝（变形缝）：缝本身是一刀，两段各自画到自己那块楼的外墙面，
    两边都按外墙出尺寸；② 轴线：整根轴线在楼里的部分都落在墙上（分户墙）的标 wall=True，切口一侧外伸 CUT_EXT_MM。"""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    parts = [Polygon(g).buffer(0) for g in rec["footprint"] if len(g) >= 3]
    fp = unary_union(parts)
    walls = unary_union([Polygon(w["polygon"]).buffer(0) for w in rec["walls"] if len(w["polygon"]) >= 3])
    y0, y1 = fp.bounds[1] - 10.0, fp.bounds[3] + 10.0
    axes = sorted((rec.get("axes") or {}).get("x", []), key=lambda a: a["pos"])
    # 楼块之间的缝：各块的 x 范围排序合并后，相邻两段之间的空当
    spans = sorted((p.bounds[0], p.bounds[2]) for p in parts if p.area > 0)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    cands = []
    for (_, g0), (g1, _) in zip(merged[:-1], merged[1:]):
        left = [a for a in axes if a["pos"] <= g0 + 1.0]
        right = [a for a in axes if a["pos"] >= g1 - 1.0]
        cands.append({"pos": (g0 + g1) / 2, "lcrop": g0, "rcrop": g1, "kind": "ext", "wall": True, "gap": True,
                      "llabel": left[-1]["label"] if left else None, "rlabel": right[0]["label"] if right else None,
                      "laxis": left[-1]["pos"] if left else g0, "raxis": right[0]["pos"] if right else g1})
    gaps = [(c["lcrop"], c["rcrop"]) for c in cands]
    for a in axes:
        x = a["pos"]
        if not (x0 + CUT_EXT_MM * 2 < x < x1 - CUT_EXT_MM * 2):
            continue
        if any(g0 - CUT_EXT_MM - 200 <= x <= g1 + CUT_EXT_MM + 200 for g0, g1 in gaps):
            continue   # 缝两侧的轴：缝本身已经是一刀
        line = LineString([(x, y0), (x, y1)])
        L_in = line.intersection(fp).length
        L_w = line.intersection(walls).intersection(fp).length if L_in else 0.0
        cands.append({"pos": x, "lcrop": x + CUT_EXT_MM, "rcrop": x - CUT_EXT_MM, "kind": "cut", "gap": False,
                      "wall": bool(L_in) and L_w / L_in >= 0.95, "llabel": a["label"], "rlabel": a["label"],
                      "laxis": x, "raxis": x})
    return sorted(cands, key=lambda c: c["pos"])


def split_zones(rec: dict, sch: dict, max_len_mm) -> list[dict]:
    """楼太长、一张 A3 放不下大比例平面时，沿 x 方向切成几段（分段平面）。

    max_len_mm(左边类型, 右边类型) → 这一段最长多少毫米（"ext"=外墙面，"cut"=内部切口；两种边留给尺寸、轴号的宽度不同），
    也可以直接给一个数。段数取最少、每段都不超过上限（放不下就加一段，不会悄悄变少）。切口优先选楼块之间的缝（变形缝）
    和整进深都落在墙上的轴线（分户墙），都不合适时取离等分点最近的轴；没有轴网就等分。
    分段只记在方案的 zones 里（auto=True），不写 rooms[].zone（那是设计分区，规范检查会按它查）；
    构件归段见 zone_at。段名用切口轴号，如"1～14轴"（没有轴网叫"第1段"、"第2段"……按出图顺序连续编号）。
    每段：x_range 归属范围（切口轴/缝中线），crop 裁切范围（切口外伸 CUT_EXT_MM；首段左端、末段右端、缝两侧到外墙面），
    kinds 左右两边是外墙面还是内部切口，cut 左右切口轴位置（外墙面一侧为 None）。方案里已经有分区时不动，返回原分区。"""
    if sch.get("zones") or sch.get("cores"):
        return sch.get("zones") or sch.get("cores")
    limit = max_len_mm if callable(max_len_mm) else (lambda lk, rk: float(max_len_mm))
    xs = [p[0] for g in rec["footprint"] for p in g]
    x0, x1 = min(xs), max(xs)
    if x1 - x0 <= limit("ext", "ext"):
        return []
    cands = _cut_candidates(rec, x0, x1)
    START = {"pos": x0, "rcrop": x0, "kind": "ext", "rlabel": None, "raxis": None}
    END = {"pos": x1, "lcrop": x1, "kind": "ext", "llabel": None, "laxis": None}

    def seg_ok(a, b):
        lk = "ext" if a is START or a["kind"] == "ext" else "cut"
        rk = "ext" if b is END or b["kind"] == "ext" else "cut"
        return b["lcrop"] - a["rcrop"] <= limit(lk, rk) and b["pos"] > a["pos"]

    best = None
    for k in range(2, 40):
        # 动态规划：第 i 刀落在候选 j 上的最小代价；墙上的切口代价 0～0.3，别的切口 1 + 离等分点的距离
        targets = [x0 + (x1 - x0) * i / k for i in range(1, k)]
        cost = lambda c, t: (0.0 if c["wall"] else 1.0) + 0.3 * abs(c["pos"] - t) / (x1 - x0)
        prev = {None: (0.0, [])}   # 上一刀（None=起点）→ (代价, 刀序列)
        for t in targets:
            cur = {}
            for j, c in enumerate(cands):
                opts = [(pc + cost(c, t), seq + [j]) for pj, (pc, seq) in prev.items()
                        if seg_ok(START if pj is None else cands[pj], c)]
                if opts:
                    cur[j] = min(opts)
            prev = cur
            if not prev:
                break
        fin = [(pc, seq) for pj, (pc, seq) in prev.items() if seg_ok(cands[pj], END)]
        if fin:
            best = [cands[j] for j in min(fin)[1]]
            break
    if best is None:   # 轴线间距太大，怎么切都放不下：退回等分（不再吸附到轴线），由出图时降比例
        n = math.ceil((x1 - x0) / limit("cut", "cut"))
        best = [{"pos": x0 + (x1 - x0) * i / n, "lcrop": x0 + (x1 - x0) * i / n + CUT_EXT_MM, "rcrop": x0 + (x1 - x0) * i / n - CUT_EXT_MM,
                 "kind": "cut", "wall": False, "llabel": None, "rlabel": None, "laxis": x0 + (x1 - x0) * i / n,
                 "raxis": x0 + (x1 - x0) * i / n} for i in range(1, n)]
    axes = sorted((rec.get("axes") or {}).get("x", []), key=lambda a: a["pos"])
    seq = [START] + best + [END]
    zones = []
    for i, (a, b) in enumerate(zip(seq[:-1], seq[1:])):
        first = a["rlabel"] if a is not START else (axes[0]["label"] if axes else None)
        last = b["llabel"] if b is not END else (axes[-1]["label"] if axes else None)
        zid = f"{first}～{last}轴" if first and last and first != last else f"第{i + 1}段"
        zones.append({"id": zid, "auto": True,
                      "x_range": [x0 - 1.0 if a is START else a["pos"], x1 + 1.0 if b is END else b["pos"]],
                      "crop": [a["rcrop"], b["lcrop"]],
                      "kinds": ["ext" if a is START or a["kind"] == "ext" else "cut", "ext" if b is END or b["kind"] == "ext" else "cut"],
                      "cut": [None if a is START or a["kind"] == "ext" else a["raxis"], None if b is END or b["kind"] == "ext" else b["laxis"]],
                      "wall_cut": [bool(a is not START and a.get("wall")), bool(b is not END and b.get("wall"))],
                      "axis_x": [a["raxis"] if a is not START else (axes[0]["pos"] if axes else x0),
                                 b["laxis"] if b is not END else (axes[-1]["pos"] if axes else x1)]})
    sch["zones"] = zones
    return zones
