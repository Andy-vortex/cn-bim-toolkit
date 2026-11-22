"""读约定包里的国内出图约定（只用标准库）：门窗编号代号和类别、默认高度、尺寸排布、表格字号、图框尺寸、墙体图例名称、
折行断点，以及 cn.css 里的字号、markers-CN.svg 里的出图标记（标高符号等）。

数据只有一份，在约定包目录（pack_dir()）的 conventions.json、cn.css、markers-CN.svg 里，代码只读取；文件不在或缺某一项时
直接报错，不用别处的数据顶替。环境变量 JZ_DRAWING_PACK 指向另一个完整的约定包时整包换用它（不和默认包合并）：
出图的样式表、图框模板、视图标题、出图标记、符号库和这里读的约定都从这一个目录来。读约定包的模块（annotate、drawset、
openings、plan2ifc、schedules）都在导入时读；包里缺文件时，入口脚本解析完参数、导入这些模块之前就报（check_pack）。
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_PACK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "packs", "cn", "drawing"))
# cn.css 的字号档：Bonsai 的五档（样式类同名），num（只有数字、字母的小字），PLANLEVEL（Bonsai 写楼面标高数字用的类）
FONT_CLASSES = ("small", "regular", "large", "header", "title", "num", "PLANLEVEL")

_CACHE: dict[str, dict] = {}


class MissingConvention(KeyError):
    def __str__(self):
        return self.args[0]


class _Section(dict):
    """约定的一节（以 _ 开头的说明键去掉）：缺键时报出是哪个文件的哪一项。"""

    def __init__(self, data: dict, where: str, path: str):
        super().__init__({k: _wrap(v, f"{where}.{k}", path) for k, v in data.items() if not k.startswith("_")})
        self._where, self._path = where, path

    def __missing__(self, key):
        raise MissingConvention(f"约定包缺少 {self._where}.{key}（{self._path}）")


def _wrap(v, where, path):
    if isinstance(v, dict):
        return _Section(v, where, path)
    if isinstance(v, list):
        return [_wrap(x, where, path) for x in v]
    return v


def pack_dir() -> str:
    """约定包目录：环境变量 JZ_DRAWING_PACK（一个完整的约定包），不设时是仓库里的 packs/cn/drawing。"""
    return os.path.abspath(os.environ.get("JZ_DRAWING_PACK") or DEFAULT_PACK)


def pack_file(name: str) -> str:
    """约定包里的一个文件（如 "cn.css"、"symbols.json"、"titleblocks"）；不在时报错。"""
    p = os.path.join(pack_dir(), name)
    if not os.path.exists(p):
        raise FileNotFoundError(f"约定包缺少 {name}：{p}")
    return p


# 出图要用的约定包文件（表格图框 titleblocks/A3-CN-TABLE.svg 可以没有，没有时表格图纸用 A3-CN）
PACK_FILES = ("conventions.json", "cn.css", "markers-CN.svg", "symbols.json", "view-title-CN.svg", "titleblocks/A3-CN.svg")


def check_pack(names=PACK_FILES) -> str:
    """约定包里 names 都在时返回 ""，否则返回一行错误，一次列全缺的文件："约定包缺少 a、b：<目录>"。
    入口脚本解析完参数、建输出目录之前调用，有缺就 argparse 报错（退出码 2）。"""
    d = pack_dir()
    missing = [n for n in names if not os.path.isfile(os.path.join(d, *n.split("/")))]
    return f"约定包缺少 {'、'.join(missing)}：{d}" if missing else ""


def get(section: str | None = None) -> dict:
    """约定（约定包的 conventions.json）；section 给了就只返回那一节。"""
    p = pack_file("conventions.json")
    if p not in _CACHE:
        with open(p, encoding="utf-8") as fh:
            _CACHE[p] = _Section(json.load(fh), "conventions", p)
    return _CACHE[p][section] if section else _CACHE[p]


def css_font_mm() -> dict:
    """cn.css 里各字号档的字号（图纸毫米；字号即字高）：{"text": 默认（尺寸数字）, "small", "regular", "large", "header",
    "title", "num": 只有数字、字母的小字, "PLANLEVEL": 楼面标高的数字}。"""
    out, p = {}, pack_file("cn.css")
    with open(p, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"font-size\s*:\s*([\d.]+)px", body)
        if not m:
            continue
        names = [s.strip() for s in sel.split(",")]
        if "text" in names:
            out["text"] = float(m.group(1))
        for cls in FONT_CLASSES:
            if f"text.{cls}" in names:
                out[cls] = float(m.group(1))
    missing = [c for c in ("text",) + FONT_CLASSES if c not in out]
    if missing:
        raise MissingConvention(f"cn.css 缺少字号：{'、'.join(c if c == 'text' else 'text.' + c for c in missing)}（{p}）")
    return out


def line_breaks() -> tuple[str, str]:
    """折行的自然断点：(断在这些字后面, 断在这些字前面)。空格、"/"、右括号和 conventions.json 的 schedule.breaks（顿号、
    逗号等）后面，左括号前面。图例和标题栏（drawset.wrap_text）、房间名（annotate._wrap_name）共用这一份。"""
    return " /)）" + get("schedule")["breaks"], "(（"


def markers() -> dict:
    """约定包 markers-CN.svg 里的出图标记：{id: <marker …>…</marker> 原文}。
    出图时（drawset.configure_project）换掉 Bonsai 自带 markers.svg 里同 id 的标记。"""
    with open(pack_file("markers-CN.svg"), encoding="utf-8") as fh:
        return {m.group(1): m.group(0) for m in re.finditer(r'<marker\b[^>]*\bid="([^"]+)"[^>]*>.*?</marker>', fh.read(), flags=re.S)}


def marker_mm(marker_id: str) -> tuple[float, float]:
    """约定包里一个标记的宽、高（图纸毫米，markerUnits="userSpaceOnUse" 的 markerWidth、markerHeight）。"""
    m = markers().get(marker_id)
    if m is None:
        raise MissingConvention(f"约定包缺少标记 {marker_id}（markers-CN.svg）")
    size = lambda k: float(re.search(rf'\b{k}="([\d.]+)"', m).group(1))
    return size("markerWidth"), size("markerHeight")
