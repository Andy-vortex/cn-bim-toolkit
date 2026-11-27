"""家具布置（在 .venv 里跑，不用 Blender）：
  python scripts/furnish.py context <识别.json> [--scheme 方案.json] [--room 客厅,R002] [-o 摘要.json]
      房间摘要：每间房的类型、面积、墙边 W1..Wn 和上面的门窗（给 agent 写布置意图用）；不给 -o 就打印出来
  python scripts/furnish.py place <识别.json> <意图.json> <输出目录> [--scheme 方案.json]
      意图 → <输出目录>/scheme.json（方案 items 加上家具）+ report.json（校验报告）；有违规或意图摆不了退出 1。
      灶台在窗前的橱柜先换摆法（报告的 arranged、日志 JZLOG furnish arranged 写换了哪件、怎么换的）
格式见 src/jiazhuang/furnish.py 和 packs/cn/furniture/README.md。退出码：0 成功，1 有违规或摆不了，2 参数或输入错误。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from jiazhuang import printing  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="python scripts/furnish.py", allow_abbrev=False,
                                description="家具布置：房间摘要（context）；布置意图 → 带家具的方案 + 校验报告（place）。")
    sub = p.add_subparsers(dest="cmd", required=True)
    rec_t = lambda s: printing.read_json(s, "识别结果", ("rooms", "walls"))
    sch_t = lambda s: printing.read_json(s, "方案")

    def intent_t(s):   # 意图的结构（rooms 是对象列表……）在解析参数时就查：结构不对退出 2，单件写错留给 place 报
        from jiazhuang import furnish
        d = printing.read_json(s, "布置意图")
        try:
            furnish.check_intent(d)
        except furnish.IntentError as e:
            raise argparse.ArgumentTypeError(f"{e}：{s}")
        return d
    c = sub.add_parser("context", help="房间摘要 JSON", allow_abbrev=False)
    c.add_argument("recognition", type=rec_t, metavar="识别.json")
    c.add_argument("--scheme", type=sch_t, metavar="方案.json", help="房名用方案里的用途，房间可以带 type 覆盖类型")
    c.add_argument("--room", default="", help="只要这几间（房名或房间 id，逗号分隔）")
    c.add_argument("-o", dest="out", metavar="摘要.json", help="写到文件（缺省打印）")
    pl = sub.add_parser("place", help="意图 → 方案 + 校验报告", allow_abbrev=False)
    pl.add_argument("recognition", type=rec_t, metavar="识别.json")
    pl.add_argument("intent", type=intent_t, metavar="意图.json")
    pl.add_argument("out", metavar="输出目录")
    pl.add_argument("--scheme", type=sch_t, metavar="方案.json", help="在这个方案上加家具（缺省：识别结果原样当方案）")
    a = p.parse_args(argv)

    from jiazhuang import furnish, scheme
    if a.cmd == "context":
        ctx = furnish.room_context(a.recognition, a.scheme, [s for s in a.room.split(",") if s] or None)
        text = json.dumps(ctx, ensure_ascii=False, indent=1)
        if not a.out:
            print(text)
            return
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        printing.done(a.out)
        return
    sch, rep = furnish.furnish(a.recognition, a.scheme or scheme.from_recognition(a.recognition), a.intent)
    os.makedirs(a.out, exist_ok=True)
    for name, data in (("scheme.json", sch), ("report.json", rep)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    for m in rep.get("arranged", []):
        printing.log("furnish", "arranged", m["msg"])
    for e in rep["errors"] + rep["violations"]:
        printing.log("furnish", e["check"], e["msg"])
    printing.log("items", rep["items"], "errors", len(rep["errors"]), "violations", len(rep["violations"]))
    if not rep["ok"]:
        printing.log("failed", len(rep["errors"]) + len(rep["violations"]), os.path.abspath(a.out))
        sys.exit(1)
    printing.done(a.out)


if __name__ == "__main__":
    main()
