"""水电点位（在 .venv 里跑，不用 Blender）：
  python scripts/mep.py <识别.json> <方案.json> <输出目录> [--intent 意图.json] [--unit U1,U2]（只重布这几户，户号不存在退出 2）
      带家具的方案（scripts/furnish.py place 的输出）按规则表布点 → <输出目录>/scheme.json（items 加上点位，带回路号和开关
      控制的灯）+ report.json（校验报告）；有违规或意图写错退出 1
格式见 src/jiazhuang/mep.py 和 packs/cn/mep/README.md。退出码：0 成功，1 有违规或意图写错，2 参数或输入错误。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="python scripts/mep.py", allow_abbrev=False,
                                description="水电点位：带家具的方案 [+ 意图] → 带点位的方案 + 校验报告。")

    def intent_t(s):   # 意图的结构在解析参数时就查：结构不对退出 2，单条写错留给布点时报
        from jiazhuang import furnish, mep
        d = printing.read_json(s, "点位意图")
        try:
            mep.check_intent(d)
        except furnish.IntentError as e:
            raise argparse.ArgumentTypeError(f"{e}：{s}")
        return d
    p.add_argument("recognition", type=lambda s: printing.read_json(s, "识别结果", ("rooms", "walls")), metavar="识别.json")
    p.add_argument("scheme", type=lambda s: printing.read_json(s, "方案", ("rooms",)), metavar="方案.json")
    p.add_argument("out", metavar="输出目录")
    p.add_argument("--intent", type=intent_t, metavar="意图.json", help="增删点位（只用锚点，不写坐标）")
    p.add_argument("--unit", default="", help="只重布这几户（户 id，逗号分隔；别的户的点位原样保留；缺省全布）")
    a = p.parse_args(argv)

    from jiazhuang import mep
    units = [u.strip() for u in a.unit.split(",") if u.strip()] or None
    if units and (bad := [u for u in units if u not in mep.unit_ids(a.recognition, a.scheme)]):
        p.error(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(mep.unit_ids(a.recognition, a.scheme)) or '无'}）")
    sch, rep = mep.mep(a.recognition, a.scheme, a.intent, units)
    os.makedirs(a.out, exist_ok=True)
    for name, data in (("scheme.json", sch), ("report.json", rep)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    for e in rep["errors"] + rep["violations"]:
        printing.log("mep", e["check"], e["msg"])
    for s in rep["skipped"]:   # 跟着删掉的开关（规则表里放不下的只进 report.json）
        if "意图" in s["reason"]:
            printing.log("mep", "skipped", f"{s['room']} {s['point']}：{s['reason']}")
    printing.log("points", rep["points"], "errors", len(rep["errors"]), "violations", len(rep["violations"]))
    if not rep["ok"]:
        printing.log("failed", len(rep["errors"]) + len(rep["violations"]), os.path.abspath(a.out))
        sys.exit(1)
    printing.done(a.out)


if __name__ == "__main__":
    main()
