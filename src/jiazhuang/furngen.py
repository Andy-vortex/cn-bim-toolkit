"""按尺寸参数生成的中国常用家具（furniture.json 里键以 cn_ 开头的类型）。几种画法（recipe）覆盖全部型号：
  box      素面（茶几）
  cabinet  带门线的柜体（电视柜、鞋柜、书柜；衣柜加挂衣杆）
  sofa     分座沙发，return > 0 时带转角（side 一侧）
  bed      床头板、枕头（宽 1200 以上两个）、被子折线
  counter  一字/L 形台面：橱柜的水槽、灶台位置（sink、hob）、浴室柜的台盆位置（basin）是参数，都从台面左端（家具自己的左）
           量到中心，一字、L 形一样
  shower   方形淋浴房：挡水、斜向玻璃门线、地漏
局部坐标同 furnish：靠墙边中点为原点，x 沿墙，+y 朝室内，毫米。shape() 只用标准库（.venv 里校验器从参数算外包和使用面）；
create_type() 在 Blender 里建 IFC 类型：三维实体用 ifcopenshell.util.shape_builder 把外轮廓拉伸（床头板单独一块、自己的高），
平面二维线用 IfcPolyline，
二维外框和实体外框是同一个轮廓。
"""
from __future__ import annotations

import math


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _closed(ring):
    return list(ring) + [ring[0]]


def _circle(cx, cy, r, n=24):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def _ellipse(cx, cy, rx, ry, n=32):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def _l_outline(w, d, ret):
    """一字（ret=0）或在左端带一条进深同为 d、从墙伸出 ret 的转角段的外轮廓。"""
    if ret <= d:
        return _rect(-w / 2, 0, w / 2, d)
    return [(-w / 2, 0), (w / 2, 0), (w / 2, d), (-w / 2 + d, d), (-w / 2 + d, ret), (-w / 2, ret)]


def shape(spec: dict, p: dict) -> dict:
    """类型数据 + 参数 → {outline: 外轮廓点列, lines: 二维线（折线列表）, use: 使用面矩形 [x0,y0,x1,y1] 列表, width, depth,
    height（最高处）, parts: 实体 [(轮廓, 高)]}。类型写了 back_height、back_depth 的（床头板），靠墙那一条单独一块。
    转角在右侧的（side=right）先按左侧算，再左右镜像。"""
    r, w, d = spec["recipe"], float(p["width"]), float(p["depth"])
    ret = float(p.get("return") or 0)
    use = spec.get("use", {})
    out = _l_outline(w, d, ret)
    lines = [_closed(out)]
    x_in = -w / 2 + (d if ret > d else 0)   # 正面从哪里开始（转角段让出去）
    if r == "cabinet":
        lines.append([(-w / 2, d - 20), (w / 2, d - 20)])                        # 门线
        n = max(1, round(w / 600))
        lines += [[(-w / 2 + i * w / n, d - 20), (-w / 2 + i * w / n, d)] for i in range(1, n)]
        if spec.get("rod"):                                                     # 挂衣杆 + 衣架
            lines.append([(-w / 2 + 50, d / 2), (w / 2 - 50, d / 2)])
            k = max(1, int((w - 200) // 300))
            lines += [[(-w / 2 + 100 + i * (w - 200) / k - 60, d / 2 - 180), (-w / 2 + 100 + i * (w - 200) / k + 60, d / 2 + 180)]
                      for i in range(k + 1)]
    elif r == "sofa":
        arm, back = 150.0, 200.0
        xs0 = x_in if ret > d else -w / 2 + arm
        lines.append([(-w / 2 + (back if ret > d else arm), back), (w / 2 - arm, back)])  # 靠背
        lines.append([(w / 2 - arm, back), (w / 2 - arm, d)])                           # 扶手
        if ret > d:
            lines.append([(-w / 2 + back, back), (-w / 2 + back, ret - arm)])
            lines.append([(-w / 2, ret - arm), (-w / 2 + d, ret - arm)])
            n = max(1, round((ret - arm - d) / 700))
            lines += [[(-w / 2 + back, d + i * (ret - arm - d) / n), (-w / 2 + d, d + i * (ret - arm - d) / n)] for i in range(1, n)]
        else:
            lines.append([(-w / 2 + arm, back), (-w / 2 + arm, d)])
        n = max(1, round((w / 2 - arm - xs0) / 700))
        lines += [[(xs0 + i * (w / 2 - arm - xs0) / n, back), (xs0 + i * (w / 2 - arm - xs0) / n, d)] for i in range(1, n)]
    elif r == "bed":
        lines.append([(-w / 2, 100), (w / 2, 100)])                                    # 床头板
        n = 1 if w < 1200 else 2
        pw = (w - 200 - (n - 1) * 100) / n
        lines += [_closed(_rect(-w / 2 + 100 + i * (pw + 100), 180, -w / 2 + 100 + i * (pw + 100) + pw, 500)) for i in range(n)]
        lines.append([(-w / 2, 0.4 * d + 300), (w / 2, 0.4 * d + 300)])                # 被子折线
    elif r == "counter":
        if "basin" in p:                                                              # 浴室柜：台盆
            bx = -w / 2 + (p["basin"] if p.get("basin") is not None else w / 2)
            lines.append(_ellipse(bx, d / 2 + 10, min(0.3 * w, 230), 0.3 * d))
        else:                                                                         # 橱柜：水槽、灶台
            pos = positions(p)
            m = -1 if p.get("side") == "right" else 1   # 镜像前的坐标：右侧转角的最后整体镜像
            sx, hx = m * (-w / 2 + pos["sink"]), m * (-w / 2 + pos["hob"])
            lines += [_closed(_rect(sx - 350, d / 2 - 210, sx + 350, d / 2 + 210)), _closed(_rect(sx - 320, d / 2 - 180, sx + 320, d / 2 + 180))]
            lines += [_closed(_rect(hx - 350, d / 2 - 220, hx + 350, d / 2 + 220)), _circle(hx - 170, d / 2, 100), _circle(hx + 170, d / 2, 100)]
    elif r == "shower":
        lines.append(_closed(_rect(-w / 2 + 50, 50, w / 2 - 50, d - 50)))
        lines.append([(-w / 2 + 50, d - 50), (w / 2 - 50, 50)])
        lines.append(_circle(w / 2 - 150, 150, 40, 12))
    elif r != "box":
        raise ValueError(f"没有这种画法 {r}")
    rects = []
    if use.get("front"):
        f = float(use["front"])
        rects.append([x_in, d, w / 2, d + f])
        if ret > d:
            rects.append([-w / 2 + d, d, -w / 2 + d + f, ret])
    if use.get("sides"):
        s, y0 = float(use["sides"]), float(use.get("from", 0))
        rects += [[-w / 2 - s, y0, -w / 2, d], [w / 2, y0, w / 2 + s, d]]
    if p.get("side") == "right":   # 镜像
        out = [(-x, y) for x, y in reversed(out)]
        lines = [[(-x, y) for x, y in ln] for ln in lines]
        rects = [[-r_[2], r_[1], -r_[0], r_[3]] for r_ in rects]
    h, parts = float(spec["height"]), [(out, float(spec["height"]))]
    if spec.get("back_height"):   # 靠墙一条更高（床头板）：两块，各自的高
        bd = float(spec["back_depth"])
        parts = [([(x, max(y, bd)) for x, y in out], h), (_rect(-w / 2, 0, w / 2, bd), float(spec["back_height"]))]
        h = max(h, parts[1][1])
    return {"outline": out, "lines": lines, "use": rects, "width": w, "depth": d, "height": h, "parts": parts}


def _run(p):
    """橱柜正面一段（不含转角段）从左端量的起止。"""
    w, d, ret = p["width"], p["depth"], p.get("return") or 0
    corner = ret > d
    return (d if corner and p.get("side") != "right" else 0.0), (w - d if corner and p.get("side") == "right" else w)


def positions(p: dict) -> dict:
    """水槽、灶台、台盆中心离台面左端（家具自己的左）多远：一字、L 形同一个量法。没写的：台盆居中，水槽、灶台分在正面那一段
    的两头（各离端头 350 以上）。"""
    lo, hi = _run(p)
    edge = max(350.0, 0.25 * (hi - lo))
    out = {"sink": p.get("sink"), "hob": p.get("hob"), "basin": p.get("basin")}
    out["sink"] = lo + edge if out["sink"] is None else out["sink"]
    out["hob"] = hi - edge if out["hob"] is None else out["hob"]
    out["basin"] = p["width"] / 2 if out["basin"] is None else out["basin"]
    return out


def check(spec: dict, p: dict) -> str:
    """参数之间的约束（范围在 furnish.resolve 里查过）：台面（L 形是正面一段）放得下、水槽、灶台、台盆要在台面上
    （不进转角段）、水槽和灶台不挤在一起。
    没问题返回 ""。"""
    w, d, ret = p["width"], p["depth"], p.get("return") or 0
    if ret and ret <= d:
        return f"return 要大于进深 {d:.0f}（0 表示一字）"
    if spec["recipe"] != "counter":
        return ""
    lo, hi = _run(p)
    pos = positions(p)
    need = 460 if "basin" in p else 350 + 700 + 350   # 台盆宽 460；水槽、灶台各离端头 350、中心隔 700
    if hi - lo < need:
        return f"台面太短：{'' if lo == 0 and hi == w else '正面一段'}{hi - lo:.0f}，至少要 {need}"
    keys = (("basin", 230, 0.0, w),) if "basin" in p else (("sink", 350, lo, hi), ("hob", 350, lo, hi))
    for k, half, a, b in keys:
        if not (a + half <= pos[k] <= b - half):
            return f"{k} {pos[k]:.0f} 不在台面上（从左端量 {a + half:.0f}～{b - half:.0f}）"
    if "sink" in p and abs(pos["sink"] - pos["hob"]) < 700:
        return f"水槽和灶台中心至少隔 700（现在 {abs(pos['sink'] - pos['hob']):.0f}）"
    return ""


def create_type(f, spec: dict, p: dict, name: str):
    """建一个 IFC 类型（Blender 里）：Body 是每块（parts）的轮廓各自拉伸到自己的高，Plan/Body 是二维线（IfcPolyline）。"""
    import ifcopenshell.api as api
    from ifcopenshell.util.shape_builder import ShapeBuilder
    from .symbols import get_or_add_context
    s = shape(spec, p)
    body = get_or_add_context(f, "Model", "Body", "MODEL_VIEW")
    plan = get_or_add_context(f, "Plan", "Body", "PLAN_VIEW")
    b = ShapeBuilder(f)
    solids = [b.extrude(b.profile(b.polyline([(float(x), float(y)) for x, y in ring], closed=True)), magnitude=h) for ring, h in s["parts"]]
    t = api.root.create_entity(f, ifc_class=spec["class"], name=name, predefined_type=spec["predefined"])
    api.geometry.assign_representation(f, product=t, representation=b.get_representation(body, solids))
    curves = [f.createIfcPolyline([f.createIfcCartesianPoint((float(x), float(y))) for x, y in ln]) for ln in s["lines"]]
    api.geometry.assign_representation(f, product=t, representation=f.createIfcShapeRepresentation(plan, "Body", "Curve2D", curves))
    return t
