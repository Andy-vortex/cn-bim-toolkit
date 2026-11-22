"""门窗编号文字的解析（只用标准库；识别 dxfplan、建模 plan2ifc、编号 openings 三处共用）。

国内门窗编号惯例：代号（M、C、FM、TC…，至少一个大写字母）+ 可选防火等级（甲/乙/丙）+ 宽、高各两位分米，
如 M0921、C1815、FM乙1021、TC1818。没有代号的串（"1121""乙1121"，多是拆开的编号碎片）不算编号。
原图上的编号只有在宽度位和识别出的洞口宽度对得上时才可信。
"""
from __future__ import annotations

import re

_CODE = re.compile(r"([A-Z]+)([甲乙丙])?(\d{2})(\d{2})")
FIRE_PREFIXES = ("FM", "FD", "FH")   # 防火门类代号；FD、FH 各家含义不一，图上没写清时只按"防火门"处理
TOL_MM = 100.0                       # 编号按分米取整，宽高差 1dm 以内算对得上


def parse(label: str) -> dict | None:
    """'FM乙1221' → {'prefix': 'FM', 'grade': '乙', 'w': 1200, 'h': 2100}；不是"代号+宽高"格式（没有代号的也算不是）返回 None。"""
    m = _CODE.fullmatch((label or "").strip())
    if not m:
        return None
    return {"prefix": m.group(1), "grade": m.group(2) or "", "w": int(m.group(3)) * 100, "h": int(m.group(4)) * 100}


def fits(label: str, width: float, height: float | None = None, tol: float = TOL_MM) -> bool:
    """编号里的宽（和高，给了的话）与实际尺寸相差都不超过 tol。"""
    p = parse(label)
    if p is None or abs(p["w"] - width) > tol:
        return False
    return height is None or abs(p["h"] - height) <= tol


def fire_rating(label: str) -> str | None:
    """防火门类代号（FM/FD/FH）→ Pset_DoorCommon.FireRating 的写法（'乙级'；没写等级为'待定'）；不是防火门返回 None。"""
    p = parse(label)
    if p is None or p["prefix"] not in FIRE_PREFIXES:
        return None
    return p["grade"] + "级" if p["grade"] else "待定"
