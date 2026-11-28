"""公开自动测试：全案交付预览站（viewer.py、scripts/viewer.py，纯标准库，无需 Blender）。

覆盖：图纸目录 xlsx 读取、六种规范自查结论原样透传、静态页生成与 XSS 防护、
CLI 参数校验退出 2、只读 HTTP 服务挂载与路径穿越/符号链接拦截。
运行：PYTHONPATH=src python -m unittest tests/test_viewer.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jiazhuang import viewer  # noqa: E402


def _write_minimal_xlsx(path: Path, project: str, rows: list[tuple[str, str, str]]) -> None:
    """用标准库写一份含 inlineStr 的最小 图纸目录.xlsx。"""
    sheet_rows = [
        f'<row r="1"><c r="A1" t="inlineStr"><is><t>图纸目录　工程：{project}</t></is></c></row>',
        '<row r="2"><c r="A2" t="inlineStr"><is><t>序号</t></is></c>'
        '<c r="B2" t="inlineStr"><is><t>图号</t></is></c>'
        '<c r="C2" t="inlineStr"><is><t>图名</t></is></c>'
        '<c r="D2" t="inlineStr"><is><t>比例</t></is></c></row>',
    ]
    for idx, (ident, title, scale) in enumerate(rows, start=1):
        r = idx + 2
        sheet_rows.append(
            f'<row r="{r}"><c r="A{r}" t="n"><v>{idx}</v></c>'
            f'<c r="B{r}" t="inlineStr"><is><t>{ident}</t></is></c>'
            f'<c r="C{r}" t="inlineStr"><is><t>{title}</t></is></c>'
            f'<c r="D{r}" t="inlineStr"><is><t>{scale}</t></is></c></row>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", xml.encode("utf-8"))


def _make_sample_tree(base: Path) -> tuple[Path, Path]:
    sheets = base / "sheets"
    sheets.mkdir()
    (sheets / "A-00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sheets / "A-00.pdf").write_bytes(b"%PDF-1.4\n")
    (sheets / "A-01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sheets / "A-01.pdf").write_bytes(b"%PDF-1.4\n")
    (sheets / "A-10.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sheets / "model.ifc").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
    _write_minimal_xlsx(
        sheets / "图纸目录.xlsx",
        "示例工程",
        [("A-00", "图纸目录", "—"), ("A-01", "平面布置图", "1:50"), ("A-10", "室内立面图", "1:50")],
    )
    (sheets / "example-home-report.json").write_text(
        json.dumps(
            {
                "facts": {"standard": "示例家装检查", "project": "示例工程"},
                "summary": {"不通过": 1, "待确认": 2, "信息不足": 0, "人工复核": 3, "通过": 5, "不涉及": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (sheets / "example-home-report.html").write_text("<html><body>report</body></html>", encoding="utf-8")
    (sheets / "A-00a_example-home-report.pdf").write_bytes(b"%PDF-1.4\n")
    (sheets / "renovation-list.html").write_text("<html><body>reno</body></html>", encoding="utf-8")

    render = base / "render"
    (render / "corners").mkdir(parents=True)
    (render / "tour" / "img").mkdir(parents=True)
    (render / "panos").mkdir(parents=True)
    (render / "corners" / "R1_客厅.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (render / "panos" / "R1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (render / "tour" / "img" / "R1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (render / "tour" / "index.html").write_text("<html><body>tour</body></html>", encoding="utf-8")
    (render / "tour" / "tour.json").write_text(
        json.dumps(
            {
                "title": "示例工程 全景漫游",
                "offline_pannellum": True,
                "rooms": [{"id": "R1", "name": "客厅", "area_m2": 15.6}],
                "stations": [{"id": "R1", "name": "客厅", "slug": "R1_客厅", "hotspots": [{"sceneId": "R2"}]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return sheets, render


class TestViewerBuildAndCli(unittest.TestCase):
    def test_build_index_reads_xlsx_reports_and_escapes_xss(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sheets, render = _make_sample_tree(base)
            out = base / "hub"
            evil = '</script><!--<img src=x onerror=alert(1)>'
            res = viewer.build_index(out, sheets_dir=sheets, render_dir=render, title=evil)

            self.assertEqual(res["counts"]["sheets"], 3)
            self.assertEqual(res["counts"]["corners"], 1)
            self.assertEqual(res["counts"]["panos"], 1)
            self.assertEqual(res["counts"]["hotspots"], 1)
            self.assertEqual(res["counts"]["reports"], 1)
            self.assertEqual(res["counts"]["schedules"], 1)
            # 六种结论原样透传
            self.assertEqual(
                res["verdict_totals"],
                {"不通过": 1, "待确认": 2, "信息不足": 0, "人工复核": 3, "通过": 5, "不涉及": 1},
            )

            idx = json.loads((out / "project-index.json").read_text(encoding="utf-8"))
            self.assertEqual([s["id"] for s in idx["sheets"]["sheets"]], ["A-00", "A-01", "A-10"])
            self.assertEqual(idx["sheets"]["sheets"][1]["title"], "平面布置图")
            self.assertEqual(idx["sheets"]["sheets"][1]["scale"], "1:50")
            self.assertEqual(idx["sheets"]["sheets"][1]["png_url"], "../sheets/A-01.png")
            self.assertEqual(idx["render"]["tour"]["html_url"], "../render/tour/index.html")
            self.assertEqual(idx["render"]["corners"][0]["title"], "客厅")
            self.assertEqual(idx["render"]["panos"][0]["title"], "客厅")

            html_txt = (out / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("</script><!--", html_txt)
            self.assertNotIn("<img src=x onerror=alert(1)>", html_txt)
            self.assertIn("\\u003c/script\\u003e", html_txt)

    def test_cli_build_and_arg_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sheets, render = _make_sample_tree(base)
            env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

            # 缺少 --sheets 和 --render 退出 2，不建输出目录
            no_out = base / "should_not_exist"
            p_bad = subprocess.run([sys.executable, "scripts/viewer.py", "build", str(no_out)], cwd=str(ROOT), env=env, capture_output=True)
            self.assertEqual(p_bad.returncode, 2)
            self.assertFalse(no_out.exists())

            # 不存在的目录退出 2，不建输出目录
            p_bad2 = subprocess.run(
                [sys.executable, "scripts/viewer.py", "build", str(no_out), "--sheets", str(base / "missing")],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            self.assertEqual(p_bad2.returncode, 2)
            self.assertFalse(no_out.exists())

            # 正常 build
            out = base / "cli_hub"
            p_ok = subprocess.run(
                [sys.executable, "scripts/viewer.py", "build", str(out), "--sheets", str(sheets), "--render", str(render)],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            txt = p_ok.stdout.decode("utf-8", "replace") + p_ok.stderr.decode("utf-8", "replace")
            self.assertEqual(p_ok.returncode, 0, txt)
            self.assertIn(f"JZLOG done {out.resolve()}", txt)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "project-index.json").is_file())

            # 空字符串 --sheets 退出 2；输出路径为已有文件返回 1 并输出 JZLOG failed
            p_empty = subprocess.run(
                [sys.executable, "scripts/viewer.py", "build", str(no_out), "--sheets", "", "--render", str(render)],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            self.assertEqual(p_empty.returncode, 2)
            blocker = base / "already_a_file"
            blocker.write_text("x", encoding="utf-8")
            p_file = subprocess.run(
                [sys.executable, "scripts/viewer.py", "build", str(blocker), "--sheets", str(sheets)],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            self.assertEqual(p_file.returncode, 1)
            self.assertIn("JZLOG failed", p_file.stderr.decode("utf-8", "replace"))

    def test_malformed_report_summary_and_cross_drive_flag(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sheets, render = _make_sample_tree(base)
            # 畸形非整数 summary 不应导致崩溃
            (sheets / "bad-report.json").write_text(
                json.dumps({"facts": {"standard": "畸形报告"}, "summary": {"不通过": "oops", "通过": None}}, ensure_ascii=False),
                encoding="utf-8",
            )
            res = viewer.build_index(base / "hub_resilient", sheets_dir=sheets)
            self.assertEqual(res["counts"]["reports"], 2)

            # 跨盘场景下（仅 corners 无 tour）cross_drive_missing 仍须为 True
            (render / "tour" / "index.html").unlink()
            with mock.patch("jiazhuang.viewer.os.path.relpath", side_effect=ValueError("different mount")):
                res_cd = viewer.build_index(base / "hub_cd", render_dir=render)
            self.assertTrue(res_cd["cross_drive_missing"])


class TestViewerHttpServer(unittest.TestCase):
    def test_readonly_http_server_and_path_jail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sheets, render = _make_sample_tree(base)
            secret = base / "secret.txt"
            secret.write_text("TOP_SECRET", encoding="utf-8")

            # 可选：若系统允许创建 symlink，同时测符号链接逃逸拦截
            sym_escape = sheets / "escape.txt"
            has_symlink = False
            try:
                sym_escape.symlink_to(secret)
                has_symlink = True
            except OSError:
                has_symlink = False

            srv = viewer.make_server(sheets_dir=sheets, render_dir=render, host="127.0.0.1", port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{port}"
                with urllib.request.urlopen(f"{base_url}/") as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
                    body = resp.read().decode("utf-8")
                    self.assertIn("示例工程", body)

                with urllib.request.urlopen(f"{base_url}/project-index.json") as resp:
                    idx = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(idx["sheets"]["sheets"][1]["png_url"], "/sheets/A-01.png")
                    self.assertEqual(idx["render"]["tour"]["html_url"], "/render/tour/index.html")

                with urllib.request.urlopen(f"{base_url}/sheets/A-01.png") as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get("Content-Type"), "image/png")

                with urllib.request.urlopen(f"{base_url}/render/tour/index.html") as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("tour", resp.read().decode("utf-8"))

                # 路径穿越、反斜杠、隐藏文件必须被拒（403/404）
                for bad_path in (
                    "/sheets/../secret.txt",
                    "/sheets/%2e%2e/secret.txt",
                    "/sheets/..%5csecret.txt",
                    "/render/%2e%2e/secret.txt",
                    "/sheets/.hidden",
                    "/sheets/not_found.png",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(f"{base_url}{bad_path}")
                    self.assertIn(ctx.exception.code, (403, 404), bad_path)

                if has_symlink:
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(f"{base_url}/sheets/escape.txt")
                    self.assertEqual(ctx.exception.code, 403)

                # 非只读方法拒绝（405）
                req = urllib.request.Request(f"{base_url}/", method="POST", data=b"x")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req)
                self.assertEqual(ctx.exception.code, 405)
            finally:
                srv.shutdown()
                srv.server_close()
                t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
