"""墙面（在 .venv 里跑，不用 Blender）：
  python scripts/wall.py <识别.json> <方案.json> <输出目录> [--intent 意图.json] [--unit U1,U2]（只重做这几户，户号不存在退出 2）
      方案（家具、吊顶、水电、地面之后：要读门窗、吊顶底、点位、地面完成面）按规则表（packs/cn/wall/rules.json）和意图给每面墙定做法、
      高度、墙面轮廓、墙砖排砖、出不出立面和立面字母 → <输出目录>/scheme.json（方案加上 walls）+ report.json（校验报告）；
      有违规或意图写错退出 1
格式见 src/jiazhuang/wall.py 和 packs/cn/wall/README.md。退出码：0 成功，1 有违规或意图写错，2 参数或输入错误。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="python scripts/wall.py", allow_abbrev=False,
                                description="墙面：方案 [+ 意图] → 带每面墙做法、墙面轮廓、墙砖排砖、立面字母的方案 + 校验报告。",
                                epilog="流水线：scripts/ceiling.py → scripts/mep.py → scripts/flooring.py → 本命令 → make_plan_set。"
                                       "退出码：0 成功（JZLOG done <输出目录>）；1 有违规或意图写错（点位落在洞口里或面层外、墙砖顶露出墙、面积对不上、"
                                       "房间没分到做法、一间房都没做），方案和报告照样写出（JZLOG failed）；2 参数或输入错误（文件不存在、意图结构不对、"
                                       "户号不存在），不建输出目录。")

    def intent_t(s):   # 意图的结构在解析参数时就查：结构不对退出 2，单条写错留给生成时报
        from jiazhuang import wall
        d = printing.read_json(s, "墙面意图")
        try:
            wall.check_intent(d)
        except wall.IntentError as e:
            raise argparse.ArgumentTypeError(f"{e}：{s}")
        return d
    p.add_argument("recognition", type=lambda s: printing.read_json(s, "识别结果", ("rooms", "walls")), metavar="识别.json")
    p.add_argument("scheme", type=lambda s: printing.read_json(s, "方案", ("rooms",)), metavar="方案.json")
    p.add_argument("out", metavar="输出目录")
    p.add_argument("--intent", type=intent_t, metavar="意图.json", help="按房间、按墙边（W1..Wn）改做法、贴法、顶高、踢脚线，点名出立面")
    p.add_argument("--unit", default="", help="只重做这几户（户 id，逗号分隔；别的户以前生成的墙面原样保留；缺省全做）")
    a = p.parse_args(argv)

    from jiazhuang import unitview, wall
    units = [u.strip() for u in a.unit.split(",") if u.strip()] or None
    if units and (bad := [u for u in units if u not in unitview.unit_ids(a.recognition, a.scheme)]):
        p.error(f"没有户 {'、'.join(bad)}（识别结果里的户：{'、'.join(unitview.unit_ids(a.recognition, a.scheme)) or '无'}）")
    sch, rep = wall.walls(a.recognition, a.scheme, a.intent, units)
    os.makedirs(a.out, exist_ok=True)
    for name, data in (("scheme.json", sch), ("report.json", rep)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    for e in rep["errors"] + rep["violations"]:
        printing.log("wall", e["check"], e["msg"])
    for n in rep["notes"]:
        printing.log("wall", "note", n)
    printing.log("rooms", rep["rooms"], "walls", rep["walls"], "open", rep["open"], "elevations", rep["elevations"], "notes", len(rep["notes"]),
                 "errors", len(rep["errors"]), "violations", len(rep["violations"]))
    if not rep["ok"]:
        printing.log("failed", len(rep["errors"]) + len(rep["violations"]), os.path.abspath(a.out))
        sys.exit(1)
    printing.done(a.out)


if __name__ == "__main__":
    main()
