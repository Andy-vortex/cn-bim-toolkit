"""在 Blender 里运行（借用 Bonsai 自带的 ifcopenshell、shapely）：按规则文件检查 IFC，出 JSON + HTML + PDF 报告。
blender -b --python scripts/check_ifc.py -- <ifc> <规则.json> <输出目录> [项目名]"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import checker
args = sys.argv[sys.argv.index("--") + 1:]
ifc, rules, out = args[:3]
project = args[3] if len(args) > 3 else os.path.basename(ifc)
os.makedirs(out, exist_ok=True)
res = checker.run(ifc, rules)
stem = os.path.splitext(os.path.basename(rules))[0] + "-report"
with open(os.path.join(out, stem + ".json"), "w", encoding="utf-8") as fh:
    json.dump(res, fh, ensure_ascii=False, indent=1)
h = checker.to_html(res, os.path.join(out, stem + ".html"), project)
print("JZLOG pdf", checker.to_pdf(h, os.path.join(out, stem + ".pdf")))
for r in res["results"]:
    print("JZLOG", r["clause"], r["status"], sum(1 for i in r["items"] if i["status"] == "通过"), "/", len(r["items"]),
          [(i["status"], i["target"], i["detail"]) for i in r["items"] if i["status"] not in ("通过", "不涉及", "人工复核")][:3])
