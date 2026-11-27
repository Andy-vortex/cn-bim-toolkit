"""家具布置：房间摘要（给 agent 看）、布置意图 → 坐标、几何校验。纯 shapely（.venv 里秒级跑完，不起 Blender），
坐标是识别结果的图纸毫米。

约定包 packs/cn/furniture：furniture.json（支持的家具类型：尺寸、靠墙边、使用面、挂墙高度；校验门槛）、
room_types.json（房名 → 标准类型）。摆放约定和约定包符号一样：家具的 (x, y) 是靠墙那条边的中点，rot 让局部 +y 指向室内，
局部 +x 沿墙。布置结果写进方案的 items（{key, name, x, y, z, rot, room, plan_id}），plan2ifc 照常建模。

布置意图（agent 写"放什么、靠哪面墙、和谁的关系"，不写坐标）：
  {"rooms": [{"room": 房名或房间 id, "items": [{"id": 自取的名字, "key": furniture.json 的键, 摆法…, "name"?: 图上的名字,
                                                "params"?: cn_ 类型的尺寸参数}]}]}
  摆法四选一（offset、gap 单位毫米，缺省 0）：
    "wall": "W3", "align": "start|center|end", "offset": …   靠墙：start/end 从墙的起点/终点量到家具边，center 是中心偏移；
                                                              可加 "gap" 离开墙（餐桌）
    "wall": "W4", "facing": "<id>", "offset": …               靠墙，中心对准另一件的中心（如单人沙发对着茶几）
    "front_of": "<id>", "gap": …, "offset": …                 在另一件前面隔 gap，面朝它，中心对齐（offset 沿它横移）
    "beside": "<id>", "side": "left|right", "gap": …          在另一件旁边、同一条靠墙线；left/right 是那件家具自己的左右
  墙边来自 room_context：沿房间轮廓逆时针 W1..Wn（从最南、再最西的角起），站在房内面朝这面墙，起点在右手边。
  参照的那件要写在前面。意图是确定性换算：不搜索、不打分，摆不了就给一行原因，agent 改意图重来。
  唯一的例外是灶台避窗（arrange）：橱柜的灶台上方要装油烟机，按意图摆出来油烟机沿墙进了窗洞或离洞边太近时，换一种摆法
  （对调、镜像、挪灶台、平移、换墙，按代价从小到大取第一个满足的），换不开就照意图摆、校验报 hob_window。
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from . import furngen, openings

PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "furniture"))


def _read(name: str) -> dict:
    with open(os.path.join(PACK, name), encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


_FURN = _read("furniture.json")
LIBRARY = _FURN["library"]           # Bonsai 自带家具库的文件名（GPL：只在运行时引用，不进仓库）
TYPES = _FURN["types"]               # 键 → 库类型 {name, width, depth, height, back, use, …} 或参数化类型 {name, recipe, params, …}
RULES = _FURN["rules"]               # 校验门槛
SOURCES = _FURN["sources"]           # 规则的依据
ROOM_TYPES = _read("room_types.json")["types"]
DOOR_H = openings.DEFAULT_DOOR_H
SLIVER = 120.0   # 房间轮廓上比这窄的凸条（门洞条）不算墙边
CHECKS = ("outside_room", "in_wall", "collision", "door_swing", "door_front", "window_blocked", "aisle", "use_blocked", "hob_window")
ON_WALL = 50.0   # 家具靠墙边离墙线这么近算靠着这面墙（灶台避窗找灶台在哪面墙上）
STEP = 10.0      # 灶台避窗里挪灶台、平移整件时一档挪多少（毫米）


def library_path() -> str:
    """Bonsai 自带家具库的路径：从 bonsai 包的位置推出（只找位置，不导入 bonsai）。"""
    spec = importlib.util.find_spec("bonsai")
    p = os.path.join(os.path.dirname(spec.origin), "bim", "data", "libraries", LIBRARY) if spec and spec.origin else ""
    if not os.path.isfile(p):
        raise FileNotFoundError(f"找不到家具库 {LIBRARY}（随 Bonsai 分发，在装了 Bonsai 的 Blender 里运行）")
    return p


def lib_frame(bbox, back: str) -> tuple[float, tuple[float, float]]:
    """库类型的局部外包 (x0, y0, z0, x1, y1, z1) + 靠墙边（+y/-y/+x/-x；any 前后对称，按 +y）→ (要多转的角度, 靠墙边中点的局部坐标)：
    实例转 rot + 角度、靠墙边中点放到 (x, y)，就符合"靠墙边中点、局部 +y 指向室内"的约定。"""
    x0, y0, _, x1, y1, _ = bbox
    return {"-y": (0.0, ((x0 + x1) / 2, y0)), "+y": (180.0, ((x0 + x1) / 2, y1)), "any": (180.0, ((x0 + x1) / 2, y1)),
            "-x": (90.0, (x0, (y0 + y1) / 2)), "+x": (-90.0, (x1, (y0 + y1) / 2))}[back]


def resolve(key: str, given) -> dict:
    """参数化类型的参数：缺的用缺省值，查范围、可选值和参数之间的约束；库类型没有参数。出错抛 ValueError（一行原因）。
    缺省值本来就是空的参数（台盆、水槽、灶台的位置：空着由画法定）可以写 null，别的不行。"""
    t, given = TYPES[key], given if given is not None else {}
    spec = t.get("params", {})
    if not isinstance(given, dict):
        raise ValueError("params 要写成对象")
    bad = [k for k in given if k not in spec]
    if bad:
        raise ValueError(f"{t['name']}没有参数 {'、'.join(bad)}" + (f"（可用：{'、'.join(spec)}）" if spec else ""))
    out = {}
    for k, rng in spec.items():
        text = isinstance(rng[0], str)
        v = given.get(k, rng[0] if text else rng[2])
        if text and v not in rng:
            raise ValueError(f"{k} 是 {'、'.join(rng)} 之一")
        if not text and not (v is None and rng[2] is None) and (isinstance(v, bool) or not isinstance(v, (int, float))
                                                                 or not math.isfinite(v) or not rng[0] <= v <= rng[1]):
            raise ValueError(f"{k} 要在 {rng[0]}～{rng[1]} 之间")
        out[k] = v
    if spec and (err := furngen.check(t, out)):
        raise ValueError(err)
    return out


_GEOM: dict = {}


def geom(key: str, params=None) -> dict:
    """一件家具的局部几何（靠墙边中点为原点）：width、depth、height（最高处）、mount_z、outline（平面外轮廓）、
    parts（[(轮廓, 高)]，床头板这类靠墙更高的一条单独一块）、use（使用面列表）、use_any。库类型取 furniture.json 的尺寸，参数化类型由 furngen 从参数算。"""
    k = (key, json.dumps(params or {}, sort_keys=True))
    if k not in _GEOM:
        t = TYPES[key]
        if "recipe" in t:
            s = furngen.shape(t, resolve(key, params))
            g = {"width": s["width"], "depth": s["depth"], "height": s["height"], "outline": Polygon(s["outline"]), "use": s["use"],
                 "parts": [(Polygon(r), h) for r, h in s["parts"]]}
        else:
            w, d = t["width"], t["depth"]
            g = {"width": w, "depth": d, "height": t["height"], "outline": box(-w / 2, 0, w / 2, d), "use": t["use"]}
            g["parts"] = [(g["outline"], g["height"])]
        g.update(use=[box(*r) for r in g["use"]], use_any=bool(t.get("use_any")), mount_z=float(t.get("mount_z", 0)))
        _GEOM[k] = g
    return _GEOM[k]


def room_type(name: str) -> str:
    """房名 → 标准类型（room_types.json，按表里的顺序找第一个含有的词；"客厅/餐厅"按第一个名字）；认不出是 other。"""
    for part in re.split(r"[/／]", name or ""):
        for t, spec in ROOM_TYPES.items():
            if any(w in part for w in spec["words"]):
                return t
    return "other"


# ---------------------------------------------------------------- 房间
def _unjog(ring: list, holes: list) -> list:
    """去掉门洞条：识别出的房间轮廓在门窗洞口处伸进墙厚一条窄条。两条同向平行的边之间隔着一段不到 SLIVER 的台阶、其中一条
    落在门窗洞口里（holes：洞口在墙厚范围里的矩形）时，把这一条挪到另一条的线上，再合并共线的点。不在洞口里的台阶（真实的
    墙面凹凸）不动。ring 逆时针、不闭合。"""
    P = [np.array(p, dtype=float) for p in ring]
    in_hole = lambda s, e: any(h.contains(shapely.Point(*((s + e) / 2))) for h in holes)
    changed = True
    while changed and len(P) > 4:
        changed = False
        n = len(P)
        for i in range(n):
            p, a, b, q = P[i - 1], P[i], P[(i + 1) % n], P[(i + 2) % n]
            if np.linalg.norm(b - a) >= SLIVER or min(np.linalg.norm(a - p), np.linalg.norm(q - b)) < 1:
                continue
            u1, u2 = (a - p) / np.linalg.norm(a - p), (q - b) / np.linalg.norm(q - b)
            if u1 @ u2 < 0.999:
                continue
            first, second = in_hole(p, a), in_hole(b, q)
            if first == second:
                continue
            nrm = np.array([-u1[1], u1[0]])
            off = float((b - a) @ nrm)
            if first:     # 前一条（p→a）是洞口里的窄条：挪到后一条的线上
                P[i - 1], P[i] = p + nrm * off, a + nrm * off
            else:
                P[(i + 1) % n], P[(i + 2) % n] = b - nrm * off, q - nrm * off
            g = Polygon([tuple(c) for c in P]).buffer(0).simplify(1.0)
            if g.geom_type != "Polygon" or len(g.exterior.coords) - 1 >= n:   # 没变简单：不再动
                return ring
            P = [np.array(c) for c in orient(g, 1.0).exterior.coords[:-1]]
            changed = True
            break
    return [tuple(p) for p in P]


def _start_corner(w, poly) -> str:
    """墙边的起点在房间的哪个角：按外包的半宽、半高归一后从中心看过去（狭长房间的四个角也分得出 SW/SE/NE/NW）。"""
    return _corner_at(w["a"], poly)


def _corner_at(pt, poly) -> str:
    x0, y0, x1, y1 = poly.bounds
    dx = (pt[0] - (x0 + x1) / 2) / max((x1 - x0) / 2, 1.0)
    dy = (pt[1] - (y0 + y1) / 2) / max((y1 - y0) / 2, 1.0)
    ang = math.degrees(math.atan2(dy, dx)) % 360
    return ("E", "NE", "N", "NW", "W", "SW", "S", "SE")[int((ang + 22.5) // 45) % 8]


def _walls(poly: Polygon, holes=()) -> list[dict]:
    """房间轮廓 → 墙边：去掉门洞条（_unjog）、共线的边合并，逆时针，从最南（再最西）的角起编 W1..Wn。n 指向室内。"""
    ring = _unjog(list(orient(poly.simplify(1.0), 1.0).exterior.coords)[:-1], list(holes))
    k = min(range(len(ring)), key=lambda i: (round(ring[i][1]), round(ring[i][0])))
    ring = ring[k:] + ring[:k]
    out = []
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        out.append({"id": f"W{i + 1}", "a": a, "b": b, "u": u, "n": (-u[1], u[0]), "len": L})
    return out


def _side(w) -> str:
    """墙在房间的哪一边（朝外的法线方向，图纸 y 向上为北）。"""
    ang = math.degrees(math.atan2(-w["n"][1], -w["n"][0])) % 360
    return ("E", "NE", "N", "NW", "W", "SW", "S", "SE")[int((ang + 22.5) // 45) % 8]


def on_wall(w, o) -> tuple[float, float] | None:
    """门窗落在这条墙边上的起止（从墙的起点量，毫米）；不在这条边上返回 None。"""
    a, u, n, L = np.array(w["a"]), np.array(w["u"]), np.array(w["n"]), w["len"]
    if o.get("polygon"):   # 凸窗：轮廓贴着这条边的那一段
        seg = LineString([w["a"], w["b"]]).intersection(Polygon(o["polygon"]).buffer(60))
        if seg.is_empty or seg.length < 100:
            return None
        s = [float((np.array(p) - a) @ u) for g in getattr(seg, "geoms", [seg]) for p in g.coords]
        return max(0.0, min(s) + 60), min(L, max(s) - 60)
    c, uo, _, wd, t, _ = openings.rec_frame(o)
    if abs(float(uo @ n)) > 0.1:
        return None
    s, d = float((c - a) @ u), float((c - a) @ n)
    if not (-(t + 60) <= d <= 60):
        return None
    lo, hi = max(0.0, s - wd / 2), min(L, s + wd / 2)
    return (lo, hi) if hi - lo >= 50 else None


def rooms_of(rec: dict, sch: dict | None) -> list[dict]:
    rec = (sch or {}).get("after") or rec
    use = {r["id"]: r for r in (sch or {}).get("rooms", [])}
    holes = []   # 门窗洞口在墙厚范围里的矩形（多放 20）：门洞条只在这里面
    for o in rec["openings"]:
        if not o.get("polygon"):
            c, u, _, w, t, _ = openings.rec_frame(o)
            holes.append(openings.oriented_box(c[0], c[1], u[0], u[1], w / 2 + 20, t / 2 + 20))
    out = []
    for r in rec["rooms"]:
        u = use.get(r["id"], {})
        name = u.get("usage") or r["name"]
        poly = Polygon(r["polygon"]).buffer(0)
        if poly.geom_type != "Polygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        near = [h for h in holes if h.intersects(poly)]
        out.append({"id": r["id"], "name": name, "type": u.get("type") or room_type(name), "area_m2": r["area_m2"],
                    "poly": poly, "walls": _walls(poly, near)})
    return out


def _openings(rec: dict, sch: dict | None) -> list[dict]:
    return ((sch or {}).get("after") or rec)["openings"]


def room_context(rec: dict, sch: dict | None = None, only=None) -> dict:
    """给 agent 看的房间摘要：每间房的名称、类型、面积、外包尺寸、墙边 W1..Wn（长度、朝向、上面的门窗：沿墙起止，
    门向内开/向外开/推拉，窗台高）。only：只要这些房间（id 或房名）。"""
    ops = _openings(rec, sch)
    rooms = []
    for rm in rooms_of(rec, sch):
        if only and rm["id"] not in only and rm["name"] not in only:
            continue
        x0, y0, x1, y1 = rm["poly"].bounds
        walls = []
        for w in rm["walls"]:
            ws = []
            for o in ops:
                span = on_wall(w, o)
                if span is None:
                    continue
                e = {"id": o["id"], "kind": o["kind"], "from": round(span[0]), "to": round(span[1])}
                if o["kind"] == "door":
                    _, _, n, _, _, op = openings.rec_frame(o)
                    e["opens"] = "slide" if op.startswith("SLIDING") else ("in" if float(n @ np.array(w["n"])) > 0 else "out")
                elif o.get("sill") is not None:   # 识别结果有窗台高才写
                    e["sill"] = o["sill"]
                ws.append(e)
            walls.append({"id": w["id"], "side": _side(w), "start": _start_corner(w, rm["poly"]), "length": round(w["len"]),
                          "openings": sorted(ws, key=lambda e: e["from"])})
        rooms.append({"id": rm["id"], "name": rm["name"], "type": rm["type"], "area_m2": rm["area_m2"],
                      "size_mm": [round(x1 - x0), round(y1 - y0)], "walls": walls})
    return {"rooms": rooms}


# ---------------------------------------------------------------- 意图 → 坐标
COMMON = ("id", "key", "name", "params")
MODES = {"wall": ("wall", "align", "offset", "gap", "facing"),   # 每种摆法认的字段（另加 COMMON）
         "front_of": ("front_of", "gap", "offset"),
         "beside": ("beside", "side", "gap")}


class IntentError(ValueError):
    """整份意图的结构不对（不是对象、rooms 不是对象列表……）：命令行退出 2。单件写错不抛它，只记一行。"""


def num_field(spec, k):
    v = spec.get(k, 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{k} 要写毫米数")
    return float(v)


def text_field(spec, k, what="名字"):
    v = spec.get(k)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{k} 要写{what}")
    return v


def frame(it):
    """家具的 (靠墙边中点, 沿墙 u, 朝里 n)。"""
    a = math.radians(it["rot"])
    return np.array([it["x"], it["y"]]), np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])


def _place(spec: dict, rm: dict, placed: dict, failed: set, later: set) -> dict:
    pid = text_field(spec, "id")
    if pid in placed:
        raise ValueError("id 重复")
    t = TYPES.get(text_field(spec, "key", "类型名"))
    if t is None:
        raise ValueError(f"未知类型 {spec['key']}（见 packs/cn/furniture/furniture.json）")
    modes = [m for m in MODES if m in spec]
    if len(modes) != 1:
        raise ValueError("摆法在 wall、front_of、beside 里选一个")
    mode = modes[0]
    bad = [k for k in spec if k not in COMMON + MODES[mode]]
    if bad:
        raise ValueError(f"{mode} 摆法不认 {'、'.join(bad)}（可用：{'、'.join(COMMON + MODES[mode])}）")
    params = resolve(spec["key"], spec.get("params"))
    if "name" in spec:
        text_field(spec, "name")

    def ref(k):
        rid = text_field(spec, k, "参照那件的 id")
        r = placed.get(rid)
        if r is None:
            raise ValueError(f"参照的 {rid} 没摆成" if rid in failed else f"参照的 {rid} 还没摆（写在它后面）" if rid in later
                             else f"没有 {rid} 这一件")
        return r, geom(r["key"], r.get("params"))

    g = geom(spec["key"], params)
    w, d = g["width"], g["depth"]
    off, gap = num_field(spec, "offset"), num_field(spec, "gap")
    if mode == "wall":
        wid = text_field(spec, "wall", "墙边编号")
        W = next((x for x in rm["walls"] if x["id"] == wid), None)
        if W is None:
            raise ValueError(f"{wid} 不是{rm['name']}的墙（有 W1～W{len(rm['walls'])}）")
        a, u, L = np.array(W["a"]), np.array(W["u"]), W["len"]
        if "facing" in spec:
            if "align" in spec:
                raise ValueError("align 和 facing 只能选一个")
            r, rt = ref("facing")
            p, _, n = frame(r)
            s = float((p + n * rt["depth"] / 2 - a) @ u) + off
        else:
            al = spec.get("align", "center")
            if al not in ("start", "center", "end"):
                raise ValueError("align 是 start、center、end 之一")
            s = {"start": off + w / 2, "end": L - off - w / 2, "center": L / 2 + off}[al]
        if s - w / 2 < -0.5 or s + w / 2 > L + 0.5:
            raise ValueError(f"摆不下：{W['id']} 长 {L:.0f}，{t['name']}宽 {w:.0f}，要占 {s - w / 2:.0f}～{s + w / 2:.0f}")
        pos, rot = a + u * s + np.array(W["n"]) * gap, math.degrees(math.atan2(u[1], u[0]))
    elif mode == "front_of":
        r, rt = ref("front_of")
        p, u, n = frame(r)
        pos, rot = p + n * (rt["depth"] + gap + d) + u * off, r["rot"] + 180.0
    else:
        r, rt = ref("beside")
        if spec.get("side") not in ("left", "right"):
            raise ValueError("side 是 left 或 right（那件家具自己的左右）")
        p, u, _ = frame(r)
        k = -1.0 if spec["side"] == "left" else 1.0
        pos, rot = p + u * k * (rt["width"] / 2 + gap + w / 2), r["rot"]
    it = {"key": spec["key"], "name": spec.get("name") or t["name"], "x": round(float(pos[0]), 1), "y": round(float(pos[1]), 1),
          "z": g["mount_z"], "rot": round(rot % 360.0, 3) % 360.0, "room": rm["id"], "plan_id": pid}
    if params:
        it["params"] = params
    return it


def find_room(rooms, ref):
    if not isinstance(ref, str) or not ref:
        return "room 要写房名或房间 id"
    hit = [r for r in rooms if r["id"] == ref] or [r for r in rooms if r["name"] == ref]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        return f"没有房间 {ref}"
    return f"房名 {ref} 有 {len(hit)} 间（{'、'.join(r['id'] for r in hit)}），写房间 id"


def check_intent(intent) -> None:
    """整份意图的结构：对象，rooms 是对象列表，每块的 items 是列表。不对抛 IntentError（一行原因）。"""
    if not isinstance(intent, dict):
        raise IntentError("布置意图要是 JSON 对象")
    blocks = intent.get("rooms")
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise IntentError("rooms 要是对象列表：[{\"room\": …, \"items\": […]}]")
    for b in blocks:
        if not isinstance(b.get("items", []), list):
            raise IntentError(f"房间 {b.get('room')} 的 items 要是列表")


def solve(intent: dict, rec: dict, sch: dict | None = None) -> tuple[list, list]:
    """布置意图 → (家具 items, 错误)。确定性换算；一件摆不了记一行原因（check="intent"），参照它的也摆不了。
    结构不对（check_intent）抛 IntentError。"""
    check_intent(intent)
    rooms = rooms_of(rec, sch)
    items, errors, placed, failed = [], [], {}, set()
    later = {s.get("id") for b in intent["rooms"] for s in b.get("items", []) if isinstance(s, dict) and isinstance(s.get("id"), str)}
    for blk in intent["rooms"]:
        rm = find_room(rooms, blk.get("room"))
        if isinstance(rm, str):
            errors.append({"room": str(blk.get("room")), "item": "", "check": "intent", "msg": rm})
            failed |= {s.get("id") for s in blk.get("items", []) if isinstance(s, dict) and isinstance(s.get("id"), str)}
            continue
        for n, spec in enumerate(blk.get("items", []), 1):
            sid = spec.get("id") if isinstance(spec, dict) and isinstance(spec.get("id"), str) else f"第 {n} 件"
            try:
                if not isinstance(spec, dict):
                    raise ValueError("要写成对象 {\"id\": …, \"key\": …, 摆法…}")
                it = _place(spec, rm, placed, failed, later)
            except ValueError as e:
                errors.append({"room": rm["name"], "room_id": rm["id"], "item": sid, "check": "intent", "msg": f"{sid}：{e}"})
                failed.add(sid)
                continue
            placed[it["plan_id"]] = it
            items.append(it)
    return items, errors


# ---------------------------------------------------------------- 校验
def to_world(it, g):
    return shapely.affinity.translate(shapely.affinity.rotate(g, it["rot"], origin=(0, 0)), it["x"], it["y"])


def footprint(it) -> Polygon:
    """家具的平面外轮廓（毫米，图纸坐标）。"""
    return to_world(it, geom(it["key"], it.get("params"))["outline"])


def _door_fronts(fr, dep):
    c, u, n, w, t, _ = fr
    return [openings.oriented_box(*(c + n * s * (t / 2 + dep / 2)), u[0], u[1], w / 2, dep / 2) for s in (1, -1)]


def _reach(free, seeds, target, w):
    """空地里能不能从门口走到 target：空地内缩 w/2 后（站得下 w 宽的人）的某一块同时挨着门口和 target。
    内缩少 1mm：净宽正好等于门槛时内缩后只剩一条线，算通得过。"""
    core = free.buffer(-(w / 2 - 1))
    for p in getattr(core, "geoms", [core]):
        if not p.is_empty and p.distance(target) <= w / 2 + 1 and any(p.distance(s) <= w / 2 + 1 for s in seeds):
            return True
    return False


def has_hob(key) -> bool:
    """有灶台的家具：cn_ 橱柜（参数里有 hob）、库里写了 parts 的橱柜。"""
    t = TYPES.get(key) or {}
    return "hob" in t.get("params", {}) or "hob" in t.get("parts", [])


def part_params(it) -> dict:
    """算水槽、灶台位置用的参数：cn_ 类型是补齐缺省值以后的参数，库类型只有宽和进深（按画法的缺省位置）。"""
    t = TYPES[it["key"]]
    return resolve(it["key"], it.get("params")) if "recipe" in t else {"width": t["width"], "depth": t["depth"]}


def _back_wall(it, rm):
    """家具靠墙边贴着的墙边：靠墙边中点离墙线不超过 ON_WALL、投影落在墙边上、家具沿墙方向和墙边平行；墙角处取离得近的。
    不靠墙返回 None。"""
    p, u, _ = frame(it)
    best = None
    for W in rm["walls"]:
        a = np.array(W["a"])
        s, d = float((p - a) @ np.array(W["u"])), float((p - a) @ np.array(W["n"]))
        if abs(float(u @ np.array(W["u"]))) > 0.99 and -1.0 <= s <= W["len"] + 1.0 and abs(d) <= ON_WALL and (best is None or abs(d) < best[1]):
            best = (W, abs(d))
    return best[0] if best else None


def hob_windows(it, rm, ops) -> list:
    """灶台上方的油烟机（rules.hood_width_mm 宽、对着灶台中线）沿墙进了窗洞、或离洞边不到 rules.hood_window_gap_mm 的窗：
    [(窗, 灶台还要沿墙挪多少才离开, 灶台中线, 墙边, 洞口起止)]，都从墙边起点量。只看橱柜靠着的那面墙上的窗、只看沿墙的范围
    （不看窗台、窗顶高低）；没有灶台、不靠墙的返回 []。"""
    if not has_hob(it["key"]) or rm is None:
        return []
    W = _back_wall(it, rm)
    if W is None:
        return []
    p, u, _ = frame(it)
    s = float((p + u * furngen.part_x(part_params(it), "hob") - np.array(W["a"])) @ np.array(W["u"]))
    half, gap = RULES["hood_width_mm"] / 2, RULES["hood_window_gap_mm"]
    out = []
    for o in ops:
        if o["kind"] != "window" or (span := on_wall(W, o)) is None:
            continue
        lo, hi = span[0] - gap, span[1] + gap
        if s + half > lo + 0.5 and s - half < hi - 0.5:
            out.append((o, min(s + half - lo, hi - (s - half)), s, W, span))
    return out


def _vctx(rec, sch) -> dict:
    """校验用的几何（validate 每次都要的，灶台避窗试摆法时只算一遍）：房间、墙、门（洞口和门扇）、窗前要留空的范围。"""
    rooms = {r["id"]: r for r in rooms_of(rec, sch)}
    rec2 = (sch or {}).get("after") or rec
    walls = unary_union([Polygon(w["polygon"]).buffer(0) for w in rec2["walls"] if len(w["polygon"]) >= 3])
    doors = [(o, openings.rec_opening_zones(o), openings.rec_frame(o)) for o in rec2["openings"] if o["kind"] == "door" and not o.get("polygon")]
    dep = RULES["window_front_mm"]

    def bay_front(o):   # 凸窗：本身 + 它在房间墙边上的洞口起止往室内 dep（两侧不外扩，和普通窗一样）
        out = [Polygon(o["polygon"]).buffer(0)]
        for rm in rooms.values():
            for w in rm["walls"]:
                if s := on_wall(w, o):
                    a, u, n = np.array(w["a"]), np.array(w["u"]), np.array(w["n"])
                    out.append(Polygon([a + u * s[0], a + u * s[1], a + u * s[1] + n * dep, a + u * s[0] + n * dep]))
        return unary_union(out)
    wins = [(o, bay_front(o) if o.get("polygon") else unary_union(_door_fronts(openings.rec_frame(o), dep)),
             o["sill"] if o.get("sill") is not None else RULES["window_sill_mm"]) for o in rec2["openings"] if o["kind"] == "window"]
    return {"rooms": rooms, "walls": walls, "doors": doors, "wins": wins, "ops": rec2["openings"]}


def validate(items: list, rec: dict, sch: dict | None = None, ctx: dict | None = None) -> list[dict]:
    """家具（键在 furniture.json 里的 items）的几何校验 → 违规列表 [{room, room_id, item, check, value, unit, with, msg}]。
    check：outside_room 出房间、in_wall 压墙、collision 互相碰撞（高度区间不重叠的不算，挂墙的按高度分层）、
    door_swing 压门扇开启范围、door_front 门口留空（门洞两侧 door_front_mm 进深）、window_blocked 挡窗（顶高过窗台、又占了
    窗前 window_front_mm 进深；窗台高识别结果没有时用 window_sill_mm）、aisle 从房门到使用面没有通道净宽、use_blocked 使用面
    被别的家具、墙占了或伸出房间、hob_window 灶台上方的油烟机沿墙进了窗洞或离洞边不到 hood_window_gap_mm（value 是灶台还要
    挪多少）。ctx：_vctx 算好的几何（不给就现算）。"""
    tol, low = RULES["tolerance_mm2"], RULES["block_below_mm"]
    C = ctx or _vctx(rec, sch)
    rooms, walls, doors, wins = C["rooms"], C["walls"], C["doors"], C["wins"]
    F = []
    for it in items:
        if it["key"] not in TYPES:
            continue
        g = geom(it["key"], it.get("params"))
        uses = [to_world(it, u) for u in g["use"]]
        F.append({"it": it, "id": it.get("plan_id") or it["name"], "fp": footprint(it), "z0": it["z"], "z1": it["z"] + g["height"],
                  "parts": [(to_world(it, p), it["z"] + h) for p, h in g["parts"]], "uses": uses, "use": unary_union(uses) if uses else None, "use_any": g["use_any"], "room": rooms.get(it["room"])})
    out = []

    def add(f, check, value, unit, msg, other=None):
        rm = f["room"]
        out.append({"room": rm["name"] if rm else "", "room_id": f["it"]["room"], "item": f["id"], "check": check,
                    "value": round(value), "unit": unit, "with": other, "msg": f"{f['id']}：{msg}"})

    m2 = lambda a: f"{a / 1e6:.2f}㎡" if a >= 1e4 else f"{a:.0f}mm²"
    for f in F:
        fp, rm = f["fp"], f["room"]
        if rm is None:
            add(f, "outside_room", fp.area, "mm2", f"房间 {f['it']['room']} 不存在")
            continue
        if (a := fp.difference(rm["poly"]).area) > tol:
            add(f, "outside_room", a, "mm2", f"伸出{rm['name']} {m2(a)}")
        if (a := fp.intersection(walls).area) > tol:
            add(f, "in_wall", a, "mm2", f"压墙 {m2(a)}")
        for o, zone, sill in wins:   # 按块算：床身低于窗台不算，床头板高过窗台才算
            hit = [(p.intersection(zone).area, z1) for p, z1 in f["parts"] if z1 > sill]
            if (a := sum(x for x, _ in hit)) > tol:
                top = max(z1 for x, z1 in hit if x > 0)
                add(f, "window_blocked", a, "mm2", f"高 {top:.0f}，挡了窗 {o['id']}（窗台 {sill:.0f}）{m2(a)}", o["id"])
        for o, need, s, W, (w0, w1) in hob_windows(f["it"], rm, C["ops"]):
            hw, gap = RULES["hood_width_mm"], RULES["hood_window_gap_mm"]
            rel = (f"进了窗 {o['id']} 的洞口（{w0:.0f}～{w1:.0f}）" if s + hw / 2 > w0 and s - hw / 2 < w1
                   else f"离窗 {o['id']} 的洞边（{w0:.0f}～{w1:.0f}）不到 {gap:.0f}")
            add(f, "hob_window", need, "mm", f"灶台中线在 {W['id']} 上 {s:.0f}，上方油烟机按 {hw:.0f} 宽占 {s - hw / 2:.0f}～{s + hw / 2:.0f}，"
                f"{rel}，灶台还差 {need:.0f}", o["id"])
        if f["z0"] >= DOOR_H:
            continue
        for o, zs, fr in doors:
            if len(zs) > 1 and (a := fp.intersection(unary_union(zs[1:])).area) > tol:
                add(f, "door_swing", a, "mm2", f"压门 {o['id']} 的开启范围 {m2(a)}", o["id"])
            elif (a := fp.intersection(unary_union(_door_fronts(fr, RULES["door_front_mm"]))).area) > tol:
                add(f, "door_front", a, "mm2", f"占了门 {o['id']} 门口 {RULES['door_front_mm']} 进深的通行区 {m2(a)}", o["id"])
    tree = shapely.STRtree([f["fp"] for f in F])
    for i, f in enumerate(F):
        for j in sorted(int(j) for j in tree.query(f["fp"])):
            g = F[j]
            if j <= i:
                continue
            a = unary_union([p.intersection(q) for p, pz in f["parts"] for q, qz in g["parts"]   # 按块分层：高度区间重叠的块才算
                             if min(pz, qz) > max(f["z0"], g["z0"])]).area
            if a > tol:
                add(f, "collision", a, "mm2", f"和 {g['id']} 重叠 {m2(a)}", g["id"])
    for f in F:
        rm = f["room"]
        if not f["uses"] or rm is None:
            continue

        def blockers(U):   # 这块使用面被谁占了：[(谁, 面积)]
            hit = [(g["id"], a) for g in F if g is not f and g["z0"] < low and (a := U.intersection(g["fp"]).area) > tol]
            if (a := U.intersection(walls).area) > tol:
                hit.append(("墙", a))
            elif (a := U.difference(rm["poly"]).area) > tol:
                hit.append(("房外", a))
            return hit
        say = lambda who, a: f"使用面压墙 {m2(a)}" if who == "墙" else f"使用面伸出房间 {m2(a)}" if who == "房外" else f"使用面被 {who} 占了 {m2(a)}"
        if f["use_any"]:   # 有一块可用就行（床的两侧）：每块都被占才报
            hits = [blockers(U) for U in f["uses"]]
            if all(hits):
                add(f, "use_blocked", sum(a for h in hits for _, a in h), "mm2", "各处使用面都不能用：" + "；".join(say(*x) for h in hits for x in h),
                    hits[0][0][0])
            continue
        for who, a in blockers(f["use"]):
            add(f, "use_blocked", a, "mm2", say(who, a), who)
    # 通道：从这间房的门口能不能走到每件家具的使用面（空地内缩半个净宽后连通）
    for rid, rm in rooms.items():
        mine = [f for f in F if f["it"]["room"] == rid]
        targets = [f for f in mine if f["use"] is not None]
        if not targets:
            continue
        seeds = [b for o, _, fr in doors if rid in o.get("rooms", []) for b in _door_fronts(fr, RULES["door_front_mm"])
                 if rm["poly"].contains(b.centroid)]
        if not seeds:
            continue
        free = rm["poly"].difference(unary_union([f["fp"] for f in mine if f["z0"] < low]))
        need = ROOM_TYPES.get(rm["type"], {}).get("aisle_mm", RULES["aisle_mm"])
        for f in targets:
            if _reach(free, seeds, f["use"], need):
                continue
            got = next((w for w in range(int(need) - 100, 299, -100) if _reach(free, seeds, f["use"], w)), 0)
            add(f, "aisle", got, "mm", f"从门口到使用面没有 {need:.0f} 宽的通道（最宽约 {got}）")
    order = {c: i for i, c in enumerate(CHECKS)}
    return sorted(out, key=lambda v: (v["room_id"], v["item"], order[v["check"]], str(v["with"])))


# ---------------------------------------------------------------- 灶台避窗
def _mm(v):
    v = round(float(v), 1)
    return int(v) if v.is_integer() else v


def _corner_ok(it, rm) -> bool:
    """L 形橱柜的转角段靠着墙：转角段外侧那条边（局部 x = ±width/2，从靠墙边到 return）整条落在房间轮廓上。一字形和别的家具都算行。"""
    if "recipe" not in TYPES[it["key"]]:
        return True
    p = part_params(it)
    ret = float(p.get("return") or 0)
    if ret <= p["depth"]:
        return True
    x = p["width"] / 2 * (1.0 if p.get("side") == "right" else -1.0)
    return to_world(it, LineString([(x, 0.0), (x, ret)])).difference(rm["poly"].exterior.buffer(2.0)).length < 1.0


def _mirrored(p) -> dict:
    """整件左右镜像的参数：水槽、灶台从另一端量，L 形转角翻到另一侧。"""
    pos, w = furngen.positions(p), float(p["width"])
    out = dict(p, sink=_mm(w - pos["sink"]), hob=_mm(w - pos["hob"]))
    if float(p.get("return") or 0) > p["depth"]:
        out["side"] = "left" if p.get("side") == "right" else "right"
    return out


def _swapped(p) -> dict:
    pos = furngen.positions(p)
    return dict(p, sink=_mm(pos["hob"]), hob=_mm(pos["sink"]))


def _on(it, W, s, params=None) -> dict:
    """把这件靠到墙边 W 上，中心离起点 s（摆法同意图的 wall）。"""
    q = np.array(W["a"]) + np.array(W["u"]) * s
    out = dict(it, x=round(float(q[0]), 1), y=round(float(q[1]), 1), rot=round(math.degrees(math.atan2(W["u"][1], W["u"][0])) % 360.0, 3) % 360.0)
    if params is not None:
        out["params"] = params
    return out


def _near(v0, lo, hi, extra=()) -> list:
    """lo～hi 里离 v0 由近到远的候选：STEP 一档，另加 extra（正好离开窗的位置）；一样近的取小的。"""
    vals = {round(v, 1) for v in extra if lo - 0.05 <= v <= hi + 0.05}
    k = 0
    while v0 - k * STEP >= lo - 0.05 or v0 + k * STEP <= hi + 0.05:
        vals |= {round(v, 1) for v in (v0 - k * STEP, v0 + k * STEP) if lo - 0.05 <= v <= hi + 0.05}
        k += 1
    return sorted(vals, key=lambda v: (round(abs(v - v0), 1), v))


def _candidates(it, rm, ops):
    """灶台在窗前时换的摆法，按代价从小到大（见 arrange）：(做法, 说明, 换过的这件)。参数不合法的（furngen.check）不出。"""
    W0, p = _back_wall(it, rm), part_params(it)
    cn = "recipe" in TYPES[it["key"]]
    w = float(p["width"])
    corner = cn and float(p.get("return") or 0) > p["depth"]
    half, gap = RULES["hood_width_mm"] / 2, RULES["hood_window_gap_mm"]
    fr, fu, _ = frame(it)
    a0, u0 = np.array(W0["a"]), np.array(W0["u"])
    s0, sgn = float((fr - a0) @ u0), float(fu @ u0)   # 靠墙边中点在墙边上的位置；家具 +x 和墙边同向为 1
    bounds = [e for o in ops if o["kind"] == "window" and (sp := on_wall(W0, o)) for e in (sp[0] - gap - half, sp[1] + gap + half)]

    def legal(q):
        try:
            resolve(it["key"], q)
            return True
        except ValueError:
            return False
    parts = lambda q: f"水槽 {_mm(furngen.positions(q)['sink'])}、灶台 {_mm(furngen.positions(q)['hob'])}（从台面左端量）"
    if cn:   # 1 同一个位置：对调、镜像
        for code, text, q in (("swap", "水槽、灶台对调", _swapped(p)), ("mirror", "左右镜像" + ("（转角翻到另一侧）" if corner else ""), _mirrored(p))):
            if legal(q):
                yield code, f"{text}，{parts(q)}", dict(it, params=q)
        pos, (lo, hi) = furngen.positions(p), furngen._run(p)   # 2 灶台沿台面挪，水槽不动
        edges = [(e - s0) * sgn + w / 2 for e in bounds] + [pos["sink"] - 700, pos["sink"] + 700]
        for v in _near(pos["hob"], lo + furngen.PART_HALF["hob"], hi - furngen.PART_HALF["hob"], edges):
            q = dict(p, sink=_mm(pos["sink"]), hob=_mm(v))
            if abs(v - pos["hob"]) > 0.05 and legal(q):
                yield "hob", f"灶台沿台面从 {_mm(pos['hob'])} 挪到 {_mm(v)}（水槽不动）", dict(it, params=q)
    if not corner:   # 3 整件沿墙平移（L 形的转角离不开墙角）
        hx = sgn * furngen.part_x(p, "hob")
        for s in _near(s0, w / 2, W0["len"] - w / 2, [e - hx for e in bounds]):
            if abs(s - s0) > 0.05:
                yield "slide", f"沿 {W0['id']} 平移 {s - s0:+.0f}", _shift(it, u0 * (s - s0))
    c0 = fr + fu * (w / 2 if p.get("side") == "right" else -w / 2)   # L 形原来的转角在哪个墙角（靠墙边转角那一头）
    tries = []
    for W in rm["walls"]:   # 4 换一面墙
        if W["id"] == W0["id"] or W["len"] < w - 0.5:
            continue
        if corner:   # 转角放墙角：先原来那一侧，再整件镜像到另一侧；水槽、灶台先照原样再对调
            for q in (p, _mirrored(p)):
                left = q["side"] != "right"
                cp = np.array(W["a"] if left else W["b"], dtype=float)
                where = f"转角在 {_corner_at(cp, rm['poly'])}"
                s = w / 2 if left else W["len"] - w / 2
                keep = float(np.linalg.norm(cp - c0)) < 1.0
                tries += [(not keep, W, s, q, where), (not keep, W, s, _swapped(q), where + "，水槽、灶台对调")]
        else:        # 一字形：靠起点、靠终点、居中；水槽、灶台照原样、对调、镜像
            tries += [(False, W, s, q, where + extra) for s, where in ((w / 2, "靠起点"), (W["len"] - w / 2, "靠终点"), (W["len"] / 2, "居中"))
                      for q, extra in ((p, ""),) + (((_swapped(p), "，水槽、灶台对调"), (_mirrored(p), "，左右镜像")) if cn else ())]
    # L 形转角留在原来那个墙角的先试（换到这个墙角的另一面墙、两条腿换一下，占的地方差不多），再按墙边编号试别的墙角
    for _, W, s, q, where in sorted(tries, key=lambda t: t[0]):
        if not cn:
            yield "wall", f"换到 {W['id']}（{where}）", _on(it, W, s)
        elif legal(q):
            yield "wall", f"换到 {W['id']}（{where}），{parts(q)}", _on(it, W, s, q)


def _shift(it, d) -> dict:
    return dict(it, x=round(float(it["x"] + d[0]), 1), y=round(float(it["y"] + d[1]), 1))


def arrange(items: list, rec: dict, sch: dict | None = None, fixed=()) -> tuple[list, list]:
    """灶台避窗（规则表 rules.hood_window_gap_mm、hood_width_mm，依据 sources.F1）：意图摆出来的橱柜，灶台上方的油烟机沿墙进了
    窗洞或离洞边不到门槛（hob_windows）时，按代价从小到大换一种摆法，取第一个满足的：
      1 同一个位置：水槽、灶台对调；整件左右镜像（L 形转角跟着翻到另一侧，转角段还要靠着墙）
      2 灶台沿台面挪到最近的满足处，水槽不动（还在正面那一段上、离端头 350、和水槽隔 700）
      3 整件沿墙平移，离原位置由近到远（只一字形：L 形的转角离不开墙角）
      4 换一面墙：L 形转角放墙角，先试转角留在原来那个墙角（换到这个墙角的另一面墙，两条腿换一下，占的地方差不多），再按墙边
        编号试别的墙角（先原来那一侧，再整件镜像到另一侧），水槽、灶台先照原样再对调；一字形按墙边编号，靠起点、靠终点、居中，
        水槽、灶台照原样、对调、镜像
    每一种都要参数合法（furngen.check）、灶台离开那面墙上所有的窗、这间房的家具校验（碰撞、挡门、通道……）不多出违规。
    满足规则的一件都不动；都不行照意图摆（校验报 hob_window）。fixed：方案里原有的家具，当障碍、不挪。确定性：同样的输入
    同样的结果。→ (items, 换过的 [{room, room_id, item, how, msg}]，how 是 swap、mirror、hob、slide、wall)。"""
    ctx = _vctx(rec, sch)
    rooms, ops = ctx["rooms"], ctx["ops"]
    out, notes = list(items), []
    vkey = lambda v: (v["item"], v["check"], str(v["with"]))
    for i, it in enumerate(items):
        rm = rooms.get(it.get("room"))
        if it.get("key") not in TYPES or not (bad := hob_windows(it, rm, ops)):
            continue
        mates = [x for j, x in enumerate(out) if j != i and x.get("room") == it["room"]] + [x for x in fixed if x.get("room") == it["room"]]
        viol = lambda c: {vkey(v) for v in validate(mates + [c], rec, sch, ctx) if v["check"] != "hob_window"}
        base = viol(it)
        for how, text, cand in _candidates(it, rm, ops):
            if hob_windows(cand, rm, ops) or not _corner_ok(cand, rm) or not viol(cand) <= base:
                continue
            out[i] = cand
            o, need, s, W, _ = max(bad, key=lambda b: b[1])
            notes.append({"room": rm["name"], "room_id": rm["id"], "item": it.get("plan_id") or it["name"], "how": how,
                          "msg": f"{it.get('plan_id') or it['name']}：灶台中线在 {W['id']} 上 {s:.0f}，油烟机离窗 {o['id']} 不够（差 {need:.0f}）→ {text}"})
            break
    return out, notes


def furnish(rec: dict, sch: dict, intent: dict) -> tuple[dict, dict]:
    """识别 + 方案 + 意图 → (带家具的方案, 校验报告)。方案里以前布置的家具（带 plan_id 的）先去掉再换成这次的；灶台在窗前的
    橱柜先换摆法（arrange，报告的 arranged 写换了哪件、怎么换的，没换的不写这一项）。"""
    items, errors = solve(intent, rec, sch)
    keep = [it for it in sch.get("items", []) if "plan_id" not in it]
    items, moved = arrange(items, rec, sch, fixed=[it for it in keep if it.get("key") in TYPES])
    out = dict(sch, items=keep + items)
    viol = validate(out["items"], rec, out)
    counts = {c: n for c in CHECKS if (n := sum(v["check"] == c for v in viol))}
    rep = {"ok": not errors and not viol, "items": len(items), "errors": errors, "violations": viol, "counts": counts}
    if moved:
        rep["arranged"] = moved
    return out, rep
