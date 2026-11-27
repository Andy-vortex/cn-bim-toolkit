"""公开自动测试：经 MCP stdio 驱动 Blender/Bonsai 全套出图与规范自查。

环境里有 Blender（JZ_BLENDER 或 PATH）时自动运行，未装 Blender 时自动跳过；
设 JZ_TEST_BLENDER=1 时强制运行（找不到 Blender 会失败）。
运行：PYTHONPATH=src JZ_TEST_BLENDER=1 python -m unittest tests/test_blender_e2e.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jiazhuang import mcp_server  # noqa: E402
from tests.test_mcp_server import _McpClient  # noqa: E402


def _has_blender() -> bool:
    return bool(mcp_server.find_blender())


@unittest.skipUnless(_has_blender() or os.environ.get("JZ_TEST_BLENDER") == "1", "Blender not found (set JZ_BLENDER to run)")
class TestBlenderMcpEndToEnd(unittest.TestCase):
    def test_demo_full_set_and_check_ifc_via_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            cli = _McpClient()
            try:
                cli.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}})
                cli.notify("notifications/initialized")

                rec = t / "recog.json"
                r, err = cli.call_tool("demo_apartment", {"out": str(rec)})
                self.assertFalse(err, r)
                rf, err = cli.call_tool("furnish_place", {
                    "recognition": str(rec), "intent": "packs/cn/furniture/example-intent.json", "out": str(t / "furnish"),
                })
                self.assertFalse(err, rf)
                rc_, err = cli.call_tool("ceiling", {
                    "recognition": str(rec), "scheme": rf["files"]["scheme"],
                    "intent": "packs/cn/ceiling/example-intent.json", "out": str(t / "ceiling"),
                })
                self.assertFalse(err, rc_)
                rm, err = cli.call_tool("mep", {
                    "recognition": str(rec), "scheme": rc_["files"]["scheme"], "out": str(t / "mep"),
                })
                self.assertFalse(err, rm)
                rfl, err = cli.call_tool("flooring", {
                    "recognition": str(rec), "scheme": rm["files"]["scheme"],
                    "intent": "packs/cn/flooring/example-intent.json", "out": str(t / "flooring"),
                })
                self.assertFalse(err, rfl)
                rw, err = cli.call_tool("wall", {
                    "recognition": str(rec), "scheme": rfl["files"]["scheme"],
                    "intent": "packs/cn/wall/example-intent.json", "out": str(t / "wall"),
                })
                self.assertFalse(err, rw)

                # 全套出图（含立面 + 示例规范自查）
                sheets_dir = t / "sheets"
                rs, err = cli.call_tool("make_plan_set", {
                    "recognition": str(rec),
                    "scheme": rw["files"]["scheme"],
                    "out": str(sheets_dir),
                    "kinds": ["plan", "floor", "ceiling", "mep", "elev"],
                    "rules": ["packs/cn/rules/example-home.json"],
                    "project": "示例户型（合成数据）",
                    "title": "{kind}",
                })
                self.assertFalse(err, rs)
                self.assertEqual(rs["returncode"], 0)
                self.assertTrue(Path(rs["files"]["model"]).is_file())
                # 14 张 A3 图（A-00..A-13）+ 1 份自查报告 A-00a
                self.assertEqual(rs["counts"]["pdfs"], 15)
                self.assertEqual(rs["counts"]["pngs"], 14)
                self.assertEqual(rs["counts"]["xlsx"], 7)
                self.assertEqual(rs["counts"]["reports"], 1)

                # 独立 check_ifc：六种结论原样透传
                chk_dir = t / "check"
                rk, err = cli.call_tool("check_ifc", {
                    "ifc": rs["files"]["model"],
                    "rules": "packs/cn/rules/example-home.json",
                    "out": str(chk_dir),
                    "project": "示例户型",
                })
                self.assertFalse(err, rk)
                self.assertEqual(set(rk["status_counts"].keys()), set(mcp_server.STATUSES))
                self.assertEqual(sum(rk["status_counts"].values()), len(rk["results"]))
                self.assertGreater(len(rk["results"]), 0)
                for ext in ("json", "html", "pdf"):
                    self.assertTrue(Path(rk["files"][ext]).is_file())

                # 缺失文件退出 2，不建输出目录
                bad_dir = t / "bad_plan_out"
                rb, err = cli.call_tool("make_plan_set", {"recognition": str(t / "nope.json"), "out": str(bad_dir)})
                self.assertTrue(err)
                self.assertEqual(rb["returncode"], 2)
                self.assertFalse(bad_dir.exists())
            finally:
                self.assertEqual(cli.close(), 0)


if __name__ == "__main__":
    unittest.main()
