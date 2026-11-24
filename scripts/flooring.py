"""地面（在 .venv 里跑，不用 Blender）：
  python scripts/flooring.py <识别.json> <方案.json> <输出目录> [--intent 意图.json] [--unit U1,U2]（只重做这几户，户号不存在退出 2）
      方案（家具、吊顶、水电之后：要读地漏的位置）按规则表（packs/cn/flooring/rules.json）和意图生成铺装区、排砖、门槛石、
      压条 → <输出目录>/scheme.json（方案加上 flooring）+ report.json（校验报告）；有违规或意图写错退出 1
格式见 src/jiazhuang/flooring.py 和 packs/cn/flooring/README.md。退出码：0 成功，1 有违规或意图写错，2 参数或输入错误。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="python scripts/flooring.py", allow_abbrev=False,
                                description="地面：方案 [+ 意图] → 带铺装区、排砖、门槛石的方案 + 校验报告。",
                                epilog="流水线：scripts/ceiling.py → scripts/mep.py（布地漏）→ 本命令 → make_plan_set --kinds ...,floor。"
                                       "退出码：0 成功（JZLOG done <输出目录>）；1 有违规或意图写错（覆盖面积对不上、门槛石和门对不上、房间没分到材料、"
                                       "一间房都没做），方案和报告照样写出（JZLOG failed）；2 参数或输入错误（文件不存在、意图结构不对、户号不存在），不建输出目录。")

    def intent_t(s):   # 意图的结构在解析参数时就查：结构不对退出 2，单条写错留给生成时报
        from jiazhuang import flooring
        d = printing.read_json(s, "地面意图")
        try:
            flooring.check_intent(d)
        except flooring.IntentError as e:
            raise argparse.ArgumentTypeError(f"{e}：{s}")
        return d
    p.add_argument("recognition", type=lambda s: printing.read_json(s, "识别结果", ("rooms", "walls")), metavar="识别.json")
    p.add_argument("scheme", type=lambda s: printing.read_json(s, "方案", ("rooms",)), metavar="方案.json")
    p.add_argument("out", metavar="输出目录")
    p.add_argument("--intent", type=intent_t, metavar="意图.json", help="按房间改材料、规格、方向、起铺（不写坐标，起铺点除外）")
    p.add_argument("--unit", default="", help="只重做这几户（户 id，逗号分隔；别的户以前生成的地面原样保留；缺省全做）")
    a = p.parse_args(argv)

    from jiazhuang import flooring, unitview
    units = [u.strip() for u in a.unit.split(",") if u.strip()] or None
    if units and (bad := [u for u in units if u not in unitview.unit_ids(a.recognition, a.scheme)]):
        p.error(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unitview.unit_ids(a.recognition, a.scheme)) or '无'}）")
    sch, rep = flooring.flooring(a.recognition, a.scheme, a.intent, units)
    os.makedirs(a.out, exist_ok=True)
    for name, data in (("scheme.json", sch), ("report.json", rep)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    for e in rep["errors"] + rep["violations"]:
        printing.log("flooring", e["check"], e["msg"])
    for n in rep["notes"]:
        printing.log("flooring", "note", n)
    printing.log("zones", rep["zones"], "thresholds", rep["thresholds"], "strips", rep["strips"], "notes", len(rep["notes"]),
                 "errors", len(rep["errors"]), "violations", len(rep["violations"]))
    if not rep["ok"]:
        printing.log("failed", len(rep["errors"]) + len(rep["violations"]), os.path.abspath(a.out))
        sys.exit(1)
    printing.done(a.out)


if __name__ == "__main__":
    main()
