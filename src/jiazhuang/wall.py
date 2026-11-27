"""墙面：每间房每面墙的做法、底高顶高、墙面轮廓、墙砖排砖、出不出立面和立面字母（只依赖 shapely，在 .venv 里能跑）。

规则表 packs/cn/wall/rules.json：材料（乳胶漆、墙砖、壁纸、踢脚线）、房间类型 → 缺省做法、出哪些墙的立面、排砖门槛、校验门槛。
意图（可选）一间房一条，按墙边编号逐面改，不写坐标：
  {"elevations": "finished" | "all" | "intent",
   "rooms": [{"room": 房名或房间 id, "finish": 材料键, "dir": "h" | "v", "top": "full" | "ceiling+100" | 毫米,
              "skirting": 踢脚线材料键 | true | false, "size": [短边, 长边], "joint": 缝宽, "elev": ["W2", …],
              "walls": {"W1": {"finish": …, "dir": …, "top": …, "skirting": …, "size": …, "joint": …, "elev": true | false, "note": 文字}}}]}
墙边就是 scripts/furnish.py context 给的 W1..Wn（furnish.rooms_of：去掉门洞条，逆时针，从最南再最西的角起）。
两套坐标：平面是图纸毫米（同识别结果）；墙面 (u, v) 是面向这面墙看，u 从左端（墙边的终点 b）往右量，v 从本层楼面往上量（毫米），
墙边的起点 a 在右端（u = 墙长）。
方案的 walls 每面墙一条：做法、底高 bottom（这间房的地面完成面，湿区降板）、顶高 top（到原顶 / 吊顶底以上 n / 意图写的数，不超过
原顶；写"吊顶底以上 n"的墙砖，吊顶底以上 checks.over_min_mm～n 之间有横缝时停在这道缝上）、墙面轮廓 parts（墙面矩形减去门窗洞、减去背后没有墙的开敞段）、墙砖的排砖网格 grid（dir 0、origin 一个缝交点、pitch 缝距，
和 JZ_Flooring 同一套：flooring.joint_lines(grid, 墙面轮廓) 现算砖缝）、踢脚线、要不要出立面 elev、立面字母 letter。
背后没有墙的墙边（客餐厅之间的开敞边）不分做法（open: true），只编字母。排砖是确定性的换算，规则见 packs/cn/wall/README.md。"""
from __future__ import annotations

import json
import math
import os
import re

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from . import ceiling, conventions, flooring, furnish, openings

PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "wall"))
with open(os.path.join(PACK, "rules.json"), encoding="utf-8") as _fh:
    _R = json.load(_fh)
SOURCES, MATERIALS, ROOMS, ELEVATIONS, LAYOUT, CHECKS = (
    _R[k] for k in ("sources", "materials", "rooms", "elevations", "layout", "checks"))
SPEC_KEYS = ("finish", "dir", "top", "skirting", "size", "joint")
ROOM_KEYS = ("room",) + SPEC_KEYS + ("elev", "walls")
WALL_KEYS = SPEC_KEYS + ("elev", "note")
MODES = ("finished", "all", "intent")
TOP_RE = re.compile(r"ceiling(?:\+(\d+(?:\.\d+)?))?")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
with open(conventions.pack_file("symbols.json"), encoding="utf-8") as _fh:
    _SYM = json.load(_fh)["types"]
POINTS = {k: s["body"] for k, s in _SYM.items() if s.get("ifc_type") in CHECKS["point_types"]}   # 墙上装的点位 → 盒子 [宽, 厚, 高]
FIXED = set(ELEVATIONS["fixed"])


class IntentError(ValueError):
    pass


def check_intent(intent) -> None:
    """整份意图的结构：对象，rooms 是对象列表，每块的 walls 是对象。不对抛 IntentError（一行原因）。"""
    if not isinstance(intent, dict):
        raise IntentError("墙面意图要是 JSON 对象")
    if not isinstance(intent.get("rooms"), list) or not all(isinstance(b, dict) for b in intent["rooms"]):
        raise IntentError("rooms 要是对象列表：[{\"room\": …, \"finish\": …}]")
    for b in intent["rooms"]:
        if not isinstance(b.get("walls", {}), dict):
            raise IntentError(f"房间 {b.get('room')} 的 walls 要是对象：{{\"W1\": {{\"finish\": …}}}}")


def _r1(v):
    return round(float(v), 1)


def finishes() -> list:
    return [k for k, m in MATERIALS.items() if m["kind"] != "skirting"]


def skirtings() -> list:
    return [k for k, m in MATERIALS.items() if m["kind"] == "skirting"]


def label(w) -> str:
    """墙面做法在图上、表里的写法："墙砖 300×600""白色乳胶漆"。"""
    return f"{w['material']} {w['size'][0]:g}×{w['size'][1]:g}" if w.get("size") else w["material"]


def letter(j: int) -> str:
    return LETTERS[j] if j < len(LETTERS) else f"Z{j - len(LETTERS) + 1}"


def letters(ring) -> dict:
    """立面字母：外法线最接近正北（图纸上方）的那面为 A，顺时针（墙边编号倒着走）B、C、D…，多于四面接着 E、F…；
    房间里每面墙都编（开敞边、不出立面的也编），字母不随出哪几面变。一样朝北的取长的，再取靠北、靠西的。"""
    def dev(w):
        ang = math.degrees(math.atan2(-w["n"][1], -w["n"][0]))
        return abs((ang - 90.0 + 180.0) % 360.0 - 180.0)
    mid = lambda w: ((w["a"][0] + w["b"][0]) / 2, (w["a"][1] + w["b"][1]) / 2)
    k = min(range(len(ring)), key=lambda i: (round(dev(ring[i]), 1), -round(ring[i]["len"]), -round(mid(ring[i])[1]), round(mid(ring[i])[0])))
    return {ring[(k - j) % len(ring)]["id"]: letter(j) for j in range(len(ring))}


# 门窗洞口的竖向范围 (底, 顶)（离本层楼面，毫米；门的底是 None）：和建模、吊顶、水电同一份 openings.rec_extent，墙面上挖的洞
# 和 IFC 里的门窗才对得上
opening_extent = openings.rec_extent


def _top(rule, ceil, H) -> float:
    """顶高（毫米）：数字照写，"full" 到原顶，"ceiling+n" 到这面墙的吊顶底以上 n（没有吊顶到原顶）；都不超过原顶 H。"""
    if isinstance(rule, (int, float)) and not isinstance(rule, bool):
        return min(float(rule), H)
    m = TOP_RE.fullmatch(rule) if isinstance(rule, str) else None
    if m is None or ceil is None:
        return float(H)
    return min(ceil + float(m.group(1) or 0.0), H)


# ---------------------------------------------------------------- 一面墙的几何
def _context(rec, sch) -> dict:
    """生成和过时检查共用的输入：墙和洞口（判断墙边背后有没有墙）、门窗、拆改前的洞口宽、各房间的吊顶块、地面完成面、items。"""
    geo = sch.get("after") or rec
    solid = [Polygon(w["polygon"]).buffer(0) for w in geo["walls"] if len(w["polygon"]) >= 3]
    for o in geo["openings"]:
        if o.get("polygon"):
            solid.append(Polygon(o["polygon"]).buffer(0))
        else:
            c, u, _, w, t, _ = openings.rec_frame(o)
            solid.append(openings.oriented_box(c[0], c[1], u[0], u[1], w / 2, t / 2))
    solid += [Polygon(c["polygon"]).buffer(0) for c in geo.get("columns") or [] if len(c.get("polygon") or []) >= 3]
    ceils = {}
    for b in sch.get("ceilings") or []:
        ceils.setdefault(b["room"], []).append(b)
    level = {r: float(z.get("level") or 0.0) for z in (sch.get("flooring") or {}).get("zones", []) for r in z["rooms"]}
    return {"sch": sch, "solid": unary_union(solid), "ops": geo["openings"], "width0": {o["id"]: float(o["width"]) for o in rec["openings"]},
            "ceilings": ceils, "level": level, "items": sch.get("items", [])}


def _backed(w, solid) -> list:
    """墙边背后有墙（或门窗洞口）的区段 [(s0, s1)]（从起点 a 量，毫米）：墙边外侧 checks.wall_gap_mm 宽的一条和墙、洞口相交的
    部分投到墙边上；相隔不到 checks.open_min_mm 的连起来（墙角、墙段接缝处的小缝不算开敞）。"""
    a, u, n, L = np.array(w["a"]), np.array(w["u"]), np.array(w["n"]), w["len"]
    g = CHECKS["wall_gap_mm"]
    strip = Polygon([a + n * 5, a + u * L + n * 5, a + u * L - n * g, a - n * g])
    hit = strip.intersection(solid)
    spans = []
    for p in getattr(hit, "geoms", [hit]):
        if p.is_empty or p.geom_type != "Polygon" or p.area < 1.0:
            continue
        s = [float((np.array(c) - a) @ u) for c in p.exterior.coords]
        spans.append([max(0.0, min(s)), min(L, max(s))])
    spans.sort()
    gap = CHECKS["open_min_mm"]
    out = []
    for s0, s1 in spans:
        if out and s0 <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], s1)
        else:
            out.append([s0, s1])
    if out and out[0][0] < gap:
        out[0][0] = 0.0
    if out and L - out[-1][1] < gap:
        out[-1][1] = L
    return out


def _open_u(spans, L) -> list:
    """背后有墙的区段 → 开敞段（墙面坐标 u，从左端量）。"""
    out, s = [], 0.0
    for s0, s1 in spans:
        if s0 - s >= CHECKS["open_min_mm"]:
            out.append((s, s0))
        s = max(s, s1)
    if L - s >= CHECKS["open_min_mm"]:
        out.append((s, L))
    return sorted([_r1(L - b), _r1(L - a)] for a, b in out)


def _ceiling_at(w, blocks, H):
    """这面墙的吊顶底（毫米）：贴着墙（checks.ceiling_gap_mm 以内）的吊顶块里最低的底；贴原顶的块（窗帘盒）不算。吊顶只挡住这面墙
    不到 checks.ceiling_cover 的长度时按没有吊顶算（贴到原顶才不露墙）。没有吊顶返回 None。"""
    seg = LineString([w["a"], w["b"]])
    hits = []
    for b in blocks:
        if float(b["bottom"]) >= H - 0.5:
            continue
        s = seg.intersection(Polygon(b["polygon"], b.get("holes") or []).buffer(CHECKS["ceiling_gap_mm"]))
        if s.length > 1.0:
            hits.append((float(b["bottom"]), s))
    if not hits or unary_union([s for _, s in hits]).length < CHECKS["ceiling_cover"] * w["len"]:
        return None
    return min(b for b, _ in hits)


def _openings_on(w, ctx) -> list:
    """这面墙上的门窗洞口（墙面坐标）：[{id, kind, from, to, bottom, top}]，bottom 为 None 是落地的门洞。"""
    out = []
    L = w["len"]
    for o in ctx["ops"]:
        span = furnish.on_wall(w, o)
        if span is None:
            continue
        v0, v1 = opening_extent(o, ctx["width0"].get(o["id"]))
        out.append({"id": o["id"], "kind": o["kind"], "from": _r1(L - span[1]), "to": _r1(L - span[0]),
                    "bottom": None if v0 is None else _r1(v0), "top": _r1(v1)})
    return sorted(out, key=lambda x: (x["from"], x["id"]))


def _assign_points(ring, rid, items) -> dict:
    """这间房墙上装的点位（rules checks.point_types 的类型）归到墙边：离墙线不超过 checks.point_wall_mm、投影在墙边上；墙角处
    两面都够得着的取朝向一致（点位局部 +y 和墙的室内法线同向）、再取离得近的。→ {墙边 id: [{id, key, u, z, w, h}]}。"""
    out = {}
    lim = CHECKS["point_wall_mm"]
    for it in items:
        if it.get("room") != rid or it.get("key") not in POINTS:
            continue
        p = np.array([float(it["x"]), float(it["y"])])
        a_ = math.radians(float(it.get("rot") or 0.0))
        face = np.array([-math.sin(a_), math.cos(a_)])
        best = None
        for w in ring:
            a, u, n = np.array(w["a"]), np.array(w["u"]), np.array(w["n"])
            s, d = float((p - a) @ u), float((p - a) @ n)
            if -1.0 <= s <= w["len"] + 1.0 and abs(d) <= lim:
                k = (-round(float(face @ n), 3), round(abs(d), 1))
                if best is None or k < best[0]:
                    best = (k, w, s)
        if best is None:
            continue
        _, w, s = best
        bw, _, bh = POINTS[it["key"]]
        out.setdefault(w["id"], []).append({"id": it.get("plan_id") or it.get("name") or it["key"], "key": it["key"], "u": _r1(w["len"] - s),
                                            "z": _r1(it.get("z") or 0.0), "w": float(bw), "h": float(bh)})
    for v in out.values():
        v.sort(key=lambda p: (p["u"], p["z"], p["id"]))
    return out


def _fixed_on(w, rid, items) -> list:
    """背靠这面墙的柜体、固定家具（elevations.fixed）：靠墙边和墙平行、离墙线不超过 fixed_gap_mm、沿墙和墙边有重叠。→ [plan_id]。"""
    a, u, n, L = np.array(w["a"]), np.array(w["u"]), np.array(w["n"]), w["len"]
    out = []
    for it in items:
        if it.get("room") != rid or it.get("key") not in FIXED or it["key"] not in furnish.TYPES:
            continue
        p, _, fn = furnish.frame(it)
        if float(fn @ n) < 0.99:
            continue
        d, s = float((p - a) @ n), float((p - a) @ u)
        half = furnish.geom(it["key"], it.get("params"))["width"] / 2
        if -10.0 <= d <= ELEVATIONS["fixed_gap_mm"] and min(L, s + half) - max(0.0, s - half) > 1.0:
            out.append(it.get("plan_id") or it.get("name") or it["key"])
    return sorted(out)


def _cuts(L, bottom, top, ops, open_u) -> list:
    """墙面上要挖掉的：门窗洞口（门洞落地）、开敞段（整高）。"""
    cut = []
    for o in ops:
        lo = bottom - 1.0 if o["bottom"] is None else o["bottom"]
        if o["to"] - o["from"] > 0.5 and o["top"] - lo > 0.5:
            cut.append(box(o["from"], lo, o["to"], o["top"]))
    cut += [box(u0, bottom - 1.0, u1, top + 1.0) for u0, u1 in open_u]
    return cut


def _parts(g) -> list:
    out = []
    for p in getattr(g, "geoms", [g]):
        if p.geom_type == "Polygon" and p.area >= 1.0:
            ext, holes = flooring._poly_coords(p)
            out.append({"polygon": ext, "holes": holes})
    return out


def face(e) -> Polygon:
    """方案里一面墙（或它的踢脚线）的墙面轮廓 → shapely（墙面坐标，毫米）。"""
    return unary_union([Polygon(p["polygon"], p.get("holes") or []) for p in e.get("parts") or []])


def _turn(p, q) -> float:
    """两条墙边交点处的转向：> 0 左转（房间轮廓逆时针，凸角：室内看是阴角），< 0 右转（阳角）。"""
    return p["u"][0] * q["u"][1] - p["u"][1] * q["u"][0]


def _ends(ring, i, opn, open_u) -> tuple:
    """这面墙两端露不露在外面：(左端, 右端)。阳角、旁边是开敞边或开敞段算露着；阴角藏得住。"""
    w, n = ring[i], len(ring)
    left = _turn(w, ring[(i + 1) % n]) < -1e-6 or opn[(i + 1) % n] or any(u0 < 1.0 for u0, _ in open_u)
    right = _turn(ring[i - 1], w) < -1e-6 or opn[i - 1] or any(u1 > w["len"] - 1.0 for _, u1 in open_u)
    return left, right


def _start(ends, L, ops) -> str:
    """墙砖沿墙从哪起："left" 左端整砖、"right" 右端整砖、"center" 对中。一端露着、另一端藏得住（阴角，或门洞边离这一端不到
    layout.door_end_mm：门后）时整砖从露着的一端起，非整砖落在藏得住的一端；两端一样时对中，两头的非整砖对称。"""
    lim = LAYOUT["door_end_mm"]
    doors = [o for o in ops if o["bottom"] is None]
    left = ends[0] and not any(o["from"] <= lim for o in doors)
    right = ends[1] and not any(o["to"] >= L - lim for o in doors)
    return "left" if left and not right else "right" if right and not left else "center"


def _v_origin(bottom, top, ceil, size, joint, d) -> float:
    """竖向网格的起点（一道横缝的 v）。和沿墙同一个思路：整砖从露着的一端起，非整砖落在另一端；非整砖（含半条缝）小于
    layout.min_edge_mm + 半条缝时，网格往起铺那端挪，让它正好等于这个数。
      贴进吊顶（top 高过这面墙的吊顶底 ceil）：起铺端是吊顶底，一条缝对齐吊顶底、往下整砖，非整砖落在最下一排（贴地面完成面）；
      最下一排窄了网格往上挪，这时吊顶底下露出的最上一排是一块整砖的下半截，切口藏在吊顶里。
      没有吊顶、砖不到吊顶（贴到原顶或某个高度）：从地面完成面整砖起，最上一排切；最上一排窄了网格往下挪，最下一排跟着变成
      非整砖。
    墙面不到一块砖高（只有一排）时不挪。"""
    short, long_ = sorted(float(v) for v in size)
    pv = (short if d == "h" else long_) + joint
    need = LAYOUT["min_edge_mm"] + joint / 2
    bottom, top = float(bottom), float(top)
    if top - bottom <= pv + 0.5:
        return bottom
    if ceil is not None and top > ceil + 0.5:
        e = flooring._edge(float(ceil), bottom, pv)   # 最下一排（含半条缝）
        return float(ceil) + (need - e if e < pv - 0.5 and e < need - 0.5 else 0.0)
    e = flooring._edge(-bottom, -top, pv)             # 最上一排（含半条缝）
    return bottom - (need - e if e < pv - 0.5 and e < need - 0.5 else 0.0)


def _tile_top(bottom, top, rule, ceil, size, joint, d) -> tuple:
    """墙砖的顶和竖向网格起点 → (顶, 起点)。顶写"吊顶底以上 n"的（"ceiling+100" 这类）贴进吊顶时，吊顶底以上 checks.over_min_mm
    到这个顶之间有一道横缝，顶就停在（最高的）这道缝上：网格对齐吊顶底往上挪过以后，这道缝上面只剩一窄条切块，藏在吊顶里，
    不贴、也不算量。没有缝落在这一段里照旧贴到这个顶；顶写 "full"、写数字的不动（贴到原顶的墙吊顶以上那一排照贴）。"""
    ov = _v_origin(bottom, top, ceil, size, joint, d)
    if ceil is None or not (isinstance(rule, str) and TOP_RE.fullmatch(rule)) or top <= ceil + 0.5:
        return top, ov
    short, long_ = sorted(float(v) for v in size)
    pv = (short if d == "h" else long_) + joint
    v = ov + math.floor((top - ov) / pv + 1e-9) * pv
    return (_r1(v), ov) if ceil + CHECKS["over_min_mm"] - 0.5 <= v < top - 0.5 else (top, ov)


def _grid(L, bottom, top, size, joint, d, start, ov=None) -> dict:
    """一面墙的排砖网格（墙面坐标）：d "h" 横贴（长边水平）/ "v" 竖贴；竖向一道横缝在 ov（_v_origin 定：贴进吊顶时对齐吊顶底，
    否则在地面完成面；缺省地面完成面）。"墙砖压地砖"说的是施工顺序（墙砖立在地砖上），不要求第一排整砖。沿墙按 start 对中或从
    一端整砖起，从一端起时另一端的非整砖小于 layout.min_edge_mm 就把网格往起铺那端挪，两端都不小于它。"""
    short, long_ = sorted(float(v) for v in size)
    su, sv = (long_, short) if d == "h" else (short, long_)
    pu, pv = su + joint, sv + joint
    need = LAYOUT["min_edge_mm"] + joint / 2
    if start == "center":
        ou, ku = flooring._axis(0.0, L, pu)
    else:
        ou, ku = (0.0, "左端整砖") if start == "left" else (float(L), "右端整砖")
        cut = flooring._edge(-ou, -L, pu) if start == "left" else flooring._edge(ou, 0.0, pu)
        if cut < pu - 0.5 and cut < need - 0.5:
            ou = ou - (need - cut) if start == "left" else ou + (need - cut)
    ov = float(bottom if ov is None else ov)
    cap = lambda v, hi: _r1(min(v, hi))   # 比一块砖还短的墙：边砖就是整面墙
    edges = {"u": [cap(flooring._piece(flooring._edge(ou, 0.0, pu), pu, su, joint), L), cap(flooring._piece(flooring._edge(-ou, -L, pu), pu, su, joint), L)],
             "v": [cap(flooring._piece(flooring._edge(ov, bottom, pv), pv, sv, joint), top - bottom),
                   cap(flooring._piece(flooring._edge(-ov, -top, pv), pv, sv, joint), top - bottom)]}
    return {"dir": 0.0, "origin": [_r1(ou % pu), _r1(ov)], "pitch": [_r1(pu), _r1(pv)], "stagger": 0, "start": ku, "size_uv": [su, sv],
            "edges": edges}


def _small(grid, g, joint, top, ceil) -> list:
    """切出来窄于 layout.min_edge_mm 的条砖宽（毫米）：墙两端、门窗洞边；藏在吊顶里的（整块在吊顶底以上的、最上面一排）不算。"""
    pu, pv = grid["pitch"]
    lim = LAYOUT["min_edge_mm"]
    out = []
    for p, full, w, h in flooring.cells(grid, g):
        if p.area >= full - 1.0 or (ceil is not None and p.bounds[1] >= ceil - 0.5):
            continue
        dims = []
        if w < pu - 0.5:
            dims.append(w - joint / 2)
        if h < pv - 0.5 and not (ceil is not None and p.bounds[3] >= top - 0.5):
            dims.append(h - joint / 2)
        dims = [x for x in dims if 2.0 <= x < lim - 0.5]
        if dims:
            out.append(min(dims))
    return out


def _under_ceiling(grid, joint, top, ceil):
    """墙砖伸进吊顶时，吊顶底下露出的最上面那一排砖高（毫米，不含缝；一条缝正好在吊顶底时是整砖）；没有吊顶、砖不到吊顶返回 None。"""
    if ceil is None or top <= ceil + 0.5:
        return None
    pv = grid["pitch"][1]
    return flooring._piece(flooring._edge(-grid["origin"][1], -float(ceil), pv), pv, grid["size_uv"][1], joint)


def _near_joint(p, grid, gap) -> str:
    """点位盒子离砖缝不到 gap：返回哪道缝（"竖缝 u=…""横缝 v=…"），没有返回空串。"""
    (ou, ov), (pu, pv) = grid["origin"], grid["pitch"]
    for lo, hi, o, pitch, name in ((p["u"] - p["w"] / 2, p["u"] + p["w"] / 2, ou, pu, "竖缝 u"), (p["z"], p["z"] + p["h"], ov, pv, "横缝 v")):
        k = math.ceil((lo - gap - o) / pitch - 1e-9)
        s = o + k * pitch
        if s < hi + gap - 1e-6:
            return f"{name}={s:.0f}"
    return ""


# ---------------------------------------------------------------- 方案
def _spec(blk, keys, rm, where, H, errors) -> dict:
    """意图里的一块（整间或一面墙）→ 认得的做法字段；写错的一项报一行（check=intent），那一项不用。"""
    spec = {}
    tag = f"{rm['name']}{' ' + where if where else ''}"
    err = lambda msg: errors.append({"room": rm["name"], "room_id": rm["id"], "wall": where, "check": "intent", "msg": f"{tag}：{msg}"})
    bad = sorted(set(blk) - set(keys))
    if bad:
        err(f"不认识的键 {'、'.join(bad)}（可用：{'、'.join(keys)}）")
    num = lambda v, lo, hi: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
    if "finish" in blk:
        if isinstance(blk["finish"], str) and blk["finish"] in finishes():
            spec["finish"] = blk["finish"]
        else:
            err(f"没有墙面材料 {blk['finish']}（可用：{'、'.join(finishes())}）")
    if "dir" in blk:
        if blk["dir"] in ("h", "v"):
            spec["dir"] = blk["dir"]
        else:
            err("dir 是 \"h\"（横贴，长边水平）或 \"v\"（竖贴）")
    if "top" in blk:
        v = blk["top"]
        if v == "full" or (isinstance(v, str) and TOP_RE.fullmatch(v)):
            spec["top"] = v
        elif num(v, 1, 1e5):
            if v > H + 0.5:
                err(f"top {v:g} 高于原顶 {H:g}")
            else:
                spec["top"] = float(v)
        else:
            err("top 写 \"full\"（到原顶）、\"ceiling+100\"（吊顶底以上 100）或离楼面的毫米数")
    if "skirting" in blk:
        v = blk["skirting"]
        if isinstance(v, bool):
            spec["skirting"] = skirtings()[0] if v and skirtings() else False
        elif isinstance(v, str) and v in skirtings():
            spec["skirting"] = v
        else:
            err(f"skirting 写踢脚线材料键（{'、'.join(skirtings())}）、true 或 false")
    if "size" in blk:
        v = blk["size"]
        if isinstance(v, list) and len(v) == 2 and all(num(x, 50, 3000) for x in v):
            spec["size"] = [float(x) for x in sorted(v)]
        else:
            err("size 要写 [短边, 长边]（50～3000 毫米）")
    if "joint" in blk:
        if num(blk["joint"], 0, 20):
            spec["joint"] = float(blk["joint"])
        else:
            err("joint 要是 0～20 的数（毫米）")
    return spec


def _read_intent(intent, rooms, errors) -> tuple:
    """意图 → (出立面的方式, {房间 id: 意图块})。房间写错、意图顶层键写错的各报一行。"""
    mode = ELEVATIONS["mode"]
    if intent is None:
        return mode, {}
    top = lambda msg: errors.append({"room": "", "room_id": "", "wall": "", "check": "intent", "msg": msg})
    bad = sorted(set(intent) - {"rooms", "elevations"})
    if bad:
        top(f"意图里不认识的键 {'、'.join(bad)}（可用：rooms、elevations）")
    if "elevations" in intent:
        if intent["elevations"] in MODES:
            mode = intent["elevations"]
        else:
            top(f"elevations 是 {'、'.join(MODES)} 之一（写的是 {intent['elevations']}）")
    given = {}
    for blk in intent["rooms"]:
        rm = furnish.find_room(rooms, blk.get("room"))
        if isinstance(rm, str):
            errors.append({"room": str(blk.get("room")), "room_id": "", "wall": "", "check": "intent", "msg": rm})
            continue
        given[rm["id"]] = blk
    return mode, given


def _room_intent(blk, rm, H, errors) -> tuple:
    """一间房的意图块 → (整间的做法, {墙边: 做法}, {墙边: 出不出立面}, {墙边: 说明})。"""
    if blk is None:
        return {}, {}, {}, {}
    ids = [w["id"] for w in rm["walls"]]
    err = lambda wid, msg: errors.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "intent",
                                          "msg": f"{rm['name']}{' ' + wid if wid else ''}：{msg}"})
    room_spec = _spec(blk, ROOM_KEYS, rm, "", H, errors)
    elev, notes, specs = {}, {}, {}
    if "elev" in blk:
        v = blk["elev"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            err("", "elev 要写墙边编号的列表：[\"W2\", \"W4\"]")
        else:
            for x in v:
                if x in ids:
                    elev[x] = True
                else:
                    err("", f"elev 里的 {x} 不是{rm['name']}的墙（有 W1～W{len(ids)}）")
    for wid, wb in blk.get("walls", {}).items():
        if wid not in ids:
            err("", f"{wid} 不是{rm['name']}的墙（有 W1～W{len(ids)}）")
            continue
        if not isinstance(wb, dict):
            err(wid, "要写成对象 {\"finish\": …}")
            continue
        specs[wid] = _spec(wb, WALL_KEYS, rm, wid, H, errors)
        if "elev" in wb:
            if isinstance(wb["elev"], bool):
                elev[wid] = wb["elev"]
            else:
                err(wid, "elev 是 true 或 false")
        if "note" in wb:
            if isinstance(wb["note"], str):
                notes[wid] = wb["note"]
            else:
                err(wid, "note 要写文字")
    return room_spec, specs, elev, notes


def _wall(rm, i, ring, opn, lets, rule, specs, ctx, mode, elev_set, note, errors, viol) -> tuple:
    """一面墙 → (方案里的一条, 提醒列表)。"""
    w = ring[i]
    wid, L = w["id"], w["len"]
    H = ceiling.room_height(ctx["sch"], rm["id"])
    bottom = ctx["level"].get(rm["id"], 0.0)
    e = {"id": f"WF-{rm['id']}-{wid}", "room": rm["id"], "wall": wid, "letter": lets[wid], "a": [_r1(w["a"][0]), _r1(w["a"][1])],
         "b": [_r1(w["b"][0]), _r1(w["b"][1])], "length": _r1(L), "bottom": _r1(bottom), "height": _r1(H)}
    if opn[i]:
        e.update(open=True, elev=False, why=[])
        return e, ["背后没有墙（开敞），不分做法"]
    sp, src = dict(rule), "规则"
    for s in specs:
        if s:
            sp.update(s)
            src = "意图"
    mat = MATERIALS[sp["finish"]]
    sk = sp.get("skirting") or False
    skm = MATERIALS[sk] if sk else None
    ceil = _ceiling_at(w, ctx["ceilings"].get(rm["id"], []), H)
    floor_ = bottom + (skm["height"] if skm else 0.0)
    top_rule = sp.get("top", "full")
    top = _top(top_rule, ceil, H)
    top_from = next((lvl for lvl, s in ((wid, specs[1]), ("", specs[0])) if "top" in s), None)
    if top <= floor_ + 0.5 and top_from is not None:   # 意图写的顶高不高于踢脚线顶（或地面）：这一项不用，按规则表
        msg = (f"{rm['name']}{' ' + top_from if top_from else ''}：top {top:g} 不高于{'踢脚线顶' if skm else '地面完成面'} {floor_:g}，"
               f"按规则表的 {rule.get('top', 'full')}")
        if not any(x["msg"] == msg for x in errors):
            errors.append({"room": rm["name"], "room_id": rm["id"], "wall": top_from, "check": "intent", "msg": msg})
        top_rule = rule.get("top", "full")
        top = _top(top_rule, ceil, H)
    if mat["kind"] == "tile":   # 墙砖的顶可能停在吊顶上方那道缝上（_tile_top），面积、块数、建模都按这个顶
        size = [float(v) for v in sorted(sp.get("size") or mat["size"])]
        joint = float(sp["joint"] if sp.get("joint") is not None else mat.get("joint") or 0.0)
        d = sp.get("dir") or "h"
        top, ov = _tile_top(bottom, top, top_rule, ceil, size, joint, d)
    ops = _openings_on(w, ctx)
    open_u = _open_u(_backed(w, ctx["solid"]), L)
    rect = box(0.0, bottom, L, top)
    cut = _cuts(L, bottom, top, ops, open_u)
    g = rect.difference(unary_union(cut)) if cut else rect
    tag = f"{rm['name']} {wid}"
    notes = [f"有 {sum(b - a for a, b in open_u):.0f} 长背后没有墙（开敞），这一段不做墙面"] if open_u else []
    expect = L * (top - bottom) - (unary_union([c.intersection(rect) for c in cut]).area if cut else 0.0)
    if abs(g.area - expect) > CHECKS["area_tol_mm2"]:
        viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "area",
                     "msg": f"{tag}：墙面面积 {g.area / 1e6:.3f}㎡，墙面减去洞口应是 {expect / 1e6:.3f}㎡"})
    vis = ceil if ceil is not None else H
    if top > H + 0.5:
        viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "top", "msg": f"{tag}：{mat['name']}顶 {top:g} 高于原顶 {H:g}"})
    elif top < vis - 0.5:
        viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "top",
                     "msg": f"{tag}：{mat['name']}顶 {top:g} 低于{'吊顶底' if ceil is not None else '原顶'} {vis:g}，露出一截没做面层的墙"})
    elif mat["kind"] in ("tile", "wallpaper") and ceil is not None and top < min(ceil + CHECKS["over_min_mm"], H) - 0.5:
        viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "top",
                     "msg": f"{tag}：{mat['name']}顶 {top:g} 伸进吊顶（底 {ceil:g}）不到 {CHECKS['over_min_mm']:g}，吊顶边口会露出砖顶"})
    pts = ctx["points"].get(wid, [])
    fixed = _fixed_on(w, rm["id"], ctx["items"])
    e.update(open=False, finish=sp["finish"], material=mat["name"], kind=mat["kind"], top=_r1(top), top_rule=top_rule,
             ceiling=None if ceil is None else _r1(ceil), openings=ops, open_spans=open_u, parts=_parts(g), area_m2=round(g.area / 1e6, 2))
    grid = None
    if mat["kind"] == "tile":
        ends = _ends(ring, i, opn, open_u)
        grid = _grid(L, bottom, top, size, joint, d, _start(ends, L, ops), ov)
        q, _ = flooring.quantities(grid, g, {"size": grid["size_uv"], "waste": mat.get("waste") or 0.0, "kind": "tile"})
        grid["quantity"] = q
        if q["cover_diff_mm2"] > CHECKS["area_tol_mm2"]:
            viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "area",
                         "msg": f"{tag}：整块 + 切块覆盖的面积和墙面差 {q['cover_diff_mm2']:.0f}mm²"})
        thin = _small(grid, g, joint, top, ceil)
        if thin:
            notes.append(f"小于 {LAYOUT['min_edge_mm']:g} 的条砖 {len(thin)} 块（最窄 {min(thin):.0f}）")
        strip = _under_ceiling(grid, joint, top, ceil)
        if strip is not None and 2.0 <= strip < LAYOUT["min_edge_mm"] - 0.5:   # 砖竖向那条边不到两个 min_edge 时才会有
            notes.append(f"吊顶底下露出的最上面一排砖只有 {strip:.0f} 高")
        if grid["start"] in ("缝对中", "砖对中") and all(ends):
            notes.append("两端都是阳角或开敞，非整砖对称放在露着的两端")
        e.update(size=size, joint=joint, dir=d)
    e["grid"] = grid
    for p in pts:
        vc = p["z"] + p["h"] / 2
        hole = next((o for o in ops if o["from"] - 0.5 <= p["u"] <= o["to"] + 0.5
                     and (o["bottom"] if o["bottom"] is not None else -1e9) <= vc <= o["top"]), None)
        if hole is not None:
            viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "points",
                         "msg": f"{tag}：点位 {p['id']}（离地 {p['z']:g}）落在{'窗' if hole['bottom'] is not None else '门'} {hole['id']} 的洞口里"})
        elif not g.buffer(0.5).contains(Point(p["u"], vc)):
            viol.append({"room": rm["name"], "room_id": rm["id"], "wall": wid, "check": "points",
                         "msg": f"{tag}：点位 {p['id']}（离地 {p['z']:g}）在墙面面层外（面层 {bottom:g}～{top:g}）"})
        elif grid and (j := _near_joint(p, grid, LAYOUT["socket_gap_mm"])):
            notes.append(f"点位 {p['id']} 离砖缝不到 {LAYOUT['socket_gap_mm']:g}（{j}）")
    if skm:
        sr = box(0.0, bottom, L, bottom + skm["height"])
        sg = sr.difference(unary_union(cut)) if cut else sr
        e["skirting"] = {"key": sk, "material": skm["name"], "height": float(skm["height"]), "top": _r1(bottom + skm["height"]),
                         "parts": _parts(sg), "area_m2": round(sg.area / 1e6, 3)}
    else:
        e["skirting"] = None
    why = []
    if mode == "all":
        why = ["全部"]
    elif mode == "finished":
        why = [k for k, on in (("房间", rm["type"] in ELEVATIONS["always"]), ("柜体", bool(fixed)), ("做法", mat["kind"] != "paint")) if on]
    if elev_set.get(wid) is True and mode != "all":
        why.append("意图")
    if elev_set.get(wid) is False:
        why = []
    e.update(points=pts, fixed=fixed, elev=bool(why), why=why, note=note, source=src)
    return e, notes


def walls(rec: dict, sch: dict, intent: dict | None = None, units=None) -> tuple[dict, dict]:
    """识别 + 方案 [+ 意图] → (带墙面的方案, 校验报告)。只做户内房间（公共空间不做）；units：只重做这几户，别的户以前生成的墙面
    原样留着。意图写错的只报那一条（check=intent），别的照做；户号不在识别结果里抛 ValueError。"""
    from .unitview import unit_ids
    geo = sch.get("after") or rec
    if units and (bad := [u for u in units if u not in unit_ids(rec, sch)]):
        raise ValueError(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unit_ids(rec, sch)) or '无'}）")
    if intent is not None:
        check_intent(intent)
    unit = {r["id"]: r.get("unit") or "" for r in geo["rooms"]}
    public = {r["id"] for r in geo["rooms"] if r.get("public")}
    rooms = furnish.rooms_of(rec, sch)
    order = {rm["id"]: i for i, rm in enumerate(rooms)}
    errors, viol, notes, layout = [], [], [], []
    mode, given = _read_intent(intent, rooms, errors)
    doable = lambda rid: bool(unit.get(rid)) and rid not in public
    redo = lambda rid: doable(rid) and (not units or unit[rid] in units)
    for rid in given:
        if not doable(rid):
            nm = next(rm["name"] for rm in rooms if rm["id"] == rid)
            errors.append({"room": nm, "room_id": rid, "wall": "", "check": "intent", "msg": f"{nm}：公共部分或没有户号的房间不做墙面"})
    keep = [w for w in sch.get("walls") or [] if units and not redo(w["room"])]
    ctx = _context(rec, sch)
    new, done = [], 0
    for rm in rooms:
        if not redo(rm["id"]):
            continue
        rule = ROOMS.get(rm["type"]) or ROOMS.get("other")
        if rule is None or rule.get("finish") not in MATERIALS:
            viol.append({"room": rm["name"], "room_id": rm["id"], "wall": "", "check": "no_finish",
                         "msg": f"{rm['name']}：没分到墙面做法（规则表 rooms 里没有 {rm['type']}，也没有 other）"})
            continue
        done += 1
        H = ceiling.room_height(sch, rm["id"])
        ring = rm["walls"]
        opn = [sum(b - a for a, b in _backed(w, ctx["solid"])) < CHECKS["open_min_mm"] for w in ring]
        lets = letters(ring)
        room_spec, wall_specs, elev_set, wall_notes = _room_intent(given.get(rm["id"]), rm, H, errors)
        ctx["points"] = _assign_points(ring, rm["id"], ctx["items"])
        for i, w in enumerate(ring):
            e, ns = _wall(rm, i, ring, opn, lets, rule, (room_spec, wall_specs.get(w["id"], {})), ctx, mode, elev_set,
                          wall_notes.get(w["id"], ""), errors, viol)
            e["unit"] = unit.get(rm["id"], "")
            new.append(e)
            notes += [f"{rm['name']} {w['id']}（{e['letter']}）：{n}" for n in ns]
            layout.append({"room": rm["name"], "room_id": rm["id"], "wall": w["id"], "letter": e["letter"],
                           "finish": "开敞" if e["open"] else label(e), "bottom": e["bottom"], "top": e.get("top"), "elev": e["elev"],
                           "why": e["why"], **({"start": e["grid"]["start"], "edges": e["grid"]["edges"],
                                                "quantity": {k: e["grid"]["quantity"][k] for k in ("area_m2", "whole", "cut", "total", "unit")}}
                                               if e.get("grid") else {}),
                           **({"area_m2": e["area_m2"]} if not e["open"] else {}), "source": e.get("source", "")})
    if not done and not any(v["check"] == "no_finish" for v in viol):
        errors.append({"room": "", "room_id": "", "wall": "", "check": "no_rooms",
                       "msg": "一间房都没做墙面：识别结果里的房间都没有户号（unit）或都是公共空间（public）；先给户内房间写上 unit"})
    ws = sorted(keep + new, key=lambda w: (order.get(w["room"], 1e9), int(w["wall"][1:]) if w["wall"][1:].isdigit() else 0))
    out = dict(sch, walls=ws)
    viol += check(rec, out)
    counts = {}
    for v in viol + errors:
        counts[v["check"]] = counts.get(v["check"], 0) + 1
    fin = [e for e in new if not e["open"]]
    return out, {"ok": not errors and not viol, "rooms": done, "walls": len(fin), "open": len(new) - len(fin),
                 "elevations": sum(e["elev"] for e in new), "mode": mode, "layout": layout, "notes": notes, "errors": errors,
                 "violations": viol, "counts": counts}


def check(rec: dict, sch: dict) -> list:
    """方案里的墙面数据和现在的墙边、门窗、吊顶、地面完成面、点位、柜体对不对得上（出图入口调，生成时也调一遍）：墙面之后改过门窗、
    重跑过吊顶、地面、水电、家具，就对不上。每处一条（check：walls、openings、top、bottom、points、fixed），写明重跑 scripts/wall.py。
    只查方案里有墙面数据的房间。"""
    ws = sch.get("walls") or []
    if not ws:
        return []
    ctx = _context(rec, sch)
    rooms = {rm["id"]: rm for rm in furnish.rooms_of(rec, sch)}
    redo = "：重跑 scripts/wall.py"
    viol = []
    add = lambda rm, wid, check_, msg: viol.append({"room": rm["name"] if rm else "", "room_id": rm["id"] if rm else "", "wall": wid,
                                                     "check": check_, "msg": msg + redo})
    by = {}
    for e in ws:
        by.setdefault(e["room"], []).append(e)
    okey = lambda ops: sorted((o["id"], round(o["from"]), round(o["to"]), -1 if o["bottom"] is None else round(o["bottom"]), round(o["top"]))
                              for o in ops)
    pkey = lambda ps: sorted((p["id"], round(p["u"]), round(p["z"])) for p in ps)
    for rid, es in by.items():
        rm = rooms.get(rid)
        if rm is None:
            add(None, "", "walls", f"墙面数据里的房间 {rid} 现在没有了")
            continue
        ring = {w["id"]: w for w in rm["walls"]}
        moved = lambda e: e["wall"] not in ring or max(abs(ring[e["wall"]][k][j] - e[k][j]) for k in ("a", "b") for j in (0, 1)) > 1.0
        if sorted(e["wall"] for e in es) != sorted(ring) or any(moved(e) for e in es):
            add(rm, "", "walls", f"{rm['name']} 的墙边变了（墙面数据 {len(es)} 面，现在 {len(ring)} 面或位置不同）")
            continue
        H = ceiling.room_height(sch, rid)
        bottom = ctx["level"].get(rid, 0.0)
        pts = _assign_points(rm["walls"], rid, ctx["items"])
        for e in es:
            w, tag = ring[e["wall"]], f"{rm['name']} {e['wall']}"
            spans = _backed(w, ctx["solid"])
            open_u = _open_u(spans, w["len"])
            now_open = sum(b - a for a, b in spans) < CHECKS["open_min_mm"]
            if now_open != bool(e.get("open")) or (not now_open and [[round(a), round(b)] for a, b in open_u]
                                                    != [[round(a), round(b)] for a, b in e.get("open_spans") or []]):
                add(rm, e["wall"], "walls", f"{tag}：背后的墙变了（开敞段不同）")
                continue
            if e.get("open"):
                continue
            if abs(bottom - e["bottom"]) > 0.5:
                add(rm, e["wall"], "bottom", f"{tag}：地面完成面变了（墙面数据 {e['bottom']:g}，现在 {bottom:g}）")
            ceil = _ceiling_at(w, ctx["ceilings"].get(rid, []), H)
            top = _top(e["top_rule"], ceil, H)
            if e.get("grid") and e.get("size"):   # 墙砖：顶可能停在吊顶上方那道缝上
                top, _ = _tile_top(bottom, top, e["top_rule"], ceil, e["size"], e.get("joint") or 0.0, e.get("dir") or "h")
            if abs(top - e["top"]) > 0.5:
                add(rm, e["wall"], "top", f"{tag}：吊顶或原顶变了，墙面顶高应是 {top:g}（墙面数据 {e['top']:g}）")
            ops = _openings_on(w, ctx)
            if okey(ops) != okey(e["openings"]):
                add(rm, e["wall"], "openings", f"{tag}：门窗变了（墙面数据 {'、'.join(o['id'] for o in e['openings']) or '无'}，"
                                               f"现在 {'、'.join(o['id'] for o in ops) or '无'}，或位置、高度不同）")
            if pkey(pts.get(e["wall"], [])) != pkey(e.get("points") or []):
                add(rm, e["wall"], "points", f"{tag}：墙上的点位变了（墙面数据 {len(e.get('points') or [])} 个，现在 {len(pts.get(e['wall'], []))} 个，"
                                             "或位置不同）")
            if _fixed_on(w, rid, ctx["items"]) != (e.get("fixed") or []):
                add(rm, e["wall"], "fixed", f"{tag}：贴墙的柜体变了")
    return viol
