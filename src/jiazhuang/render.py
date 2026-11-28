"""Blender Cycles 室内效果图与 360° 全景渲染（以 IFC4 为唯一真源，只读源 IFC）。

从 IFC4 读取房间空间（IfcSpace）、门窗（IfcDoor/IfcWindow）、墙地顶面层（IfcCovering + JZ_Flooring /
JZ_WallFinish / JZ_Ceiling）、家具（IfcFurniture）与灯具（IfcLightFixture），按 packs/cn/render/materials.json
上程序化 PBR（可选本地 CC0 贴图与 HDRI 升级），自动布置角落透视机位与每间房 360° 全景站并生成 Pannellum 漫游页。
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from . import printing, tour


def _parse_size_mm(text: str) -> tuple[float, float] | None:
    parts = [p.strip() for p in str(text or "").replace("x", "×").replace("*", "×").split("×") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]) / 1000.0, float(parts[1]) / 1000.0
    except ValueError:
        return None


def _entity_of(obj, tool):
    try:
        return tool.Ifc.get_entity(obj)
    except Exception:
        return None


def _is_demolished(entity, uel) -> bool:
    for pset in uel.get_psets(entity).values():
        if isinstance(pset, dict) and str(pset.get("Status", "")).upper() == "DEMOLISH":
            return True
    return False


def _mesh_xy_hull(obj) -> list[list[float]]:
    from mathutils import Vector
    from shapely.geometry import MultiPoint

    if obj.type != "MESH" or not obj.data.vertices:
        return []
    pts = [(round((obj.matrix_world @ v.co).x, 4), round((obj.matrix_world @ v.co).y, 4)) for v in obj.data.vertices]
    # 按底面网格顶点投影求房间实际轮廓（支持矩形与 L 形房间，比 bbox 准确）
    zs = [round((obj.matrix_world @ v.co).z, 3) for v in obj.data.vertices]
    z_min = min(zs)
    bottom_idx = {i for i, z in enumerate(zs) if abs(z - z_min) < 0.05}
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    tris = []
    for poly in obj.data.polygons:
        if all(vi in bottom_idx for vi in poly.vertices):
            coords = [(pts[vi][0], pts[vi][1]) for vi in poly.vertices]
            if len(coords) >= 3:
                p = Polygon(coords)
                if p.is_valid and p.area > 1e-4:
                    tris.append(p)
    if tris:
        u = unary_union(tris)
        if u.geom_type == "MultiPolygon":
            u = max(u.geoms, key=lambda g: g.area)
        if u.geom_type == "Polygon" and u.area > 1e-4:
            return [[round(x, 4), round(y, 4)] for x, y in list(u.exterior.coords)[:-1]]
    hull = MultiPoint(pts).convex_hull
    if hull.geom_type == "Polygon":
        return [[round(x, 4), round(y, 4)] for x, y in list(hull.exterior.coords)[:-1]]
    return []


def _bbox_world(obj) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    from mathutils import Vector

    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    mn = (min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))
    mx = (max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))
    return mn, mx


def _build_material(bpy, name: str, spec: dict[str, Any], assets_dir: Path | None, tile_override: tuple[float, float] | None = None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    out_node = nt.nodes["Material Output"]

    # 1. 建筑玻璃（对阴影和漫射光透明，保证太阳/天光无损耗进室内，相机仍可见微弱玻璃反射）
    if spec.get("pattern") == "arch_glass":
        lp = nt.nodes.new("ShaderNodeLightPath")
        add_ray = nt.nodes.new("ShaderNodeMath")
        add_ray.operation = "MAXIMUM"
        nt.links.new(lp.outputs["Is Shadow Ray"], add_ray.inputs[0])
        nt.links.new(lp.outputs["Is Diffuse Ray"], add_ray.inputs[1])
        transp = nt.nodes.new("ShaderNodeBsdfTransparent")
        transp.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        glossy = nt.nodes.new("ShaderNodeBsdfGlossy")
        glossy.inputs["Color"].default_value = (*spec.get("rgb", [0.92, 0.97, 0.98]), 1.0)
        glossy.inputs["Roughness"].default_value = float(spec.get("roughness", 0.04))
        fres = nt.nodes.new("ShaderNodeLayerWeight")
        fres.inputs["Blend"].default_value = 0.04
        mix_glass = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(fres.outputs["Fresnel"], mix_glass.inputs["Fac"])
        nt.links.new(transp.outputs["BSDF"], mix_glass.inputs[1])
        nt.links.new(glossy.outputs["BSDF"], mix_glass.inputs[2])
        mix_ray = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(add_ray.outputs["Value"], mix_ray.inputs["Fac"])
        nt.links.new(mix_glass.outputs["Shader"], mix_ray.inputs[1])
        nt.links.new(transp.outputs["BSDF"], mix_ray.inputs[2])
        nt.links.new(mix_ray.outputs["Shader"], out_node.inputs["Surface"])
        return m

    # 2. 若传了本地 CC0 贴图目录且存在对应纹理，加载 2K PBR 贴图（用 Path.glob 避免路径含 [ ] 通配符问题）
    ph = spec.get("polyhaven")
    if assets_dir is not None and ph:
        tex_dir = assets_dir / "textures" / ph["folder"]
        diff_files = (sorted(tex_dir.glob("*_diff_*.jpg")) + sorted(tex_dir.glob("*_diff_*.png"))) if tex_dir.is_dir() else []
        if diff_files:
            tc = nt.nodes.new("ShaderNodeTexCoord")
            mp = nt.nodes.new("ShaderNodeMapping")
            tile_m = float(ph.get("tile_m", 2.0))
            mp.inputs["Scale"].default_value = (1.0 / tile_m,) * 3
            nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])

            def _load_tex(suffix: str, cs: str):
                hits = sorted(tex_dir.glob(f"*_{suffix}_*.jpg")) + sorted(tex_dir.glob(f"*_{suffix}_*.png"))
                if not hits:
                    return None
                n = nt.nodes.new("ShaderNodeTexImage")
                n.image = bpy.data.images.load(str(hits[0].resolve()), check_existing=True)
                n.image.colorspace_settings.name = cs
                n.projection = "BOX"
                n.projection_blend = 0.25
                nt.links.new(mp.outputs["Vector"], n.inputs["Vector"])
                return n

            d_node = _load_tex("diff", "sRGB")
            if d_node:
                tint = ph.get("tint")
                if tint:
                    mix = nt.nodes.new("ShaderNodeMix")
                    mix.data_type = "RGBA"
                    mix.blend_type = "MULTIPLY"
                    mix.inputs["Factor"].default_value = 1.0
                    nt.links.new(d_node.outputs["Color"], mix.inputs[6])
                    mix.inputs[7].default_value = (*tint, 1.0)
                    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
                else:
                    nt.links.new(d_node.outputs["Color"], bsdf.inputs["Base Color"])
            r_node = _load_tex("rough", "Non-Color")
            if r_node:
                mul = nt.nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.inputs[1].default_value = float(ph.get("rough_mul", 1.0))
                nt.links.new(r_node.outputs["Color"], mul.inputs[0])
                nt.links.new(mul.outputs[0], bsdf.inputs["Roughness"])
            else:
                bsdf.inputs["Roughness"].default_value = float(spec.get("roughness", 0.5))
            n_node = _load_tex("nor_gl", "Non-Color")
            if n_node:
                nmap = nt.nodes.new("ShaderNodeNormalMap")
                nmap.inputs["Strength"].default_value = float(ph.get("normal", 0.4))
                nt.links.new(n_node.outputs["Color"], nmap.inputs["Color"])
                nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
            if spec.get("coat"):
                bsdf.inputs["Coat Weight"].default_value = float(spec["coat"])
            if spec.get("metallic"):
                bsdf.inputs["Metallic"].default_value = float(spec["metallic"])
            return m

    # 3. 零下载程序化 PBR（瓷砖/木地板/微纹理/纯色）
    rgb = spec.get("rgb", [0.85, 0.85, 0.85])
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = float(spec.get("roughness", 0.5))
    if spec.get("metallic"):
        bsdf.inputs["Metallic"].default_value = float(spec["metallic"])
    if spec.get("coat"):
        bsdf.inputs["Coat Weight"].default_value = float(spec["coat"])
    if spec.get("emit_rgb"):
        bsdf.inputs["Emission Color"].default_value = (*spec["emit_rgb"], 1.0)
        bsdf.inputs["Emission Strength"].default_value = float(spec.get("emit_strength", 5.0))

    pat = spec.get("pattern")
    if pat in ("tile", "wood"):
        tw, th = tile_override or tuple(spec.get("tile_m", [0.6, 0.6]))
        tc = nt.nodes.new("ShaderNodeTexCoord")
        brick = nt.nodes.new("ShaderNodeTexBrick")
        brick.offset = 0.333 if pat == "wood" else 0.0
        brick.inputs["Scale"].default_value = 1.0
        brick.inputs["Brick Width"].default_value = max(0.05, float(tw))
        brick.inputs["Row Height"].default_value = max(0.05, float(th))
        brick.inputs["Mortar Size"].default_value = float(spec.get("mortar_m", 0.003))
        brick.inputs["Mortar Smooth"].default_value = 0.1
        c2 = spec.get("rgb2", [max(0.0, c * 0.96) for c in rgb])
        brick.inputs["Color1"].default_value = (*rgb, 1.0)
        brick.inputs["Color2"].default_value = (*c2, 1.0)
        brick.inputs["Mortar"].default_value = (*spec.get("mortar_rgb", [0.6, 0.6, 0.6]), 1.0)
        # IfcCovering(FLOORING/CEILING/CLADDING) 局部坐标的 (X, Y) 均为表面平面坐标，直接连 Object 即可横平竖直
        nt.links.new(tc.outputs["Object"], brick.inputs["Vector"])
        nt.links.new(brick.outputs["Color"], bsdf.inputs["Base Color"])
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.12 if pat == "wood" else 0.18
        bump.inputs["Distance"].default_value = 0.004
        nt.links.new(brick.outputs["Fac"], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    elif pat == "noise":
        tc = nt.nodes.new("ShaderNodeTexCoord")
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = float(spec.get("pattern_scale", 80.0))
        noise.inputs["Detail"].default_value = 6.0
        nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.06
        bump.inputs["Distance"].default_value = 0.002
        nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def _choose_material_key(entity, uel) -> tuple[str, tuple[float, float] | None]:
    cls = entity.is_a()
    psets = uel.get_psets(entity)
    if cls == "IfcCovering":
        ptype = str(getattr(entity, "PredefinedType", "") or "").upper()
        if ptype == "FLOORING":
            fl = psets.get("JZ_Flooring", {})
            kind = str(fl.get("Kind", "")).lower()
            sz = _parse_size_mm(fl.get("Size", ""))
            if kind == "stone" or fl.get("Part") == "门槛石":
                return "floor_stone", None
            if kind == "wood":
                return "floor_wood", sz
            if sz and max(sz) <= 0.45:
                return "floor_tile_small", sz
            if sz and max(sz) <= 0.65:
                return "floor_tile_mid", sz
            return "floor_tile_large", sz
        if ptype == "CLADDING":
            wf = psets.get("JZ_WallFinish", {})
            kind = str(wf.get("Kind", "")).lower()
            if kind == "wallpaper":
                return "wall_wallpaper", None
            if kind == "tile":
                sz = _parse_size_mm(wf.get("Size", ""))
                if sz and str(wf.get("Dir", "h")).lower() == "v":
                    sz = (min(sz), max(sz))
                elif sz:
                    sz = (max(sz), min(sz))
                return "wall_tile", sz
            return "wall_paint", None
        if ptype == "SKIRTINGBOARD":
            return "skirting", None
        if ptype == "CEILING":
            cl = psets.get("JZ_Ceiling", {})
            if "铝" in str(cl.get("Material", "")):
                return "ceil_alu", None
            return "ceil_gypsum", None
        return "wall_paint", None
    if cls in ("IfcWall", "IfcWallStandardCase"):
        return "wall_paint", None
    if cls == "IfcDoor":
        return "door_wood", None
    if cls == "IfcWindow":
        return "window_glass", None
    if cls == "IfcLightFixture":
        if "灯带" in str(getattr(entity, "Name", "") or ""):
            return "cove_emit", None
        return "lamp_emit", None
    if cls in ("IfcSanitaryTerminal", "IfcWasteTerminal"):
        return "ceramic", None
    if cls in ("IfcOutlet", "IfcSwitchingDevice", "IfcElectricDistributionBoard", "IfcJunctionBox", "IfcValve"):
        return "plastic", None
    if cls == "IfcFurniture":
        fk = str(psets.get("JZ_Furniture", {}).get("Key", "")).lower()
        name = str(getattr(entity, "Name", "") or "")
        if "shower" in fk or "淋浴" in name:
            return "shower_glass", None
        if any(k in fk for k in ("kitchen", "vanity")) or any(k in name for k in ("橱柜", "浴室柜")):
            return "furn_counter", None
        if any(k in fk for k in ("sofa", "bed", "chair")) or any(k in name for k in ("沙发", "床", "椅")):
            return "furn_fabric", None
        if any(k in fk for k in ("wardrobe", "shoe", "bookcase", "tv_cabinet", "cupboard")) or "柜" in name:
            return "furn_cabinet", None
        return "furn_wood", None
    return "wall_paint", None


def _setup_device(bpy, scene, device: str) -> str:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    scene.render.engine = "CYCLES"
    order = ("OPTIX", "CUDA") if device == "auto" else ((device.upper(),) if device in ("optix", "cuda") else ())
    for backend in order:
        try:
            prefs.compute_device_type = backend
            prefs.get_devices()
            gpus = [d for d in prefs.devices if d.type == backend]
            if gpus:
                for d in prefs.devices:
                    d.use = (d.type == backend)
                scene.cycles.device = "GPU"
                return backend
        except Exception:
            continue
    prefs.compute_device_type = "NONE"
    scene.cycles.device = "CPU"
    return "CPU"


def _add_box_slab(bpy, scene, name: str, x0: float, y0: float, x1: float, y1: float, z0: float, z1: float, mat):
    mesh = bpy.data.meshes.new(name)
    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 3, 2, 1), (4, 5, 6, 7),
        (0, 1, 5, 4), (2, 3, 7, 6),
        (1, 2, 6, 5), (3, 0, 4, 7),
    ]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def _setup_world_and_sun(bpy, scene, defaults: dict[str, Any], assets_dir: Path | None) -> str:
    w = scene.world or bpy.data.worlds.new("World")
    scene.world = w
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])

    hdri_dir = (assets_dir / "hdri") if assets_dir else None
    hdrs = (sorted(hdri_dir.glob("*.hdr")) + sorted(hdri_dir.glob("*.exr"))) if (hdri_dir and hdri_dir.is_dir()) else []
    add_extra_sun = True
    if hdrs:
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(str(hdrs[0].resolve()), check_existing=True)
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.9
        sky_mode = f"hdri:{hdrs[0].name}"
    else:
        sky = nt.nodes.new("ShaderNodeTexSky")
        chosen = "HOSEK_WILKIE"
        for candidate in ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING", "HOSEK_WILKIE"):
            try:
                sky.sky_type = candidate
                chosen = candidate
                break
            except (TypeError, ValueError):
                continue
        if hasattr(sky, "sun_disc"):
            sky.sun_disc = True
        if hasattr(sky, "sun_elevation"):
            sky.sun_elevation = math.radians(float(defaults.get("sun_elevation_deg", 28.0)))
        if hasattr(sky, "sun_rotation"):
            sky.sun_rotation = math.radians(float(defaults.get("sun_rotation_deg", 215.0)))
        if hasattr(sky, "sun_intensity"):
            sky.sun_intensity = 0.35
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
        # Blender 物理天空（MULTIPLE_SCATTERING / NISHITA）自带高亮度物理辐射与太阳，强度压到 ~0.09 才能与 HDRI 曝光一致
        if chosen in ("MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "NISHITA"):
            bg.inputs["Strength"].default_value = 0.09
            add_extra_sun = False
        else:
            bg.inputs["Strength"].default_value = 0.55
        sky_mode = f"sky:{chosen.lower()}"

    if add_extra_sun:
        sun = bpy.data.lights.new("JZ_Sun", "SUN")
        sun.energy = float(defaults.get("sun_energy", 2.4))
        sun.angle = math.radians(1.5)
        sun.color = (1.0, 0.96, 0.90)
        so = bpy.data.objects.new("JZ_Sun", sun)
        scene.collection.objects.link(so)
        so.rotation_euler = (math.radians(90.0 - float(defaults.get("sun_elevation_deg", 28.0))), 0.0, math.radians(-70.0))
    return sky_mode


def render_ifc(
    ifc_path: str,
    out_dir: str,
    *,
    mode: str = "all",
    rooms: list[str] | None = None,
    samples: int | None = None,
    pano_samples: int | None = None,
    size: tuple[int, int] | None = None,
    pano_size: tuple[int, int] | None = None,
    device: str = "auto",
    assets_dir: str | None = None,
    pannellum_dir: str | None = None,
    save_blend: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """在 Blender 中载入 IFC 并执行 Cycles 效果图 + 360° 全景渲染与漫游页打包。"""
    import bpy
    import bonsai.tool as tool
    import ifcopenshell.util.element as uel
    from mathutils import Vector
    from shapely.geometry import Point, Polygon

    pack = tour.load_pack()
    defaults = pack["defaults"]
    mat_specs = pack["materials"]

    c_samples = int(samples or defaults["corner_samples"])
    p_samples = int(pano_samples or defaults["pano_samples"])
    c_size = tuple(size or defaults["corner_size"])
    p_size = tuple(pano_size or defaults["pano_size"])
    eye_z = float(defaults["eye_height_m"])
    inset_m = float(defaults["corner_inset_m"])
    lens_mm = float(defaults["corner_lens_mm"])

    raw_assets = assets_dir or os.environ.get("JZ_RENDER_ASSETS")
    a_path = Path(raw_assets).resolve() if raw_assets and Path(raw_assets).is_dir() else None

    out_p = Path(out_dir).resolve()

    bpy.ops.bim.load_project(filepath=str(Path(ifc_path).resolve()), should_start_fresh_session=True)
    scene = bpy.context.scene
    bpy.context.view_layer.update()

    # 1. 扫描 IFC 对象：收集房间、门、家具、灯具并清理非渲染辅助对象
    spaces_info: list[dict[str, Any]] = []
    space_polys: dict[str, Polygon] = {}
    doors_info: list[dict[str, Any]] = []
    furn_boxes: list[tuple[list[list[float]], tuple[float, float]]] = []
    light_items: list[tuple[str, tuple[float, float, float], float]] = []
    all_xs: list[float] = []
    all_ys: list[float] = []
    floor_zs: list[float] = []
    ceil_zs: list[float] = []

    mat_cache: dict[tuple[str, tuple[float, float] | None], Any] = {}

    def get_mat(key: str, tile_override: tuple[float, float] | None = None):
        ck = (key, tile_override)
        if ck not in mat_cache:
            spec = mat_specs.get(key, mat_specs["wall_paint"])
            mat_cache[ck] = _build_material(bpy, f"JZ_{key}_{len(mat_cache)}", spec, a_path, tile_override)
        return mat_cache[ck]

    hide_classes = {"IfcSpace", "IfcOpeningElement", "IfcAnnotation", "IfcGridAxis", "IfcGrid"}

    for obj in list(bpy.data.objects):
        if obj.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        ent = _entity_of(obj, tool)
        if ent is None:
            continue
        cls = ent.is_a()

        if ent.is_a("IfcTypeObject") or _is_demolished(ent, uel):
            obj.hide_render = True
            obj.hide_viewport = True
            continue

        if cls == "IfcSpace" and obj.type == "MESH":
            poly_coords = _mesh_xy_hull(obj)
            mn, mx = _bbox_world(obj)
            ps = uel.get_psets(ent).get("JZ_Space", {})
            rid = str(ent.Name or f"S{ent.id()}")
            rname = str(ps.get("Usage") or ent.LongName or rid)
            ch_mm = ps.get("CeilingHeight_mm")
            z1 = mn[2] + (float(ch_mm) / 1000.0 if ch_mm else (mx[2] - mn[2]))
            floor_zs.append(mn[2])
            ceil_zs.append(z1)
            all_xs.extend([mn[0], mx[0]])
            all_ys.extend([mn[1], mx[1]])
            if poly_coords:
                p = Polygon(poly_coords)
                if p.is_valid and p.area > 1e-4:
                    space_polys[rid] = p
                    spaces_info.append({
                        "id": rid,
                        "name": rname,
                        "polygon_m": poly_coords,
                        "z0": mn[2],
                        "z1": z1,
                        "area_m2": float(ps.get("NetArea_m2") or p.area),
                    })
            obj.hide_render = True
            obj.hide_viewport = True
            continue

        if cls in hide_classes:
            obj.hide_render = True
            obj.hide_viewport = True
            continue

        if cls == "IfcCovering":
            cl = uel.get_psets(ent).get("JZ_Ceiling", {})
            if cl.get("Part") == "窗帘盒":
                obj.hide_render = True
                obj.hide_viewport = True
                continue

        if obj.type == "MESH":
            mn, mx = _bbox_world(obj)
            all_xs.extend([mn[0], mx[0]])
            all_ys.extend([mn[1], mx[1]])

            if cls == "IfcDoor":
                op = uel.get_psets(ent).get("JZ_Opening", {})
                r_str = str(op.get("Rooms", ""))
                rids = [s.strip() for s in r_str.split(",") if s.strip()]
                doors_info.append({
                    "id": str(ent.Name or ent.id()),
                    "name": str(ent.Tag or ent.Name or ""),
                    "rooms": rids,
                    "center_xyz": [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2],
                })
            elif cls in ("IfcFurniture", "IfcSanitaryTerminal"):
                box = [[mn[0], mn[1]], [mx[0], mn[1]], [mx[0], mx[1]], [mn[0], mx[1]]]
                furn_boxes.append((box, ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2)))
            elif cls == "IfcLightFixture":
                cx, cy, cz = (mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2]
                span = max(mx[0] - mn[0], mx[1] - mn[1])
                light_items.append((str(ent.Name or "灯"), (cx, cy, cz), span))

            mkey, tile_ov = _choose_material_key(ent, uel)
            mat = get_mat(mkey, tile_ov)
            obj.data.materials.clear()
            obj.data.materials.append(mat)

    if not spaces_info:
        raise RuntimeError("IFC 中未找到可渲染的房间（IfcSpace）")

    # 2. 按房间归类家具轮廓，规划机位与门洞热点
    furn_by_room: dict[str, list[list[list[float]]]] = {}
    for box, (fx, fy) in furn_boxes:
        pt = Point(fx, fy)
        for rid, poly in space_polys.items():
            if poly.buffer(0.05).contains(pt):
                furn_by_room.setdefault(rid, []).append(box)
                break

    plan = tour.plan_cameras(spaces_info, doors_info, furn_by_room, eye_z=eye_z, inset_m=inset_m)
    if rooms:
        wanted = {r.strip() for r in rooms if r.strip()}
        plan["stations"] = [s for s in plan["stations"] if s["id"] in wanted or s["name"] in wanted]
        if not plan["stations"]:
            raise ValueError(f"未找到指定房间：{','.join(sorted(wanted))}")
        valid_ids = {s["id"] for s in plan["stations"]}
        for s in plan["stations"]:
            s["hotspots"] = [h for h in s["hotspots"] if h["sceneId"] in valid_ids]
        plan["hotspot_count"] = sum(len(s["hotspots"]) for s in plan["stations"])
        if plan["first_scene"] not in valid_ids:
            plan["first_scene"] = plan["stations"][0]["id"]

    # 房间筛选与校验通过后再建输出目录，避免无效 --rooms 留下空目录
    out_p.mkdir(parents=True, exist_ok=True)

    # 3. 补齐渲染用上下封闭结构板（不改源 IFC），防止原顶/门槛处天空漏光
    if all_xs and all_ys:
        pad = 0.25
        x0, x1 = min(all_xs) - pad, max(all_xs) + pad
        y0, y1 = min(all_ys) - pad, max(all_ys) + pad
        slab_t = float(defaults.get("slab_thickness_m", 0.15))
        base_z = min(floor_zs) if floor_zs else 0.0
        ceil_z = max(ceil_zs) if ceil_zs else (base_z + 2.8)
        _add_box_slab(bpy, scene, "JZ_Render_Subfloor", x0, y0, x1, y1, base_z - 0.03 - slab_t, base_z - 0.03, get_mat("slab_subfloor"))
        _add_box_slab(bpy, scene, "JZ_Render_CeilingSlab", x0, y0, x1, y1, ceil_z, ceil_z + slab_t, get_mat("ceil_gypsum"))

    # 4. 天空、太阳与室内灯具光源
    sky_mode = _setup_world_and_sun(bpy, scene, defaults, a_path)
    lit_rooms: set[str] = set()
    for idx, (lname, (lx, ly, lz), span) in enumerate(light_items):
        pt = Point(lx, ly)
        for rid, poly in space_polys.items():
            if poly.buffer(0.1).contains(pt):
                lit_rooms.add(rid)
                break
        if "灯带" in lname:
            continue  # 灯带靠自发光面层与主/筒灯补光，避免点光源过曝
        if "筒灯" in lname:
            ld = bpy.data.lights.new(f"JZ_Downlight_{idx}", "AREA")
            ld.shape = "DISK"
            ld.size = 0.14
            ld.energy = float(defaults.get("downlight_w", 10.0))
            ld.color = (1.0, 0.92, 0.80)
            if hasattr(ld, "spread"):
                ld.spread = math.radians(110.0)
            lo = bpy.data.objects.new(ld.name, ld)
            scene.collection.objects.link(lo)
            lo.location = (lx, ly, lz - 0.03)
        elif "壁灯" in lname:
            ld = bpy.data.lights.new(f"JZ_WallLight_{idx}", "POINT")
            ld.energy = float(defaults.get("wall_light_w", 12.0))
            ld.shadow_soft_size = 0.08
            ld.color = (1.0, 0.90, 0.78)
            lo = bpy.data.objects.new(ld.name, ld)
            scene.collection.objects.link(lo)
            lo.location = (lx, ly, lz)
        else:
            ld = bpy.data.lights.new(f"JZ_MainLight_{idx}", "AREA")
            ld.shape = "RECTANGLE"
            ld.size = max(0.35, min(0.65, span or 0.45))
            ld.size_y = ld.size
            ld.energy = float(defaults.get("main_light_w", 45.0))
            ld.color = (1.0, 0.93, 0.84)
            lo = bpy.data.objects.new(ld.name, ld)
            scene.collection.objects.link(lo)
            lo.location = (lx, ly, lz - 0.05)

    # 没有任何 IfcLightFixture 的房间补一盏柔和顶灯，避免全黑
    for st in plan["stations"]:
        if st["id"] not in lit_rooms:
            ld = bpy.data.lights.new(f"JZ_Fill_{st['id']}", "AREA")
            ld.size = 0.5
            ld.energy = float(defaults.get("fallback_light_w", 35.0))
            ld.color = (1.0, 0.94, 0.86)
            lo = bpy.data.objects.new(ld.name, ld)
            scene.collection.objects.link(lo)
            lo.location = (st["pano_cam"][0], st["pano_cam"][1], st["z1"] - 0.12)

    # 5. Cycles 渲染参数
    active_dev = _setup_device(bpy, scene, device)
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.025
    scene.cycles.use_denoising = True
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.exposure = float(defaults.get("exposure", 0.35))
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        pass
    scene.render.resolution_percentage = 100

    printing.log("render setup", "device", active_dev, "sky", sky_mode, "stations", len(plan["stations"]), "hotspots", plan["hotspot_count"])

    # 6. 逐机位渲染
    corner_files: list[str] = []
    pano_files: list[str] = []
    jpg_files: list[str] = []
    timings: list[dict[str, Any]] = []

    def _shoot(cam_name: str, loc, *, look_at=None, pano: bool = False, res=(1600, 1000), n_samples=128, out_file: Path):
        out_file.parent.mkdir(parents=True, exist_ok=True)
        cd = bpy.data.cameras.new(cam_name)
        co = bpy.data.objects.new(cam_name, cd)
        scene.collection.objects.link(co)
        co.location = tuple(loc)
        if pano:
            cd.type = "PANO"
            try:
                cd.panorama_type = "EQUIRECTANGULAR"
            except Exception:
                cd.cycles.panorama_type = "EQUIRECTANGULAR"
            # 与 tour.yaw_pitch 严格一致：(90°, 0°, 0°) => -Z=+Y(北), +X=+X(东), +Y=+Z(上)
            co.rotation_euler = (math.radians(90.0), 0.0, 0.0)
        else:
            cd.lens = lens_mm
            dvec = Vector(look_at) - Vector(loc)
            co.rotation_euler = dvec.to_track_quat("-Z", "Y").to_euler()
        scene.camera = co
        scene.render.resolution_x, scene.render.resolution_y = int(res[0]), int(res[1])
        scene.cycles.samples = int(n_samples)
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(out_file)
        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        dt = round(time.time() - t0, 1)
        printing.log("render", cam_name, f"{res[0]}x{res[1]}", "samples", n_samples, "sec", dt, out_file.name)
        timings.append({"camera": cam_name, "pano": pano, "size": list(res), "samples": n_samples, "seconds": dt, "file": str(out_file)})

    if mode in ("corner", "all"):
        for st in plan["stations"]:
            fp = out_p / "corners" / f"{st['slug']}.png"
            _shoot(f"corner_{st['id']}", st["corner_cam"], look_at=st["corner_target"], pano=False, res=c_size, n_samples=c_samples, out_file=fp)
            corner_files.append(str(fp))

    tour_info: dict[str, Any] | None = None
    if mode in ("pano", "all"):
        img_dir = out_p / "tour" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        for st in plan["stations"]:
            sid_fn = tour.safe_filename(st["id"])
            png_p = out_p / "panos" / f"{sid_fn}.png"
            _shoot(f"pano_{st['id']}", st["pano_cam"], pano=True, res=p_size, n_samples=p_samples, out_file=png_p)
            pano_files.append(str(png_p))
            # 顺手在 Blender 内压成轻量 JPG 给 Pannellum 网页用（不需 Pillow）
            # PNG 已在渲染时做过 AgX 色调映射，转存 JPG 前须切回 Standard/None/0.0 避免二次 AgX 压灰
            jpg_p = img_dir / f"{sid_fn}.jpg"
            scene.render.image_settings.file_format = "JPEG"
            scene.render.image_settings.quality = int(defaults.get("jpg_quality", 85))
            img = bpy.data.images.load(str(png_p), check_existing=False)
            vs = scene.view_settings
            old_vt, old_look, old_exp = vs.view_transform, vs.look, vs.exposure
            try:
                vs.view_transform = "Standard"
                vs.look = "None"
                vs.exposure = 0.0
                img.save_render(str(jpg_p), scene=scene)
            finally:
                vs.view_transform = old_vt
                vs.look = old_look
                vs.exposure = old_exp
                bpy.data.images.remove(img)
            jpg_files.append(str(jpg_p))

        title = f"{project} 全景漫游" if project else "室内全景漫游"
        tour_info = tour.build_site(
            out_p / "tour",
            plan,
            title=title,
            img_ext="jpg",
            hfov=int(defaults.get("hfov", 100)),
            pannellum_dir=pannellum_dir,
            cdn_base=str(defaults.get("pannellum_cdn", tour.DEFAULT_CDN)),
        )

    blend_path = None
    if save_blend:
        bp = out_p / "scene.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(bp))
        blend_path = str(bp)

    manifest = {
        "ifc": str(Path(ifc_path).resolve()),
        "mode": mode,
        "device": active_dev,
        "sky": sky_mode,
        "assets_dir": str(a_path) if a_path else None,
        "corners": corner_files,
        "panos": pano_files,
        "tour_jpgs": jpg_files,
        "tour": tour_info,
        "blend": blend_path,
        "timings": timings,
    }
    man_p = out_p / "render-manifest.json"
    man_p.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest
