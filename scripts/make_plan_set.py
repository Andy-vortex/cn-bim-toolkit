"""通用出图流程：识别结果（+ 方案）→ IFC → A3 中文平面图、门窗表、图纸目录（+ 规范自查报告、拆改清单）。
用法：blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- <识别.json> <输出目录> [选项]（选项见 -- --help）"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import conventions, printing  # noqa: E402


def _files(s: str) -> list:
    return [printing.existing_file(p) for p in s.split(",") if p]


def _recognition(p: str) -> str:
    """识别结果：文件要在，是 JSON 对象，且认出了房间和墙（--region 写错时 dxfplan 认不出东西），否则解析参数时就报一行错
    （退出码 2）。"""
    try:
        with open(printing.existing_file(p), encoding="utf-8-sig") as fh:
            rec = json.load(fh)
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        raise argparse.ArgumentTypeError(f"不是识别结果 JSON：{p}")
    if not rec.get("rooms") or not rec.get("walls"):
        raise argparse.ArgumentTypeError(f"识别结果里没有房间或墙：{p}（检查 dxfplan 的 --region）")
    return p


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="blender -b --python-exit-code 1 --python scripts/make_plan_set.py --", allow_abbrev=False,
        description="识别结果（+ 方案）→ IFC → A3 中文平面图、门窗表、图纸目录（+ 规范自查报告、拆改清单）。"
                    "在装了 Bonsai 的 Blender 里运行。",
        epilog="图号：{前缀}-00 图纸目录，{前缀}-00a… 自查报告和拆改清单（A4），{前缀}-01… 平面图，最后是门窗表；"
               "分区或分段时 {前缀}-01 是组合平面图。输出：图号.pdf/png、*-report.json/html、门窗表.xlsx、图纸目录.xlsx、"
               "model.ifc、work/（中间文件）。")
    p.add_argument("recognition", type=_recognition, metavar="识别.json", help="识别结果（python -m jiazhuang.dxfplan 的输出）")
    p.add_argument("out", metavar="输出目录")
    p.add_argument("--scheme", type=printing.existing_file, metavar="方案.json",
                   help="方案：房间用途、分区、设施点位、拆改（格式见 src/jiazhuang/scheme.py）；不给就把识别结果原样建模（现状平面）")
    p.add_argument("--rules", type=_files, default=[], metavar="a.json,b.json", help="规则包，逗号分隔，每个出一份自查报告")
    p.add_argument("--project", default="平面图（示例）", help="工程名（缺省：%(default)s）")
    p.add_argument("--short", help="图框标题栏里的工程名；缺省同 --project")
    p.add_argument("--building", default="楼栋", help="楼栋名，写进 IFC（缺省：%(default)s）")
    p.add_argument("--storey-name", default="标准层", help="图名里的层次，如\"四～十七层\"（缺省：%(default)s）")
    p.add_argument("--labels", type=printing.existing_file, metavar="简称.json",
                   help='房间名表 {"abbr": {用途: 总图简称}, "short": {用途: 分段图上的短名}, "legend_text": "总图图例简称对照后面的补充说明"}')
    p.add_argument("--title", help="总图名，可用 {storey}（缺省\"{storey}平面图\"，有分区或分段时\"{storey}组合平面图\"）")
    p.add_argument("--zone-title", help="分区、分段平面的图名，可用 {storey}、{id}（缺省：分段\"{storey} {id}平面图\"，分区\"{id} 分区平面\"）")
    p.add_argument("--prefix", default="A", help="图号前缀（缺省：%(default)s）")
    p.add_argument("--mep", action="store_true",
                   help="水电图：每个平面换成插座布置图、开关连线图、给排水点位图三张（方案要有 scripts/mep.py 布的点位），"
                        "表格是回路表、点位统计表，不出门窗表、不标门窗编号；--title、--zone-title 可用 {kind}（图的种类，没写时空一格接在图名后面）")
    p.add_argument("--split", default="auto", choices=("auto", "none"),
                   help="方案没有分区、整层一张 A3 放不下 1:100 时，auto 自动沿 x 切成几段出分段平面，none 不切（缺省：%(default)s）")
    a = p.parse_args(argv)
    if err := conventions.check_pack():
        p.error(err)
    if a.mep:   # 水电图要有 scripts/mep.py 布过点位的方案，没有点位时三张图都是空的：解析参数时就报错（退出码 2）
        from jiazhuang import mep
        try:
            items = printing.read_json(a.scheme, "方案").get("items", []) if a.scheme else []
        except argparse.ArgumentTypeError as e:
            p.error(str(e))
        if not isinstance(items, list) or not any(isinstance(it, dict) and mep.point_type(it) for it in items):
            p.error("--mep 要给带点位的方案（scripts/mep.py 的输出），用 --scheme 指定" if not a.scheme
                    else f"--mep 要给带点位的方案（scripts/mep.py 的输出）：{a.scheme} 里没有点位")
    return a


A = parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:] if sys.argv[0].endswith(".py") else [])

import bpy  # noqa: E402
import bonsai.tool as tool  # noqa: E402
from jiazhuang import annotate, checker, drawset, openings, plan2ifc, renovate, scheme, schedules, symbols  # noqa: E402

SYMBOLS = conventions.pack_file("symbols.json")
HEIGHTS = symbols.default_heights(SYMBOLS)   # 点位默认安装高度：图例写它，图上只标和它不同的
PROJECT, BUILDING, PREFIX, STOREY, RULES, SPLIT = A.project, A.building, A.prefix, A.storey_name, A.rules, A.split
PROJECT_SHORT = A.short if A.short is not None else A.project


def read_json(p):
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


LABELS = read_json(A.labels) if A.labels else {}

OUT = os.path.abspath(A.out)
os.makedirs(OUT, exist_ok=True)
rec0 = read_json(A.recognition)   # 识别结果（拆改前）
sch = read_json(A.scheme) if A.scheme else scheme.from_recognition(rec0)
rec = sch.get("after") or rec0   # 有拆改时，出图和外包都用拆改后的几何

# 版面：A3 图框可放图区域 336×275mm（drawset.AREA；下边留给视图图名，高按 250 算）；四周给尺寸和轴号的留边按每一侧实际要画的
# 道数和轴号算（annotate.block_plan / side_margin_mm），这里先按最多的情况估，判断要不要分段、每段最长多少
AVAIL_W, AVAIL_H = drawset.AREA[2] - drawset.AREA[0], 250.0
AREA_R = drawset.AREA[2]   # 可放图区域右边（图纸毫米）
LEGEND_W, LEGEND_MIN = 58.0, 44.0   # 分区图右侧图例栏宽（图例 56 + 间隙）；为了不降比例最窄可收到 44
VIEW_PAD = 1.0             # 视图四周再留 1mm
DENS = (50, 100, 150, 200, 300, 500, 1000)
M_EXT3, M_EXT2 = annotate.side_margin_mm(3, True), annotate.side_margin_mm(2, True)   # 外墙一侧：三道/两道 + 轴号
M_CUT = annotate.side_margin_mm(2, True)                                              # 内部切口一侧：轴线 +（总尺寸）+ 轴号


def est_margin(kind, which):
    return M_CUT if kind == "cut" else (M_EXT3 if which in ("left", "bottom") else M_EXT2)


def fits_mm(w_mm, h_mm, den, mx=M_EXT3 + M_EXT2, my=M_EXT3 + M_EXT2):
    return w_mm / den + mx + 2 * VIEW_PAD <= AVAIL_W and h_mm / den + my + 2 * VIEW_PAD <= AVAIL_H


# 方案没有分区、整层放不下 1:100（含三道尺寸）时自动分段（只沿 x 切）
fp_x = [p[0] for g in rec["footprint"] for p in g]
fp_y = [p[1] for g in rec["footprint"] for p in g]
auto_split = False
if SPLIT != "none" and not (sch.get("zones") or sch.get("cores")) and not fits_mm(max(fp_x) - min(fp_x), max(fp_y) - min(fp_y), 100):
    depth = max(fp_y) - min(fp_y)
    seg_den = next((d for d in (100, 150) if fits_mm(1, depth, d)), 150)
    if not fits_mm(1, depth, seg_den):
        drawset.log("WARNING depth too large for 1:150, segments will drop to a smaller scale (no opening tags below 1:150)", depth)
    auto_split = bool(scheme.split_zones(rec, sch, lambda lk, rk: (AVAIL_W - est_margin(lk, "left") - est_margin(rk, "right") - 2 * VIEW_PAD) * seg_den))
    drawset.log("split", [(z["id"], z["kinds"], [round(v) for v in z["crop"]], z["wall_cut"]) for z in sch.get("zones", [])])
zones = sch.get("zones") or sch.get("cores") or []
TITLE_T = A.title or ("{storey}组合平面图" if zones else "{storey}{kind}" if A.mep else "{storey}平面图")
TITLE = TITLE_T.format(storey=STOREY, kind="")
ZONE_TITLE = A.zone_title if A.zone_title is not None else ("{id} {kind}" if A.mep else "{storey} {id}平面图" if auto_split else "{id} 分区平面")
if A.mep:   # 水电图一个平面出三张：自己写的图名里没有 {kind} 时空一格接在后面（"示例户型 插座布置图"），三张不同名
    TITLE_T = TITLE_T if "{kind}" in TITLE_T else TITLE_T + " {kind}"
    ZONE_TITLE = ZONE_TITLE if "{kind}" in ZONE_TITLE else ZONE_TITLE + " {kind}"

ifc_path = os.path.join(OUT, "model.ifc")
info = plan2ifc.build(rec0, sch, SYMBOLS, ifc_path, PROJECT, BUILDING)   # 传拆改前的：原图编号按改前的洞口宽判断
drawset.log("ifc", info)
marks = openings.assign_marks_file(ifc_path)   # 门窗编号写进 Tag，按编号建 IfcDoorType/IfcWindowType（电梯层门不编号）
drawset.log("marks", len(marks), len({r["mark"] for r in marks if r["mark"]}))
zone_of = lambda r: r.get("zone") or r.get("core") or ""
levels = sorted(sch.get("levels") or rec.get("levels") or [0.0])
today = datetime.date.today().isoformat()
attachments = []   # 目录里列出的 A4 附件：(图号, 名称)

# 规范检查（读刚生成的 IFC）：每个规则包出一份报告
for i, fn in enumerate(RULES):
    tag = f"{PREFIX}-00{'abcdefgh'[i]}"
    res = checker.run(ifc_path, fn)
    stem = os.path.splitext(os.path.basename(fn))[0]
    with open(os.path.join(OUT, f"{stem}-report.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    h = checker.to_html(res, os.path.join(OUT, f"{stem}-report.html"), PROJECT)
    printing.html_to_pdf(h, os.path.join(OUT, f"{tag}_{stem}-report.pdf"))
    attachments.append((tag, f"{res['facts']['standard']} 自查报告"))

# 拆改清单
changes = sch.get("changes") or []
if changes:
    tag = f"{PREFIX}-00{'abcdefgh'[len(RULES)]}"
    hp = renovate.to_html(changes, os.path.join(OUT, "renovation-list.html"), PROJECT, today)
    printing.html_to_pdf(hp, os.path.join(OUT, f"{tag}_renovation-list.pdf"))
    attachments.append((tag, "拆改清单"))

# 出图（先清掉上次留在 work/ 里的视图和图纸文件）
drawset.clean_outputs(OUT)
bpy.ops.bim.load_project(filepath=ifc_path, should_start_fresh_session=True)
drawset.configure_project()
f = tool.Ifc.get()
storey = f.by_type("IfcBuildingStorey")[0]
Z = info["z0"] / 1000.0 + 1.0
OX, OY = info["offset"]
polys = {r["id"]: r["polygon"] for r in rec["rooms"]}
wall_pts = [p for w in rec["walls"] for p in w["polygon"]]
has_grid = bool(f.by_type("IfcGrid"))
PACK_KEYS = symbols.load_pack(SYMBOLS)
legend = sorted({it["key"] for it in sch.get("items", []) if it["key"] in PACK_KEYS})   # 家具看图认得出，不进图例
has_furniture = any(it["key"] not in PACK_KEYS for it in sch.get("items", []))
L = lambda v: (v - OX) / 1000.0     # 图纸 x（毫米）→ 本地 x（米）
# 水电图（--mep）：三种图各画哪几类点位（别的点位类排除）、哪几类标高度（default：只标和图例默认高度不同的；给水点没有统一的
# 默认高度，每个都标）、画不画开关到灯的接线。点位一律补纸面符号（灯、强电箱、挂机空调插座在剖切面以上，平面图本身不画）
MEP_CLASSES = ("IfcOutlet", "IfcElectricDistributionBoard", "IfcJunctionBox", "IfcSwitchingDevice", "IfcLightFixture", "IfcValve",
               "IfcWasteTerminal")
KINDS = {"插座布置图": {"keep": ("IfcOutlet", "IfcElectricDistributionBoard", "IfcJunctionBox"), "default": True},
         "开关连线图": {"keep": ("IfcSwitchingDevice", "IfcLightFixture"), "heights": ("IfcSwitchingDevice",), "default": True,
                   "wires": True},
         "给排水点位图": {"keep": ("IfcValve", "IfcWasteTerminal"), "heights": ("IfcValve",), "default": False}} if A.mep else {"": {}}
MEP_CSS = drawset.mep_stylesheet() if A.mep else None
annotate.PAPER_SHIFT = A.mep   # 水电图：同一面墙上的符号沿墙排开，挪开的加引线（点位坐标不动）


def kind_spec(kind, exclude):
    """一种水电图在视图上的设定（spec 里的字段）：排除别的点位类、样式表、补符号、标高度的类和默认高度、接线。"""
    if not A.mep:
        return {"exclude": exclude}
    k = KINDS[kind]
    drop = ", ".join(c for c in MEP_CLASSES if c not in k["keep"])
    return {"exclude": f"{drop} + {exclude}", "stylesheet": MEP_CSS, "symbols_for": k["keep"],
            "heights": k.get("heights", k["keep"]), "defaults": HEIGHTS if k["default"] else {}, "wires": k.get("wires", False)}


def edge_of(pts):
    """建筑外包（米，本地坐标）。"""
    return ((min(p[0] for p in pts) - OX) / 1000, (min(p[1] for p in pts) - OY) / 1000,
            (max(p[0] for p in pts) - OX) / 1000, (max(p[1] for p in pts) - OY) / 1000)


def fit_scale(e, width_mm, height_mm=AVAIL_H, dens=DENS, margin=40.0):
    """A3 图框内放得下的最大比例（margin：轴号、尺寸的留边，图纸毫米）。"""
    w, h = (e[2] - e[0]) * 1000, (e[3] - e[1]) * 1000
    return next((d for d in dens if w / d + margin <= width_mm and h / d + margin <= height_mm), dens[-1])


def pick_scale(plan, with_legend):
    """分区/分段图的比例：按每一侧实际的留边算，取放得下的最大比例；有图例栏时先把图例栏从 58 收窄到 44mm 再试，
    实在放不下才降比例。返回 (比例分母, 图例栏宽)。"""
    env, s = plan["env"], plan["sides"]
    w, h = (env[2] - env[0]) * 1000, (env[3] - env[1]) * 1000
    mx = s["left"]["margin"] + s["right"]["margin"] + 2 * VIEW_PAD
    my = s["bottom"]["margin"] + s["top"]["margin"] + 2 * VIEW_PAD
    for d in DENS:
        if h / d + my > AVAIL_H:
            continue
        room = AVAIL_W - (w / d + mx)
        if not with_legend and room >= 0:
            return d, 0.0
        if with_legend and room >= LEGEND_MIN:
            return d, min(LEGEND_W, int(room * 2) / 2.0)
    return DENS[-1], (LEGEND_W if with_legend else 0.0)


pad = lambda den, mm: mm * den / 1000.0
EXCL = "IfcGrid"   # 轴网由我们自己画（轴号按原图错开），不让 Bonsai 再画一遍
G = annotate.envelope(f)   # 整栋楼的墙、柱、门窗外包（米）
blocks, spans = [], []     # blocks: (分区, 尺寸安排 plan)
for i, k in enumerate(zones):
    if k.get("auto"):
        # 自动分段：裁切框 = 左右切口之间（内部切口外伸 0.5m；首段左端、末段右端、变形缝两侧到外墙面），
        # y 取这一段里构件的外包；构件按几何与裁切框相交纳入（Bonsai 按相机取构件，模型线裁在框上），
        # 跨切口的墙和门窗两张图都画
        cx0 = min(G[0], L(k["crop"][0])) - 0.01 if i == 0 else L(k["crop"][0])
        cx1 = max(G[2], L(k["crop"][1])) + 0.01 if i == len(zones) - 1 else L(k["crop"][1])
        ey = annotate.envelope(f, (cx0, G[1] - 1.0, cx1, G[3] + 1.0)) or G
        crop = (cx0, ey[1] - 0.01, cx1, ey[3] + 0.01)
        kinds = {"left": k["kinds"][0], "right": k["kinds"][1], "bottom": "ext", "top": "ext"}
        cut_pos = {s: L(c) for s, c in zip(("left", "right"), k["cut"]) if c is not None}
        plan = annotate.block_plan(f, crop, kinds, cut_pos)
        spans.append((L(k["axis_x"][0]), L(k["axis_x"][1])))
    else:
        # 方案自带的分区：裁切框 = 分区房间 + 附近墙 + 本分区构件的外包；哪一边紧挨着别的分区按切口处理
        pts = [p for r in sch["rooms"] if zone_of(r) == k["id"] and r["id"] in polys for p in polys[r["id"]]]
        if not pts:
            continue
        e = edge_of(pts)
        near = [p for p in wall_pts if e[0] * 1000 + OX - 400 <= p[0] <= e[2] * 1000 + OX + 400 and e[1] * 1000 + OY - 400 <= p[1] <= e[3] * 1000 + OY + 400]
        e = edge_of(pts + near)
        # 以这个分区实际的墙、柱、门窗为准：伸出去的墙垛、凸窗也算进去；往外最多比按房间算的大 1m
        # （一道贯通的长墙归到本分区时也不能把外包撑大）
        ee = annotate.elements_extent(f, f'IfcElement, JZ_Zone.Block="{k["id"]}"')
        if ee:
            e = (max(ee[0], e[0] - 1.0), max(ee[1], e[1] - 1.0), min(ee[2], e[2] + 1.0), min(ee[3], e[3] + 1.0))
        plan = annotate.block_plan(f, e)
        spans.append((e[0], e[2]))
    blocks.append((k, plan))

if A.mep:   # 水电图只出有点位的段（只布了一户时，别的段是空的）
    mep_xy = [(L(it["x"]), (it["y"] - OY) / 1000.0) for it in sch.get("items", []) if it.get("plan_id") and it["key"] in PACK_KEYS]
    blocks = [(k, pl) for k, pl in blocks if any(pl["crop"][0] <= x <= pl["crop"][2] and pl["crop"][1] <= y <= pl["crop"][3] for x, y in mep_xy)]
# 每个分区/分段的图号；内部切口两侧互注"接 x"（按裁切框找左右紧挨着的那一段）
# 水电图每段三张（插座、开关连线、给排水）连着编号
idents = {(k["id"], kind): f"{PREFIX}-{j * len(KINDS) + ki + 2:02d}" for j, (k, _) in enumerate(blocks) for ki, kind in enumerate(KINDS)}


def neighbor(j, side):
    c = blocks[j][1]["crop"]
    best = None
    for jj, (kk, pl) in enumerate(blocks):
        o = pl["crop"]
        if jj == j or o[3] < c[1] or o[1] > c[3]:
            continue
        if side == "left" and o[0] < c[0] and o[2] >= c[0] - 1.0 and (best is None or o[2] > best[0]):
            best = (o[2], kk["id"])
        if side == "right" and o[2] > c[2] and o[0] <= c[2] + 1.0 and (best is None or o[0] < best[0]):
            best = (o[0], kk["id"])
    return best and best[1]


def detail_views(specs):
    """大比例平面（分区图、分段图，或没有分区时的整层）：房间名、门窗编号、楼面标高、三道尺寸 + 轴号、切口折断线。
    相机 = 外包四周加上每一侧实际的留边；模型线裁在裁切框上（drawset.add_view(crop=…)）。
    顺序（先放的位置要求最死，后放的避开前面已写的字）：小房间、走道的房名先占好位置（只对门窗编号生效，annotate.space_labels
    reserve）→ 门窗编号（贴着洞口）→ 点位高度 → 楼面标高（要一块空地）→
    房间名（在本房间里找空地）→ 尺寸、轴号（数字避让放在室外的编号）→ 折断线。
    楼面标高一套图只用一种写法（annotate.level_style）。
    水电图（kind_spec）不标门窗编号，另有：stylesheet、symbols_for（add_view），heights、defaults（只标这几类点位的高度，同一面墙上挨着的同类
    同高点位只标一个），wires（点位高度之后画开关到灯的接线，后放的字避开它）。
    specs = [{name, den, plan, zone?, strict?, exclude?, crop?, cut_notes?, …}]，返回视图列表（顺序相同）。"""
    made = []
    for sp in specs:
        name, den, plan = sp["name"], sp["den"], sp["plan"]
        env, s = plan["env"], plan["sides"]
        ext = (env[0] - pad(den, s["left"]["margin"]), env[1] - pad(den, s["bottom"]["margin"]),
               env[2] + pad(den, s["right"]["margin"]), env[3] + pad(den, s["top"]["margin"]))
        v = drawset.add_view(name, "PLAN_VIEW", storey.id(), den, ext, margin_paper_mm=VIEW_PAD, exclude=sp.get("exclude", EXCL),
                             crop=plan["crop"] if sp.get("crop", True) else None, stylesheet=sp.get("stylesheet"),
                             symbols_for=sp.get("symbols_for", ()), z=Z)
        drawset.log("reserve", name, annotate.space_labels(f, v, den, mode="full", bbox_m=plan["crop"], abbr=LABELS.get("abbr"),
                                                           short=LABELS.get("short"), zone=sp.get("zone"), strict=sp.get("strict", False),
                                                           plan=plan, reserve=True))
        if not A.mep:   # 水电图不标门窗编号：电工、水工用不着，腾出地方给点位、高度和接线
            drawset.log("tags", name, annotate.opening_tags(f, v, den, z=Z, bbox_m=plan["crop"], plan=plan))
        more = {"classes": sp["heights"], "merge": True} if "heights" in sp else {}
        drawset.log("heights", name, annotate.point_heights(f, v, den, defaults=sp.get("defaults", HEIGHTS), bbox_m=plan["crop"], z=Z, **more))
        if sp.get("wires"):
            drawset.log("wires", name, annotate.wires(f, v, den, z=Z))
        made.append(v)
    style = annotate.level_style(f, [(v, sp["den"], sp["plan"]["crop"]) for v, sp in zip(made, specs)], levels, z=Z)
    for v, sp in zip(made, specs):
        name, den, plan = sp["name"], sp["den"], sp["plan"]
        bb, s = plan["crop"], plan["sides"]
        drawset.log("level", name, style, annotate.level_mark(f, v, den, levels, z=Z, bbox_m=bb, style=style))
        drawset.log("labels", name, annotate.space_labels(f, v, den, mode="full", bbox_m=bb, z=Z, abbr=LABELS.get("abbr"),
                                                          short=LABELS.get("short"), zone=sp.get("zone"), strict=sp.get("strict", False),
                                                          plan=plan))
        drawset.log("dims", name, annotate.grid_annotations(f, v, den, plan["env"], z=Z, mode="block", plan=plan))
        if sp.get("cut_notes"):
            drawset.log("cuts", name, annotate.cut_marks(v, den, plan, sp["cut_notes"], z=Z))
        drawset.log("plan", name, {sd: (plan["kinds"][sd], len(s[sd]["chains"]), [a[0] for a in s[sd]["sel"]], round(s[sd]["margin"], 1))
                                   for sd in annotate.SIDES})
    return made


views = []
E = edge_of([p for g in rec["footprint"] for p in g])
legend_w = {}
if not blocks:
    plan_w = annotate.block_plan(f, (min(E[0], G[0]) - 0.01, min(E[1], G[1]) - 0.01, max(E[2], G[2]) + 0.01, max(E[3], G[3]) + 0.01))
    whole_den, lw0 = pick_scale(plan_w, A.mep and bool(legend))   # 水电图整层一张时图例也在右侧一栏
    if A.mep:
        legend_w["*"] = lw0
if not blocks and whole_den <= 150:
    # 没有分区、整层放得下大比例：总图就是详图（编号、标高、三道尺寸都在这张上）
    wholes = detail_views([dict({"name": TITLE_T.format(storey=STOREY, kind=kind), "den": whole_den, "plan": plan_w, "crop": False},
                                **kind_spec(kind, EXCL + ' + IfcWall, Pset_WallCommon.Status="DEMOLISH"')) for kind in KINDS])
    whole, den = wholes[0], whole_den
else:
    den = fit_scale(E, AVAIL_W)
    ov = M_EXT2 + 2   # 总图只有下、左两侧尺寸（各单元首末轴 + 总尺寸）
    top = 12.0 if blocks else 3.0   # 分区时上方注各段图号
    # 1:200 及更小的总图不画点位、扶手，家具按 drawset.overview_exclude（图例也不列点位，点位看分区图）
    whole = drawset.add_view(TITLE, "PLAN_VIEW", storey.id(), den,
                             (E[0] - pad(den, ov), E[1] - pad(den, ov), E[2] + pad(den, 3), E[3] + pad(den, top)), margin_paper_mm=2,
                             exclude=drawset.overview_exclude(den, has_furniture, EXCL + ' + IfcWall, Pset_WallCommon.Status="DEMOLISH"'))
    drawset.log("tags", annotate.opening_tags(f, whole, den, z=Z))
    drawset.log("labels", annotate.space_labels(f, whole, den, mode="abbr" if den >= 200 else "full", z=Z,
                                                abbr=LABELS.get("abbr"), short=LABELS.get("short")))
    if has_grid:
        drawset.log("grid", annotate.grid_annotations(f, whole, den, E, z=Z, mode="overall", spans=spans or [(E[0], E[2])]))
    if blocks:
        # 组合示意：分区线 + 各段图号（GB/T 50104：分区绘制时要有组合示意图）
        cuts, labs = [], []
        for j, (k, pl) in enumerate(blocks):
            c = pl["crop"]
            if k.get("auto"):
                lo, hi = L(k["x_range"][0]), L(k["x_range"][1])
                lo, hi = max(lo, E[0]), min(hi, E[2])
            else:
                lo, hi = c[0], c[2]
            if j:
                pc = blocks[j - 1][1]["crop"]
                cuts.append(lo if blocks[j - 1][0].get("auto") else (pc[2] + c[0]) / 2)
            ids = [idents[(k["id"], kind)] for kind in KINDS]
            labs.append(((lo + hi) / 2, ids[0] if len(ids) == 1 else f"{ids[0]}～{ids[-1]}", k["id"]))
        drawset.log("zone key", annotate.zone_key(whole, den, E, cuts, labs, z=Z))
if not blocks and whole_den <= 150:
    views += [(f"{PREFIX}-{i:02d}", w, den, "*" if A.mep else None) for i, w in enumerate(wholes, 1)]
else:
    views.append((f"{PREFIX}-01", whole, den, None))

specs = []
for j, (k, plan) in enumerate(blocks):
    zden, lw = pick_scale(plan, bool(legend))
    legend_w[k["id"]] = lw
    drawset.log("zone", k["id"], "extent(m)", [round(x, 2) for x in plan["env"]], "scale", zden, "legend", lw,
                "kinds", plan["kinds"])
    for kind in KINDS:
        notes_cut = {s: f"接 {idents[(nb, kind)]}" for s in ("left", "right") if plan["kinds"][s] == "cut" for nb in [neighbor(j, s)] if nb}
        notes_cut.update({s: "" for s in ("left", "right") if plan["kinds"][s] == "cut" and s not in notes_cut})
        specs.append(dict({"name": ZONE_TITLE.format(id=k["id"], storey=STOREY, kind=kind), "den": zden, "plan": plan, "zone": k["id"],
                           "strict": bool(k.get("auto")), "cut_notes": notes_cut, "kind": kind}, **kind_spec(kind, EXCL)))
for sp, v in zip(specs, detail_views(specs)):
    views.append((idents[(sp["zone"], sp["kind"])], v, sp["den"], sp["zone"]))

for _, v, _, _ in views:
    drawset.render_view(v)

crops = {k["id"]: pl["crop"] for k, pl in blocks}   # 分区/分段的裁切框（米）；"*" 是水电图整层一张（不裁切）
crops["*"] = None
# 一套图的图例同宽（取各分区图图例栏里最窄的），同一条名称在各张图上断法一致
LEG_W = (min(legend_w.values()) if legend and legend_w else LEGEND_W) - 2
# 图面上不写说明：统计、自查结论、拆改计数只在报告、xlsx、IFC 里；分区、分段靠总图上的分区线和各段图号、分段图的"接 x-xx"索引
sheet_list = []   # (图号, 图名, 比例, 图幅, 备注)
for ident, v, den, zid in views:
    # 图例的点位行只列这张图上实际画出的种类（平面图上不画的吸顶监控等不列）；写过简称的（总图，1:150 上写不下全名的小房间）
    # 末尾一行是简称对照
    drawn = drawset.drawn_symbol_keys(v) if legend and den < 200 else set()
    keys = [k for k in legend if k in drawn] if drawn else []
    abbr = annotate.abbr_legend(v)
    if zid:
        # 分区/分段图：有设施点位时图例在右侧预留的一栏（墙体行并在里面）；没有点位的不留栏，墙体图例找图面上的空地放，
        # 视图位置和比例都不变，放不下就不画（JZLOG legend skipped）
        lw = legend_w[zid] if legend else 0.0
        sheet, svg = drawset.make_sheet(v, ident, v.Name, PROJECT_SHORT, f"1:{den}", today, reserve_right_mm=lw)
        walls = annotate.wall_materials(f, v, bbox_m=crops[zid])
        if legend:
            drawset.add_legend(svg, SYMBOLS, keys, den, x=AREA_R - lw + 3, y=24, width=LEG_W, walls=walls, extra=abbr or None,
                               leader=A.mep and drawset.has_shift_leaders(v))   # 水电图上有挪开画的符号才加引线一行
        else:
            drawset.place_legend(svg, SYMBOLS, [], den, near=(AREA_R + 1 - LEG_W, 24), width=LEG_W, walls=walls, extra=abbr or None)
    else:
        # 总图：图例在视图标题下面——画了点位时的点位行、墙体行，简称对照（小比例总图的 --labels 的 legend_text 接在后面作
        # 补充说明，这一行宽 180）
        sheet, svg = drawset.make_sheet(v, ident, v.Name, PROJECT_SHORT, f"1:{den}", today)
        abbr_text = " ".join(t for t in (abbr, LABELS.get("legend_text") if den >= 200 else "") if t)
        drawset.add_legend(svg, SYMBOLS, keys, den, x=36, y=drawset.view_title_bottom(sheet) + 9,
                           width=LEG_W, walls=annotate.wall_materials(f, v), extra=abbr_text or None, extra_width=180)
    drawset.export_pdf(svg, os.path.join(OUT, f"{ident}.pdf"), f"{ident} {v.Name}")
    drawset.export_png(svg, os.path.join(OUT, f"{ident}.png"))
    sheet_list.append((ident, v.Name, f"1:{den}", "A3", ""))

# 表格（门窗表；水电图是回路表、点位统计表）：从 IFC 统计，xlsx → Bonsai Schedules → 图纸；一页放不下就分几张图，每页表下都写脚注。
# 喂给 Bonsai 的表格（断好行的 xlsx、同名 CSS、Bonsai 画的 SVG）是中间文件，放 work/schedules/；交付的 xlsx 放输出目录顶层
sched_dir = os.path.join(OUT, drawset.WORK, "schedules")
TABLE_TOP, AREA_BOTTOM = 24.0, drawset.AREA[3]


def mep_tables():
    """回路表（每户每路：点位数、所在房间、漏电保护；导线、断路器留给电工）和点位统计表（每种点位的数量；房间 × 种类的明细
    只进交付的 点位明细.xlsx）：都从 IFC 查
    （回路是 IfcDistributionCircuit，点位是带 Tag（plan_id）的符号库构件，房间是它所在的 IfcSpace）。"""
    import ifcopenshell.util.element as uel
    import ifcopenshell.util.system as usys
    from jiazhuang import mep
    room = lambda e: (lambda sp: (sp.LongName or sp.Name) if sp is not None and sp.is_a("IfcSpace") else "")(uel.get_container(e))
    unit = lambda e: (lambda sp: uel.get_pset(sp, "JZ_Space", "Unit") or "" if sp is not None and sp.is_a("IfcSpace") else "")(uel.get_container(e))
    order = list(mep.CIRCUITS)
    pre = lambda c: c.Name.rstrip("0123456789")
    crows = []
    for c in sorted(f.by_type("IfcDistributionCircuit"),
                    key=lambda c: (c.Description or "", order.index(pre(c)) if pre(c) in order else 99, int(c.Name[len(pre(c)):] or 0))):
        els = sorted(usys.get_system_elements(c), key=lambda e: e.Tag or "")
        rooms = list(dict.fromkeys(room(e) for e in els if room(e)))
        crows.append([c.Description or "", c.Name, c.LongName or "", len(els), "、".join(rooms),
                      "30mA" if mep.CIRCUITS.get(pre(c), {}).get("rcd") else "", "", ""])
    chead = ["户", "回路", "用途", "点位数", "所在房间", "漏电保护", "导线", "断路器"]
    pts = [e for e in f.by_type("IfcDistributionElement") if e.Tag and getattr(uel.get_type(e), "Tag", None) in PACK_KEYS]
    keys = [t for t in PACK_KEYS if any(uel.get_type(e).Tag == t for e in pts)]   # 符号库的顺序
    by_key = {t: sum(uel.get_type(e).Tag == t for e in pts) for t in keys}
    prow = [[PACK_KEYS[t]["name"], PACK_KEYS[t]["legend"], f"H{PACK_KEYS[t]['default_height']:g}" if "default_height" in PACK_KEYS[t] else "—",
             by_key[t]] for t in keys] + [["合计", "", "", len(pts)]]
    phead = ["点位", "图例名称", "默认高度", "数量"]
    # 交付另给一份明细（户、房间 × 点位种类）：种类多，按列排放不下 A3，只进 xlsx
    names = [PACK_KEYS[t]["name"] for t in keys]
    count = {}
    for e in pts:
        c = count.setdefault((unit(e), room(e)), {})
        n = PACK_KEYS[uel.get_type(e).Tag]["name"]
        c[n] = c.get(n, 0) + 1
    drow = [[u, r] + [c.get(n, "") for n in names] + [sum(c.values())] for (u, r), c in sorted(count.items())]   # 按户、房间排
    drow.append(["合计", ""] + [sum(c.get(n, 0) for c in count.values()) for n in names] + [len(pts)])
    schedules.write_delivery(os.path.join(OUT, "点位明细.xlsx"), "点位明细", ["户", "房间"] + names + ["合计"], drow, subtitle=PROJECT)
    note = ["导线、断路器规格由电工确定；漏电保护按 GB 55038-2025 第 7.4.3 条（待原文核对）。"]
    return [("回路表", "circuit_schedule", chead, crows, {4: 120.0}, {2: "left", 4: "left"}, note),
            ("点位统计表", "point_schedule", phead, prow, {1: 90.0}, {0: "left", 1: "left"}, [])]


if A.mep:
    TABLES = mep_tables()
else:
    head, rows, dw = openings.schedule_rows(f, annotate.plan_geometry(f))
    if not rows:
        drawset.log("WARNING no doors/windows in model, door/window schedule sheet skipped")
    TABLES = [("门窗表", "door_window_schedule", head, rows, {head.index("所在位置"): 110.0, head.index("备注"): 62.0},
               {head.index("所在位置"): "left", head.index("备注"): "left"}, openings.schedule_notes(dw))]
n_sheet = len(views)
for title, stem, head, rows, widths, align, foot in TABLES:
    max_table_h = AREA_BOTTOM - TABLE_TOP - drawset.footnote_mm(len(foot))   # 改它会改变分页数和后面各张的图号
    if rows:
        schedules.write_delivery(os.path.join(OUT, f"{title}.xlsx"), title, head, rows, footnote=foot, subtitle=PROJECT)
    pages = schedules.paginate(head, rows, max_table_h, widths) if rows else []
    drawset.log(f"{stem.replace('_', ' ')} pages" if A.mep else "door/window schedule pages", len(pages), "table height budget", round(max_table_h, 1))
    for i, part in enumerate(pages):
        xp = os.path.join(sched_dir, f"{stem}.xlsx" if len(pages) == 1 else f"{stem}_p{i + 1}.xlsx")
        schedules.write_table(xp, head, part, max_width_mm=widths, align=align)
        name = title if len(pages) == 1 else f"{title}（{i + 1}/{len(pages)}）"
        n_sheet += 1
        dw_ident = f"{PREFIX}-{n_sheet:02d}"
        sheet, svg = drawset.make_table_sheet([(xp, name)], dw_ident, name, PROJECT_SHORT, today, origin=(40.0, TABLE_TOP), footnote=foot)
        drawset.export_pdf(svg, os.path.join(OUT, f"{dw_ident}.pdf"), f"{dw_ident} {name}")
        drawset.export_png(svg, os.path.join(OUT, f"{dw_ident}.png"))
        sheet_list.append((dw_ident, name, "—", "A3", ""))

# 图纸目录（排在最前）；备注一栏留空，给人手写
idx_ident = f"{PREFIX}-00"
entries = [(idx_ident, "图纸目录", "—", "A3", "")] + [(t, n, "—", "A4", "") for t, n in attachments] + sheet_list
entries.sort(key=lambda e: e[0])
idx_head = ["序号", "图号", "图名", "比例", "图幅", "备注"]
idx_rows = [[i, *e] for i, e in enumerate(entries, 1)]
xp = os.path.join(sched_dir, "drawing_index.xlsx")
schedules.write_table(xp, idx_head, idx_rows, align={2: "left", 5: "left"}, min_width_mm=14.0)
schedules.write_delivery(os.path.join(OUT, "图纸目录.xlsx"), "图纸目录", idx_head, idx_rows, subtitle=PROJECT,
                         align={0: "center", 1: "center", 3: "center", 4: "center"})
sheet, svg = drawset.make_table_sheet([(xp, "图纸目录")], idx_ident, "图纸目录", PROJECT_SHORT, today)
drawset.export_pdf(svg, os.path.join(OUT, f"{idx_ident}.pdf"), f"{idx_ident} 图纸目录")
drawset.export_png(svg, os.path.join(OUT, f"{idx_ident}.png"))

bpy.ops.bim.save_project(filepath=ifc_path)
printing.done(OUT)
