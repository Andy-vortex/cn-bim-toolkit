"""通用出图流程：识别结果（+ 方案）→ IFC →（规则包检查报告、拆改清单）→ A3 中文平面图。

在装了 Bonsai 的 Blender 里运行（Bonsai 装在项目专用用户目录时，先设 BLENDER_USER_RESOURCES 指向它）：
  blender -b --python scripts/make_plan_set.py -- <识别.json> <输出目录> [选项]

选项：
  --scheme 方案.json        方案（房间用途、分区、床和设施点位、拆改）；不给就把识别结果原样建模（现状平面）
  --rules a.json,b.json     规则包，每个出一份检查报告
  --project 工程名  --short 图框短名（约 10 字）  --building 楼栋名
  --labels 简称.json        房间简称表 {"abbr": {...}, "short": {...}, "legend_text": "卧=卧室 …"}
  --title 总图名  --zone-title "{id} 分区平面"  --prefix 图号前缀（默认 A）
识别结果来自 python -m jiazhuang.dxfplan；方案可以由自己的排布程序生成，格式见 src/jiazhuang/scheme.py。
"""
import collections
import datetime
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bpy  # noqa: E402
import bonsai.tool as tool  # noqa: E402
from jiazhuang import annotate, checker, drawset, plan2ifc, scheme  # noqa: E402

PACK = os.path.join(ROOT, "packs", "cn", "drawing")
SYMBOLS = os.path.join(PACK, "symbols.json")
ARGS = sys.argv[sys.argv.index("--") + 1:]


def _opt(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS and ARGS.index(name) + 1 < len(ARGS) else default


PROJECT = _opt("--project", "平面图（示例）")
PROJECT_SHORT = _opt("--short", "示例工程")
BUILDING = _opt("--building", "楼栋")
PREFIX = _opt("--prefix", "A")
TITLE = _opt("--title", "平面总图")
ZONE_TITLE = _opt("--zone-title", "{id} 分区平面")
RULES = [p for p in (_opt("--rules", "") or "").split(",") if p]
LABELS = json.load(open(_opt("--labels"), encoding="utf-8")) if _opt("--labels") else {}

rec_p, OUT = ARGS[0], os.path.abspath(ARGS[1])
os.makedirs(OUT, exist_ok=True)
rec = json.load(open(rec_p, encoding="utf-8"))
sch = json.load(open(_opt("--scheme"), encoding="utf-8")) if _opt("--scheme") else scheme.from_recognition(rec)
rec = sch.get("after") or rec   # 有拆改时，出图和外包都用拆改后的几何
ifc_path = os.path.join(OUT, "model.ifc")
info = plan2ifc.build(rec, sch, SYMBOLS, ifc_path, PROJECT, BUILDING)
drawset.log("ifc", info)
zones = sch.get("zones") or sch.get("cores") or []
zone_of = lambda r: r.get("zone") or r.get("core") or ""
st = sch.get("stats") or {}
notes = [("方案：" + "，".join(f"{k} {v}" for k, v in st.items() if not isinstance(v, dict))) if st else "方案：识别结果原样建模（现状）"]
if sch.get("assumptions"):
    notes.append("假设：" + "；".join(f"{k}={v}" for k, v in sch["assumptions"].items()))

# 规范检查（读刚生成的 IFC）：每个规则包出一份报告
summaries = []
for i, fn in enumerate(RULES):
    tag = f"{PREFIX}-00{'abcdefgh'[i]}"
    res = checker.run(ifc_path, fn)
    stem = os.path.splitext(os.path.basename(fn))[0]
    with open(os.path.join(OUT, f"{stem}-report.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    h = checker.to_html(res, os.path.join(OUT, f"{stem}-report.html"), PROJECT, notes)
    drawset.log("report pdf", tag, checker.to_pdf(h, os.path.join(OUT, f"{tag}_{stem}-report.pdf")))
    cnt = {s: sum(1 for r in res["results"] if r["status"] == s) for s in checker.STATUSES}
    summaries.append(f"{res['facts']['standard']} 自动检查：" + "，".join(f"{s} {cnt[s]} 条" for s in checker.STATUSES if cnt[s]) + f"（详见 {tag}）")

# 拆改清单
changes = sch.get("changes") or []
if changes:
    tag = f"{PREFIX}-00{'abcdefgh'[len(RULES)]}"
    rows = "".join(f"<tr><td>{i}</td><td>{html.escape(c.get('core', '') or c.get('zone', ''))}</td><td>{html.escape(c['type'])}</td>"
                   f"<td>{html.escape(c['target'])}</td><td>{html.escape(c['detail'])}</td></tr>" for i, c in enumerate(changes, 1))
    cnt_c = collections.Counter(c["type"] for c in changes)
    basis = html.escape(str((sch.get("assumptions") or {}).get("walls", "")))
    page = (f'<!doctype html><html><head><meta charset="utf-8"><style>@page{{size:A4;margin:14mm}}body{{font-family:"Microsoft YaHei";font-size:10pt}}'
            f'table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:3px 5px;vertical-align:top}}th{{background:#eee}}'
            f'.warn{{background:#fff8e1;border:1px solid #f0c36d;padding:6px 10px}}</style></head><body>'
            f'<h1 style="font-size:16pt">拆改清单</h1><div>{html.escape(PROJECT)} ｜ {datetime.date.today().isoformat()}</div>'
            f'<p class="warn">程序按"厚度 &lt;180mm 的墙是非承重隔墙"判断，厚墙和外墙一律不动；新砌卫生间要新接给排水（降板或同层排水），'
            f'<b>都要结构、给排水专业复核</b>。{basis}</p>'
            f'<p>' + "，".join(f"{k} {v} 处" for k, v in cnt_c.items()) + '</p>'
            f'<table><tr><th>#</th><th>分区</th><th>类型</th><th>对象</th><th>内容</th></tr>{rows}</table></body></html>')
    hp = os.path.join(OUT, "renovation-list.html")
    with open(hp, "w", encoding="utf-8") as fh:
        fh.write(page)
    drawset.log("changes pdf", tag, checker.to_pdf(hp, os.path.join(OUT, f"{tag}_renovation-list.pdf")))

# 出图
bpy.ops.bim.load_project(filepath=ifc_path, should_start_fresh_session=True)
drawset.configure_project(PACK)
f = tool.Ifc.get()
storey = f.by_type("IfcBuildingStorey")[0]
Z = info["z0"] / 1000.0 + 1.0
OX, OY = info["offset"]
polys = {r["id"]: r["polygon"] for r in rec["rooms"]}
wall_pts = [p for w in rec["walls"] for p in w["polygon"]]
has_grid = bool(f.by_type("IfcGrid"))
legend = sorted({it["key"] for it in sch.get("items", [])})


def edge_of(pts):
    """建筑外包（米，本地坐标）。"""
    return ((min(p[0] for p in pts) - OX) / 1000, (min(p[1] for p in pts) - OY) / 1000,
            (max(p[0] for p in pts) - OX) / 1000, (max(p[1] for p in pts) - OY) / 1000)


def fit_scale(e, width_mm, height_mm=250.0, dens=(50, 100, 150, 200, 300, 500, 1000)):
    """A3 图框内放得下的最大比例（含轴号、尺寸约 40mm 的留边）。"""
    w, h = (e[2] - e[0]) * 1000, (e[3] - e[1]) * 1000
    return next((d for d in dens if w / d + 40 <= width_mm and h / d + 40 <= height_mm), dens[-1])


pad = lambda den, mm: mm * den / 1000.0
EXCL = "IfcGrid"   # 轴网由我们自己画（轴号按原图错开），不让 Bonsai 再画一遍
blocks, spans = [], []
for k in zones:
    pts = [p for r in sch["rooms"] if zone_of(r) == k["id"] and r["id"] in polys for p in polys[r["id"]]]
    if not pts:
        continue
    e = edge_of(pts)
    near = [p for p in wall_pts if e[0] * 1000 + OX - 400 <= p[0] <= e[2] * 1000 + OX + 400 and e[1] * 1000 + OY - 400 <= p[1] <= e[3] * 1000 + OY + 400]
    e = edge_of(pts + near)
    blocks.append((k, e))
    spans.append((e[0], e[2]))

views = []
E = edge_of([p for g in rec["footprint"] for p in g])
den = fit_scale(E, 336.0)
whole = drawset.add_view(TITLE, "PLAN_VIEW", storey.id(), den,
                         (E[0] - pad(den, 23), E[1] - pad(den, 23), E[2] + pad(den, 3), E[3] + pad(den, 3)), margin_paper_mm=2,
                         exclude=("IfcAlarm, IfcOutlet, IfcRailing, IfcAudioVisualAppliance, " if den >= 200 else "") + EXCL
                         + ' + IfcWall, Pset_WallCommon.Status="DEMOLISH"')
drawset.log("labels", annotate.space_labels(f, whole, den, mode="abbr" if den >= 200 else "full", z=Z,
                                            abbr=LABELS.get("abbr"), short=LABELS.get("short")))
if has_grid:
    drawset.log("grid", annotate.grid_annotations(f, whole, den, E, z=Z, mode="overall", spans=spans or [(E[0], E[2])]))
views.append((f"{PREFIX}-01", whole, den, None))

for k, e in blocks:
    zden = fit_scale(e, 336.0 - (70 if legend else 0))
    v = drawset.add_view(ZONE_TITLE.format(id=k["id"]), "PLAN_VIEW", storey.id(), zden,
                         (e[0] - pad(zden, 23), e[1] - pad(zden, 23), e[2] + pad(zden, 17), e[3] + pad(zden, 17)), margin_paper_mm=2,
                         exclude=EXCL, include=f'IfcElement, JZ_Zone.Block="{k["id"]}"')
    drawset.log("labels", k["id"], annotate.space_labels(f, v, zden, mode="full", bbox_m=e, z=Z, abbr=LABELS.get("abbr"), short=LABELS.get("short")))
    if has_grid:
        drawset.log("grid", k["id"], annotate.grid_annotations(f, v, zden, e, z=Z, mode="block"))
    views.append((f"{PREFIX}-{len(views) + 1:02d}", v, zden, k["id"]))

for _, v, _, _ in views:
    drawset.render_view(v)

today = datetime.date.today().isoformat()
wall_note = "黑=疑似承重/外墙，灰=原有隔墙" + ("；斜线=新砌隔墙，红色虚线=拆除" if changes else "") + "；承重判断待结构专业复核"
for ident, v, den, zid in views:
    keys = legend if zid else None
    sheet, svg = drawset.make_sheet(v, ident, v.Name, PROJECT_SHORT, f"1:{den}", today, reserve_right_mm=70 if keys else 0)
    if keys:
        drawset.add_legend(svg, SYMBOLS, keys, den, x=301, y=24)
        beds = sum(r.get("beds", 0) for r in sch["rooms"] if zone_of(r) == zid)
        ch = collections.Counter(c["type"] for c in changes if (c.get("core") or c.get("zone")) == zid)
        its = [f"{a} {b}" for a, b in ch.items()]
        ch_lines = [("拆改：" if i == 0 else "　　　") + "，".join(its[i:i + 2]) for i in range(0, len(its), 2)]
        drawset.add_notes(svg, ([f"本分区床位 {beds} 张"] if beds else []) + ["黑=疑似承重/外墙，灰=原有隔墙"]
                          + (["斜线=新砌隔墙，红色虚线=拆除"] if changes else []) + ch_lines + ["结构、给排水待专业复核"],
                          301, 150, size=3.0, title="说明")
    else:
        lines = ([LABELS["legend_text"]] if LABELS.get("legend_text") else []) + notes[:1] + [wall_note]
        if changes:
            lines.append("拆改：" + "，".join(f"{a} {b}" for a, b in collections.Counter(c["type"] for c in changes).items()))
        drawset.add_notes(svg, lines + summaries, 36, 215, size=3.2, title="说明")
    drawset.export_pdf(svg, os.path.join(OUT, f"{ident}.pdf"))

bpy.ops.bim.save_project(filepath=ifc_path)
drawset.log("done", OUT)
