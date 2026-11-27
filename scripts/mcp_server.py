"""cn-bim-toolkit 的 MCP stdio 入口（纯标准库，不用 Blender 即可启动；出图和 IFC 检查自动调 Blender）。

用法：
  python scripts/mcp_server.py               启动 MCP stdio 服务（按行 JSON-RPC 2.0）
  python scripts/mcp_server.py --check       检查 Python、依赖、约定包、Blender 和 Edge 环境并打印 JSON
  python scripts/mcp_server.py --list-tools  列出所有 MCP 工具名和入参 schema
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python scripts/mcp_server.py",
        allow_abbrev=False,
        description="cn-bim-toolkit MCP stdio 服务（JSON-RPC 2.0，按行分隔 JSON）。",
    )
    p.add_argument("--check", action="store_true", help="检查本地依赖、约定包、Blender 与 Edge 环境并退出")
    p.add_argument("--list-tools", action="store_true", help="打印可用 MCP 工具清单 JSON 并退出")
    a = p.parse_args(argv)

    # 强制 stdio 使用 UTF-8（Windows 控制台缺省常为 GBK），在 --check / --list-tools 输出之前生效
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    from jiazhuang import mcp_server

    if a.check:
        res = mcp_server._tool_check_env({})
        print(json.dumps(res, ensure_ascii=False, indent=1))
        sys.exit(0 if res["ok"] else 1)
    if a.list_tools:
        print(json.dumps(mcp_server.TOOLS, ensure_ascii=False, indent=1))
        return

    mcp_server.serve_stdio()


if __name__ == "__main__":
    main()
