"""水电点位：按规则表（packs/cn/mep/points.json）在带家具的方案上布点、分回路、记开关控制哪些灯，再做几何校验。纯 shapely
（.venv 里秒级跑完）；房间、墙边、家具外框、门扇都用 furnish、openings 现成的，坐标是识别结果的图纸毫米。

点位照旧写进方案的 items：{key（符号键）, name, x, y, z, rot, room, plan_id, circuit?, controls?}。plan_id 是
"<点位类型>-<房间 id>-<序号>"；circuit 是回路号（每户各自编：WL1、WX1、WX2……）；controls 是开关控制的灯的 plan_id。
重跑时方案里以前布的点位（plan_id 以点位类型开头的）先去掉再换成这次的，家具和手写的点位不动；只布几户（units）时只换这几户的。

点位的位置只按实物和规则定（面板、家具、门扇的实际尺寸），和出图比例、纸面符号大小无关：图上符号比实物大带来的压线、叠字
由出图避让（annotate.paper_pose 挑摆法，排水点的符号偏开画）。

锚点（规则表和意图共用，不写坐标）：
  beside: 家具, side, gap          家具靠墙线上、家具宽度以外 gap；side 是家具自己的 left、right 或 both
  behind: 家具, dx                 家具背后的墙上（家具组可以指到水槽、灶台、台盆），沿墙偏 dx（面对墙向左为正）
  on: 家具, count, clear           台面背后的墙上均布 count 个，离水槽、灶台、台盆的边（按部件实际宽度）至少 clear；
                                   放不下的个数记进 skipped
  above: 家具                      家具正上方（灯）
  floor: 家具, at, inset, dy       地面上：at 是 center、left、right（离那一侧边 inset，负数在家具外），离墙 dy
  door: room | entrance, gap       房门（有入户门就是入户门）或入户门锁舌一侧的墙上，离门洞边 gap（开关放不下再放宽到
                                   switch_gap_max）；墙垛实物上放不下面板（面板宽 + 100）才放到拐角另一面墙上，再不行放门外那间
  wall: free, prefer, count, fill  沿墙找一段空墙（避开门窗、高家具、已有的点位，插座另避开淋浴、浴缸）；prefer 是 long（缺省）
                                   或 window（有窗的墙）；fill 补到这类房间规则表 min 的个数
  ceiling: center                  房间顶面中心（房名标注点）
家具写规则表 furniture 里的组名（bed、sink……）或 furniture.json 的键。规则另可带 z（换高度）、if（组名：房里有这类家具才布，
!组名：没有才布）、controls（开关控制哪些灯：main 主灯，lights 本房的灯）。插座（家具旁、背后、台面上、空墙）都避开淋浴、浴缸
shower_clear 以内，单写一侧的换另一侧。规则表里没有的户内房间不布点，在 skipped 里记一行。

意图（可选，处理个例；只增删，不写坐标）：
  {"rooms": [{"room": 房名或房间 id, "add": [{"point": 点位类型, 锚点…, "z"?: …}], "remove": [点位类型或 plan_id]}]}
结构不对抛 furnish.IntentError（命令行退出 2）；一条写错只记这一条的原因（check="intent"），别的照做；只放下一部分的也算写错（报放下几个、
还差几个、为什么，放下的留着）。删掉的灯是哪个开关控制的，开关跟着去掉（记进 skipped）。
"""
from __future__ import annotations

import json
import math
import os
import re

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from . import conventions, furngen, furnish, openings, scheme
from .unitview import unit_ids  # noqa: F401（户 id 只有一份，按户出图也用它）

PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "mep"))
with open(os.path.join(PACK, "points.json"), encoding="utf-8") as _fh:
    _P = json.load(_fh)
SOURCES, POINTS, CIRCUITS, FURNITURE, ROOMS, LIMITS = (_P[k] for k in ("sources", "points", "circuits", "furniture", "rooms", "checks"))
with open(conventions.pack_file("symbols.json"), encoding="utf-8") as _fh:
    SYMBOLS = json.load(_fh)["types"]

ANCHORS = ("beside", "behind", "on", "above", "floor", "door", "wall", "ceiling")
OPTS = {"beside": ("side", "gap"), "behind": ("dx",), "on": ("count", "clear"), "above": (), "floor": ("at", "inset", "dy"),
        "door": ("gap",), "wall": ("prefer", "count", "fill"), "ceiling": ()}
ASK_KEYS = ("point", "z", "controls")   # 意图里每条另认的字段（规则表另有 if、src）
PARTS = ("sink", "hob", "basin")
CHECKS = ("in_door", "blocked", "outside_room", "in_wall", "overlap", "socket_count", "switch_reach", "near_shower", "circuit_over")
NO_BLOCK = ("light", "cold", "hot", "drain", "floor_drain")   # 灯、给排水点就是要在家具上方、背后，不查挡没挡
DOOR_H = openings.DEFAULT_DOOR_H
# 手写的点位（没有 plan_id）按符号认类别：数插座、免查遮挡用
SYMBOL_KIND = {}
for _t in POINTS.values():
    for _s in (_t["symbol"].values() if isinstance(_t["symbol"], dict) else [_t["symbol"]]):
        SYMBOL_KIND.setdefault(_s, _t["kind"])


def room_rules(rtype: str) -> dict:
    spec = ROOMS.get(rtype, {})
    return ROOMS[spec["like"]] if "like" in spec else spec


def point_type(it) -> str | None:
    """方案里一个点位是规则表的哪种点位（plan_id 的前缀）；手写的、家具返回 None。"""
    pid = it.get("plan_id")
    t = pid.split("-")[0] if isinstance(pid, str) else None
    return t if t in POINTS and it["key"] in SYMBOLS else None


def _symbol(t: str, n: int = 1) -> str:
    s = POINTS[t]["symbol"]
    if isinstance(s, str):
        return s
    ks = sorted(int(k) for k in s)   # 开关按控制几盏灯选单联、双联、三联
    return s[str(min(max(n, ks[0]), ks[-1]))]


def group(name) -> list[tuple[str, str | None]]:
    """家具组名或家具键 → [(furniture.json 的键, 部件)]，部件是 sink、hob、basin 或 None。认不出抛 ValueError。"""
    if not isinstance(name, str) or not name:
        raise ValueError("家具要写组名或家具键")
    out = []
    for e in FURNITURE.get(name, [name]):
        head, _, tail = e.rpartition(":")
        key, part = (head, tail) if tail in PARTS and head else (e, None)
        if key not in furnish.TYPES:
            raise ValueError(f"没有家具 {name}（组名见 packs/cn/mep/points.json 的 furniture）")
        out.append((key, part))
    return out


def footprint(it) -> Polygon:
    """点位的平面外框（符号库的 body，图纸坐标）。"""
    s = SYMBOLS[it["key"]]
    w, d = s["body"][0], s["body"][1]
    if s.get("body_centered"):
        g = box(-w / 2, -d / 2, w / 2, d / 2)
    elif "body_offset" in s:
        g = box(s["body_offset"][0], s["body_offset"][1], s["body_offset"][0] + w, s["body_offset"][1] + d)
    else:
        g = box(-w / 2, 0, w / 2, d)
    return furnish.to_world(it, g)


def _parts(it) -> list:
    """家具的分块：[(平面轮廓, 底, 顶)]。"""
    return [(furnish.to_world(it, p), it["z"], it["z"] + h) for p, h in furnish.geom(it["key"], it.get("params"))["parts"]]


def _blocked(rm, p, z, h):
    """贴着点位（不到 block_gap）、高度又和点位重叠的家具块：(家具, 顶高)；没有返回 None。"""
    return next(((f, top) for f in rm["furn"] for part, z0, top in _parts(f)
                 if top > z and z0 < z + h and part.distance(p) < LIMITS["block_gap"]), None)


# ---------------------------------------------------------------- 房间
def _env(rec: dict, sch: dict | None) -> dict:
    """布点和校验共用的几何：furnish 的房间（多边形、墙边、类型）加上户、标注点、房里的家具、门、入户门；墙；门洞和门扇。"""
    after = (sch or {}).get("after") or rec
    info = {r["id"]: r for r in after["rooms"]}
    labels = {r["id"]: r.get("label") for r in (sch or {}).get("rooms", [])}
    furn = [it for it in (sch or {}).get("items", []) if it["key"] in furnish.TYPES]
    doors = [o for o in after["openings"] if o["kind"] == "door" and not o.get("polygon")]
    rooms = []
    for rm in furnish.rooms_of(rec, sch):
        r = info[rm["id"]]
        rooms.append(dict(rm, unit=r.get("unit") or "", public=bool(r.get("public")), label=labels.get(rm["id"]) or scheme.label_point(r["polygon"]),
                          furn=[it for it in furn if it["room"] == rm["id"]], doors=[o for o in doors if rm["id"] in o.get("rooms", [])]))
    by = {r["id"]: r for r in rooms}
    for rm in rooms:   # 入户门：平开，一侧是本户的房间，另一侧是室外、公共空间或别的户
        rm["entrance"] = [o for o in rm["doors"] if rm["unit"] and o.get("leafs", 1) != 0 and (
            o.get("exterior") or len(o["rooms"]) == 1
            or any(by.get(x) is None or by[x]["unit"] != rm["unit"] or by[x]["public"] for x in o["rooms"] if x != rm["id"]))]
    return {"rooms": rooms, "by": by, "ops": after["openings"],
            "walls": unary_union([Polygon(w["polygon"]).buffer(0) for w in after["walls"] if len(w["polygon"]) >= 3]),
            "doors": [(o, openings.rec_opening_zones(o), openings.rec_frame(o)) for o in doors]}


def _main_door(rm, by):
    """房门：有入户门用入户门，否则通向最大那间房的门（主卧通客厅的门，不是通主卫的门）。"""
    if rm["entrance"]:
        return rm["entrance"][0]
    other = lambda o: max([by[x]["poly"].area for x in o["rooms"] if x != rm["id"] and x in by] or [0.0])
    return min(rm["doors"], key=lambda o: (-other(o), o["id"]), default=None)


def _furn(rm, name) -> list:
    """本房里属于这一组的家具 [(家具, 部件)]，按方案里的顺序。"""
    want = group(name)
    return [(it, part) for it in rm["furn"] for key, part in want if it["key"] == key]


def _params(it) -> dict:
    t = furnish.TYPES[it["key"]]
    return furnish.resolve(it["key"], it.get("params")) if "recipe" in t else {"width": t["width"], "depth": t["depth"]}


def _part_x(it, part) -> float:
    """部件（水槽、灶台、台盆）中心在家具局部 x 上的位置；没有部件是 0。和 furngen 画的是同一个量法（库里的橱柜没有
    水槽、灶台参数，按 furngen 的缺省位置）。"""
    if part is None:
        return 0.0
    p = _params(it)
    return (-1.0 if p.get("side") == "right" else 1.0) * (-p["width"] / 2 + furngen.positions(p)[part])


def _part_half(it, part) -> float:
    """部件（水槽、灶台、台盆）沿台面的半宽：和 furngen 画的同一个尺寸。"""
    return furngen.part_half(_params(it), part)


# ---------------------------------------------------------------- 锚点 → 坐标
def _num(r, k, default=0.0):
    return furnish.num_field(r, k) if k in r else default


def _deg(u) -> float:
    return math.degrees(math.atan2(u[1], u[0]))


def _wet(rm) -> list:
    """房里的淋浴房、浴缸（插座要离开 shower_clear）。"""
    keys = {k for k, _ in group("wet")}
    return [f for f in rm["furn"] if f["key"] in keys]


def _fits(E, rm, key, pos, rot, z, kind) -> str:
    """点位放在这里行不行（都按实物：符号库的 body）：在房里、不压墙、不在门洞和门扇里（够高的除外）、没被高家具挡住
    （灯、给排水点不查）、插座离淋浴、浴缸不到 shower_clear。行返回 ""，不行返回一句原因（报告的 skipped 用）。"""
    tol = LIMITS["tolerance_mm2"]
    fp = footprint({"key": key, "x": pos[0], "y": pos[1], "rot": rot})
    if fp.difference(rm["poly"]).area > tol:
        return "出房间"
    if fp.intersection(E["walls"]).area > tol:
        return "压墙"
    if z < DOOR_H and (o := next((o for o, zs, _ in E["doors"] if fp.intersection(unary_union(zs)).area > tol), None)):
        return f"在门 {o['id']} 的门洞或门扇里"
    hit = None if kind in NO_BLOCK else _blocked(rm, Point(*pos), z, SYMBOLS[key]["body"][2])
    if hit:
        return f"被{hit[0]['name']}挡住"
    if SYMBOLS[key]["ifc_type"] == "IfcOutletType":
        w = next((f for f in _wet(rm) if furnish.footprint(f).distance(Point(*pos)) < LIMITS["shower_clear"]), None)
        if w:
            return f"离{w['name']}不到 {LIMITS['shower_clear']}"
    return ""


def _latch_ends(o) -> list:
    """门的锁舌一头（铰链对面，墙中线上的洞口端点）：单扇平开门一个，推拉门、双扇门两头都算。"""
    c, u, _, w, _, op = openings.rec_frame(o)
    return [Point(*(c + u * k * w / 2)) for k in {"SINGLE_SWING_LEFT": (1,), "SINGLE_SWING_RIGHT": (-1,)}.get(op, (-1, 1))]


def _door_spot(rm, o, ops, gap, bw):
    """门锁一侧的墙上离门洞边 gap（到点位边）：(位置, 墙边)。墙垛（门洞边到墙角）实物上放不下面板（gap + 面板宽 + 100）
    时放到拐角另一面墙上；推拉门、双扇门没有锁舌，取墙垛长的一头。"""
    walls = rm["walls"]
    hit = [(i, s) for i, W in enumerate(walls) if (s := furnish.on_wall(W, o))]
    if not hit:
        return None
    i, (lo, hi) = hit[0]
    W = walls[i]
    a, u = np.array(W["a"]), np.array(W["u"])
    others = [s for o2 in ops if o2 is not o and (s := furnish.on_wall(W, o2))]
    stop = {"hi": min([W["len"]] + [s[0] for s in others if s[0] >= hi - 1]), "lo": max([0.0] + [s[1] for s in others if s[1] <= lo + 1])}
    pier = {"hi": stop["hi"] - hi, "lo": lo - stop["lo"]}
    c, ud, _, w, _, op = openings.rec_frame(o)
    if op in ("SINGLE_SWING_LEFT", "SINGLE_SWING_RIGHT"):
        sh = float((c + ud * w / 2 * (-1 if op == "SINGLE_SWING_LEFT" else 1) - a) @ u)   # 铰链在墙边上的位置
        end = "hi" if abs(sh - lo) <= abs(sh - hi) else "lo"
    else:
        end = max(("hi", "lo"), key=lambda e: pier[e])
    off = gap + bw / 2
    if pier[end] >= gap + bw + 100 or stop[end] not in (0.0, W["len"]):   # 墙垛够长，或到别的门窗洞口为止（不是墙角）
        return a + u * (hi + off if end == "hi" else lo - off), W
    W2 = walls[(i + 1) % len(walls)] if end == "hi" else walls[i - 1]
    return np.array(W2["a"]) + np.array(W2["u"]) * (off if end == "hi" else W2["len"] - off), W2


def _subtract(span, cuts):
    segs = [span]
    for c0, c1 in sorted(cuts):
        segs = [s for a, b in segs for s in ((a, min(b, c0)), (max(a, c1), b)) if s[1] > s[0]]
    return segs


def _free_spot(E, rm, pts, z, h, bw, kind, prefer, outlet=False):
    """沿墙边找一段空墙：去掉墙角 150、门洞两侧 200、高过窗台的点位所在的窗洞、贴墙又在这个高度上的家具、已有的点位、
    （插座）离淋浴、浴缸不到 shower_clear 的墙，剩下够长的段里，prefer=window 先挑有窗的墙；再挑同类点位少的墙（插座宜分设
    不同墙面）、最长的段，取中点。"""
    best = None
    wet = [furnish.footprint(f).buffer(LIMITS["shower_clear"]) for f in _wet(rm)] if outlet else []
    for i, W in enumerate(rm["walls"]):
        a, u, L = np.array(W["a"]), np.array(W["u"]), W["len"]
        line = LineString([W["a"], W["b"]])
        cut, win = [(0, 150), (L - 150, L)], False
        for o in E["ops"]:
            s = furnish.on_wall(W, o)
            if not s:
                continue
            if o["kind"] == "door":
                if z < DOOR_H:
                    cut.append((s[0] - 200, s[1] + 200))
                continue
            win = True
            if z + h > (o["sill"] if o.get("sill") is not None else furnish.RULES["window_sill_mm"]):
                cut.append((s[0] - 100, s[1] + 100))
        for it in rm["furn"]:
            for part, z0, top in _parts(it):
                if top > z and z0 < z + h and part.distance(line) < LIMITS["block_gap"]:
                    ss = [float((np.array(c) - a) @ u) for c in part.exterior.coords]
                    cut.append((min(ss) - 50, max(ss) + 50))
        for g in wet:
            seg = g.intersection(line)
            if not seg.is_empty:
                ss = [float((np.array(c) - a) @ u) for c in (seg.coords if hasattr(seg, "coords") else
                                                              [q for s_ in seg.geoms for q in s_.coords])]
                cut.append((min(ss) - bw / 2, max(ss) + bw / 2))
        same = 0
        for q in pts:
            if q["room"] == rm["id"] and Point(q["x"], q["y"]).distance(line) < 50:
                s = float((np.array([q["x"], q["y"]]) - a) @ u)
                cut.append((s - bw / 2 - 200, s + bw / 2 + 200))
                same += q["kind"] == kind
        for s0, s1 in _subtract((0.0, L), cut):
            if s1 - s0 < bw + 100:
                continue
            score = (0 if prefer == "window" and win else 1, same, -(s1 - s0), i)
            if best is None or score < best[0]:
                best = (score, a + u * (s0 + s1) / 2, W)
    return best[1:] if best else None


def _spots(r, rm, E, key, z, kind, why) -> list:
    """一条规则（或意图里的一条）的锚点 → [(位置, 转角, 房间 id)]；wall 锚点由 _add 一个一个找。值写错抛 ValueError。
    家具旁、背后、台面上的点位放不下（出房间、压墙、在门洞或门扇里、被高家具挡住）的不要，原因记进 why；单写一侧的换另一侧。
    房门开关放不下时放到门外那间房（同一户）锁舌一侧的墙上，再不行照放，由校验报出来。"""
    a = next(k for k in ANCHORS if k in r)
    v = r[a]
    ok = lambda pos, rot, room=rm: _fits(E, room, key, pos, rot, z, kind)
    if a in ("beside", "behind", "on", "above", "floor") and not _furn(rm, v):
        why.append(f"没有这件家具（{v}）")
        return []
    bw = SYMBOLS[key]["body"][0]
    out = []
    if a == "beside":
        side = r.get("side", "both")
        if side not in ("left", "right", "both"):
            raise ValueError("side 是 left、right 或 both（家具自己的左右）")
        for it, _ in _furn(rm, v):
            p, u, _n = furnish.frame(it)
            w = furnish.geom(it["key"], it.get("params"))["width"]
            cand = [p + u * k * (w / 2 + _num(r, "gap")) for k in {"left": (-1, 1), "right": (1, -1), "both": (-1, 1)}[side]]
            bad = [ok(c, it["rot"]) for c in cand]
            good = [c for c, b in zip(cand, bad) if not b]
            why += [b for b in bad if b] if side == "both" else ([] if good else bad[:1])
            out += [(c, it["rot"], rm["id"]) for c in (good if side == "both" else good[:1])]
        return out
    if a == "behind":
        for it, part in _furn(rm, v):
            p, u, _n = furnish.frame(it)
            c = p + u * (_part_x(it, part) + _num(r, "dx"))
            out += [(c, it["rot"], rm["id"])] if not (b := ok(c, it["rot"])) else []
            why += [b] if b else []
        return out
    if a == "on":
        # 插座中心能放的段：台面两头各让 150，水槽、灶台、台盆按实际宽度两边再让 clear 和半个面板；各段按长度分到个数
        # （每次给"再加一个后间距最大"的段），同一段里均布、相邻中心至少隔一个面板宽加 200；放不下的个数记进 why
        n, clear = _count(r), _num(r, "clear", 100.0)
        for it, _ in _furn(rm, v):
            p, u, _n = furnish.frame(it)
            w = furnish.geom(it["key"], it.get("params"))["width"]
            busy = [(_part_x(it, k), _part_half(it, k)) for k in (("basin",) if "basin" in _params(it) else ("sink", "hob"))]
            free = [s for s in _subtract((-w / 2 + 150, w / 2 - 150), [(x - h - clear - bw / 2, x + h + clear + bw / 2) for x, h in busy])
                    if s[1] - s[0] >= 0]
            k_of, step = [0] * len(free), bw + 200
            for _j in range(n):   # 空段优先（长的先），再给加一个后间距最大的段；加了间距不够的段不再给
                gap_after = lambda i: math.inf if not k_of[i] else (free[i][1] - free[i][0]) / (k_of[i] + 1)
                cand = [i for i in range(len(free)) if gap_after(i) >= step]
                if not cand:
                    break
                k_of[max(cand, key=lambda i: (gap_after(i), free[i][1] - free[i][0], -i))] += 1
            for (a_, b), k in zip(free, k_of):
                for j in range(k):
                    c = p + u * (a_ + (b - a_) * (j + 0.5) / k)
                    out += [(c, it["rot"], rm["id"])] if not (bad := ok(c, it["rot"])) else []
                    why += [bad] if bad else []
            why += [f"台面上放不下（离水槽、灶台、台盆的边至少 {clear:.0f}，插座之间隔 {step:.0f}）"] * (n - sum(k_of))
        return out
    if a == "above":
        for it, part in _furn(rm, v):
            p, u, n = furnish.frame(it)
            c = p + u * _part_x(it, part) + n * furnish.geom(it["key"], it.get("params"))["depth"] / 2 if part \
                else np.array(furnish.footprint(it).centroid.coords[0])
            out.append((c, 0.0, rm["id"]))
        return out
    if a == "floor":
        at = r.get("at", "center")
        if at not in ("center", "left", "right"):
            raise ValueError("at 是 center、left 或 right（家具自己的左右）")
        for it, part in _furn(rm, v):
            p, u, n = furnish.frame(it)
            w = furnish.geom(it["key"], it.get("params"))["width"]
            x = _part_x(it, part) if at == "center" else (w / 2 - _num(r, "inset")) * (1 if at == "right" else -1)
            out.append((p + u * x + n * _num(r, "dy"), it["rot"], rm["id"]))
        return out
    if a == "door":
        if v not in ("room", "entrance"):
            raise ValueError("door 是 room（房门）或 entrance（入户门）")
        o = _main_door(rm, E["by"]) if v == "room" else (rm["entrance"] or [None])[0]
        if o is None:
            why.append("没有入户门" if v == "entrance" else "没有房门")
            return []
        gap = _num(r, "gap", 150.0)
        # 依次试（按实物，和出图比例无关）：本墙离门洞边 gap、放宽到 switch_gap_max（规则允许的区间）；墙垛放不下面板才到拐角
        # 另一面墙（_door_spot）；再不行放门外那间（同一户）。取第一个面板放得下（_fits）、开关离锁舌边不超过 switch_reach 的
        gaps = [gap] + ([LIMITS["switch_gap_max"]] if kind == "switch" and gap < LIMITS["switch_gap_max"] else [])
        rooms = [rm] + ([E["by"][x] for x in o["rooms"] if x != rm["id"] and x in E["by"] and E["by"][x]["unit"] == rm["unit"]] if v == "room" else [])
        cand = [(room, _door_spot(room, o, E["ops"], g, bw)) for room in rooms for g in gaps]
        cand = [(room, hit[0], _deg(hit[1]["u"])) for room, hit in cand if hit]
        reach = lambda c: kind != "switch" or min(Point(*c).distance(q) for q in _latch_ends(o)) <= LIMITS["switch_reach"]
        best = next(((room, c, rot) for room, c, rot in cand if not ok(c, rot, room) and reach(c)),
                    next(((room, c, rot) for room, c, rot in cand if not ok(c, rot, room)), cand[0] if cand else None))
        return [(best[1], best[2], best[0]["id"])] if best else []
    if a == "ceiling":
        if v != "center":
            raise ValueError("ceiling 只有 center（房间顶面中心）")
        return [(np.array(rm["label"], dtype=float), 0.0, rm["id"])]
    raise ValueError(f"{a} 锚点要单独找")


def _count(r) -> int:
    n = r.get("count", 1)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 20:
        raise ValueError("count 要写 1～20 的整数")
    return n


def _check(r, extra=()) -> str:
    """一条规则或意图的字段：点位类型、恰好一个锚点、这个锚点认的字段、数字。返回点位类型；不对抛 ValueError。"""
    if not isinstance(r, dict):
        raise ValueError('要写成对象 {"point": …, 锚点…}')
    t = furnish.text_field(r, "point", "点位类型")
    if t not in POINTS:
        raise ValueError(f"没有点位类型 {t}（见 packs/cn/mep/points.json 的 points）")
    anchors = [k for k in ANCHORS if k in r]
    if len(anchors) != 1:
        raise ValueError(f"锚点在 {'、'.join(ANCHORS)} 里选一个")
    allowed = ASK_KEYS + tuple(extra) + (anchors[0],) + OPTS[anchors[0]]
    bad = [k for k in r if k not in allowed]
    if bad:
        raise ValueError(f"{anchors[0]} 锚点不认 {'、'.join(bad)}（可用：{'、'.join(allowed)}）")
    for k in ("z", "gap", "dx", "inset", "dy", "clear"):
        if k in r:
            furnish.num_field(r, k)
    if "z" in r and not 0 <= r["z"] <= 3000:
        raise ValueError("z 要在 0～3000 之间（毫米，离本层楼面）")
    if "count" in r:
        _count(r)
    if "fill" in r and not isinstance(r["fill"], bool):
        raise ValueError("fill 写 true 或 false")
    if r.get("prefer", "long") not in ("long", "window"):
        raise ValueError("prefer 是 long 或 window")
    if "controls" in r and (r["controls"] not in ("main", "lights") or POINTS[t]["kind"] not in ("switch", "bed_switch")):
        raise ValueError("controls 只给开关，写 main（主灯）或 lights（本房的灯）")
    for k, ok in (("side", ("left", "right", "both")), ("at", ("center", "left", "right")), ("door", ("room", "entrance")),
                  ("ceiling", ("center",)), ("wall", ("free",))):
        if k in r and r[k] not in ok:
            raise ValueError(f"{k} 是 {'、'.join(ok)} 之一")
    if anchors[0] not in ("wall", "door", "ceiling"):
        group(r[anchors[0]])
    if "if" in r:
        group(furnish.text_field(r, "if", "家具组名").lstrip("!"))
    return t


def _add(r, rm, E, pts, need, skipped, ask=False) -> tuple[int, int]:
    """按一条规则（或意图里的一条，ask=True）往 pts 里加点位，返回 (加了几个, 还差几个)；没放的记进 skipped（房间、点位类型、
    锚点、原因）。need：这类房间至少几个（fill 补到这个数；意图里的 fill 已经够数时抛 ValueError 说明原因）。
    新放的家具上方的灯离顶面中心的主灯不到 light_gap 时，不要主灯，这一盏当主灯。"""
    t = r["point"]
    P = POINTS[t]
    cond = r.get("if")
    if cond and bool(_furn(rm, cond.lstrip("!"))) == cond.startswith("!"):
        return 0, 0
    key = _symbol(t)
    z = float(r["z"]) if "z" in r else float(P.get("z", SYMBOLS[key].get("default_height", 0)))
    anchor = next(k for k in ANCHORS if k in r)
    skip = lambda why: skipped.append({"room": rm["name"], "room_id": rm["id"], "point": t, "anchor": f"{anchor}:{r[anchor]}", "reason": why})
    new = lambda pos, rot, room: {"type": t, "kind": P["kind"], "x": float(pos[0]), "y": float(pos[1]), "z": z, "rot": rot, "room": room,
                                  "main": anchor == "ceiling", "controls": r.get("controls")}
    if anchor == "wall":
        have = sum(q["kind"] == P["kind"] for q in pts)
        n = max(0, need.get(P["kind"], 0) - have) if r.get("fill") else _count(r)
        if ask and r.get("fill") and not n:
            what = "插座" if P["kind"] == "socket" else P["name"]
            raise ValueError(f"{rm['name']}已经有 {have} 个{what}，" + (f"不少于规则表的 {need[P['kind']]} 个，不用补" if P["kind"] in need
                                                                    else f"规则表没给{rm['name']}定{what}最少几个，fill 无从补起"))
        for k in range(n):
            hit = _free_spot(E, rm, pts, z, SYMBOLS[key]["body"][2], SYMBOLS[key]["body"][0], P["kind"], r.get("prefer", "long"),
                             SYMBOLS[key]["ifc_type"] == "IfcOutletType")
            if hit is None:
                skip(f"找不到空墙（还差 {n - k} 个）")
                return k, n - k
            pts.append(new(hit[0], _deg(hit[1]["u"]), rm["id"]))
        return n, 0
    why = []
    spots = _spots(r, rm, E, key, z, P["kind"], why)
    for w in why:
        skip(w)
    missing = 0 if not spots else len(why)   # 一件家具都没有时 why 只有一条"没有这件家具"
    for s in spots:
        q = new(*s)
        if q["kind"] == "light" and not q["main"]:
            for m in [m for m in pts if m["main"] and math.hypot(m["x"] - q["x"], m["y"] - q["y"]) < LIMITS["light_gap"]]:
                pts.remove(m)
                q["main"] = True
                skipped.append({"room": rm["name"], "room_id": rm["id"], "point": m["type"], "anchor": "ceiling:center",
                                "reason": f"离{P['name']}（{r[anchor]}上方）只有 {math.hypot(m['x'] - q['x'], m['y'] - q['y']):.0f}，"
                                          f"不到 {LIMITS['light_gap']}：留家具上方那盏"})
        pts.append(q)
    return len(spots), missing


# ---------------------------------------------------------------- 布点
def check_intent(intent) -> None:
    """整份意图的结构：对象，rooms 是对象列表，add、remove 是列表。不对抛 furnish.IntentError（一行原因）。"""
    if not isinstance(intent, dict):
        raise furnish.IntentError("点位意图要是 JSON 对象")
    blocks = intent.get("rooms")
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise furnish.IntentError('rooms 要是对象列表：[{"room": …, "add": […], "remove": […]}]')
    for b in blocks:
        for k in ("add", "remove"):
            if not isinstance(b.get(k, []), list):
                raise furnish.IntentError(f"房间 {b.get('room')} 的 {k} 要是列表")


def place(rec: dict, sch: dict, intent: dict | None = None, units=None) -> tuple[list, list, list]:
    """带家具的方案 [+ 意图] → (点位 items, 意图错误, 没放的 skipped)。units：只布这几户（户 id），缺省全布。
    逐间房按规则表布点，再按意图删、增；开关记控制的灯、按灯数选联数；
    最后按户分回路。确定性：同样输入同样输出。"""
    if intent is not None:
        check_intent(intent)
    E = _env(rec, sch)
    errors, asks, items, skipped = [], {}, [], []
    for blk in (intent or {}).get("rooms", []):
        rm = furnish.find_room(E["rooms"], blk.get("room"))
        if isinstance(rm, str):
            errors.append({"room": str(blk.get("room")), "room_id": "", "item": "", "check": "intent", "msg": rm})
        elif rm["public"]:
            errors.append({"room": rm["name"], "room_id": rm["id"], "item": "", "check": "intent", "msg": f"{rm['name']}是公共空间，不布户内点位"})
        else:
            asks.setdefault(rm["id"], []).append(blk)
    for rm in E["rooms"]:
        if units and rm["unit"] not in units or rm["public"]:   # 公共空间（走道、楼梯、前室）不布户内点位
            continue
        spec = room_rules(rm["type"])
        need, pts, seq = spec.get("min", {}), [], {}
        if not spec.get("rules"):
            skipped.append({"room": rm["name"], "room_id": rm["id"], "point": "", "anchor": "",
                            "reason": f"认不出房间类型（房名 {rm['name']}），规则表里没有这类房间：不布点"})

        def name_new():   # 还没有 plan_id 的点位按类型接着编号
            for q in [q for q in pts if "id" not in q]:
                seq[q["type"]] = seq.get(q["type"], 0) + 1
                q["id"] = f"{q['type']}-{rm['id']}-{seq[q['type']]}"

        def err(what, msg):
            errors.append({"room": rm["name"], "room_id": rm["id"], "item": what, "check": "intent", "msg": f"{what}：{msg}"})
        for r in spec.get("rules", []):
            _check(r, ("if", "src"))   # 规则表写错是约定包的错：直接抛出
            _add(r, rm, E, pts, need, skipped)
        name_new()
        for blk in asks.get(rm["id"], []):
            extra = [k for k in blk if k not in ("room", "add", "remove")]
            if extra:
                err("、".join(map(str, extra)), "意图的房间块只认 room、add、remove，这几项没用上")
            for x in blk.get("remove", []):
                if not isinstance(x, str) or not x:
                    err(str(x), "remove 写点位类型或 plan_id")
                elif not any(q["type"] == x or q["id"] == x for q in pts):
                    err(x, f"{rm['name']}没有这个点位（点位类型见 points.json，plan_id 见 scheme.json）")
                pts = [q for q in pts if q["type"] != x and q["id"] != x]
            for n, r in enumerate(blk.get("add", []), 1):
                what = r["point"] if isinstance(r, dict) and isinstance(r.get("point"), str) else f"第 {n} 条"
                why = []
                try:
                    _check(r)
                    k, missing = _add(r, rm, E, pts, need, why, ask=True)
                    if not k:
                        raise ValueError(f"{rm['name']}里放不下：" + ("；".join(w["reason"] for w in why) or "找不到锚点"))
                except ValueError as e:
                    err(what, str(e))
                    continue
                fails = [w for w in why if w["point"] == r["point"]]
                skipped += [w for w in why if w not in fails]   # 灯距换掉的主灯
                if missing:   # 只放下一部分：算意图错误（放下的留着）
                    err(what, f"{rm['name']}里只放下 {k} 个，还差 {missing} 个："
                        + "；".join(dict.fromkeys(w["reason"] for w in fails)))
                name_new()
        lights = {q["id"] for q in pts if q["kind"] == "light"}
        for q in [q for q in pts if q["controls"]]:   # 控的灯（意图里）都删了的开关一并去掉
            if not any(x["id"] in lights and (q["controls"] == "lights" or x["main"]) for x in pts):
                pts.remove(q)
                skipped.append({"room": rm["name"], "room_id": rm["id"], "point": q["type"], "anchor": f"plan_id:{q['id']}",
                                "reason": "它控制的灯都删了（意图 remove），开关一并去掉"})
        for q in pts:
            it = {"key": "", "name": POINTS[q["type"]]["name"], "x": round(q["x"], 1), "y": round(q["y"], 1), "z": q["z"],
                  "rot": round(q["rot"] % 360.0, 3) % 360.0, "room": q["room"], "plan_id": q["id"]}
            if q["controls"]:
                it["controls"] = [x["id"] for x in pts if x["kind"] == "light" and (q["controls"] == "lights" or x["main"])]
            it["key"] = _symbol(q["type"], len(it.get("controls", [])))
            items.append(it)
    _circuits(items, {r["id"]: r["unit"] for r in E["rooms"]})
    return items, errors, skipped


def _circuits(items, unit_of) -> None:
    """按户、按回路前缀（规则表 circuits 的顺序）依次编号；有 max 的满了换下一路（weight：柜机占 2 个名额，挂机 1 个）。
    一个前缀里先排名额多的（柜机单独一路，挂机两台一路），名额相同的按方案里的顺序。"""
    for unit in dict.fromkeys(unit_of[it["room"]] for it in items):
        for pre, c in CIRCUITS.items():
            k, used = 1, 0
            weight = lambda it: c.get("weight", {}).get(point_type(it), 1)
            for it in sorted((it for it in items if unit_of[it["room"]] == unit and POINTS[point_type(it)].get("circuit") == pre),
                             key=lambda it: -weight(it)):
                t = point_type(it)
                w = c.get("weight", {}).get(t, 1)
                if c.get("max") and used and used + w > c["max"]:
                    k, used = k + 1, 0
                used += w
                it["circuit"] = f"{pre}{k}"



# ---------------------------------------------------------------- 校验
def validate(items: list, rec: dict, sch: dict | None = None, units=None) -> list[dict]:
    """点位（键在符号库里、不是家具的 items）的校验 → 违规列表 [{room, room_id, item, check, value, unit, with, msg}]。check：
    in_door 落在门洞里或门扇开启范围里（门后；开关的门后归 switch_reach）、blocked 被高家具挡住（点位离家具某一块不到 block_gap、
    高度又和这一块重叠；灯、给排水点不查）、outside_room 出房间、in_wall 压墙、overlap 和同一间房里另一个点位叠在一起（平面外框
    重叠、高度也重叠）、socket_count 插座少于规则表的 min、switch_reach 房门开关离锁舌边超过 switch_reach 或在门扇开启范围里、
    near_shower 插座离淋浴、浴缸不到 shower_clear、circuit_over 一路超过上限。units：只查这几户的点位、插座数和回路。"""
    E = _env(rec, dict(sch or {}, items=items))
    by, tol = E["by"], LIMITS["tolerance_mm2"]
    wet = {k for k, _ in group("wet")}
    kind_of = lambda it: POINTS[point_type(it)]["kind"] if point_type(it) else SYMBOL_KIND.get(it["key"])
    out = []

    def add(rm, item, check, value, unit, msg, other=None):
        out.append({"room": rm["name"] if rm else "", "room_id": rm["id"] if rm else "", "item": item, "check": check,
                    "value": round(value), "unit": unit, "with": other, "msg": f"{item or (rm['name'] if rm else '')}：{msg}"})

    mine = lambda it: not units or (by.get(it["room"]) or {}).get("unit") in units
    pts = [it for it in items if it["key"] in SYMBOLS and it["key"] not in furnish.TYPES and mine(it)]
    for i, it in enumerate(pts):   # 叠在一起：同一间房、平面外框重叠、高度（z 到 z + body 高）也重叠
        rm, fp, h = by.get(it["room"]), footprint(it), SYMBOLS[it["key"]]["body"][2]
        for o in pts[:i]:
            if o["room"] == it["room"] and rm and (a := fp.intersection(footprint(o)).area) > tol \
                    and it["z"] < o["z"] + SYMBOLS[o["key"]]["body"][2] and o["z"] < it["z"] + h:
                add(rm, it.get("plan_id") or it["name"], "overlap", a, "mm2", f"和 {o.get('plan_id') or o['name']} 叠在一起 {a:.0f}mm²",
                    o.get("plan_id") or o["name"])
    for it in pts:
        kind = kind_of(it)
        name, rm, fp, p = it.get("plan_id") or it["name"], by.get(it["room"]), footprint(it), Point(it["x"], it["y"])
        if rm is None:
            add(None, name, "outside_room", fp.area, "mm2", f"房间 {it['room']} 不存在")
            continue
        if (a := fp.difference(rm["poly"]).area) > tol:
            add(rm, name, "outside_room", a, "mm2", f"伸出{rm['name']} {a:.0f}mm²")
        if (a := fp.intersection(E["walls"]).area) > tol:
            add(rm, name, "in_wall", a, "mm2", f"压墙 {a:.0f}mm²")
        if it["z"] < DOOR_H and kind != "light":
            for o, zs, _ in E["doors"]:
                if (a := fp.intersection(zs[0]).area) > tol:
                    add(rm, name, "in_door", a, "mm2", f"落在门 {o['id']} 的门洞里", o["id"])
                elif kind != "switch" and len(zs) > 1 and (a := fp.intersection(unary_union(zs[1:])).area) > tol:
                    add(rm, name, "in_door", a, "mm2", f"在门 {o['id']} 的门扇开启范围里（门后）", o["id"])
        if kind not in NO_BLOCK and (hit := _blocked(rm, p, it["z"], SYMBOLS[it["key"]]["body"][2])):
            f, top = hit
            add(rm, name, "blocked", top, "mm", f"被{f['name']}挡住（离它不到 {LIMITS['block_gap']}，它高 {top:.0f}，点位高 {it['z']:.0f}）",
                f.get("plan_id") or f["name"])
        if point_type(it) and kind == "switch":
            ends = []
            for o, zs, (c, u, n, w, tk, op) in E["doors"]:
                if rm["id"] in o.get("rooms", []):
                    ends += [(p.distance(q), o, zs) for q in _latch_ends(o)]
            if ends:
                d, o, zs = min(ends, key=lambda e: (e[0], e[1]["id"]))
                if d > LIMITS["switch_reach"]:
                    add(rm, name, "switch_reach", d, "mm", f"离门 {o['id']} 的锁舌边 {d:.0f}，超过 {LIMITS['switch_reach']}", o["id"])
                elif len(zs) > 1 and (a := fp.intersection(unary_union(zs[1:])).area) > tol:
                    add(rm, name, "switch_reach", a, "mm2", f"在门 {o['id']} 的门扇开启范围里，开门后够不着", o["id"])
        if SYMBOLS[it["key"]]["ifc_type"] == "IfcOutletType":
            for f in rm["furn"]:
                if f["key"] in wet and (d := furnish.footprint(f).distance(p)) < LIMITS["shower_clear"]:
                    add(rm, name, "near_shower", d, "mm", f"离{f['name']}只有 {d:.0f}，不到 {LIMITS['shower_clear']}", f.get("plan_id") or f["name"])
    for rm in E["rooms"]:
        need = room_rules(rm["type"]).get("min", {}).get("socket") if not units or rm["unit"] in units else None
        have = sum(1 for it in pts if it["room"] == rm["id"] and kind_of(it) == "socket")
        if need and have < need:
            add(rm, "", "socket_count", have, "个", f"插座 {have} 个，少于 {need} 个")
    load = {}
    for it in pts:
        if it.get("circuit"):
            k = ((by.get(it["room"]) or {}).get("unit", ""), it["circuit"])
            c = CIRCUITS.get(re.match(r"[A-Z]*", it["circuit"]).group(0), {})
            load[k] = load.get(k, 0) + c.get("weight", {}).get(point_type(it), 1)
    for (unit, cid), n in sorted(load.items()):
        c = CIRCUITS.get(re.match(r"[A-Z]*", cid).group(0), {})
        if c.get("max") and n > c["max"]:
            out.append({"room": "", "room_id": "", "item": cid, "check": "circuit_over", "value": n, "unit": "个", "with": unit or None,
                        "msg": f"{cid}：{unit + ' ' if unit else ''}这一路 {n} 个，超过 {c['max']}"})
    order = {c: i for i, c in enumerate(CHECKS)}
    return sorted(out, key=lambda v: (v["room_id"], v["item"], order[v["check"]], str(v["with"])))


def mep(rec: dict, sch: dict, intent: dict | None = None, units=None) -> tuple[dict, dict]:
    """识别 + 带家具的方案 [+ 意图] → (带点位的方案, 校验报告)。方案里以前布的点位先去掉再换成这次的。units：只布这几户
    （别的户以前布的点位原样留着、不重新校验）；户 id 不在识别结果里抛 ValueError。"""
    unit = {r["id"]: r.get("unit") or "" for r in ((sch or {}).get("after") or rec)["rooms"]}
    if units and (bad := [u for u in units if u not in unit_ids(rec, sch)]):
        raise ValueError(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unit_ids(rec, sch)) or '无'}）")
    keep = [it for it in sch.get("items", []) if point_type(it) is None or (units and unit.get(it["room"]) not in units)]
    items, errors, skipped = place(rec, dict(sch, items=keep), intent, units)
    out = dict(sch, items=keep + items)
    viol = validate(out["items"], rec, out, units)
    circuits = {}
    for it in items:
        if it.get("circuit"):
            c = circuits.setdefault(unit[it["room"]], {})
            c[it["circuit"]] = c.get(it["circuit"], 0) + 1
    counts = {c: n for c in CHECKS if (n := sum(v["check"] == c for v in viol))}
    return out, {"ok": not errors and not viol, "points": len(items), "errors": errors, "violations": viol, "counts": counts, "circuits": circuits,
                 "skipped": skipped}
