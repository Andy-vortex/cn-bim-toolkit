"""打印：HTML 页面骨架，Edge 无头打印 PDF、截图 PNG、读页内脚本算出的结果；以及命令行脚本共用的 JZLOG 输出（log、done）和参数检查（existing_file）。
不依赖 bpy，出图（drawset）、规范报告（checker）和各入口脚本共用。"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _find_edge() -> str:
    """Edge（Chromium 内核，无头模式能把 HTML 打成 PDF、截成 PNG，不用装 Inkscape）：环境变量 JZ_EDGE，
    否则 PATH 里的 msedge，再试 Program Files、Program Files (x86)；都没有时返回 "msedge"，打印时记失败。"""
    if os.environ.get("JZ_EDGE"):
        return os.environ["JZ_EDGE"]
    found = shutil.which("msedge")
    if found:
        return found
    for env, dflt in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        p = os.path.join(os.environ.get(env, dflt), "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(p):
            return p
    return "msedge"


EDGE = _find_edge()
FAILED: list[str] = []   # 本次运行没打出来的 PDF/PNG（done 按它决定最后一行和退出码）

# A4 页面（自查报告、拆改清单）的基础样式：雅黑；表格只画横线，表头灰色小字，分组行加粗、下面一道深线；
# 灰色次要文字；不通过用红，待确认、信息不足和要人拍板的用橙
A4_CSS = ("@page{size:A4;margin:14mm 13mm}"
          "body{font-family:'Microsoft YaHei',sans-serif;font-size:10pt;color:#222}h1{font-size:16pt;margin:0 0 4pt}"
          "table{border-collapse:collapse;width:100%;margin:10pt 0}"
          "th,td{border-bottom:.5pt solid #ccc;padding:3pt 5pt;vertical-align:top;text-align:left}th{white-space:nowrap}"
          ".minor,thead th{color:#777;font-size:9pt;font-weight:normal}"
          "tbody th{font-size:12pt;padding-top:12pt;border-bottom:1pt solid #222}"
          ".fail{color:#c62828}.warn{color:#e65100}")


def log(*a) -> None:
    print("JZLOG", *a, flush=True)


def done(out: str) -> None:
    """入口脚本的最后一行：全部打出来写 "JZLOG done <输出>"；有没打出来的写 "JZLOG failed <张数> <输出>"，退出码 1。"""
    out = os.path.abspath(out)
    if FAILED:
        log("failed", len(FAILED), out)
        sys.exit(1)
    log("done", out)


def existing_file(p: str) -> str:
    """argparse 的 type：文件不存在时报一行错误（退出码 2），不等到读文件时才抛 traceback。"""
    if not os.path.isfile(p):
        raise argparse.ArgumentTypeError(f"文件不存在：{p}")
    return p


def read_json(p: str, what: str, need=()):
    """argparse 的 type：文件要在，是 JSON 对象，有 need 里的键（非空），否则报一行错（退出码 2）。"""
    try:
        with open(existing_file(p), encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        raise argparse.ArgumentTypeError(f"不是{what} JSON：{p}")
    miss = [k for k in need if not d.get(k)]
    if miss:
        raise argparse.ArgumentTypeError(f"{what}里没有 {'、'.join(miss)}：{p}")
    return d


def json_script(data) -> str:
    """把数据原样内嵌进 HTML：<script type="application/json" id="data">，"</" 写成 "<\\/"，不会提前结束 script。"""
    return ('<script type="application/json" id="data">'
            + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")


def page(title: str, body: str, data=None, css: str = A4_CSS) -> str:
    """HTML 页面骨架：<title>（打出的 PDF 的文档标题）、generator 元数据、样式；给了 data 就在末尾内嵌一份 JSON。"""
    return ('<!doctype html><html><head><meta charset="utf-8">'
            f'<title>{html.escape(title)}</title><meta name="generator" content="cn-bim-toolkit">'
            f'<style>{css}</style></head><body>{body}'
            + (json_script(data) if data is not None else "") + "</body></html>")


def _edge(kind: str, args, html_path: str, out_path: str, temp: bool) -> int:
    """跑一次 Edge 无头，返回输出文件的字节数（失败为 0）。旧的输出先删掉，失败时不会留一份过期的文件。
    temp：html 是临时打印页，成功后删掉，失败时留着排查。"""
    try:
        os.remove(out_path)
    except OSError:
        pass
    t0, err = time.time(), ""
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    try:
        r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", *args, url], timeout=180, capture_output=True)
        err = f"exit {r.returncode}" if r.returncode else ""
    except (OSError, subprocess.SubprocessError) as e:
        err = f"{type(e).__name__}: {e}"
    ok = os.path.exists(out_path) and os.path.getmtime(out_path) >= t0 - 2
    size = os.path.getsize(out_path) if ok else 0
    if size:
        if temp:
            os.remove(html_path)
        log(kind, os.path.basename(out_path), size)
    else:
        FAILED.append(out_path)
        log(f"{kind} FAILED", os.path.basename(out_path), err or "no output", *(("kept", html_path) if temp else ()))
    return size


def html_to_pdf(html_path: str, pdf_path: str, temp: bool = False) -> int:
    """HTML 打印成 PDF，纸张由页面的 @page 决定。temp=True：html 是临时打印页，成功后删掉。返回 PDF 字节数，失败为 0。"""
    return _edge("pdf", ["--no-pdf-header-footer", f"--print-to-pdf={os.path.abspath(pdf_path)}"], html_path, pdf_path, temp)


def screenshot(html_path: str, png_path: str, size_px=(1587, 1123), scale: float = 1.0, temp: bool = False) -> int:
    """HTML 截图成 PNG：窗口 size_px（CSS 像素），scale 是设备像素比（决定分辨率）。返回 PNG 字节数，失败为 0。"""
    return _edge("png", ["--hide-scrollbars", f"--force-device-scale-factor={scale:.4f}",
                         f"--window-size={size_px[0]},{size_px[1]}", f"--screenshot={os.path.abspath(png_path)}"],
                 html_path, png_path, temp)


def dom_json(html_path: str, window=(1600, 1200)):
    """Edge 无头打开页面，跑完页内脚本后读它写进 <pre id="jz-out"> 的 JSON（--dump-dom）；拿不到返回 None。
    出图时量图纸上已经画了什么用（drawset.place_legend），不出文件，失败不计入 FAILED。"""
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    try:
        r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--dump-dom", "--virtual-time-budget=10000",
                            f"--window-size={window[0]},{window[1]}", url], timeout=180, capture_output=True)
        m = re.search(r'<pre id="jz-out">(.*?)</pre>', r.stdout.decode("utf-8", "replace"), re.S)
        return json.loads(html.unescape(m.group(1))) if m else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
