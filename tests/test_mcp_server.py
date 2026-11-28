"""公开自动测试：MCP stdio 服务（真实子进程管道 JSON-RPC 2.0，无需 Blender）。

运行：PYTHONPATH=src python -m unittest tests/test_mcp_server.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class _McpClient:
    """最小同步 stdio JSON-RPC 2.0 客户端，用于黑盒测试 scripts/mcp_server.py。"""

    def __init__(self, env_extra: dict[str, str] | None = None):
        env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "mcp_server.py")],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._id = 0

    def send_raw(self, line: str) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        resp = self.proc.stdout.readline()
        if not resp:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(f"MCP server closed stdout unexpectedly; stderr={err}")
        return json.loads(resp)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.send_raw(json.dumps(msg, ensure_ascii=False))

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in resp:
            raise AssertionError(f"unexpected JSON-RPC error: {resp['error']}")
        result = resp["result"]
        payload = json.loads(result["content"][0]["text"])
        return payload, bool(result["isError"])

    def close(self) -> int:
        if self.proc.stdin:
            self.proc.stdin.close()
        rc = self.proc.wait(timeout=15)
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()
        return rc


class TestMcpStdioServer(unittest.TestCase):
    def test_cli_check_and_list_tools_flags(self):
        # 故意清除 PYTHONUTF8 / PYTHONIOENCODING，确认 Windows 默认代码页下仍严格输出 UTF-8 JSON
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONPATH"] = str(SRC)
        p1 = subprocess.run([sys.executable, "scripts/mcp_server.py", "--check"], cwd=str(ROOT), env=env, capture_output=True)
        self.assertEqual(p1.returncode, 0, p1.stderr.decode("utf-8", "replace"))
        data = json.loads(p1.stdout.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["drawing_pack_ok"])

        p2 = subprocess.run([sys.executable, "scripts/mcp_server.py", "--list-tools"], cwd=str(ROOT), env=env, capture_output=True)
        self.assertEqual(p2.returncode, 0)
        tools = json.loads(p2.stdout.decode("utf-8"))
        self.assertEqual(len(tools), 15)

    def test_stage_error_handling_and_first_error_line(self):
        sys.path.insert(0, str(SRC))
        from jiazhuang import mcp_server

        # stderr 普通警告不得遮蔽 stdout 中的 JZLOG failed
        err = mcp_server._first_error_line("Read prefs: harmless warning", "JZLOG stale 1\nJZLOG failed 方案已过时")
        self.assertEqual(err, "JZLOG failed 方案已过时")

        # 超时（rc=124）在已有产物的目录上不得读旧 report.json，且必须保留 error 字段
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "scheme.json").write_text('{"rooms": []}', encoding="utf-8")
            (p / "report.json").write_text('{"ok": true, "walls": 12, "errors": [], "violations": []}', encoding="utf-8")
            res = mcp_server._stage_result(124, "", "timeout after 120s", "", str(p), ("walls",))
            self.assertFalse(res["ok"])
            self.assertEqual(res["returncode"], 124)
            self.assertEqual(res["files"], {})
            self.assertNotIn("counts", res)
            self.assertEqual(res["error"], "timeout after 120s")

    def test_protocol_handshake_resources_and_errors(self):
        cli = _McpClient()
        try:
            # 握手（支持 2024-11-05 与 2025-03-26）
            r = cli.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
            self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
            self.assertEqual(r["result"]["serverInfo"]["name"], "cn-bim-toolkit")
            cli.notify("notifications/initialized")
            self.assertEqual(cli.request("ping")["result"], {})

            # 工具列表
            tools = cli.request("tools/list")["result"]["tools"]
            names = {t["name"] for t in tools}
            self.assertEqual(names, {
                "check_env", "list_packs", "demo_apartment", "dxfplan",
                "furnish_context", "furnish_place", "ceiling", "mep",
                "flooring", "wall", "make_plan_set", "check_ifc", "make_demo_set", "render", "build_viewer",
            })

            # 资源列表与读取 + 路径穿越拦截
            res_list = cli.request("resources/list")["result"]["resources"]
            uris = {r["uri"] for r in res_list}
            self.assertIn("cn-bim://pack/wall/rules.json", uris)
            self.assertIn("cn-bim://pack/furniture/example-intent.json", uris)

            rd = cli.request("resources/read", {"uri": "cn-bim://pack/wall/rules.json"})
            wall_rules = json.loads(rd["result"]["contents"][0]["text"])
            self.assertIn("materials", wall_rules)

            for bad_uri in ("cn-bim://pack/../../README.md", "cn-bim://pack/../conventions.py", "file:///etc/passwd", "cn-bim://pack/nope.json"):
                err = cli.request("resources/read", {"uri": bad_uri})
                self.assertIn("error", err)
                self.assertEqual(err["error"]["code"], -32602)

            # 协议错误处理
            self.assertEqual(cli.send_raw("{not valid json")["error"]["code"], -32700)
            self.assertEqual(cli.request("no/such/method")["error"]["code"], -32601)
            self.assertEqual(cli.request("tools/call", {"name": "unknown_tool", "arguments": {}})["error"]["code"], -32602)
            self.assertEqual(cli.request("tools/call", {"name": "demo_apartment", "arguments": {}})["error"]["code"], -32602)
            self.assertEqual(cli.request("tools/call", {"name": "demo_apartment", "arguments": {"out": "a.json", "extra": 1}})["error"]["code"], -32602)
            self.assertEqual(cli.request("tools/call", {"name": "make_plan_set", "arguments": {"recognition": "a.json", "out": "b", "kinds": ["plan", "bogus"]}})["error"]["code"], -32602)
            self.assertEqual(cli.send_raw('{"jsonrpc":"2.0","id":99,"method":"ping","params":[]}')["error"]["code"], -32602)
        finally:
            self.assertEqual(cli.close(), 0)

    def test_pipeline_tools_via_stdio_and_missing_blender(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            # 故意把 JZ_BLENDER 指到不存在路径，验证纯 Python 工具正常、Blender 工具干净报错
            cli = _McpClient(env_extra={"JZ_BLENDER": str(t / "no_blender.exe")})
            try:
                cli.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
                cli.notify("notifications/initialized")

                env_res, is_err = cli.call_tool("check_env")
                self.assertFalse(is_err)
                self.assertTrue(env_res["ok"])
                self.assertFalse(env_res["blender_ok"])

                packs, is_err = cli.call_tool("list_packs")
                self.assertFalse(is_err)
                self.assertIn("cn_sofa", packs["furniture"]["cn"])
                self.assertIn("回字形", packs["ceiling"]["templates"])
                self.assertIn("tile300x600", packs["wall"]["materials"])

                rec = t / "recog.json"
                r1, is_err = cli.call_tool("demo_apartment", {"out": str(rec)})
                self.assertFalse(is_err, r1)
                self.assertEqual(r1["counts"]["rooms"], 3)

                ctx, is_err = cli.call_tool("furnish_context", {"recognition": str(rec)})
                self.assertFalse(is_err, ctx)
                self.assertEqual(len(ctx["context"]["rooms"]), 3)

                # 家具 → 吊顶 → 水电 → 地面 → 墙面全链路
                f_dir = t / "furnish"
                rf, is_err = cli.call_tool("furnish_place", {
                    "recognition": str(rec),
                    "intent": "packs/cn/furniture/example-intent.json",
                    "out": str(f_dir),
                })
                self.assertFalse(is_err, rf)
                self.assertEqual(rf["counts"]["items"], 7)

                # 家具违规返回 isError=True、returncode=1 和原始 violations
                bad_i = t / "bad.json"
                bad_i.write_text(json.dumps({"rooms": [{"room": "客厅", "items": [
                    {"id": "a", "key": "cn_sofa", "wall": "W3", "align": "center"},
                    {"id": "b", "key": "cn_sofa", "wall": "W3", "align": "center"},
                ]}]}, ensure_ascii=False), encoding="utf-8")
                rb, is_err = cli.call_tool("furnish_place", {"recognition": str(rec), "intent": str(bad_i), "out": str(t / "bad_f")})
                self.assertTrue(is_err)
                self.assertEqual(rb["returncode"], 1)
                self.assertTrue(rb["violations"])

                c_dir = t / "ceiling"
                rc_res, is_err = cli.call_tool("ceiling", {
                    "recognition": str(rec),
                    "scheme": rf["files"]["scheme"],
                    "intent": "packs/cn/ceiling/example-intent.json",
                    "out": str(c_dir),
                })
                self.assertFalse(is_err, rc_res)

                m_dir = t / "mep"
                rm, is_err = cli.call_tool("mep", {
                    "recognition": str(rec),
                    "scheme": rc_res["files"]["scheme"],
                    "out": str(m_dir),
                })
                self.assertFalse(is_err, rm)
                self.assertGreater(rm["counts"]["points"], 0)

                fl_dir = t / "flooring"
                rfl, is_err = cli.call_tool("flooring", {
                    "recognition": str(rec),
                    "scheme": rm["files"]["scheme"],
                    "intent": "packs/cn/flooring/example-intent.json",
                    "out": str(fl_dir),
                })
                self.assertFalse(is_err, rfl)

                w_dir = t / "wall"
                rw, is_err = cli.call_tool("wall", {
                    "recognition": str(rec),
                    "scheme": rfl["files"]["scheme"],
                    "intent": "packs/cn/wall/example-intent.json",
                    "out": str(w_dir),
                })
                self.assertFalse(is_err, rw)
                self.assertEqual(rw["counts"]["elevations"], 7)

                # 无效输入退出 2，不建输出目录
                no_out = t / "nope_out"
                r2, is_err = cli.call_tool("wall", {
                    "recognition": str(t / "missing.json"),
                    "scheme": rfl["files"]["scheme"],
                    "out": str(no_out),
                })
                self.assertTrue(is_err)
                self.assertEqual(r2["returncode"], 2)
                self.assertFalse(no_out.exists())

                # Blender 未安装时返回清晰错误，不抛未捕获异常
                for btool, bargs in (
                    ("make_plan_set", {"recognition": str(rec), "out": str(t / "sheets")}),
                    ("make_demo_set", {"out": str(t / "demo_out")}),
                    ("check_ifc", {"ifc": str(t / "a.ifc"), "rules": "packs/cn/rules/example-home.json", "out": str(t / "chk")}),
                    ("render", {"ifc": str(t / "a.ifc"), "out": str(t / "rend")}),
                ):
                    r_bl, is_err = cli.call_tool(btool, bargs)
                    self.assertTrue(is_err, btool)
                    self.assertEqual(r_bl["returncode"], 2, btool)
                    self.assertIn("Blender", r_bl["error"], btool)

                # dxfplan 经 MCP 识别合成标准层 DXF（含错误 region 返回 1）
                fl_d = t / "floor_dxf"
                p_gen = subprocess.run(
                    [sys.executable, "tools/make_demo_floor.py", str(fl_d)],
                    cwd=str(ROOT), capture_output=True,
                )
                self.assertEqual(p_gen.returncode, 0)
                reg_line = next(ln for ln in p_gen.stdout.decode("utf-8", "replace").splitlines() if ln.startswith("JZLOG region "))
                reg = [float(x) for x in reg_line.split()[2:6]]
                rdxf, is_err = cli.call_tool("dxfplan", {
                    "dxf": str(fl_d / "floor.dxf"),
                    "out": str(fl_d / "recog.json"),
                    "svg": str(fl_d / "recog.svg"),
                    "region": reg,
                })
                self.assertFalse(is_err, rdxf)
                self.assertEqual(rdxf["counts"]["units"], 4)
                self.assertTrue(Path(rdxf["files"]["svg"]).is_file())

                rbad_reg, is_err = cli.call_tool("dxfplan", {
                    "dxf": str(fl_d / "floor.dxf"),
                    "out": str(fl_d / "empty.json"),
                    "region": [0, 0, 1, 1],
                })
                self.assertTrue(is_err)
                self.assertEqual(rbad_reg["returncode"], 1)
                self.assertFalse((fl_d / "empty.json").exists())

                # build_viewer：缺少输入源返回退出码 2 且不建目录；给有效目录生成 index.html
                v_bad_out = t / "viewer_bad"
                rv_bad, is_err = cli.call_tool("build_viewer", {"out": str(v_bad_out)})
                self.assertTrue(is_err)
                self.assertEqual(rv_bad["returncode"], 2)
                self.assertFalse(v_bad_out.exists())

                fake_sheets = t / "fake_sheets"
                fake_sheets.mkdir()
                (fake_sheets / "A-00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                v_out = t / "viewer_ok"
                rv_ok, is_err = cli.call_tool("build_viewer", {"out": str(v_out), "sheets": str(fake_sheets), "title": "测试预览"})
                self.assertFalse(is_err, rv_ok)
                self.assertEqual(rv_ok["counts"]["sheets"], 1)
                self.assertTrue(Path(rv_ok["files"]["index_html"]).is_file())
                self.assertIn("scripts/viewer.py serve", rv_ok["serve_command"])
            finally:
                self.assertEqual(cli.close(), 0)


if __name__ == "__main__":
    unittest.main()
