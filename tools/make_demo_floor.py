"""合成的住宅标准层（公开示例，不对应任何真实工程）：写一张打散的天正 T3 风格 DXF，给 dxfplan 识别、make_plan_set 出图。

  python tools/make_demo_floor.py build/floor            写 build/floor/floor.dxf；JZLOG region 一行是识别范围（dxfplan 的 --region）
  python tools/make_demo_floor.py build/floor --scheme   读 build/floor/recog.json，写 build/floor/scheme.json（两个分区 + 水电点位）
最后一行是 JZLOG done <写出的文件>。

一梯四户板式住宅：两端各一户三室，中间两户两室，核心筒（楼梯、电梯、消防电梯、合用前室、管井）居中，北侧公共走道。
图层和图元按 dxfplan 读取的约定写：WALL 墙线（每段墙一个矩形），WINDOW 门窗块（插入点在洞口中心，X 比例 = 洞宽，
窗和推拉门的 Y 比例 = 墙厚，平开门带开启弧）和凸窗散线，WINDOW_TEXT 门窗编号（防火门编号拆成几段），PUB_TEXT 房间名和户型，
DIM_ELEV 各层标高，DOTE 轴线 + _AXISO 轴号。只依赖 ezdxf、shapely。
"""
import argparse
import json
import os
import sys

import ezdxf
from ezdxf import bbox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import conventions, printing  # noqa: E402

ORIGIN = (20000.0, 10000.0)   # 图纸坐标原点偏移（mm）：识别和建模都要处理不在 0 点的图
LEN = 53400.0                 # 两端外墙轴线之间的总长；4 号户是 1 号户的镜像（x → LEN - x）
LEVELS = [3.0 * k for k in range(1, 18)]   # 二～十八层，层高 3.000
TEXT_H, MARK_H = 350.0, 250.0


# ---------------------------------------------------------------- 平面：房间矩形（墙轴线坐标）、门窗
# 房间 (名称, x0, x1, y0, y1, 合并组)：同一户里同一合并组的矩形之间不画墙（客厅、餐厅、过道、门厅连成一个空间）；名称为 None 的不写字
def unit1():
    R = [("主卧", 0, 3600, 0, 4500, None), ("客厅", 3600, 7800, 0, 4500, "L"), ("次卧", 7800, 10800, 0, 4500, None),
         ("书房", 10800, 13200, 0, 4500, None), ("主卫", 0, 2400, 4500, 6900, None), (None, 2400, 3600, 4500, 6900, "L"),
         ("餐厅", 3600, 7800, 4500, 6900, "L"), (None, 7800, 13200, 4500, 6900, "L"), ("储藏", 0, 2400, 6900, 9000, None),
         (None, 2400, 3600, 6900, 9000, "L"), ("厨房", 0, 3600, 9000, 12000, None), ("卫生间", 3600, 6000, 9600, 12000, None),
         ("卧室", 6000, 9600, 9600, 12000, None), (None, 3600, 13200, 6900, 9600, "L"), (None, 9600, 13200, 9600, 12000, "L"),
         ("阳台", 3600, 7800, -1500, 0, None)]
    # 门窗 (类, 墙向, x, y, 宽, 编号, 开向)：墙向 h = 沿 x 的墙（在 y 处），v = 沿 y 的墙（在 x 处）；
    # 类：door 单扇平开 / door2 双扇平开 / slide 推拉门（电梯门同）/ win 窗 / bay 凸窗（散线，开向 = 外挑方向）
    O = [("door", "h", 1200, 4500, 700, "", "N"), ("door", "h", 3000, 4500, 900, "", "S"),
         ("door", "h", 8400, 4500, 900, "", "S"), ("door", "h", 11400, 4500, 900, "", "S"), ("door", "v", 2400, 7950, 700, "", "W"),
         ("door", "h", 3000, 9000, 800, "", "N"), ("door", "h", 4800, 9600, 700, "", "N"), ("door", "h", 7200, 9600, 900, "", "N"),
         ("door", "v", 13200, 11250, 1000, "", "W"), ("slide", "h", 5700, 0, 2400, "TLM2421", "S"),
         ("bay", "h", 1800, 0, 1800, "TC1819", "S"), ("win", "v", 0, 2250, 1200, "C1215", "W"), ("win", "h", 9300, 0, 1500, "C1515", "S"),
         ("win", "h", 12000, 0, 1200, "C1215", "S"), ("win", "v", 0, 5700, 600, "C0615", "W"), ("win", "h", 1800, 12000, 1500, "C1515", "N"),
         ("win", "h", 4800, 12000, 600, "C0615", "N"), ("win", "h", 7800, 12000, 1500, "C1515", "N"), ("win", "h", 11400, 12000, 1200, "C1215", "N"),
         ("win", "h", 5700, -1500, 3600, "C3615", "S")]
    return R, O


def unit2():
    R = [("次卧", 13200, 16500, 0, 4500, None), ("客厅", 16500, 20700, 0, 4500, "L"), ("主卫", 20700, 22800, 0, 2700, None),
         (None, 20700, 22800, 2700, 6900, "L"), ("主卧", 22800, 26700, 0, 4500, None), ("卫生间", 13200, 15600, 4500, 6900, None),
         (None, 15600, 16500, 4500, 6900, "L"), ("餐厅", 16500, 20700, 4500, 6900, "L"), ("厨房", 13200, 16500, 6900, 10500, None),
         (None, 16500, 20700, 6900, 10500, "L"), ("储藏", 20700, 22800, 6900, 8700, None), (None, 20700, 22800, 8700, 10500, "L"),
         ("阳台", 16500, 20700, -1500, 0, None)]
    O = [("door", "v", 16500, 3600, 900, "", "W"), ("door", "v", 22800, 1350, 700, "", "W"), ("door", "v", 22800, 3600, 900, "M1221", "E"),
         ("door", "v", 15600, 5700, 700, "", "W"), ("door", "v", 16500, 8700, 800, "", "W"), ("door", "v", 20700, 7800, 700, "", "E"),
         ("door", "h", 21600, 10500, 1000, "", "S"), ("slide", "h", 18600, 0, 2400, "TLM2421", "S"), ("bay", "h", 24750, 0, 2100, "TC2119", "S"),
         ("win", "h", 14850, 0, 1500, "C1515", "S"), ("win", "h", 21750, 0, 600, "C0615", "S"), ("win", "h", 14850, 10500, 1200, "C1212", "N"),
         ("win", "h", 18600, -1500, 3600, "C3615", "S")]
    return R, O


def unit3():
    R = [("主卧", 26700, 31200, 0, 4500, None), ("主卫", 31200, 33300, 0, 2700, None), (None, 31200, 33300, 2700, 6900, "L"),
         ("客厅", 33300, 37200, 0, 4500, "L"), ("次卧", 37200, 40200, 0, 4500, None), ("餐厅", 33300, 37200, 4500, 6900, "L"),
         ("卫生间", 37200, 40200, 4500, 6900, None), ("厨房", 37200, 40200, 6900, 10500, None), (None, 31200, 37200, 6900, 10500, "L"),
         ("阳台", 33300, 37200, -1500, 0, None)]
    O = [("door", "v", 31200, 3600, 900, "", "W"), ("door", "v", 31200, 1350, 700, "", "E"), ("door", "v", 37200, 3600, 900, "", "E"),
         ("door", "v", 37200, 5700, 700, "", "E"), ("door", "v", 37200, 8700, 800, "", "E"), ("door", "h", 32400, 10500, 1000, "", "S"),
         ("slide", "h", 35250, 0, 2400, "TLM2421", "S"), ("bay", "h", 28950, 0, 2100, "TC2119", "S"), ("win", "h", 32250, 0, 600, "C0615", "S"),
         ("win", "h", 38700, 0, 1500, "C1515", "S"), ("win", "h", 38700, 10500, 1200, "C1212", "N"), ("win", "h", 35250, -1500, 3600, "C3615", "S")]
    return R, O


def core():
    R = [("楼梯", 22800, 25500, 4500, 10500, None), ("消防电梯", 25500, 27600, 4500, 7500, None), ("电梯", 27600, 29700, 4500, 7500, None),
         ("前室", 25500, 29700, 7500, 10500, None), ("风井", 29700, 31200, 4500, 7500, None), ("水井", 29700, 31200, 7500, 8700, None),
         ("强电", 29700, 31200, 8700, 10500, None)]
    O = [("door", "v", 25500, 9000, 1000, ["FM", "乙", "1021"], "E"), ("slide", "h", 26550, 7500, 1000, "", "N"),
         ("slide", "h", 28650, 7500, 1000, "", "N"), ("door2", "h", 27600, 10500, 1200, ["FM乙", "1221"], "S"),
         ("door", "v", 29700, 8100, 600, ["FM丙", "0618"], "W"), ("door", "h", 30450, 10500, 600, ["FM丙", "0618"], "N")]
    return R, O


def corridor():
    return [("走道", 13200, 40200, 10500, 12000, None)], [("win", "h", 18000, 12000, 1500, "C1515", "N"), ("win", "h", 35400, 12000, 1500, "C1515", "N")]


def mirror(R, O):
    """1 号户 → 4 号户：x → LEN - x；开向东西互换；沿 x 的墙上的门铰链换到另一端（门窗元组末尾加 -1，写块时乘到 X 比例上）。"""
    R2 = [(n, LEN - x1, LEN - x0, y0, y1, g) for n, x0, x1, y0, y1, g in R]
    flip = {"E": "W", "W": "E"}
    O2 = [(k, d, LEN - x, y, w, lab, flip.get(s, s)) + ((-1,) if d == "h" else ()) for k, d, x, y, w, lab, s in O]
    return R2, O2


def plan():
    """[(户, 房间)], [(户, 门窗)], [(户型文字, x, y)]"""
    rooms, opens = [], []
    for uid, (R, O) in (("U1", unit1()), ("U2", unit2()), ("core", core()), ("pub", corridor()), ("U3", unit3()), ("U4", mirror(*unit1()))):
        rooms += [(uid, r) for r in R]
        opens += [(uid, o) for o in O]
    seeds = [("A户型", 5700, 1500), ("B户型", 18600, 1500), ("C户型", 35250, 1500), ("A'户型", LEN - 5700, 1500)]
    return rooms, opens, seeds


# ---------------------------------------------------------------- 墙：从房间矩形的公共边推出墙段
def wall_thickness(a, b):
    """墙两侧的房间（None = 室外）→ 墙厚：阳台栏板 120；外墙、分户墙、核心筒和走道的墙 200；户内隔墙 100；同一合并组不画墙。"""
    if a is b or (a and b and a[1][5] and a[1][5] == b[1][5] and a[0] == b[0]):
        return 0
    bal = [x for x in (a, b) if x and x[1][0] == "阳台"]
    if bal:
        return 120 if (a is None or b is None) else 200
    if a is None or b is None or a[0] != b[0] or a[0] in ("core", "pub"):
        return 200
    return 100


def wall_runs(rooms):
    """[(墙向, 坐标, 起, 止, 厚)]：沿每条网格线逐格判断两侧房间，同厚的连续格并成一段。"""
    xs = sorted({v for _, r in rooms for v in r[1:3]})
    ys = sorted({v for _, r in rooms for v in r[3:5]})
    at = lambda x, y: next((ur for ur in rooms if ur[1][1] < x < ur[1][2] and ur[1][3] < y < ur[1][4]), None)
    runs = []
    for d, lines, steps in (("h", ys, xs), ("v", xs, ys)):
        for c in lines:
            cur = None
            for a0, a1 in zip(steps, steps[1:]):
                m = (a0 + a1) / 2
                t = wall_thickness(*((at(m, c - 1), at(m, c + 1)) if d == "h" else (at(c - 1, m), at(c + 1, m))))
                if cur and t == cur[4] and cur[3] == a0:
                    cur[3] = a1
                    continue
                if cur:
                    runs.append(tuple(cur))
                cur = [d, c, a0, a1, t] if t else None
            if cur:
                runs.append(tuple(cur))
    return runs


def wall_pieces(runs, opens):
    """墙段在洞口处断开；段端头（不是洞口边）伸进相交的墙半个厚度，让墙角、丁字接头封闭。返回矩形 [(x0, y0, x1, y1)]。"""
    def ext(run, a):   # 端点 a 处：伸进相交墙半个厚度；同一条线上另一侧还有一段（厚度不同）时两段都只到相交墙的墙面
        d, c = run[:2]
        perp = [t / 2 for d2, c2, b0, b1, t in runs if d2 != d and c2 == a and b0 <= c <= b1]
        if not perp:
            return 0.0
        other = any(r is not run and r[0] == d and r[1] == c and a in (r[2], r[3]) for r in runs)
        return -max(perp) if other else max(perp)
    out = []
    for run in runs:
        d, c, a0, a1, t = run
        cuts = sorted((o[2] - o[4] / 2, o[2] + o[4] / 2) if d == "h" else (o[3] - o[4] / 2, o[3] + o[4] / 2)
                      for _, o in opens if o[1] == d and (o[3] if d == "h" else o[2]) == c
                      and a0 < (o[2] if d == "h" else o[3]) < a1)
        edges = [a0 - ext(run, a0)] + [v for cut in cuts for v in cut] + [a1 + ext(run, a1)]
        for p, q in zip(edges[0::2], edges[1::2]):
            assert q - p >= 100, f"墙段太短：{d} {c} {p}～{q}"
            out.append((p, c - t / 2, q, c + t / 2) if d == "h" else (c - t / 2, p, c + t / 2, q))
    return out


def thickness_at(runs, o):
    d, x, y = o[1], o[2], o[3]
    return next(t for d2, c, a0, a1, t in runs if d2 == d and c == (y if d == "h" else x) and a0 < (x if d == "h" else y) < a1)


# ---------------------------------------------------------------- DXF
def blocks(doc):
    b = doc.blocks.new("DORLIB_SWING1")      # 单扇平开：铰链在 (-0.5, 0)，门扇开到 +y
    b.add_line((-0.5, 0), (-0.5, 1.0)); b.add_line((-0.45, 0), (-0.45, 1.0)); b.add_arc((-0.5, 0), 1.0, 0, 90)
    b = doc.blocks.new("DORLIB_SWING2")      # 双扇平开
    for s in (-1, 1):
        b.add_line((0.5 * s, 0), (0.5 * s, 0.5))
    b.add_arc((-0.5, 0), 0.5, 0, 90); b.add_arc((0.5, 0), 0.5, 90, 180)
    b = doc.blocks.new("DORLIB_SLIDE")       # 推拉门（两扇错开），Y 比例 = 墙厚
    for x0, x1, y0, y1 in ((-0.5, 0.05, 0.0, 0.15), (-0.05, 0.5, -0.15, 0.0)):
        b.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True)
    b = doc.blocks.new("WINLIB_C")           # 窗：洞口框 + 两道玻璃线，X 比例 = 洞宽，Y 比例 = 墙厚
    b.add_lwpolyline([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)], close=True)
    b.add_line((-0.5, -1 / 6), (0.5, -1 / 6)); b.add_line((-0.5, 1 / 6), (0.5, 1 / 6))
    b = doc.blocks.new("_AXISO")             # 轴号圈 + 属性
    b.add_circle((0, 0), 0.5)
    b.add_attdef("A", (0, 0), dxfattribs={"height": 0.35, "layer": "AXIS_TEXT"})


def add_text(msp, s, x, y, h, layer, center=True):
    e = msp.add_text(s, height=h, dxfattribs={"layer": layer})
    e.set_placement((x, y))
    if center:   # 文字外包的中心落在 (x, y)
        b = bbox.extents([e], fast=True)
        e.set_placement((2 * x - (b.extmin.x + b.extmax.x) / 2, 2 * y - (b.extmin.y + b.extmax.y) / 2))
    return e


def add_mark(msp, lab, x, y, rot):
    """门窗编号：一段，或拆成首尾相接的几段（天正导出后常见）；整串的中心落在 (x, y)。"""
    parts = lab if isinstance(lab, list) else [lab]
    es, pos = [], 0.0
    for s in parts:
        e = msp.add_text(s, height=MARK_H, rotation=rot, dxfattribs={"layer": "WINDOW_TEXT"})
        e.set_placement((pos, 0.0) if rot == 0 else (0.0, pos))
        b = bbox.extents([e], fast=True)
        pos = (b.extmax.x if rot == 0 else b.extmax.y) + 0.2 * MARK_H
        es.append(e)
    b = bbox.extents(es, fast=True)
    dx, dy = x - (b.extmin.x + b.extmax.x) / 2, y - (b.extmin.y + b.extmax.y) / 2
    for e in es:
        p = e.dxf.insert
        e.set_placement((p.x + dx, p.y + dy))


SIDE = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}


def write_dxf(path):
    rooms, opens, seeds = plan()
    runs = wall_runs(rooms)
    ox, oy = ORIGIN
    P = lambda x, y: (x + ox, y + oy)
    doc = ezdxf.new("R2010")
    for name in ("WALL", "WINDOW", "WINDOW_TEXT", "PUB_TEXT", "DIM_ELEV", "DOTE", "AXIS", "AXIS_TEXT"):
        doc.layers.add(name)
    blocks(doc)
    msp = doc.modelspace()
    for x0, y0, x1, y1 in wall_pieces(runs, opens):
        for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            msp.add_line(P(*a), P(*b), dxfattribs={"layer": "WALL"})
    for _, o in opens:
        k, d, x, y, w, lab, side = o[:7]
        t = thickness_at(runs, o)
        rot = 0.0 if d == "h" else 90.0
        n = (0, 1) if d == "h" else (-1, 0)              # 块的 +y 方向（墙法线）
        sv = SIDE[side]
        sgn = 1 if sv[0] * n[0] + sv[1] * n[1] > 0 else -1
        if k in ("door", "door2"):
            hinge = o[7] if len(o) > 7 else 1
            msp.add_blockref("DORLIB_SWING1" if k == "door" else "DORLIB_SWING2", P(x, y),
                             dxfattribs={"layer": "WINDOW", "rotation": rot, "xscale": w * hinge, "yscale": w * sgn})
        elif k in ("slide", "win"):
            msp.add_blockref("DORLIB_SLIDE" if k == "slide" else "WINLIB_C", P(x, y),
                             dxfattribs={"layer": "WINDOW", "rotation": rot, "xscale": w, "yscale": t})
        else:   # 凸窗：墙洞两侧窗台线 + 外挑窗体（外框、内框、中间一道玻璃线）
            D = 600.0
            for s in (-1, 1):
                msp.add_line(P(x - w / 2, y + s * t / 2), P(x + w / 2, y + s * t / 2), dxfattribs={"layer": "WINDOW"})
            for inset, dep in ((0.0, D), (100.0, D - 100.0)):
                y0 = y + sv[1] * t / 2
                pts = [(x - w / 2 + inset, y0), (x - w / 2 + inset, y0 + sv[1] * dep), (x + w / 2 - inset, y0 + sv[1] * dep), (x + w / 2 - inset, y0)]
                msp.add_lwpolyline([P(*p) for p in pts], dxfattribs={"layer": "WINDOW"})
            ym = y + sv[1] * (t / 2 + (D - 100.0) / 2)
            msp.add_line(P(x - w / 2 + 100, ym), P(x + w / 2 - 100, ym), dxfattribs={"layer": "WINDOW"})
        if lab:
            off = (t / 2 + 600 + 350) if k == "bay" else (t / 2 + 350)
            lx, ly = x + sv[0] * off, y + sv[1] * off
            add_mark(msp, lab, *P(lx, ly), rot)
    for uid, (name, x0, x1, y0, y1, _) in rooms:
        if name:
            add_text(msp, name, *P((x0 + x1) / 2, (y0 + y1) / 2), TEXT_H, "PUB_TEXT")
    for s, x, y in seeds:
        add_text(msp, s, *P(x, y), 500.0, "PUB_TEXT")
    for i, v in enumerate(LEVELS):   # 一个标准层代表的各层标高，叠注在 1 号户餐厅、门厅里
        add_text(msp, f"{v:.3f}", *P(4500, 2400 + i * 380), 300.0, "DIM_ELEV", center=False)
    # 轴线（DOTE 层长线）+ 两端轴号（_AXISO 块，属性 = 轴号）
    xa = [0, 3600, 7800, 10800, 13200, 16500, 20700, 22800, 25500, 26700, 29700, 31200, 33300, 37200, 40200,
          LEN - 10800, LEN - 7800, LEN - 3600, LEN]
    ya = [0, 4500, 6900, 10500, 12000]
    y_lo, y_hi, x_lo, x_hi = -4000.0, 14500.0, -3000.0, LEN + 3000.0
    for i, x in enumerate(xa, 1):
        msp.add_line(P(x, y_lo), P(x, y_hi), dxfattribs={"layer": "DOTE"})
        for y in (y_lo - 400, y_hi + 400):
            msp.add_blockref("_AXISO", P(x, y), dxfattribs={"layer": "AXIS", "xscale": 800, "yscale": 800}).add_auto_attribs({"A": str(i)})
    for lab, y in zip("ABCDE", ya):
        msp.add_line(P(x_lo, y), P(x_hi, y), dxfattribs={"layer": "DOTE"})
        for x in (x_lo - 400, x_hi + 400):
            msp.add_blockref("_AXISO", P(x, y), dxfattribs={"layer": "AXIS", "xscale": 800, "yscale": 800}).add_auto_attribs({"A": lab})
    doc.saveas(path)
    # 识别范围：除轴线、轴号外全部图元的外包，四周放 1m，取整到米
    b = bbox.extents(e for e in msp if e.dxf.layer not in ("DOTE", "AXIS"))
    reg = (int(b.extmin.x // 1000 - 1) * 1000, int(b.extmin.y // 1000 - 1) * 1000, int(b.extmax.x // 1000 + 2) * 1000, int(b.extmax.y // 1000 + 2) * 1000)
    return reg


# ---------------------------------------------------------------- 方案：两个分区 + 水电点位（出分区平面和图例用）
def write_scheme(recog_path, out_path, rec=None):
    from shapely.geometry import Point, Polygon
    from jiazhuang import scheme
    if os.path.exists(out_path):   # 失败（退出 1）时不留上一次的 scheme.json
        os.remove(out_path)
    if rec is None:
        with open(recog_path, encoding="utf-8") as fh:
            rec = json.load(fh)
    sch = scheme.from_recognition(rec)
    ox, oy = ORIGIN
    split = 31200.0 + ox   # 西区 = 1、2 号户 + 核心筒；东区 = 3、4 号户；走道不分区
    unit_of = {r["id"]: r["unit"] for r in rec["rooms"]}
    for r in sch["rooms"]:
        if r["usage"] == "走道":
            continue
        u = unit_of.get(r["id"], "")
        r["zone"] = ("西区" if u in ("U1", "U2") else "东区") if u else ("西区" if r["label"][0] < split else "东区")
    sch["zones"] = [{"id": "西区"}, {"id": "东区"}]
    polys = [(r["id"], Polygon(r["polygon"])) for r in rec["rooms"]]
    with open(conventions.pack_file("symbols.json"), encoding="utf-8") as fh:
        pack = json.load(fh)["types"]
    heights = {k: s.get("default_height", 0) for k, s in pack.items()}   # 都按默认安装高度，图上不出例外高度
    items = []

    rooms, opens, _ = plan()
    runs = wall_runs(rooms)
    pieces = wall_pieces(runs, opens)
    inner = [(i, g.buffer(-10.0)) for i, g in polys]
    room_of = lambda x, y: next((i for i, g in inner if g.contains(Point(x + ox, y + oy))), None)   # 点所在的房间（离墙面 10 以上）

    def put(key, name, x, y, rot):
        if not key.startswith("cn_switch"):   # 开关由 switch_spot 放在门边（离洞边 SWITCH_FROM_DOOR）
            xy = off_doors(x, y, opens, runs, room_of)
            if xy is None:
                return
            x, y = xy
        p = Point(x + ox, y + oy)
        rid = next((i for i, g in polys if g.contains(p)), None)
        if rid:
            items.append({"key": key, "name": name, "x": round(x + ox), "y": round(y + oy), "z": float(heights.get(key, 0)),
                          "rot": rot, "room": rid})
    for _, r in rooms:
        name, x0, x1, y0, y1, _g = r
        if name in ("主卫", "卫生间"):
            put("cn_toilet", "坐便器", x0 + 600, y1 - 150, 180)
            put("cn_floor_drain", "地漏", (x0 + x1) / 2, (y0 + y1) / 2, 0)
            put("cn_socket_splash", "防溅插座", x1 - 120, y0 + 600, 90)
            put("cn_nurse_call", "紧急呼叫按钮", x0 + 120, y0 + 700, -90)   # 适老化：卫生间紧急呼叫
        elif name == "厨房":
            put("cn_socket_splash", "防溅插座", x0 + 600, y0 + 120, 0)
            put("cn_socket_splash", "防溅插座", x1 - 600, y0 + 120, 0)
        elif name in ("主卧", "次卧", "卧室", "书房", "客厅"):
            put("cn_socket", "五孔插座", x0 + 120, (y0 + y1) / 2, -90)
            put("cn_socket", "五孔插座", x1 - 120, (y0 + y1) / 2 - 600, 90)
            sw = switch_spot(room_of((x0 + x1) / 2, (y0 + y1) / 2), opens, pieces, room_of)
            if sw:
                put("cn_switch_1", "单联开关", *sw)
    # 开关是贴墙的点位：离最近的墙（识别结果）超过 SWITCH_WALL_MAX 就是方案生成错了
    walls = [Polygon(w["polygon"]) for w in rec["walls"]]
    far = [(it["name"], it["x"], it["y"]) for it in items if it["key"].startswith("cn_switch")
           and min(w.distance(Point(it["x"], it["y"])) for w in walls) > SWITCH_WALL_MAX]
    if far:
        printing.log("failed", "switch off the wall", far)
        sys.exit(1)
    # 点位不落在门洞宽度及两侧 DOOR_CLEAR 以内（门扇、开启范围里）：off_doors 挪不开的已经不生成，还有就是方案生成错了
    wx = lambda it: (it["x"] - ox, it["y"] - oy)
    in_door = [(it["name"], it["x"], it["y"]) for it in items if door_at(*wx(it), opens, runs)]
    if in_door:
        printing.log("failed", "point in a doorway", in_door)
        sys.exit(1)
    sch["items"] = items
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(sch, fh, ensure_ascii=False, indent=1)
    return len(items)


SWITCH_FROM_DOOR = 200.0   # 开关中心离门洞边多远（mm）
SWITCH_OFF_FACE = 20.0     # 开关离墙面多远（mm）
SWITCH_HALF = 60.0         # 开关面板半宽（mm，86 见方的面板再留一点）：整个面板都在这面墙上、这间房里
SWITCH_WALL_MAX = 300.0    # 开关离最近的墙不超过这么远（mm）
ROT_INTO = {(0, 1): 0, (0, -1): 180, (1, 0): -90, (-1, 0): 90}   # 点位局部 +y（朝室内）的方向 → 放置转角（symbols.place_occurrence）
DOOR_CLEAR = 200.0         # 插座、地漏等点位离门洞边至少这么远（mm，沿墙量；门洞两侧墙面以内的都算，免得画在门扇、开启范围里）
DOOR_REACH = 300.0         # 离门所在那道墙的墙面这么近（mm）的点位才算贴着这道墙


def door_at(x, y, opens, runs):
    """(x, y)（墙轴线坐标）落在哪樘门（平开、推拉）的门洞宽度及两侧 DOOR_CLEAR 以内、离墙面不到 DOOR_REACH：返回那樘门的
    (沿墙方向下标 0/1, 门洞中心沿墙坐标, 半宽 + DOOR_CLEAR)，不在任何门洞旁返回 None。"""
    for _, o in opens:
        k, d, ox_, oy_, w = o[:5]
        if k not in ("door", "door2", "slide"):
            continue
        i = 0 if d == "h" else 1                       # 沿墙的坐标下标
        a0, c = (ox_, oy_) if d == "h" else (oy_, ox_)
        along, perp = (x, y) if d == "h" else (y, x)
        half = w / 2 + DOOR_CLEAR
        if abs(along - a0) < half - 1.0 and abs(perp - c) < thickness_at(runs, o) / 2 + DOOR_REACH:
            return i, a0, half
    return None


def off_doors(x, y, opens, runs, room_of):
    """点位落在门洞旁（door_at）时沿墙挪到门洞边 DOOR_CLEAR 以外（先往离门洞中心远的一侧），挪后还要在原来那间房里、不在
    别的门洞旁；挪不开返回 None（不生成这个点位）。不在门洞旁原样返回。"""
    hit = door_at(x, y, opens, runs)
    if hit is None:
        return x, y
    i, a0, half = hit
    a = (x, y)[i]
    rid = room_of(x, y)
    for s in ((1, -1) if a >= a0 else (-1, 1)):
        p = [x, y]
        p[i] = a0 + s * half
        if rid is not None and room_of(*p) == rid and door_at(*p, opens, runs) is None:
            return p[0], p[1]
    return None


def switch_spot(rid, opens, pieces, room_of):
    """单联开关的位置 (x, y, 转角)（墙轴线坐标）：识别出的房间 rid（户内连通的客厅、餐厅、过道算一间）的平开门里开向这间房的
    那樘（房门、户门），门洞边锁舌一侧的墙上、离洞边 SWITCH_FROM_DOOR，离墙面 SWITCH_OFF_FACE；锁舌一侧没有墙或面板不全在
    这间房里（门挨着墙角）换铰链一侧。没有这样的门（阳台推拉门不算）返回 None，不生成开关。room_of(x, y)：点所在的房间 id。"""
    def wall_at(x, y, d):   # 过 (x, y)、沿 d 向（门所在那道墙）的墙段（矩形），没有（是洞口或没有墙）返回 None
        c = y if d == "h" else x
        return next((pc for pc in pieces if pc[0] < x < pc[2] and pc[1] < y < pc[3]
                     and abs((pc[1] + pc[3] if d == "h" else pc[0] + pc[2]) / 2 - c) < 1e-6), None)
    for _, o in opens:
        k, d, x, y, w, _lab, side = o[:7]
        sv = SIDE[side]
        if rid is None or k not in ("door", "door2") or room_of(x + sv[0] * 250, y + sv[1] * 250) != rid:
            continue
        ax = (1, 0) if d == "h" else (0, 1)            # 沿墙方向
        latch = (o[7] if len(o) > 7 else 1) if k == "door" else 1   # 单扇门的铰链在洞口 -ax 一端（X 比例为负时在 +ax 一端）
        for s in (latch, -latch):
            t = s * (w / 2 + SWITCH_FROM_DOOR)
            px, py = x + ax[0] * t, y + ax[1] * t
            pc = wall_at(px, py, d)
            if pc is None:
                continue
            off = (pc[3] - pc[1] if d == "h" else pc[2] - pc[0]) / 2 + SWITCH_OFF_FACE   # 墙厚的一半 + 离墙面
            qx, qy = px + sv[0] * off, py + sv[1] * off
            if all(room_of(qx + ax[0] * e, qy + ax[1] * e) == rid for e in (-SWITCH_HALF, 0.0, SWITCH_HALF)):
                return qx, qy, ROT_INTO[sv]
    return None


def main():
    p = argparse.ArgumentParser(prog="python tools/make_demo_floor.py", allow_abbrev=False,
                                description="合成的住宅标准层（公开示例）：写 floor.dxf；加 --scheme 时读 recog.json 写 scheme.json。")
    p.add_argument("out", metavar="输出目录")
    p.add_argument("--scheme", action="store_true", help="读 <输出目录>/recog.json，写 <输出目录>/scheme.json（两个分区 + 水电点位）")
    a = p.parse_args()
    recog = os.path.join(a.out, "recog.json")
    if a.scheme and not os.path.isfile(recog):
        p.error(f"文件不存在：{recog}（先用 dxfplan 识别 floor.dxf）")
    if a.scheme and (err := conventions.check_pack(("symbols.json",))):   # 点位的默认安装高度
        p.error(err)
    if a.scheme:
        out = os.path.join(a.out, "scheme.json")
        if os.path.exists(out):   # 出错退出时不留上一次的 scheme.json
            os.remove(out)
        try:   # 和 make_plan_set 同样的检查：识别结果是 JSON 对象，认出了房间和墙
            with open(recog, encoding="utf-8") as fh:
                rec = json.load(fh)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            p.error(f"不是识别结果 JSON：{recog}")
        if not rec.get("rooms") or not rec.get("walls"):
            p.error(f"识别结果里没有房间或墙：{recog}（检查 dxfplan 的 --region）")
        printing.log("items", write_scheme(recog, out, rec))
    else:
        os.makedirs(a.out, exist_ok=True)
        out = os.path.join(a.out, "floor.dxf")
        printing.log("region", *write_dxf(out))
    printing.done(out)


if __name__ == "__main__":
    main()
