"""从天正 T3 风格的 DXF 平面里识别：房间（闭合多边形 + 名称 + 净面积 + 所属户）、墙段（厚度、保留/可拆）、门、窗（是否外窗）、标高、本层建筑面积。

思路（不追求通用，先吃透天正导出图的规律）：
  1. 取 WALL 层的线、COLUMN 层柱子轮廓、WINDOW 层门窗块（窗展开成线；门只取"洞口中线"封口）、WINDOW 层的散线（洞口线、推拉门线）；
  2. shapely 打断成网 → polygonize 得到所有闭合面；
  3. 面的分类：含房间名文字 = 房间；边界大部分落在 WALL 线上的细长面/小块 = 墙；面积≥1㎡ 的其余面 = 未标注空间；剩下的是窗框条、门洞条等碎面；
  4. 全部面合并后的外轮廓 = 建筑外边，洞口中心离外边不超过半个墙厚 = 外窗/外门；
  5. 分户：从每个"X 户型"文字所在的面出发，穿过非墙边界（门洞中线、窗线）做广度搜索，碰到公共空间（前室、通道、电梯…）就停。
门窗块规律（天正）：插入点 = 洞口中心（在墙中线上），块 X 轴沿墙，|X 比例| = 洞宽，|Y 比例| = 墙厚（窗）。
门窗编号（WINDOW_TEXT 层）：一个编号常被拆成同一基线上的几段 TEXT（"FM" "丙" "1221"），先拼回完整编号，
再和门窗一对一配对；编号里的宽度位和识别出的洞宽差 1dm 以上的不采用（label 留空，原文留在 label_raw）。
只依赖 ezdxf + shapely，在项目 venv 里跑。所有坐标单位 mm（与图纸一致）。
命令行：python -m jiazhuang.dxfplan <平面.dxf> <识别.json> [检查图.svg] --region XMIN YMIN XMAX YMAX（选项见 --help）
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, asdict

import ezdxf
from ezdxf import bbox, path as dpath
from shapely import STRtree, set_precision
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from . import markcode, printing

U = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
ROOM_WORDS = ["主卧", "次卧", "卧室", "书房", "客厅", "起居室", "餐厅", "厨房", "主卫", "公卫", "卫生间", "阳台",
              "前室", "通道", "走道", "门厅", "储藏", "设备", "机房", "电梯", "楼梯"]
SHAFT_WORDS = {"电", "水", "暖", "风", "送", "排", "强电", "弱电", "水井", "风井", "管井"}
PUBLIC_WORDS = ("前室", "通道", "走道", "门厅", "电梯", "楼梯", "设备", "机房", "管井")


def clean(s):
    s = U.sub(lambda m: chr(int(m.group(1), 16)), s or "")
    return re.sub(r"\\[A-Za-z][^;\\]*;|[{}]|\\P", " ", s).strip()


@dataclass
class Opening:
    id: str
    kind: str          # door / window / opening
    block: str
    x: float
    y: float
    rot: float
    width: float
    depth: float = 0.0  # 窗块 |Y 比例| ≈ 墙厚
    label: str = ""     # 采用的原图编号（宽度位与洞宽相差 ≤1dm）；没有或对不上为空
    label_raw: str = "" # 配到这樘门窗的原图编号原文（label 为空而这里有值 = 与识别尺寸不符，未采用）
    exterior: bool = False
    rooms: list = field(default_factory=list)
    polygon: list = field(default_factory=list)   # 散线窗（凸窗）的外包轮廓
    sx: int = 1           # 块 X 比例的符号（-1 = 左右镜像，门的开启方向用）
    sy: int = 1           # 块 Y 比例的符号（-1 = 开向另一侧）
    wall_t: float = 0.0   # 洞口所在墙的厚度（取附近墙段）
    leafs: int = -1       # 门：块里开启弧的个数（0 = 推拉门，1 = 单扇，2 = 双扇）；窗为 -1
    swing: int = 1        # 门：开向墙法线 n=(-sin,cos) 的哪一侧（+1/-1），取自开启弧
    hinge_end: int = 0    # 单扇门：铰链在洞口沿墙方向 u=(cos,sin) 的哪一端（-1/+1）


def door_swing(e, ins, rot):
    """天正门块：开启弧的圆心 = 铰链，弧离墙最远的端点 = 门扇打开后的位置。返回 (弧数, 开向, 铰链端)。
    镜像块的弧在 OCS 里（拉伸方向 0,0,-1），圆心要先转成 WCS。"""
    a = math.radians(rot)
    u, n = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))
    arcs = [v for v in e.virtual_entities() if v.dxftype() == "ARC"]
    if not arcs:
        return 0, 1, 0
    sides, ends = [], []
    for arc in arcs:
        c = arc.ocs().to_wcs(arc.dxf.center)
        pts = [arc.start_point, arc.end_point]
        leaf = max(pts, key=lambda p: abs((p.x - ins.x) * n[0] + (p.y - ins.y) * n[1]))
        sides.append(1 if (leaf.x - ins.x) * n[0] + (leaf.y - ins.y) * n[1] > 0 else -1)
        ends.append(1 if (c.x - ins.x) * u[0] + (c.y - ins.y) * u[1] > 0 else -1)
    return len(arcs), sides[0], ends[0] if len(arcs) == 1 else 0


def read_axes(msp, region, pad=9000.0):
    """轴网：DOTE 层的长直线 = 轴线；_AXISO 块的属性 = 轴号（天正把挤在一起的轴号上下/左右错开放）。
    轴号按"保序的最近匹配"对到轴线上（允许 600mm 的错位）。返回 {"x": [...], "y": [...]}，坐标为图纸毫米。"""
    x0, y0, x1, y1 = region[0] - pad, region[1] - pad, region[2] + pad, region[3] + pad
    vx, hy = {}, {}
    for e in msp.query("LINE[layer=='DOTE']"):
        p, q = e.dxf.start, e.dxf.end
        if not (x0 <= min(p.x, q.x) and max(p.x, q.x) <= x1 and y0 <= min(p.y, q.y) and max(p.y, q.y) <= y1):
            continue
        if abs(p.x - q.x) < 1 and abs(p.y - q.y) > 3000:
            k = round(p.x); vx[k] = (min(vx.get(k, (1e18, -1e18))[0], p.y, q.y), max(vx.get(k, (1e18, -1e18))[1], p.y, q.y))
        elif abs(p.y - q.y) < 1 and abs(p.x - q.x) > 3000:
            k = round(p.y); hy[k] = (min(hy.get(k, (1e18, -1e18))[0], p.x, q.x), max(hy.get(k, (1e18, -1e18))[1], p.x, q.x))
    bub = collections.defaultdict(list)   # 轴号 → [(x, y)]
    for e in msp.query("INSERT[name=='_AXISO']"):
        p = e.dxf.insert
        if x0 <= p.x <= x1 and y0 <= p.y <= y1:
            for a in e.attribs:
                lab = clean(a.dxf.text)
                if lab:
                    bub[lab].append((p.x, p.y))
    out = {"x": [], "y": []}
    for key, lines, coord in (("x", vx, 0), ("y", hy, 1)):
        is_num = (lambda s: s.isdigit()) if key == "x" else (lambda s: not s.isdigit())
        labs = sorted(((sum(p[coord] for p in v) / len(v), lab, v) for lab, v in bub.items() if is_num(lab)))
        pos = sorted(lines)
        j = 0
        for m, lab, pts in labs:
            cand = [(abs(pos[k] - m), k) for k in range(j, len(pos)) if abs(pos[k] - m) <= 600]
            if not cand:
                continue
            _, k = min(cand)
            j = k + 1
            other = 1 - coord
            mid = (lines[pos[k]][0] + lines[pos[k]][1]) / 2
            ends = sorted({("bottom" if p[other] < mid else "top") if key == "x" else ("left" if p[other] < mid else "right") for p in pts})
            out[key].append({"label": lab, "pos": float(pos[k]), "span": [round(lines[pos[k]][0]), round(lines[pos[k]][1])], "ends": ends})
    return out


@dataclass
class Room:
    id: str
    name: str            # 多个名称用 / 连接，如 "客厅/餐厅"
    names: list
    area_m2: float
    polygon: list
    unit: str = ""       # 户编号 U1..U8；公共空间为 ""
    unit_type: str = ""  # 户型字母 A/B/C
    public: bool = False
    labeled: bool = True
    windows: list = field(default_factory=list)       # 相邻窗编号
    ext_windows: list = field(default_factory=list)   # 相邻外窗编号
    doors: list = field(default_factory=list)


@dataclass
class WallSeg:
    id: str
    thickness: float
    length: float
    polygon: list
    exterior: bool
    keep: bool
    reason: str


def _segments(entity, tol=20.0):
    """任意曲线实体 → 折线点列（WCS），处理块镜像产生的 OCS。"""
    try:
        p = dpath.make_path(entity)
    except Exception:
        return []
    pts = [(v.x, v.y) for v in p.flattening(tol)]
    return [pts] if len(pts) >= 2 else []


def _inside(e, reg):
    try:
        b = bbox.extents([e], fast=True)
    except Exception:
        return False
    x0, y0, x1, y1 = reg
    return b.has_data and b.extmin.x >= x0 and b.extmax.x <= x1 and b.extmin.y >= y0 and b.extmax.y <= y1


def rect_thickness(area, perim):
    """同面积同周长的矩形的短边；接近正方形时返回短边 = 长边。"""
    d = perim * perim - 16 * area
    return (perim - math.sqrt(max(d, 0.0))) / 4


def _coords(poly):
    return [[round(x, 1), round(y, 1)] for x, y in poly.exterior.coords]


def _num(s):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s.replace("±", ""))
    return float(m.group()) if m else None


def _angle_diff(a, b, period=360.0):
    d = (a - b) % period
    return min(d, period - d)


def join_labels(frags, gap_h=1.0):
    """门窗编号：天正导出 T3 后一个编号常被拆成同一基线上的几段 TEXT（如 "FM" "丙" "1221"）。
    按阅读方向（文字自身的 X 轴）把同一基线上首尾相接的碎片接起来：前一段文字外包的末端到后一段的起点
    不超过 gap_h 倍字高（按字高算，不用绝对距离：同一张图换比例出图、字高变大也拼得上）；
    前一段以数字结尾、后一段以字母开头的不接（那是挨着的另一个编号）。
    frags: [(文字, x, y, 旋转角°, 字高, 实体)] → [(编号, 中心 x, 中心 y, 旋转角°)]，中心取各段文字外包的中心。"""
    def corners(k):
        s, x, y, _, _, e = frags[k]
        try:
            b = bbox.extents([e], fast=True)
            if b.has_data:
                return [(b.extmin.x, b.extmin.y), (b.extmax.x, b.extmin.y), (b.extmin.x, b.extmax.y), (b.extmax.x, b.extmax.y)]
        except Exception:
            pass
        return [(x, y)]
    ext = [corners(k) for k in range(len(frags))]
    succ = {}
    for i, (s, x, y, r, h, _) in enumerate(frags):
        ux, uy = math.cos(math.radians(r)), math.sin(math.radians(r))
        end_i = max((px - x) * ux + (py - y) * uy for px, py in ext[i])   # 本段文字末端（沿阅读方向，相对插入点）
        for j, (s2, x2, y2, r2, h2, _) in enumerate(frags):
            if j == i or _angle_diff(r, r2) > 1.0 or (s[-1:].isdigit() and s2[:1].isascii() and s2[:1].isalpha()):
                continue
            along = (x2 - x) * ux + (y2 - y) * uy
            off = (y2 - y) * ux - (x2 - x) * uy
            hh = max(h, h2, 1.0)
            gap = min((px - x) * ux + (py - y) * uy for px, py in ext[j]) - end_i
            if 0 < along and gap < gap_h * hh and abs(off) < 0.3 * hh and (i not in succ or along < succ[i][0]):
                succ[i] = (along, j)
    pred = {}
    for i, (along, j) in succ.items():   # 一段只能接在一段后面：取最近的
        if j not in pred or along < pred[j][0]:
            pred[j] = (along, i)
    nxt = {i: j for j, (_, i) in pred.items()}
    out = []
    for i in range(len(frags)):
        if i in pred:
            continue
        chain = [i]
        while chain[-1] in nxt:
            chain.append(nxt[chain[-1]])
        ents = [frags[k][5] for k in chain]
        try:
            b = bbox.extents(ents, fast=True)
            cx, cy = (b.extmin.x + b.extmax.x) / 2, (b.extmin.y + b.extmax.y) / 2
        except Exception:
            cx, cy = sum(frags[k][1] for k in chain) / len(chain), sum(frags[k][2] for k in chain) / len(chain)
        out.append(("".join(frags[k][0] for k in chain), cx, cy, frags[i][3]))
    return out


def match_labels(openings, labels, max_d=1200.0, ang_tol=10.0):
    """编号文字和门窗一对一配对：文字方向要和洞口（所在墙）平行；按文字中心到洞口（块门窗为洞口中线段，
    散线窗为外轮廓）的距离全局从近到远贪心，一个编号只给一樘、一樘只取一个编号，超过 max_d 的不配。
    返回 {门窗序号: (编号, 距离)}。"""
    geoms = []
    for o in openings:
        if o.polygon:
            geoms.append(Polygon(o.polygon))
        else:
            a = math.radians(o.rot)
            dx, dy = math.cos(a) * o.width / 2, math.sin(a) * o.width / 2
            geoms.append(LineString([(o.x - dx, o.y - dy), (o.x + dx, o.y + dy)]))
    cand = []
    for li, (s, cx, cy, r) in enumerate(labels):
        p = Point(cx, cy)
        for oi, (o, g) in enumerate(zip(openings, geoms)):
            if _angle_diff(r, o.rot, 180.0) > ang_tol:
                continue
            d = g.distance(p)
            if d <= max_d:
                cand.append((d, math.hypot(o.x - cx, o.y - cy), li, oi))
    cand.sort()
    used_l, out = set(), {}
    for d, _, li, oi in cand:
        if li in used_l or oi in out:
            continue
        used_l.add(li)
        out[oi] = (labels[li][0], d)
    return out


def recognize(dxf_path: str, region, wall_keep_min=180.0) -> dict:
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    lines, wall_lines, openings, texts, wtexts = [], [], [], [], []
    for e in msp:
        if not _inside(e, region):
            continue
        layer, t = e.dxf.layer, e.dxftype()
        if layer == "WALL" and t in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC"):
            for pts in _segments(e):
                lines.append(LineString(pts)); wall_lines.append(lines[-1])
        elif layer in ("COLUMN", "柱") and t in ("INSERT", "LWPOLYLINE", "POLYLINE"):
            for v in (e.virtual_entities() if t == "INSERT" else [e]):
                for pts in _segments(v):
                    lines.append(LineString(pts)); wall_lines.append(lines[-1])
        elif layer == "WINDOW" and t == "INSERT":
            name = e.dxf.name
            kind = "door" if "DORLIB" in name.upper() else "window"
            ins, rot = e.dxf.insert, e.dxf.rotation
            w = abs(e.dxf.xscale)
            o = Opening(f"{'M' if kind == 'door' else 'C'}{len(openings) + 1:03d}", kind, name, round(ins.x, 1), round(ins.y, 1),
                        round(rot, 2), round(w), round(abs(e.dxf.yscale)) if kind == "window" else 0.0)
            o.sx = -1 if e.dxf.xscale < 0 else 1
            o.sy = -1 if e.dxf.yscale < 0 else 1
            if kind == "door":
                o.leafs, o.swing, o.hinge_end = door_swing(e, ins, rot)
            openings.append(o)
            if kind == "window":  # 窗框线参与封口；门扇和开启弧不参与
                for v in e.virtual_entities():
                    for pts in _segments(v):
                        lines.append(LineString(pts))
            a = math.radians(rot)
            dx, dy = math.cos(a) * (w / 2 + 8), math.sin(a) * (w / 2 + 8)
            lines.append(LineString([(ins.x - dx, ins.y - dy), (ins.x + dx, ins.y + dy)]))  # 洞口中线封口
        elif layer == "WINDOW" and t in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC"):
            for pts in _segments(e):   # 散画的洞口线、推拉门线
                lines.append(LineString(pts))
        elif t in ("TEXT", "MTEXT"):
            s = clean(e.dxf.text if t == "TEXT" else e.text)
            p = e.dxf.insert
            texts.append((s, p.x, p.y, layer))
            if layer == "WINDOW_TEXT" and s:   # 门窗编号碎片：拼接要用方向和字高
                rot, h = (e.dxf.rotation, e.dxf.height) if t == "TEXT" else (e.get_rotation(), e.dxf.char_height)
                wtexts.append((s, p.x, p.y, rot, h, e))

    net = set_precision(unary_union(lines), 1.0)
    allfaces = [f for f in polygonize(net) if f.area > 2000]  # >0.002 ㎡
    merged = unary_union([f.buffer(1) for f in allfaces]).buffer(-1)
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    parts = [Polygon(g.exterior) for g in parts if g.area > 50e6]   # 去掉图例、表格；保留变形缝两侧的各段
    footprint = unary_union(parts)
    outer = footprint.boundary
    faces = [f for f in allfaces if footprint.contains(f.representative_point())]
    wall_buf = unary_union(wall_lines).buffer(3)

    # 文字
    room_texts = [(s, x, y) for s, x, y, l in texts if l in ("PUB_TEXT", "SPACE") and len(s) <= 8
                  and (any(w in s for w in ROOM_WORDS) or s in SHAFT_WORDS)]
    win_labels = join_labels(wtexts)
    unit_seeds = []   # (类型字母, x, y)
    for s, x, y, l in texts:
        if s.replace(" ", "") == "户型":
            letters = [(x - lx, ls) for ls, lx, ly, ll in texts
                       if re.fullmatch(r"[A-Z](?:-?\d+)?'?", ls) and 0 < x - lx < 1500 and abs(ly - y) < 200]
            unit_seeds.append((min(letters)[1] if letters else "?", x, y))
        elif re.fullmatch(r"[A-Z](?:-?\d+)?'?\s*户型", s):
            unit_seeds.append((s.replace("户型", "").strip(), x, y))
    levels = sorted({v for s, x, y, l in texts if l == "DIM_ELEV" for v in [_num(s)] if v is not None and -10 < v < 200})
    declared = None
    for s, x, y, l in texts:
        if "本层建筑面积" in s:
            cand = [(lx - x, ls) for ls, lx, ly, ll in texts if abs(ly - y) < 100 and 0 <= lx - x < 6000 and _num(ls)]
            v = _num(s) or (_num(min(cand)[1]) if cand else None)
            declared = v

    # 面上的名称
    labels = collections.defaultdict(list)
    for s, x, y in room_texts:
        pt = Point(x, y)
        cands = [(f.area, i) for i, f in enumerate(faces) if f.contains(pt)]
        if cands:
            i = min(cands)[1]
            name = "管井" if s in SHAFT_WORDS else s
            if name not in labels[i]:
                labels[i].append(name)

    # 面分类
    cat, thick, wallfrac = {}, {}, {}
    for i, f in enumerate(faces):
        A, P = f.area, f.length
        t = rect_thickness(A, P)
        L = P / 2 - t
        wf = f.exterior.intersection(wall_buf).length / P if P else 0
        thick[i], wallfrac[i] = t, wf
        thin = 40 <= t <= 450 and L > 2.5 * t
        if labels.get(i):
            cat[i] = "room"
        elif wf >= 0.8 and (thin or A < 0.6e6):
            cat[i] = "wall"
        elif A >= 1.0e6 and not thin and t >= 800:   # 短边 <0.8m 的是凸窗台、窗套之类，不算空间
            cat[i] = "space"
        else:
            cat[i] = "other"   # 窗框条、门洞条、碎面

    # 相邻关系（穿过非墙边界）
    tree = STRtree(faces)
    adj = collections.defaultdict(set)
    for i, f in enumerate(faces):
        if cat[i] == "wall":
            continue
        for j in tree.query(f, predicate="intersects"):
            j = int(j)
            if j <= i or cat[j] == "wall":
                continue
            shared = f.exterior.intersection(faces[j].exterior)
            if shared.is_empty or shared.length < 300:
                continue
            if shared.difference(wall_buf).length >= 300:
                adj[i].add(j); adj[j].add(i)

    def is_public(i):
        return any(any(w in n for w in PUBLIC_WORDS) for n in labels.get(i, []))

    # 分户：多源同步广度搜索。源 = 各户"X 户型"文字所在的面 + 已标名的公共空间；
    # 每个面归到最先到达的源；同一步被两个以上的源同时到达 → 公共。公共源只能占未标名的面。
    seeds = []
    for typ, x, y in unit_seeds:
        cands = [(f.area, i) for i, f in enumerate(faces) if f.contains(Point(x, y))]
        if cands:
            seeds.append((typ, x, y, min(cands)[1]))
    seeds.sort(key=lambda s: (round(-s[2] / 3000), s[1]))   # 先上后下、从左到右
    PUB = ("", "")
    owner = {i: PUB for i in range(len(faces)) if is_public(i)}
    conflicts = []
    for k, (typ, x, y, i0) in enumerate(seeds, 1):
        if i0 in owner:
            conflicts.append((f"U{k}", owner[i0][0], i0))
        owner[i0] = (f"U{k}", typ)
    current = list(owner)
    while current:
        nxt = collections.defaultdict(set)
        for i in current:
            for j in adj[i]:
                if j in owner or cat[j] == "wall":
                    continue
                if owner[i] == PUB and labels.get(j):
                    continue          # 公共空间不吞并已标名的户内房间
                nxt[j].add(owner[i])
        current = []
        for j, owners in nxt.items():
            if len(owners) == 1:
                owner[j] = owners.pop()
            else:
                owner[j] = PUB
                conflicts.append(("tie", sorted(o[0] for o in owners), j))
            current.append(j)
    unit_of = {i: o for i, o in owner.items() if o != PUB}

    # 散线画的窗（凸窗、阳台窗）：贴着外轮廓、和房间共边、不是门窗块的碎面
    #   凸窗 = 窗台条（墙洞两侧的 WINDOW 散线）+ 外挑的窗体，可能被玻璃线分成几块碎面 → 按"碎面连通块"合成一扇窗
    win_pts = [Point(o.x, o.y) for o in openings if o.kind == "window"]
    blockwin = {i for i, f in enumerate(faces) if cat[i] == "other" and any(f.buffer(150).contains(p) for p in win_pts)}
    done = set()
    for i0, f0 in enumerate(faces):
        if cat[i0] != "other" or i0 in blockwin or i0 in done or f0.area < 0.1e6 or f0.distance(outer) > 5:
            continue
        comp, q = {i0}, [i0]
        while q:
            i = q.pop()
            for j in adj[i]:
                if j not in comp and cat[j] == "other" and j not in blockwin:
                    comp.add(j); q.append(j)
        done |= comp
        f = unary_union([faces[i] for i in comp])
        if len(comp) > 8 or f.area > 5e6:
            continue
        # 窗台条可能被判成墙（WINDOW 散线和 WALL 线重合），所以按距离找房间：隔一道墙（≤450mm）里最近的那间
        near = {j: faces[j].distance(f) for j in tree.query(f.buffer(450), predicate="intersects")
                if cat[int(j)] in ("room", "space")}
        if not near:
            continue
        dmin = min(near.values())
        nb = {int(j) for j, d in near.items() if d <= dmin + 50}
        # 窗体主体 = 碎面里最大的那块（窗扇所在），宽度和方向按它的长边算；连通块里还会并进墙角凸出、侧面切角之类的
        # 小碎面，只保留落在主体沿墙范围内的部分（窗台条、窗框条），否则宽度会被撑大 120～400mm
        main = max((faces[i] for i in comp), key=lambda g: g.area)
        mrr = main.minimum_rotated_rectangle
        cs = list(mrr.exterior.coords)
        e1 = math.dist(cs[0], cs[1]); e2 = math.dist(cs[1], cs[2])
        a = math.degrees(math.atan2(cs[1][1] - cs[0][1], cs[1][0] - cs[0][0])) if e1 >= e2 else \
            math.degrees(math.atan2(cs[2][1] - cs[1][1], cs[2][0] - cs[1][0]))
        u, n = (math.cos(math.radians(a)), math.sin(math.radians(a))), (-math.sin(math.radians(a)), math.cos(math.radians(a)))
        ts = [x * u[0] + y * u[1] for g in comp if faces[g].area >= 0.25 * main.area for x, y in faces[g].exterior.coords]
        lo, hi = min(ts), max(ts)
        s0 = main.centroid.x * n[0] + main.centroid.y * n[1]
        slab = Polygon([(u[0] * t_ + n[0] * s_, u[1] * t_ + n[1] * s_) for t_, s_ in
                        ((lo, s0 - 1e5), (hi, s0 - 1e5), (hi, s0 + 1e5), (lo, s0 + 1e5))])
        if f.difference(slab).area > 1.0:
            body = f.intersection(slab)
            body = unary_union([g for g in getattr(body, "geoms", [body]) if g.geom_type == "Polygon"])
        else:
            body = f
        ns = [x * n[0] + y * n[1] for x, y in body.convex_hull.exterior.coords]
        c = body.representative_point()
        o = Opening(f"C{len(openings) + 1:03d}", "window", "散线窗（凸窗/阳台窗）", round(c.x, 1), round(c.y, 1),
                    round(a % 180, 2), round(hi - lo), round(max(ns) - min(ns)))
        o.exterior = True
        o._rooms = nb
        o.polygon = _coords(body.convex_hull)
        openings.append(o)

    # 房间
    rooms, face_room = [], {}
    order = sorted([i for i in range(len(faces)) if cat[i] in ("room", "space")],
                   key=lambda i: (unit_of.get(i, ("U99",))[0].rjust(4), -faces[i].centroid.y, faces[i].centroid.x))
    for i in order:
        f = faces[i]
        names = labels.get(i, [])
        uid, typ = unit_of.get(i, ("", ""))
        r = Room(f"R{len(rooms) + 1:03d}", "/".join(names) if names else "未标注", names, round(f.area / 1e6, 2), _coords(f),
                 uid, typ, is_public(i) or not uid, bool(names))
        rooms.append(r)
        face_room[i] = r

    # 门窗编号：拼好的编号和门窗（含散线窗）一对一配对，宽度位对不上的不采用
    matched = match_labels(openings, win_labels)
    for oi, (s, _) in matched.items():
        o = openings[oi]
        o.label_raw = s
        o.label = s if markcode.fits(s, o.width) else ""
    used = collections.Counter(s for s, _ in matched.values())
    unmatched = []
    for s, x, y, _ in win_labels:
        if used[s] > 0:
            used[s] -= 1
        else:
            unmatched.append([s, round(x), round(y)])

    # 门窗：是否在外边、相邻房间
    for o in openings:
        c = Point(o.x, o.y)
        nb = getattr(o, "_rooms", None)
        if nb is None:
            o.exterior = outer.distance(c) < 260
        for i, r in face_room.items():
            if (i in nb) if nb is not None else (faces[i].distance(c) < 220):
                o.rooms.append(r.id)
                (r.doors if o.kind == "door" else r.windows).append(o.id)
                if o.kind == "window" and o.exterior:
                    r.ext_windows.append(o.id)

    # 墙段
    walls = []
    for i, f in enumerate(faces):
        if cat[i] != "wall":
            continue
        t = thick[i]
        ext = f.distance(outer) < 5
        keep = t >= wall_keep_min or ext
        reason = "外墙" if ext else ("厚度≥%d，疑似承重/分户墙" % wall_keep_min if t >= wall_keep_min else "薄墙，疑似砌块隔墙")
        walls.append(WallSeg(f"W{len(walls) + 1:03d}", round(t), round(f.length / 2 - t), _coords(f), ext, keep, reason))

    # 洞口所在墙厚：洞口两侧 400mm 内的墙段厚度取中位数（窗块有 Y 比例就直接用）
    wpolys = [(Polygon(w.polygon), w.thickness) for w in walls]
    for o in openings:
        if o.polygon:
            continue
        a = math.radians(o.rot)
        ends = [Point(o.x + s * math.cos(a) * (o.width / 2 + 150), o.y + s * math.sin(a) * (o.width / 2 + 150)) for s in (-1, 1)]
        ts = sorted(t for p, t in wpolys if any(p.distance(e) < 120 for e in ends))
        o.wall_t = float(o.depth) if o.kind == "window" and o.depth >= 60 else (float(ts[len(ts) // 2]) if ts else 200.0)

    units = {}
    for r in rooms:
        if r.unit:
            u = units.setdefault(r.unit, {"id": r.unit, "type": r.unit_type, "area_m2": 0.0, "rooms": []})
            u["area_m2"] = round(u["area_m2"] + r.area_m2, 2)
            u["rooms"].append(r.id)
    return {
        "source": dxf_path, "region": list(region),
        "footprint": [[list(map(lambda v: round(v, 1), c)) for c in g.exterior.coords] for g in parts],
        "footprint_area_m2": round(footprint.area / 1e6, 2),
        "declared_area_m2": declared,
        "levels": levels,
        "faces": len(faces),
        "face_categories": dict(collections.Counter(cat.values())),
        "units": list(units.values()),
        "unit_conflicts": conflicts,
        "unit_seeds": [(t, round(x), round(y)) for t, x, y, _ in seeds],
        "rooms": [asdict(r) for r in rooms],
        "walls": [asdict(w) for w in walls],
        "openings": [asdict(o) for o in openings],
        "axes": read_axes(msp, region),
        "unmatched_labels": unmatched,   # 没配上门窗的原图编号 [文字, x, y]
    }


def to_svg(res: dict, out: str, width=1800):
    """识别结果的检查图：按户着色的房间、保留墙（深）/可拆墙（浅）、外窗（蓝）/内窗（青）/门（红）。"""
    xs = [p[0] for g in res["footprint"] for p in g]; ys = [p[1] for g in res["footprint"] for p in g]
    x0, y0, x1, y1 = min(xs) - 1500, min(ys) - 1500, max(xs) + 1500, max(ys) + 1500
    W, H = x1 - x0, y1 - y0
    pal = ["#cfe8ff", "#ffe0c2", "#d8f5d0", "#f5d0f0", "#fff3b0", "#d0f0f0", "#e8d8ff", "#ffd6d6", "#e0e0c0", "#c8e6c9"]
    ids = sorted({r["unit"] for r in res["rooms"] if r["unit"]}, key=lambda s: int(s[1:]))
    col = {u: pal[k % len(pal)] for k, u in enumerate(ids)}
    P = lambda pts: "M" + " L".join(f"{x:.0f},{-y:.0f}" for x, y in pts) + "Z"
    parts = [f'<path d="{P(g)}" fill="#f4f4f4" stroke="#000" stroke-width="40"/>' for g in res["footprint"]]
    for r in res["rooms"]:
        fill = "#e6e6e6" if r["public"] else col.get(r["unit"], "#ffffff")
        parts.append(f'<path d="{P(r["polygon"])}" fill="{fill}" stroke="#666" stroke-width="15"/>')
    for w in res["walls"]:
        parts.append(f'<path d="{P(w["polygon"])}" fill="{"#222" if w["keep"] else "#bbb"}" stroke="none"/>')
    for o in res["openings"]:
        c = "#d00" if o["kind"] == "door" else ("#06f" if o["exterior"] else "#0bb")
        if o["polygon"]:
            parts.append(f'<path d="{P(o["polygon"])}" fill="#9cf" stroke="#06f" stroke-width="40"/>')
            continue
        a = math.radians(o["rot"]); dx, dy = math.cos(a) * o["width"] / 2, math.sin(a) * o["width"] / 2
        parts.append(f'<line x1="{o["x"] - dx:.0f}" y1="{-(o["y"] - dy):.0f}" x2="{o["x"] + dx:.0f}" y2="{-(o["y"] + dy):.0f}" stroke="{c}" stroke-width="160"/>')
    for r in res["rooms"]:
        pts = r["polygon"]; cx = sum(p[0] for p in pts[:-1]) / (len(pts) - 1); cy = sum(p[1] for p in pts[:-1]) / (len(pts) - 1)
        if r["area_m2"] >= 2:
            parts.append(f'<text x="{cx:.0f}" y="{-cy:.0f}" font-size="420" text-anchor="middle" font-family="Microsoft YaHei">{r["name"]}</text>')
            parts.append(f'<text x="{cx:.0f}" y="{-cy + 520:.0f}" font-size="340" text-anchor="middle" fill="#555">{r["area_m2"]}㎡ {r["unit"]}{r["unit_type"]}</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.0f} {-y1:.0f} {W:.0f} {H:.0f}" width="{width}" height="{width * H / W:.0f}">'
           f'<rect x="{x0:.0f}" y="{-y1:.0f}" width="{W:.0f}" height="{H:.0f}" fill="#fff"/>' + "".join(parts) + "</svg>")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(svg)


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m jiazhuang.dxfplan", allow_abbrev=False,
                                 description="从打散的天正 DXF 平面识别房间、户、墙、门窗、轴网，写成识别 JSON（make_plan_set 的输入）。")
    ap.add_argument("dxf", type=printing.existing_file, metavar="平面.dxf")
    ap.add_argument("out", metavar="识别.json")
    ap.add_argument("svg", nargs="?", metavar="检查图.svg", help="识别检查图：按户着色的房间、墙、门窗")
    ap.add_argument("--region", nargs=4, type=float, required=True, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="这一层在图上的外包（图纸毫米），只识别完全落在里面的图元（一张 DXF 里常有好几层）")
    ap.add_argument("--verbose", action="store_true", help="另外打印识别统计（调试用）")
    a = ap.parse_args(argv)
    res = recognize(a.dxf, tuple(a.region))
    ops = res["openings"]
    printing.log("recognized", "rooms", len(res["rooms"]), "units", len(res["units"]), "walls", len(res["walls"]),
                 "doors", sum(o["kind"] == "door" for o in ops), "windows", sum(o["kind"] == "window" for o in ops),
                 "labels", sum(bool(o["label"]) for o in ops))
    if a.verbose:
        _stats(res)
    outs = [a.out] + ([a.svg] if a.svg else [])
    if not res["rooms"] or not res["walls"]:
        # --region 不是这一层在图上的外包（常见：忘了图纸坐标的偏移）：不写结果，旧的也删掉，免得下一步拿去出图
        for p in outs:
            if os.path.exists(p):
                os.remove(p)
        printing.log("failed", "no rooms or walls: check --region", os.path.abspath(a.out))
        sys.exit(1)
    for p in outs:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    if a.svg:
        to_svg(res, a.svg)
    printing.done(a.out)


def _stats(res: dict) -> None:
    C = collections.Counter
    print("faces", res["faces"], res["face_categories"], "footprint m2", res["footprint_area_m2"], "declared", res["declared_area_m2"])
    print("levels", res["levels"])
    print("rooms", len(res["rooms"]), C(r["name"] for r in res["rooms"]).most_common())
    print("units", [(u["id"], u["type"], u["area_m2"], len(u["rooms"])) for u in res["units"]], "conflicts", res["unit_conflicts"][:5])
    print("public", [(r["name"], r["area_m2"]) for r in res["rooms"] if r["public"]])
    print("no unit, not public", [(r["name"], r["area_m2"]) for r in res["rooms"] if not r["unit"] and not r["public"]])
    print("walls", len(res["walls"]), "keep", sum(w["keep"] for w in res["walls"]), "thickness", C(w["thickness"] // 10 * 10 for w in res["walls"]).most_common(10))
    print("openings", C((o["kind"], o["exterior"]) for o in res["openings"]))
    print("labels adopted", C(o["label"] for o in res["openings"] if o["label"]).most_common(),
          "rejected (size)", [(o["id"], o["label_raw"], o["width"]) for o in res["openings"] if o["label_raw"] and not o["label"]],
          "unmatched", res["unmatched_labels"])
    bed = [r for r in res["rooms"] if any(n in ("主卧", "次卧", "书房", "卧室") for n in r["names"])]
    print("bed w/o ext", [(r["id"], r["name"], r["unit"], r["windows"], r["area_m2"]) for r in bed if not r["ext_windows"]])
    print("doors by leafs", C(o["leafs"] for o in res["openings"] if o["kind"] == "door"), "swing", C(o["swing"] for o in res["openings"] if o["kind"] == "door"))
    print("axes x", [a["label"] for a in res["axes"]["x"]], "y", [(a["label"], a["ends"]) for a in res["axes"]["y"]])
    print("bedrooms", len(bed), "with ext window", sum(1 for r in bed if r["ext_windows"]), "areas", sorted(r["area_m2"] for r in bed))


if __name__ == "__main__":
    main()
