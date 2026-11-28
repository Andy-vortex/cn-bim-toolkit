"""全案预览站命令行入口：生成静态预览页（build）或起只读本地 HTTP 服务（serve）。

用法：
  python scripts/viewer.py build <输出目录> [--sheets 施工图目录] [--render 渲染目录] [--title 标题]
  python scripts/viewer.py serve [--sheets 施工图目录] [--render 渲染目录] [--host 127.0.0.1] [--port 8765] [--title 标题] [--open]

退出码：0 成功，2 参数或输入目录错误（不建输出目录）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jiazhuang import viewer  # noqa: E402


def _ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _add_sources(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sheets", help="施工图输出目录（含 A-00.png/.pdf、图纸目录.xlsx、*-report.json、model.ifc 等）")
    p.add_argument("--render", help="Cycles 渲染与漫游目录（含 corners/*.png、tour/index.html 等）")
    p.add_argument("--title", help="预览页标题（缺省从图纸目录或目录名推断）")


def _validate_sources(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    if args.sheets is None and args.render is None:
        parser.error("至少指定 --sheets 或 --render 之一")
    s_p: Path | None = None
    r_p: Path | None = None
    if args.sheets is not None:
        if not args.sheets.strip() or not Path(args.sheets).is_dir():
            parser.error(f"--sheets 目录不存在：{args.sheets}")
        s_p = Path(args.sheets)
    if args.render is not None:
        if not args.render.strip() or not Path(args.render).is_dir():
            parser.error(f"--render 目录不存在：{args.render}")
        r_p = Path(args.render)
    return s_p, r_p


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8()
    p = argparse.ArgumentParser(
        prog="python scripts/viewer.py",
        allow_abbrev=False,
        description="全案交付预览站：生成静态总览页或启动零依赖只读本地 HTTP 服务（浏览全套图纸、自查报告与 360° 全景漫游）。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", allow_abbrev=False, help="生成静态 index.html 与 project-index.json")
    pb.add_argument("out", help="输出目录")
    _add_sources(pb)

    ps = sub.add_parser("serve", allow_abbrev=False, help="启动只读本地 HTTP 服务（不修改源目录）")
    _add_sources(ps)
    ps.add_argument("--host", default="127.0.0.1", help="监听地址（缺省 127.0.0.1；局域网手机看漫游传 0.0.0.0）")
    ps.add_argument("--port", type=int, default=8765, help="监听端口（缺省 8765，传 0 随机分配）")
    ps.add_argument("--open", action="store_true", help="启动后用系统默认浏览器打开")

    args = p.parse_args(argv)
    sub_p = pb if args.cmd == "build" else ps
    s_p, r_p = _validate_sources(sub_p, args)

    if args.cmd == "build":
        try:
            res = viewer.build_index(args.out, sheets_dir=s_p, render_dir=r_p, title=args.title)
        except OSError as exc:
            print(f"JZLOG failed {exc}", file=sys.stderr, flush=True)
            return 1
        c = res["counts"]
        print(f"JZLOG viewer sheets={c['sheets']} corners={c['corners']} panos={c['panos']} reports={c['reports']}", flush=True)
        print(f"JZLOG done {os.path.abspath(args.out)}", flush=True)
        return 0

    if not (0 <= args.port <= 65535):
        ps.error(f"--port 必须在 0..65535 之间（收到 {args.port}）")
    try:
        viewer.serve(
            sheets_dir=s_p,
            render_dir=r_p,
            host=args.host,
            port=args.port,
            title=args.title,
            open_browser=args.open,
        )
    except OSError as exc:
        print(f"JZLOG failed {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
