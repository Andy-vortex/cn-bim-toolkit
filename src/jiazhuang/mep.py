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
        over, clear                或 window（有窗的墙）；fill 补到这类房间规则表 min 的个数；over（家具）只在这件家具台面上方的
                                   墙段里找（_over_spans：正面一段和 L 形转角段靠着的墙，离水槽、灶台、台盆的边至少 clear），
                                   放不下就少放、记进 skipped，不放到没有台面的墙上
  ceiling: center                  房间顶面中心（房名标注点）
家具写规则表 furniture 里的组名（bed、sink……）或 furniture.json 的键。规则另可带 z（换高度）、if（组名：房里有这类家具才布，
!组名：没有才布）、controls（开关控制哪些灯：main 主灯，lights 本房的灯）。插座（家具旁、背后、台面上、空墙）都避开淋浴、浴缸
shower_clear 以内，单写一侧的换另一侧。规则表里没有的户内房间不布点，在 skipped 里记一行。
墙上装的点位（checks.wall_types）不压窗：盒子的高度和窗台～窗顶（openings.rec_extent，和建模、墙面同一套）重叠时，沿墙离窗洞边
至少 window_gap_mm。家具旁、背后、台面上的点位压窗时顺着墙挪到最近的允许位置（_slide），离原位置不超过 window_slide_mm；挪不下
走原来的退路（换另一侧、不放）。家具旁的专用插座（冰箱这类）两侧都放不下时退到它服务的那件家具背后正中，那件家具不算挡着它。

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
from shapely.ops import linemerge, unary_union

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
        "door": ("gap",), "wall": ("prefer", "count", "fill", "over", "clear"), "ceiling": ()}
ASK_KEYS = ("point", "z", "controls")   # 意图里每条另认的字段（规则表另有 if、src）
PARTS = ("sink", "hob", "basin")
CHECKS = ("in_door", "in_window", "blocked", "outside_room", "in_wall", "overlap", "socket_count", "switch_reach", "near_shower",
          "circuit_over", "ceiling_light", "circuit_gang")
CEIL_ANCHORS = ("ring", "slot", "axis")   # 跟吊顶走的灯（筒灯、灯带）的锚点：方案里没有吊顶块时一个都不放、也不记 skipped
GROUPED = ("downlight", "strip")          # 成组控制的灯：同一组连成一串，开关只接这一组的第一盏
NO_BLOCK = ("light", "cold", "hot", "drain", "floor_drain")   # 灯、给排水点就是要在家具上方、背后，不查挡没挡
DOOR_H = openings.DEFAULT_DOOR_H
ON_WALL_MM = 50     # 点位插入点离墙线这么近算装在这面墙上（数一面墙上已有的点位、查压窗都用它）
SLIDE_STEP = 10     # 压窗的点位顺着墙找位置的步长（毫米）
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


def _blocked(rm, p, z, h, own=()):
    """贴着点位（不到 block_gap）、高度又和点位重叠的家具块：(家具, 顶高)；没有返回 None。own 里的家具不算（专用插座服务的
    那件电器，_own）。"""
    skip = {id(f) for f in own}
    return next(((f, top) for f in rm["furn"] if id(f) not in skip for part, z0, top in _parts(f)
                 if top > z and z0 < z + h and part.distance(p) < LIMITS["block_gap"]), None)


def _own(rm, t, pos) -> list:
    """专用插座（kind dedicated）在它服务的电器背后时的那件家具：规则表这类房间里这种点位锚到（beside、behind）的家具里，
    点位落在它靠墙边上、宽度以内的。布点时退到电器背后的就是这种，挡不挡不查它（点位就是给它用的），校验也不报。"""
    if t not in POINTS or POINTS[t]["kind"] != "dedicated":
        return []
    keys = {k for r in room_rules(rm["type"]).get("rules", []) if r.get("point") == t
            for a in ("beside", "behind") if a in r for k, _ in group(r[a])}
    out = []
    for f in rm["furn"]:
        if f["key"] not in keys:
            continue
        p, u, n = furnish.frame(f)
        d = np.asarray(pos, dtype=float) - p
        if abs(float(d @ u)) <= furnish.geom(f["key"], f.get("params"))["width"] / 2 + 0.5 and -ON_WALL_MM <= float(d @ n) <= 0.5:
            out.append(f)
    return out


# ---------------------------------------------------------------- 房间
def _env(rec: dict, sch: dict | None) -> dict:
    """布点和校验共用的几何：furnish 的房间（多边形、墙边、类型）加上户、标注点、房里的家具、门、入户门；墙；门洞和门扇。"""
    after = (sch or {}).get("after") or rec
    info = {r["id"]: r for r in after["rooms"]}
    labels = {r["id"]: r.get("label") for r in (sch or {}).get("rooms", [])}
    furn = [it for it in (sch or {}).get("items", []) if it["key"] in furnish.TYPES]
    doors = [o for o in after["openings"] if o["kind"] == "door" and not o.get("polygon")]
    width0 = {o["id"]: float(o["width"]) for o in rec["openings"]}   # 拆改前的识别宽度（拆改改了宽度的，原图编号和它的高度不沿用）
    rooms = []
    for rm in furnish.rooms_of(rec, sch):
        r = info[rm["id"]]
        rooms.append(dict(rm, unit=r.get("unit") or "", public=bool(r.get("public")), label=labels.get(rm["id"]) or scheme.label_point(r["polygon"]),
                          furn=[it for it in furn if it["room"] == rm["id"]], doors=[o for o in doors if rm["id"] in o.get("rooms", [])],
                          windows=_windows(rm, after["openings"], width0)))
    by = {r["id"]: r for r in rooms}
    for rm in rooms:   # 入户门：平开，一侧是本户的房间，另一侧是室外、公共空间或别的户
        rm["entrance"] = [o for o in rm["doors"] if rm["unit"] and o.get("leafs", 1) != 0 and (
            o.get("exterior") or len(o["rooms"]) == 1
            or any(by.get(x) is None or by[x]["unit"] != rm["unit"] or by[x]["public"] for x in o["rooms"] if x != rm["id"]))]
    ceil = {}
    for b in (sch or {}).get("ceilings", []):
        ceil.setdefault(b["room"], []).append(b)
    return {"rooms": rooms, "by": by, "ops": after["openings"], "ceil": ceil, "width0": width0,
            "walls": unary_union([Polygon(w["polygon"]).buffer(0) for w in after["walls"] if len(w["polygon"]) >= 3]),
            "doors": [(o, openings.rec_opening_zones(o), openings.rec_frame(o)) for o in doors]}


def _windows(rm, ops, width0) -> list:
    """房间各墙边上的窗、凸窗：[(窗, 墙边 id, 起, 止, 窗台, 窗顶)]。起止从墙边起点量（furnish.on_wall），窗台、窗顶是
    openings.rec_extent（和建模、墙面挖的洞同一套）。"""
    out = []
    for o in ops:
        if o["kind"] == "door":
            continue
        v0, v1 = openings.rec_extent(o, width0.get(o["id"]))
        out += [(o, W["id"], s[0], s[1], v0, v1) for W in rm["walls"] if (s := furnish.on_wall(W, o))]
    return out


def _wall_of(rm, pos):
    """点位插入点在哪面墙上：(墙边, 从起点量的 s, 离墙线 d)，离墙线不超过 ON_WALL_MM、投影落在墙边上；墙角处取离得近的。
    都不在返回 None。"""
    p, best = np.asarray(pos, dtype=float), None
    for W in rm["walls"]:
        a, u, n = np.array(W["a"]), np.array(W["u"]), np.array(W["n"])
        s, d = float((p - a) @ u), float((p - a) @ n)
        if -1.0 <= s <= W["len"] + 1.0 and abs(d) <= ON_WALL_MM and (best is None or abs(d) < abs(best[2])):
            best = (W, s, d)
    return best


def _in_window(rm, key, pos, z):
    """墙上装的点位（checks.wall_types）压不压窗：盒子的高度（z 到 z + 盒子高）和窗台～窗顶重叠、沿墙离洞边不到 window_gap_mm
    （压进洞口宽度的更不行）→ (窗, 盒子边离洞边多远：负数是压进洞口宽度)，几樘都压时取压得最深的；不压返回 None。"""
    if SYMBOLS[key]["ifc_type"] not in LIMITS["wall_types"] or not rm.get("windows"):
        return None
    hit = _wall_of(rm, pos)
    if hit is None:
        return None
    W, s, _ = hit
    bw, _, h = SYMBOLS[key]["body"]
    bad = [(o, max(lo - (s + bw / 2), (s - bw / 2) - hi)) for o, wid, lo, hi, v0, v1 in rm["windows"]
           if wid == W["id"] and z < v1 - 0.5 and z + h > v0 + 0.5]
    bad = [(o, g) for o, g in bad if g < LIMITS["window_gap_mm"] - 0.5]
    return min(bad, key=lambda x: (x[1], x[0]["id"])) if bad else None


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
    """部件（水槽、灶台、台盆）中心在家具局部 x 上的位置；没有部件是 0。就是 furngen.part_x，和画出来的同一个位置（转角在右侧的
    橱柜也一样；库里的橱柜没有水槽、灶台参数，按 furngen 的缺省位置）。"""
    if part is None:
        return 0.0
    return furngen.part_x(_params(it), part)


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


def _fits(E, rm, key, pos, rot, z, kind, own=()) -> str:
    """点位放在这里行不行（都按实物：符号库的 body）：在房里、不压墙、不在门洞和门扇里（够高的除外）、墙上装的不压窗
    （_in_window）、没被高家具挡住（灯、给排水点不查；own 里的家具不算）、插座离淋浴、浴缸不到 shower_clear。行返回 ""，
    不行返回一句原因（报告的 skipped 用）。"""
    tol = LIMITS["tolerance_mm2"]
    fp = footprint({"key": key, "x": pos[0], "y": pos[1], "rot": rot})
    if fp.difference(rm["poly"]).area > tol:
        return "出房间"
    if fp.intersection(E["walls"]).area > tol:
        return "压墙"
    if z < DOOR_H and (o := next((o for o, zs, _ in E["doors"] if fp.intersection(unary_union(zs)).area > tol), None)):
        return f"在门 {o['id']} 的门洞或门扇里"
    if (w := _in_window(rm, key, pos, z)):
        return f"压窗 {w[0]['id']}（离洞边不到 {LIMITS['window_gap_mm']}）"
    hit = None if kind in NO_BLOCK else _blocked(rm, Point(*pos), z, SYMBOLS[key]["body"][2], own)
    if hit:
        return f"被{hit[0]['name']}挡住"
    if SYMBOLS[key]["ifc_type"] == "IfcOutletType":
        w = next((f for f in _wet(rm) if furnish.footprint(f).distance(Point(*pos)) < LIMITS["shower_clear"]), None)
        if w:
            return f"离{w['name']}不到 {LIMITS['shower_clear']}"
    return ""


def _overlaps(key, pos, rot, z, taken) -> bool:
    """和已放的点位（taken：[{key, x, y, z, rot}]）叠在一起：平面外框重叠、高度也重叠（同 validate 的 overlap）。"""
    fp, h = footprint({"key": key, "x": pos[0], "y": pos[1], "rot": rot}), SYMBOLS[key]["body"][2]
    return any(z < q["z"] + SYMBOLS[q["key"]]["body"][2] and q["z"] < z + h and fp.intersection(footprint(q)).area > LIMITS["tolerance_mm2"]
               for q in taken)


def _slide(E, rm, key, pos, rot, z, kind, taken=(), keep=None, reach=None):
    """压窗的墙上点位顺着所在的墙边挪到最近的允许位置：_fits 通过（离每樘窗的洞边至少 window_gap_mm、不出房间、不压墙、不在
    门洞门扇里、没被挡住……）、不和已放的点位叠在一起（taken）、keep(位置) 为真（台面插座：还在台面上能放的段里）。先试正好离
    洞边 window_gap_mm 的地方，再往外每 SLIDE_STEP 一试，取离原位置最近的（一样近取 s 小的）；只在同一面墙边上、离原位置不超过
    reach（None 不限）的范围里找，找不到返回 None。"""
    hit = _wall_of(rm, pos)
    if hit is None:
        return None
    W, s0, d = hit
    a, u, n = np.array(W["a"]), np.array(W["u"]), np.array(W["n"])
    bw, _, h = SYMBOLS[key]["body"]
    gap = LIMITS["window_gap_mm"]
    cand = set()
    for o, wid, lo, hi, v0, v1 in rm["windows"]:
        if wid != W["id"] or not (z < v1 - 0.5 and z + h > v0 + 0.5):
            continue
        for e, step in ((lo - gap - bw / 2, -SLIDE_STEP), (hi + gap + bw / 2, SLIDE_STEP)):
            t = e
            while -1.0 <= t <= W["len"] + 1.0:
                cand.add(round(t, 1))
                t += step
    for t in sorted(cand, key=lambda t: (abs(t - s0), t)):
        if reach is not None and abs(t - s0) > reach + 0.5:
            break
        c = a + u * t + n * d
        if not _fits(E, rm, key, c, rot, z, kind) and not _overlaps(key, c, rot, z, taken) and (keep is None or keep(c)):
            return c
    return None


def _settle(E, rm, key, pos, rot, z, kind, taken=(), keep=None) -> tuple:
    """(位置, 原因)：放得下（_fits）原样返回；压窗的顺着墙挪（_slide），离原位置 window_slide_mm 以内挪得下返回新位置；挪不下或
    别的原因放不下返回原位置和原因。压窗挪不下的原因里写明同一面墙上最近放得下的位置有多远（超过上限的那个距离）。
    上限的道理：挪得再远，插头线够不着，也不在这件家具旁边或背后了，就不是给它用的点位。"""
    why = _fits(E, rm, key, pos, rot, z, kind)
    if not why or not _in_window(rm, key, pos, z):
        return pos, why
    reach = LIMITS["window_slide_mm"]
    if (c := _slide(E, rm, key, pos, rot, z, kind, taken, keep, reach)) is not None:
        return c, ""
    far = _slide(E, rm, key, pos, rot, z, kind, taken, keep)
    d = None if far is None else float(np.hypot(*(np.asarray(far, dtype=float) - np.asarray(pos, dtype=float))))
    return pos, why + ("，同一面墙上挪不开" if d is None else f"，同一面墙上最近放得下的位置要挪 {d:.0f}，超过 {reach}")


def _behind(E, rm, key, it, z, kind, taken=()) -> tuple:
    """专用插座退到它服务的家具 it 背后正中：家具局部 x = 0 那条线和墙线的交点，高度不变 → (位置, 原因)，放得下原因是 ""。
    it 自己不算挡着它（点位就是给它用的）；别的照查：压窗、门洞门扇、别的家具挡、压墙、出房间、和已放的点位（taken）叠。"""
    p = np.asarray(furnish.frame(it)[0], dtype=float)
    hit = _wall_of(rm, p)
    if hit is None:
        return p, "背后没有墙"
    W, _s, d = hit
    c = p - np.array(W["n"], dtype=float) * d
    why = _fits(E, rm, key, c, it["rot"], z, kind, (it,))
    return c, why or ("和已放的点位叠在一起" if _overlaps(key, c, it["rot"], z, taken) else "")


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


def _over_spans(rm, name, clear, bw) -> dict:
    """台面上方能放点位中心的墙段：{墙边 id: [(起, 止)]}（从墙边起点量）。这一组的每件家具：正面一段靠着的墙上，台面两头各让
    150，水槽、灶台、台盆按实际宽度两边再让 clear 和半个面板（同 on 锚点）；L 形（return 大于进深）转角段外侧那条边靠着的墙上，
    从墙角到转角段端头（端头让 150；转角段上没有水槽、灶台）。边不靠墙（离墙线超过 ON_WALL_MM）的那一段不算：台面上方没有墙。"""
    out = {}
    for it, _ in _furn(rm, name):
        p, u, n = furnish.frame(it)
        g, prm = furnish.geom(it["key"], it.get("params")), _params(it)
        w, d, ret = g["width"], g["depth"], float(prm.get("return") or 0)
        busy = [(_part_x(it, k), _part_half(it, k)) for k in (("basin",) if "basin" in prm else ("sink", "hob"))]
        front = _subtract((-w / 2 + 150, w / 2 - 150), [(x - h - clear - bw / 2, x + h + clear + bw / 2) for x, h in busy])
        runs = [(p - u * w / 2, u, w, [(s0 + w / 2, s1 + w / 2) for s0, s1 in front])]   # (起点, 方向, 全长, 从起点量能放的段)
        if ret > d:
            k = 1.0 if prm.get("side") == "right" else -1.0
            runs.append((p + u * k * w / 2, n, ret, [(0.0, ret - 150)]))
        for A, v, length, segs in runs:
            for W in rm["walls"]:
                a, wu, wn = (np.array(W[c], dtype=float) for c in ("a", "u", "n"))
                sg, s0 = float(v @ wu), float((A - a) @ wu)
                if abs(sg) < 0.99 or abs(float((A - a) @ wn)) > ON_WALL_MM or min(s0, s0 + sg * length) < -1.0 \
                        or max(s0, s0 + sg * length) > W["len"] + 1.0:
                    continue
                out.setdefault(W["id"], []).extend(sorted((s0 + sg * t0, s0 + sg * t1)) for t0, t1 in segs if t1 >= t0)
                break
    return out


def _free_spot(E, rm, pts, z, h, bw, kind, prefer, outlet=False, within=None):
    """沿墙边找一段空墙：去掉墙角 150、门洞两侧 200、高过窗台的点位所在的窗洞（窗顶以上也不放，两侧各让 window_gap_mm；窗台和
    建模同一套 openings.rec_extent）、贴墙又在这个高度上的家具、已有的点位、
    （插座）离淋浴、浴缸不到 shower_clear 的墙，剩下够长的段里，prefer=window 先挑有窗的墙；再挑同类点位少的墙（插座宜分设
    不同墙面）、最长的段，取中点。within（_over_spans）给了时只认中心落在这些段里的位置：中点在里面照旧取中点，不在就取这一段
    落在里面的那部分（最长的）的中点，一点都不落在里面的段不要。"""
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
            if z + h > openings.rec_extent(o, E["width0"].get(o["id"]))[0]:
                cut.append((s[0] - LIMITS["window_gap_mm"], s[1] + LIMITS["window_gap_mm"]))
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
            if q["room"] == rm["id"] and Point(q["x"], q["y"]).distance(line) < ON_WALL_MM:
                s = float((np.array([q["x"], q["y"]]) - a) @ u)
                cut.append((s - bw / 2 - 200, s + bw / 2 + 200))
                same += q["kind"] == kind
        for s0, s1 in _subtract((0.0, L), cut):
            if s1 - s0 < bw + 100:
                continue
            pos = a + u * (s0 + s1) / 2
            if within is not None:   # 中心能放的范围（离两头够 bw/2 + 50）和台面上方的段的交集
                lo, hi = s0 + bw / 2 + 50, s1 - bw / 2 - 50
                inner = [(max(lo, c0), min(hi, c1)) for c0, c1 in within.get(W["id"], []) if max(lo, c0) <= min(hi, c1)]
                if not inner:
                    continue
                if not any(c0 - 0.5 <= (s0 + s1) / 2 <= c1 + 0.5 for c0, c1 in inner):
                    c0, c1 = max(inner, key=lambda c: (c[1] - c[0], -c[0]))
                    pos = a + u * (c0 + c1) / 2
            score = (0 if prefer == "window" and win else 1, same, -(s1 - s0), i)
            if best is None or score < best[0]:
                best = (score, pos, W)
    return best[1:] if best else None


def _spots(r, rm, E, key, z, kind, why, pts=()) -> list:
    """一条规则（或意图里的一条）的锚点 → [(位置, 转角, 房间 id)]；wall 锚点由 _add 一个一个找。值写错抛 ValueError。
    家具旁、背后、台面上的点位压窗时顺着墙挪到最近的允许位置（_settle：离原位置不超过 window_slide_mm，不叠到 pts 里本房已放的
    点位上，台面插座还在台面能放的段里、和别的台面插座隔开）；挪不下的、别的原因放不下的（出房间、压墙、在门洞或门扇里、被高家具
    挡住）不要，原因记进 why；单写一侧的换另一侧，家具旁的专用插座两侧都放不下时退到这件家具背后正中（_behind）。房门开关放不下
    （含压窗）时放到门外那间房（同一户）锁舌一侧的墙上，再不行照放，由校验报出来。"""
    a = next(k for k in ANCHORS if k in r)
    v = r[a]
    ok = lambda pos, rot, room=rm: _fits(E, room, key, pos, rot, z, kind)
    taken = [{"key": _symbol(q["type"]), "x": q["x"], "y": q["y"], "z": q["z"], "rot": q["rot"]} for q in pts if q["room"] == rm["id"]]

    def settle(c, rot, keep=None):   # 放下了的记进 taken：后面挪的点位不叠上去
        c, b = _settle(E, rm, key, c, rot, z, kind, taken, keep)
        if not b:
            taken.append({"key": key, "x": float(c[0]), "y": float(c[1]), "z": z, "rot": rot})
        return c, b
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
            good, bad = [], []
            for c in cand:   # 单写一侧的：这一侧（挪过也算）放下了就不试另一侧
                c, b = settle(c, it["rot"])
                (bad if b else good).append(b or c)
                if good and side != "both":
                    break
            if not good and kind == "dedicated":   # 专用插座两侧都放不下：退到它服务的这件家具背后正中（一件电器一个就够）
                c, b = _behind(E, rm, key, it, z, kind, taken)
                if b:
                    bad = [f"{bad[0]}；{it['name']}背后正中也放不下：{b}"]
                else:
                    taken.append({"key": key, "x": float(c[0]), "y": float(c[1]), "z": z, "rot": it["rot"]})
                    good, bad = [c], []
            why += bad if side == "both" else ([] if good else bad[:1])
            out += [(c, it["rot"], rm["id"]) for c in good]
        return out
    if a == "behind":
        for it, part in _furn(rm, v):
            p, u, _n = furnish.frame(it)
            c, b = settle(p + u * (_part_x(it, part) + _num(r, "dx")), it["rot"])
            out += [(c, it["rot"], rm["id"])] if not b else []
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
            xs = lambda c, p=p, u=u: float((np.asarray(c) - p) @ u)   # 台面上的位置（家具局部 x）
            placed = [p + u * (a_ + (b - a_) * (j + 0.5) / k) for (a_, b), k in zip(free, k_of) for j in range(k)]
            for i in range(len(placed)):   # 压窗的挪到最近的允许位置：还在能放的段里、和这条台面上别的插座隔 step
                others = [q for q in placed[:i] + placed[i + 1:] if q is not None]
                keep = lambda q, others=others: (any(s0 - 0.5 <= xs(q) <= s1 + 0.5 for s0, s1 in free)
                                                 and all(abs(xs(q) - xs(o)) >= step - 0.5 for o in others))
                c, bad = settle(placed[i], it["rot"], keep)
                placed[i] = None if bad else c
                out += [(c, it["rot"], rm["id"])] if not bad else []
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
            raise ValueError(f"ceiling 是 center、{'、'.join(CEIL_ANCHORS)} 之一")
        return [(np.array(rm["label"], dtype=float), 0.0, rm["id"])]
    raise ValueError(f"{a} 锚点要单独找")


def _cr(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _runs(line) -> list:
    """线 → 直段 [(起点, 终点)]，在拐角处断开；闭合的环首尾共线的两段并成一段。"""
    out = []
    line = linemerge(line) if line.geom_type == "MultiLineString" else line
    for g in getattr(line, "geoms", [line]):
        if g.geom_type != "LineString" or g.length == 0:
            continue
        cs = [np.array(c, dtype=float) for c in g.coords]
        runs, run0 = [], cs[0]
        for i in range(1, len(cs) - 1):
            u1, u2 = cs[i] - cs[i - 1], cs[i + 1] - cs[i]
            n1, n2 = np.linalg.norm(u1), np.linalg.norm(u2)
            if n1 > 1e-6 and n2 > 1e-6 and abs(_cr(u1 / n1, u2 / n2)) > 0.1:
                runs.append((run0, cs[i]))
                run0 = cs[i]
        runs.append((run0, cs[-1]))
        if g.is_closed and len(runs) > 1:
            (a0, b0), (a1, b1) = runs[0], runs[-1]
            u0, u1 = (b0 - a0) / np.linalg.norm(b0 - a0), (b1 - a1) / np.linalg.norm(b1 - a1)
            if abs(_cr(u0, u1)) < 0.1 and float(u0 @ u1) > 0:
                runs = [(a1, b0)] + runs[1:-1]
        out += [(a, b) for a, b in runs if np.linalg.norm(b - a) > 1]
    return out


def _ceiling_spots(rm, E, v, pts) -> list:
    """跟吊顶走的灯（规则表 checks.ceiling_lights 的门槛）→ [(位置, 灯带路径或 None)]。没有对应的吊顶块返回空。
    ring：边吊、回字形外圈的带子中线（离墙半个带宽）分成直段（不整个落在吊顶块里、离墙不到 wall_min、离窗帘盒不到
    curtain_clear 的部分先去掉），两段平行、相距不到 spacing_min 的地方（窄过道里的两侧）只留它们正中的一段（_one_row）；
    长的段先放，每段离两头至少 end_min（拐角处两排各让到拐角两盏中心距不小于 spacing_min，_end_clear），再避开离本房已有的
    灯不够远的地方（主灯 light_clear，筒灯 spacing_min），间距在 spacing_min～spacing_max 之间均布、取整到 spacing_round、
    余数分给两头（_even）；短于 min_run 的段不放，放不下两盏的段只放一盏在正中。axis：石膏板平顶（整块）沿房间长向中线，同样均布。slot：回字形灯槽中线一条灯带（每段连着的一条），
    闭合的从离房门最近的拐点起。"""
    C = LIMITS["ceiling_lights"]
    blocks = E["ceil"].get(rm["id"], [])
    poly = lambda b: Polygon(b["polygon"], b.get("holes") or [])
    R = Polygon([w["a"] for w in rm["walls"]]).buffer(0)
    curtain = unary_union([poly(b) for b in blocks if b["part"] == "窗帘盒"])
    if v == "slot":
        out = []
        for b in [b for b in blocks if b["part"] == "灯槽"][:1]:
            band = unary_union([poly(x) for x in blocks if x["part"] == "灯槽"])
            mid = R.buffer(-(b["width"] - b["slot"] / 2), join_style="mitre").boundary.intersection(band.buffer(0.5))
            o = _main_door(rm, E["by"])
            door = openings.rec_frame(o)[0] if o is not None else np.array(R.centroid.coords[0])
            mid = linemerge(mid) if mid.geom_type == "MultiLineString" else mid
            for g in getattr(mid, "geoms", [mid]):
                if g.geom_type != "LineString" or g.length < C["min_run"]:
                    continue
                cs = [(round(x, 1), round(y, 1)) for x, y in g.coords]
                if g.is_closed:   # 环：从离房门最近的拐点起
                    k = min(range(len(cs) - 1), key=lambda i: (math.dist(cs[i], door), i))
                    cs = cs[k:-1] + cs[:k + 1]
                out.append((np.array(cs[0]), [list(c) for c in cs]))
        return out
    if v == "ring":
        mine = [b for b in blocks if b["part"] in ("边吊", "外圈")]
        if not mine:
            return []
        band = mine[0]["width"] - mine[0].get("slot", 0)
        line = R.buffer(-band / 2, join_style="mitre").boundary
    else:   # axis：石膏板平顶
        mine = [b for b in blocks if b["part"] == "整块" and b["material"] == "gypsum"]
        if not mine:
            return []
        rect = R.minimum_rotated_rectangle
        q = [np.array(c) for c in rect.exterior.coords[:3]]
        e = max((q[1] - q[0], q[2] - q[1]), key=lambda d: float(np.linalg.norm(d)))
        cen = np.array(rect.centroid.coords[0])
        line = LineString([cen - e, cen + e])
    area = unary_union([poly(b) for b in mine])
    ok = area.buffer(-LIMITS["ceiling_lights"]["radius"]).intersection(R.buffer(-C["wall_min"])).difference(curtain.buffer(C["curtain_clear"]))
    runs = _one_row(_runs(line.intersection(ok)), C["spacing_min"], ok, R)
    # 离不开的灯：本房已有的灯（主灯、家具上方的灯离 light_clear 以上；已有的筒灯离 spacing_min 以上），每排放好后也算进去
    keep = [(np.array([p["x"], p["y"]]), C["spacing_min"] if p["type"] in GROUPED else C["light_clear"])
            for p in pts if p["kind"] == "light" and p["room"] == rm["id"] and p["type"] != "strip"]
    out, placed = [], []
    for a, b in sorted(runs, key=lambda r: (-round(float(np.linalg.norm(r[1] - r[0])), 1), round(float(r[0][0]), 1), round(float(r[0][1]), 1))):
        L = float(np.linalg.norm(b - a))
        if L < C["min_run"]:
            continue
        u = (b - a) / L
        e0, e1 = _end_clear(a, u, runs, C), _end_clear(b, -u, runs, C)
        # 这一段上能放灯的地方：两头各让 e0、e1，再去掉离已有的灯不够远的
        cuts = []
        for q, r_ in keep:
            t, d = float((q - a) @ u), abs(_cr(u, q - a))
            if d < r_:
                h = math.sqrt(r_ * r_ - d * d)
                cuts.append((t - h, t + h))
        ok_at = lambda t: all(not (c0 < t < c1) for c0, c1 in cuts)
        if L - e0 - e1 < C["spacing_min"]:   # 放不下两盏：只放一盏，在这一段正中
            ts = [L / 2] if ok_at(L / 2) else []
        else:
            ts = []
            for s0, s1 in _subtract((e0, L - e1), cuts):
                ts += _even(s0, s1, C)
        if len(ts) >= 2:
            ts = _symmetric(ts, a, u, R, e0, L - e1, ok_at, C)
        first = len(out)
        for t in ts:
            c = a + u * t
            out.append((c, None))
            keep.append((c, C["spacing_min"]))
        placed.append((a, u, L, e0, e1, list(range(first, len(out)))))
    _align_opposite(out, placed, keep, C)
    return out


def _wall_dist(R, c, d) -> float:
    """c 沿 d 到房间边的距离（近的那个交点）；没有返回 inf。"""
    hit = LineString([tuple(c), tuple(c + d * 1e5)]).intersection(R.exterior)
    pts = [np.array(q) for g in getattr(hit, "geoms", [hit]) for q in getattr(g, "coords", [])]
    ds = [float(np.linalg.norm(q - c)) for q in pts if np.linalg.norm(q - c) > 1e-3]
    return min(ds) if ds else math.inf


def _symmetric(ts, a, u, R, lo, hi, ok_at, C) -> list:
    """一排灯两头到墙的距离只差一点（不到 end_sym_max）时取对称：近墙那头往里收到和另一头一样远，再按取整的间距重排，余数分给
    两头（只往里收，不会更靠近窗帘盒、拐角）；排不下（间距小于 spacing_min、落到不能放灯的地方）就保持原样。"""
    d0, d1 = _wall_dist(R, a + u * ts[0], -u), _wall_dist(R, a + u * ts[-1], u)
    if not 0.5 < abs(d0 - d1) < C["end_sym_max"]:
        return ts
    e = max(d0, d1)
    t0, t1 = ts[0] + (e - d0), ts[-1] - (e - d1)
    k = len(ts) - 1
    step = C["spacing_round"]
    s = math.floor((t1 - t0) / k / step + 1e-9) * step
    if s < C["spacing_min"]:
        return ts
    t0 += ((t1 - t0) - s * k) / 2
    new = [t0 + s * i for i in range(k + 1)]
    return new if all(lo - 1e-6 <= t <= hi + 1e-6 and ok_at(t) for t in new) else ts


def _align_opposite(out, placed, keep, C) -> None:
    """同一圈里相对的两排（平行、隔着房间、沿线大半重叠）可布灯的长度相差不到 align_within 个间距时，两排灯位对齐：长的那排改用
    短的那排的排法（投到长的那排上，两头多出来的留空）；改了以后仍要在这一排能放灯的范围里、离别的灯不小于 spacing_min，
    不行就各排各的。就地改 out。"""
    for i, (a, u, L, e0, e1, idx) in enumerate(placed):
        for a2, u2, L2, e02, e12, idx2 in placed[i + 1:]:
            if len(idx) < 2 or len(idx) != len(idx2) or abs(_cr(u, u2)) > 0.01 or abs(_cr(u, a2 - a)) < C["spacing_min"]:
                continue
            ta = sorted(float((out[k][0] - a) @ u) for k in idx)
            tb = sorted(float((out[k][0] - a) @ u) for k in idx2)   # 第二排投到第一排的轴上
            sa, sb = ta[-1] - ta[0], tb[-1] - tb[0]
            step = min(sa, sb) / (len(idx) - 1)
            if max(abs(x - y) for x, y in zip(ta, tb)) < 0.5 or abs(sa - sb) >= C["align_within"] * step:
                continue
            short_t, long_set = (ta, (a2, u2, L2, e02, e12, idx2)) if sa <= sb else (tb, (a, u, L, e0, e1, idx))
            la, lu, lL, le0, le1, lidx = long_set
            base = a + u * 0   # 第一排的轴
            mine = {k for k in lidx}
            others = [q for q, r_ in keep if not any(np.allclose(q, out[k][0]) for k in mine)]
            new = []
            for t in short_t:
                p = base + u * t
                tl = float((p - la) @ lu)   # 投到长的那排上
                c = la + lu * tl
                if not (le0 - 1e-6 <= tl <= lL - le1 + 1e-6) or any(np.linalg.norm(c - q) < C["spacing_min"] - 1e-6 for q in others):
                    new = None
                    break
                new.append(c)
            if new:
                order = sorted(lidx, key=lambda k: float((out[k][0] - a) @ u))
                for k, c in zip(order, new):
                    out[k] = (c, None)


def _even(s0, s1, C) -> list:
    """s0～s1（沿一排的位置）里均布筒灯：间距不超过 spacing_max、不小于 spacing_min，取整到 spacing_round，余数分给两头；
    放不下两盏的放一盏在正中。返回各盏的位置。"""
    use = s1 - s0
    if use < C["spacing_min"]:
        return [(s0 + s1) / 2] if use >= 0 else []
    k = max(1, math.ceil(use / C["spacing_max"] - 1e-9))   # 间隔数
    if use / k < C["spacing_min"]:
        k = max(1, k - 1)
    step = C["spacing_round"]
    s = math.floor(min(C["spacing_max"], use / k) / step + 1e-9) * step
    t0 = s0 + (use - s * k) / 2
    return [t0 + s * i for i in range(k + 1)]


def _end_clear(p, u, runs, C) -> float:
    """一排的一头 p（u 指向这一排里面）离端点至少多远：拐角处（另一排的端点也在 p）两排各让到两盏拐角灯的中心距不小于
    spacing_min（两条射线夹角 φ：让 spacing_min / (2 sin(φ/2))），别处是 end_min。"""
    e = C["end_min"]
    for a, b in runs:
        for q, v in ((a, b - a), (b, a - b)):
            n = float(np.linalg.norm(v))
            if n < 1 or np.linalg.norm(q - p) > 1:
                continue
            v = v / n
            if abs(_cr(u, v)) < 1e-6 and float(u @ v) > 0:   # 就是这一排自己
                continue
            half = math.acos(max(-1.0, min(1.0, float(u @ v)))) / 2
            if half > 1e-6:
                e = max(e, C["spacing_min"] / (2 * math.sin(half)))
    return e


def _one_row(runs, gap, ok, room) -> list:
    """窄的地方只放一排：两段平行的直段相距不到 gap（spacing_min，如 1.2m 宽的过道里外圈的两侧）时，重叠的那一截两段都去掉，
    换成它们正中的一段（还要在能放灯的范围 ok 里）。"""
    runs = [(np.array(a, dtype=float), np.array(b, dtype=float)) for a, b in runs]
    mids = []
    for _ in range(len(runs) * len(runs) + 1):
        hit = None
        for i, (a, b) in enumerate(runs):
            Li = float(np.linalg.norm(b - a))
            u = (b - a) / Li
            for j in range(i + 1, len(runs)):
                c, d = runs[j]
                Lj = float(np.linalg.norm(d - c))
                if Lj < 1 or abs(_cr(u, (d - c) / Lj)) > 0.01:
                    continue
                off = _cr(u, c - a)
                if not 1 < abs(off) < gap:
                    continue
                t0, t1 = sorted((float((c - a) @ u), float((d - a) @ u)))
                o0, o1 = max(0.0, t0), min(Li, t1)
                if o1 - o0 <= 1:
                    continue
                nrm = np.array([-u[1], u[0]]) * off / 2
                mid = LineString([a + u * o0 + nrm, a + u * o1 + nrm])
                if LineString([a + u * (o0 + o1) / 2, a + u * (o0 + o1) / 2 + 2 * nrm]).difference(room.buffer(1)).length > 1:
                    continue   # 两段之间隔着墙（不是同一块吊顶里的窄处）
                hit = (i, j, o0, o1, mid if mid.difference(ok.buffer(1)).length <= 1 else None)
                break
            if hit:
                break
        if hit is None:
            return runs + _straighten(mids, ok)
        i, j, o0, o1, mid = hit
        a, b = runs[i]
        c, d = runs[j]
        u = (b - a) / np.linalg.norm(b - a)
        uj = (d - c) / np.linalg.norm(d - c)
        tj = sorted((float((a + u * o0 - c) @ uj), float((a + u * o1 - c) @ uj)))
        cut_i = [(a, a + u * o0), (a + u * o1, b)]
        cut_j = [(c, c + uj * tj[0]), (c + uj * tj[1], d)]
        if mid is not None:   # 两排都去掉重叠的那一截，换成正中一排
            keep = cut_i + cut_j
        else:   # 正中放不了灯（如回字形的灯槽在那里）：只留长的那一排
            keep = [(a, b)] + cut_j if np.linalg.norm(b - a) >= np.linalg.norm(d - c) else cut_i + [(c, d)]
        runs = [r for k, r in enumerate(runs) if k not in (i, j)] + [(p, q) for p, q in keep if np.linalg.norm(q - p) > 1]
        if mid is not None:   # 中线另放：不再和别的段配对
            mids.append((np.array(mid.coords[0]), np.array(mid.coords[-1])))
    return runs + _straighten(mids, ok)


def _straighten(mids, ok) -> list:
    """同一段直过道里的中线取直共线：平行、沿线首尾相接（或重叠）、离得不到 wall_min 的几段中线（过道一侧墙有小台阶时会错开一点）
    并成一段，放在最长那段的线上（仍要在能放灯的范围 ok 里，不在就不并）。"""
    tol = LIMITS["ceiling_lights"]["wall_min"]
    mids = [(np.array(a, dtype=float), np.array(b, dtype=float)) for a, b in mids]
    merged = True
    while merged:
        merged = False
        for i, (a, b) in enumerate(mids):
            L = float(np.linalg.norm(b - a))
            u = (b - a) / L
            for j in range(len(mids)):
                if j == i:
                    continue
                c, d = mids[j]
                Lj = float(np.linalg.norm(d - c))
                if Lj > L or abs(_cr(u, (d - c) / Lj)) > 0.01 or not 0 < abs(_cr(u, c - a)) <= tol:
                    continue
                t = sorted((float((c - a) @ u), float((d - a) @ u)))
                if t[0] > L + 1 or t[1] < -1:   # 沿线不相接
                    continue
                lo, hi = min(0.0, t[0]), max(L, t[1])
                seg = LineString([a + u * lo, a + u * hi])
                if seg.difference(ok.buffer(1)).length > 1:
                    continue
                mids = [m for k, m in enumerate(mids) if k not in (i, j)] + [(a + u * lo, a + u * hi)]
                merged = True
                break
            if merged:
                break
    return mids


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
                  ("ceiling", ("center",) + CEIL_ANCHORS), ("wall", ("free",))):
        if k in r and r[k] not in ok:
            raise ValueError(f"{k} 是 {'、'.join(ok)} 之一")
    if anchors[0] not in ("wall", "door", "ceiling"):
        group(r[anchors[0]])
    if "over" in r:
        group(furnish.text_field(r, "over", "家具组名"))
    elif anchors[0] == "wall" and "clear" in r:
        raise ValueError("clear 和 over 一起写（台面上方离水槽、灶台、台盆的边多远）")
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
                                  "main": anchor == "ceiling" and r[anchor] == "center", "controls": r.get("controls")}
    if anchor == "wall":
        have = sum(q["kind"] == P["kind"] for q in pts)
        n = max(0, need.get(P["kind"], 0) - have) if r.get("fill") else _count(r)
        if ask and r.get("fill") and not n:
            what = "插座" if P["kind"] == "socket" else P["name"]
            raise ValueError(f"{rm['name']}已经有 {have} 个{what}，" + (f"不少于规则表的 {need[P['kind']]} 个，不用补" if P["kind"] in need
                                                                    else f"规则表没给{rm['name']}定{what}最少几个，fill 无从补起"))
        bw = SYMBOLS[key]["body"][0]
        within = _over_spans(rm, r["over"], _num(r, "clear", 100.0), bw) if "over" in r else None
        for k in range(n):
            hit = None if within == {} else _free_spot(E, rm, pts, z, SYMBOLS[key]["body"][2], bw, P["kind"], r.get("prefer", "long"),
                                                       SYMBOLS[key]["ifc_type"] == "IfcOutletType", within)
            if hit is None and within is not None:   # 台面上方放不下：少放，不放到没有台面的墙上
                skip((f"没有这件家具（{r['over']}）" if not _furn(rm, r["over"]) else
                      f"台面上方放不下（离水槽、灶台、台盆的边至少 {_num(r, 'clear', 100.0):.0f}，避开窗和已有的点位）")
                     + f"，还差 {n - k} 个：只放在台面上方，不放到没有台面的墙上")
                return k, n - k
            if hit is None:
                skip(f"找不到空墙（还差 {n - k} 个）")
                return k, n - k
            pts.append(new(hit[0], _deg(hit[1]["u"]), rm["id"]))
        return n, 0
    if anchor == "ceiling" and r["ceiling"] in CEIL_ANCHORS:   # 跟吊顶走的灯：没有吊顶块就一个都不放（不记 skipped）
        got = _ceiling_spots(rm, E, r["ceiling"], pts)
        for c, path in got:
            q = new(c, 0.0, rm["id"])
            if path:
                q["path"], q["length"] = path, round(LineString(path).length)
            pts.append(q)
        return len(got), 0
    why = []
    spots = _spots(r, rm, E, key, z, P["kind"], why, pts)
    for w in why:
        skip(w)
    missing = 0 if not spots else len(why)   # 一件家具都没有时 why 只有一条"没有这件家具"
    for s in spots:
        q = new(*s)
        if q["kind"] == "light" and not q["main"] and t not in GROUPED:
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
        if _group_lights(rm, E, pts, seq):   # 门口没有面板：提醒（筒灯、灯带不挂到床头开关上）
            skipped.append({"room": rm["name"], "room_id": rm["id"], "point": "switch", "anchor": "door:room",
                            "reason": "门口没有开关面板：筒灯、灯带没有开关控制（床头开关只管主灯）"})
        for q in pts:
            it = {"key": "", "name": POINTS[q["type"]]["name"], "x": round(q["x"], 1), "y": round(q["y"], 1), "z": q["z"],
                  "rot": round(q["rot"] % 360.0, 3) % 360.0, "room": q["room"], "plan_id": q["id"]}
            if q["controls"]:
                it["controls"] = q["ctl"] if "ctl" in q else \
                    [x["id"] for x in pts if x["kind"] == "light" and (q["controls"] == "lights" or x["main"])]
            for k in ("next", "path", "length"):
                if k in q:
                    it[k] = q[k]
            it["key"] = _symbol(q["type"], len(it.get("controls", [])))
            items.append(it)
    _circuits(items, {r["id"]: r["unit"] for r in E["rooms"]})
    return items, errors, skipped


def _group_lights(rm, E, pts, seq) -> list:
    """有筒灯、灯带的房间（只在方案有吊顶时）：灯按组控制。主灯和家具上方的灯照旧各一组，筒灯一组、灯带一组；同一组按离
    开关由近到远连成一串（next），开关只接每组的第一盏（ctl）；一串超过一路的上限时沿串拆成几组（_split_chain，后几段从离开关
    近的那头接）。门口开关的联数 = 组数，一个面板最多 checks.ceiling_lights 的 panel_max 联、接的灯不超过一路的上限（_panels），
    多出来的挨着（中心隔 panel_step，往离门远的一侧）再加面板。同一个门口只有一块面板：卧室门口的双控开关
    （管主灯）加上筒灯、灯带各一联，成了多联双控面板（符号按联数：单联、双联、三联双控）；床头开关照旧只管主灯。
    没有筒灯、灯带时什么都不改。"""
    chains = [[q for q in pts if q["type"] == t] for t in GROUPED]
    if not any(chains):
        return []
    C, N = LIMITS["ceiling_lights"], LIMITS["ceiling_lights"]["panel_max"]
    door_sw = [q for q in pts if q["controls"] and q["kind"] == "switch"]
    ref = (door_sw[0]["x"], door_sw[0]["y"]) if door_sw else tuple(rm["label"])
    heads, load = [], {}   # load：每一联接的灯占这一路多少名额
    for left in chains:
        cur, chain = ref, []
        while left:
            nx = min(left, key=lambda q: (math.hypot(q["x"] - cur[0], q["y"] - cur[1]), q["id"]))
            left.remove(nx)
            chain.append(nx)
            cur = (nx["x"], nx["y"])
        for k, part in enumerate(_split_chain(chain)):   # 一串超过一路的上限：沿串连续拆成几组，各占一联
            # 拆出来的后几段从离开关近的那头接（一圈灯的最后一段多半又回到门口），接线不用横穿房间
            if k and math.dist(ref, (part[-1]["x"], part[-1]["y"])) < math.dist(ref, (part[0]["x"], part[0]["y"])) - 1:
                part = part[::-1]
            for a, b in zip(part, part[1:]):
                a["next"] = b["id"]
            heads.append(part[0]["id"])
            load[part[0]["id"]] = sum(_weight(q["type"]) for q in part)
    plain = [q["id"] for q in pts if q["kind"] == "light" and q["type"] not in GROUPED]
    for q in [q for q in pts if q["controls"]]:
        q["ctl"] = [x["id"] for x in pts if x["kind"] == "light" and x["main"]] if q["controls"] == "main" else plain + heads
    base = next((q for q in door_sw if q["controls"] == "lights"), door_sw[0] if door_sw else None)
    if base is None:
        return heads   # 门口没有开关面板（意图删了、放不下）：筒灯、灯带没有开关；床头开关只管主灯，不往它上面挂
    # 同一个门口的各组合在一块面板上（联数 = 组数）：双控开关（管主灯）的面板再加上筒灯、灯带各一联，成了多联双控面板
    gangs = base["ctl"] + ([h for h in heads if h not in base["ctl"]] if base["controls"] == "main" else [])
    w = {q["id"]: _weight(q["type"]) for q in pts}
    base["ctl"], *rest = _panels(gangs, lambda g: load.get(g, w.get(g, 1)), N)
    o = _main_door(rm, E["by"])
    dc = openings.rec_frame(o)[0] if o is not None else np.array(ref)
    u, p0 = np.array([math.cos(math.radians(base["rot"])), math.sin(math.radians(base["rot"]))]), np.array([base["x"], base["y"]])
    s = 1.0 if np.linalg.norm(p0 + u * C["panel_step"] - dc) >= np.linalg.norm(p0 - u * C["panel_step"] - dc) else -1.0
    for k, ctl in enumerate(rest, 1):
        room = E["by"].get(base["room"], rm)
        key = _symbol("switch", len(ctl))
        cand = [p0 + u * s * k * C["panel_step"], p0 - u * s * k * C["panel_step"]]
        pos = next((c for c in cand if not _fits(E, room, key, c, base["rot"], base["z"], "switch")), cand[0])
        seq["switch"] = seq.get("switch", 0) + 1
        pts.append({"type": "switch", "kind": "switch", "x": float(pos[0]), "y": float(pos[1]), "z": base["z"], "rot": base["rot"],
                    "room": base["room"], "main": False, "controls": "lights", "ctl": ctl, "id": f"switch-{rm['id']}-{seq['switch']}"})


def _weight(t) -> int:
    """这类点位在它那一路占几个名额（规则表 circuits 的 weight，缺省 1；不进回路的 0）。"""
    c = CIRCUITS.get(POINTS.get(t, {}).get("circuit"), None)
    return c.get("weight", {}).get(t, 1) if c is not None else 0


def _split_chain(chain) -> list:
    """一串灯（同一联开关接的）占的名额超过这一路的上限（规则表 circuits 的 max）：沿串连续拆成几组，组数最少、各组灯数尽量均匀
    （前面的组多一盏）；不超过就原样一组。"""
    if not chain:
        return []
    cap = CIRCUITS.get(POINTS[chain[0]["type"]].get("circuit"), {}).get("max")
    total = sum(_weight(q["type"]) for q in chain)
    if not cap or total <= cap:
        return [chain]
    n = len(chain)
    for m in range(-(-total // cap), n + 1):
        sizes = [n // m + (i < n % m) for i in range(m)]
        out = [chain[sum(sizes[:i]):sum(sizes[:i + 1])] for i in range(m)]
        if all(sum(_weight(q["type"]) for q in p) <= cap for p in out):
            return out
    return [[q] for q in chain]


def _panels(gangs, load, n) -> list:
    """门口的各联排到面板上：一块面板最多 n 联；一块面板接的灯合起来超过一路的上限时，这一联换到下一块（一块面板只进一路，
    回路表接得出来）。都放得下时就是按顺序每 n 联一块。"""
    cap = CIRCUITS.get(POINTS[GROUPED[0]].get("circuit"), {}).get("max")
    out = []
    for g in gangs:
        p = next((p for p in out if len(p) < n and (not cap or sum(map(load, p)) + load(g) <= cap)), None)
        if p is None:
            out.append(p := [])
        p.append(g)
    return out or [[]]


def _control_groups(mine, by) -> tuple[list, dict]:
    """一户一个回路前缀里的点位 → (控制组, 开关每一联的灯)。控制组 = 一联开关加它接的整串灯（next 连着）；双控的两只开关接同一串，
    算一组。没有开关接的灯、手写的点位各自一组。返回的组按 mine 里第一次出现的顺序。"""
    ids = {it["plan_id"] for it in mine}
    root = {i: i for i in ids}

    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i
    gang = {}
    for it in mine:
        if not it.get("controls"):
            continue
        gang[it["plan_id"]] = []
        for h in it["controls"]:
            chain, cur = [], h
            while cur in ids and cur not in chain:
                chain.append(cur)
                cur = by[cur].get("next")
            gang[it["plan_id"]].append(chain)
            for a in chain[1:]:
                root[find(a)] = find(chain[0])
    groups = {}
    for it in mine:
        if it["plan_id"] not in gang:
            groups.setdefault(find(it["plan_id"]), []).append(it)
    return list(groups.values()), gang


def _circuits(items, unit_of) -> None:
    """按户、按回路前缀（规则表 circuits 的顺序）依次编号；有 max 的满了换下一路（weight：柜机占 2 个名额，挂机 1 个）。
    装的单位是控制组（一联开关和它接的整串灯，不拆）；同一块面板（和与它双控的开关）接的各组能一起装下时一起装，装不下才按组分开。
    一个前缀里先排名额多的（柜机单独一路，挂机两台一路），名额相同的按方案里的顺序。开关的回路 = 它那一联接的灯的回路；
    一块面板各联不在同一路时（很少：面板加上和它双控的开关超过一路的上限）另记 gang_circuits（按联）。"""
    by = {it["plan_id"]: it for it in items}
    for unit in dict.fromkeys(unit_of[it["room"]] for it in items):
        for pre, c in CIRCUITS.items():
            weight = lambda it: c.get("weight", {}).get(point_type(it), 1)
            mine = sorted((it for it in items if unit_of[it["room"]] == unit and POINTS[point_type(it)].get("circuit") == pre),
                          key=lambda it: -weight(it))
            groups, gang = _control_groups(mine, by)
            gid = {it["plan_id"]: n for n, g in enumerate(groups) for it in g}
            link = {n: n for n in range(len(groups))}   # 面板（开关）把它接的各组连成一束

            def top(n):
                while link[n] != n:
                    n = link[n]
                return n
            for chains in gang.values():
                ns = sorted({top(gid[ch[0]]) for ch in chains if ch})
                for n in ns[1:]:
                    link[n] = ns[0]
            bundles = {}
            for n in range(len(groups)):
                bundles.setdefault(top(n), []).append(groups[n])
            used = [0]   # 每一路已占的名额；装进第一路放得下的（都只占 1 个名额时就是按顺序装满一路换下一路）
            for b in sorted(bundles.values(), key=lambda b: -max(weight(it) for g in b for it in g)):
                w = sum(weight(it) for g in b for it in g)
                for part in ([b] if not c.get("max") or w <= c["max"] else [[g] for g in b]):
                    pw = sum(weight(it) for g in part for it in g)
                    k = next((k for k, u in enumerate(used) if not c.get("max") or not u or u + pw <= c["max"]), None)
                    if k is None:
                        used.append(0)
                        k = len(used) - 1
                    used[k] += pw
                    for g in part:
                        for it in g:
                            it["circuit"] = f"{pre}{k + 1}"
            k = len(used)
            for it in mine:   # 开关：每一联跟它接的灯走
                if it["plan_id"] in gang:
                    gc = [by[ch[0]]["circuit"] for ch in gang[it["plan_id"]] if ch] or [f"{pre}{k}"]
                    it["circuit"] = gc[0]
                    if len(set(gc)) > 1:
                        it["gang_circuits"] = gc



# ---------------------------------------------------------------- 校验
def validate(items: list, rec: dict, sch: dict | None = None, units=None) -> list[dict]:
    """点位（键在符号库里、不是家具的 items）的校验 → 违规列表 [{room, room_id, item, check, value, unit, with, msg}]。check：
    in_door 落在门洞里或门扇开启范围里（门后；开关的门后归 switch_reach）、in_window 墙上装的点位压窗（盒子的高度和窗台～窗顶
    重叠、沿墙离洞边不到 window_gap_mm，value 是盒子边离洞边多远、负数是压进洞口宽度）、blocked 被高家具挡住（点位离家具某一块不到 block_gap、
    高度又和这一块重叠；灯、给排水点不查；专用插座在它服务的电器背后时不算那件电器挡着，_own）、outside_room 出房间、
    in_wall 压墙、overlap 和同一间房里另一个点位叠在一起（平面外框重叠、高度也重叠）、socket_count 插座少于规则表的 min、switch_reach 房门开关离锁舌边超过 switch_reach 或在门扇开启范围里、
    near_shower 插座离淋浴、浴缸不到 shower_clear、circuit_over 一路超过上限、circuit_gang 一联开关接的灯不在同一路或开关这一联
    和灯不同路。units：只查这几户的点位、插座数和回路。"""
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
        if (hw := _in_window(rm, it["key"], (it["x"], it["y"]), it["z"])):
            o, g = hw
            add(rm, name, "in_window", g, "mm", f"落在窗 {o['id']} 的洞口宽度里（盒子压进 {-g:.0f}）" if g < 0 else
                f"离窗 {o['id']} 的洞边只有 {g:.0f}，不到 {LIMITS['window_gap_mm']}", o["id"])
        if kind not in NO_BLOCK and (hit := _blocked(rm, p, it["z"], SYMBOLS[it["key"]]["body"][2], _own(rm, point_type(it), (it["x"], it["y"])))):
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
    C, tol_c = LIMITS["ceiling_lights"], 0.5   # 0.5mm 吸收 0.1mm 坐标落盘舍入与斜向房间浮点误差
    for it in pts:   # 筒灯、灯带（只在有吊顶的方案里有）
        t, rm = point_type(it), by.get(it["room"])
        if t not in GROUPED or rm is None:
            continue
        name, bl, p = it["plan_id"], E["ceil"].get(it["room"], []), Point(it["x"], it["y"])
        g = lambda parts: unary_union([Polygon(b["polygon"], b.get("holes") or []) for b in bl if b["part"] in parts])
        if t == "strip":
            d = LineString(it.get("path") or [(it["x"], it["y"])] * 2).difference(g(("灯槽",)).buffer(1)).length
            if d > 1:
                add(rm, name, "ceiling_light", d, "mm", f"灯带有 {d:.0f} 不在灯槽里")
            continue
        cur, R = g(("窗帘盒",)), Polygon([w["a"] for w in rm["walls"]]).buffer(0)
        others = [o for o in pts if o["room"] == it["room"] and kind_of(o) == "light" and point_type(o) not in GROUPED]
        if not any(Polygon(b["polygon"], b.get("holes") or []).buffer(-(C["radius"] - tol_c)).contains(p) for b in bl if b["part"] != "窗帘盒"):
            add(rm, name, "ceiling_light", 0, "mm", "不在一块吊顶里，或压到跌级线（灯边离吊顶块的边不到半个灯宽）")
        elif (d := R.exterior.distance(p)) < C["wall_min"] - tol_c:
            add(rm, name, "ceiling_light", d, "mm", f"离墙 {d:.0f}，不到 {C['wall_min']}")
        elif not cur.is_empty and (d := cur.distance(p)) < C["curtain_clear"] - tol_c:
            add(rm, name, "ceiling_light", d, "mm", f"离窗帘盒 {d:.0f}，不到 {C['curtain_clear']}")
        elif (o := next((o for o in others if Point(o["x"], o["y"]).distance(p) < C["light_clear"] - tol_c), None)):
            d = Point(o["x"], o["y"]).distance(p)
            add(rm, name, "ceiling_light", d, "mm", f"离{o['name']} {d:.0f}，不到 {C['light_clear']}", o.get("plan_id") or o["name"])
    downs = [it for it in pts if point_type(it) == "downlight" and it["room"] in by]
    for i, it in enumerate(downs):   # 两盏筒灯中心距不到 spacing_min
        for o in downs[:i]:
            if o["room"] == it["room"] and (d := math.hypot(it["x"] - o["x"], it["y"] - o["y"])) < C["spacing_min"] - tol_c:
                add(by[it["room"]], it["plan_id"], "ceiling_light", d, "mm", f"离{o['plan_id']} 中心距 {d:.0f}，不到 {C['spacing_min']}", o["plan_id"])
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
    pid = {it["plan_id"]: it for it in pts if it.get("plan_id")}
    for it in pts:   # 一联开关只能断一路：这一联接的整串灯同一路，开关这一联也在这一路
        if not it.get("controls") or not it.get("circuit") or it.get("plan_id") is None:
            continue
        gcs = it.get("gang_circuits") or [it["circuit"]] * len(it["controls"])
        for h, gc in zip(it["controls"], gcs):
            chain, cur = [], h
            while cur in pid and cur not in chain:
                chain.append(cur)
                cur = pid[cur].get("next")
            cs = sorted({pid[x].get("circuit") or "" for x in chain})
            if chain and cs != [gc]:
                add(by.get(it["room"]), it["plan_id"], "circuit_gang", len(chain), "个",
                    f"这一联（{h} 起的 {len(chain)} 盏）在 {'、'.join(cs)}，开关这一联在 {gc}：一联只能断一路", h)
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
    if sch.get("ceilings"):   # 有吊顶时灯的高度跟吊顶走（ceiling.adjust_lights，只改高度、不改位置）
        from . import ceiling
        out["items"] = ceiling.adjust_lights(out)
    viol = validate(out["items"], rec, out, units)
    circuits = {}
    for it in items:
        if it.get("circuit"):
            c = circuits.setdefault(unit[it["room"]], {})
            c[it["circuit"]] = c.get(it["circuit"], 0) + 1
    counts = {c: n for c in CHECKS if (n := sum(v["check"] == c for v in viol))}
    return out, {"ok": not errors and not viol, "points": len(items), "errors": errors, "violations": viol, "counts": counts, "circuits": circuits,
                 "skipped": skipped}
