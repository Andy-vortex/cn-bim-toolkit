"""门窗：按国内惯例编号、建门窗类型、统计门窗表（只用 ifcopenshell + shapely，不用 bpy）。

代号、类别名、默认高度、入户门和电梯层门的判断词表都在约定包 packs/cn/drawing/conventions.json（见 conventions.py）。

编号规则（同类别、同洞口尺寸共用一个编号；宽、高各取分米两位）：
  入户门（防盗门）FDM1021、平开门 M0921、双扇平开门 SM1521、推拉门 TLM1821、折叠门 ZDM1821、卷帘门 JLM3024、
  旋转门 XZM2424、门联窗 MLC2421、防火门 FM乙1021、窗 C1815、凸窗 TC1819。
  - 分米取整用四舍五入 int(v/100+0.5)，不用 round()（round 是银行家舍入，650 会变成 06）；
  - 取整后撞号（如 640 与 600 都是 06）时，取整后正好等于自身尺寸的那组（600）用不带后缀的编号，其余加 a、b…；
  - 宽或高 ≥ 9950mm 时分米位变成三位，编号多一位（如 10200 宽的窗是 C10215）。
类别：门联窗看 JZ_Opening.Kind 或名称；防火门看 Pset_DoorCommon.FireRating（认得出甲/乙/丙级或耐火时间才算，
  "0""无"之类不算）；入户门 = 名称写明入户门，或外墙上开向玄关、门厅的平开门，或连接户内（JZ_Space.Unit）与公共交通
  空间的平开门（阳台、露台、后门、单元门等除外，见 is_entrance_door）；
  其余按 OperationType 对应的类别（推拉、折叠、卷帘、旋转…）。电梯层门（相邻空间是电梯）由电梯厂家配套，不编号、不进门窗表。
原图编号优先：构件带原图编号（JZ_Opening.Label，如 FD乙1121、TC1818）、格式是"代号+宽高"、宽高与洞口相差 ≤1dm、
代号与类别和防火等级不矛盾的，直接沿用原图编号；否则按上面的规则生成。同一编号仍须同类别、同尺寸，冲突的照样加后缀。
编号写进 IfcDoor/IfcWindow 的 Tag；每个编号建一个 IfcDoorType/IfcWindowType（Name = 编号）并指派，
便于 Bonsai 和其他软件按类型统计。

高度：取 OverallHeight；构件属性 JZ_Opening.HeightSource == "默认值"（识别结果没有高度时 plan2ifc 写的），
或者没有 OverallHeight（这时按默认值），门窗表备注写"高度为默认值，待实测"。窗台高 = 窗洞底到所在房间楼面的高度，
JZ_Opening.SillSource == "默认值" 的注明待实测。洞口宽：OverallWidth，没有就用构件投影在墙方向上的宽度。

几何（门窗的墙面法线、所在房间、外墙朝向）都从 IFC 几何算，单位：米（世界坐标），和 Blender 场景一致。
门窗本身和平开门扫过的地方（opening_zones）是纯几何：出图避让（annotate）和家具校验（furnish，识别结果的毫米坐标）共用这一份。
"""
from __future__ import annotations

import collections
import importlib
import math
import re

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

try:   # 家具校验器（furnish）在没有 ifcopenshell 的 .venv 里只用纯几何的 opening_zones、rec_opening_zones
    import ifcopenshell
    import ifcopenshell.api as api
    import ifcopenshell.geom
    import ifcopenshell.util.element as uel
    import ifcopenshell.util.placement as upl
    import ifcopenshell.util.unit as uu
except ImportError:
    ifcopenshell = None

from . import conventions, markcode
from .printing import log

if ifcopenshell is not None:
    for _m in ("root", "type"):
        importlib.import_module("ifcopenshell.api." + _m)

# 约定包 conventions.json 的 openings 一节：和别的模块一样在导入时读（入口脚本先用 conventions.check_pack 查约定包，再导入本模块）
_CONV = conventions.get("openings")
_H = _CONV["default_height_mm"]
DEFAULT_DOOR_H, DEFAULT_WINDOW_H, DEFAULT_BAY_H = float(_H["door"]), float(_H["window"]), float(_H["bay"])
DEFAULT_SILL, DEFAULT_BAY_SILL = float(_CONV["default_sill_mm"]["window"]), float(_CONV["default_sill_mm"]["bay"])
WINDOW_HEAD = DEFAULT_SILL + DEFAULT_WINDOW_H   # 窗高不知道的窗一律到这个窗顶（缺省窗台 + 缺省窗高）
MIN_WINDOW_H = float(_CONV["min_window_height_mm"])
HIGH_SILL = float(_CONV["high_window_sill_mm"])
# 类别：代号 → (类别名, IFC 类)；顺序就是门窗表里的顺序
CATEGORIES = collections.OrderedDict((c["code"], (c["name"], c["ifc"])) for c in _CONV["categories"])
ROLE = _CONV["roles"]
_OP_CODE = _CONV["operation_category"]
_OP_TEXT = _CONV["operation_text"]
_SWING = ("SINGLE_SWING_LEFT", "SINGLE_SWING_RIGHT", "DOUBLE_SWING_LEFT", "DOUBLE_SWING_RIGHT", "DOUBLE_DOOR_SINGLE_SWING",
          "DOUBLE_DOOR_DOUBLE_SWING", "DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_LEFT", "DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_RIGHT",
          "NOTDEFINED", "USERDEFINED", "")
CIRCULATION = tuple(_CONV["circulation_words"])
# 防火等级在表里的先后：甲、乙、丙（按耐火时间从长到短），等级待定的排最后
_GRADE_ORDER = {g: i for i, g in enumerate(sorted(_CONV["fire"]["grade_minutes"], key=lambda g: -_CONV["fire"]["grade_minutes"][g]))}
_grade_key = lambda g: _GRADE_ORDER.get(g or "", len(_GRADE_ORDER)) if g is not None else -1

if ifcopenshell is not None:
    _GS = ifcopenshell.geom.settings()
    _GS.set("use-world-coords", True)


# ------------------------------------------------------------------ 门窗占的地方（纯几何）
def oriented_box(cx, cy, ux, uy, half_u, half_n):
    """以 (cx, cy) 为中心、沿 u 半长 half_u、沿法线半宽 half_n 的矩形。"""
    nx, ny = -uy, ux
    return Polygon([(cx + sx * ux * half_u + sy * nx * half_n, cy + sx * uy * half_u + sy * ny * half_n)
                    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))])


_LEAF_SWING = ("SINGLE_SWING_LEFT", "SINGLE_SWING_RIGHT", "DOUBLE_SWING_LEFT", "DOUBLE_SWING_RIGHT")


def opening_zones(c, u, n, w, t, op=None, pad=0.02):
    """门窗本身占的地方（墙厚范围内的洞口，四周放出 pad）+ 平开门扫过的地方：[洞口, 扇形…]。单位跟着输入（出图用米，
    pad 0.02；家具校验用毫米，pad 20）。c 墙中线上的洞口中心，u 沿墙，n 门扇开向的一侧（numpy 单位向量），w 洞宽，t 墙厚，
    op 门的 OperationType（窗为 None）。门扇向 +n 开，铰链在墙面上（ifcopenshell 参数化门的约定：单扇 LEFT 铰链在 -u 端、
    RIGHT 在 +u 端；双扇两头各一扇）：单扇、双扇平开门按扇形算（扇形外的角上能放字），其余开启方式按整块矩形算。"""
    zones = [oriented_box(c[0], c[1], u[0], u[1], w / 2 + pad, t / 2 + pad)]
    if op is not None:
        face = c + n * (t / 2)

        def sector(h, d, r):   # 铰链 h，门扇关着时朝 d，开到 +n
            return Polygon([tuple(h)] + [tuple(h + r * (math.cos(a) * d + math.sin(a) * n)) for a in np.linspace(0.0, math.pi / 2, 10)])
        if op == "SINGLE_SWING_LEFT":
            zones.append(sector(face - u * w / 2, u, w))
        elif op == "SINGLE_SWING_RIGHT":
            zones.append(sector(face + u * w / 2, -u, w))
        elif op in ("DOUBLE_DOOR_SINGLE_SWING", "DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_LEFT", "DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_RIGHT"):
            zones += [sector(face - u * w / 2, u, w / 2), sector(face + u * w / 2, -u, w / 2)]
        else:
            leaf = w if op in _LEAF_SWING else (w / 2 if op.startswith("DOUBLE_DOOR") and "SWING" in op else 0.0)
            if leaf:
                cc = c + n * (t / 2 + leaf / 2)
                zones.append(oriented_box(cc[0], cc[1], u[0], u[1], w / 2, leaf / 2))
    return zones


def rec_door(o: dict) -> tuple[float, str]:
    """识别结果（dxfplan）里的一樘门 → (IFC 门的转角, OperationType)。转角让局部 +y 指向开启一侧（swing=-1 时转 180°）；
    leafs 0 推拉、2 双扇平开，单扇看铰链在哪端。plan2ifc 建门和家具校验算门扇用的是这一份。"""
    s = o.get("swing", 1) or 1
    rot = o["rot"] + (180.0 if s < 0 else 0.0)
    leafs = o.get("leafs", 1)
    op = ("SLIDING_TO_LEFT" if leafs == 0 else "DOUBLE_DOOR_SINGLE_SWING" if leafs >= 2
          else "SINGLE_SWING_LEFT" if o.get("hinge_end", -1) * s < 0 else "SINGLE_SWING_RIGHT")
    return rot, op


def rec_frame(o: dict) -> tuple:
    """识别结果的门窗（不含凸窗）→ (c, u, n, w, t, op)，毫米、图纸坐标；门的 n 是开启一侧，窗的 op 为 None。"""
    if o["kind"] == "door":
        rot, op = rec_door(o)
    else:
        rot, op = o["rot"] + (180.0 if o.get("sy", 1) < 0 else 0.0), None
    u = np.array([math.cos(math.radians(rot)), math.sin(math.radians(rot))])
    return np.array([float(o["x"]), float(o["y"])]), u, np.array([-u[1], u[0]]), float(o["width"]), float(o["wall_t"] or 200.0), op


def rec_opening_zones(o: dict) -> list:
    """识别结果的门窗占的地方（毫米）：和出图时从 IFC 算的（annotate._opening_zone）是同一个函数；凸窗是它的轮廓。"""
    if o.get("polygon"):
        return [Polygon(o["polygon"]).buffer(0)]
    c, u, n, w, t, op = rec_frame(o)
    return opening_zones(c, u, n, w, t, op, pad=20.0)


def rec_size(o: dict, width0: float | None = None) -> dict:
    """识别结果里的一樘门窗 → 编号和竖向尺寸：{label, note, sill, sill_src, height, height_src}（毫米，离本层楼面；门的 sill 是 None）。
    建模（plan2ifc）、墙面（wall 挖的洞）、吊顶（门窗顶）、水电（点位避窗）都用这一份，改取法只改这里，几处才对得上。
    - 原图编号：markcode.adopt（width0 是拆改前的识别宽度，拆改改了宽度的不沿用）；采用的编号带高度。
    - 门：落地；高 = 编号里的，没有用缺省门高。
    - 凸窗（有 polygon）：窗台一律缺省；高 = 识别结果的 height、编号里的、缺省凸窗高。
    - 窗：窗台 = 识别结果的 sill，没有用缺省；高 = 识别结果的 height、编号里的；都没有时按统一的窗顶 WINDOW_HEAD
      （缺省窗台 + 缺省窗高）算，窗高 = 窗顶 − 窗台，不小于 min_window_height_mm。窗台是缺省窗台时正好是缺省窗高；
      窗台 1500 的高窗是 900，不会按 1500 + 1500 顶到 3000（穿过吊顶和原顶）。这样算的高度来源仍是"默认值"（门窗表注待实测）。"""
    w = float(o["width"])
    lab, note = markcode.adopt(o, w, width0)
    lab_h = markcode.label_height(lab, w)
    rec_h = markcode.rec_height(o) if o["kind"] == "window" else None
    out = {"label": lab, "note": note}
    if o.get("polygon"):
        sill, sill_src, dflt = DEFAULT_BAY_SILL, "默认值", DEFAULT_BAY_H
    elif o["kind"] == "door":
        return dict(out, sill=None, sill_src=None, height=lab_h or DEFAULT_DOOR_H, height_src="原图编号" if lab_h else "默认值")
    else:
        sill, sill_src = (float(o["sill"]), "识别结果") if o.get("sill") is not None else (DEFAULT_SILL, "默认值")
        dflt = max(WINDOW_HEAD - sill, MIN_WINDOW_H)
    h, h_src = (rec_h, "识别结果") if rec_h else (lab_h, "原图编号") if lab_h else (dflt, "默认值")
    return dict(out, sill=sill, sill_src=sill_src, height=float(h), height_src=h_src)


def rec_extent(o: dict, width0: float | None = None) -> tuple:
    """门窗洞口的竖向范围 (底, 顶)（离本层楼面，毫米），门的底是 None（落地）；取法见 rec_size。"""
    s = rec_size(o, width0)
    return (None, s["height"]) if s["sill"] is None else (s["sill"], s["sill"] + s["height"])


# ------------------------------------------------------------------ 属性
def _jz(el) -> dict:
    return uel.get_psets(el).get("JZ_Opening", {})


def _is_bay(el) -> bool:
    t = uel.get_type(el)
    return (el.ObjectType == "BAY" or "凸窗" in str(_jz(el).get("Kind", ""))
            or (t is not None and (getattr(t, "ElementType", None) == "BAY" or str(t.Name or "").startswith(ROLE["bay"]))))


def parse_fire_rating(fr) -> str | None:
    """FireRating 的写法 → 防火等级 甲/乙/丙；要求防火但等级待定返回 ""；认不出来（"0""无"等）返回 None。
    认得出的：含 甲/乙/丙；耐火时间（"1.5h""90min""A1.50""EI60""E 30"），按约定包的分钟数换算；约定包里的"待定"类字样。"""
    s = str(fr or "").strip()
    if not s:
        return None
    m = re.search("[甲乙丙]", s)
    if m:
        return m.group(0)
    fire = _CONV["fire"]
    minutes = None
    if (m := re.search(r"(\d+(?:\.\d+)?)\s*(h|H|小时)", s)):
        minutes = float(m.group(1)) * 60
    elif (m := re.search(r"(\d+(?:\.\d+)?)\s*(min|分钟)", s)):
        minutes = float(m.group(1))
    elif (m := re.fullmatch(r"(?:EI|EW|E)\s*(\d+)", s, flags=re.I)):
        minutes = float(m.group(1))
    elif (m := re.fullmatch(r"[ABC]\s*(\d\.\d{2})", s)):
        minutes = float(m.group(1)) * 60
    if minutes is not None:
        if minutes <= 0:
            return None
        for g, need in sorted(fire["grade_minutes"].items(), key=lambda kv: -kv[1]):
            if minutes >= need - 1e-6:
                return g
        return ""
    return "" if any(w in s for w in fire["pending_words"]) else None


def fire_grade(el) -> str | None:
    """防火门等级：Pset_DoorCommon.FireRating 认得出的 甲/乙/丙；要求防火但等级待定返回 ""；不是防火门返回 None。"""
    if not el.is_a("IfcDoor"):
        return None
    return parse_fire_rating(uel.get_psets(el).get("Pset_DoorCommon", {}).get("FireRating"))


def _has(usage: str, words) -> bool:
    return any(w in (usage or "") for w in words)


def is_elevator_door(el, geo: "PlanGeometry | None" = None) -> bool:
    """电梯层门：名称写明（电梯门、层门），或相邻空间是电梯（"电梯""消防电梯"等，不含电梯厅、前室、机房）。"""
    if not el.is_a("IfcDoor"):
        return False
    ev = _CONV["elevator"]
    if _has(str(el.Name or "") + str(_jz(el).get("Kind", "")), ev["name_words"]):
        return True
    if geo is None:
        return False
    return any(_has(u, ev["room_words"]) and not _has(u, ev["room_exclude"]) for u in (geo.usage(s) for s in geo.rooms_of(el)))


def is_entrance_door(el, geo: "PlanGeometry | None" = None) -> bool:
    """入户门：平开门，名称和两侧空间都不含 exclude_words（阳台、露台、后门、单元门……），且满足其一：
    ① 名称写明入户门；② 在外墙上，户内一侧是玄关、门厅、客厅这类（约定包 inner_room_words）、不是公共空间；
    ③ 一侧是户内空间（JZ_Space.Unit 不为空）、另一侧是不属于任何户的公共交通空间（JZ_Space.Public 或 public_room_words）。
    模型里没有户的信息时 ③ 不用：分不清户内户外，走廊上的房间门（办公、宿舍、客房）不能当成防盗门。"""
    if not el.is_a("IfcDoor") or (el.OperationType or "") not in _SWING:
        return False
    ent = _CONV["entrance"]
    if _has(str(el.Name or ""), ent["exclude_words"]):
        return False
    if _has(str(el.Name or ""), ent["name_words"]):
        return True
    if geo is None:
        return False
    rooms = geo.rooms_of(el)
    if any(_has(geo.usage(s), ent["exclude_words"]) for s in rooms):
        return False
    if uel.get_psets(el).get("JZ_Opening", {}).get("Entrance"):   # 按户出图：公共一侧不在模型里，裁的时候按 ③ 认好记下的（unitview）
        return True
    public = lambda s: bool(uel.get_psets(s).get("JZ_Space", {}).get("Public")) or _has(geo.usage(s), ent["public_room_words"])
    fr = geo.frame(el)
    if fr is not None and fr.get("out") is not None:
        return len(rooms) == 1 and _has(geo.usage(rooms[0]), ent["inner_room_words"]) and not public(rooms[0])
    if not geo.has_units or len(rooms) != 2:
        return False
    info = []
    for s in rooms:
        info.append((str(uel.get_psets(s).get("JZ_Space", {}).get("Unit") or ""), public(s)))
    return any(not ua and pa and ub and not pb for (ua, pa), (ub, pb) in (info, info[::-1]))


def category(el, geo: "PlanGeometry | None" = None) -> str:
    """门窗类别代号（见 CATEGORIES）；电梯层门返回 ""（不编号、不进门窗表）。
    给 geo（平面几何）时才判断入户门和电梯层门（要看在不在外墙上、两侧是什么空间）。"""
    if el.is_a("IfcWindow"):
        return ROLE["bay"] if _is_bay(el) else ROLE["window"]
    if is_elevator_door(el, geo):
        return ""
    kind = str(_jz(el).get("Kind", "")) + str(el.Name or "")
    if "门联窗" in kind:
        return ROLE["door_with_window"]
    if fire_grade(el) is not None:
        return ROLE["fire"]
    if is_entrance_door(el, geo):
        # 原图标了别的门类编号（如 M1021）且尺寸对得上：以原图为准，不改成入户门
        lab = str(_jz(el).get("Label") or "")
        p = markcode.parse(lab)
        if not (p and p["prefix"] != ROLE["entrance"] and p["prefix"] in CATEGORIES and markcode.fits(lab, size_mm(el, geo)[0])):
            return ROLE["entrance"]
    return _OP_CODE.get(el.OperationType or "", ROLE["door"])


def size_mm(el, geo: "PlanGeometry | None" = None) -> tuple[float, float, bool]:
    """(洞口宽, 洞口高, 高度是否为默认值)，毫米。OverallWidth 为空时用构件投影在墙方向上的宽度（要给 geo）。"""
    s = uu.calculate_unit_scale(el.file) * 1000.0
    w = float(el.OverallWidth or 0.0) * s
    if not w and geo is not None:
        fr = geo.frame(el)
        w = fr["w"] * 1000.0 if fr else 0.0
    default = str(_jz(el).get("HeightSource", "")) == "默认值"
    if el.OverallHeight:
        h = float(el.OverallHeight) * s
    else:
        h, default = (DEFAULT_DOOR_H if el.is_a("IfcDoor") else DEFAULT_BAY_H if _is_bay(el) else DEFAULT_WINDOW_H), True
    return w, h, default


def _dm(v: float) -> str:
    """分米：四舍五入（int(v/100+0.5)，不用银行家舍入的 round()），不足两位补 0；≥99.5 分米时是三位。"""
    return f"{int(v / 100.0 + 0.5):02d}"


def operation_text(el) -> str:
    if el.is_a("IfcDoor"):
        return _OP_TEXT.get(el.OperationType or "", "待定")
    return "待定"   # 窗的开启方式识别不出来，交给设计确定


# ------------------------------------------------------------------ 编号 + 类型
def orig_label(el, code: str | None = None, geo: "PlanGeometry | None" = None) -> str:
    """构件的原图编号（JZ_Opening.Label）能直接当编号用就返回它，否则返回 ""：
    格式是"代号+宽高"，宽高与洞口相差 ≤1dm，代号认得的要和类别一致（FM/FD/FH 都算防火门），防火等级不矛盾。"""
    label = str(_jz(el).get("Label") or "")
    p = markcode.parse(label)
    if p is None:
        return ""
    code = category(el, geo) if code is None else code
    if not code:
        return ""
    w, h, _ = size_mm(el, geo)
    want = ROLE["fire"] if p["prefix"] in markcode.FIRE_PREFIXES else (p["prefix"] if p["prefix"] in CATEGORIES else None)
    grade = fire_grade(el) if code == ROLE["fire"] else None
    if not markcode.fits(label, w, h) or (want is not None and want != code) or (p["grade"] or None) != (grade or None):
        return ""
    return label


def assign_marks(f: ifcopenshell.file, geo: "PlanGeometry | None" = None) -> list[dict]:
    """给全部门窗编号（写 Tag）、按编号建立并指派 IfcDoorType/IfcWindowType。返回每樘门窗的记录（电梯层门 mark 为空）。
    调用时 Bonsai 可能已经加载（在 Blender 里跑），所以 API 调用一律关掉监听器（should_run_listeners=False），
    否则 Bonsai 会按类型去重建门窗洞口。"""
    geo = geo or PlanGeometry(f)
    recs, skipped = [], []
    for el in list(f.by_type("IfcDoor")) + list(f.by_type("IfcWindow")):
        code = category(el, geo)
        if not code:   # 电梯层门：电梯厂家配套，不编号
            el.Tag = None
            skipped.append({"el": el, "code": "", "mark": ""})
            continue
        w, h, default = size_mm(el, geo)
        grade = fire_grade(el) if code == ROLE["fire"] else None
        recs.append({"el": el, "code": code, "grade": grade, "w": round(w), "h": round(h), "default_h": default,
                     "label": str(_jz(el).get("Label") or ""), "orig": orig_label(el, code, geo), "op": operation_text(el)})
    # 分组：类别（含防火等级）+ 精确尺寸 + 编号（原图编号能用就用它，否则按规则生成）
    groups = collections.OrderedDict()
    for r in sorted(recs, key=lambda r: (list(CATEGORIES).index(r["code"]), _grade_key(r["grade"]), r["w"], r["h"], r["orig"])):
        base = r["orig"] or f"{r['code']}{r['grade'] or ''}{_dm(r['w'])}{_dm(r['h'])}"
        groups.setdefault((r["code"], r["grade"], r["w"], r["h"], base), []).append(r)
    # 同一个号被不同类别/尺寸占用时：原图编号先占号，其次是取整后正好等于自身尺寸的那组，其余按尺寸加 a、b…
    exact = lambda k: int(_dm(k[2])) * 100 == k[2] and int(_dm(k[3])) * 100 == k[3]
    used = collections.defaultdict(int)
    marks = {}
    for key in sorted(groups, key=lambda k: (not groups[k][0]["orig"], not exact(k))):
        base = key[4]
        n = used[base]
        used[base] += 1
        marks[key] = base + ("" if n == 0 else "abcdefghijklmnopqrstuvwxyz"[n - 1])
    types = {}
    for t in list(f.by_type("IfcDoorType")) + list(f.by_type("IfcWindowType")):
        types[(t.is_a(), t.Name)] = t
    for key, rs in groups.items():
        mark = marks[key]
        code = key[0]
        ifc_cls = CATEGORIES[code][1] + "Type"
        t = types.get((ifc_cls, mark))
        if t is None:
            t = api.root.create_entity(f, ifc_class=ifc_cls, name=mark, should_run_listeners=False)
            types[(ifc_cls, mark)] = t
        t.Description = f"{CATEGORIES[code][0]} {key[2]}×{key[3]}"
        if ifc_cls == "IfcDoorType":
            t.PredefinedType = "DOOR"
            ops = {r["el"].OperationType for r in rs}
            t.OperationType = ops.pop() if len(ops) == 1 and None not in ops else "NOTDEFINED"
        else:
            if code == ROLE["bay"]:
                t.PredefinedType, t.ElementType = "USERDEFINED", "BAY"
            else:
                t.PredefinedType = "WINDOW"
            parts = {getattr(r["el"], "PartitioningType", None) for r in rs}
            t.PartitioningType = parts.pop() if len(parts) == 1 and None not in parts else "NOTDEFINED"
        api.type.assign_type(f, related_objects=[r["el"] for r in rs], relating_type=t, should_run_listeners=False)
        for r in rs:
            r["el"].Tag = mark
            r["mark"] = mark
    if skipped:
        log("elevator doors not marked", len(skipped), sorted(str(r["el"].Name) for r in skipped))
    return recs + skipped


def assign_marks_file(path: str) -> list[dict]:
    """打开 IFC 文件、编号、写回。返回记录里的构件只用于统计（文件已经写完）。"""
    f = ifcopenshell.open(path)
    recs = assign_marks(f)
    f.write(path)
    return recs


# ------------------------------------------------------------------ 几何（米，世界坐标）
class PlanGeometry:
    """平面几何缓存：构件投影轮廓、建筑外轮廓、门窗的墙面坐标系。供编号定位、尺寸、门窗表共用。"""

    SOFT = ("IfcFurniture", "IfcFurnishingElement", "IfcSanitaryTerminal", "IfcElectricAppliance", "IfcOutlet", "IfcSwitchingDevice", "IfcAlarm",
            "IfcRailing", "IfcAudioVisualAppliance", "IfcLightFixture", "IfcWasteTerminal", "IfcStair", "IfcStairFlight")

    def __init__(self, f: ifcopenshell.file):
        self.f = f
        self.S = uu.calculate_unit_scale(f)          # 项目单位 → 米
        self._fp = {}
        self._zmin = {}
        self.walls = [w for w in f.by_type("IfcWall")]
        self.spaces = list(f.by_type("IfcSpace"))
        self.openings = list(f.by_type("IfcDoor")) + list(f.by_type("IfcWindow"))
        self.has_units = any(uel.get_psets(s).get("JZ_Space", {}).get("Unit") for s in self.spaces)
        wall_polys = [p for p in (self.fp(w) for w in self.walls) if p is not None]
        self.wall_union = unary_union(wall_polys) if wall_polys else Polygon()
        self.wall_tree = shapely.STRtree(wall_polys) if wall_polys else None
        self.wall_polys = wall_polys   # 墙的投影轮廓，wall_tree 的下标和它一一对应
        parts = wall_polys + [p for p in (self.fp(s) for s in self.spaces) if p is not None] \
            + [p for p in (self.fp(o) for o in self.openings) if p is not None]
        # raw：构件直接合并（变形缝两侧是分开的几块）；building：外扩再内缩 0.15m，把墙和门窗之间的细缝合上。
        # 两者都可能是 MultiPolygon（分开的楼块、宽变形缝），用 .boundary 而不是 .exterior
        self.raw = unary_union(parts) if parts else Polygon()
        self.building = self.raw.buffer(0.15, join_style="mitre").buffer(-0.15, join_style="mitre") if parts else Polygon()
        self._frames = {}

    def fp(self, el):
        """构件在平面上的投影轮廓（三角面投影后合并）；没有几何返回 None（几何出错的记日志）。"""
        k = el.id()
        if k not in self._fp:
            poly, zmin = None, None
            try:
                sh = ifcopenshell.geom.create_shape(_GS, el)
                v3 = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3)
                v = v3[:, :2]
                fc = np.array(sh.geometry.faces, dtype=int).reshape(-1, 3)
                tris = v[fc]
                area = np.abs((tris[:, 1, 0] - tris[:, 0, 0]) * (tris[:, 2, 1] - tris[:, 0, 1])
                              - (tris[:, 2, 0] - tris[:, 0, 0]) * (tris[:, 1, 1] - tris[:, 0, 1]))
                tris = tris[area > 1e-9]
                if len(tris):
                    poly = shapely.union_all(shapely.polygons(np.concatenate([tris, tris[:, :1]], axis=1))).buffer(0)
                if len(v3):
                    zmin = float(v3[:, 2].min())
            except Exception as ex:   # 没有几何的构件（如只有属性的占位构件）也会到这里
                if getattr(el, "Representation", None) is not None:
                    log("footprint failed", el.id(), el.is_a(), el.Name, f"{type(ex).__name__}: {ex}")
                poly = None
            self._fp[k] = poly
            self._zmin[k] = zmin
        return self._fp[k]

    def zmin(self, el):
        """构件几何的最低点 z（米，世界坐标）；没有几何返回 None。"""
        self.fp(el)
        return self._zmin.get(el.id())

    def usage(self, sp) -> str:
        return uel.get_psets(sp).get("JZ_Space", {}).get("Usage") or sp.LongName or sp.Name or ""

    def space_at(self, p: Point):
        hit = [(self.fp(s).area, s) for s in self.spaces if self.fp(s) is not None and self.fp(s).buffer(0.005).contains(p)]
        return min(hit, key=lambda t: t[0])[1] if hit else None

    def sill_mm(self, el) -> float | None:
        """窗台高（毫米）：窗几何最低点到所在房间楼面（房间几何最低点）；找不到房间时到所在楼层的标高。"""
        z = self.zmin(el)
        if z is None:
            return None
        floors = [zz for zz in (self.zmin(s) for s in self.rooms_of(el)) if zz is not None]
        if floors:
            base = min(floors)
        else:
            st = uel.get_container(el)
            base = upl.get_storey_elevation(st) * self.S if st is not None and st.is_a("IfcBuildingStorey") else 0.0
        return (z - base) * 1000.0

    def crosses_wall(self, line, tol: float) -> bool:
        """线段（米）压墙的长度超过 tol（米）：引线、编号到洞口的连线、房名的引线是否穿墙；tol 吃掉线端碰到墙面的那一点
        （房名引线取一道墙厚）。按墙的并集量：几块墙重叠的地方（T 形、L 形接头）只算一次。"""
        if self.wall_tree is None or line.length <= 1e-6:
            return False
        idx = self.wall_tree.query(line)
        return len(idx) > 0 and shapely.union_all([self.wall_polys[i].intersection(line) for i in idx]).length > tol

    def _wall_band(self, q, n):
        """过 q 沿法线 n 的墙厚段：返回 (厚度, 段中点沿 n 的坐标)，q 不在墙里返回 None。"""
        if self.wall_tree is None:
            return None
        line = LineString([(q[0] - n[0] * 0.8, q[1] - n[1] * 0.8), (q[0] + n[0] * 0.8, q[1] + n[1] * 0.8)])
        best = None
        for i in self.wall_tree.query(line):
            inter = self.wall_polys[i].intersection(line)
            for g in getattr(inter, "geoms", [inter]):
                if g.is_empty or g.geom_type != "LineString":
                    continue
                if g.distance(Point(q)) > 0.03:
                    continue
                ts = [(c[0] - q[0]) * n[0] + (c[1] - q[1]) * n[1] for c in g.coords]
                seg = (min(ts), max(ts))
                if best is None or seg[1] - seg[0] > best[1] - best[0]:
                    best = seg
        if best is None:
            return None
        return best[1] - best[0], (best[0] + best[1]) / 2

    def frame(self, el) -> dict | None:
        """门窗的墙面坐标系：c 墙中线上的洞口中心，u 沿墙，n 墙法线（门：+n 是开启一侧），w 洞宽，t 墙厚，
        out 朝外的法线（不是外墙门窗为 None），side 外墙朝向 bottom/top/left/right，span 沿墙两端，bay 是否凸窗。"""
        k = el.id()
        if k in self._frames:
            return self._frames[k]
        fpl = self.fp(el)
        res = None
        if fpl is not None and _is_bay(el):
            res = self._bay_frame(el, fpl)
        elif fpl is not None:
            m = upl.get_local_placement(el.ObjectPlacement)
            O = np.array([m[0][3], m[1][3]]) * self.S
            u = np.array([m[0][0], m[1][0]]); u = u / (np.linalg.norm(u) or 1.0)
            n = np.array([m[0][1], m[1][1]]); n = n / (np.linalg.norm(n) or 1.0)
            # 洞口沿墙的起止：构件投影轮廓在墙方向（局部 x）上的最小、最大值，不依赖局部原点在哪
            # （ifcopenshell 参数化门窗在洞口一角，别的软件导出的可能在洞口中心）；OverallWidth 为空时洞宽也用它
            rings = [fpl.exterior] if fpl.geom_type == "Polygon" else [g.exterior for g in fpl.geoms]
            pts = np.array([p for r in rings for p in r.coords], dtype=float)
            ts = (pts - O) @ u
            a0, a1 = float(ts.min()), float(ts.max())
            w = float(el.OverallWidth or 0.0) * self.S or (a1 - a0)
            c = O + u * (a0 + a1) / 2
            # 墙厚和墙中线：在洞口两侧（墙垛）沿法线量
            t, mid = None, None
            for s in (-1, 1):
                q = c + u * s * (w / 2 + 0.06)
                band = self._wall_band(q, n)
                if band and band[0] > 0.04 and (t is None or band[0] < t):   # 取短的：墙垛那边可能正好是垂直相交的墙
                    t, mid = band
            if t is None:   # 两侧都没有墙（独立门洞等）：退回构件自身沿法线的厚度
                tn = (pts - c) @ n
                lo, hi = float(tn.min()), min(float(tn.max()), 0.3)
                t, mid = max(hi - lo, 0.1), (lo + hi) / 2
            c = c + n * mid
            res = {"el": el, "c": c, "u": u, "n": n, "w": w, "t": t, "bay": False, "fp": fpl,
                   "span": (c - n * mid - u * w / 2, c - n * mid + u * w / 2)}
            self._exterior(res)
        self._frames[k] = res
        return res

    def _bay_frame(self, el, fpl) -> dict:
        x0, y0, x1, y1 = fpl.bounds
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best = None
        for d, half in (((0, -1), (y1 - y0) / 2), ((0, 1), (y1 - y0) / 2), ((-1, 0), (x1 - x0) / 2), ((1, 0), (x1 - x0) / 2)):
            p = Point(cx + d[0] * (half + 0.3), cy + d[1] * (half + 0.3))
            dist = self.building.boundary.distance(p) if not self.building.is_empty else 0.0
            outside = not self.building.contains(p)
            score = (1 if outside else 0, dist)
            if best is None or score > best[0]:
                best = (score, d, half)
        _, d, half = best
        out = np.array(d, dtype=float)
        u = np.array([-out[1], out[0]])
        inner_c = np.array([cx, cy]) - out * half          # 凸窗靠室内的那条边的中点
        w = abs(float(np.dot(np.array([x1 - x0, y1 - y0]), u)))
        res = {"el": el, "c": inner_c, "u": u, "n": -out, "w": w, "t": 0.0, "bay": True, "fp": fpl, "depth": 2 * half,
               "span": (inner_c - u * w / 2, inner_c + u * w / 2), "out": out}
        res["side"] = _side_of(out)
        res["visible"] = self._visible(np.array([cx, cy]) + out * (half + 0.3), out)
        return res

    def _exterior(self, fr):
        """判断外墙门窗：洞口中心沿法线两边各探一点，恰好一边在建筑外轮廓外。"""
        c, n, t = fr["c"], fr["n"], fr["t"]
        ins = []
        for s in (1, -1):
            p = c + n * s * (t / 2 + 0.3)
            ins.append(self.building.contains(Point(p)))
        fr["out"] = None
        if ins[0] != ins[1]:
            out = n * (1 if not ins[0] else -1)
            fr["out"] = out
            fr["side"] = _side_of(out)
            fr["visible"] = self._visible(c + out * (t / 2 + 0.3), out)

    def _visible(self, p, out) -> bool:
        """从外墙面往外看出去不再碰到建筑（被前面的楼体挡住的洞口不进外墙细部尺寸）。"""
        far = LineString([(p[0], p[1]), (p[0] + out[0] * 500.0, p[1] + out[1] * 500.0)])
        return not far.intersects(self.building.buffer(-0.02))

    def rooms_of(self, el) -> list:
        """门窗所在的房间：门 = 两侧各探一点落在哪个空间；窗 = 室内一侧的空间；凸窗 = 离它最近的空间。"""
        fr = self.frame(el)
        if fr is None:
            return []
        if fr["bay"]:
            near = [(self.fp(s).distance(fr["fp"]), s) for s in self.spaces if self.fp(s) is not None]
            near = [t for t in near if t[0] < 0.45]
            return [min(near, key=lambda t: t[0])[1]] if near else []
        out = []
        for s in (1, -1):
            sp = self.space_at(Point(fr["c"] + fr["n"] * s * (fr["t"] / 2 + 0.25)))
            if sp is not None and all(sp.id() != o.id() for o in out):
                out.append(sp)
        return out


def _side_of(out) -> str | None:
    x, y = float(out[0]), float(out[1])
    if abs(x) < 0.17 and y < 0:
        return "bottom"
    if abs(x) < 0.17 and y > 0:
        return "top"
    if abs(y) < 0.17 and x < 0:
        return "left"
    if abs(y) < 0.17 and x > 0:
        return "right"
    return None


# ------------------------------------------------------------------ 门窗表
UNNAMED_ROOM = "未标注"   # 识别时没标房名的空间（dxfplan 给的用途）


def _primary_room(geo: PlanGeometry, el) -> str:
    """门窗表"所在位置"按一樘门窗记一个房间：先排除没标房名的空间，再排除公共空间和交通空间（玄关、走道等），
    再取面积小的那间（卧室门记卧室，卧室里的卫生间门记卫生间）。两侧都没有房名的写"—"。"""
    rooms = geo.rooms_of(el)
    if not rooms:
        return "外墙" if el.is_a("IfcWindow") else "其他"
    def key(sp):
        u = geo.usage(sp)
        pub = bool(uel.get_psets(sp).get("JZ_Space", {}).get("Public"))
        fp = geo.fp(sp)
        return (u in ("", UNNAMED_ROOM), pub, any(w in u for w in CIRCULATION), fp.area if fp is not None else 0.0)
    u = geo.usage(min(rooms, key=key))
    return "—" if u in ("", UNNAMED_ROOM) else u


def _room_canon(geo: PlanGeometry) -> dict:
    """组合房名的统一写法（只用于门窗表，不改 IFC 里的房名）："客厅/餐厅"和"餐厅/客厅"是同一组房间，
    取模型里用得多的那种写法（一样多取先出现的）。返回 {原写法: 统一写法}。"""
    names = [geo.usage(s) for s in geo.spaces]
    cnt = collections.Counter(names)
    groups = collections.defaultdict(list)
    for nm in dict.fromkeys(names):
        if "/" in nm:
            groups[frozenset(p.strip() for p in nm.split("/"))].append(nm)
    out = {}
    for spell in groups.values():
        best = max(spell, key=lambda s: (cnt[s], -spell.index(s)))
        out.update({s: best for s in spell})
    return out


def schedule_rows(f: ifcopenshell.file, geo: PlanGeometry | None = None) -> tuple[list, list, dict]:
    """从 IFC 统计门窗表。返回 (表头, 行, 统计信息)。要求已经 assign_marks（Tag 和类型都在）。
    电梯层门不进表，只计数（info["elevator_doors"]），表下脚注注明由电梯厂家配套（schedule_notes）。"""
    geo = geo or PlanGeometry(f)
    canon = _room_canon(geo)
    groups = collections.OrderedDict()
    lifts = []
    for el in list(f.by_type("IfcDoor")) + list(f.by_type("IfcWindow")):
        code = category(el, geo)
        if not code:
            lifts.append(el)
            continue
        mark = el.Tag or "（未编号）"
        groups.setdefault(mark, []).append((el, code))
    def order(mark):
        el, code = groups[mark][0]
        w, h, _ = size_mm(el, geo)
        return (list(CATEGORIES).index(code), _grade_key(fire_grade(el) if code == ROLE["fire"] else None), w, h, mark)
    head = ["类别", "编号", "洞口尺寸（宽×高）", "窗台高", "樘数", "开启方式", "所在位置", "备注"]
    rows = []
    n_default, codes, high, five, any_orig, orig_codes = 0, [], 0, False, False, set()
    for mark in sorted(groups, key=order):
        els = [e for e, _ in groups[mark]]
        el, code = groups[mark][0]
        w, h, _ = size_mm(el, geo)
        grade = fire_grade(el) if code == ROLE["fire"] else None
        cname = CATEGORIES[code][0] + (f"（{grade}级）" if grade else "")
        if code not in codes:
            codes.append(code)
        five = five or len(_dm(w)) > 2 or len(_dm(h)) > 2
        ops = collections.Counter(operation_text(e) for e in els)
        rooms = collections.Counter(canon.get(r, r) for r in (_primary_room(geo, e) for e in els))
        where = "、".join(f"{k}×{v}" if v > 1 else k for k, v in rooms.most_common())
        sills = []
        if el.is_a("IfcWindow"):
            sills = sorted({int(s / 10.0 + 0.5) * 10 for s in (geo.sill_mm(e) for e in els) if s is not None})
        notes = []
        h_default = any(size_mm(e, geo)[2] for e in els)
        s_default = bool(sills) and any(str(_jz(e).get("SillSource", "")) == "默认值" for e in els)
        if h_default or s_default:
            notes.append(("高度、窗台高" if h_default and s_default else "高度" if h_default else "窗台高") + "为默认值，待实测")
            n_default += 1
        if sills and max(sills) >= HIGH_SILL:
            notes.append("高窗" if min(sills) >= HIGH_SILL else f"窗台 ≥{HIGH_SILL:.0f} 的为高窗")
            high += 1
        # 原图编号：沿用为编号的不再重复写；代号不在本约定里的（如 FD、FH）注明含义待核；没采用的注明待核
        origs = sorted({orig_label(e, code, geo) for e in els} - {""})
        any_orig = any_orig or bool(origs)
        for p in sorted({markcode.parse(x)["prefix"] for x in origs} - set(CATEGORIES)):
            notes.append(f"原图代号 {p}，含义待核")
            orig_codes.add(p)
        unused = sorted({str(_jz(e).get("Label") or "") for e in els} - set(origs) - {""})
        if unused:
            notes.append("原图编号 " + "、".join(unused) + " 与类别或尺寸不符，未采用（待核）")
        notes += sorted({str(_jz(e).get("LabelNote") or "") for e in els} - {""})
        if code == ROLE["fire"] and not grade:
            notes.append("防火等级待定")
        rows.append([cname, mark, f"{round(w)}×{round(h)}", "、".join(str(s) for s in sills), len(els), "、".join(ops), where,
                     "；".join(notes)])
    storeys = []
    for st in f.by_type("IfcBuildingStorey"):
        cnt = uel.get_psets(st).get("JZ_Storey", {}).get("StoreyCount")
        if cnt:
            storeys.append(int(cnt))
    n_door = sum(len(v) for v in groups.values() if v[0][0].is_a("IfcDoor"))
    info = {"marks": len(rows), "doors": n_door, "windows": sum(len(v) for v in groups.values()) - n_door,
            "elevator_doors": len(lifts), "default_height_marks": n_default, "storey_count": max(storeys) if storeys else 1,
            "codes": codes, "high_window_marks": high, "five_digit": five, "orig_labels": any_orig,
            "orig_codes": sorted(orig_codes)}
    return head, rows, info


def schedule_notes(info: dict) -> list[str]:
    """门窗表下的脚注：只写表里看不出来、又必须知道的（电梯层门不在表里；一个标准层代表多层时樘数是单层的），
    没有就是空列表。类别、编号、尺寸、默认值和原图编号的情况逐行写在表里，不再另写编号规则。示例户型和通用出图流程共用。"""
    out = []
    if info.get("elevator_doors"):
        out.append(f"电梯层门 {info['elevator_doors']} 樘由电梯厂家配套，未列入本表。")
    if info.get("storey_count", 1) > 1:
        out.append(f"樘数为单层数量，本层代表 {info['storey_count']} 层。")
    return out
