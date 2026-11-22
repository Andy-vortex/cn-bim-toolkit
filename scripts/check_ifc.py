"""按规则包检查 IFC，出 JSON、HTML、PDF 报告（在 Blender 里运行，借用 Bonsai 自带的 ifcopenshell、shapely）。
用法：blender -b --python-exit-code 1 --python scripts/check_ifc.py -- <ifc> <规则.json> <输出目录> [工程名]"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402

p = argparse.ArgumentParser(prog="blender -b --python-exit-code 1 --python scripts/check_ifc.py --", allow_abbrev=False,
                            description="按规则包检查 IFC，出 <规则包名>-report.json、.html、.pdf。")
p.add_argument("ifc", type=printing.existing_file)
p.add_argument("rules", type=printing.existing_file, metavar="规则.json")
p.add_argument("out", metavar="输出目录")
p.add_argument("project", nargs="?", metavar="工程名", help="写在报告抬头；不给就不写")
A = p.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:] if sys.argv[0].endswith(".py") else [])

from jiazhuang import checker  # noqa: E402

os.makedirs(A.out, exist_ok=True)
res = checker.run(A.ifc, A.rules)
stem = os.path.splitext(os.path.basename(A.rules))[0] + "-report"
with open(os.path.join(A.out, stem + ".json"), "w", encoding="utf-8") as fh:
    json.dump(res, fh, ensure_ascii=False, indent=1)
h = checker.to_html(res, os.path.join(A.out, stem + ".html"), A.project)
printing.html_to_pdf(h, os.path.join(A.out, stem + ".pdf"))
for r in res["results"]:
    printing.log(r["clause"], r["status"], sum(1 for i in r["items"] if i["status"] == "通过"), "/", len(r["items"]),
                 [(i["status"], i["target"], i["detail"]) for i in r["items"] if i["status"] not in ("通过", "不涉及", "人工复核")][:3])
printing.done(A.out)
