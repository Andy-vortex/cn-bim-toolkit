"""在 Blender 里运行（借用 Bonsai 自带的 ifcopenshell）：生成示例户型 IFC。
blender -b --python scripts/build_demo_ifc.py -- <out.ifc>"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import demo_apartment
out = sys.argv[sys.argv.index("--") + 1]
os.makedirs(os.path.dirname(out), exist_ok=True)
demo_apartment.build(out, os.path.join(ROOT, "packs", "cn", "drawing", "symbols.json"))
print("JZLOG built", out)
