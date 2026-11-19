"""用约定包给示例户型出一套 A3 图（平面布置图、水电点位图、顶面布置图），每张出 SVG 和 PDF。

在装了 Bonsai 的 Blender 里运行（Bonsai 装在项目专用用户目录时，先设 BLENDER_USER_RESOURCES 指向它）：
  blender -b --python scripts/make_demo_set.py
输出：build/demo/（demo.ifc、drawings/、sheets/、JZ-0x.pdf）
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bpy  # noqa: E402
import bonsai.tool as tool  # noqa: E402
from jiazhuang import annotate, demo_apartment, drawset  # noqa: E402

PACK = os.path.join(ROOT, "packs", "cn", "drawing")
SYMBOLS = os.path.join(PACK, "symbols.json")
OUT = os.path.join(ROOT, "build", "demo")
DEN = 50
EXTENTS = (0.0, 0.0, 6.8, 4.0)  # 外包范围（米）
PROJECT = "示例户型（合成数据）"

os.makedirs(OUT, exist_ok=True)
ifc_path = os.path.join(OUT, "demo.ifc")
demo_apartment.build(ifc_path, SYMBOLS)
bpy.ops.bim.load_project(filepath=ifc_path, should_start_fresh_session=True)
drawset.configure_project(PACK)
f = tool.Ifc.get()
storey = f.by_type("IfcBuildingStorey")[0]

plan = drawset.add_view("平面布置图", "PLAN_VIEW", storey.id(), DEN, EXTENTS, exclude="IfcOutlet, IfcSwitchingDevice")
drawset.log("dims", annotate.three_chains(f, plan, DEN), "labels", annotate.room_labels(f, plan, DEN))

mep = drawset.add_view("水电点位图", "PLAN_VIEW", storey.id(), DEN, EXTENTS, exclude="IfcFurniture")
drawset.log("dims", annotate.three_chains(f, mep, DEN, sides=("S", "W")),
            "labels", annotate.room_labels(f, mep, DEN, with_area=False, size=2.5),
            "heights", annotate.point_heights(f, mep, DEN))

rcp = drawset.add_view("顶面布置图", "REFLECTED_PLAN_VIEW", storey.id(), DEN, EXTENTS)
drawset.log("dims", annotate.three_chains(f, rcp, DEN, sides=("S", "W")), "ceilings", annotate.ceiling_notes(f, rcp, DEN))

for v in (plan, mep, rcp):
    drawset.render_view(v)

today = datetime.date.today().isoformat()
legends = {
    "JZ-02": ["cn_socket", "cn_socket_splash", "cn_switch_1", "cn_switch_2", "cn_floor_drain", "cn_toilet", "cn_basin"],
    "JZ-03": ["cn_light_ceiling", "cn_downlight"],
}
for ident, view in (("JZ-01", plan), ("JZ-02", mep), ("JZ-03", rcp)):
    keys = legends.get(ident)
    sheet, svg = drawset.make_sheet(view, ident, view.Name, PROJECT, f"1:{DEN}", today,
                                    reserve_right_mm=70 if keys else 0)
    if keys:
        drawset.add_legend(svg, SYMBOLS, keys, DEN, x=301, y=24)
    drawset.export_pdf(svg, os.path.join(OUT, f"{ident}.pdf"))

bpy.ops.bim.save_project(filepath=ifc_path)
drawset.log("done", OUT)
