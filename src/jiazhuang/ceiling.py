"""吊顶：按房间选模板（原顶、平顶、边吊、回字形）和参数，确定性生成吊顶块（只依赖 shapely，在 .venv 里能跑）。

规则表 packs/cn/ceiling/rules.json：房间类型 → 缺省模板和参数（惯例，每条带来源），模板的参数和适用的原顶净高下限，净高门槛。
意图（可选）按房间改模板和参数，不写坐标：
  {"rooms": [{"room": 房名或房间 id, "ceiling": 模板名, 参数…}]}
  参数：width（边宽、外圈宽）、drop（下吊）、slot（灯槽宽）、bottom（平顶底标高）、material（材料键）、
  curtain（窗帘盒：true 有窗的墙都做、false 不做、"W2" 或 ["W2", "W3"] 指定墙边）、curtain_width（窗帘盒宽）
房间的原顶净高写在方案 rooms[].height（毫米），不写用规则表 checks.height。
输出：方案的 ceilings = [{id, room, template, part, material, bottom, polygon, holes, width?, slot?, curtain?}]（毫米，
底标高离本层楼面），每块建成一个 IfcCovering；窗帘盒是 part="窗帘盒" 的一块（贴原顶）。方案里有 ceilings 时，灯的高度跟吊顶走
（adjust_lights：灯体顶贴所在那块吊顶的底，原顶处贴原顶）。"""
from __future__ import annotations

import json
import os

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from . import conventions, furnish, markcode

PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "ceiling"))
with open(os.path.join(PACK, "rules.json"), encoding="utf-8") as _fh:
    _R = json.load(_fh)
SOURCES, MATERIALS, TEMPLATES, ROOMS, CHECKS = (_R[k] for k in ("sources", "materials", "templates", "rooms", "checks"))
_OPC = conventions.get("openings")
with open(conventions.pack_file("symbols.json"), encoding="utf-8") as _fh:
    _SYM = json.load(_fh)["types"]
LIGHTS = {k: s["body"][2] for k, s in _SYM.items() if s.get("ifc_type") == "IfcLightFixtureType"}   # 灯的键 → 灯体高
KEYS = ("room", "ceiling", "width", "drop", "slot", "bottom", "material", "curtain", "curtain_width")
NUMS = ("width", "drop", "slot", "bottom", "curtain_width")


class IntentError(ValueError):
    pass


def check_intent(intent) -> None:
    """整份意图的结构：对象，rooms 是对象列表。不对抛 IntentError（一行原因）。"""
    if not isinstance(intent, dict):
        raise IntentError("吊顶意图要是 JSON 对象")
    if not isinstance(intent.get("rooms"), list) or not all(isinstance(b, dict) for b in intent["rooms"]):
        raise IntentError("rooms 要是对象列表：[{\"room\": …, \"ceiling\": …}]")


def room_height(sch: dict, rid: str) -> float:
    """原顶净高（毫米）：方案 rooms[].height，没有用规则表 checks.height。"""
    r = next((r for r in sch.get("rooms", []) if r["id"] == rid), {})
    return float(r.get("height") or CHECKS["height"])


def _heads(rm, ops) -> float:
    """房间里门、窗的上沿最高多少（毫米，离楼面）：门窗高按原图编号（对得上洞口宽时），否则用约定包缺省；窗台高用识别结果的，
    没有用缺省；窗高不知道的高窗按窗台高算（不按缺省窗高往上加，免得上沿算到 3m）。"""
    top = 0.0
    for o in ops:
        if rm["id"] not in (o.get("rooms") or []):
            continue
        lab = o.get("label") or ""
        h = markcode.parse(lab)["h"] if lab and markcode.fits(lab, float(o["width"])) else None
        if o["kind"] == "door":
            top = max(top, h or _OPC["default_height_mm"]["door"])
        else:   # 窗高不知道时上沿按缺省窗（缺省窗台 + 缺省窗高）算，窗台比这还高的（高窗）按窗台算
            k = "bay" if o.get("polygon") else "window"
            d = _OPC["default_sill_mm"][k] + _OPC["default_height_mm"][k]
            sill = o["sill"] if o.get("sill") is not None else _OPC["default_sill_mm"][k]
            top = max(top, sill + h if h else max(d, sill))
    return float(top)


def _window_walls(rm, ops) -> list[str]:
    return [w["id"] for w in rm["walls"] if any(o["kind"] != "door" and furnish.on_wall(w, o) for o in ops)]


def _coords(g):
    return [[round(x, 1), round(y, 1)] for x, y in g.exterior.coords[:-1]], [[[round(x, 1), round(y, 1)] for x, y in r.coords[:-1]] for r in g.interiors]


def _parts(g):
    return [p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon) and p.area > 1e4]


def _center(R, w):
    """边吊、回字形中间那块：房间轮廓往里收 w，再去掉窄于 checks.center_min_width 的细长部分（并进外圈一起吊平）。"""
    c = R.buffer(-w, join_style="mitre")
    t = CHECKS["center_min_width"] / 2
    return c.buffer(-t, join_style="mitre").buffer(t, join_style="mitre").intersection(c) if not c.is_empty else c


def _shape(t, p, R, rm, cw):
    """模板 → [(part, 几何, 底标高偏移…)]：底标高在 build 里按 H 算。cw：窗帘盒墙边 id 列表。中间那块的细长部分
    （_center 去掉的）并进外圈：边吊直接吊平；回字形的灯槽改成从剩下的中间往外扩 slot，外圈到灯槽为止。"""
    strips = []
    for w in rm["walls"]:
        if w["id"] in cw:
            a, b, n = w["a"], w["b"], w["n"]
            c = p["curtain_width"]
            strips.append(Polygon([a, b, (b[0] + n[0] * c, b[1] + n[1] * c), (a[0] + n[0] * c, a[1] + n[1] * c)]).intersection(R))
    curtain = unary_union(strips) if strips else Polygon()
    out = []
    if t == "平顶":
        out.append(("整块", R.difference(curtain), "flat"))
    elif t == "边吊":
        out.append(("边吊", R.difference(_center(R, p["width"])).difference(curtain), "drop"))
    elif t == "回字形":
        a = p["width"] - p["slot"]
        raw, c = R.buffer(-p["width"], join_style="mitre"), _center(R, p["width"])
        mid = R.buffer(-a, join_style="mitre") if raw.difference(c).area < 1.0 else c.buffer(p["slot"], join_style="mitre").intersection(R)
        out.append(("外圈", R.difference(mid).difference(curtain), "drop"))
        out.append(("灯槽", mid.difference(c).difference(curtain), "slot"))
    if not curtain.is_empty:
        out.append(("窗帘盒", curtain, "curtain"))
    return out


def _bottom(kind, p, H):
    if kind == "flat":
        return float(p["bottom"]) if p.get("bottom") else H - p["drop"]
    if kind == "drop":
        return H - p["drop"]
    if kind == "slot":
        return H - p["drop"] + p["slot_rise"]
    return H   # 窗帘盒贴原顶


def _lowest(t, p, H):
    if t == "原顶":
        return H
    if t == "平顶":
        return _bottom("flat", p, H)
    return H - p["drop"]


def plan_room(rm, spec, H, ops, from_intent, named=None):
    """一间房：模板 + 参数 → (块, 说明, 违规)。说明是退成别的模板的原因。规则表给的缺省做不了时退一级（只记说明）；
    named（意图点名的模板）做不了时照样退，但记一条违规 template（写明原因）。意图来的房间吊顶底低于净高门槛、门窗上沿不退，
    记违规；任何吊顶块的底不低于原顶（下吊 0、底标高写到原顶以上）记违规 above_slab。"""
    t = spec["ceiling"]
    notes, viol = [], []
    R = Polygon([w["a"] for w in rm["walls"]]).buffer(0)
    need = CHECKS["min_net"].get(rm["type"], CHECKS["min_net"]["default"])
    head = _heads(rm, ops)
    chain, named_v = [t], None
    while True:
        tpl = TEMPLATES[t]
        p = dict(tpl.get("params", {}), **{k: v for k, v in spec.items() if k in tpl.get("params", {})})
        why = None
        if H < tpl.get("min_height", 0):
            why = f"原顶净高 {H:.0f} 低于{t}的下限 {tpl['min_height']}"
        elif t in ("边吊", "回字形") and _center(R, p["width"]).area < CHECKS["min_center_m2"] * 1e6:
            why = f"房间太小，{t}中间剩不到 {CHECKS['min_center_m2']}㎡"
        elif t == "回字形" and p["width"] - p["slot"] < CHECKS["slot_margin"]:
            why = f"外圈宽 {p['width']} 放不下灯槽 {p['slot']}（灯槽外至少 {CHECKS['slot_margin']}）"
        elif not from_intent and t != "原顶" and _lowest(t, p, H) < max(need, head):
            why = f"{t}底标高 {_lowest(t, p, H):.0f} 低于净高门槛 {need} 或门窗上沿 {head:.0f}"
        if why is None:
            break
        nxt = tpl.get("fallback", "原顶")
        notes.append(f"{why}，改成{nxt}")
        if from_intent and why.startswith(("外圈宽",)):
            viol.append({"check": "slot", "msg": f"{rm['name']}：{why}"})
        elif named and t == named:
            named_v = {"check": "template", "why": why}
            viol.append(named_v)
        t = nxt
        chain.append(t)
    if named_v is not None:   # 报最终真实的结果（可能一路退了几级）
        res = "原顶（这间房没有吊顶）" if t == "原顶" else t
        steps = f"（{' → '.join(chain)}）" if len(chain) > 2 else ""
        named_v["msg"] = f"{rm['name']}：意图写的{named}做不了（{named_v.pop('why')}），最后退成{res}{steps}；改意图的模板或参数"
    low = _lowest(t, p, H)
    if low < need:
        viol.append({"check": "net_height", "value": round(low), "msg": f"{rm['name']}：吊顶底 {low:.0f} 低于净高门槛 {need}"})
    if t != "原顶" and low < head:
        viol.append({"check": "below_opening", "value": round(low), "msg": f"{rm['name']}：吊顶底 {low:.0f} 低于门窗上沿 {head:.0f}"})
    if t != "原顶":
        top = max(_bottom(k, p, H) for k in {"平顶": ("flat",), "边吊": ("drop",), "回字形": ("drop", "slot")}[t])   # 最高的那块的底
        if top >= H:
            what = {"平顶": "底标高", "边吊": "下吊后的底", "回字形": "外圈、灯槽的底"}[t]
            viol.append({"check": "above_slab", "value": round(top), "msg": f"{rm['name']}：{t}{what} {top:.0f} 不低于原顶 {H:.0f}"
                                                                           f"（吊顶底要在 {max(need, head):.0f}～{H - 1:.0f} 之间：bottom 写在这个范围里，"
                                                                           f"或 drop 写 1～{H - max(need, head):.0f}）"})
    blocks = []
    if t != "原顶":
        cw = spec.get("_curtain_walls", [])
        for part, g, kind in _shape(t, p, R, rm, cw):
            for k, poly in enumerate(_parts(g)):
                ext, holes = _coords(poly)
                b = {"room": rm["id"], "template": t, "part": part, "material": "curtain" if kind == "curtain" else p["material"],
                     "bottom": round(_bottom(kind, p, H)), "polygon": ext, "holes": holes}
                if t in ("边吊", "回字形") and kind != "curtain":
                    b["width"] = p["width"]
                    if t == "回字形":
                        b["slot"] = p["slot"]
                if kind == "curtain":
                    b["width"] = p["curtain_width"]
                blocks.append(b)
    return t, blocks, notes, viol


def _intent_spec(blk, rm, errors):
    """意图里一间房的块 → 规则表缺省上的改动；写错的字段报一行、不采用。"""
    spec = {}
    bad = sorted(set(blk) - set(KEYS))
    if bad:
        errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：不认识的键 {'、'.join(bad)}"})
    if "ceiling" in blk:
        if blk["ceiling"] in TEMPLATES:
            spec["ceiling"] = blk["ceiling"]
        else:
            errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent",
                           "msg": f"{rm['name']}：没有模板 {blk['ceiling']}（可用：{'、'.join(TEMPLATES)}）"})
    for k in NUMS:
        if k in blk:
            v = blk[k]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 5000:
                spec[k] = float(v)
            else:
                errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：{k} 要是 0～5000 的数（毫米）"})
    if "material" in blk:
        if blk["material"] in MATERIALS and blk["material"] != "curtain":
            spec["material"] = blk["material"]
        else:
            errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：没有材料 {blk['material']}"})
    if "curtain" in blk:
        spec["curtain"] = blk["curtain"]
    return spec


def _curtain_walls(rm, ops, want, errors, from_intent):
    if want in (None, False, 0):
        return []
    ww = _window_walls(rm, ops)
    if want is True:
        return ww
    ids = [want] if isinstance(want, str) else want if isinstance(want, list) else None
    if ids is None or not all(isinstance(x, str) for x in ids):
        errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：curtain 要写 true、false 或墙边 id（如 \"W2\"）"})
        return []
    out = []
    for x in ids:
        if not any(w["id"] == x for w in rm["walls"]):
            errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：没有墙边 {x}"})
        elif x not in ww:
            errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent",
                           "msg": f"{rm['name']}：{x} 上没有窗，窗帘盒要靠窗（有窗的墙：{'、'.join(ww) or '无'}）"})
        else:
            out.append(x)
    return out


def stale_lights(rec: dict, sch: dict, units=None) -> list:
    """方案里已经有跟着吊顶走的灯（筒灯、灯带：mep.py 布过）时，按 mep.validate 的 ceiling_light 那项查一遍：吊顶改过以后灯还在旧位置
    （不在吊顶块里、灯带不在灯槽里…）的每盏记一条违规 stale_lights，提示重跑 mep.py。没有这类灯时（水电还没布、或没吊顶）直接返回空。"""
    from . import mep
    if not any(mep.point_type(it) in mep.GROUPED for it in sch.get("items", [])):
        return []
    bad = [v for v in mep.validate(sch["items"], rec, sch, units) if v["check"] == "ceiling_light"]
    return [{"room": v["room"], "room_id": v["room_id"], "check": "stale_lights", "item": v["item"],
             "msg": f"吊顶改了，筒灯、灯带要跟着重布：{v['msg']}；重跑 scripts/mep.py"} for v in bad]


def adjust_lights(sch: dict) -> list:
    """方案里有吊顶时，灯的高度跟吊顶走：灯体顶贴所在那块吊顶的底（窗帘盒不算），不在吊顶块里的贴原顶。只改 z，不改位置。"""
    blocks = [(b, Polygon(b["polygon"], b.get("holes") or [])) for b in sch.get("ceilings", []) if b["part"] != "窗帘盒"]
    out = []
    for it in sch.get("items", []):
        if it.get("key") in LIGHTS:
            pt = Point(it["x"], it["y"])
            hit = [b["bottom"] for b, g in blocks if b["room"] == it.get("room") and g.buffer(1).contains(pt)]
            top = min(hit) if hit else room_height(sch, it.get("room"))
            it = dict(it, z=float(top - LIGHTS[it["key"]]))
        out.append(it)
    return out


def ceiling(rec: dict, sch: dict, intent: dict | None = None, units=None) -> tuple[dict, dict]:
    """识别 + 带家具的方案 [+ 意图] → (带吊顶的方案, 校验报告)。只做户内房间（公共空间不吊）；units：只重做这几户，别的户
    以前生成的吊顶原样留着。意图写错的只报那一条（check=intent），别的照做；户号不在识别结果里抛 ValueError。"""
    from .unitview import unit_ids
    geo = sch.get("after") or rec
    if units and (bad := [u for u in units if u not in unit_ids(rec, sch)]):
        raise ValueError(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unit_ids(rec, sch)) or '无'}）")
    if intent is not None:
        check_intent(intent)
    unit = {r["id"]: r.get("unit") or "" for r in geo["rooms"]}
    public = {r["id"] for r in geo["rooms"] if r.get("public")}
    ops = geo["openings"]
    rooms = furnish.rooms_of(rec, sch)
    errors, viol, report_rooms = [], [], []
    given = {}
    for blk in (intent or {}).get("rooms", []):
        rm = furnish.find_room(rooms, blk.get("room"))
        if isinstance(rm, str):
            errors.append({"room": str(blk.get("room")), "room_id": "", "check": "intent", "msg": rm})
            continue
        if units and unit.get(rm["id"]) not in units:
            continue
        given[rm["id"]] = (blk, rm)
    keep = [b for b in sch.get("ceilings", []) if units and unit.get(b["room"]) not in units]
    blocks = []
    for rm in rooms:
        if rm["id"] in public or not unit.get(rm["id"]) or (units and unit[rm["id"]] not in units):
            continue
        base = dict(ROOMS.get(rm["type"], ROOMS["other"]))
        from_intent = rm["id"] in given
        spec = dict(base, **(_intent_spec(given[rm["id"]][0], rm, errors) if from_intent else {}))
        if from_intent and "ceiling" in spec and spec["ceiling"] != base["ceiling"] and "curtain" not in given[rm["id"]][0]:
            spec["curtain"] = TEMPLATES[spec["ceiling"]].get("curtain", False)
        spec["_curtain_walls"] = _curtain_walls(rm, ops, spec.get("curtain"), errors, from_intent and "curtain" in given[rm["id"]][0])
        H = room_height(sch, rm["id"])
        named = spec["ceiling"] if from_intent and "ceiling" in given[rm["id"]][0] and "ceiling" in spec else None
        t, bl, notes, vv = plan_room(rm, spec, H, ops, from_intent, named)
        for v in vv:
            viol.append(dict({"room": rm["name"], "room_id": rm["id"]}, **v))
        report_rooms.append({"room": rm["name"], "room_id": rm["id"], "template": t, "source": "意图" if from_intent else "规则",
                             "height": H, "blocks": len(bl), **({"fallback": "；".join(notes)} if notes else {})})
        blocks += bl
    allb = keep + blocks
    for i, b in enumerate(allb):
        b.pop("id", None)
    n = {}
    for b in allb:
        n[b["room"]] = n.get(b["room"], 0) + 1
        b["id"] = f"C-{b['room']}-{n[b['room']]}"
    out = dict(sch, ceilings=[dict({"id": b["id"]}, **{k: v for k, v in b.items() if k != "id"}) for b in allb])
    out["items"] = adjust_lights(out)
    if not report_rooms:   # 一间房都没做：房间都没有户号（unit），或都是公共空间
        errors.append({"room": "", "room_id": "", "check": "no_rooms",
                       "msg": "一间房都没做吊顶：识别结果里的房间都没有户号（unit）或都是公共空间（public）；先给户内房间写上 unit"})
    viol += stale_lights(rec, out, units)
    counts = {}
    for v in viol:
        counts[v["check"]] = counts.get(v["check"], 0) + 1
    return out, {"ok": not errors and not viol, "ceilings": len(blocks), "rooms": report_rooms, "errors": errors, "violations": viol, "counts": counts}
