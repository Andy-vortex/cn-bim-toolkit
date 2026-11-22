"""拆改：在识别结果（墙、房间、门窗）上做非承重隔墙的改动，并记下每一处改动。

只动"薄墙"（厚度 < 180mm，按隔墙对待）；厚墙（≥180 或外墙）一律不动，需要动的地方记为"需结构确认"。
能做的改动：
  - 门洞加宽：洞口沿墙方向加宽，必要时整体挪一点（不切到墙角、别的洞口和第三个房间）；
  - 新开门洞：在两个房间共用的薄墙上开门；
  - 封堵门洞：原门洞砌死；
  - 新砌卫生间：在房间角落用 100 厚隔墙围出一间。
所有坐标为图纸毫米，几何用 shapely。被拆掉的墙块记在 demolish 里（出图画红色虚线），新砌墙 status="新砌"（出图画斜线）。
拆改清单（changes）出成 A4 页面见 to_html。
"""
from __future__ import annotations

import html
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import printing

PARTITION_MAX_T = 180.0
DECIDE_FIRST = ("需结构确认", "无法加宽")   # 要人拍板的改动类型：拆改清单里排在最前
WC_SIZE = (1800.0, 2100.0)   # 新砌专用卫生间外包（含两面 100 隔墙），净尺寸约 1700×2000
WC_WALL = 100.0
DOOR_NEW = 900.0             # 新开门洞宽（平开门开启净宽约 800）


def coords(g):
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda x: x.area)
    return [[round(x, 1), round(y, 1)] for x, y in g.exterior.coords]


def _parts(g):
    if g.is_empty:
        return []
    return [x for x in (g.geoms if hasattr(g, "geoms") else [g]) if x.geom_type == "Polygon"]


def _axis(g):
    """外包矩形长边方向（度，0~180）和长短边比。"""
    cs = list(g.minimum_rotated_rectangle.exterior.coords)
    e1 = (cs[1][0] - cs[0][0], cs[1][1] - cs[0][1]); e2 = (cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    e = e1 if l1 >= l2 else e2
    return math.degrees(math.atan2(e[1], e[0])) % 180, max(l1, l2) / max(min(l1, l2), 1.0), max(l1, l2)


def to_html(changes: list, path: str, project: str = "", date: str = "") -> str:
    """拆改清单页：按类型分组（要人拍板的在前，其余按首次出现的顺序），每行"分区 | 对象 | 内容"（整张表都没有分区时
    不要分区列）；判断依据一句放在页脚。末尾内嵌 changes 原样的 JSON。"""
    e = html.escape
    zone = lambda c: c.get("core") or c.get("zone") or ""
    types = sorted(dict.fromkeys(c["type"] for c in changes),
                   key=lambda t: DECIDE_FIRST.index(t) if t in DECIDE_FIRST else len(DECIDE_FIRST))
    zoned = any(zone(c) for c in changes)
    cols = (["分区"] if zoned else []) + ["对象", "内容"]
    out = ["<h1>拆改清单</h1>", '<p class="minor">' + " · ".join(e(x) for x in (project, date) if x) + "</p>",
           "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead>"]
    for t in types:
        g = [c for c in changes if c["type"] == t]
        warn = ' class="warn"' if t in DECIDE_FIRST else ""
        out.append(f'<tbody><tr><th colspan="{len(cols)}"{warn}>{e(t)} {len(g)}</th></tr>')
        out += ["<tr>" + (f"<td>{e(zone(c))}</td>" if zoned else "") + f'<td>{e(c["target"])}</td><td>{e(c["detail"])}</td></tr>' for c in g]
        out.append("</tbody>")
    out.append(f'</table><p class="minor">墙厚 &lt;{PARTITION_MAX_T:g}mm 按非承重隔墙处理，厚墙、外墙不动；新砌卫生间需新接给排水；'
               '须结构、给排水专业复核。</p>')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(printing.page("拆改清单", "".join(out), data=changes))
    return path


def opening_rect(x, y, rot, width, depth):
    a = math.radians(rot)
    ux, uy, nx, ny = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
    w, t = width / 2, depth / 2
    return Polygon([(x - ux * w - nx * t, y - uy * w - ny * t), (x + ux * w - nx * t, y + uy * w - ny * t),
                    (x + ux * w + nx * t, y + uy * w + ny * t), (x - ux * w + nx * t, y - uy * w + ny * t)])


class Renovator:
    def __init__(self, after: dict, use: dict):
        self.a = after
        self.use = use
        self.rooms = {r["id"]: r for r in after["rooms"]}
        self.ops = {o["id"]: o for o in after["openings"]}
        self.changes, self.demolish = [], []
        self.nid = {"R": 200, "M": 200, "W": 500}
        self._sr = {}

    # ---------------- 基础
    def new_id(self, k):
        self.nid[k] += 1
        return f"{k}{self.nid[k]}"

    def P(self, rid):
        return Polygon(self.rooms[rid]["polygon"])

    def log(self, kind, target, detail):
        core = ""
        if target in self.use:
            core = self.use[target].get("core", "")
        elif target in self.ops:
            core = next((self.use[x]["core"] for x in self.side_rooms(self.ops[target]) if self.use[x].get("core")), "")
        self.changes.append({"type": kind, "target": target, "detail": detail, "core": core})

    def side_rooms(self, o):
        """门两侧的房间：从洞口中心沿墙法线两边各探出半墙厚 + 150mm。（结果按门缓存，房间几何变了就清空）"""
        key = (o["id"], o["x"], o["y"])
        if key in self._sr:
            return self._sr[key]
        a = math.radians(o["rot"])
        nx, ny = -math.sin(a), math.cos(a)
        d = (o.get("wall_t") or 200) / 2 + 150
        out = []
        for s in (-1, 1):
            p = Point(o["x"] + s * nx * d, o["y"] + s * ny * d)
            hit = [rid for rid, r in self.rooms.items() if Polygon(r["polygon"]).buffer(5).contains(p)]
            if hit:
                out.append(min(hit, key=lambda rid: self.P(rid).area))
        self._sr[key] = out
        return out

    def doors_of(self, rid):
        return [o for o in self.ops.values() if o["kind"] == "door" and rid in self.side_rooms(o)]

    def is_wc(self, rid):
        return "卫生间" in self.use[rid]["usage"]

    def ensuite_of(self, rid):
        """和房间有门直通的卫生间。"""
        return [x for o in self.doors_of(rid) for x in self.side_rooms(o) if x != rid and self.is_wc(x)]

    def open_rects(self, skip=()):
        out = []
        for o in self.ops.values():
            if o["id"] in skip:
                continue
            if o.get("polygon"):
                out.append(Polygon(o["polygon"]))
            else:
                out.append(opening_rect(o["x"], o["y"], o["rot"], o["width"], (o.get("wall_t") or 200) + 20))
        return out

    def cut_walls(self, R, reason, demolish=True):
        out = []
        for w in self.a["walls"]:
            W = Polygon(w["polygon"])
            if not W.intersects(R) or W.intersection(R).area < 500:
                out.append(w)
                continue
            if demolish and w.get("status") != "新砌":
                for g in _parts(W.intersection(R)):
                    if g.area > 500:
                        self.demolish.append({"polygon": coords(g), "reason": reason, "from": w["id"], "thickness": w["thickness"]})
            rest = _parts(W.difference(R))
            rest = [g for g in rest if g.area > 2000]
            for k, g in enumerate(rest):
                nw = dict(w)
                nw["id"] = w["id"] + ("abcdefgh"[k] if len(rest) > 1 else "")
                nw["polygon"] = coords(g)
                if w.get("status") != "新砌":
                    nw["status"] = "局部拆除"
                out.append(nw)
        self.a["walls"] = out

    def add_wall(self, g, t, reason):
        for p in _parts(g):
            if p.area > 2000:
                _, _, L = _axis(p)
                self.a["walls"].append({"id": self.new_id("W"), "thickness": t, "length": round(L), "polygon": coords(p),
                                        "exterior": False, "keep": False, "reason": reason, "status": "新砌"})

    def add_door(self, x, y, rot, width, t, rooms, swing_toward, label):
        a = math.radians(rot)
        nx, ny = -math.sin(a), math.cos(a)
        tx, ty = swing_toward
        swing = 1 if (tx - x) * nx + (ty - y) * ny > 0 else -1
        o = {"id": self.new_id("M"), "kind": "door", "block": label, "x": round(x, 1), "y": round(y, 1), "rot": round(rot, 2),
             "width": round(width), "depth": 0.0, "label": "", "exterior": False, "rooms": list(rooms), "polygon": [],
             "sx": 1, "sy": 1, "wall_t": float(t), "leafs": 1, "swing": swing, "hinge_end": -1}
        self.a["openings"].append(o)
        self.ops[o["id"]] = o
        for rid in rooms:
            self.rooms[rid]["doors"].append(o["id"])
        return o

    def remove_door(self, o):
        self.a["openings"] = [x for x in self.a["openings"] if x["id"] != o["id"]]
        self.ops.pop(o["id"], None)
        for r in self.rooms.values():
            if o["id"] in r["doors"]:
                r["doors"].remove(o["id"])

    # ---------------- 共用墙上开门
    def contact(self, A: Polygon, B: Polygon):
        """A、B 之间的薄墙接触带：返回 (长度, 中心, 方向角, 墙厚)；没有返回 None。"""
        ws = [(w, Polygon(w["polygon"])) for w in self.a["walls"] if w["thickness"] < PARTITION_MAX_T]
        near = [(w, W) for w, W in ws if W.distance(A) < 15 and W.distance(B) < 15]
        if not near:
            return None
        t = max(w["thickness"] for w, _ in near)
        Z = unary_union([W for _, W in near]).intersection(A.buffer(t + 40)).intersection(B.buffer(t + 40))
        parts = _parts(Z)
        if not parts:
            return None
        z = max(parts, key=lambda g: g.area)
        ang, _, L = _axis(z)
        c = z.centroid
        return L, (c.x, c.y), ang, t, z

    def open_in_contact(self, a_id, b_id, A, B, swing_to, label):
        ct = self.contact(A, B)
        if not ct or ct[0] < DOOR_NEW + 200:
            return None
        L, (cx, cy), ang, t, z = ct
        core = opening_rect(cx, cy, ang, DOOR_NEW, t)
        if core.difference(z.buffer(25)).area > 0.05 * core.area:
            return None
        R = opening_rect(cx, cy, ang, DOOR_NEW, t + 30)
        if any(R.intersects(x) for x in self.open_rects()):
            return None
        self.cut_walls(R, label)
        return self.add_door(cx, cy, ang, DOOR_NEW, t, (a_id, b_id), swing_to, label)

    # ---------------- 封堵
    def infill(self, o, reason):
        t = o.get("wall_t") or 200
        self.add_wall(opening_rect(o["x"], o["y"], o["rot"], o["width"] + 10, t), t, reason)
        self.remove_door(o)

    # ---------------- 新砌卫生间
    def wc_candidates(self, rid):
        A = self.P(rid)
        # 先做一次开运算去掉门洞条那种 5cm 的小凸出，否则外包矩形的角会落在房间外面
        clean = A.buffer(-80, join_style=2).buffer(80, join_style=2)
        cs = list(clean.minimum_rotated_rectangle.exterior.coords)[:4]
        out = []
        for i in range(4):
            c = cs[i]; pn = cs[(i + 1) % 4]; pp = cs[i - 1]
            e1 = ((pn[0] - c[0]), (pn[1] - c[1])); l1 = math.hypot(*e1); e1 = (e1[0] / l1, e1[1] / l1)
            e2 = ((pp[0] - c[0]), (pp[1] - c[1])); l2 = math.hypot(*e2); e2 = (e2[0] / l2, e2[1] / l2)
            for a, b in (WC_SIZE, WC_SIZE[::-1]):
                # 角点落在房间外（门洞条等小凸出）时，沿两条边各往里挪最多 120mm
                for s1, s2 in ((0, 0), (0, 60), (60, 0), (60, 60), (0, 120), (120, 0), (120, 120)):
                    c2 = (c[0] + e1[0] * s1 + e2[0] * s2, c[1] + e1[1] * s1 + e2[1] * s2)
                    pt = lambda s, t_, c=c2, e1=e1, e2=e2: (c[0] + e1[0] * s + e2[0] * t_, c[1] + e1[1] * s + e2[1] * t_)
                    rect = Polygon([pt(0, 0), pt(a, 0), pt(a, b), pt(0, b)])
                    if A.buffer(15).contains(rect):
                        out.append({"c": c2, "e1": e1, "e2": e2, "a": a, "b": b, "rect": rect, "pt": pt})
                        break
        return out

    def wc_door(self, cand, door_side):
        """候选卫生间在 door_side 那面新墙上的门：返回 (x, y, 方向角, 门扇开向的点)。"""
        pt, a, b, e1, e2 = cand["pt"], cand["a"], cand["b"], cand["e1"], cand["e2"]
        if door_side == "e1":   # 平行 e1 的那面墙（在 e2 = b 处）
            x, y = pt((a - WC_WALL) / 2, b - WC_WALL / 2)
            return x, y, math.degrees(math.atan2(e1[1], e1[0])), pt((a - WC_WALL) / 2, b + 600)
        x, y = pt(a - WC_WALL / 2, (b - WC_WALL) / 2)
        return x, y, math.degrees(math.atan2(e2[1], e2[0])), pt(a + 600, (b - WC_WALL) / 2)

    def build_wc(self, rid, cand, door_side):
        """按候选围出卫生间：新砌两面隔墙，在 door_side（'e1' 墙或 'e2' 墙）上开门，门往房间里开。"""
        pt, a, b = cand["pt"], cand["a"], cand["b"]
        inner = Polygon([pt(0, 0), pt(a - WC_WALL, 0), pt(a - WC_WALL, b - WC_WALL), pt(0, b - WC_WALL)])
        walls = cand["rect"].difference(inner)
        A = self.P(rid)
        r = self.rooms[rid]
        self.add_wall(walls, WC_WALL, f"新砌 {rid} 专用卫生间")
        new_room = A.difference(cand["rect"])
        r["polygon"] = coords(new_room)
        r["area_m2"] = round(Polygon(r["polygon"]).area / 1e6, 2)
        wid = self.new_id("R")
        wr = {"id": wid, "name": "卫生间（新建）", "names": ["卫生间"], "area_m2": round(inner.area / 1e6, 2), "polygon": coords(inner),
              "unit": r["unit"], "unit_type": r["unit_type"], "public": False, "labeled": True, "windows": [], "ext_windows": [], "doors": []}
        self.a["rooms"].append(wr)
        self.rooms[wid] = wr
        self.use[wid] = dict(self.use[rid], id=wid, usage="卫生间（专用）", orig_name="（新建）", beds=0, note=f"为 {rid} 新砌")
        x, y, ang, toward = self.wc_door(cand, door_side)
        self._sr.clear()
        self.cut_walls(opening_rect(x, y, ang, DOOR_NEW, WC_WALL + 30), "新砌卫生间门洞", demolish=False)
        self.add_door(x, y, ang, DOOR_NEW, WC_WALL, (rid, wid), toward, "新砌卫生间门")
        return wid

    # ---------------- 门洞加宽
    def widen(self, o, new_w):
        t = o.get("wall_t") or 200
        old = opening_rect(o["x"], o["y"], o["rot"], o["width"], t)
        sides = set(self.side_rooms(o))
        others = [Polygon(r["polygon"]) for rid, r in self.rooms.items() if rid not in sides]
        rects = self.open_rects(skip=(o["id"],))
        a = math.radians(o["rot"])
        ux, uy = math.cos(a), math.sin(a)
        span = (new_w - o["width"]) / 2
        steps = [0.0] + [s * k for k in range(1, int(span // 50) + 1) for s in (50.0, -50.0)]
        for s in steps:
            x, y = o["x"] + ux * s, o["y"] + uy * s
            R = opening_rect(x, y, o["rot"], new_w, t + 30)
            if old.difference(R.buffer(1)).area > 1000:
                continue
            if any(R.buffer(30).intersects(p) for p in others) or any(R.intersects(q) for q in rects):
                continue
            bad = False
            for w in self.a["walls"]:
                W = Polygon(w["polygon"])
                if W.intersects(R) and W.intersection(R).area > 2000:
                    ang, ratio, _ = _axis(W)
                    diff = abs((ang - o["rot"]) % 180)
                    if ratio > 2 and min(diff, 180 - diff) > 20:
                        bad = True   # 会切到垂直相交的墙（墙角）
                        break
            if bad:
                continue
            self.cut_walls(R, f"{o['id']} 门洞加宽 {o['width']:.0f}→{new_w:.0f}")
            self.log("门洞加宽", o["id"], f"{'/'.join(self.use[r]['usage'] for r in sides)}：洞口 {o['width']:.0f}→{new_w:.0f}mm"
                                         + (f"，沿墙挪 {s:+.0f}mm" if s else ""))
            o["x"], o["y"], o["width"] = round(x, 1), round(y, 1), round(new_w)
            return True
        return False
