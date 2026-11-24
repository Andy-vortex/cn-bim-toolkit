"""按户出图：一户的识别结果和方案（只依赖 shapely 和约定包）。

subset(rec, sch, 户号) 把识别结果、方案裁成只剩这一户：这一户的房间，围着它的墙（分户墙、外墙画全厚，别户和公共部分的墙、
房间、门窗、家具、点位都去掉），洞口开向这一户房间的门窗（入户门也在），穿过这一户的轴线。出图流程照常把它当成整层：
尺寸、轴号、外墙判断、门窗编号、表格都只算这一户，不用裁切框、折断线和接图注。DENS 是按户出图的比例档。
以后顶面、地面、立面按户出图也从这里取。"""
from __future__ import annotations

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from . import conventions

# 按户出图（--unit）的比例档：放得下的最大一档。1:60、1:80 是 GB/T 50001 的可用比例（待原文核对）
DENS = (50, 60, 80, 100, 150, 200, 300, 500, 1000)
NEAR = 30.0   # 墙离这一户的房间不超过这么远（mm）算围着它
_ENT = conventions.get("openings")["entrance"]


def unit_ids(rec: dict, sch: dict | None = None) -> list[str]:
    """识别结果（有拆改时是拆改后）里的户 id。"""
    return sorted({r["unit"] for r in ((sch or {}).get("after") or rec)["rooms"] if r.get("unit")})


def _poly(pts):
    p = Polygon(pts)
    return p if p.is_valid else p.buffer(0)


def _rings(g) -> list:
    parts = [p for p in getattr(g, "geoms", [g]) if isinstance(p, (Polygon, MultiPolygon))]
    parts = [q for p in parts for q in getattr(p, "geoms", [p])]
    return [[[round(x, 1), round(y, 1)] for x, y in p.exterior.coords] for p in parts if not p.is_empty and p.area > 1.0]


def _entrance(o: dict, ids: set, by: dict, usage: dict) -> bool:
    """入户门：一侧是这一户的房间、另一侧是不属于任何户的公共交通空间（openings.is_entrance_door 的第 ③ 条，两侧房名都不含
    排除词；房名用方案里的用途 usage，和整层建模时 IfcSpace 的名字一致）。按户出图时公共那一侧不在模型里，IFC 里认不出来，
    所以裁的时候先认好，记在门上（entrance）。"""
    has = lambda s, words: any(w in s for w in words)
    rs = [by[x] for x in o.get("rooms") or [] if x in by]
    name = lambda r: usage.get(r["id"]) or r.get("name") or ""
    if o.get("kind") != "door" or len(rs) != 2 or any(has(name(r), _ENT["exclude_words"]) for r in rs):
        return False
    public = lambda r: bool(r.get("public")) or has(name(r), _ENT["public_room_words"])
    mine = [r for r in rs if r["id"] in ids]
    other = [r for r in rs if r["id"] not in ids]
    return len(mine) == 1 and len(other) == 1 and not public(mine[0]) and not other[0].get("unit") and public(other[0])


def _part(rec: dict, ids: set, usage: dict | None = None) -> tuple[dict, set]:
    """识别结果里只留房间 ids 和围着它们的墙、开向它们的门窗、穿过它们的轴线；外轮廓按留下的房间和墙重算。
    返回 (裁过的识别结果, 留下的墙、门窗的原 id)。"""
    rooms = [r for r in rec["rooms"] if r["id"] in ids]
    U = unary_union([_poly(r["polygon"]) for r in rooms])
    walls, kept = [], set()
    for w in rec["walls"]:
        g = _poly(w["polygon"])
        if g.is_empty or g.distance(U) > NEAR:
            continue
        # 一段长墙伸过这一户时只取这一户旁边的部分（墙厚以内），分户墙、外墙仍是全厚
        g = g.intersection(U.buffer(float(w.get("thickness") or 0) + NEAR, join_style="mitre"))
        rings = _rings(g)
        for i, ring in enumerate(rings):
            walls.append(dict(w, id=w["id"] if len(rings) == 1 else f"{w['id']}-{i + 1}", polygon=ring))
        kept |= {w["id"]} if rings else set()
    by = {r["id"]: r for r in rec["rooms"]}
    openings = [dict(o, entrance=True) if _entrance(o, ids, by, usage or {}) else o for o in rec["openings"] if ids & set(o.get("rooms") or [])]
    kept |= {o["id"] for o in openings}
    x0, y0, x1, y1 = unary_union([U] + [_poly(w["polygon"]) for w in walls]).bounds
    axes = rec.get("axes") or {}
    keep_ax = {"x": [dict(a, ends=["bottom", "top"]) for a in axes.get("x", [])
                     if x0 - NEAR <= a["pos"] <= x1 + NEAR and a["span"][0] < y1 and a["span"][1] > y0],
               "y": [dict(a, ends=["left", "right"]) for a in axes.get("y", [])
                     if y0 - NEAR <= a["pos"] <= y1 + NEAR and a["span"][0] < x1 and a["span"][1] > x0]} if axes else axes
    fp = unary_union([U] + [_poly(w["polygon"]) for w in walls] + [_poly(o["polygon"]) for o in openings if o.get("polygon")])
    fp = fp.buffer(300, join_style="mitre").buffer(-300, join_style="mitre")
    units = [u for u in rec.get("units", []) if set(u.get("rooms", [])) & ids]
    return dict(rec, rooms=rooms, walls=walls, openings=openings, axes=keep_ax, units=units, unit_conflicts=[],
                footprint=_rings(fp), footprint_area_m2=round(fp.area / 1e6, 2)), kept


def outside_rooms(rec: dict, sch: dict, unit: str) -> list[str]:
    """这一户的门窗通向、但不属于这一户的房间（"房名 id"）：走道、前室这类公共部分是正常的；户内房间漏归户时也会出现在这里，
    它不会画在任何一户的图上。"""
    geo = sch.get("after") or rec
    ids = {r["id"] for r in geo["rooms"] if r.get("unit") == unit}
    by = {r["id"]: r for r in geo["rooms"]}
    out = {x for o in geo["openings"] if ids & set(o.get("rooms") or []) for x in o["rooms"] if x not in ids and x in by}
    return sorted(f"{by[x].get('name') or ''} {x}" for x in out)


def subset(rec: dict, sch: dict, unit: str) -> tuple[dict, dict]:
    """(识别结果, 方案) → 只剩这一户的 (识别结果, 方案)。方案里：这一户的房间和家具、点位；拆除的墙块、拆改清单只留这一户的；
    不分区（zones 清空）。户号不在识别结果里抛 ValueError。"""
    if unit not in unit_ids(rec, sch):
        raise ValueError(unit)
    geo = sch.get("after") or rec
    ids = {r["id"] for r in geo["rooms"] if r.get("unit") == unit}
    usage = {r["id"]: r.get("usage") for r in sch.get("rooms", []) if r.get("usage")}
    new_geo, kept = _part(geo, ids, usage)
    out = dict(sch, zones=[], rooms=[r for r in sch["rooms"] if r["id"] in ids], items=[it for it in sch.get("items", []) if it.get("room") in ids])
    if "cores" in out:
        out["cores"] = []
    if "ceilings" in sch:   # 吊顶块只留这一户的
        out["ceilings"] = [c for c in sch["ceilings"] if c.get("room") in ids]
    if "flooring" in sch:   # 铺装区只留这一户的；门槛石、压条只留门还在的
        fl, doors = sch["flooring"], {o["id"] for o in new_geo["openings"]}
        out["flooring"] = dict(fl, zones=[z for z in fl.get("zones", []) if set(z["rooms"]) <= ids],
                               thresholds=[t for t in fl.get("thresholds", []) if t["door"] in doors and set(t["rooms"]) & ids],
                               strips=[s for s in fl.get("strips", []) if s["door"] in doors and set(s["rooms"]) & ids])
    kept |= ids
    if sch.get("after"):
        out["after"] = new_geo
        rec, kept0 = _part(rec, {r["id"] for r in rec["rooms"] if r["id"] in ids} or ids, usage)
        kept |= kept0
    else:
        rec = new_geo
    if sch.get("demolish"):
        U = unary_union([_poly(r["polygon"]) for r in new_geo["rooms"]]).buffer(500)
        out["demolish"] = [d for d in sch["demolish"] if _poly(d["polygon"]).intersects(U)]
    if sch.get("changes"):
        out["changes"] = [c for c in sch["changes"] if c.get("target") in kept]
    return rec, out
