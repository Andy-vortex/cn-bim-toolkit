"""渲染机位规划、全景热点角度计算与 Pannellum 漫游页打包（纯 Python，不依赖 bpy）。

全景相机按 Blender (90°, 0°, 0°) 朝向约定：
  - 相机正前（-Z，Pannellum yaw=0°）指向 +Y（北）；
  - 相机右侧（+X，Pannellum yaw=+90°）指向 +X（东）；
  - 相机上方（+Y，Pannellum pitch=+90°）指向 +Z（上）。
因此从相机 (cx, cy, cz) 看向目标 (tx, ty, tz) 的 Pannellum 角度为：
  yaw = degrees(atan2(tx - cx, ty - cy))
  pitch = degrees(atan2(tz - cz, hypot(tx - cx, ty - cy)))
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel, unary_union

DEFAULT_CDN = "https://cdn.jsdelivr.net/npm/pannellum@2.5.7/build"
RENDER_PACK = Path(__file__).resolve().parents[2] / "packs" / "cn" / "render"


def load_pack(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else (RENDER_PACK / "materials.json")
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(text: str) -> str:
    """将房间 id / 房名转为跨平台安全文件名片段（保留中文、字母、数字、下划线和连字符）。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text).strip())
    s = s.strip("._") or "room"
    if s.upper() in _WIN_RESERVED:
        s = f"room_{s}"
    return s


def _json_for_html(obj: Any) -> str:
    """将对象序列化为可安全嵌入 HTML <script> 的 JSON（转义 < > &，阻断 </script> 与 <!-- 逃逸）。"""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def yaw_pitch(cam: tuple[float, float, float], target: tuple[float, float, float]) -> tuple[float, float]:
    """计算从 cam 看向 target 的 Pannellum (yaw, pitch) 角度（单位：度，保留 1 位小数）。"""
    dx = float(target[0]) - float(cam[0])
    dy = float(target[1]) - float(cam[1])
    dz = float(target[2]) - float(cam[2])
    horiz = math.hypot(dx, dy)
    yaw = math.degrees(math.atan2(dx, dy)) if horiz > 1e-6 else 0.0
    pitch = math.degrees(math.atan2(dz, horiz)) if (horiz > 1e-6 or abs(dz) > 1e-6) else 0.0
    return round(yaw, 1), round(pitch, 1)


def _make_poly(coords: list[list[float]] | list[tuple[float, float]]) -> Polygon | None:
    if not coords or len(coords) < 3:
        return None
    p = Polygon(coords)
    if not p.is_valid:
        p = p.buffer(0)
    if p.is_empty:
        return None
    if p.geom_type == "MultiPolygon":
        p = max(p.geoms, key=lambda g: g.area)
    return p if p.geom_type == "Polygon" and p.area > 1e-4 else None


def _largest_poly(geom) -> Polygon | None:
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom if geom.area > 1e-4 else None
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and g.area > 1e-4]
    return max(polys, key=lambda g: g.area) if polys else None


def _pick_station_xy(room_poly: Polygon, furn_polys: list[Polygon], label_xy: tuple[float, float] | None = None) -> tuple[float, float]:
    """在房间内找最开阔且避开家具的站立点（米）：依次尝试扣家具后留边 0.35 / 0.18 / 0.05m，退回整房 polylabel。"""
    if furn_polys:
        f_union = unary_union(furn_polys)
        for margin in (0.35, 0.18, 0.05):
            free = _largest_poly(room_poly.difference(f_union.buffer(margin)))
            if free is not None and free.area >= 0.25:
                pt = polylabel(free, tolerance=0.05)
                return round(pt.x, 3), round(pt.y, 3)
    pt = polylabel(room_poly, tolerance=0.05)
    return round(pt.x, 3), round(pt.y, 3)


def _pick_corner_view(
    room_poly: Polygon,
    furn_polys: list[Polygon],
    z0: float,
    eye_z: float,
    inset_m: float = 0.45,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """为房间自动选视野最通透的墙角透视机位，返回 (cam_xyz, look_at_xyz)。"""
    center_pt = polylabel(room_poly, tolerance=0.05)
    f_union = unary_union(furn_polys) if furn_polys else None
    target_xy = (
        (f_union.centroid.x * 0.4 + center_pt.x * 0.6, f_union.centroid.y * 0.4 + center_pt.y * 0.6)
        if f_union is not None and not f_union.is_empty and room_poly.contains(f_union.centroid)
        else (center_pt.x, center_pt.y)
    )
    target = (round(target_xy[0], 3), round(target_xy[1], 3), round(z0 + min(1.05, eye_z * 0.72), 3))

    # 候选点：收缩后的内多边形顶点 + 原多边形顶点向内偏移 inset_m
    cands: list[tuple[float, float]] = []
    for d in (inset_m, inset_m * 0.65, 0.22):
        shrunk = _largest_poly(room_poly.buffer(-d))
        if shrunk is not None:
            for x, y in list(shrunk.exterior.coords)[:-1]:
                cands.append((x, y))
            break
    if not cands:
        x0, y0, x1, y1 = room_poly.bounds
        for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            vx, vy = center_pt.x - cx, center_pt.y - cy
            L = math.hypot(vx, vy) or 1.0
            p = Point(cx + vx / L * min(inset_m, L * 0.4), cy + vy / L * min(inset_m, L * 0.4))
            if room_poly.contains(p):
                cands.append((p.x, p.y))
    if not cands:
        cands.append((center_pt.x, center_pt.y))

    best_score = -1e9
    best_xy = cands[0]
    for cx, cy in cands:
        pt = Point(cx, cy)
        if not room_poly.contains(pt):
            continue
        ray = LineString([(cx, cy), (target[0], target[1])])
        dist = ray.length
        # 视线穿出房间扣重分；机位压在家具内扣分
        inside_ratio = room_poly.intersection(ray).length / dist if dist > 1e-4 else 1.0
        in_furn = 1.0 if (f_union is not None and f_union.buffer(0.10).contains(pt)) else 0.0
        wall_clear = min(room_poly.exterior.distance(pt), 0.6)
        score = dist * (1.0 if inside_ratio > 0.98 else 0.2) + wall_clear * 0.5 - in_furn * 3.0
        if score > best_score:
            best_score = score
            best_xy = (cx, cy)
    cam = (round(best_xy[0], 3), round(best_xy[1], 3), round(z0 + eye_z, 3))
    return cam, target


def plan_cameras(
    spaces: list[dict[str, Any]],
    doors: list[dict[str, Any]],
    furniture_by_room: dict[str, list[list[list[float]]]] | None = None,
    *,
    eye_z: float = 1.45,
    inset_m: float = 0.45,
) -> dict[str, Any]:
    """根据房间多边形、门洞和家具轮廓规划全景站点、角落透视机位与房间跳转热点。

    spaces: [{id, name, polygon_m: [[x,y],...], z0, z1, area_m2?, label_xy?}]
    doors:  [{id, name, rooms: [rid1, rid2], center_xyz: [x,y,z]}]
    """
    furn_map = furniture_by_room or {}
    room_polys: dict[str, Polygon] = {}
    room_meta: dict[str, dict[str, Any]] = {}
    stations: list[dict[str, Any]] = []

    for sp in spaces:
        poly = _make_poly(sp.get("polygon_m", []))
        if poly is None:
            continue
        rid = str(sp["id"])
        name = str(sp.get("name") or rid)
        z0 = float(sp.get("z0", 0.0))
        z1 = float(sp.get("z1", z0 + 2.8))
        eff_eye = min(eye_z, max(0.8, (z1 - z0) * 0.55))
        fpolys = [p for coords in furn_map.get(rid, []) if (p := _make_poly(coords)) is not None]
        label_xy = tuple(sp["label_xy"]) if sp.get("label_xy") else None
        sx, sy = _pick_station_xy(poly, fpolys, label_xy)
        pano_cam = [sx, sy, round(z0 + eff_eye, 3)]
        corner_cam, corner_target = _pick_corner_view(poly, fpolys, z0, eff_eye, inset_m=inset_m)
        room_polys[rid] = poly
        st = {
            "id": rid,
            "name": name,
            "slug": safe_filename(f"{rid}_{name}"),
            "area_m2": round(float(sp.get("area_m2") or poly.area), 2),
            "z0": round(z0, 3),
            "z1": round(z1, 3),
            "pano_cam": pano_cam,
            "corner_cam": list(corner_cam),
            "corner_target": list(corner_target),
            "hotspots": [],
        }
        room_meta[rid] = st
        stations.append(st)

    # 1. 门洞连通热点（每个门连接两间都有站点的房间）
    linked_pairs: set[tuple[str, str]] = set()
    for d in doors:
        rids = [str(r) for r in d.get("rooms", []) if str(r) in room_meta]
        if len(rids) < 2:
            continue
        r1, r2 = rids[0], rids[1]
        if r1 == r2:
            continue
        c = d["center_xyz"]
        # 热点高度落在门中下部（按本层地面标高 z0 偏移 +1.05m，兼容任意非零楼层）
        floor_z = min(room_meta[r1]["z0"], room_meta[r2]["z0"])
        hz = float(c[2]) if len(c) >= 3 else (floor_z + 1.0)
        target = (float(c[0]), float(c[1]), min(hz, floor_z + 1.05))
        for src_id, dst_id in ((r1, r2), (r2, r1)):
            if (src_id, dst_id) in linked_pairs:
                continue
            src = room_meta[src_id]
            dst = room_meta[dst_id]
            yaw, pitch = yaw_pitch(tuple(src["pano_cam"]), target)
            src["hotspots"].append({
                "sceneId": dst_id,
                "text": f"去{dst['name']}",
                "yaw": yaw,
                "pitch": pitch,
                "door": str(d.get("id", "")),
            })
            linked_pairs.add((src_id, dst_id))

    # 2. 开敞相通房间（无门但共享边 ≥ 0.8m，如客餐厅分间或开敞门厅）
    rids_all = list(room_polys.keys())
    for i, r1 in enumerate(rids_all):
        p1 = room_polys[r1]
        for r2 in rids_all[i + 1:]:
            if (r1, r2) in linked_pairs:
                continue
            shared = p1.boundary.intersection(room_polys[r2].boundary)
            if shared.length >= 0.8:
                mid = shared.interpolate(0.5, normalized=True) if shared.geom_type == "LineString" else shared.centroid
                target = (mid.x, mid.y, min(room_meta[r1]["z0"], room_meta[r2]["z0"]) + 1.0)
                for src_id, dst_id in ((r1, r2), (r2, r1)):
                    src, dst = room_meta[src_id], room_meta[dst_id]
                    yaw, pitch = yaw_pitch(tuple(src["pano_cam"]), target)
                    src["hotspots"].append({
                        "sceneId": dst_id,
                        "text": f"去{dst['name']}",
                        "yaw": yaw,
                        "pitch": pitch,
                        "open": True,
                    })
                    linked_pairs.add((src_id, dst_id))

    # 默认首场景：优先客厅/客餐厅，其次面积最大房间
    first_id = ""
    for st in stations:
        if any(k in st["name"] for k in ("客厅", "客餐", "起居")):
            first_id = st["id"]
            break
    if not first_id and stations:
        first_id = max(stations, key=lambda s: s["area_m2"])["id"]

    return {
        "first_scene": first_id,
        "stations": stations,
        "hotspot_count": sum(len(s["hotspots"]) for s in stations),
    }


def _find_pannellum(pannellum_dir: str | Path | None) -> Path | None:
    cand = pannellum_dir or os.environ.get("JZ_PANNELLUM")
    if not cand:
        return None
    p = Path(cand)
    if (p / "pannellum.js").is_file() and (p / "pannellum.css").is_file():
        return p
    return None


def build_site(
    tour_dir: str | Path,
    plan: dict[str, Any],
    *,
    title: str = "室内全景漫游",
    img_ext: str = "jpg",
    hfov: int = 100,
    pannellum_dir: str | Path | None = None,
    cdn_base: str = DEFAULT_CDN,
) -> dict[str, Any]:
    """生成 Pannellum 静态漫游页（index.html + tour.json；若提供本地 Pannellum 则拷入 pannellum/）。"""
    out = Path(tour_dir)
    out.mkdir(parents=True, exist_ok=True)

    local_pan = _find_pannellum(pannellum_dir)
    if local_pan is not None:
        dst_pan = out / "pannellum"
        dst_pan.mkdir(parents=True, exist_ok=True)
        for fn in ("pannellum.js", "pannellum.css"):
            shutil.copy2(local_pan / fn, dst_pan / fn)
        css_href = "pannellum/pannellum.css"
        js_src = "pannellum/pannellum.js"
        offline = True
    else:
        base = cdn_base.rstrip("/")
        css_href = f"{base}/pannellum.css"
        js_src = f"{base}/pannellum.js"
        offline = False

    scenes: dict[str, Any] = {}
    room_list: list[dict[str, Any]] = []
    for st in plan["stations"]:
        sid = st["id"]
        scenes[sid] = {
            "title": st["name"],
            "type": "equirectangular",
            "panorama": f"img/{safe_filename(sid)}.{img_ext}",
            "hotSpots": [
                {
                    "pitch": hs["pitch"],
                    "yaw": hs["yaw"],
                    "type": "scene",
                    "text": hs["text"],
                    "sceneId": hs["sceneId"],
                }
                for hs in st["hotspots"]
            ],
        }
        room_list.append({"id": sid, "name": st["name"], "area_m2": st["area_m2"]})

    config = {
        "default": {
            "firstScene": plan["first_scene"],
            "sceneFadeDuration": 500,
            "autoLoad": True,
            "showControls": True,
            "orientationOnByDefault": False,
            "hfov": hfov,
        },
        "scenes": scenes,
    }
    tour_json = {
        "title": title,
        "offline_pannellum": offline,
        "first_scene": plan["first_scene"],
        "rooms": room_list,
        "stations": plan["stations"],
        "pannellum": config,
    }
    json_path = out / "tour.json"
    json_path.write_text(json.dumps(tour_json, ensure_ascii=False, indent=1), encoding="utf-8")

    safe_json = _json_for_html(tour_json)
    safe_cfg = _json_for_html(config)
    safe_first = _json_for_html(plan["first_scene"])
    buttons = "\n".join(
        f'  <button type="button" data-scene="{html.escape(r["id"])}">{html.escape(r["name"])}</button>'
        for r in room_list
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="generator" content="cn-bim-toolkit">
<link rel="stylesheet" href="{html.escape(css_href)}">
<style>
html,body{{margin:0;height:100%;background:#111;font-family:"PingFang SC","Microsoft YaHei",sans-serif;overflow:hidden}}
#pano{{width:100%;height:100%}}
#nav{{position:fixed;left:12px;top:12px;display:flex;flex-wrap:wrap;gap:6px;max-width:calc(100vw - 96px);z-index:10}}
#nav button{{border:0;border-radius:6px;padding:6px 11px;font-size:13px;cursor:pointer;color:#fff;background:rgba(0,0,0,.58);backdrop-filter:blur(4px)}}
#nav button.active{{background:#1976d2}}
#tip{{position:fixed;left:12px;bottom:12px;color:#fff;background:rgba(0,0,0,.55);padding:5px 10px;border-radius:6px;font-size:13px;z-index:10}}
</style>
</head>
<body>
<div id="nav">
{buttons}
</div>
<div id="pano"></div>
<div id="tip">拖动环视 · 点击门口箭头或左上角房名切换房间</div>
<script src="{html.escape(js_src)}"></script>
<script>
const viewer = pannellum.viewer('pano', {safe_cfg});
const btns = Array.from(document.querySelectorAll('#nav button'));
function syncNav(id) {{ btns.forEach(b => b.classList.toggle('active', b.dataset.scene === id)); }}
btns.forEach(b => b.addEventListener('click', () => viewer.loadScene(b.dataset.scene)));
viewer.on('scenechange', syncNav);
syncNav({safe_first});
</script>
<script type="application/json" id="data">{safe_json}</script>
</body>
</html>
"""
    html_path = out / "index.html"
    html_path.write_text(html_text, encoding="utf-8")
    return {
        "index_html": str(html_path.resolve()),
        "tour_json": str(json_path.resolve()),
        "offline_pannellum": offline,
        "scenes": len(scenes),
        "hotspots": plan["hotspot_count"],
    }
