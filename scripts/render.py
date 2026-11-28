"""Blender Cycles 室内渲染与 Pannellum 360° 全景漫游打包（以 IFC4 为唯一真源）。

用法：
  blender -b --python-exit-code 1 --python scripts/render.py -- <model.ifc> <输出目录> [选项]
退出码：0 成功（JZLOG done <输出目录>）；1 渲染或场景构建失败；2 参数或输入错误（不建输出目录）。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402


def _size_t(s: str) -> tuple[int, int]:
    parts = s.lower().replace("×", "x").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"分辨率应为 宽x高（如 1600x1000）：{s}")
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"分辨率必须是正整数：{s}") from e
    if w < 64 or h < 64:
        raise argparse.ArgumentTypeError(f"分辨率不能小于 64x64：{s}")
    return w, h


def _pos_int(s: str) -> int:
    try:
        v = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"必须是正整数：{s}") from e
    if v < 1:
        raise argparse.ArgumentTypeError(f"必须大于 0：{s}")
    return v


def _existing_dir(s: str) -> str:
    if not os.path.isdir(s):
        raise argparse.ArgumentTypeError(f"目录不存在：{s}")
    return s


def _pannellum_dir(s: str) -> str:
    _existing_dir(s)
    missing = [fn for fn in ("pannellum.js", "pannellum.css") if not os.path.isfile(os.path.join(s, fn))]
    if missing:
        raise argparse.ArgumentTypeError(f"Pannellum 目录缺少 {'、'.join(missing)}：{s}")
    return s


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="blender -b --python-exit-code 1 --python scripts/render.py --",
        allow_abbrev=False,
        description="从 IFC4 模型用 Cycles 渲染各房间角落透视图与 360° 全景，并生成 Pannellum 全景漫游页。",
    )
    p.add_argument("ifc", type=printing.existing_file, metavar="model.ifc", help="输入 IFC4 模型")
    p.add_argument("out", metavar="输出目录", help="输出目录（含 corners/、panos/、tour/、render-manifest.json）")
    p.add_argument("--mode", default="all", choices=("corner", "pano", "all"), help="渲染模式：corner 角落图 / pano 全景漫游 / all 全部（缺省 all）")
    p.add_argument("--rooms", default="", help="只渲指定房间（房名或房间 id，逗号分隔；缺省全部房间）")
    p.add_argument("--samples", type=_pos_int, help="角落透视图 Cycles 采样数")
    p.add_argument("--pano-samples", type=_pos_int, help="360° 全景图 Cycles 采样数")
    p.add_argument("--size", type=_size_t, metavar="WxH", help="角落图分辨率（如 1600x1000）")
    p.add_argument("--pano-size", type=_size_t, metavar="WxH", help="全景图分辨率（2:1，如 3072x1536）")
    p.add_argument("--device", default="auto", choices=("auto", "optix", "cuda", "cpu"), help="Cycles 计算设备（缺省 auto）")
    p.add_argument("--assets", type=_existing_dir, metavar="素材目录", help="可选本地 CC0 贴图与 HDRI 目录（缺省读 JZ_RENDER_ASSETS，无则用程序化 PBR）")
    p.add_argument("--pannellum", type=_pannellum_dir, metavar="pannellum目录", help="可选本地 Pannellum 目录（含 pannellum.js/css，拷入 tour/pannellum/ 离线使用）")
    p.add_argument("--save-blend", action="store_true", help="保存组装后的 scene.blend")
    p.add_argument("--project", help="漫游页标题前缀")
    return p.parse_args(argv)


def main(argv=None):
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else (sys.argv[1:] if sys.argv[0].endswith(".py") else [])
    a = parse_args(argv)

    from jiazhuang import render

    rooms = [s.strip() for s in a.rooms.split(",") if s.strip()] or None
    try:
        render.render_ifc(
            a.ifc,
            a.out,
            mode=a.mode,
            rooms=rooms,
            samples=a.samples,
            pano_samples=a.pano_samples,
            size=a.size,
            pano_size=a.pano_size,
            device=a.device,
            assets_dir=a.assets,
            pannellum_dir=a.pannellum,
            save_blend=a.save_blend,
            project=a.project,
        )
    except Exception as e:  # noqa: BLE001
        printing.log("failed", f"{type(e).__name__}: {e}", os.path.abspath(a.out))
        sys.exit(1)
    printing.done(a.out)


if __name__ == "__main__":
    main()
