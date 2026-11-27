"""室内立面（在 Blender + Bonsai 里运行）：方案的墙面数据（scripts/wall.py 写的 walls）里要出立面的墙（elev 为真、不是开敞边），
每面墙一张立面视图（Bonsai 的 ELEVATION_VIEW），画两道横向尺寸（洞口分段、总长）、一道竖向尺寸（楼面、窗台、洞口顶、吊顶底、
原顶，按这面墙实际有的）、楼面和吊顶底的标高、原顶粗线、墙上点位距地高度（H300）、墙面材料与踢脚线引出标注、踢脚线填充和
墙砖排砖缝（减去家具投影并在文字与点位处断开）；多面墙的房间（厨卫等）单独成张，1～2 面墙的房间合并排在 A3（2×2）上
（drawset.pack_views、make_views_sheet）；平面图上每间出了立面的房间画内视符号（圆 + 实心方向三角 + 字母 + 立面图号）。
出哪些墙、立面字母都取墙面数据（wall.py：elevations.mode、朝北那面为 A、顺时针），这里不另算。

立面坐标系 (u, v, w)：面向墙看，u 从墙面左端往右量，v 从本层楼面往上量，w 指向室内，原点在墙面左端楼面处（米）。和墙面数据的
(u, v) 同一套（那边是毫米）。墙边编号逆时针走，起点 a 在右端（u = 墙长）、终点 b 在左端：立面的左右和墙边的起止方向相反。
标注函数按 (u, v) 写，视图坐标系登记给 annotate（set_frame），同一套尺寸、标高、引出线函数在立面上原样能用。

相机朝向任意，直接写相机矩阵（x 沿墙、y 向上、z 朝室内，相机往 -z 看）：放在房间里离墙面 depth 处，相机平面就是剖切面，两侧有
垂直实墙时在这里剖到（阳角、开敞或剖在洞口处不外伸）。depth 按去掉门洞条后的墙边（furnish.rooms_of）量到对面墙前
depth_gap_mm：按识别出的房间轮廓量的话，门洞条让房间"变深"，相机落进对面墙里，整面画成剖到的墙。L 形房间取墙面前面一整条
都还在房间里的深度。
只画本房间：EPset_Drawing.Include 只列本房间的墙、门窗、面层，加上本房间里离这面墙线不超过 near_mm 的家具、洁具、电器、点位
和壁灯（GlobalId），Bonsai 再和相机范围取交集。只靠相机范围不够：相机盒子里有对面墙上的柜子、点位（从背后画出来），左右留给
标注的边里有隔壁房间的东西。GlobalReferencing 关掉、Exclude 加楼层和轴网：开着的话 Bonsai 在别的正交视图激活时自动给它们加
立面索引（现有各张平面图都会变），立面里也会自动加楼层标高线、轴线。
模型里没有楼板，墙一直画到层高：裁切框顶放在原顶净高，另画一道粗线当原顶；平顶吊顶以上用白底遮罩盖住墙段接缝。吸顶灯、筒灯、
灯带不画，壁灯照画。"""
from __future__ import annotations

import math

import ifcopenshell.geom
import ifcopenshell.util.element as uel
import numpy as np
import shapely
import shapely.affinity
from mathutils import Matrix
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box as sbox
from shapely.ops import unary_union

from . import annotate, ceiling, conventions, drawset, flooring, furnish, wall
from .printing import log

E = conventions.get("elevation")
DEN = int(E["scale"])
PSET = "JZ_Elevation"   # 立面视图的属性集：Room、RoomName、Wall、Letter…（重开 IFC 按它认出是哪间房哪面墙）
SKIP_CLASSES = ("IfcWall", "IfcDoor", "IfcWindow", "IfcCovering", "IfcOpeningElement", "IfcVirtualElement",
                "IfcBuildingElementProxy")
CEIL_LIGHT_TAGS = ("cn_light_ceiling", "cn_downlight", "cn_light_strip")
POINT_CLASSES = ("IfcOutlet", "IfcSwitchingDevice", "IfcElectricDistributionBoard", "IfcJunctionBox", "IfcValve", "IfcAlarm")
CALLOUT_CLASS = "jz-callout"
CALLOUT_RING_CLASS = "jz-callout-ring"
CALLOUT_TRI_CLASS = "jz-callout-tri"
PLENUM_CLASS = "jz-plenum-mask"
SKIRTING_CLASS = "jz-skirting"
WALLJOINT_CLASS = "jz-walljoint"


def walls_of(sch: dict) -> list:
    """方案里要出立面的墙，按房间分组：[(房间 id, [墙面数据…])]，房间按方案 walls 的顺序，一间房里按字母排。"""
    out = {}
    for w in sch.get("walls") or []:
        if w.get("elev") and not w.get("open"):
            out.setdefault(w["room"], []).append(w)
    return [(rid, sorted(ws, key=lambda w: (len(w["letter"]), w["letter"]))) for rid, ws in out.items()]


def skipped(sch: dict) -> list:
    """不出立面的墙：[(房间 id, 墙边, 字母, 原因)]。"""
    return [(w["room"], w["wall"], w["letter"], "开敞" if w.get("open") else "不出")
            for w in sch.get("walls") or [] if w.get("open") or not w.get("elev")]


def _room_labels(rooms: dict) -> dict:
    """房间 id → 显示房名（同名房间按房间顺序加 1、2，如"次卧1"，不写内部房间编号）。"""
    count, labels = {}, {}
    for rm in rooms.values():
        count[rm["name"]] = count.get(rm["name"], 0) + 1
    seen = {}
    for rm in rooms.values():
        seen[rm["name"]] = seen.get(rm["name"], 0) + 1
        labels[rm["id"]] = rm["name"] if count[rm["name"]] == 1 else f"{rm['name']}{seen[rm['name']]}"
    return labels


def _by_room_wall(sch: dict) -> dict:
    """{房间 id: {墙边 id: 墙面数据}}，供判断相邻墙边是否开敞、是否有洞口。"""
    out = {}
    for w in sch.get("walls") or []:
        out.setdefault(w["room"], {})[w["wall"]] = w
    return out


def _side_widths(w: dict, ring: list, by_wid: dict, d_mm: float) -> tuple[float, float]:
    """一面立面左右两侧剖切墙露出的宽度 (tl, tr)（米）：只有室内阴角、相邻墙边非开敞且相机剖切面（离本墙 d_mm）确实落在相邻
    实墙段上（未落进洞口或开敞段）时，那一侧才露出 side_mm；阳角或无实墙剖到时取 0，避免顶线与裁切框悬空外伸。"""
    T = float(E["side_mm"]) / 1000.0
    idx = next((i for i, rw in enumerate(ring) if rw["id"] == w["wall"]), None)
    if idx is None:
        return T, T
    cur = ring[idx]
    nvec = np.array(cur["n"], float)
    n = len(ring)

    def _solid_at(other_rw, other_e, s_from_corner: float, from_a: bool) -> bool:
        if other_e is None or other_e.get("open"):
            return False
        oL = float(other_e["length"])
        if not (0.0 <= s_from_corner <= oL + 1.0):
            return False
        u_pos = oL - s_from_corner if from_a else s_from_corner
        for o in other_e.get("openings") or []:
            if float(o["from"]) + 1.0 < u_pos < float(o["to"]) - 1.0:
                return False
        for u0, u1 in other_e.get("open_spans") or []:
            if float(u0) + 1.0 < u_pos < float(u1) - 1.0:
                return False
        return True

    # 左端（u = 0，顶点 b）：逆时针下一面墙 nw，其起点 nw["a"] == cur["b"]，沿 +nw["u"] 伸入室内
    nw = ring[(idx + 1) % n]
    nw_e = by_wid.get(nw["id"])
    cos_l = float(np.dot(np.array(nw["u"], float), nvec))
    has_left = (wall._turn(cur, nw) > 1e-3 and cos_l > 0.5
                and not any(float(u0) < 1.0 for u0, _ in (w.get("open_spans") or []))
                and _solid_at(nw, nw_e, d_mm / cos_l, from_a=True))

    # 右端（u = L，顶点 a）：逆时针前一面墙 pw，其终点 pw["b"] == cur["a"]，沿 -pw["u"] 伸入室内
    pw = ring[(idx - 1) % n]
    pw_e = by_wid.get(pw["id"])
    cos_r = float(np.dot(-np.array(pw["u"], float), nvec))
    has_right = (wall._turn(pw, cur) > 1e-3 and cos_r > 0.5
                 and not any(float(u1) > float(w["length"]) - 1.0 for _, u1 in (w.get("open_spans") or []))
                 and _solid_at(pw, pw_e, d_mm / cos_r, from_a=False))

    return (T if has_left else 0.0), (T if has_right else 0.0)


def view_rect(w: dict, tl: float | None = None, tr: float | None = None) -> tuple:
    """一面立面的 (rect, paper_size_mm, vb)：相机范围 (u0, v0, u1, v1)（米）、图纸毫米 (宽, 高)、楼面以下裁切底 vb（米）。
    建视图（make_views）和预先排立面图号（plan_sheets）共用这一个函数，排版结果一致。"""
    T = float(E["side_mm"]) / 1000.0
    tl = T if tl is None else float(tl)
    tr = T if tr is None else float(tr)
    ml, mr, mb, mt = (float(m) * DEN / 1000.0 for m in E["margins_mm"])
    L, H = w["length"] / 1000.0, w["height"] / 1000.0
    vb = (min(float(w["bottom"]), 0.0) - float(E["below_mm"])) / 1000.0
    rect = (-tl - ml, vb - mb, L + tr + mr, H + mt)
    size = ((rect[2] - rect[0]) * 1000.0 / DEN, (rect[3] - rect[1]) * 1000.0 / DEN)
    return rect, size, vb


def _pack_balanced(sizes: list, cols: int, rows: int, gap_mm: float) -> list:
    """把一组视图按 2×2 排进若干张 A3；多于一张且最后一张只有 1 个视图时，尝试把最后两张的视图匀成 (n-2, 2)（只要每张仍放得下），
    避免最后一张只孤零零落一个视图。"""
    packs = drawset.pack_views(sizes, cols, rows, gap_mm)
    if len(packs) >= 2 and sum(len(r) for r in packs[-1]) == 1:
        a_idx = [i for r in packs[-2] for i in r]
        b_idx = [i for r in packs[-1] for i in r]
        comb = a_idx + b_idx
        if len(comb) >= 4:
            split = len(comb) - 2
            p1 = drawset.pack_views([sizes[i] for i in comb[:split]], cols, rows, gap_mm)
            p2 = drawset.pack_views([sizes[i] for i in comb[split:]], cols, rows, gap_mm)
            if len(p1) == 1 and len(p2) == 1:
                packs[-2] = [[comb[i] for i in r] for r in p1[0]]
                packs[-1] = [[comb[split + i] for i in r] for r in p2[0]]
    return packs


def _shared_title(room_labels: list) -> str:
    """拼合多间房在一张 A3 上的图名：标题栏两行放得下且不把单个房名拆行就列房名（"客厅、玄关立面图"），否则写"室内立面图"。"""
    uniq = list(dict.fromkeys(room_labels))
    if len(uniq) == 1:
        return f"{uniq[0]}立面图"
    cand = f"{'、'.join(uniq)}立面图"
    size_mm, max_lines = drawset.TB_WRAP["tb-title"]
    lines = drawset.wrap_text(cand, drawset.TB_TEXT_MM, size_mm)
    if len(lines) <= max_lines and all(any(r in ln for ln in lines) for r in uniq):
        return cand
    return "室内立面图"


def _pack_room_sheets(room_entries: list) -> list:
    """把全部房间的立面条目排成图纸序列：[(图名, [[条目, …] 各行])]，plan_sheets() 与 sheets() 共用。
    room_entries：[(rid, label, [(item, size_mm), …])]。
    墙面数 > share_max_walls（默认 2，即厨卫等 3～4 面墙的房间）一间房单独成张；1～2 面墙的房间合并排在 2×2 A3 上，
    减少单视图空页。"""
    cols, rows, gap_mm = int(E["cols"]), int(E["rows"]), float(E["gap_mm"])
    share_max = int(E.get("share_max_walls", 2))
    dedicated = [e for e in room_entries if len(e[2]) > share_max]
    shared = [e for e in room_entries if len(e[2]) <= share_max]
    raw = []   # [(base_title, [[item, …] 各行], is_single_room_split_group)]
    for rid, label, entries in dedicated:
        packs = drawset.pack_views([s for _, s in entries], cols, rows, gap_mm)
        for k, pk in enumerate(packs, 1):
            title = f"{label}立面图" + (f"（{k}/{len(packs)}）" if len(packs) > 1 else "")
            raw.append((title, [[entries[i][0] for i in r] for r in pk], False))
    if shared:
        flat = [(label, item, sz) for _, label, entries in shared for item, sz in entries]
        packs = _pack_balanced([sz for _, _, sz in flat], cols, rows, gap_mm)
        # 统计需要用通用名"室内立面图"的张数，超过一张时编号（1/2）
        titles = []
        for pk in packs:
            rls = [flat[i][0] for r in pk for i in r]
            titles.append(_shared_title(rls))
        counts = {}
        for t in titles:
            counts[t] = counts.get(t, 0) + 1
        seen = {}
        for t, pk in zip(titles, packs):
            seen[t] = seen.get(t, 0) + 1
            ft = f"{t}（{seen[t]}/{counts[t]}）" if counts[t] > 1 else t
            raw.append((ft, [[flat[i][1] for i in r] for r in pk], True))
    return [(t, rs) for t, rs, _ in raw]


def plan_sheets(rec: dict, sch: dict, start_num: int, prefix: str) -> dict:
    """预先按与 sheets() 相同的排版算法算出立面图号，供平面图画内视符号使用（此时立面视图尚未创建）。
    返回 {房间 id: [(墙面数据 w, 立面图号如 'A-12'), …]}。"""
    rooms = {rm["id"]: rm for rm in furnish.rooms_of(rec, sch)}
    labels = _room_labels(rooms)
    by_rw = _by_room_wall(sch)
    room_entries = []
    for rid, ws in walls_of(sch):
        rm = rooms.get(rid)
        if rm is None:
            continue
        ring_poly = Polygon([rw["a"] for rw in rm["walls"]])
        entries = []
        for w in ws:
            d_mm = depth_mm(w, ring_poly)
            tl, tr = _side_widths(w, rm["walls"], by_rw.get(rid, {}), d_mm)
            _, size_mm, _ = view_rect(w, tl, tr)
            entries.append(((rid, w), size_mm))
        room_entries.append((rid, labels[rid], entries))
    packed = _pack_room_sheets(room_entries)
    out = {}
    n = start_num
    for _, rows_items in packed:
        ident = f"{prefix}-{n:02d}"
        n += 1
        for r in rows_items:
            for rid, w in r:
                out.setdefault(rid, []).append((w, ident))
    return out


def frame(w: dict, ox: float, oy: float, z0: float) -> Matrix:
    """一面墙的立面坐标系（4×4，米，模型坐标）：x 从墙面左端（墙边终点 b）指向右端（起点 a），y 竖直向上，z 是室内法线
    （x 的右手系：(x_y, -x_x)），原点在 b、本层楼面。"""
    bx, by = (w["b"][0] - ox) / 1000.0, (w["b"][1] - oy) / 1000.0
    ax, ay = (w["a"][0] - ox) / 1000.0, (w["a"][1] - oy) / 1000.0
    L = math.hypot(ax - bx, ay - by)
    x = ((ax - bx) / L, (ay - by) / L)
    n = (x[1], -x[0])
    return Matrix(((x[0], 0.0, n[0], bx), (x[1], 0.0, n[1], by), (0.0, 1.0, 0.0, z0), (0.0, 0.0, 0.0, 1.0)))


def depth_mm(w: dict, ring: Polygon) -> float:
    """相机离墙面多远（毫米）：墙面前面一整条（两端各让 5）还在房间里的最大深度，再让 depth_gap_mm。"""
    a, b = np.array(w["a"], float), np.array(w["b"], float)
    L = float(np.linalg.norm(a - b))
    x = (a - b) / L
    n = np.array([x[1], -x[0]])
    full = max(float((np.array(p) - b) @ n) for p in ring.exterior.coords)
    room = ring.buffer(2.0)
    p0, p1 = b + x * 5.0, a - x * 5.0
    inside = lambda d: room.contains(Polygon([p0, p1, p1 + n * d, p0 + n * d]))
    if inside(full):
        d = full
    else:
        lo, hi = 0.0, full
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if inside(mid) else (lo, mid)
        d = lo
    return max(d - float(E["depth_gap_mm"]), 100.0)


def thickness_mm(w: dict, geo: dict) -> float:
    """墙边背后那道墙的厚（毫米）：识别结果里和这条墙边贴着（重合 50 以上）的墙段取最厚的；没有（开敞）按 200。"""
    seg = LineString([w["a"], w["b"]])
    ts = [float(x["thickness"]) for x in geo["walls"] if len(x["polygon"]) >= 3
          and Polygon(x["polygon"]).buffer(5.0).intersection(seg).length > 50.0]
    return max(ts) if ts else 200.0


def _mesh_feature_edges(verts: np.ndarray, faces: np.ndarray) -> list:
    """三维三角网格的轮廓边和折棱边（二面角 > 20°）：[(顶点序号 a, b)]，用于投影到立面 (u, v) 做文字避让。"""
    if len(faces) == 0:
        return []
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(fn, axis=1)
    ok = ln > 1e-9
    fn[ok] /= ln[ok, None]
    adj = {}
    for fi in np.nonzero(ok)[0]:
        a, b, c = int(faces[fi, 0]), int(faces[fi, 1]), int(faces[fi, 2])
        for p, q in ((a, b), (b, c), (c, a)):
            k = (p, q) if p < q else (q, p)
            adj.setdefault(k, []).append(int(fi))
    out = []
    for k, fs in adj.items():
        if len(fs) == 1 or any(float(np.dot(fn[fs[0]], fn[f2])) < 0.94 for f2 in fs[1:]):
            out.append(k)
    return out


class _Room:
    """一间房在 IFC 里的东西：墙、门窗、面层（GlobalId），房间里的家具、洁具、电器、壁灯、点位的平面轮廓与三维网格（米，模型坐标）。"""

    def __init__(self, f, rid: str, poly_m: Polygon):
        geo = annotate.plan_geometry(f)
        self.walls = []
        for e in f.by_type("IfcWall"):
            if uel.get_pset(e, "Pset_WallCommon", "Status") == "DEMOLISH":
                continue
            fp = geo.fp(e)
            if fp is not None and fp.distance(poly_m) <= 0.03:
                self.walls.append(e.GlobalId)
        rooms_of = lambda e: (uel.get_pset(e, "JZ_Opening", "Rooms") or "").split(",")
        self.openings = []   # [(GlobalId, 平面上的洞口中心（米）)]
        for e in list(f.by_type("IfcDoor")) + list(f.by_type("IfcWindow")):
            if rid in rooms_of(e):
                fr, fp = geo.frame(e), geo.fp(e)
                c = fr["c"] if fr is not None else (fp.centroid.coords[0] if fp is not None else None)
                if c is not None:
                    self.openings.append((e.GlobalId, (float(c[0]), float(c[1]))))
        self.coverings = []
        for c in f.by_type("IfcCovering"):
            ps = uel.get_psets(c)
            if ps.get("JZ_WallFinish", {}).get("Room") == rid or ps.get("JZ_Ceiling", {}).get("Room") == rid \
                    or rid in (ps.get("JZ_Flooring", {}).get("Rooms") or "").split(","):
                self.coverings.append(c.GlobalId)
        self.items = {}
        for e in f.by_type("IfcElement"):
            if any(e.is_a(c) for c in SKIP_CLASSES):
                continue
            if e.is_a("IfcLightFixture"):
                tag = getattr(uel.get_type(e), "Tag", None) or ""
                if tag in CEIL_LIGHT_TAGS:
                    continue
            sp = uel.get_container(e)
            if sp is None or not sp.is_a("IfcSpace") or sp.Name != rid:
                continue
            try:
                g = ifcopenshell.geom.create_shape(annotate._GS, e).geometry
            except Exception as ex:   # 不静默吞掉：取不到几何的构件不进立面，记一行
                log("elevation shape failed", rid, e.is_a(), e.Name, type(ex).__name__)
                continue
            verts = np.array(g.verts, dtype=float).reshape(-1, 3)
            faces = np.array(g.faces, dtype=int).reshape(-1, 3) if g.faces else np.zeros((0, 3), dtype=int)
            fp = geo.fp(e)
            if fp is None or fp.is_empty:
                fp = MultiPoint(verts[:, :2]).convex_hull
            self.items[e.GlobalId] = (e, fp, verts, faces)

    def openings_before(self, F: Matrix, depth: float) -> list:
        """本房间的门窗里洞口中心在相机平面这一侧的（离墙面沿室内法线不超过 depth 米；这面墙上的门窗中心在墙厚里，是负的）。
        对面墙上的门窗在相机背后：门框、推拉门扇常伸出墙面几十毫米，比相机离对面墙面还近，不排除的话从背后画出来。"""
        o, n = F.col[3], F.col[2]
        return [g for g, (x, y) in self.openings if (x - o.x) * n.x + (y - o.y) * n.y <= depth - 0.01]

    def near(self, a_m, b_m) -> list:
        """离墙线 a～b（米）不超过 near_mm 的家具、洁具、电器、壁灯、点位：[(GlobalId, 名称)]。"""
        seg = LineString([a_m, b_m])
        lim = float(E["near_mm"]) / 1000.0
        return [(g, e.Name or e.is_a()) for g, (e, fp, _, _) in self.items.items() if fp.distance(seg) <= lim]

    def uv_geoms(self, gids: list, F: Matrix) -> tuple:
        """这面墙取到的家具、洁具、电器、壁灯（不含点位）投到立面坐标系 (u, v)（米）的精确二维投影：
        (轮廓多边形列表, 轮廓和折棱线段列表)。点位文字避让、墙砖缝与踢脚线减家具都用它。"""
        Fi = np.array(F.inverted(), dtype=float)
        R, t = Fi[:3, :3].T, Fi[:3, 3]
        polys, lines = [], []
        for g in gids:
            e, _, verts, faces = self.items[g]
            if any(e.is_a(c) for c in POINT_CLASSES) or len(verts) == 0:
                continue
            uv = (verts @ R + t)[:, :2]
            tris = []
            for a, b, c in faces:
                p = Polygon([uv[a], uv[b], uv[c]])
                if p.is_valid and p.area > 1e-6:
                    tris.append(p)
            if tris:
                u = unary_union(tris).buffer(0)
                if not u.is_empty:
                    polys.append(u)
            for a, b in _mesh_feature_edges(verts, faces):
                if math.hypot(uv[b, 0] - uv[a, 0], uv[b, 1] - uv[a, 1]) > 1e-4:
                    lines.append(LineString([tuple(uv[a]), tuple(uv[b])]))
        return polys, lines


# ---------------------------------------------------------------- 平面图上的四向内视符号
def _wall_n(w: dict) -> tuple[float, float]:
    """一面墙的室内法线单位向量（同 frame(w) 的 z 轴：由左端 b 指向右端 a 的向量顺时针转 90°）。"""
    ax, ay = float(w["a"][0]), float(w["a"][1])
    bx, by = float(w["b"][0]), float(w["b"][1])
    L = math.hypot(ax - bx, ay - by)
    ux, uy = (ax - bx) / L, (ay - by) / L
    return uy, -ux


def _split_callout_groups(wall_sheets: list) -> list:
    """一间房要出立面的墙按 (立面图号, 方向不重) 分组，返回 [(图号, [墙面数据…])]。
    一般一间房同在一张图纸、四向不同，合成一个符号；跨两张图纸，或 L 形房间同一方向有两面墙（夹角 < 25°）时拆成多个符号。"""
    by_sheet = {}
    for w, ident in wall_sheets:
        by_sheet.setdefault(ident, []).append(w)
    groups = []
    for ident, ws in by_sheet.items():
        buckets = []
        for w in ws:
            nx, ny = _wall_n(w)
            dx, dy = -nx, -ny
            placed = False
            for bk in buckets:
                if all(dx * (-_wall_n(o)[0]) + dy * (-_wall_n(o)[1]) < math.cos(math.radians(25.0)) for o in bk):
                    bk.append(w)
                    placed = True
                    break
            if not placed:
                buckets.append([w])
        for bk in buckets:
            groups.append((ident, bk))
    return groups


def _callout_parts(ident: str, ws: list, den: int) -> tuple:
    """以 (0, 0) 为圆心构造一个内视符号的相对几何（米）：
    (圆半径 r, [(字母, (dx, dy), 弧边三角点列, (lx, ly), 字母框)], 图号字框, 整组占地区域含留边)。
    圆直径按 conventions elevation.callout_r_mm（默认 4.0mm → 直径 8mm，JGJ/T 244 8～10mm）；
    实心三角完全画在圆周外侧（贴合圆弧，不侵入圆内），图号居中在圆内，字母不随箭头旋转。"""
    P = lambda mm: mm * den / 1000.0
    r = P(float(E["callout_r_mm"]))
    tip_h = P(float(E["callout_tip_mm"]))
    gap = P(float(E["callout_gap_mm"]))
    fs = annotate.FONT_MM[annotate.NUM]
    arrows = []
    shapes = [Point(0.0, 0.0).buffer(r)]
    for w in ws:
        nx, ny = _wall_n(w)
        dx, dy = -nx, -ny
        px, py = -dy, dx
        ang = math.radians(26.0)
        tip = (dx * (r + tip_h), dy * (r + tip_h))
        # 底边贴合圆周外沿（±26°、±13°、0° 五点圆弧），不侵入圆内挡住图号
        arc = []
        for t in (ang, ang / 2, 0.0, -ang / 2, -ang):
            ct, st = math.cos(t), math.sin(t)
            arc.append((r * (dx * ct + px * st), r * (dy * ct + py * st)))
        tri = [tip] + arc
        lt = w["letter"]
        tw, th = P(annotate.text_mm(lt, fs)), P(0.95 * fs)
        ld = r + tip_h + gap + abs(dx) * (tw / 2) + abs(dy) * (th / 2)
        lx, ly = dx * ld, dy * ld
        lbox = annotate._text_box(lt, annotate.NUM, den, lx, ly, "center", 0.0)
        arrows.append((lt, (dx, dy), tri, (lx, ly), lbox))
        shapes.append(Polygon(tri))
        shapes.append(lbox)
    sbox_id = annotate._text_box(ident, annotate.NUM, den, 0.0, 0.0, "center", 0.0)
    shapes.append(sbox_id)
    union = unary_union(shapes)
    return r, arrows, sbox_id, union


def _shift_geom(g, dx: float, dy: float):
    return shapely.affinity.translate(g, xoff=dx, yoff=dy)


def draw_callouts(f, drawing, rec: dict, sch: dict, offset, den: int, plan_map: dict, bbox_m=None, z: float = 0.0) -> int:
    """在平面图上为本视图里出了立面的房间画内视符号（圆 + 实心方向三角 + 字母 + 立面图号）。
    在 level_mark 之后、space_labels 之前调用：大房间优先避开房名标注点让房名留在原位；小房间（如 1:80 卫生间）优先占中心
    空位并把符号登记进 _OCC，后面的 space_labels 自动避开。返回画了几个符号。"""
    if not plan_map:
        return 0
    P = lambda mm: mm * den / 1000.0
    ox, oy = offset
    geo = annotate.plan_geometry(f)
    hard_list, _, _ = annotate._obstacles(f, drawing)
    syms = annotate.paper_shapes(f, drawing)
    hard_base = hard_list + syms
    soft_x = [p for p, _ in annotate.soft_shapes(f, drawing, paper=False, exact=True)]
    soft_lines = [p.boundary.buffer(P(0.25)) for p in soft_x if not p.is_empty]
    occ = annotate._OCC.setdefault(drawing.id(), [])
    spaces = {sp.Name: sp for sp in f.by_type("IfcSpace")}
    clip = sbox(*bbox_m).buffer(-P(annotate.CUT_GAP_MM), join_style="mitre") if bbox_m else None
    step = P(0.6)
    drawn = 0

    for rid, wall_sheets in plan_map.items():
        sp = spaces.get(rid)
        if sp is None:
            continue
        region = geo.fp(sp)
        if region is None or region.is_empty:
            continue
        if clip is not None:
            region = region.intersection(clip)
            if region.is_empty:
                continue
        ps = uel.get_psets(sp).get("JZ_Space", {})
        area = float(ps.get("NetArea_m2") or region.area)
        if "LabelX_mm" in ps and "LabelY_mm" in ps:
            lab_pt = Point(ps["LabelX_mm"] / 1000.0, ps["LabelY_mm"] / 1000.0)
        else:
            lab_pt = region.representative_point()
        room_in = region.buffer(-P(0.5))
        if room_in.is_empty:
            room_in = region

        for ident, ws in _split_callout_groups(wall_sheets):
            r, arrows, sbox_id, shape0 = _callout_parts(ident, ws, den)
            text_boxes0 = unary_union([sbox_id] + [a[4] for a in arrows])
            # 目标中心：多面墙优先房间中心；单面墙略偏向它指向的墙中点，同时避开房名
            cen = region.centroid if region.contains(region.centroid) else region.representative_point()
            if len(ws) == 1:
                w = ws[0]
                mx = ((w["a"][0] + w["b"][0]) / 2 - ox) / 1000.0
                my = ((w["a"][1] + w["b"][1]) / 2 - oy) / 1000.0
                nx, ny = _wall_n(w)
                bias = Point(mx + nx * max(r * 2.2, 0.5), my + ny * max(r * 2.2, 0.5))
                target = Point(0.55 * cen.x + 0.45 * bias.x, 0.55 * cen.y + 0.45 * bias.y)
                if not region.contains(target):
                    target = cen
            else:
                target = cen

            # 生成房间内候选格点（按离 target 由近到远）
            rx0, ry0, rx1, ry1 = room_in.bounds
            xs = np.arange(rx0 + r, rx1 - r + 1e-6, step) if rx1 - rx0 > 2 * r else np.array([(rx0 + rx1) / 2])
            ys = np.arange(ry0 + r, ry1 - r + 1e-6, step) if ry1 - ry0 > 2 * r else np.array([(ry0 + ry1) / 2])
            cands = []
            for x in xs:
                for y in ys:
                    pt = Point(float(x), float(y))
                    if room_in.contains(pt):
                        cands.append((math.hypot(float(x) - target.x, float(y) - target.y), float(x), float(y)))
            if not cands:
                cands = [(0.0, target.x, target.y)]
            cands.sort(key=lambda t: t[0])

            # 筛选本房间相关的障碍，加速判断
            near_box = region.buffer(P(4.0))
            h_obs = [g for g in hard_base + occ if g.intersects(near_box)]
            h_union = unary_union(h_obs) if h_obs else Polygon()
            s_obs = [g for g in soft_x if g.intersects(near_box)]
            s_union = unary_union(s_obs) if s_obs else Polygon()
            sl_obs = [g for g in soft_lines if g.intersects(near_box)]
            sl_union = unary_union(sl_obs) if sl_obs else Polygon()
            # 大房间（≥6㎡）：第一档同时避开房名标注点，让房名仍落在最自然的位置
            lab_keep = lab_pt.buffer(P(6.0)) if area >= 6.0 else Polygon()

            shape_pad = shape0.buffer(P(0.45))
            text_pad = text_boxes0.buffer(P(0.35))
            best = None
            # 档位：0 避硬障碍+家具+房名区；1 避硬障碍+家具；2 避硬障碍+家具轮廓线（白底可落，文字绝不压线）；3 最小重叠兜底
            for tier in (0, 1, 2):
                if tier == 0 and lab_keep.is_empty:
                    continue
                for _, cx, cy in cands:
                    sh = _shift_geom(shape_pad, cx, cy)
                    if not room_in.contains(sh):
                        continue
                    if not h_union.is_empty and sh.intersects(h_union):
                        continue
                    if tier in (0, 1) and not s_union.is_empty and sh.intersects(s_union):
                        continue
                    if tier == 0 and sh.intersects(lab_keep):
                        continue
                    if tier == 2:
                        tb = _shift_geom(text_pad, cx, cy)
                        if not sl_union.is_empty and (sh.intersects(sl_union) or tb.intersects(sl_union)):
                            continue
                    best = (cx, cy)
                    break
                if best is not None:
                    break
            if best is None:
                # 兜底：选房间内与硬障碍+轮廓线重叠最小的位置（优先文字不压任何线）
                def score(item):
                    dist, cx, cy = item
                    sh = _shift_geom(shape_pad, cx, cy)
                    tb = _shift_geom(text_pad, cx, cy)
                    out_a = sh.difference(region).area
                    t_hit = (tb.intersection(h_union).area if not h_union.is_empty else 0.0) + \
                        (tb.intersection(sl_union).area if not sl_union.is_empty else 0.0)
                    h_hit = sh.intersection(h_union).area if not h_union.is_empty else 0.0
                    s_hit = sh.intersection(s_union).area if not s_union.is_empty else 0.0
                    return (round(t_hit, 6), round(out_a, 6), round(h_hit, 6), round(s_hit, 6), dist)
                _, cx, cy = min(cands, key=score)
                best = (cx, cy)

            cx, cy = best
            letters_str = ",".join(a[0] for a in arrows)
            ring_pts = [(cx + r * math.cos(k / 18.0 * math.pi), cy + r * math.sin(k / 18.0 * math.pi)) for k in range(37)]
            ro = annotate.polyline(drawing, ring_pts, z, classes=CALLOUT_RING_CLASS)
            annotate.tool.Ifc.get_entity(ro).Name = f"内视符号 {rid} {ident} {letters_str}"
            so = annotate.text(drawing, cx, cy, ident, size=annotate.NUM, z=z, align="center", register=True, classes=CALLOUT_CLASS)
            annotate.tool.Ifc.get_entity(so).Name = f"内视图号 {rid} {ident} {letters_str}"
            for lt, _, tri, (lx, ly), _ in arrows:
                annotate.fill_area(drawing, [(cx + x, cy + y) for x, y in tri], z, classes=CALLOUT_TRI_CLASS,
                                   name=f"内视三角 {rid} {lt}")
                lo = annotate.text(drawing, cx + lx, cy + ly, lt, size=annotate.NUM, z=z, align="center",
                                   register=True, classes=CALLOUT_CLASS)
                annotate.tool.Ifc.get_entity(lo).Name = f"内视字母 {rid} {ident} {lt}"
            # 圆圈与三角整体登记进占位表，后面的房名避让整个符号（不只是字框）
            occ.append(_shift_geom(shape0, cx, cy))
            drawn += 1
            log("elev callout", rid, ident, letters_str, (round(cx, 3), round(cy, 3)))
    return drawn


# ---------------------------------------------------------------- 立面填充、遮罩、砖缝、点位与材料标注
def _polys_only(geom, min_area: float = 1e-6):
    """只保留面积 >= min_area 的 Polygon 分量（过滤 difference/intersection 产生的零维/一维 GeometryCollection，避免 .boundary 为 None）。"""
    if geom is None or geom.is_empty:
        return Polygon()
    if geom.geom_type == "Polygon":
        return geom if geom.area >= min_area else Polygon()
    ps = []
    stack = list(getattr(geom, "geoms", [geom]))
    while stack:
        g = stack.pop()
        if g.is_empty:
            continue
        if g.geom_type == "Polygon":
            if g.area >= min_area:
                ps.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    return unary_union(ps) if ps else Polygon()


def _fill_geom(dr, geom, z: float, classes: str, name: str) -> int:
    """把 shapely Polygon / MultiPolygon 画成 Bonsai FILL_AREA；带内洞的多边形按洞的 x 边界切条后分别填充。"""
    geom = _polys_only(geom, 1e-4)
    if geom.is_empty:
        return 0
    n = 0
    for p in getattr(geom, "geoms", [geom]):
        if p.is_empty or p.geom_type != "Polygon" or p.area < 1e-4:
            continue
        if not p.interiors:
            pts = list(p.exterior.coords)[:-1]
            if len(pts) >= 3:
                annotate.fill_area(dr, pts, z, classes=classes, name=name)
                n += 1
        else:
            x0, y0, x1, y1 = p.bounds
            xs = sorted({x0, x1} | {c for hole in p.interiors for c in (hole.bounds[0], hole.bounds[2])})
            for a, b in zip(xs[:-1], xs[1:]):
                if b - a < 1e-4:
                    continue
                sub = _polys_only(p.intersection(sbox(a, y0 - 1.0, b, y1 + 1.0)), 1e-4)
                for sp in getattr(sub, "geoms", [sub]):
                    if sp.geom_type == "Polygon" and sp.area >= 1e-4 and not sp.interiors:
                        pts = list(sp.exterior.coords)[:-1]
                        if len(pts) >= 3:
                            annotate.fill_area(dr, pts, z, classes=classes, name=name)
                            n += 1
    return n


def _draw_plenum_mask(dr, w: dict, sch: dict, z: float) -> int:
    """平顶吊顶（无跌级中洞、贴墙覆盖全长）以上到原顶之间的空腔画白底遮罩，盖住吊顶背后识别墙段在洞口处断开的竖向接缝和
    伸进吊顶的面层顶边；回字形、边吊这类有跌级/灯槽的吊顶不遮。"""
    if w.get("ceiling") is None:
        return 0
    L, H = w["length"] / 1000.0, w["height"] / 1000.0
    ceil_mm = float(w["ceiling"])
    ceil = ceil_mm / 1000.0
    thick = float(ceiling.CHECKS["thickness"]) / 1000.0
    v0, v1 = ceil + thick + 0.002, H - 0.004
    if v1 <= v0 + 0.005:
        return 0
    # 确认这面墙的吊顶是平顶块（无内洞）且贴墙覆盖足够长
    blocks = [b for b in (sch.get("ceilings") or []) if b.get("room") == w["room"] and float(b["bottom"]) < w["height"] - 0.5]
    seg = LineString([w["a"], w["b"]])
    flat_hit = []
    for b in blocks:
        if b.get("holes"):
            continue
        s = seg.intersection(Polygon(b["polygon"]).buffer(wall.CHECKS["ceiling_gap_mm"]))
        if s.length > 1.0 and abs(float(b["bottom"]) - ceil_mm) < 1.0:
            flat_hit.append(s)
    if not flat_hit or unary_union(flat_hit).length < wall.CHECKS["ceiling_cover"] * float(w["length"]):
        return 0
    mask = sbox(0.004, v0, L - 0.004, v1)
    cuts = [sbox(u0 / 1000.0, v0 - 0.01, u1 / 1000.0, v1 + 0.01) for u0, u1 in (w.get("open_spans") or [])]
    if cuts:
        mask = mask.difference(unary_union(cuts))
    return _fill_geom(dr, mask, z, PLENUM_CLASS, f"吊顶遮罩 {w['wall']}")


def _draw_skirting(dr, w: dict, z: float, furn_polys: list):
    """踢脚线填充：取墙面数据里的踢脚线轮廓（已挖开门洞和开敞段），再减去贴墙家具的立面投影，用浅灰 FILL_AREA 画出。
    返回实际可见的踢脚线 shapely 几何（米），供踢脚线引出标注和墙砖缝避让使用。"""
    sk = w.get("skirting")
    if not sk:
        return Polygon()
    sk_m = shapely.affinity.scale(wall.face(sk), 0.001, 0.001, origin=(0, 0))
    if furn_polys:
        sk_m = sk_m.difference(unary_union(furn_polys))
    sk_m = _polys_only(sk_m, 1e-4)
    kept = [p for p in getattr(sk_m, "geoms", [sk_m]) if p.geom_type == "Polygon" and p.area >= 1e-4 and (p.bounds[2] - p.bounds[0]) >= 0.03]
    vis = unary_union(kept) if kept else Polygon()
    if not vis.is_empty:
        _fill_geom(dr, vis, z, SKIRTING_CLASS, f"踢脚线 {w['wall']}")
    return vis


def _group_points(pts: list) -> list:
    """墙上点位按同高且沿墙相邻（<= point_merge_mm）合并为一组：[(z_mm, [点位…])]。
    避免四联电视插座、冷热水对、床头插座开关叠字；不同高度或相隔较远的点位各自独立标注。"""
    merge = float(E["point_merge_mm"])
    ordered = sorted(pts, key=lambda p: (round(float(p["z"])), float(p["u"]), p.get("id", "")))
    groups = []
    for p in ordered:
        z = float(p["z"])
        u = float(p["u"])
        if groups and abs(groups[-1][0] - z) < 1.0 and u - float(groups[-1][1][-1]["u"]) <= merge + 1e-6:
            groups[-1][1].append(p)
        else:
            groups.append((z, [p]))
    return groups


def _wall_obstacles(w: dict, den: int, furn_polys: list, furn_lines: list) -> tuple:
    """本面墙立面内部的障碍几何（米）：(hard_union, furn_union, op_union, pt_union, pt_boxes_m, wall_in)。"""
    P = lambda mm: mm * den / 1000.0
    L, H, bottom = w["length"] / 1000.0, w["height"] / 1000.0, w["bottom"] / 1000.0
    ceil = w["ceiling"] / 1000.0 if w.get("ceiling") is not None else None
    top_lim = ceil if ceil is not None else H
    pad_l = P(0.35)
    wall_lines = [
        LineString([(0.0, bottom), (L, bottom)]).buffer(pad_l),
        LineString([(0.0, H), (L, H)]).buffer(pad_l),
        LineString([(0.0, bottom), (0.0, H)]).buffer(pad_l),
        LineString([(L, bottom), (L, H)]).buffer(pad_l),
    ]
    if ceil is not None:
        wall_lines.append(sbox(0.0, ceil - pad_l, L, ceil + 0.02 + pad_l))
    if w.get("skirting"):
        st = float(w["skirting"].get("top") or (w["bottom"] + 80.0)) / 1000.0
        wall_lines.append(LineString([(0.0, st), (L, st)]).buffer(pad_l))
    op_boxes = []
    for o in w.get("openings") or []:
        ob = bottom if o["bottom"] is None else o["bottom"] / 1000.0
        op_boxes.append(sbox(o["from"] / 1000.0, ob, o["to"] / 1000.0, o["top"] / 1000.0).buffer(P(0.4)))
    pts = w.get("points") or []
    pt_raw = [sbox((p["u"] - p["w"] / 2) / 1000.0, p["z"] / 1000.0,
                   (p["u"] + p["w"] / 2) / 1000.0, (p["z"] + p["h"]) / 1000.0) for p in pts]
    pt_boxes = [b.buffer(P(0.35)) for b in pt_raw]
    fl_buf = [ln.buffer(P(0.3)) for ln in furn_lines]
    hard_union = unary_union(wall_lines + op_boxes + pt_boxes + fl_buf) if (wall_lines + op_boxes + pt_boxes + fl_buf) else Polygon()
    furn_union = unary_union(furn_polys).buffer(P(0.35)) if furn_polys else Polygon()
    op_union = unary_union(op_boxes) if op_boxes else Polygon()
    pt_union = unary_union(pt_boxes) if pt_boxes else Polygon()
    wall_in = sbox(P(0.5), bottom + P(0.5), L - P(0.5), top_lim - P(0.5))
    return hard_union, furn_union, op_union, pt_union, pt_raw, wall_in


def _annotate_points(dr, w: dict, den: int, z: float, obs: tuple) -> int:
    """在立面图上为本面墙的点位标注距地高度（如 H300）：同高相邻的点位合标一个，优先贴近点位方块（上→下→右→左），
    避开墙边、吊顶线、门窗洞、点位方块、家具折棱和已写文字；紧贴放不下（如被洁具线条遮挡）时用短引出线引到附近空地。"""
    pts = w.get("points") or []
    if not pts:
        return 0
    P = lambda mm: mm * den / 1000.0
    L, H, bottom = w["length"] / 1000.0, w["height"] / 1000.0, w["bottom"] / 1000.0
    ceil = w["ceiling"] / 1000.0 if w.get("ceiling") is not None else None
    top_lim = ceil if ceil is not None else H
    gap = P(float(E["point_gap_mm"]))
    lead_max = P(float(E["point_leader_mm"]))
    fs = annotate.FONT_MM[annotate.NUM]
    occ = annotate._OCC.setdefault(dr.id(), [])
    hard_union, furn_union, _, _, _, wall_in = obs

    def ok_box(tb, avoid_furn=True):
        if not wall_in.contains(tb):
            return False
        tbp = tb.buffer(P(0.25))
        if not hard_union.is_empty and tbp.intersects(hard_union):
            return False
        if avoid_furn and not furn_union.is_empty and tbp.intersects(furn_union):
            return False
        if any(tbp.intersects(o) for o in occ):
            return False
        return True

    count = 0
    for z_mm, grp in _group_points(pts):
        lit = f"H{int(round(z_mm))}"
        tw, th = P(annotate.text_mm(lit, fs)), P(0.95 * fs)
        u0 = min((p["u"] - p["w"] / 2) / 1000.0 for p in grp)
        u1 = max((p["u"] + p["w"] / 2) / 1000.0 for p in grp)
        v0 = min(p["z"] / 1000.0 for p in grp)
        v1 = max((p["z"] + p["h"]) / 1000.0 for p in grp)
        uc, vc = (u0 + u1) / 2, (v0 + v1) / 2

        shifts = (0.0, P(2.0), -P(2.0), P(4.0), -P(4.0))
        cands = []
        for g_extra in (0.0, P(1.2)):
            g = gap + g_extra
            for du in shifts:
                cands.append((uc + du, v1 + g + th / 2))
            for du in shifts:
                cands.append((uc + du, v0 - g - th / 2))
            cands.append((u1 + g + tw / 2, vc))
            cands.append((u0 - g - tw / 2, vc))
            cands.append((u1 + g + tw / 2, v1 + g + th / 2))
            cands.append((u0 - g - tw / 2, v1 + g + th / 2))

        placed = False
        for avoid_furn in (True, False):
            for tx, ty in cands:
                tb = annotate._text_box(lit, annotate.NUM, den, tx, ty, "center", 0.0)
                if ok_box(tb, avoid_furn=avoid_furn):
                    obj = annotate.text(dr, tx, ty, lit, size=annotate.NUM, z=z, align="center", register=True)
                    annotate.tool.Ifc.get_entity(obj).Name = f"点位高度 {w['wall']} {lit}"
                    count += 1
                    placed = True
                    break
            if placed:
                break
        if placed:
            continue

        best_lead = None
        for rmax in (lead_max, lead_max * 1.8):
            step = P(1.8)
            for avoid_furn in (True, False):
                spots = []
                gx0, gx1 = max(wall_in.bounds[0] + tw / 2, uc - rmax), min(wall_in.bounds[2] - tw / 2, uc + rmax)
                gy0, gy1 = max(wall_in.bounds[1] + th / 2, vc - rmax), min(wall_in.bounds[3] - th / 2, vc + rmax)
                if gx1 < gx0 or gy1 < gy0:
                    continue
                for ty in np.arange(gy0, gy1 + 1e-6, step):
                    for tx in np.arange(gx0, gx1 + 1e-6, step):
                        d = math.hypot(float(tx) - uc, float(ty) - vc)
                        if P(2.5) <= d <= rmax:
                            spots.append((d, float(tx), float(ty)))
                spots.sort(key=lambda t: t[0])
                for _, tx, ty in spots:
                    tb = annotate._text_box(lit, annotate.NUM, den, tx, ty, "center", 0.0)
                    if not ok_box(tb, avoid_furn=avoid_furn):
                        continue
                    left_end = (tx - tw / 2, ty)
                    right_end = (tx + tw / 2, ty)
                    if uc >= tx:
                        p0, out_dir, first = right_end, (1.0, 0.0), False
                    else:
                        p0, out_dir, first = left_end, (-1.0, 0.0), True
                    path = annotate._leader_path(p0, (uc, vc), out_dir, den)
                    if not path:
                        continue
                    ln = LineString(path)
                    if ln.length > rmax * 1.3 or any(ln.intersects(o) for o in occ):
                        continue
                    best_lead = (path, first, tb)
                    break
                if best_lead is not None:
                    break
            if best_lead is not None:
                break

        if best_lead is not None:
            path, first, tb = best_lead
            obj = annotate._leader(dr, path, lit, annotate.NUM, first, z=z)
            annotate.tool.Ifc.get_entity(obj).Name = f"点位高度 {w['wall']} {lit}"
            occ.append(tb)
            count += 1
        else:
            ty = min(top_lim - th / 2 - P(0.4), v1 + gap + th / 2)
            tx = min(max(uc, tw / 2 + P(0.4)), L - tw / 2 - P(0.4))
            obj = annotate.text(dr, tx, ty, lit, size=annotate.NUM, z=z, align="center", register=True)
            annotate.tool.Ifc.get_entity(obj).Name = f"点位高度 {w['wall']} {lit}"
            count += 1
    return count


def _sample_finish_dots(vis_m, L: float, top_vis: float, den: int) -> list:
    """在可见墙面区域内取候选引线落点 [(u, v)]：优先离边界有余量、靠上（引到顶部留白最短）、靠近墙面水平中段。"""
    P = lambda mm: mm * den / 1000.0
    dots = []
    for inset in (0.10, 0.05, 0.02):
        inner = vis_m.buffer(-inset)
        if inner.is_empty:
            continue
        x0, y0, x1, y1 = inner.bounds
        step = max(P(3.0), 0.15)
        ys = np.arange(y1, y0 - 1e-6, -step)
        xs = np.arange(x0, x1 + 1e-6, step)
        if len(xs) == 0:
            xs = np.array([(x0 + x1) / 2])
        if len(ys) == 0:
            ys = np.array([(y0 + y1) / 2])
        for y in ys:
            for x in xs:
                pt = Point(float(x), float(y))
                if inner.contains(pt):
                    dots.append((float(x), float(y)))
        if dots:
            break
    if not dots:
        for p in sorted(getattr(vis_m, "geoms", [vis_m]), key=lambda g: -g.area):
            if p.geom_type == "Polygon" and not p.is_empty:
                rp = p.representative_point()
                dots.append((rp.x, rp.y))
    # 优先靠上、靠近墙宽中段
    dots.sort(key=lambda uv: (round((top_vis - uv[1]) / 0.15), abs(uv[0] - L / 2)))
    return dots[:48]


def _annotate_materials(dr, w: dict, den: int, z: float, rect: tuple, furn_polys: list, sk_vis_m, obs: tuple) -> int:
    """立面材料引出标注（字号 small = 3.5mm，GB/T 50001 汉字 ≥ 3.5mm）：
    - 墙面做法（如"墙砖 300×600""白色乳胶漆"，有 note 时附括号）：引线端头实心圆点落在可见墙面上，文字优先排在顶部留白
      （原顶上方 mat_offset_mm），引线不穿家具、门窗洞口和已有文字；顶部被吊柜完全遮挡时试墙内空地，最后回落顶部留白。
    - 踢脚线（如"踢脚线 H80"）：有可见踢脚线时引线落到最长一段可见踢脚线中点，文字排在上方墙面空地上（避开家具、洞口、点位、
      已写文字）。返回画了几处材料引出标注。"""
    P = lambda mm: mm * den / 1000.0
    L, H, bottom = w["length"] / 1000.0, w["height"] / 1000.0, w["bottom"] / 1000.0
    ceil = w["ceiling"] / 1000.0 if w.get("ceiling") is not None else None
    top_vis = ceil if ceil is not None else H
    occ = annotate._OCC.setdefault(dr.id(), [])
    hard_union, furn_union, op_union, pt_union, pt_raw, wall_in = obs
    blk_union = unary_union([g for g in (furn_union, op_union, pt_union) if not g.is_empty]) if any(
        not g.is_empty for g in (furn_union, op_union, pt_union)) else Polygon()

    # 1. 墙面材料引出标注
    vis_fin = shapely.affinity.scale(wall.face(w), 0.001, 0.001, origin=(0, 0))
    vis_fin = vis_fin.intersection(sbox(-0.01, bottom - 0.01, L + 0.01, top_vis))
    sub_list = list(furn_polys) + pt_raw + ([sk_vis_m] if not sk_vis_m.is_empty else [])
    if sub_list:
        vis_fin = vis_fin.difference(unary_union(sub_list))
    vis_fin = _polys_only(vis_fin, 1e-4)
    mat_lit = wall.label(w) + (f"（{w['note']}）" if w.get("note") else "")
    size = 1.8
    fs = annotate.FONT_MM[size]
    tw, th = P(annotate.text_mm(mat_lit, fs)), P(0.95 * fs)
    ty_top = H + P(float(E["mat_offset_mm"]))
    u_min, u_max = rect[0] + P(1.2), rect[2] - P(1.2)
    count = 0

    if not vis_fin.is_empty:
        dots = _sample_finish_dots(vis_fin, L, top_vis, den)
        picked = None

        def _try_top(avoid_blk: bool):
            best = None
            for uq, vq in dots:
                # 字端离开落点 dx：让斜段约 45°～60°
                dy = max(ty_top - vq, P(2.0))
                dxs = (max(P(2.2), min(dy * 0.55, P(5.5))), P(2.0), P(7.0))
                sides = (True, False) if uq <= L / 2 else (False, True)
                for first in sides:
                    out_dir = (-1.0, 0.0) if first else (1.0, 0.0)
                    align = "middle-left" if first else "middle-right"
                    for dx in dxs:
                        px = uq + dx if first else uq - dx
                        x_lo, x_hi = (px, px + tw) if first else (px - tw, px)
                        if x_lo < u_min or x_hi > u_max:
                            continue
                        tb = annotate._text_box(mat_lit, size, den, px, ty_top, align, 0.0)
                        if any(tb.buffer(P(0.25)).intersects(o) for o in occ):
                            continue
                        path = annotate._leader_path((px, ty_top), (uq, vq), out_dir, den)
                        if not path:
                            continue
                        ln = LineString(path)
                        if any(ln.intersects(o) for o in occ):
                            continue
                        if avoid_blk and not blk_union.is_empty and ln.intersects(blk_union):
                            continue
                        cost = ln.length + abs((x_lo + x_hi) / 2 - L / 2) * 0.15
                        if best is None or cost < best[0]:
                            best = (cost, path, first, tb, (uq, vq))
            return best

        picked = _try_top(avoid_blk=True)
        if picked is None:
            # 顶部有遮挡：尝试在可见墙面空地上写（避开家具、洞口、点位、已写文字）
            best_in = None
            step = P(2.5)
            gx0, gx1 = wall_in.bounds[0] + tw / 2, wall_in.bounds[2] - tw / 2
            gy0, gy1 = wall_in.bounds[1] + th / 2, wall_in.bounds[3] - th / 2
            if gx1 >= gx0 and gy1 >= gy0:
                for uq, vq in dots[:12]:
                    for ty in np.arange(min(gy1, vq + P(10.0)), max(gy0, vq - P(10.0)) - 1e-6, -step):
                        for tx in np.arange(max(gx0, uq - P(14.0)), min(gx1, uq + P(14.0)) + 1e-6, step):
                            tb = annotate._text_box(mat_lit, size, den, float(tx), float(ty), "center", 0.0)
                            tbp = tb.buffer(P(0.35))
                            if not wall_in.contains(tb) or (not hard_union.is_empty and tbp.intersects(hard_union)) \
                                    or (not furn_union.is_empty and tbp.intersects(furn_union)) or any(tbp.intersects(o) for o in occ):
                                continue
                            first = float(tx) >= uq
                            p0 = (float(tx) - tw / 2, float(ty)) if first else (float(tx) + tw / 2, float(ty))
                            out_dir = (-1.0, 0.0) if first else (1.0, 0.0)
                            path = annotate._leader_path(p0, (uq, vq), out_dir, den)
                            if not path:
                                continue
                            ln = LineString(path)
                            if ln.length > P(14.0) or any(ln.intersects(o) for o in occ) \
                                    or (not blk_union.is_empty and ln.intersects(blk_union)):
                                continue
                            if best_in is None or ln.length < best_in[0]:
                                best_in = (ln.length, path, first, tb, (uq, vq))
            picked = best_in
        if picked is None:
            picked = _try_top(avoid_blk=False)
        if picked is None and dots:
            # 极窄墙或带长 note 兜底：钳制在顶部留白左右边界内，不静默丢掉材料标注
            uq, vq = dots[0]
            first = uq <= L / 2
            out_dir = (-1.0, 0.0) if first else (1.0, 0.0)
            align = "middle-left" if first else "middle-right"
            px = min(max(uq + P(2.2), u_min), max(u_min, u_max - tw)) if first else max(min(uq - P(2.2), u_max), min(u_max, u_min + tw))
            path = annotate._leader_path((px, ty_top), (uq, vq), out_dir, den) or [(px, ty_top), (uq, ty_top), (uq, vq)]
            tb = annotate._text_box(mat_lit, size, den, px, ty_top, align, 0.0)
            picked = (0.0, path, first, tb, (uq, vq))
            log("elevation material clamped", w["room"], w["wall"], mat_lit)
        if picked is not None:
            _, path, first, tb, dot = picked
            obj = annotate._leader(dr, path, mat_lit, size, first, z=z)
            annotate.tool.Ifc.get_entity(obj).Name = f"墙面材料 {w['wall']} {mat_lit}"
            occ.append(tb)
            occ.append(Point(dot).buffer(P(0.6)))
            count += 1

    # 2. 踢脚线引出标注（干区有可见踢脚线时）
    sk = w.get("skirting")
    if sk and not sk_vis_m.is_empty:
        pieces = [p for p in getattr(sk_vis_m, "geoms", [sk_vis_m]) if p.geom_type == "Polygon" and not p.is_empty]
        if pieces:
            best_p = max(pieces, key=lambda p: p.bounds[2] - p.bounds[0])
            bx0, by0, bx1, by1 = best_p.bounds
            sv = (by0 + by1) / 2
            span_w = bx1 - bx0
            dot_us = [(bx0 + bx1) / 2]
            if span_w > 0.16:
                dot_us += [bx0 + min(0.12, span_w * 0.2), bx1 - min(0.12, span_w * 0.2)]
            sk_lit = f"{sk['material']} H{int(round(float(sk['height'])))}"
            stw, sth = P(annotate.text_mm(sk_lit, fs)), P(0.95 * fs)
            lead_lim = P(float(E["skirting_leader_mm"]))
            non_furn_blk = unary_union([g for g in (op_union, pt_union) if not g.is_empty]) if (
                not op_union.is_empty or not pt_union.is_empty) else Polygon()
            best_sk = None
            for avoid_furn in (True, False):
                for rmax in (lead_lim, lead_lim * 1.8):
                    step = P(2.0)
                    for su in dot_us:
                        gx0 = max(wall_in.bounds[0] + stw / 2, su - rmax)
                        gx1 = min(wall_in.bounds[2] - stw / 2, su + rmax)
                        gy0 = max(by1 + P(2.5) + sth / 2, wall_in.bounds[1] + sth / 2)
                        gy1 = min(wall_in.bounds[3] - sth / 2, by1 + rmax)
                        if gx1 < gx0 or gy1 < gy0:
                            continue
                        spots = []
                        for ty in np.arange(gy0, gy1 + 1e-6, step):
                            for tx in np.arange(gx0, gx1 + 1e-6, step):
                                d = math.hypot(float(tx) - su, float(ty) - sv)
                                if P(3.0) <= d <= rmax:
                                    spots.append((d, float(tx), float(ty)))
                        spots.sort(key=lambda t: t[0])
                        for _, tx, ty in spots:
                            tb = annotate._text_box(sk_lit, size, den, tx, ty, "center", 0.0)
                            tbp = tb.buffer(P(0.3))
                            if not wall_in.contains(tb):
                                continue
                            if not hard_union.is_empty and tbp.intersects(hard_union):
                                continue
                            if avoid_furn and not furn_union.is_empty and tbp.intersects(furn_union):
                                continue
                            if any(tbp.intersects(o) for o in occ):
                                continue
                            for first in ((True, False) if tx >= su else (False, True)):
                                p0 = (tx - stw / 2, ty) if first else (tx + stw / 2, ty)
                                out_dir = (-1.0, 0.0) if first else (1.0, 0.0)
                                path = annotate._leader_path(p0, (su, sv), out_dir, den)
                                if not path:
                                    continue
                                ln = LineString(path)
                                if ln.length > rmax * 1.3 or any(ln.intersects(o) for o in occ):
                                    continue
                                if not non_furn_blk.is_empty and ln.intersects(non_furn_blk):
                                    continue
                                if avoid_furn and not blk_union.is_empty and ln.intersects(blk_union):
                                    continue
                                best_sk = (path, first, tb, (su, sv))
                                break
                            if best_sk is not None:
                                break
                        if best_sk is not None:
                            break
                    if best_sk is not None:
                        break
                if best_sk is not None:
                    break
            if best_sk is not None:
                path, first, tb, dot = best_sk
                obj = annotate._leader(dr, path, sk_lit, size, first, z=z)
                annotate.tool.Ifc.get_entity(obj).Name = f"踢脚线标注 {w['wall']} {sk_lit}"
                occ.append(tb)
                occ.append(Point(dot).buffer(P(0.6)))
                count += 1
    return count


def _draw_wall_joints(f, dr, w: dict, den: int, z: float, furn_polys: list, sk_vis_m, pt_raw: list) -> int:
    """真实墙砖缝（w["kind"] == "tile" 且有排砖网格 grid）：按 wall.face(w) 裁到吊顶底以下、减去家具投影、点位方块和踢脚线，
    调用 flooring.joint_lines 现算砖缝，再用 annotate.lines_avoiding 在本视图已写文字与引线处断开留白。"""
    if w.get("kind") != "tile" or not w.get("grid"):
        return 0
    P = lambda mm: mm * den / 1000.0
    face_mm = wall.face(w)
    if w.get("ceiling") is not None:
        face_mm = face_mm.intersection(sbox(-1.0, float(w["bottom"]) - 1.0, float(w["length"]) + 1.0, float(w["ceiling"])))
    sub_m = list(furn_polys) + pt_raw + ([sk_vis_m] if not sk_vis_m.is_empty else [])
    if sub_m:
        sub_mm = shapely.affinity.scale(unary_union(sub_m), 1000.0, 1000.0, origin=(0, 0))
        face_mm = face_mm.difference(sub_mm)
    face_mm = _polys_only(face_mm, 1.0)
    if face_mm.is_empty:
        return 0
    segs_mm = []
    for pg in getattr(face_mm, "geoms", [face_mm]):
        if pg.geom_type == "Polygon" and pg.area >= 1.0:
            segs_mm.extend(flooring.joint_lines(w["grid"], pg))
    if not segs_mm:
        return 0
    segs_m = [shapely.affinity.scale(s, 0.001, 0.001, origin=(0, 0)) for s in segs_mm]
    gap = P(float(E["joint_gap_mm"]))
    cut_shapes = list(annotate._OCC.get(dr.id(), [])) + pt_raw
    cut = unary_union([g.buffer(gap, join_style="mitre") for g in cut_shapes]) if cut_shapes else Polygon()
    segs_m = annotate.lines_avoiding(f, dr, den, segs_m, cut=cut)
    if not segs_m:
        return 0
    annotate.segments(dr, segs_m, z, classes=WALLJOINT_CLASS, name=f"墙砖缝 {w['wall']}")
    return len(segs_m)


def _annotate(f, dr, w: dict, sch: dict, den: int, tl: float, tr: float, vb: float, rect: tuple,
              furn_polys=(), furn_lines=()) -> None:
    """一面墙的立面标注与细部（视图坐标，米）：
    1) 平顶吊顶以上遮罩；2) 踢脚线浅灰填充（扣家具）；3) 横向两道、竖向一道尺寸与界线、左侧标高、原顶粗线；
    4) 点位距地高度字（H300）；5) 墙面材料与踢脚线引出标注；6) 墙砖排砖缝（扣家具、遇字断开，最后画）。"""
    P = lambda mm: mm * den / 1000.0
    z = 0.01
    L, H, bottom = w["length"] / 1000.0, w["height"] / 1000.0, w["bottom"] / 1000.0
    ceil = w["ceiling"] / 1000.0 if w.get("ceiling") is not None else None
    _draw_plenum_mask(dr, w, sch, z)
    sk_vis_m = _draw_skirting(dr, w, z, furn_polys)

    cuts = {0.0, L}
    for o in w.get("openings") or []:
        cuts |= {min(max(o["from"] / 1000.0, 0.0), L), min(max(o["to"] / 1000.0, 0.0), L)}
    for s0, s1 in w.get("open_spans") or []:
        cuts |= {s0 / 1000.0, s1 / 1000.0}
    chains = annotate._dedupe([annotate._dim_points(cuts), [0.0, L]])
    edge = (0.0, vb, L, H)
    annotate._draw_side_chains(dr, chains, "bottom", edge, den, z)
    pts = {}
    for i, c in enumerate(chains):
        for v in c:
            k = next((q for q in pts if abs(q - v) < 0.02), v)
            pts[k] = max(pts.get(k, -1), i)
    annotate._ext_lines(dr, "bottom", pts, edge, den, z)

    # 竖向尺寸：基准标高（楼面、吊顶底、原顶）优先保留，洞口边离基准 ≤ 20mm 时不挤掉基准点
    anchors = [bottom, H] + ([ceil] if ceil is not None else [])
    vs = set(anchors)
    for o in w.get("openings") or []:
        for x in (o["bottom"], o["top"]):
            if x is None:
                continue
            xv = x / 1000.0
            if bottom + 0.02 < xv < H - 0.02 and all(abs(xv - a) > 0.02 for a in anchors):
                vs.add(xv)
    vchain = annotate._dim_points(vs)
    side = (-tl, vb, L + tr, H)
    if len(vchain) >= 2:
        annotate._draw_side_chains(dr, [vchain], "right", side, den, z)
        annotate._ext_lines(dr, "right", {v: 0 for v in vchain}, side, den, z)
    xl = -tl - P(float(E["level_gap_mm"]))
    for v in (bottom, ceil if ceil is not None else H):
        annotate.level_at(dr, xl, v, v, den, z)
    annotate.polyline(dr, [(-tl, H), (L + tr, H)], z, classes="cut")   # 原顶（模型里没有楼板）

    obs = _wall_obstacles(w, den, furn_polys, furn_lines)
    n_ph = _annotate_points(dr, w, den, z, obs)
    n_mat = _annotate_materials(dr, w, den, z, rect, furn_polys, sk_vis_m, obs)
    n_jt = _draw_wall_joints(f, dr, w, den, z, furn_polys, sk_vis_m, obs[4])
    log("elevation details", w["room"], w["wall"], w["letter"], "pts", n_ph, "mat", n_mat, "joints", n_jt,
        "sides", (round(tl * 1000), round(tr * 1000)))


def make_views(f, rec: dict, sch: dict, offset, z0_mm: float) -> list:
    """建全部立面视图并出视图 SVG：[(房间 id, 房名, [(视图, 墙面数据, (宽, 高) 图纸毫米)…])]。视图名"厨房 A 立面图"（同名的房间
    按房间顺序加 1、2："次卧1 B 立面图"，视图名在一个 IFC 里不重名）；视图坐标系登记给 annotate；视图属性集 JZ_Elevation 记
    房间、墙边、字母和几项尺寸。"""
    ox, oy = offset
    z0 = z0_mm / 1000.0
    geo = sch.get("after") or rec
    rooms = {rm["id"]: rm for rm in furnish.rooms_of(rec, sch)}
    labels = _room_labels(rooms)
    by_rw = _by_room_wall(sch)
    out = []
    for rid, ws in walls_of(sch):
        rm = rooms.get(rid)
        if rm is None:
            log("elevation skipped (room not found)", rid)
            continue
        name, label = rm["name"], labels[rid]
        ring_poly = Polygon([rw["a"] for rw in rm["walls"]])
        room = _Room(f, rid, Polygon([((x - ox) / 1000.0, (y - oy) / 1000.0) for x, y in rm["poly"].exterior.coords]))
        views = []
        for w in ws:
            L, H = w["length"] / 1000.0, w["height"] / 1000.0
            F = frame(w, ox, oy, z0)
            d_mm = depth_mm(w, ring_poly)
            D = d_mm / 1000.0
            tl, tr = _side_widths(w, rm["walls"], by_rw.get(rid, {}), d_mm)
            rect, size_mm, vb = view_rect(w, tl, tr)
            t = thickness_mm(w, geo) / 1000.0
            near = room.near(((w["a"][0] - ox) / 1000.0, (w["a"][1] - oy) / 1000.0), ((w["b"][0] - ox) / 1000.0, (w["b"][1] - oy) / 1000.0))
            ops = room.openings_before(F, D)
            include = room.walls + ops + room.coverings + [g for g, _ in near]
            props = {"Room": rid, "RoomName": name, "Wall": w["wall"], "Letter": w["letter"], "Unit": w.get("unit", ""),
                     "Length_mm": float(w["length"]), "Height_mm": float(w["height"]), "Bottom_mm": float(w["bottom"]),
                     "Depth_mm": round(D * 1000.0, 1), "Elements": len(near)}
            if w.get("ceiling") is not None:
                props["Ceiling_mm"] = float(w["ceiling"])
            vname = f"{label} {w['letter']} 立面图"
            dr = drawset.add_elevation(vname, F, rect, (-tl, vb, L + tr, H), D, D + t + 0.004, DEN, " + ".join(include),
                                       "IfcBuildingStorey + IfcGrid", PSET, props)
            annotate.set_frame(dr, F)
            furn_polys, furn_lines = room.uv_geoms([g for g, _ in near], F)
            _annotate(f, dr, w, sch, DEN, tl, tr, vb, rect, furn_polys=furn_polys, furn_lines=furn_lines)
            log("elevation", rid, name, w["wall"], w["letter"], "L", w["length"], "H", w["height"], "depth", round(D * 1000.0),
                "walls/openings/coverings", len(room.walls), len(ops), len(room.coverings), "near", len(near),
                [n for _, n in near])
            views.append((dr, w, size_mm))
        out.append((rid, label, views))
    for rid, wid, letter, why in skipped(sch):
        log("elevation not drawn", rid, wid, letter, why)
    for _, _, views in out:
        for dr, _, _ in views:
            drawset.render_view(dr)
    return out


def sheets(f, rec: dict, sch: dict, offset, z0_mm: float) -> list:
    """立面图纸：多面墙房间（> share_max_walls）一间房单独成张，1～2 面墙的房间合并按 2×2 排在 A3 上，每张不超过图框
    （drawset.pack_views）。返回 [(图名, [[视图, …], …] 各行)]。"""
    made = make_views(f, rec, sch, offset, z0_mm)
    room_entries = [(rid, label, [(dr, sz) for dr, _, sz in views]) for rid, label, views in made]
    packed = _pack_room_sheets(room_entries)
    for title, rows_dr in packed:
        log("elevation sheet", title, [[dr.Name for dr in r] for r in rows_dr])
    return packed
