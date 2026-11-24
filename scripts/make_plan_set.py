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
    p.add_argument("--title", help="总图名，可用 {storey}、{unit}（缺省\"{storey}平面图\"，有分区或分段时\"{storey}组合平面图\"，"
                                   "--unit 时\"{unit} 平面图\"）")
    p.add_argument("--zone-title", help="分区、分段平面的图名，可用 {storey}、{id}（缺省：分段\"{storey} {id}平面图\"，分区\"{id} 分区平面\"）")
    p.add_argument("--prefix", default="A", help="图号前缀（缺省：%(default)s）")
    p.add_argument("--mep", action="store_true",
                   help="水电图：每个平面换成插座布置图、开关连线图、给排水点位图三张（方案要有 scripts/mep.py 布的点位），"
                        "表格是回路表、点位统计表，不出门窗表、不标门窗编号；--title、--zone-title 可用 {kind}（图的种类，没写时空一格接在图名后面）。"
                        "同 --kinds mep")
    p.add_argument("--kinds", metavar="plan,floor,ceiling,mep",
                   help="出哪几种图（逗号分隔）：plan 平面（布置）图、floor 地面铺装图（方案要有 scripts/flooring.py 生成的地面）、"
                        "ceiling 顶面布置图（方案要有 scripts/ceiling.py 生成的吊顶）、mep 水电三张；不论写的先后，按平面 → 地面 → 顶面 → 水电排，"
                        "表格跟在后面（门窗表、地面材料表、回路表、点位统计表）。缺省 plan")
    p.add_argument("--split", default="auto", choices=("auto", "none"),
                   help="方案没有分区、整层一张 A3 放不下 1:100 时，auto 自动沿 x 切成几段出分段平面，none 不切（缺省：%(default)s）")
    p.add_argument("--unit", metavar="户号",
                   help="只出这一户（识别结果里的户 id，如 U1）的一整套图：只画这一户的房间和围着它的墙，比例取 A3 放得下的最大一档（1:150 也放不下时退出 1）；"
                        "不分区、不分段（--split、--zone-title 不起作用）")
    a = p.parse_args(argv)
    if err := conventions.check_pack():
        p.error(err)
    kinds = [k.strip() for k in (a.kinds or ("mep" if a.mep else "plan")).split(",") if k.strip()]
    if bad := [k for k in kinds if k not in KIND_ORDER]:
        p.error(f"没有图种 {'、'.join(bad)}（可用：{'、'.join(KIND_ORDER)}）")
    if not kinds:
        p.error("--kinds 至少写一种图")
    if a.mep and "mep" not in kinds:
        kinds.append("mep")
    a.kinds = [k for k in KIND_ORDER if k in kinds]   # 按固定顺序排，和写的先后无关
    a.mep = "mep" in a.kinds
    own = None
    if a.unit is not None:   # 户号在解析参数时就查：不存在退出 2，列出识别结果里的户
        from jiazhuang import unitview
        try:
            rec = printing.read_json(a.recognition, "识别结果")
            sch = printing.read_json(a.scheme, "方案") if a.scheme else None
        except argparse.ArgumentTypeError as e:
            p.error(str(e))
        ids = unitview.unit_ids(rec, sch)
        if a.unit not in ids:
            p.error(f"没有户 {a.unit}（识别结果里的户：{'、'.join(ids) or '无'}）")
        own = {r["id"] for r in ((sch or {}).get("after") or rec)["rooms"] if r.get("unit") == a.unit}
    if a.mep:   # 水电图要有 scripts/mep.py 布过点位的方案，没有点位时三张图都是空的：解析参数时就报错（退出码 2）
        from jiazhuang import mep
        try:
            items = printing.read_json(a.scheme, "方案").get("items", []) if a.scheme else []
        except argparse.ArgumentTypeError as e:
            p.error(str(e))
        if not isinstance(items, list) or not any(isinstance(it, dict) and mep.point_type(it) and (own is None or it.get("room") in own)
                                                  for it in items):
            p.error("--mep 要给带点位的方案（scripts/mep.py 的输出），用 --scheme 指定" if not a.scheme
                    else f"--mep 要给带点位的方案（scripts/mep.py 的输出）：{a.scheme} 里没有{'' if own is None else ' ' + a.unit + ' 的'}点位")
    if "ceiling" in a.kinds:   # 顶面布置图要有 scripts/ceiling.py 生成的吊顶：没有时解析参数就报错（退出码 2）
        try:
            ceil = printing.read_json(a.scheme, "方案").get("ceilings") if a.scheme else None
        except argparse.ArgumentTypeError as e:
            p.error(str(e))
        if not isinstance(ceil, list) or not ceil or (own is not None and not any(isinstance(c, dict) and c.get("room") in own for c in ceil)):
            p.error("--kinds ceiling 要给带吊顶的方案（scripts/ceiling.py 的输出），用 --scheme 指定" if not a.scheme
                    else f"--kinds ceiling 要给带吊顶的方案（scripts/ceiling.py 的输出）：{a.scheme} 里没有{'' if own is None else ' ' + a.unit + ' 的'}吊顶")
    if "floor" in a.kinds:   # 地面铺装图要有 scripts/flooring.py 生成的地面：没有时解析参数就报错（退出码 2）
        try:
            fl = printing.read_json(a.scheme, "方案").get("flooring") if a.scheme else None
        except argparse.ArgumentTypeError as e:
            p.error(str(e))
        zs = fl.get("zones") if isinstance(fl, dict) else None
        if not isinstance(zs, list) or not zs or (own is not None and not any(isinstance(z, dict) and set(z.get("rooms") or []) & own for z in zs)):
            p.error("--kinds floor 要给带地面的方案（scripts/flooring.py 的输出），用 --scheme 指定" if not a.scheme
                    else f"--kinds floor 要给带地面的方案（scripts/flooring.py 的输出）：{a.scheme} 里没有{'' if own is None else ' ' + a.unit + ' 的'}地面")
    return a


KIND_ORDER = ("plan", "floor", "ceiling", "mep")   # --kinds 认的图种，也是一套图里的顺序（顶面一种出顶面布置图、灯具定位图两张）


A = parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:] if sys.argv[0].endswith(".py") else [])

import bpy  # noqa: E402
import bonsai.tool as tool  # noqa: E402
from jiazhuang import annotate, checker, drawset, openings, plan2ifc, renovate, scheme, schedules, symbols, unitview  # noqa: E402

SYMBOLS = conventions.pack_file("symbols.json")
HEIGHTS = symbols.default_heights(SYMBOLS)   # 点位默认安装高度：图例写它，图上只标和它不同的
PROJECT, BUILDING, PREFIX, STOREY, RULES, SPLIT = A.project, A.building, A.prefix, A.storey_name, A.rules, A.split
PROJECT_SHORT = A.short if A.short is not None else A.project


def read_json(p):
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


LABELS = read_json(A.labels) if A.labels else {}

OUT = os.path.abspath(A.out)
rec0 = read_json(A.recognition)   # 识别结果（拆改前）
sch = read_json(A.scheme) if A.scheme else scheme.from_recognition(rec0)
if A.unit is not None:   # 按户出图：识别结果和方案裁成只剩这一户（unitview.subset），后面照整层出
    # 这一户的门通向的别的房间：走道、前室是正常的；户内房间漏归户的话不会画在任何一户的图上
    drawset.log("unit", A.unit, "doors lead to rooms not in the unit:", "、".join(unitview.outside_rooms(rec0, sch, A.unit)) or "-")
    rec0, sch = unitview.subset(rec0, sch, A.unit)
rec = sch.get("after") or rec0   # 有拆改时，出图和外包都用拆改后的几何
# 方案里的吊顶、地面数据和现在的灯、门、地漏对不上（改了吊顶没重跑 mep.py、水电重跑后没重跑 flooring.py）时不出错图：
# 一处一行 JZLOG stale，退出 1。没有吊顶、没有地面数据的方案不进这两个判断
STALE = []
if sch.get("ceilings"):
    from jiazhuang import ceiling as _ceiling
    STALE += [v["msg"] for v in _ceiling.stale_lights(rec0, sch)]
if sch.get("flooring"):
    from jiazhuang import flooring as _flooring
    STALE += [v["msg"] for v in _flooring.check(rec, sch)]
if STALE:
    for _m in STALE[:30]:
        printing.log("stale", _m)
    printing.log("failed", f"方案里的吊顶、地面数据过时了（{len(STALE)} 处），按上面的提示重跑后再出图")
    sys.exit(1)

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


if A.unit is not None:   # 按户出图：A3 上 1:150 也放不下这一户时一行错、退出 1，不建输出目录（留边按三道尺寸 + 轴号、另加图例栏估）
    _x = [p[0] for g in rec["footprint"] for p in g]
    _y = [p[1] for g in rec["footprint"] for p in g]
    _leg = LEGEND_MIN if any(it["key"] in symbols.load_pack(SYMBOLS) for it in sch.get("items", [])) or "ceiling" in A.kinds \
        or "floor" in A.kinds else 0.0
    if not fits_mm(max(_x) - min(_x) + _leg * 150, max(_y) - min(_y), 150):
        printing.log("failed", f"unit too large: {A.unit} 外包 {(max(_x) - min(_x)) / 1000:.1f}×{(max(_y) - min(_y)) / 1000:.1f}m，"
                               "A3 上 1:150 也放不下")
        sys.exit(1)
os.makedirs(OUT, exist_ok=True)

# 方案没有分区、整层放不下 1:100（含三道尺寸）时自动分段（只沿 x 切）
fp_x = [p[0] for g in rec["footprint"] for p in g]
fp_y = [p[1] for g in rec["footprint"] for p in g]
auto_split = False
if SPLIT != "none" and A.unit is None and not (sch.get("zones") or sch.get("cores")) and not fits_mm(max(fp_x) - min(fp_x), max(fp_y) - min(fp_y), 100):
    depth = max(fp_y) - min(fp_y)
    seg_den = next((d for d in (100, 150) if fits_mm(1, depth, d)), 150)
    if not fits_mm(1, depth, seg_den):
        drawset.log("WARNING depth too large for 1:150, segments will drop to a smaller scale (no opening tags below 1:150)", depth)
    auto_split = bool(scheme.split_zones(rec, sch, lambda lk, rk: (AVAIL_W - est_margin(lk, "left") - est_margin(rk, "right") - 2 * VIEW_PAD) * seg_den))
    drawset.log("split", [(z["id"], z["kinds"], [round(v) for v in z["crop"]], z["wall_cut"]) for z in sch.get("zones", [])])
zones = sch.get("zones") or sch.get("cores") or []
MIXED = A.kinds not in (["plan"], ["mep"])   # 几种图一起出（或单出顶面）：每张图名都带种类
TITLE_T = A.title or ("{unit} {kind}" if A.unit is not None and (A.mep or MIXED) else "{unit} 平面图" if A.unit is not None
                      else "{storey}组合平面图" if zones else "{storey}{kind}" if A.mep or MIXED else "{storey}平面图")
TITLE = TITLE_T.format(storey=STOREY, kind="", unit=A.unit or "")
ZONE_TITLE = A.zone_title if A.zone_title is not None else ("{id} {kind}" if A.mep or MIXED else "{storey} {id}平面图" if auto_split else "{id} 分区平面")
if A.mep or MIXED:   # 水电图一个平面出三张：自己写的图名里没有 {kind} 时空一格接在后面（"示例户型 插座布置图"），三张不同名
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
HAS_LEG = bool(legend) or "floor" in A.kinds   # 地面铺装图总有图例（材料编号对照）；没有地面图时就是 bool(legend)
has_furniture = any(it["key"] not in PACK_KEYS for it in sch.get("items", []))
L = lambda v: (v - OX) / 1000.0     # 图纸 x（毫米）→ 本地 x（米）
# 水电图（--mep）：三种图各画哪几类点位（别的点位类排除）、哪几类标高度（default：只标和图例默认高度不同的；给水点没有统一的
# 默认高度，每个都标）、画不画开关到灯的接线。点位一律补纸面符号（灯、强电箱、挂机空调插座在剖切面以上，平面图本身不画）
MEP_CLASSES = ("IfcOutlet", "IfcElectricDistributionBoard", "IfcJunctionBox", "IfcSwitchingDevice", "IfcLightFixture", "IfcValve",
               "IfcWasteTerminal")
# 图种表：图名里的种类 → 设定。plan 一种（单出平面时图名不带种类）；ceiling 顶面布置图（REFLECTED_PLAN_VIEW，画灯、吊顶说明、
# 造型尺寸，不标门窗编号和楼面标高，房名不写面积）；mep 三种（keep：留下的点位类）。一套图按这里的顺序排
MEP_KINDS = {"插座布置图": {"keep": ("IfcOutlet", "IfcElectricDistributionBoard", "IfcJunctionBox"), "default": True},
             "开关连线图": {"keep": ("IfcSwitchingDevice", "IfcLightFixture"), "heights": ("IfcSwitchingDevice",), "default": True,
                       "wires": True},
             "给排水点位图": {"keep": ("IfcValve", "IfcWasteTerminal"), "heights": ("IfcValve",), "default": False}}
KINDS = {}
if "plan" in A.kinds:
    KINDS[("平面布置图" if has_furniture else "平面图") if MIXED else ""] = {"plan": True}
if "floor" in A.kinds:   # 地面铺装图：墙、门窗、地漏、砖缝（annotate.floor_joints）、门槛石、压条、起铺点、湿区标高；不画家具洁具
    KINDS["地面铺装图"] = {"floor": True}
if "ceiling" in A.kinds:   # 顶面两张：顶面布置图（造型、标高、材料、灯）、灯具定位图（灯和定位尺寸，造型线淡显）
    KINDS["顶面布置图"] = {"ceiling": True, "keep": ("IfcLightFixture",)}
    KINDS["灯具定位图"] = {"ceiling": True, "lights": True, "keep": ("IfcLightFixture",)}
if A.mep:
    KINDS.update(MEP_KINDS)
MEP_CSS = drawset.mep_stylesheet() if A.mep else None
CEIL_CSS = drawset.ceiling_stylesheet() if "ceiling" in A.kinds else None
LIGHTS_CSS = drawset.lights_stylesheet() if "ceiling" in A.kinds else None
FLOOR_CSS = drawset.floor_stylesheet() if "floor" in A.kinds else None
# 灯带（只在有吊顶的方案里有）不按构件投影画，各视图另画沿灯槽的虚线（annotate.light_strips）
NO_STRIP = f' + IfcLightFixture, query:"type.Tag"="{annotate.STRIP_KEY}"' if any(it["key"] == annotate.STRIP_KEY for it in sch.get("items", [])) else ""
annotate.PAPER_SHIFT = A.mep   # 水电图：同一面墙上的符号沿墙排开，挪开的加引线（点位坐标不动）


def kind_spec(kind, exclude):
    """一种图在视图上的设定（spec 里的字段）：平面图只有排除（和水电一起出时不画点位）；顶面布置图：视图类型、排除别的点位类、
    样式表、补灯的符号、不标门窗编号和楼面标高、房名不写面积、吊顶说明和造型尺寸；水电图：排除别的点位类、样式表、补符号、
    标高度的类和默认高度、接线。"""
    k = KINDS[kind]
    if k.get("plan"):
        return {"exclude": f"{', '.join(MEP_CLASSES)} + {exclude}" if MIXED and A.mep else exclude}
    if k.get("floor"):   # 地面铺装图：平面视图，只留墙、门窗和地漏（排水点、别的点位、家具洁具都排除），房名下写材料
        drop = ", ".join([c for c in MEP_CLASSES if c != "IfcWasteTerminal"] + ["IfcFurniture", "IfcFurnishingElement", "IfcSanitaryTerminal", "IfcElectricAppliance"])
        return {"exclude": f'{drop} + IfcWasteTerminal, query:"type.Tag"="cn_drain" + {exclude}', "stylesheet": FLOOR_CSS, "tags": False,
                "no_heights": True, "floor": True, "floor_level": False, "labels": "name"}
    if k.get("ceiling"):
        drop = ", ".join([c for c in MEP_CLASSES if c not in k["keep"]] + ["IfcFurniture", "IfcFurnishingElement", "IfcSanitaryTerminal"])
        return {"target": "REFLECTED_PLAN_VIEW", "exclude": f"{drop} + {exclude}{NO_STRIP}", "stylesheet": LIGHTS_CSS if k.get("lights") else CEIL_CSS,
                "symbols_for": k["keep"], "tags": False, "no_heights": True, "ceiling": True, "lights": k.get("lights", False),
                "floor_level": False, "labels": "name"}
    drop = ", ".join(c for c in MEP_CLASSES if c not in k["keep"])
    return {"exclude": f"{drop} + {exclude}{NO_STRIP if k.get('wires') else ''}", "stylesheet": MEP_CSS, "symbols_for": k["keep"], "tags": False,
            "heights": k.get("heights", k["keep"]), "defaults": HEIGHTS if k["default"] else {}, "wires": k.get("wires", False)}


def edge_of(pts):
    """建筑外包（米，本地坐标）。"""
    return ((min(p[0] for p in pts) - OX) / 1000, (min(p[1] for p in pts) - OY) / 1000,
            (max(p[0] for p in pts) - OX) / 1000, (max(p[1] for p in pts) - OY) / 1000)


def fit_scale(e, width_mm, height_mm=AVAIL_H, dens=DENS, margin=40.0):
    """A3 图框内放得下的最大比例（margin：轴号、尺寸的留边，图纸毫米）。"""
    w, h = (e[2] - e[0]) * 1000, (e[3] - e[1]) * 1000
    return next((d for d in dens if w / d + margin <= width_mm and h / d + margin <= height_mm), dens[-1])


def pick_scale(plan, with_legend, dens=DENS):
    """分区/分段图的比例：按每一侧实际的留边算，取放得下的最大比例；有图例栏时先把图例栏从 58 收窄到 44mm 再试，
    实在放不下才降比例。返回 (比例分母, 图例栏宽)。"""
    env, s = plan["env"], plan["sides"]
    w, h = (env[2] - env[0]) * 1000, (env[3] - env[1]) * 1000
    mx = s["left"]["margin"] + s["right"]["margin"] + 2 * VIEW_PAD
    my = s["bottom"]["margin"] + s["top"]["margin"] + 2 * VIEW_PAD
    for d in dens:
        if h / d + my > AVAIL_H:
            continue
        room = AVAIL_W - (w / d + mx)
        if not with_legend and room >= 0:
            return d, 0.0
        if with_legend and room >= LEGEND_MIN:
            return d, min(LEGEND_W, int(room * 2) / 2.0)
    return dens[-1], (LEGEND_W if with_legend else 0.0)


pad = lambda den, mm: mm * den / 1000.0
EXCL = "IfcGrid"   # 轴网由我们自己画（轴号按原图错开），不让 Bonsai 再画一遍
if sch.get("flooring"):   # 地面面层（楼面以下 1mm）不按构件投影画：地面铺装图另画砖缝、门槛石，别的图和没有地面数据时一样
    EXCL += " + IfcCovering, PredefinedType=FLOORING"
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
        v = drawset.add_view(name, sp.get("target", "PLAN_VIEW"), storey.id(), den, ext, margin_paper_mm=VIEW_PAD, exclude=sp.get("exclude", EXCL),
                             crop=plan["crop"] if sp.get("crop", True) else None, stylesheet=sp.get("stylesheet"),
                             symbols_for=sp.get("symbols_for", ()), z=Z)
        if sp.get("ceiling"):   # 顶面布置图、灯具定位图：小段尺寸数字也自己放（annotate._dim_plan）；造型线、灯带先登记成放字的障碍
            annotate.RCP_OWN_TEXT.add(v.id())
            annotate.ceiling_lines(f, v, den)
        if (sp.get("ceiling") or sp.get("wires")) and NO_STRIP:
            drawset.log("strips", name, annotate.light_strips(f, v, den, z=Z))
        drawset.log("reserve", name, annotate.space_labels(f, v, den, mode=sp.get("labels", "full"), bbox_m=plan["crop"], abbr=LABELS.get("abbr"),
                                                           short=LABELS.get("short"), zone=sp.get("zone"), strict=sp.get("strict", False),
                                                           plan=plan, reserve=True))
        if sp.get("tags", True):   # 水电图、顶面图不标门窗编号：电工、水工用不着，腾出地方给点位、高度和接线
            drawset.log("tags", name, annotate.opening_tags(f, v, den, z=Z, bbox_m=plan["crop"], plan=plan))
        more = {"classes": sp["heights"], "merge": True} if "heights" in sp else {}
        if not sp.get("no_heights"):
            drawset.log("heights", name, annotate.point_heights(f, v, den, defaults=sp.get("defaults", HEIGHTS), bbox_m=plan["crop"], z=Z, **more))
        if sp.get("lights"):   # 灯具定位图：主灯到墙、成排筒灯的尺寸链；不写标高和材料
            drawset.log("light dims", name, annotate.light_dims(f, v, den, z=Z))
        elif sp.get("ceiling"):   # 顶面布置图：造型尺寸先画（尺寸线登记进占位表），再写各块吊顶的材料和底标高
            drawset.log("ceiling dims", name, annotate.ceiling_dims(f, v, den, z=Z))
            drawset.log("ceiling marks", name, annotate.ceiling_marks(f, v, den, z=Z, bbox_m=plan["crop"]),
                        "skipped", annotate.CEIL_SKIPPED.get(v.id(), 0))
        if sp.get("wires"):
            drawset.log("wires", name, annotate.wires(f, v, den, z=Z))
        made.append(v)
    style = annotate.level_style(f, [(v, sp["den"], sp["plan"]["crop"]) for v, sp in zip(made, specs)], levels, z=Z)
    for v, sp in zip(made, specs):
        name, den, plan = sp["name"], sp["den"], sp["plan"]
        bb, s = plan["crop"], plan["sides"]
        if sp.get("floor_level", True):
            drawset.log("level", name, style, annotate.level_mark(f, v, den, levels, z=Z, bbox_m=bb, style=style))
        if sp.get("floor"):   # 地面铺装图：起铺点记号先画，房名（带材料）在本房间里先占位置，定位尺寸、标高、找坡再找地方
            drawset.log("floor starts", name, annotate.floor_starts(f, v, den, z=Z))
        drawset.log("labels", name, annotate.space_labels(f, v, den, mode=sp.get("labels", "full"), bbox_m=bb, z=Z, abbr=LABELS.get("abbr"),
                                                          short=LABELS.get("short"), zone=sp.get("zone"), strict=sp.get("strict", False),
                                                          plan=plan, **({"second": annotate.floor_label_text(f)} if sp.get("floor") else {})))
        if sp.get("floor"):   # 地面铺装图：定位尺寸、标高、找坡、门槛石、压条（房名之后，都避开已写的字）
            drawset.log("floor dims", name, annotate.floor_dims(f, v, den, z=Z))
            drawset.log("floor levels", name, annotate.floor_levels(f, v, den, z=Z))
            drawset.log("floor falls", name, annotate.floor_falls(f, v, den, z=Z))
            drawset.log("floor marks", name, annotate.floor_marks(f, v, den, z=Z))
        drawset.log("dims", name, annotate.grid_annotations(f, v, den, plan["env"], z=Z, mode="block", plan=plan))
        if sp.get("floor"):   # 砖缝最后画，在字、尺寸、标高、符号处断开
            drawset.log("floor joints", name, annotate.floor_joints(f, v, den, z=Z))
        if sp.get("ceiling") and not sp.get("lights"):   # 顶面布置图：铝扣板的板缝网格，在灯具符号和字处断开
            drawset.log("alu grid", name, annotate.alu_grid(f, v, den, z=Z))
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
    # 水电图、按户出图整层一张时图例也在右侧一栏；按户出图（--unit）的比例档细一些（unitview.DENS）
    whole_den, lw0 = pick_scale(plan_w, (A.mep or "ceiling" in A.kinds or "floor" in A.kinds or A.unit is not None) and HAS_LEG,
                                unitview.DENS if A.unit is not None else DENS)
    if A.unit is not None and whole_den > 150:   # 按实际留边放不下 1:150：同样报错（输出目录里只有 model.ifc 和 work/，没有图）
        e_ = plan_w["env"]
        printing.log("failed", f"unit too large: {A.unit} 外包 {e_[2] - e_[0]:.1f}×{e_[3] - e_[1]:.1f}m，A3 上 1:150 也放不下")
        sys.exit(1)
    if A.mep or "ceiling" in A.kinds or "floor" in A.kinds or A.unit is not None:
        legend_w["*"] = lw0
if not blocks and whole_den <= 150:
    # 没有分区、整层放得下大比例：总图就是详图（编号、标高、三道尺寸都在这张上）；拆除的墙照画（图例带"拆除"），不像 1:200 总图那样排除
    wholes = detail_views([dict({"name": TITLE_T.format(storey=STOREY, kind=kind, unit=A.unit or ""), "den": whole_den, "plan": plan_w, "crop": False},
                                **kind_spec(kind, EXCL)) for kind in KINDS])
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
    views += [(f"{PREFIX}-{i:02d}", w, den, "*" if A.mep or "ceiling" in A.kinds or "floor" in A.kinds or A.unit is not None else None)
              for i, w in enumerate(wholes, 1)]
else:
    views.append((f"{PREFIX}-01", whole, den, None))

specs = []
for j, (k, plan) in enumerate(blocks):
    zden, lw = pick_scale(plan, HAS_LEG)
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
LEG_W = (min(legend_w.values()) if HAS_LEG and legend_w else LEGEND_W) - 2
# 图面上不写说明：统计、自查结论、拆改计数只在报告、xlsx、IFC 里；分区、分段靠总图上的分区线和各段图号、分段图的"接 x-xx"索引
sheet_list = []   # (图号, 图名, 比例, 图幅, 备注)
for ident, v, den, zid in views:
    # 图例的点位行只列这张图上实际画出的种类（平面图上不画的吸顶监控等不列）；写过简称的（总图，1:150 上写不下全名的小房间）
    # 末尾一行是简称对照
    drawn = drawset.drawn_symbol_keys(v) | ({annotate.STRIP_KEY} if annotate.STRIPS.get(v.id()) else set()) if legend and den < 200 else set()
    keys = [k for k in legend if k in drawn] if drawn else []
    abbr = annotate.abbr_legend(v)
    if v.id() in annotate.CEIL_LEGEND:   # 顶面布置图：没吊顶的房间不逐间写，图例末尾一行"原顶刷白 2.800"
        abbr = " ".join(t for t in (abbr, annotate.CEIL_LEGEND[v.id()]) if t)
    if v.id() in annotate.LIGHT_NOTE:   # 灯具定位图：在吊顶边带正中的筒灯不逐排标离墙尺寸，图例末尾一行说明
        abbr = " ".join(t for t in (abbr, annotate.LIGHT_NOTE[v.id()]) if t)
    rows = annotate.LEGEND_ROWS.get(v.id())   # 地面材料编号对照、门槛石、压条、起铺点；顶面布置图的铝扣板吊顶
    if zid:
        # 分区/分段图：有设施点位时图例在右侧预留的一栏（墙体行并在里面）；没有点位的不留栏，墙体图例找图面上的空地放，
        # 视图位置和比例都不变，放不下就不画（JZLOG legend skipped）
        lw = legend_w[zid] if HAS_LEG else 0.0
        sheet, svg = drawset.make_sheet(v, ident, v.Name, PROJECT_SHORT, f"1:{den}", today, reserve_right_mm=lw)
        walls = annotate.wall_materials(f, v, bbox_m=crops[zid])
        if HAS_LEG:
            drawset.add_legend(svg, SYMBOLS, keys, den, x=AREA_R - lw + 3, y=24, width=LEG_W, walls=walls, extra=abbr or None,
                               leader=A.mep and drawset.has_shift_leaders(v), rows=rows)   # 水电图上有挪开画的符号才加引线一行
        else:
            drawset.place_legend(svg, SYMBOLS, [], den, near=(AREA_R + 1 - LEG_W, 24), width=LEG_W, walls=walls, extra=abbr or None,
                                 rows=rows)
    else:
        # 总图：图例在视图标题下面——画了点位时的点位行、墙体行，简称对照（小比例总图的 --labels 的 legend_text 接在后面作
        # 补充说明，这一行宽 180）
        sheet, svg = drawset.make_sheet(v, ident, v.Name, PROJECT_SHORT, f"1:{den}", today)
        abbr_text = " ".join(t for t in (abbr, LABELS.get("legend_text") if den >= 200 else "") if t)
        drawset.add_legend(svg, SYMBOLS, keys, den, x=36, y=drawset.view_title_bottom(sheet) + 9,
                           width=LEG_W, walls=annotate.wall_materials(f, v), extra=abbr_text or None, extra_width=180, rows=rows)
    drawset.export_pdf(svg, os.path.join(OUT, f"{ident}.pdf"), f"{ident} {v.Name}")
    drawset.export_png(svg, os.path.join(OUT, f"{ident}.png"))
    sheet_list.append((ident, v.Name, f"1:{den}", "A3", ""))

# 表格（门窗表；水电图是回路表、点位统计表）：从 IFC 统计，xlsx → Bonsai Schedules → 图纸；一页放不下就分几张图，每页表下都写脚注。
# 喂给 Bonsai 的表格（断好行的 xlsx、同名 CSS、Bonsai 画的 SVG）是中间文件，放 work/schedules/；交付的 xlsx 放输出目录顶层
sched_dir = os.path.join(OUT, drawset.WORK, "schedules")
TABLE_TOP, AREA_BOTTOM = 24.0, drawset.AREA[3]


def mep_tables():
    """回路表（每户每路：灯具数、开关数、插座数，所在房间、漏电保护；导线、断路器留给电工）和点位统计表（每种点位的数量；房间 × 种类的明细
    只进交付的 点位明细.xlsx）：都从 IFC 查
    （回路是 IfcDistributionCircuit，点位是带 Tag（plan_id）的符号库构件，房间是它所在的 IfcSpace）。"""
    import ifcopenshell.util.element as uel
    import ifcopenshell.util.system as usys
    from jiazhuang import mep
    room = lambda e: (lambda sp: (sp.LongName or sp.Name) if sp is not None and sp.is_a("IfcSpace") else "")(uel.get_container(e))
    unit = lambda e: (lambda sp: uel.get_pset(sp, "JZ_Space", "Unit") or "" if sp is not None and sp.is_a("IfcSpace") else "")(uel.get_container(e))
    order = list(mep.CIRCUITS)
    pre = lambda c: c.Name.rstrip("0123456789")
    # 点位分三类各占一列：灯具（一条灯带算一个）、开关（不占这一路的名额）、插座（含专用插座、空调插座）；这一路没有这类点位的列写"—"
    cls = lambda kind: "灯具" if kind == "light" else "开关" if kind in ("switch", "bed_switch") else "插座"
    has = {p: {cls(t["kind"]) for t in mep.POINTS.values() if t.get("circuit") == p} for p in order}
    crows = []
    for c in sorted(f.by_type("IfcDistributionCircuit"),
                    key=lambda c: (c.Description or "", order.index(pre(c)) if pre(c) in order else 99, int(c.Name[len(pre(c)):] or 0))):
        els = sorted(usys.get_system_elements(c), key=lambda e: e.Tag or "")
        rooms = list(dict.fromkeys(room(e) for e in els if room(e)))
        n = [sum(cls(mep.SYMBOL_KIND.get(getattr(uel.get_type(e), "Tag", None))) == k for e in els) for k in ("灯具", "开关", "插座")]
        n = [v if v or k in has.get(pre(c), ()) else "—" for v, k in zip(n, ("灯具", "开关", "插座"))]
        crows.append([c.Description or "", c.Name, c.LongName or "", *n, "、".join(rooms),
                      "30mA" if mep.CIRCUITS.get(pre(c), {}).get("rcd") else "", "", ""])
    chead = ["户", "回路", "用途", "灯具数", "开关数", "插座数", "所在房间", "漏电保护", "导线", "断路器"]
    pts = [e for e in f.by_type("IfcDistributionElement") if e.Tag and getattr(uel.get_type(e), "Tag", None) in PACK_KEYS]
    keys = [t for t in PACK_KEYS if any(uel.get_type(e).Tag == t for e in pts)]   # 符号库的顺序
    # 灯带按米计（JZ_LightStrip 的长度），别的按个数
    size = lambda e: (uel.get_pset(e, "JZ_LightStrip", "Length_mm") or 0.0) / 1000.0 if uel.get_type(e).Tag == annotate.STRIP_KEY else 1
    by_key = {t: sum(size(e) for e in pts if uel.get_type(e).Tag == t) for t in keys}
    qty = lambda t, v: f"{v:.1f}m" if t == annotate.STRIP_KEY else v
    prow = [[PACK_KEYS[t]["name"], PACK_KEYS[t]["legend"], f"H{PACK_KEYS[t]['default_height']:g}" if "default_height" in PACK_KEYS[t] else "—",
             qty(t, by_key[t])] for t in keys] + [["合计", "", "", len(pts)]]
    phead = ["点位", "图例名称", "默认高度", "数量"]
    # 交付另给一份明细（户、房间 × 点位种类）：种类多，按列排放不下 A3，只进 xlsx
    names = [PACK_KEYS[t]["name"] + ("（m）" if t == annotate.STRIP_KEY else "") for t in keys]
    count = {}
    for e in pts:
        c = count.setdefault((unit(e), room(e)), {})
        t = uel.get_type(e).Tag
        n = names[keys.index(t)]
        c[n] = round(c.get(n, 0) + size(e), 1)
    npts = {k: sum(1 for e in pts if (unit(e), room(e)) == k) for k in count}   # 合计一栏是点位个数（灯带一条算一个）
    drow = [[u, r] + [c.get(n, "") for n in names] + [npts[(u, r)]] for (u, r), c in sorted(count.items())]   # 按户、房间排
    drow.append(["合计", ""] + [round(sum(c.get(n, 0) for c in count.values()), 1) for n in names] + [len(pts)])
    schedules.write_delivery(os.path.join(OUT, "点位明细.xlsx"), "点位明细", ["户", "房间"] + names + ["合计"], drow, subtitle=PROJECT)
    note = ["导线、断路器规格由电工确定；漏电保护按 GB 55038-2025 第 7.4.3 条（待原文核对）。"]
    return [("回路表", "circuit_schedule", chead, crows, {6: 120.0}, {2: "left", 6: "left"}, note),
            ("点位统计表", "point_schedule", phead, prow, {1: 90.0}, {0: "left", 1: "left"}, [])]


def floor_table():
    """地面材料表（买料用）：从 IFC 的地面 IfcCovering（FLOORING，JZ_Flooring）统计，每种材料加规格一行：编号、材料、规格、所在房间
    （一格里列全）、面积、整块数、切块数、损耗率、合计（各区含损耗的合计相加：地砖按块、木地板按 ㎡）；门槛石按尺寸一行，列出在哪几个
    门口、块数。每个区的面积、整块、切块、边砖宽、提醒另出 地面分区明细.xlsx（只进交付，不出图，同 点位明细.xlsx）。
    表里只写房名：门外不在这一户（公共走道）的一侧写"入户"，不写内部房间编号。"""
    import ifcopenshell.util.element as uel
    names = {sp.Name: sp.LongName or sp.Name for sp in f.by_type("IfcSpace")}
    nm = lambda r: names.get(r, "入户")
    zs, st = [], {}
    for c in f.by_type("IfcCovering"):
        jz = uel.get_pset(c, "JZ_Flooring") if c.PredefinedType == "FLOORING" else None
        if jz and jz.get("Part") == "铺装区":
            zs.append(jz)
        elif jz:
            st.setdefault((jz["Code"], jz["Width_mm"], jz["Depth_mm"]), []).append(jz)
    zs.sort(key=lambda j: (j["Code"], j["Rooms"]))
    rows, by = [], {}
    for jz in zs:
        by.setdefault(jz["Code"], []).append(jz)
    for code, js in by.items():
        wood = js[0]["TotalUnit"] == "㎡"
        tot = sum(float(j["Total"]) for j in js)
        rooms = "、".join(dict.fromkeys(nm(r) for j in js for r in j["Rooms"].split(",")))
        rows.append([code, js[0]["Material"], js[0]["Size"], rooms, f"{sum(float(j['Area_m2']) for j in js):.2f}",
                     sum(int(j["Whole"]) for j in js), sum(int(j["Cut"]) for j in js), f"{js[0]['Waste'] * 100:g}%",
                     f"{tot:.2f}㎡" if wood else f"{int(round(tot))}块"])
    for (code, w, d), js in sorted(st.items()):
        where = "、".join("/".join(nm(r) for r in j["Rooms"].split(",")) for j in sorted(js, key=lambda j: j["Door"]))
        rows.append([code, js[0]["Material"], f"{w:g}×{d:g}", where, f"{len(js) * w * d / 1e6:.2f}", len(js), 0, "—", f"{len(js)}块"])
    head = ["编号", "材料", "规格", "所在房间", "面积（m²）", "整块数", "切块数", "损耗率", "合计"]
    edge = lambda e: "；".join(f"{'沿铺向' if k == 'u' else '垂直'} {'、'.join(f'{v:g}' for v in vs)}" for k, vs in json.loads(e or "{}").items())
    drow = [[j["Code"], j["Material"], j["Size"], "、".join(nm(r) for r in j["Rooms"].split(",")), f"{float(j['Area_m2']):.2f}", int(j["Whole"]),
             int(j["Cut"]), edge(j.get("Edges")), f"{float(j['Total']):.2f}㎡" if j["TotalUnit"] == "㎡" else f"{int(j['Total'])}块",
             j.get("Notes") or ""] for j in zs]
    schedules.write_delivery(os.path.join(OUT, "地面分区明细.xlsx"), "地面分区明细",
                             ["编号", "材料", "规格", "铺装区（房间）", "面积（m²）", "整块数", "切块数", "边砖宽（mm）", "合计", "提醒"], drow, subtitle=PROJECT)
    return [("地面材料表", "floor_schedule", head, rows, {3: 100.0}, {1: "left", 3: "left"}, [])]

TABLES = []   # 门窗表跟平面图走，地面材料表跟地面铺装图走，回路表、点位统计表跟水电图走（顶面图没有表）
if "plan" in A.kinds:
    head, rows, dw = openings.schedule_rows(f, annotate.plan_geometry(f))
    if not rows:
        drawset.log("WARNING no doors/windows in model, door/window schedule sheet skipped")
    TABLES += [("门窗表", "door_window_schedule", head, rows, {head.index("所在位置"): 110.0, head.index("备注"): 62.0},
                {head.index("所在位置"): "left", head.index("备注"): "left"}, openings.schedule_notes(dw))]
if "floor" in A.kinds:
    TABLES += floor_table()
if A.mep:
    TABLES += mep_tables()
n_sheet = len(views)
for title, stem, head, rows, widths, align, foot in TABLES:
    max_table_h = AREA_BOTTOM - TABLE_TOP - drawset.footnote_mm(len(foot))   # 改它会改变分页数和后面各张的图号
    if rows:
        schedules.write_delivery(os.path.join(OUT, f"{title}.xlsx"), title, head, rows, footnote=foot, subtitle=PROJECT)
    pages = schedules.paginate(head, rows, max_table_h, widths) if rows else []
    drawset.log(f"{stem.replace('_', ' ')} pages" if stem != "door_window_schedule" else "door/window schedule pages", len(pages),
                "table height budget", round(max_table_h, 1))
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
