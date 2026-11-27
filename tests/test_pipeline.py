"""公开自动测试：纯 Python 流水线（无需 Blender）。

覆盖：约定包加载、合成户型与合成标准层 DXF 识别、家具/吊顶/水电/地面/墙面全链与典型反例、按户裁切。
运行：PYTHONPATH=src python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jiazhuang import conventions, scheme, unitview  # noqa: E402


def _run(*args: str, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env, capture_output=True)
    out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.startswith("JZLOG")), "")
    return proc.returncode, out, last


class TestConventions(unittest.TestCase):
    def test_default_pack_complete(self):
        self.assertEqual(conventions.check_pack(), "")
        fonts = conventions.css_font_mm()
        for cls in ("text", "small", "regular", "large", "num", "PLANLEVEL"):
            self.assertIn(cls, fonts)
            self.assertGreater(fonts[cls], 0)
        elev = conventions.get("elevation")
        self.assertEqual(elev["scale"], 50)
        self.assertIn("share_max_walls", elev)
        self.assertTrue(conventions.markers())

    def test_missing_pack_reports_all_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("JZ_DRAWING_PACK")
            os.environ["JZ_DRAWING_PACK"] = tmp
            try:
                err = conventions.check_pack()
            finally:
                if old is None:
                    os.environ.pop("JZ_DRAWING_PACK", None)
                else:
                    os.environ["JZ_DRAWING_PACK"] = old
            self.assertIn("conventions.json", err)
            self.assertIn("symbols.json", err)


class TestDemoPipeline(unittest.TestCase):
    def test_full_python_chain_and_counterexamples(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            rec = t / "recog.json"

            # 1. 示例户型识别 JSON
            rc, out, last = _run("-m", "jiazhuang.demo_apartment", str(rec))
            self.assertEqual(rc, 0, out)
            self.assertEqual(last, f"JZLOG done {rec.resolve()}")
            rec_data = json.loads(rec.read_text(encoding="utf-8"))
            self.assertEqual(len(rec_data["rooms"]), 3)

            # 2. 房间摘要
            ctx_path = t / "context.json"
            rc, out, last = _run("scripts/furnish.py", "context", str(rec), "-o", str(ctx_path))
            self.assertEqual(rc, 0, out)
            ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
            self.assertEqual(len(ctx["rooms"]), 3)
            self.assertTrue(all(r["walls"] for r in ctx["rooms"]))

            # 3. 家具布置（正常 + 碰撞/挡门反例 + 结构错误）
            furn_dir = t / "furnish"
            rc, out, last = _run(
                "scripts/furnish.py", "place", str(rec),
                "packs/cn/furniture/example-intent.json", str(furn_dir),
            )
            self.assertEqual(rc, 0, out)
            self.assertEqual(last, f"JZLOG done {furn_dir.resolve()}")
            f_rep = json.loads((furn_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(f_rep["ok"])
            self.assertEqual(f_rep["items"], 7)

            # 反例：两件沙发叠在同一位置应退出 1 且报 collision
            bad_intent = t / "bad_furn.json"
            bad_intent.write_text(json.dumps({
                "rooms": [{"room": "客厅", "items": [
                    {"id": "s1", "key": "cn_sofa", "params": {"width": 2200}, "wall": "W3", "align": "center"},
                    {"id": "s2", "key": "cn_sofa", "params": {"width": 2200}, "wall": "W3", "align": "center"},
                ]}]
            }, ensure_ascii=False), encoding="utf-8")
            bad_dir = t / "bad_furn_out"
            rc, out, last = _run("scripts/furnish.py", "place", str(rec), str(bad_intent), str(bad_dir))
            self.assertEqual(rc, 1, out)
            self.assertTrue(last.startswith("JZLOG failed"))
            b_rep = json.loads((bad_dir / "report.json").read_text(encoding="utf-8"))
            self.assertFalse(b_rep["ok"])
            self.assertIn("collision", {v["check"] for v in b_rep["violations"]})

            # 结构错误退出 2，不建输出目录
            malformed = t / "malformed.json"
            malformed.write_text('{"rooms": "not_a_list"}', encoding="utf-8")
            no_dir = t / "should_not_exist"
            rc, out, _ = _run("scripts/furnish.py", "place", str(rec), str(malformed), str(no_dir))
            self.assertEqual(rc, 2, out)
            self.assertFalse(no_dir.exists())

            # 4. 吊顶（正常 + 净高不足反例）
            ceil_dir = t / "ceiling"
            rc, out, last = _run(
                "scripts/ceiling.py", str(rec), str(furn_dir / "scheme.json"), str(ceil_dir),
                "--intent", "packs/cn/ceiling/example-intent.json",
            )
            self.assertEqual(rc, 0, out)
            self.assertEqual(last, f"JZLOG done {ceil_dir.resolve()}")

            low_ceil = t / "low_ceil.json"
            low_ceil.write_text(json.dumps({"rooms": [{"room": "客厅", "ceiling": "平顶", "bottom": 1800}]}, ensure_ascii=False), encoding="utf-8")
            rc, out, last = _run(
                "scripts/ceiling.py", str(rec), str(furn_dir / "scheme.json"), str(t / "low_ceil_out"),
                "--intent", str(low_ceil),
            )
            self.assertEqual(rc, 1, out)
            self.assertTrue(last.startswith("JZLOG failed"))

            # 5. 水电点位
            mep_dir = t / "mep"
            rc, out, last = _run("scripts/mep.py", str(rec), str(ceil_dir / "scheme.json"), str(mep_dir))
            self.assertEqual(rc, 0, out)
            m_rep = json.loads((mep_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(m_rep["ok"])
            self.assertGreater(m_rep["points"], 0)

            # 反例：先水电后再改吊顶未重跑水电，ceiling 应报过时退出 1
            rc, out, _ = _run("scripts/ceiling.py", str(rec), str(mep_dir / "scheme.json"), str(t / "stale_ceil"))
            self.assertEqual(rc, 1, out)

            # 6. 地面铺装
            floor_dir = t / "flooring"
            rc, out, last = _run(
                "scripts/flooring.py", str(rec), str(mep_dir / "scheme.json"), str(floor_dir),
                "--intent", "packs/cn/flooring/example-intent.json",
            )
            self.assertEqual(rc, 0, out)
            fl_rep = json.loads((floor_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(fl_rep["ok"])
            self.assertGreaterEqual(fl_rep["zones"], 3)

            # 7. 墙面与立面数据
            wall_dir = t / "wall"
            rc, out, last = _run(
                "scripts/wall.py", str(rec), str(floor_dir / "scheme.json"), str(wall_dir),
                "--intent", "packs/cn/wall/example-intent.json",
            )
            self.assertEqual(rc, 0, out)
            w_rep = json.loads((wall_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(w_rep["ok"])
            self.assertEqual(w_rep["walls"], 12)
            self.assertEqual(w_rep["elevations"], 7)

            # 不存在的户号退出 2，不建输出目录
            bad_unit = t / "bad_unit_out"
            rc, out, _ = _run("scripts/wall.py", str(rec), str(floor_dir / "scheme.json"), str(bad_unit), "--unit", "U99")
            self.assertEqual(rc, 2, out)
            self.assertFalse(bad_unit.exists())


class TestSyntheticFloorRecognition(unittest.TestCase):
    def test_dxf_generation_recognition_and_unit_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            rc, out, last = _run("tools/make_demo_floor.py", str(t))
            self.assertEqual(rc, 0, out)
            region_line = next((ln for ln in out.splitlines() if ln.startswith("JZLOG region ")), "")
            self.assertTrue(region_line, out)
            region = region_line.split()[2:6]
            self.assertEqual(len(region), 4)

            dxf = t / "floor.dxf"
            rec = t / "recog.json"
            svg = t / "recog.svg"
            rc, out, last = _run("-m", "jiazhuang.dxfplan", str(dxf), str(rec), str(svg), "--region", *region)
            self.assertEqual(rc, 0, out)
            self.assertEqual(last, f"JZLOG done {rec.resolve()}")
            self.assertTrue(svg.is_file())
            data = json.loads(rec.read_text(encoding="utf-8"))
            self.assertEqual(len(data["units"]), 4)

            # 按户裁切 U1
            sub_rec, sub_sch = unitview.subset(data, scheme.from_recognition(data), "U1")
            self.assertEqual([u["id"] for u in sub_rec["units"]], ["U1"])
            self.assertTrue(all(r["unit"] == "U1" for r in sub_rec["rooms"]))
            self.assertGreater(len(sub_sch["rooms"]), 0)

            # 错误 region 退出 1，不写输出且清理旧文件
            empty_rec = t / "empty.json"
            empty_rec.write_text("{}", encoding="utf-8")
            rc, out, last = _run("-m", "jiazhuang.dxfplan", str(dxf), str(empty_rec), "--region", "0", "0", "1", "1")
            self.assertEqual(rc, 1, out)
            self.assertIn("check --region", last)
            self.assertFalse(empty_rec.exists())


if __name__ == "__main__":
    unittest.main()
