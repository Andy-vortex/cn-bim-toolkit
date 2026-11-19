"""方案（scheme）数据格式和最简单的方案：把识别结果原样当作方案（现状平面）。

方案是一个 JSON 字典，plan2ifc 和出图流程按下面的键读取（都可以缺省）：
  rooms:   [{id, usage, orig_name, unit, unit_type, zone, public, area_m2, label:[x,y], beds?, care_type?, note?}]
           id 对应识别结果里的房间；usage 是新用途（出图、检查都按它）；zone 是分区编号（出分图、按分区检查）
  zones:   [{id, ...}]         分区列表（旧方案里叫 cores）
  items:   [{key, name, x, y, z, rot, room}]   约定包符号（symbols.json 的键）的实例：床、插座、呼叫按钮……
  after:   拆改后的识别结果（walls/rooms/openings 都换成改后的几何）；没有拆改就不给
  demolish:[{polygon, reason, from, thickness}]  被拆掉的墙块
  changes: [{type, target, detail, zone}]        拆改清单
  assumptions: {键: 文字}      设计假设，写进 IFC 的 JZ_Scheme
  stats:   {键: 数字或文字}    出图说明里列出来
  levels:  [标高 m, …]        一个标准层代表多层时的全部标高
坐标都是图纸毫米。只依赖 shapely。
"""
from __future__ import annotations

from shapely.geometry import Polygon
from shapely.ops import polylabel


def from_recognition(rec: dict) -> dict:
    """识别结果原样当方案：用途 = 图上的房间名，不分区，不加设施。"""
    rooms = []
    for r in rec["rooms"]:
        P = Polygon(r["polygon"])
        lab = polylabel(P, tolerance=50) if P.is_valid and not P.is_empty else P.representative_point()
        rooms.append({"id": r["id"], "orig_name": r["name"], "usage": r["name"], "unit": r.get("unit", ""), "unit_type": r.get("unit_type", ""),
                      "zone": "", "public": bool(r.get("public")), "area_m2": r["area_m2"], "beds": 0, "note": "",
                      "label": [round(lab.x), round(lab.y)]})
    named = [x for x in rooms if x["usage"] != "未标注"]
    return {"source": rec.get("source", ""), "assumptions": {}, "levels": rec.get("levels") or [0.0], "zones": [],
            "rooms": rooms, "items": [], "stats": {"房间": len(named), "户": len(rec.get("units", []))}}
