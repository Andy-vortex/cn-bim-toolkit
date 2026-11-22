"""用约定包给示例户型出一套 A3 图（图纸目录、平面布置图、水电点位图、顶面布置图、门窗表），每张出 SVG、PDF 和 PNG 预览。

在装了 Bonsai 的 Blender 里运行（Bonsai 装在项目专用用户目录时，先设 BLENDER_USER_RESOURCES 指向它）：
  blender -b --python-exit-code 1 --python scripts/make_demo_set.py [-- 输出目录]
输出（缺省 build/demo/）：JZ-0x.pdf、JZ-0x.png、门窗表.xlsx、图纸目录.xlsx、model.ifc，中间文件在 work/。
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import conventions  # noqa: E402

p = argparse.ArgumentParser(prog="blender -b --python-exit-code 1 --python scripts/make_demo_set.py --", allow_abbrev=False,
                            description="示例户型出一套 A3 图：图纸目录、平面布置图、水电点位图、顶面布置图、门窗表（PDF、PNG、xlsx、model.ifc）。"
                                        "在装了 Bonsai 的 Blender 里运行。")
p.add_argument("out", nargs="?", default=os.path.join(ROOT, "build", "demo"), metavar="输出目录", help="缺省 build/demo")
A = p.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:] if sys.argv[0].endswith(".py") else [])
if err := conventions.check_pack():
    p.error(err)

import bpy  # noqa: E402
import bonsai.tool as tool  # noqa: E402
from jiazhuang import annotate, demo_apartment, drawset, openings, printing, schedules, symbols  # noqa: E402

SYMBOLS = conventions.pack_file("symbols.json")
OUT = os.path.abspath(A.out)
SCHED = os.path.join(OUT, drawset.WORK, "schedules")   # 喂给 Bonsai 的表格（中间文件）；交付的 xlsx 在 OUT 顶层
DEN = 50
EXTENTS = (0.0, 0.0, 6.8, 4.0)  # 外包范围（米）
PROJECT = "示例户型（合成数据）"

os.makedirs(OUT, exist_ok=True)
ifc_path = os.path.join(OUT, "model.ifc")
demo_apartment.build(ifc_path, SYMBOLS)
drawset.log("marks", len(openings.assign_marks_file(ifc_path)))   # 门窗编号（Tag）和门窗类型
drawset.clean_outputs(OUT)   # 清掉上次留在 work/ 里的视图和图纸文件
bpy.ops.bim.load_project(filepath=ifc_path, should_start_fresh_session=True)
drawset.configure_project()
f = tool.Ifc.get()
storey = f.by_type("IfcBuildingStorey")[0]

plan = drawset.add_view("平面布置图", "PLAN_VIEW", storey.id(), DEN, EXTENTS, exclude="IfcOutlet, IfcSwitchingDevice")
# 顺序（先放的文字登记占位，后放的避开）：门窗编号（贴着洞口）→ 楼面标高（要一块空地）→ 房间名（在本房间里找空地）
# → 三道尺寸（数字避让放在室外的编号）
drawset.log("tags", annotate.opening_tags(f, plan, DEN), "level", annotate.level_mark(f, plan, DEN, [0.0]),
            "labels", annotate.room_labels(f, plan, DEN))
drawset.log("dims", annotate.three_chains(f, plan, DEN))

mep = drawset.add_view("水电点位图", "PLAN_VIEW", storey.id(), DEN, EXTENTS, exclude="IfcFurniture")
drawset.log("dims", annotate.three_chains(f, mep, DEN, sides=("S", "W")),
            "heights", annotate.point_heights(f, mep, DEN, defaults=symbols.default_heights(SYMBOLS)),   # 只标和图例默认值不同的
            "labels", annotate.room_labels(f, mep, DEN, with_area=False, size=2.5))

rcp = drawset.add_view("顶面布置图", "REFLECTED_PLAN_VIEW", storey.id(), DEN, EXTENTS)
drawset.log("dims", annotate.three_chains(f, rcp, DEN, sides=("S", "W")), "ceilings", annotate.ceiling_notes(f, rcp, DEN))

for v in (plan, mep, rcp):
    drawset.render_view(v)

today = datetime.date.today().isoformat()
legends = {
    "JZ-02": ["cn_socket", "cn_socket_splash", "cn_switch_1", "cn_switch_2", "cn_floor_drain"],
    "JZ-03": ["cn_light_ceiling", "cn_downlight"],
}
sheet_list = []
for ident, view in (("JZ-01", plan), ("JZ-02", mep), ("JZ-03", rcp)):
    keys = legends.get(ident)
    sheet, svg = drawset.make_sheet(view, ident, view.Name, PROJECT, f"1:{DEN}", today,
                                    reserve_right_mm=70 if keys else 0)
    walls = annotate.wall_materials(f, view)
    if keys:
        drawset.add_legend(svg, SYMBOLS, keys, DEN, x=301, y=24, walls=walls)
    else:   # 没留图例栏的平面布置图：墙体图例找图面上的空地放（不挪视图），三张图的图例同宽
        drawset.place_legend(svg, SYMBOLS, [], DEN, near=(301, 24), walls=walls)
    drawset.export_pdf(svg, os.path.join(OUT, f"{ident}.pdf"), f"{ident} {view.Name}")
    drawset.export_png(svg, os.path.join(OUT, f"{ident}.png"))
    sheet_list.append((ident, view.Name, f"1:{DEN}", "A3", ""))

# 门窗表：从 IFC 统计，xlsx → Bonsai Schedules → 图纸；表下脚注和 make_plan_set 同一套（示例户型没有要注的）
head, rows, dw = openings.schedule_rows(f, annotate.plan_geometry(f))
xp = os.path.join(SCHED, "door_window_schedule.xlsx")
wrap_w = {head.index("所在位置"): 80.0, head.index("备注"): 72.0}
schedules.write_table(xp, head, rows, max_width_mm=wrap_w, align={c: "left" for c in wrap_w})
dw_foot = openings.schedule_notes(dw)
schedules.write_delivery(os.path.join(OUT, "门窗表.xlsx"), "门窗表", head, rows, footnote=dw_foot, subtitle=PROJECT)
sheet, svg = drawset.make_table_sheet([(xp, "门窗表")], "JZ-04", "门窗表", PROJECT, today, footnote=dw_foot)
drawset.export_pdf(svg, os.path.join(OUT, "JZ-04.pdf"), "JZ-04 门窗表")
drawset.export_png(svg, os.path.join(OUT, "JZ-04.png"))
sheet_list.append(("JZ-04", "门窗表", "—", "A3", ""))

# 图纸目录（排在最前）
entries = [("JZ-00", "图纸目录", "—", "A3", "")] + sheet_list
xp = os.path.join(SCHED, "drawing_index.xlsx")
idx_head, idx_rows = ["序号", "图号", "图名", "比例", "图幅", "备注"], [[i, *e] for i, e in enumerate(entries, 1)]
schedules.write_table(xp, idx_head, idx_rows, align={2: "left", 5: "left"}, min_width_mm=14.0)
schedules.write_delivery(os.path.join(OUT, "图纸目录.xlsx"), "图纸目录", idx_head, idx_rows, subtitle=PROJECT,
                         align={0: "center", 1: "center", 3: "center", 4: "center"})
sheet, svg = drawset.make_table_sheet([(xp, "图纸目录")], "JZ-00", "图纸目录", PROJECT, today)
drawset.export_pdf(svg, os.path.join(OUT, "JZ-00.pdf"), "JZ-00 图纸目录")
drawset.export_png(svg, os.path.join(OUT, "JZ-00.png"))

bpy.ops.bim.save_project(filepath=ifc_path)
printing.done(OUT)
