"""地面：铺装区、排砖、门槛石和压条、数量（只依赖 shapely，在 .venv 里能跑）。

规则表 packs/cn/flooring/rules.json：材料（规格、缝宽、错缝、损耗率）、房间类型 → 缺省材料和湿区、排砖门槛、门槛石规则、湿区降板。
意图（可选）按房间改，不写坐标：
  {"rooms": [{"room": 房名或房间 id, "material": 材料键, "size": [长, 宽], "joint": 缝宽, "dir": 方向（度）,
              "start": "center" | "corner" | [x, y]}]}
排砖是确定性的换算（不搜索、不打分），见 packs/cn/flooring/README.md：
  - 铺装区：一间房一个区；两间房之间是不带门扇的洞口（layout.merge_kinds）、材料又相同时并成一个区通铺。
  - 方向：顺着铺装区多数的墙（墙长按方向对 90° 取模累加，总长最大的那组里最长的那面墙；main_dir），意图写了 dir 照意图。
  - 地砖对中：取铺装区里最大的矩形作基准（base_rect），两个方向各自在"缝对中""砖对中"里选边砖宽的那个（一样宽取缝对中），
    基准矩形两侧的边砖对称、不小于半块；基准矩形以外的部分顺着同一张网格铺，小于 layout.warn_edge 块的边砖记提醒。
  - 湿区有地漏：在边砖不小于 layout.drain_min_edge 块的前提下把网格平移到地漏落在一块砖正中；做不到保持对中，记提醒。
  - 木地板：长边顺着基准矩形的长向；横向同样对中；纵向第一排从一端整板起，逐排错开板长的 1/stagger。
  - 门槛石：门两侧有一侧是湿区、两侧材料不同、或入户门：门洞宽 × 墙厚一块；两侧干区同材料的门洞画一道压条。
输出：方案的 flooring = {zones, thresholds, strips}（毫米，图纸坐标）。线不存坐标：joint_lines(区, 轮廓) 从区的参数算出砖缝，
出图和别的用途都调它。"""
from __future__ import annotations

import json
import math
import os

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

from . import furnish, openings

PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "flooring"))
with open(os.path.join(PACK, "rules.json"), encoding="utf-8") as _fh:
    _R = json.load(_fh)
SOURCES, MATERIALS, ROOMS, LAYOUT, THRESHOLD, LEVELS, MODEL, CHECKS = (
    _R[k] for k in ("sources", "materials", "rooms", "layout", "threshold", "levels", "model", "checks"))
KEYS = ("room", "material", "size", "joint", "dir", "start")
DRAIN_KEY = "cn_floor_drain"
STONE = next(k for k, m in MATERIALS.items() if m["kind"] == "stone")
DIR_TOL = float(LAYOUT["dir_tol"])   # 铺装方向：边的方向差不到这么多度算同一组（main_dir）


class IntentError(ValueError):
    pass


def check_intent(intent) -> None:
    """整份意图的结构：对象，rooms 是对象列表。不对抛 IntentError（一行原因）。"""
    if not isinstance(intent, dict):
        raise IntentError("地面意图要是 JSON 对象")
    if not isinstance(intent.get("rooms"), list) or not all(isinstance(b, dict) for b in intent["rooms"]):
        raise IntentError("rooms 要是对象列表：[{\"room\": …, \"material\": …}]")


# ---------------------------------------------------------------- 网格（排砖的几何，区和吊顶的铝扣板共用）
def _r1(v):
    return round(float(v), 1)


def _rot(g, d):
    return affinity.rotate(g, d, origin=(0.0, 0.0)) if d else g


def _pt(x, y, d):
    """(x, y) 转 d 度（绕原点）。"""
    a = math.radians(d)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def main_dir(poly) -> float:
    """铺装方向（0～180 度，取整到 0.01）：各边按方向对 90° 取模归组（差不到 DIR_TOL 度算一组），边长累加最多的那组；组里取最长
    那条边的方向（一样长取角度小的）。一面斜墙再长也压不过另外几面正交墙的总长：顺着正交墙铺，不让三面墙都切斜砖。
    正交户型只有一组，就是最长那条边的方向（0 或 90）。"""
    ring = list(poly.simplify(1.0).exterior.coords)
    groups = []   # [总长, 组的代表角（对 90 取模）, 组里最长边 (长, 角)]
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        L = math.hypot(x1 - x0, y1 - y0)
        a = round(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0, 2) % 180.0
        g = next((g for g in groups if min(abs(a % 90.0 - g[1]), 90.0 - abs(a % 90.0 - g[1])) < DIR_TOL), None)
        if g is None:
            groups.append(g := [0.0, a % 90.0, None])
        g[0] += L
        if g[2] is None or L > g[2][0] + 0.5 or (abs(L - g[2][0]) <= 0.5 and a < g[2][1]):
            g[2] = (L, a)
    if not groups:
        return 0.0
    best = max(groups, key=lambda g: (round(g[0], 1), -g[1]))
    return best[2][1]


def base_rect(pf) -> tuple:
    """铺装区（已转到铺装方向的轮廓）里最大的轴向矩形 (x0, y0, x1, y1)：顶点坐标切成小格，整格在轮廓里的连成矩形取面积最大的
    （一样大取左下的）。格子太多（斜墙很多的轮廓）时退回外包。"""
    xs = sorted({_r1(x) for g in [pf.exterior] + list(pf.interiors) for x, _ in g.coords})
    ys = sorted({_r1(y) for g in [pf.exterior] + list(pf.interiors) for _, y in g.coords})
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx < 1 or ny < 1 or nx * ny > 4000:
        return pf.bounds
    pp = prep(pf.buffer(0.5, join_style="mitre"))
    full = [[pp.contains(box(xs[i], ys[j], xs[i + 1], ys[j + 1])) for j in range(ny)] for i in range(nx)]
    best = (0.0,) + tuple(pf.bounds)
    for i0 in range(nx):
        ok = [True] * ny
        for i1 in range(i0, nx):
            ok = [a and b for a, b in zip(ok, full[i1])]
            if not any(ok):
                break
            w = xs[i1 + 1] - xs[i0]
            j = 0
            while j < ny:
                if not ok[j]:
                    j += 1
                    continue
                k = j
                while k < ny and ok[k]:
                    k += 1
                if w * (ys[k] - ys[j]) > best[0] + 1e-6:
                    best = (w * (ys[k] - ys[j]), xs[i0], ys[j], xs[i1 + 1], ys[k])
                j = k
    return best[1:]


def _edge(o, lo, pitch):
    """lo 这一头的边砖（含半条缝，毫米）：lo 到它里面第一条缝的距离；正好是整砖时算整块（pitch）。"""
    r = (o - lo) % pitch
    return pitch if r < 0.5 or pitch - r < 0.5 else r


def _axis(lo, hi, pitch):
    """一个方向的对中：缝对中（中线上是缝）和砖对中（中线上是砖中）里选边砖宽的（一样宽取缝对中）。返回 (一条缝的位置, 写法)。"""
    c = (lo + hi) / 2
    ej, et = _edge(c, lo, pitch), _edge(c + pitch / 2, lo, pitch)
    return (c + pitch / 2, "砖对中") if et > ej + 0.5 else (c, "缝对中")


def _piece(e, pitch, size, joint):
    """边砖的实际宽（毫米）：整块写规格，否则扣掉半条缝。"""
    return float(size) if e >= pitch - 0.5 else _r1(max(e - joint / 2.0, 0.0))


def plan_grid(poly, mat, d=None, start="center", drains=()):
    """一块铺装区的排法：poly 轮廓（毫米），mat 材料（kind、size、joint、stagger），d 方向（度，缺省最长墙），start："center" 对中、
    "corner" 基准矩形左下角整砖起、[x, y] 指定起铺点（缝交点或整板起点），drains 地漏 [(x, y)]（只在对中时用）。
    返回 (区的排法 dict, 提醒列表)：dir、origin（一个缝交点；木地板是第一排整板起点所在的那条长缝上）、pitch、stagger、
    start、start_kind、edges（基准矩形两头的边砖宽：u 沿方向、v 垂直方向）、base（基准矩形四角，图纸坐标）。"""
    notes = []
    d = main_dir(poly) if d is None else float(d) % 180.0
    pf = _rot(poly, -d)
    rect = base_rect(pf)
    a0, b0, a1, b1 = rect
    su, sv = (float(v) for v in mat["size"])
    j = float(mat.get("joint") or 0.0)
    if mat["kind"] == "wood":
        if b1 - b0 > a1 - a0 + 0.5:   # 长边顺着基准矩形的长向
            d = (d + 90.0) % 180.0
            pf = _rot(poly, -d)
            a0, b0, a1, b1 = rect = base_rect(pf)
        pu, pv = su + j, sv + j
        if isinstance(start, (list, tuple)):
            ou, ov = _rot(Point(start), -d).coords[0]
            kv = "指定"
        elif start == "corner":
            ou, ov, kv = a0, b0, "整板起点"
        else:
            ov, kv = _axis(b0, b1, pv)
            ou = a0
            k0 = math.floor((b0 - ov) / pv + 1e-9)   # 第一排（靠 b0 这面墙的那排）
            ov = ov + k0 * pv
            kv = "整板起点"
        sx, sy = (ou, ov + pv) if not isinstance(start, (list, tuple)) and start != "corner" else (ou, ov)
        if not isinstance(start, (list, tuple)) and start != "corner" and ov + pv - b0 >= pv - 0.5:   # 第一排是整板（没有收边）：起铺点就在墙角
            sx, sy = ou, b0
        edges = {"v": [_piece(_edge(ov, b0, pv), pv, sv, j), _piece(_edge(-ov, -b1, pv), pv, sv, j)]}   # 板宽方向两头的收边
        stagger = int(mat.get("stagger") or 1)
    else:
        pu, pv = su + j, sv + j
        stagger = 0
        ku = kv = "缝对中"
        if isinstance(start, (list, tuple)):
            ou, ov = _rot(Point(start), -d).coords[0]
            sx, sy, kind = ou, ov, "指定"
        elif start == "corner":
            ou, ov = a0, b0
            sx, sy, kind = a0, b0, "整砖起点"
        else:
            ou, ku = _axis(a0, a1, pu)
            ov, kv = _axis(b0, b1, pv)
            sx, sy = (a0 + a1) / 2, (b0 + b1) / 2
            kind = {("缝对中", "缝对中"): "缝交点", ("砖对中", "砖对中"): "砖中心"}.get((ku, kv), "砖边中点")
            lim = LAYOUT["drain_min_edge"]
            for k, (dx, dy) in enumerate(drains):
                du, dv = _rot(Point(dx, dy), -d).coords[0]
                if not pf.buffer(1.0).contains(Point(du, dv)):
                    continue
                cu, cv = du - pu / 2, dv - pv / 2
                if min(_edge(cu, a0, pu), _edge(-cu, -a1, pu)) >= lim * pu - 0.5 and \
                        min(_edge(cv, b0, pv), _edge(-cv, -b1, pv)) >= lim * pv - 0.5:
                    ou, ov, sx, sy, kind = cu, cv, du, dv, "地漏砖中心"
                    break
            else:
                if drains:
                    notes.append(f"地漏落不到砖正中（边砖会小于 {_frac(lim)} 块），保持对中")
        edges = {"u": [_piece(_edge(ou, a0, pu), pu, su, j), _piece(_edge(-ou, -a1, pu), pu, su, j)],
                 "v": [_piece(_edge(ov, b0, pv), pv, sv, j), _piece(_edge(-ov, -b1, pv), pv, sv, j)]}
        kv = kind
    ox, oy = _pt(ou, ov, d)
    stx, sty = _pt(sx, sy, d)
    corners = [_pt(x, y, d) for x, y in ((a0, b0), (a1, b0), (a1, b1), (a0, b1))]
    g = {"dir": _r1(d), "origin": [_r1(ox), _r1(oy)], "pitch": [_r1(pu), _r1(pv)], "stagger": stagger,
         "start": [_r1(stx), _r1(sty)], "start_kind": kv, "edges": edges, "base": [[_r1(x), _r1(y)] for x, y in corners]}
    return g, notes


def _frac(v):
    return {0.5: "1/2", 0.3333: "1/3", 0.25: "1/4"}.get(round(v, 4), f"{v:g}")


def _rows(spec, pf):
    """木地板每排：[(排号, v 下, v 上, 这一排端缝的错开量)]，覆盖 pf 的范围。"""
    ou, ov = _rot(Point(spec["origin"]), -spec["dir"]).coords[0]
    pu, pv = spec["pitch"]
    n = max(int(spec.get("stagger") or 1), 1)
    _, y0, _, y1 = pf.bounds
    out = []
    for k in range(math.floor((y0 - ov) / pv) - 1, math.ceil((y1 - ov) / pv) + 1):
        out.append((k, ov + k * pv, ov + (k + 1) * pv, ((k % n) / n) * pu))
    return out, ou


def cells(spec, poly):
    """铺装区按网格切成一块块（毫米）：[(在区里的那部分, 整格面积, 沿方向宽, 垂直方向宽)]；格是砖加半条缝（木地板是一块板），
    整个平面不重不漏，所以各块面积加起来就是区的面积。"""
    d = spec["dir"]
    pf = _rot(poly, -d)
    pu, pv = spec["pitch"]
    x0, y0, x1, y1 = pf.bounds
    out = []
    if spec.get("stagger"):
        rows, ou = _rows(spec, pf)
        for k, lo, hi, off in rows:
            start = ou + off
            for m in range(math.floor((x0 - start) / pu) - 1, math.ceil((x1 - start) / pu) + 1):
                c = box(start + m * pu, lo, start + (m + 1) * pu, hi)
                out.append(c)
    else:
        ou, ov = _rot(Point(spec["origin"]), -d).coords[0]
        for i in range(math.floor((x0 - ou) / pu) - 1, math.ceil((x1 - ou) / pu) + 1):
            for k in range(math.floor((y0 - ov) / pv) - 1, math.ceil((y1 - ov) / pv) + 1):
                out.append(box(ou + i * pu, ov + k * pv, ou + (i + 1) * pu, ov + (k + 1) * pv))
    pp = prep(pf)
    res = []
    for c in out:
        if not pp.intersects(c):
            continue
        p = c.intersection(pf)
        if p.area < 1.0:
            continue
        bx0, by0, bx1, by1 = p.bounds
        res.append((p, c.area, bx1 - bx0, by1 - by0))
    return res


def joint_lines(spec, poly) -> list:
    """砖缝、木地板缝（毫米，和 poly 同一坐标系）：按区的参数（dir、origin、pitch、stagger）算出、裁到轮廓里；和墙重合的不要。
    出图（annotate.floor_joints）、吊顶铝扣板网格（annotate.alu_grid）都用它。"""
    d = spec["dir"]
    pf = _rot(poly, -d)
    pu, pv = spec["pitch"]
    x0, y0, x1, y1 = pf.bounds
    lines = []
    if spec.get("stagger"):
        rows, ou = _rows(spec, pf)
        for k, lo, hi, off in rows:
            lines.append(LineString([(x0 - 1, lo), (x1 + 1, lo)]))
            start = ou + off
            for m in range(math.floor((x0 - start) / pu), math.ceil((x1 - start) / pu) + 1):
                u = start + m * pu
                lines.append(LineString([(u, lo), (u, hi)]))
    else:
        ou, ov = _rot(Point(spec["origin"]), -d).coords[0]
        for i in range(math.floor((x0 - ou) / pu), math.ceil((x1 - ou) / pu) + 1):
            lines.append(LineString([(ou + i * pu, y0 - 1), (ou + i * pu, y1 + 1)]))
        for k in range(math.floor((y0 - ov) / pv), math.ceil((y1 - ov) / pv) + 1):
            lines.append(LineString([(x0 - 1, ov + k * pv), (x1 + 1, ov + k * pv)]))
    edge = pf.boundary.buffer(0.5)
    out = []
    for ln in lines:
        g = ln.intersection(pf)
        for s in getattr(g, "geoms", [g]):
            if s.geom_type == "LineString" and s.length > 1.0 and not s.within(edge):
                out.append(_rot(s, d))
    return out


def quantities(spec, poly, mat):
    """数量：面积（㎡）、整块、切块、含损耗的合计（地砖按块、木地板按 ㎡），和覆盖面积对不对得上；小于 warn_edge 块的边砖。"""
    pcs = cells(spec, poly)
    su, sv = mat["size"]
    pu, pv = spec["pitch"]
    whole = cut = 0
    cover = 0.0
    thin = []
    warn = LAYOUT["warn_edge"]
    for p, full, w, h in pcs:
        if p.area >= full - 1.0:
            whole += 1
            cover += full
        else:
            cut += 1
            cover += p.area
            # 切过的那个方向窄于 warn_edge 块的算小边砖（2mm 以下是轮廓的数值误差，不算；木地板只看板宽方向，板头错缝本来就长短不一）
            dims = ((h, pv),) if spec.get("stagger") else ((w, pu), (h, pv))
            short = [x for x, P in dims if 2.0 <= x < P - 0.5 and x < warn * P - 0.5]
            if short:
                thin.append(min(short))
    area = poly.area
    waste = float(mat.get("waste") or 0.0)
    q = {"area_m2": round(area / 1e6, 2), "whole": whole, "cut": cut, "waste": waste,
         "total": round(area / 1e6 * (1 + waste), 2) if mat["kind"] == "wood" else int(math.ceil((whole + cut) * (1 + waste) - 1e-9)),
         "unit": "㎡" if mat["kind"] == "wood" else "块", "cover_diff_mm2": round(abs(cover - area), 1)}
    return q, thin


# ---------------------------------------------------------------- 方案
def _mat(key, over):
    m = dict(MATERIALS[key], key=key)
    if over.get("size"):
        m["size"] = [float(v) for v in over["size"]]
    if over.get("joint") is not None:
        m["joint"] = float(over["joint"])
    m["size"] = [float(v) for v in m["size"]]
    m["joint"] = float(m.get("joint") or 0.0)
    return m


def mat_id(m) -> tuple:
    return (list(MATERIALS).index(m["key"]), tuple(m["size"]), m["joint"], m["key"])


def label(m) -> str:
    """材料在图上、表里的写法："地砖 800×800"。"""
    return f"{m['name']} {m['size'][0]:g}×{m['size'][1]:g}" if m.get("size") else m["name"]


def _intent_spec(blk, rm, errors):
    spec = {}
    err = lambda msg: errors.append({"room": rm["name"], "room_id": rm["id"], "check": "intent", "msg": f"{rm['name']}：{msg}"})
    bad = sorted(set(blk) - set(KEYS))
    if bad:
        err(f"不认识的键 {'、'.join(bad)}")
    if "material" in blk:
        if blk["material"] in MATERIALS and MATERIALS[blk["material"]]["kind"] != "stone":
            spec["material"] = blk["material"]
        else:
            err(f"没有材料 {blk['material']}（可用：{'、'.join(k for k, m in MATERIALS.items() if m['kind'] != 'stone')}）")
    num = lambda v, lo, hi: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
    if "size" in blk:
        v = blk["size"]
        if isinstance(v, list) and len(v) == 2 and all(num(x, 50, 3000) for x in v):
            spec["size"] = v
        else:
            err("size 要写 [长, 宽]（50～3000 毫米）")
    if "joint" in blk:
        if num(blk["joint"], 0, 20):
            spec["joint"] = blk["joint"]
        else:
            err("joint 要是 0～20 的数（毫米）")
    if "dir" in blk:
        if num(blk["dir"], 0, 360):
            spec["dir"] = float(blk["dir"])
        else:
            err("dir 要是 0～360 的数（度）")
    if "start" in blk:
        v = blk["start"]
        if v in ("center", "corner"):
            spec["start"] = v
        elif isinstance(v, list) and len(v) == 2 and all(num(x, -1e9, 1e9) for x in v):
            if Polygon([w["a"] for w in rm["walls"]]).buffer(1.0).contains(Point(v)):
                spec["start"] = [float(x) for x in v]
            else:
                err(f"起铺点 {v} 不在房间里")
        else:
            err("start 要写 \"center\"、\"corner\" 或 [x, y]")
    return spec


def _door_box(o):
    c, u, n, w, t, _ = openings.rec_frame(o)
    return openings.oriented_box(c[0], c[1], u[0], u[1], w / 2, t / 2), c, u, w, t


def _poly_coords(g):
    return [[_r1(x), _r1(y)] for x, y in g.exterior.coords[:-1]], [[[_r1(x), _r1(y)] for x, y in r.coords[:-1]] for r in g.interiors]


def zone_poly(z) -> Polygon:
    return Polygon(z["polygon"], z.get("holes") or [])


def material_of(z) -> dict:
    """区里存的材料 → 材料 dict（规则表的那一条，规格、缝宽按区里写的）。"""
    return dict(MATERIALS[z["material"]], key=z["material"], size=z["size"], joint=z["joint"])


def _passages(geo, todo_ids):
    """门洞（不是窗）：[(洞口, 两侧房间 id)]。"""
    return [(o, [r for r in (o.get("rooms") or [])]) for o in geo["openings"] if o.get("kind") != "window" and not o.get("polygon")
            and set(o.get("rooms") or []) & todo_ids]


def flooring(rec: dict, sch: dict, intent: dict | None = None, units=None) -> tuple[dict, dict]:
    """识别 + 方案 [+ 意图] → (带地面的方案, 校验报告)。只做户内房间（公共空间不做）；units：只重做这几户，别的户以前生成的
    铺装区、门槛石、压条原样留着。意图写错的只报那一条（check=intent），别的照做；户号不在识别结果里抛 ValueError。"""
    from .unitview import unit_ids
    geo = sch.get("after") or rec
    if units and (bad := [u for u in units if u not in unit_ids(rec, sch)]):
        raise ValueError(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unit_ids(rec, sch)) or '无'}）")
    if intent is not None:
        check_intent(intent)
    unit = {r["id"]: r.get("unit") or "" for r in geo["rooms"]}
    public = {r["id"] for r in geo["rooms"] if r.get("public")}
    rooms = furnish.rooms_of(rec, sch)
    byid = {r["id"]: r for r in rooms}
    errors, viol, report_rooms = [], [], []
    given = {}
    for blk in (intent or {}).get("rooms", []):
        rm = furnish.find_room(rooms, blk.get("room"))
        if isinstance(rm, str):
            errors.append({"room": str(blk.get("room")), "room_id": "", "check": "intent", "msg": rm})
            continue
        if units and unit.get(rm["id"]) not in units:
            continue
        given[rm["id"]] = blk
    old = sch.get("flooring") or {}
    redo = lambda rid: unit.get(rid) and rid not in public and (not units or unit[rid] in units)
    keep_z = [z for z in old.get("zones", []) if units and not any(redo(r) for r in z["rooms"])]
    todo = [rm for rm in rooms if redo(rm["id"])]
    ids = {rm["id"] for rm in todo}
    keep_t = [t for t in old.get("thresholds", []) if units and not set(t["rooms"]) & ids]
    keep_s = [s for s in old.get("strips", []) if units and not set(s["rooms"]) & ids]
    spec = {}
    for rm in todo:
        rule = ROOMS.get(rm["type"], ROOMS.get("other"))
        sp = dict(rule or {})
        src = "规则"
        if rm["id"] in given:
            sp.update(_intent_spec(given[rm["id"]], rm, errors))
            src = "意图"
        if sp.get("material") not in MATERIALS:
            errors.append({"room": rm["name"], "room_id": rm["id"], "check": "no_material", "msg": f"{rm['name']}：没分到材料"})
            continue
        sp["mat"] = _mat(sp["material"], sp)
        sp["src"] = src
        spec[rm["id"]] = sp
    # 并区：不带门扇的洞口两侧材料相同
    parent = {rid: rid for rid in spec}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    order = {rm["id"]: i for i, rm in enumerate(rooms)}
    psg = _passages(geo, set(spec))
    for o, rs in psg:
        if o.get("kind") in LAYOUT["merge_kinds"] and len(rs) == 2 and all(r in spec for r in rs) \
                and mat_id(spec[rs[0]]["mat"]) == mat_id(spec[rs[1]]["mat"]) \
                and bool(spec[rs[0]].get("wet")) == bool(spec[rs[1]].get("wet")):   # 湿区和干区不并（标高不同、门口要放门槛石）
            a, b = sorted((find(rs[0]), find(rs[1])), key=order.get)
            parent[b] = a
    groups = {}
    for rid in sorted(spec, key=order.get):
        groups.setdefault(find(rid), []).append(rid)
    drains = sorted((it for it in sch.get("items", []) if it.get("key") == DRAIN_KEY), key=lambda it: it.get("plan_id") or "")
    zones = []
    for root, rids in groups.items():
        parts = [Polygon([w["a"] for w in byid[r]["walls"]]).buffer(0) for r in rids]
        parts += [_door_box(o)[0] for o, rs in psg if o.get("kind") in LAYOUT["merge_kinds"] and set(rs) <= set(rids) and len(rs) == 2]
        P = unary_union(parts).buffer(0.5, join_style="mitre").buffer(-0.5, join_style="mitre")
        if P.geom_type != "Polygon":
            P = max(P.geoms, key=lambda g: g.area)
        sp = next((spec[r] for r in rids if spec[r]["src"] == "意图"), spec[rids[0]])
        m = sp["mat"]
        wet = any(spec[r].get("wet") for r in rids)
        dr = [(it["x"], it["y"]) for it in drains if it.get("room") in rids] if wet else []
        g, notes = plan_grid(P, m, sp.get("dir"), sp.get("start", "center"), dr if sp.get("start", "center") == "center" else ())
        if not dr and any(spec[r].get("need_drain") for r in rids):
            notes.append("湿区没有地漏（水电布点里没有），按对中排")
        q, thin = quantities(g, P, m)
        if thin:
            notes.append(f"小于 {_frac(LAYOUT['warn_edge'])} 块的边砖 {len(thin)} 块（最窄 {min(thin):.0f}）")
        if q["cover_diff_mm2"] > CHECKS["area_tol_mm2"]:
            viol.append({"room": byid[rids[0]]["name"], "room_id": rids[0], "check": "area",
                         "msg": f"{byid[rids[0]]['name']}：整块 + 切块覆盖的面积和铺装区差 {q['cover_diff_mm2']:.0f}mm²"})
        ext, holes = _poly_coords(P)
        z = {"id": f"FZ-{rids[0]}", "rooms": rids, "unit": unit.get(rids[0], ""), "material": m["key"], "size": m["size"], "joint": m["joint"],
             "wet": wet, "level": -float(LEVELS["wet_drop"]) if wet else 0.0, "polygon": ext, "holes": holes, **g, "quantity": q, "notes": notes,
             "drains": [[_r1(x), _r1(y)] for x, y in dr],
             "source": sp["src"]}
        zones.append(z)
        for r in rids:
            report_rooms.append({"room": byid[r]["name"], "room_id": r, "zone": z["id"], "material": label(m), "source": spec[r]["src"]})
    if not spec and not any(e["check"] == "no_material" for e in errors):
        errors.append({"room": "", "room_id": "", "check": "no_rooms",
                       "msg": "一间房都没做地面：识别结果里的房间都没有户号（unit）或都是公共空间（public）；先给户内房间写上 unit"})
    allz = keep_z + zones
    # 门槛石、压条
    zr = {r: z for z in allz for r in z["rooms"]}
    th, st = [], []
    new = {id(z) for z in zones}
    for o, rs in psg:
        zs = [zr.get(r) for r in rs]
        if not any(id(z) in new for z in zs if z is not None):
            continue
        why, inside = _reason(o, zs)
        if why is None:
            continue
        bx, c, u, w, t = _door_box(o)
        if why:
            ext, _ = _poly_coords(bx)
            th.append({"id": f"TS-{o['id']}", "door": o["id"], "rooms": rs, "unit": next((z["unit"] for z in inside), ""), "polygon": ext,
                       "width": _r1(w), "depth": _r1(t), "reason": "、".join(why)})
        elif len(inside) == 2:
            a, b = c - u * w / 2, c + u * w / 2
            st.append({"id": f"ST-{o['id']}", "door": o["id"], "rooms": rs, "unit": inside[0]["unit"], "line": [[_r1(a[0]), _r1(a[1])], [_r1(b[0]), _r1(b[1])]]})
    thr = sorted(keep_t + th, key=lambda t: t["id"])
    strips = sorted(keep_s + st, key=lambda s: s["id"])
    # 材料编号：按规则表里材料的顺序、规格、缝宽排，F-01、F-02…；门槛石排在最后
    mids = sorted({mat_id(material_of(z)) for z in allz})
    code = {k: f"F-{i + 1:02d}" for i, k in enumerate(mids)}
    for z in allz:
        z["code"] = code[mat_id(material_of(z))]
    stone_code = f"F-{len(mids) + 1:02d}" if thr else None
    for t in thr:
        t["code"] = stone_code
    allz = sorted(allz, key=lambda z: (order.get(z["rooms"][0], 1e9), z["id"]))
    out = dict(sch, flooring={"zones": allz, "thresholds": thr, "strips": strips})
    viol += check(geo, out)
    counts = {}
    for v in viol:
        counts[v["check"]] = counts.get(v["check"], 0) + 1
    report_zones = [{"zone": z["id"], "rooms": z["rooms"], "code": z["code"], "material": label(material_of(z)), "dir": z["dir"],
                     "start_kind": z["start_kind"], "edges": z["edges"], **{k: z["quantity"][k] for k in ("area_m2", "whole", "cut", "total", "unit")},
                     "notes": z["notes"]} for z in zones]
    for e in errors:
        counts[e["check"]] = counts.get(e["check"], 0) + 1
    return out, {"ok": not errors and not viol, "zones": len(zones), "thresholds": len(th), "strips": len(st), "rooms": report_rooms,
                 "layout": report_zones, "notes": [f"{z['id']}：{n}" for z in zones for n in z["notes"]], "errors": errors,
                 "violations": viol, "counts": counts}


def _reason(o, zs):
    """一个门洞两侧的区 zs（不在任何区里的一侧是 None）→ (放门槛石的原因列表，空列表是画压条；None 是都不放, 两侧的区)。
    同一个区里的洞口（通铺）不放。入户：门上记了入户（按户出图裁的时候认的）、只有一侧有房间、或另一侧不在任何区（公共部分）。"""
    inside = [z for z in zs if z is not None]
    if not inside or (len(zs) == 2 and zs[0] is not None and zs[0] is zs[1]):
        return None, inside
    entrance = bool(o.get("entrance")) or len(zs) == 1 or len(inside) < len(zs)
    wet = any(z["wet"] for z in inside)
    key = lambda z: (z["material"], list(z["size"]), z["joint"])
    diff = len(inside) == 2 and key(inside[0]) != key(inside[1])
    why = [w for w, on in (("入户", entrance and THRESHOLD["entrance"]), ("湿区", wet and THRESHOLD["wet"]),
                           ("材料不同", diff and THRESHOLD["material_change"])) if on]
    return (why if why or len(inside) == 2 else None), inside


def required(geo, fl) -> dict:
    """按区和门洞重算：每个要放门槛石的门 → 门洞矩形。"""
    zr = {r: z for z in fl.get("zones", []) for r in z["rooms"]}
    out = {}
    for o in geo["openings"]:
        if o.get("kind") == "window" or o.get("polygon"):
            continue
        why, _ = _reason(o, [zr.get(r) for r in o.get("rooms") or []])
        if why:
            out[o["id"]] = _door_box(o)[0]
    return out


def check(geo, sch) -> list:
    """方案里的地面数据和现在的门、地漏对不对得上（出图入口调）：门槛石按现在的门重算（该放的都有、位置和门洞一致、
    没有多的；这和生成时是同一条规则，只能查出数据过时或被手改，查不出规则本身错）；湿区排砖时用的地漏和方案里现在的地漏相同
    （水电在地面之后重跑过就对不上）。返回违规列表，每条写明该重跑哪条命令。"""
    fl = sch.get("flooring") or {}
    want = required(geo, fl)
    have = {t["door"]: Polygon(t["polygon"]) for t in fl.get("thresholds", [])}
    viol = []
    redo = "：重跑 scripts/flooring.py"
    for d in sorted(set(want) | set(have)):
        if d not in have:
            viol.append({"room": "", "room_id": "", "check": "threshold", "msg": f"门 {d} 该放门槛石，没有{redo}"})
        elif d not in want:
            viol.append({"room": "", "room_id": "", "check": "threshold", "msg": f"门 {d} 不该放门槛石{redo}"})
        elif have[d].symmetric_difference(want[d]).area > 1e3:
            viol.append({"room": "", "room_id": "", "check": "threshold", "msg": f"门 {d} 的门槛石和门洞对不上{redo}"})
    drains = [(it["x"], it["y"], it.get("room")) for it in sch.get("items", []) if it.get("key") == DRAIN_KEY]
    for z in fl.get("zones", []):
        if z.get("wet") and "drains" in z:
            now = sorted([_r1(x), _r1(y)] for x, y, r in drains if r in z["rooms"])
            if now != sorted(z["drains"]):
                viol.append({"room": "", "room_id": z["rooms"][0], "check": "drains",
                             "msg": f"{z['id']} 的地漏变了（排砖时 {len(z['drains'])} 个，现在 {len(now)} 个或位置不同）{redo}"})
    return viol