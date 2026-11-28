"""公开自动测试：渲染机位规划、全景热点角度与 Pannellum 漫游页生成（纯 Python，无需 Blender）。"""
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

from jiazhuang import tour  # noqa: E402


class TestTourPlanningAndSite(unittest.TestCase):
    def test_pack_loads_and_has_expected_materials(self):
        pack = tour.load_pack()
        self.assertIn("defaults", pack)
        self.assertIn("materials", pack)
        for k in ("wall_paint", "wall_tile", "floor_wood", "floor_tile_large", "ceil_gypsum", "ceil_alu", "window_glass"):
            self.assertIn(k, pack["materials"])

    def test_yaw_pitch_compass_convention(self):
        # 相机在原点 z=1.45：正北 (+Y) => yaw=0, 正东 (+X) => yaw=+90, 正西 (-X) => yaw=-90, 正南 (-Y) => ±180
        self.assertEqual(tour.yaw_pitch((0, 0, 1.45), (0, 2, 1.45)), (0.0, 0.0))
        self.assertEqual(tour.yaw_pitch((0, 0, 1.45), (2, 0, 1.45)), (90.0, 0.0))
        self.assertEqual(tour.yaw_pitch((0, 0, 1.45), (-2, 0, 1.45)), (-90.0, 0.0))
        yaw_s, pitch_d = tour.yaw_pitch((0, 0, 1.45), (0, -2, 0.45))
        self.assertAlmostEqual(abs(yaw_s), 180.0, places=1)
        self.assertLess(pitch_d, -20.0)

    def test_plan_cameras_avoids_furniture_and_links_doors_and_open_edges(self):
        spaces = [
            {"id": "R1", "name": "客厅", "polygon_m": [[0, 0], [4.4, 0], [4.4, 3.6], [0, 3.6]], "z0": 0.0, "z1": 2.8},
            {"id": "R2", "name": "玄关", "polygon_m": [[4.5, 0], [6.4, 0], [6.4, 1.8], [4.5, 1.8]], "z0": 0.0, "z1": 2.8},
            {"id": "R3", "name": "卫生间", "polygon_m": [[4.5, 1.9], [6.4, 1.9], [6.4, 3.6], [4.5, 3.6]], "z0": 0.0, "z1": 2.4},
            {"id": "R4", "name": "餐厅", "polygon_m": [[0, 3.6], [4.4, 3.6], [4.4, 6.0], [0, 6.0]], "z0": 0.0, "z1": 2.8},
        ]
        doors = [
            {"id": "M2", "name": "M0921", "rooms": ["R1", "R2"], "center_xyz": [4.45, 1.0, 1.05]},
            {"id": "M3", "name": "M0721", "rooms": ["R2", "R3"], "center_xyz": [5.4, 1.85, 1.05]},
        ]
        # 放一件大沙发在客厅北侧，确认全景相机避开它
        furn = {"R1": [[[1.0, 2.2], [3.4, 2.2], [3.4, 3.4], [1.0, 3.4]]]}
        plan = tour.plan_cameras(spaces, doors, furn)

        self.assertEqual(plan["first_scene"], "R1")
        self.assertEqual(len(plan["stations"]), 4)
        by_id = {s["id"]: s for s in plan["stations"]}
        # R1 全景站应避开沙发所在区域 y >= 2.2
        self.assertLess(by_id["R1"]["pano_cam"][1], 2.2)
        # 门洞双向热点 + R1/R4 开敞边双向热点 => 共 6 条热点
        self.assertEqual(plan["hotspot_count"], 6)
        r1_targets = {h["sceneId"] for h in by_id["R1"]["hotspots"]}
        self.assertEqual(r1_targets, {"R2", "R4"})
        # R1 朝 R2（东侧门）yaw 应接近正东（> 60°）
        hs_r2 = next(h for h in by_id["R1"]["hotspots"] if h["sceneId"] == "R2")
        self.assertGreater(hs_r2["yaw"], 60.0)
        self.assertLess(hs_r2["pitch"], 0.0)

        # 非零楼层（如标准层二层 z0=3.0m）门洞热点 pitch 应落在正常平视略向下区间，不能掉到 -80°
        spaces_2f = [
            {"id": "A", "name": "卧室", "polygon_m": [[0, 0], [3.6, 0], [3.6, 3.0], [0, 3.0]], "z0": 3.0, "z1": 5.8},
            {"id": "B", "name": "客厅", "polygon_m": [[3.7, 0], [8.0, 0], [8.0, 4.0], [3.7, 4.0]], "z0": 3.0, "z1": 5.8},
        ]
        doors_2f = [{"id": "M1", "name": "M0921", "rooms": ["A", "B"], "center_xyz": [3.65, 1.5, 4.05]}]
        plan_2f = tour.plan_cameras(spaces_2f, doors_2f)
        hs_2f = next(s for s in plan_2f["stations"] if s["id"] == "A")["hotspots"][0]
        self.assertGreater(hs_2f["pitch"], -30.0)
        self.assertLess(hs_2f["pitch"], 0.0)

        # 构建网页：先测 CDN 模式，再测本地离线 Pannellum 拷贝模式
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            s_cdn = tour.build_site(t / "site_cdn", plan, title="测试户型")
            self.assertFalse(s_cdn["offline_pannellum"])
            self.assertEqual(s_cdn["scenes"], 4)
            html_txt = Path(s_cdn["index_html"]).read_text(encoding="utf-8")
            self.assertIn("cdn.jsdelivr.net/npm/pannellum", html_txt)
            self.assertIn('<script type="application/json" id="data">', html_txt)
            tj = json.loads(Path(s_cdn["tour_json"]).read_text(encoding="utf-8"))
            self.assertEqual(tj["first_scene"], "R1")

            fake_pan = t / "local_pan"
            fake_pan.mkdir()
            (fake_pan / "pannellum.js").write_text("/* stub */", encoding="utf-8")
            (fake_pan / "pannellum.css").write_text("/* stub */", encoding="utf-8")
            s_off = tour.build_site(t / "site_off", plan, title="离线户型", pannellum_dir=fake_pan)
            self.assertTrue(s_off["offline_pannellum"])
            self.assertTrue((t / "site_off" / "pannellum" / "pannellum.js").is_file())
            self.assertIn('src="pannellum/pannellum.js"', Path(s_off["index_html"]).read_text(encoding="utf-8"))

            # HTML/JS 注入防护：恶意 room id / 房名含 </script> 与 <!-- 不得破坏 script 标签闭合
            mal_spaces = [
                {"id": '</script><script>alert("x")</script>', "name": '<!-- <script>alert(1)</script> -->',
                 "polygon_m": [[0, 0], [3.0, 0], [3.0, 3.0], [0, 3.0]], "z0": 0.0, "z1": 2.8},
            ]
            mal_plan = tour.plan_cameras(mal_spaces, [])
            s_mal = tour.build_site(t / "site_mal", mal_plan, title='</script><img src=x>')
            mal_html = Path(s_mal["index_html"]).read_text(encoding="utf-8")
            self.assertEqual(mal_html.count("</script>"), 3)
            self.assertNotIn("<!--", mal_html)
            self.assertEqual(tour.safe_filename("CON"), "room_CON")

    def test_render_cli_arg_validation_without_blender(self):
        env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "should_not_exist"
            p = subprocess.run(
                [sys.executable, "scripts/render.py", str(Path(tmp) / "nope.ifc"), str(out_dir)],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            self.assertEqual(p.returncode, 2)
            self.assertFalse(out_dir.exists())

            # 空 --pannellum 目录（缺少 pannellum.js/css）解析参数直接退出 2，不建输出目录
            dummy_ifc = Path(tmp) / "a.ifc"
            dummy_ifc.write_text("ISO-10303-21;", encoding="utf-8")
            empty_pan = Path(tmp) / "empty_pan"
            empty_pan.mkdir()
            p_pan = subprocess.run(
                [sys.executable, "scripts/render.py", str(dummy_ifc), str(out_dir), "--pannellum", str(empty_pan)],
                cwd=str(ROOT), env=env, capture_output=True,
            )
            self.assertEqual(p_pan.returncode, 2)
            self.assertFalse(out_dir.exists())

            p_help = subprocess.run([sys.executable, "scripts/render.py", "--help"], cwd=str(ROOT), env=env, capture_output=True)
            self.assertEqual(p_help.returncode, 0)
            self.assertIn("--pano-size", p_help.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
