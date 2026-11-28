"""全案交付预览站与只读本地 HTTP 服务（仅用 Python 标准库，零额外依赖）。

从施工图输出目录（--sheets）和/或 Cycles 渲染漫游目录（--render）自动扫描真实交付件：
- 施工图（*.png / *.pdf，并从 图纸目录.xlsx 读取图名与比例）；
- 规范自查报告（*-report.json / .html / .pdf，六种结论原样透传）与拆改清单；
- 交付表格（*.xlsx）与唯一真源模型（model.ifc）；
- 角落透视效果图（corners/*.png）与 360° 全景漫游（tour/index.html、tour/tour.json）。

提供两种用法：
1. build_index()：生成静态 index.html 与 project-index.json；
2. make_server() / serve()：起纯标准库只读本地 HTTP 服务，将施工图与渲染目录分别安全挂载至
   /sheets/ 与 /render/，直接支持浏览器缩放看图与离线 Pannellum 360° 漫游（不修改源目录）。
"""
from __future__ import annotations

import html
import http.server
import json
import mimetypes
import os
import re
import shutil
import socket
import socketserver
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

VERDICT_ORDER = ("不通过", "待确认", "信息不足", "人工复核", "通过", "不涉及")

REVIEW_ITEMS = (
    "承重墙与结构安全判定（以结构图及物业核定为准，程序仅按墙厚推断）",
    "配电回路容量、保护电器与线径（电工复核）",
    "防水做法与排水坡度",
    "顶面及梁位实测标高",
    "定制柜体按厂家工艺拆单",
    "开工前全屋现场复尺",
)

_MIME_EXTRA = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ifc": "application/octet-stream",
}


def _json_for_html(obj: Any) -> str:
    """安全嵌入 HTML <script> 标签的 JSON，转义 &, <, >, U+2028, U+2029。"""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _natural_key(text: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]


def _quote_rel(rel_posix: str) -> str:
    parts = [p for p in rel_posix.split("/") if p]
    return "/".join(p if p in (".", "..") else urllib.parse.quote(p, safe="") for p in parts)


def _build_url(
    base_dir: Path,
    rel_posix: str,
    *,
    out_dir: Path | None = None,
    mount_prefix: str | None = None,
) -> str | None:
    if mount_prefix is not None:
        prefix = mount_prefix.rstrip("/")
        return f"{prefix}/{_quote_rel(rel_posix)}"
    if out_dir is None:
        return _quote_rel(rel_posix)
    try:
        rel_base = os.path.relpath(str(base_dir.resolve()), str(out_dir.resolve()))
    except ValueError:
        return None
    rel_base_posix = rel_base.replace("\\", "/")
    combined = rel_posix if rel_base_posix in ("", ".") else f"{rel_base_posix}/{rel_posix}"
    return _quote_rel(combined)


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def _read_drawing_list_xlsx(xlsx_path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """用标准库 zipfile + ElementTree 读取 图纸目录.xlsx 中的工程名与图号→图名/比例映射。"""
    if not xlsx_path.is_file():
        return "", {}
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            names = set(zf.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter():
                    if si.tag.endswith("}si") or si.tag == "si":
                        shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t") or t.tag == "t"))
            sheet_name = "xl/worksheets/sheet1.xml"
            if sheet_name not in names:
                sheet_candidates = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
                if not sheet_candidates:
                    return "", {}
                sheet_name = sheet_candidates[0]
            root = ET.fromstring(zf.read(sheet_name))
    except Exception:
        return "", {}

    rows: list[list[str]] = []
    for row_el in root.iter():
        if not (row_el.tag.endswith("}row") or row_el.tag == "row"):
            continue
        cells: dict[int, str] = {}
        for c_el in row_el:
            if not (c_el.tag.endswith("}c") or c_el.tag == "c"):
                continue
            ref = c_el.attrib.get("r", "")
            col_letters = "".join(ch for ch in ref if ch.isalpha()).upper()
            col_idx = 0
            for ch in col_letters:
                col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
            ctype = c_el.attrib.get("t", "")
            val = ""
            if ctype == "inlineStr":
                val = "".join(t.text or "" for t in c_el.iter() if t.tag.endswith("}t") or t.tag == "t")
            elif ctype == "s":
                v_el = next((x for x in c_el if x.tag.endswith("}v") or x.tag == "v"), None)
                if v_el is not None and v_el.text and v_el.text.isdigit():
                    idx = int(v_el.text)
                    val = shared[idx] if 0 <= idx < len(shared) else ""
            else:
                v_el = next((x for x in c_el if x.tag.endswith("}v") or x.tag == "v"), None)
                if v_el is not None and v_el.text:
                    val = v_el.text
            if col_idx > 0:
                cells[col_idx - 1] = val.strip()
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])

    project = ""
    if rows and rows[0]:
        m = re.search(r"工程[：:]\s*(.+)", rows[0][0])
        if m:
            project = m.group(1).strip()

    header_idx = None
    col_map: dict[str, int] = {}
    for i, r in enumerate(rows):
        if "图号" in r and "图名" in r:
            header_idx = i
            for j, cell in enumerate(r):
                if cell in ("图号", "图名", "比例", "图幅", "备注"):
                    col_map[cell] = j
            break
    if header_idx is None:
        return project, {}

    meta: dict[str, dict[str, str]] = {}
    for r in rows[header_idx + 1 :]:
        ident = r[col_map["图号"]].strip() if col_map["图号"] < len(r) else ""
        if not ident or ident.startswith("注"):
            continue
        title = r[col_map["图名"]].strip() if col_map["图名"] < len(r) else ""
        scale = r[col_map["比例"]].strip() if "比例" in col_map and col_map["比例"] < len(r) else ""
        size = r[col_map["图幅"]].strip() if "图幅" in col_map and col_map["图幅"] < len(r) else ""
        note = r[col_map["备注"]].strip() if "备注" in col_map and col_map["备注"] < len(r) else ""
        meta[ident] = {"title": title, "scale": scale, "size": size, "note": note}
    return project, meta


def _is_attachment_file(stem: str) -> bool:
    return stem.endswith("-report") or stem.endswith("renovation-list")


def scan_sheets(
    sheets_dir: Path,
    *,
    out_dir: Path | None = None,
    mount_prefix: str | None = None,
) -> dict[str, Any]:
    """只读扫描施工图输出目录，返回图纸、自查报告、拆改清单、表格与 IFC 清单。"""
    sdir = Path(sheets_dir).resolve()
    if not sdir.is_dir():
        raise ValueError(f"施工图目录不存在或不是目录：{sheets_dir}")

    project_from_xlsx, sheet_meta = _read_drawing_list_xlsx(sdir / "图纸目录.xlsx")
    files = [p for p in sdir.iterdir() if p.is_file()]

    pngs = {p.stem: p for p in files if p.suffix.lower() == ".png" and not _is_attachment_file(p.stem)}
    pdfs = {p.stem: p for p in files if p.suffix.lower() == ".pdf" and not _is_attachment_file(p.stem)}
    all_ids = sorted(set(pngs) | set(pdfs), key=_natural_key)

    sheets: list[dict[str, Any]] = []
    for ident in all_ids:
        m = sheet_meta.get(ident, {})
        png_p = pngs.get(ident)
        pdf_p = pdfs.get(ident)
        sheets.append({
            "id": ident,
            "title": m.get("title") or ident,
            "scale": m.get("scale") or "",
            "size": m.get("size") or "",
            "note": m.get("note") or "",
            "png_rel": png_p.name if png_p else None,
            "pdf_rel": pdf_p.name if pdf_p else None,
            "png_url": _build_url(sdir, png_p.name, out_dir=out_dir, mount_prefix=mount_prefix) if png_p else None,
            "pdf_url": _build_url(sdir, pdf_p.name, out_dir=out_dir, mount_prefix=mount_prefix) if pdf_p else None,
        })

    # 规范自查报告（六种结论原样透传，不合并、不改写）
    reports: list[dict[str, Any]] = []
    report_jsons = sorted((p for p in files if p.name.endswith("-report.json")), key=lambda p: _natural_key(p.name))
    pdf_names = [p.name for p in files if p.suffix.lower() == ".pdf"]
    for rp in report_jsons:
        try:
            data = json.loads(rp.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
        raw_summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        counts = {k: 0 for k in VERDICT_ORDER}
        parsed_summary = False
        for k in VERDICT_ORDER:
            val = raw_summary.get(k)
            if isinstance(val, bool) or val is None:
                continue
            try:
                counts[k] = int(val)
                parsed_summary = True
            except (TypeError, ValueError):
                continue
        if (not raw_summary or not parsed_summary) and isinstance(data.get("results"), list):
            for item in data["results"]:
                st = item.get("status") if isinstance(item, dict) else None
                if st in counts:
                    counts[st] += 1
        stem = rp.name[:-5]  # strip .json -> <name>-report
        html_p = sdir / f"{stem}.html"
        pdf_match = next((n for n in sorted(pdf_names, key=_natural_key) if n == f"{stem}.pdf" or n.endswith(f"_{stem}.pdf")), None)
        reports.append({
            "name": facts.get("standard") or stem,
            "project": facts.get("project") or "",
            "counts": counts,
            "total": sum(counts.values()),
            "json_rel": rp.name,
            "html_rel": html_p.name if html_p.is_file() else None,
            "pdf_rel": pdf_match,
            "json_url": _build_url(sdir, rp.name, out_dir=out_dir, mount_prefix=mount_prefix),
            "html_url": _build_url(sdir, html_p.name, out_dir=out_dir, mount_prefix=mount_prefix) if html_p.is_file() else None,
            "pdf_url": _build_url(sdir, pdf_match, out_dir=out_dir, mount_prefix=mount_prefix) if pdf_match else None,
        })

    # 拆改清单
    reno_html = sdir / "renovation-list.html"
    reno_pdf = next((n for n in sorted(pdf_names, key=_natural_key) if n.endswith("renovation-list.pdf")), None)
    renovation = None
    if reno_html.is_file() or reno_pdf:
        renovation = {
            "html_rel": reno_html.name if reno_html.is_file() else None,
            "pdf_rel": reno_pdf,
            "html_url": _build_url(sdir, reno_html.name, out_dir=out_dir, mount_prefix=mount_prefix) if reno_html.is_file() else None,
            "pdf_url": _build_url(sdir, reno_pdf, out_dir=out_dir, mount_prefix=mount_prefix) if reno_pdf else None,
        }

    # 表格与 IFC 模型
    schedules = []
    for xp in sorted((p for p in files if p.suffix.lower() == ".xlsx"), key=lambda p: _natural_key(p.name)):
        schedules.append({
            "name": xp.name,
            "size_bytes": xp.stat().st_size,
            "size_text": _fmt_size(xp.stat().st_size),
            "url": _build_url(sdir, xp.name, out_dir=out_dir, mount_prefix=mount_prefix),
        })

    ifc_p = sdir / "model.ifc"
    ifc_info = None
    if ifc_p.is_file():
        ifc_info = {
            "name": ifc_p.name,
            "size_bytes": ifc_p.stat().st_size,
            "size_text": _fmt_size(ifc_p.stat().st_size),
            "url": _build_url(sdir, ifc_p.name, out_dir=out_dir, mount_prefix=mount_prefix),
        }

    return {
        "dir": str(sdir),
        "project": project_from_xlsx or (reports[0]["project"] if reports and reports[0]["project"] else ""),
        "sheets": sheets,
        "reports": reports,
        "renovation": renovation,
        "schedules": schedules,
        "ifc": ifc_info,
    }


def scan_render(
    render_dir: Path,
    *,
    out_dir: Path | None = None,
    mount_prefix: str | None = None,
) -> dict[str, Any]:
    """只读扫描 Cycles 渲染与全景漫游目录。"""
    rdir = Path(render_dir).resolve()
    if not rdir.is_dir():
        raise ValueError(f"渲染目录不存在或不是目录：{render_dir}")

    tour_data: dict[str, Any] | None = None
    tour_json_p = rdir / "tour" / "tour.json"
    if tour_json_p.is_file():
        try:
            raw = json.loads(tour_json_p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                tour_data = raw
        except Exception:
            tour_data = None

    slug_to_label: dict[str, str] = {}
    id_to_label: dict[str, str] = {}
    if tour_data and isinstance(tour_data.get("stations"), list):
        for st in tour_data["stations"]:
            if isinstance(st, dict):
                label = str(st.get("name") or st.get("slug") or st.get("id") or "")
                if st.get("slug"):
                    slug_to_label[str(st["slug"])] = label
                if st.get("id"):
                    id_to_label[str(st["id"])] = label

    corners_dir = rdir / "corners"
    corners: list[dict[str, Any]] = []
    if corners_dir.is_dir():
        for cp in sorted((p for p in corners_dir.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")),
                         key=lambda p: _natural_key(p.name)):
            rel = f"corners/{cp.name}"
            label = slug_to_label.get(cp.stem) or cp.stem.replace("_", " ")
            corners.append({
                "id": cp.stem,
                "title": label,
                "rel": rel,
                "url": _build_url(rdir, rel, out_dir=out_dir, mount_prefix=mount_prefix),
            })

    panos_dir = rdir / "panos"
    panos: list[dict[str, Any]] = []
    if panos_dir.is_dir():
        for pp in sorted((p for p in panos_dir.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                         key=lambda p: _natural_key(p.name)):
            rel = f"panos/{pp.name}"
            panos.append({
                "id": pp.stem,
                "title": slug_to_label.get(pp.stem) or id_to_label.get(pp.stem) or pp.stem.replace("_", " "),
                "rel": rel,
                "url": _build_url(rdir, rel, out_dir=out_dir, mount_prefix=mount_prefix),
            })

    tour_html_p = rdir / "tour" / "index.html"
    tour_info = None
    if tour_html_p.is_file():
        stations = tour_data.get("stations", []) if tour_data else []
        hotspots = sum(len(st.get("hotspots", [])) for st in stations if isinstance(st, dict))
        rooms = [
            {"id": str(r.get("id", "")), "name": str(r.get("name", "")), "area_m2": r.get("area_m2")}
            for r in (tour_data.get("rooms", []) if tour_data else [])
            if isinstance(r, dict)
        ]
        tour_info = {
            "title": (tour_data.get("title") if tour_data else None) or "360° 全景漫游",
            "offline_pannellum": bool(tour_data.get("offline_pannellum")) if tour_data else False,
            "scenes": len(stations) if stations else len(panos),
            "hotspots": hotspots,
            "rooms": rooms,
            "html_rel": "tour/index.html",
            "html_url": _build_url(rdir, "tour/index.html", out_dir=out_dir, mount_prefix=mount_prefix),
            "json_url": _build_url(rdir, "tour/tour.json", out_dir=out_dir, mount_prefix=mount_prefix) if tour_json_p.is_file() else None,
        }

    return {
        "dir": str(rdir),
        "title": (tour_data.get("title") if tour_data else "") or "",
        "corners": corners,
        "panos": panos,
        "tour": tour_info,
    }


def collect_index(
    *,
    sheets_dir: Path | None = None,
    render_dir: Path | None = None,
    out_dir: Path | None = None,
    sheets_prefix: str | None = None,
    render_prefix: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """汇总施工图与渲染目录的全部可预览资产，生成结构化索引字典。"""
    if sheets_dir is None and render_dir is None:
        raise ValueError("至少需要提供 --sheets 或 --render 之一")

    sheets_info = scan_sheets(sheets_dir, out_dir=out_dir, mount_prefix=sheets_prefix) if sheets_dir is not None else None
    render_info = scan_render(render_dir, out_dir=out_dir, mount_prefix=render_prefix) if render_dir is not None else None

    inferred = (
        title
        or (sheets_info["project"] if sheets_info and sheets_info.get("project") else "")
        or (render_info["title"].removesuffix(" 全景漫游").strip() if render_info and render_info.get("title") else "")
        or (Path(sheets_dir).resolve().name if sheets_dir is not None else Path(render_dir).resolve().name)
    )
    display_title = f"{inferred} · 全案预览" if not title else title

    verdict_totals = {k: 0 for k in VERDICT_ORDER}
    if sheets_info:
        for rep in sheets_info["reports"]:
            for k in VERDICT_ORDER:
                verdict_totals[k] += int(rep["counts"].get(k, 0))

    cross_drive_missing = False
    if out_dir is not None:
        out_res = Path(out_dir).resolve()
        for src_dir, prefix in ((sheets_dir, sheets_prefix), (render_dir, render_prefix)):
            if src_dir is not None and prefix is None:
                try:
                    os.path.relpath(str(Path(src_dir).resolve()), str(out_res))
                except ValueError:
                    cross_drive_missing = True

    counts = {
        "sheets": len(sheets_info["sheets"]) if sheets_info else 0,
        "reports": len(sheets_info["reports"]) if sheets_info else 0,
        "schedules": len(sheets_info["schedules"]) if sheets_info else 0,
        "corners": len(render_info["corners"]) if render_info else 0,
        "panos": (render_info["tour"]["scenes"] if render_info and render_info["tour"] else len(render_info["panos"]) if render_info else 0),
        "hotspots": render_info["tour"]["hotspots"] if render_info and render_info["tour"] else 0,
    }

    return {
        "title": display_title,
        "verdict_order": list(VERDICT_ORDER),
        "verdict_totals": verdict_totals,
        "counts": counts,
        "cross_drive_missing": cross_drive_missing,
        "sheets": sheets_info,
        "render": render_info,
        "review_items": list(REVIEW_ITEMS),
    }


def render_html(data: dict[str, Any]) -> str:
    """将汇总索引渲染为单文件零依赖交互式 HTML 预览页。"""
    title_esc = html.escape(str(data.get("title") or "全案预览"), quote=True)
    payload_json = _json_for_html(data)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_esc}</title>
<style>
  :root {{
    --bg: #f6f4ef;
    --card: #ffffff;
    --ink: #1b1f24;
    --muted: #5f6875;
    --line: #e2ddd3;
    --accent: #b84a32;
    --accent-soft: #f9ebe7;
    --ok: #1f7a4c;
    --ok-bg: #e7f6ee;
    --fail: #c02e2e;
    --fail-bg: #fdecec;
    --warn: #a66200;
    --warn-bg: #fef4e2;
    --info: #4b5563;
    --info-bg: #eef0f3;
    --review: #2e59a8;
    --review-bg: #eaf1fc;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.6 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  }}
  header {{
    position: sticky;
    top: 0;
    z-index: 30;
    background: rgba(246, 244, 239, 0.94);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--line);
  }}
  .wrap {{
    max-width: 1360px;
    margin: 0 auto;
    padding: 0 20px;
  }}
  .head-row {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 0 10px;
  }}
  h1 {{
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 0.01em;
  }}
  .stats {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    background: var(--card);
    border: 1px solid var(--line);
    font-size: 12px;
    color: var(--muted);
  }}
  .pill strong {{ color: var(--ink); font-weight: 600; }}
  nav.tabs {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 0 0 10px;
  }}
  nav.tabs button {{
    border: 1px solid transparent;
    background: transparent;
    color: var(--muted);
    padding: 7px 14px;
    border-radius: 8px;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }}
  nav.tabs button:hover {{ background: rgba(27, 31, 36, 0.05); color: var(--ink); }}
  nav.tabs button.active {{
    background: var(--ink);
    color: #fff;
  }}
  nav.tabs button:disabled {{
    opacity: 0.42;
    cursor: not-allowed;
  }}
  main {{
    padding: 20px 0 36px;
  }}
  .banner {{
    margin-bottom: 16px;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid #ecc98e;
    background: var(--warn-bg);
    color: #6b4000;
    font-size: 13px;
    display: none;
  }}
  .banner code {{
    background: rgba(0, 0, 0, 0.06);
    padding: 1px 5px;
    border-radius: 4px;
  }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: box-shadow 0.15s, transform 0.15s;
  }}
  .card:hover {{
    box-shadow: 0 8px 22px rgba(27, 31, 36, 0.08);
    transform: translateY(-1px);
  }}
  .thumb {{
    aspect-ratio: 16 / 11;
    background: #efede8;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: zoom-in;
    border-bottom: 1px solid var(--line);
  }}
  .thumb img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    background: #fff;
  }}
  .thumb.cover img {{ object-fit: cover; background: #1c1f24; }}
  .card-body {{
    padding: 11px 13px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .card-meta {{ min-width: 0; }}
  .card-id {{
    font-size: 12px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 0.02em;
  }}
  .card-title {{
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .card-sub {{ font-size: 12px; color: var(--muted); }}
  .btn {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 5px 10px;
    border-radius: 6px;
    border: 1px solid var(--line);
    background: var(--bg);
    color: var(--ink);
    text-decoration: none;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    cursor: pointer;
  }}
  .btn:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
  .tour-head {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 12px;
  }}
  .tour-frame {{
    width: 100%;
    height: min(76vh, 720px);
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #14171c;
    display: block;
  }}
  .section-card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 18px;
    margin-bottom: 16px;
  }}
  .section-card h2 {{
    margin: 0 0 12px;
    font-size: 16px;
  }}
  .report-row {{
    padding: 12px 0;
    border-top: 1px solid var(--line);
  }}
  .report-row:first-of-type {{ border-top: none; padding-top: 0; }}
  .report-top {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
  }}
  .verdicts {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .v-chip {{
    padding: 3px 9px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
  }}
  .v-fail {{ background: var(--fail-bg); color: var(--fail); }}
  .v-warn {{ background: var(--warn-bg); color: var(--warn); }}
  .v-info {{ background: var(--info-bg); color: var(--info); }}
  .v-review {{ background: var(--review-bg); color: var(--review); }}
  .v-ok {{ background: var(--ok-bg); color: var(--ok); }}
  .v-na {{ background: #f2f2f0; color: #7a808a; }}
  .file-list {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 10px;
  }}
  .file-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--bg);
    text-decoration: none;
    color: var(--ink);
  }}
  .file-item:hover {{ border-color: var(--accent); background: var(--accent-soft); }}
  .file-name {{ font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .file-size {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}
  .empty {{
    padding: 32px;
    text-align: center;
    color: var(--muted);
    background: var(--card);
    border: 1px dashed var(--line);
    border-radius: 10px;
  }}
  footer {{
    border-top: 1px solid var(--line);
    padding: 18px 0 32px;
    color: var(--muted);
    font-size: 12px;
  }}
  footer ol {{
    margin: 8px 0 0;
    padding-left: 18px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 4px 20px;
  }}
  .lightbox {{
    position: fixed;
    inset: 0;
    z-index: 100;
    background: rgba(15, 18, 22, 0.92);
    display: none;
    flex-direction: column;
  }}
  .lightbox.open {{ display: flex; }}
  .lb-bar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 16px;
    background: rgba(25, 29, 36, 0.95);
    color: #fff;
    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  }}
  .lb-title {{ font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .lb-actions {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
  .lb-btn {{
    border: 1px solid rgba(255, 255, 255, 0.24);
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
    padding: 5px 10px;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    text-decoration: none;
  }}
  .lb-btn:hover {{ background: rgba(255, 255, 255, 0.2); }}
  .lb-stage {{
    flex: 1;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: grab;
    user-select: none;
  }}
  .lb-stage.dragging {{ cursor: grabbing; }}
  .lb-stage img {{
    max-width: 94vw;
    max-height: 86vh;
    background: #fff;
    border-radius: 4px;
    transform-origin: center center;
    transition: transform 0.06s linear;
    pointer-events: none;
  }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="head-row">
      <h1 id="page-title">{title_esc}</h1>
      <div class="stats" id="stats"></div>
    </div>
    <nav class="tabs" id="tabs">
      <button type="button" data-tab="sheets">全套图纸</button>
      <button type="button" data-tab="tour">360° 全景漫游</button>
      <button type="button" data-tab="corners">角落效果图</button>
      <button type="button" data-tab="files">自查与交付文件</button>
    </nav>
  </div>
</header>

<main class="wrap">
  <div class="banner" id="proto-banner"></div>
  <section class="panel" id="panel-sheets"></section>
  <section class="panel" id="panel-tour"></section>
  <section class="panel" id="panel-corners"></section>
  <section class="panel" id="panel-files"></section>
</main>

<footer>
  <div class="wrap">
    <strong>方案深度交付提示</strong>：本工具链输出仅作方案深度参考，不能直接用于施工或报审；开工前以下六项须由专业人员按正式图或现场复核：
    <ol id="review-list"></ol>
  </div>
</footer>

<div class="lightbox" id="lightbox" role="dialog" aria-modal="true">
  <div class="lb-bar">
    <div class="lb-title" id="lb-title"></div>
    <div class="lb-actions">
      <button type="button" class="lb-btn" id="lb-prev">← 上一张</button>
      <button type="button" class="lb-btn" id="lb-next">下一张 →</button>
      <button type="button" class="lb-btn" id="lb-out">缩小</button>
      <button type="button" class="lb-btn" id="lb-reset">适应</button>
      <button type="button" class="lb-btn" id="lb-in">放大</button>
      <a class="lb-btn" id="lb-pdf" target="_blank" rel="noopener">打开 PDF</a>
      <button type="button" class="lb-btn" id="lb-close">关闭 (Esc)</button>
    </div>
  </div>
  <div class="lb-stage" id="lb-stage"><img id="lb-img" alt=""></div>
</div>

<script id="data" type="application/json">{payload_json}</script>
<script>
(function() {{
  var DATA = JSON.parse(document.getElementById("data").textContent);
  var VERDICT_CLASS = {{
    "不通过": "v-fail",
    "待确认": "v-warn",
    "信息不足": "v-info",
    "人工复核": "v-review",
    "通过": "v-ok",
    "不涉及": "v-na"
  }};

  function el(tag, cls, text) {{
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }}

  // 顶部概览胶囊
  var statsEl = document.getElementById("stats");
  function addPill(label, val) {{
    var p = el("span", "pill");
    p.appendChild(document.createTextNode(label + " "));
    p.appendChild(el("strong", "", val));
    statsEl.appendChild(p);
  }}
  if (DATA.counts.sheets) addPill("图纸", DATA.counts.sheets + " 张");
  if (DATA.counts.corners) addPill("效果图", DATA.counts.corners + " 张");
  if (DATA.counts.panos) addPill("全景站", DATA.counts.panos + " 站 / " + DATA.counts.hotspots + " 热点");
  if (DATA.counts.reports) addPill("规范报告", DATA.counts.reports + " 份");
  if (DATA.counts.schedules) addPill("明细表", DATA.counts.schedules + " 份");

  // 协议或跨盘提醒
  var banner = document.getElementById("proto-banner");
  if (location.protocol === "file:" && DATA.render && DATA.render.tour) {{
    banner.style.display = "block";
    banner.appendChild(document.createTextNode("当前通过 file:// 直接打开，浏览器会拦截全景漫游读取 tour.json；请运行 "));
    banner.appendChild(el("code", "", "python scripts/viewer.py serve --sheets <施工图目录> --render <渲染目录>"));
    banner.appendChild(document.createTextNode(" 后在浏览器中查看完整 360° 漫游。"));
  }} else if (DATA.cross_drive_missing) {{
    banner.style.display = "block";
    banner.textContent = "部分交付目录位于不同盘符，静态相对路径无法直连；请使用 scripts/viewer.py serve 挂载预览。";
  }}

  // 灯箱状态
  var lbItems = [], lbIdx = 0, scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0;
  var lb = document.getElementById("lightbox");
  var lbImg = document.getElementById("lb-img");
  var lbTitle = document.getElementById("lb-title");
  var lbPdf = document.getElementById("lb-pdf");
  var lbStage = document.getElementById("lb-stage");

  function applyTransform() {{
    lbImg.style.transform = "translate(" + tx + "px, " + ty + "px) scale(" + scale + ")";
  }}
  function resetZoom() {{ scale = 1; tx = 0; ty = 0; applyTransform(); }}
  function showLb(idx) {{
    if (!lbItems.length) return;
    lbIdx = (idx + lbItems.length) % lbItems.length;
    var item = lbItems[lbIdx];
    lbImg.src = item.src;
    lbImg.alt = item.title;
    lbTitle.textContent = (lbIdx + 1) + "/" + lbItems.length + " · " + item.title;
    if (item.pdf) {{
      lbPdf.href = item.pdf;
      lbPdf.style.display = "";
    }} else {{
      lbPdf.style.display = "none";
    }}
    resetZoom();
    lb.classList.add("open");
  }}
  function closeLb() {{ lb.classList.remove("open"); }}
  document.getElementById("lb-prev").onclick = function() {{ showLb(lbIdx - 1); }};
  document.getElementById("lb-next").onclick = function() {{ showLb(lbIdx + 1); }};
  document.getElementById("lb-in").onclick = function() {{ scale = Math.min(5, scale * 1.3); applyTransform(); }};
  document.getElementById("lb-out").onclick = function() {{ scale = Math.max(0.5, scale / 1.3); applyTransform(); }};
  document.getElementById("lb-reset").onclick = resetZoom;
  document.getElementById("lb-close").onclick = closeLb;
  lbStage.addEventListener("wheel", function(e) {{
    e.preventDefault();
    scale = Math.min(5, Math.max(0.5, scale * (e.deltaY < 0 ? 1.15 : 0.87)));
    applyTransform();
  }}, {{ passive: false }});
  lbStage.addEventListener("mousedown", function(e) {{
    dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
    lbStage.classList.add("dragging");
  }});
  window.addEventListener("mousemove", function(e) {{
    if (!dragging) return;
    tx = e.clientX - sx; ty = e.clientY - sy; applyTransform();
  }});
  window.addEventListener("mouseup", function() {{
    dragging = false; lbStage.classList.remove("dragging");
  }});
  document.addEventListener("keydown", function(e) {{
    if (!lb.classList.contains("open")) return;
    if (e.key === "Escape") closeLb();
    else if (e.key === "ArrowLeft") showLb(lbIdx - 1);
    else if (e.key === "ArrowRight") showLb(lbIdx + 1);
  }});

  // 1. 全套图纸
  var sheetPanel = document.getElementById("panel-sheets");
  var sheets = (DATA.sheets && DATA.sheets.sheets) || [];
  if (sheets.length) {{
    var grid = el("div", "grid");
    var sheetGallery = sheets.filter(function(s) {{ return !!s.png_url; }}).map(function(s) {{
      return {{ src: s.png_url, title: s.id + " " + s.title + (s.scale ? " (" + s.scale + ")" : ""), pdf: s.pdf_url }};
    }});
    sheets.forEach(function(s) {{
      var card = el("div", "card");
      if (s.png_url) {{
        var th = el("div", "thumb");
        var img = el("img");
        img.src = s.png_url;
        img.alt = s.id + " " + s.title;
        img.loading = "lazy";
        th.appendChild(img);
        var gIdx = sheetGallery.findIndex(function(g) {{ return g.src === s.png_url; }});
        th.onclick = function() {{ lbItems = sheetGallery; showLb(gIdx); }};
        card.appendChild(th);
      }}
      var body = el("div", "card-body");
      var meta = el("div", "card-meta");
      var showScale = s.scale && s.scale !== "—" && s.scale !== "-" && s.scale !== "－";
      meta.appendChild(el("div", "card-id", s.id + (showScale ? " · " + s.scale : "")));
      meta.appendChild(el("div", "card-title", s.title));
      body.appendChild(meta);
      if (s.pdf_url) {{
        var a = el("a", "btn", "PDF");
        a.href = s.pdf_url;
        a.target = "_blank";
        a.rel = "noopener";
        body.appendChild(a);
      }}
      card.appendChild(body);
      grid.appendChild(card);
    }});
    sheetPanel.appendChild(grid);
  }} else {{
    sheetPanel.appendChild(el("div", "empty", "未关联施工图目录，或目录中暂无可预览图纸。"));
  }}

  // 2. 360° 全景漫游
  var tourPanel = document.getElementById("panel-tour");
  var tour = DATA.render && DATA.render.tour;
  if (tour && tour.html_url) {{
    var thd = el("div", "tour-head");
    var left = el("div");
    left.appendChild(el("strong", "", tour.title + " "));
    left.appendChild(el("span", "card-sub", tour.scenes + " 个站点 · " + tour.hotspots + " 个切换热点" + (tour.offline_pannellum ? " · 离线 Pannellum" : " · CDN")));
    thd.appendChild(left);
    var openBtn = el("a", "btn", "新窗口全屏打开");
    openBtn.href = tour.html_url;
    openBtn.target = "_blank";
    openBtn.rel = "noopener";
    thd.appendChild(openBtn);
    tourPanel.appendChild(thd);

    var iframe = el("iframe", "tour-frame");
    iframe.setAttribute("data-src", tour.html_url);
    iframe.setAttribute("allowfullscreen", "true");
    iframe.title = tour.title;
    tourPanel.appendChild(iframe);
  }} else {{
    tourPanel.appendChild(el("div", "empty", "未关联全景漫游输出（tour/index.html）。"));
  }}

  // 3. 角落效果图
  var cornerPanel = document.getElementById("panel-corners");
  var corners = (DATA.render && DATA.render.corners) || [];
  if (corners.length) {{
    var cgrid = el("div", "grid");
    var cornerGallery = corners.filter(function(c) {{ return !!c.url; }}).map(function(c) {{
      return {{ src: c.url, title: c.title, pdf: null }};
    }});
    corners.forEach(function(c) {{
      var card = el("div", "card");
      if (c.url) {{
        var th = el("div", "thumb cover");
        var img = el("img");
        img.src = c.url;
        img.alt = c.title;
        img.loading = "lazy";
        th.appendChild(img);
        var gIdx = cornerGallery.findIndex(function(g) {{ return g.src === c.url; }});
        th.onclick = function() {{ lbItems = cornerGallery; showLb(gIdx); }};
        card.appendChild(th);
      }}
      var body = el("div", "card-body");
      var meta = el("div", "card-meta");
      meta.appendChild(el("div", "card-title", c.title));
      meta.appendChild(el("div", "card-sub", c.id));
      body.appendChild(meta);
      if (c.url) {{
        var a = el("a", "btn", "原图");
        a.href = c.url;
        a.target = "_blank";
        a.rel = "noopener";
        body.appendChild(a);
      }}
      card.appendChild(body);
      cgrid.appendChild(card);
    }});
    cornerPanel.appendChild(cgrid);
  }} else {{
    cornerPanel.appendChild(el("div", "empty", "未关联角落透视效果图（corners/*.png）。"));
  }}

  // 4. 自查与交付文件
  var filesPanel = document.getElementById("panel-files");
  var hasFiles = false;
  var reports = (DATA.sheets && DATA.sheets.reports) || [];
  if (reports.length) {{
    hasFiles = true;
    var sec = el("div", "section-card");
    sec.appendChild(el("h2", "", "规范自查报告（六种结论原样透传）"));
    reports.forEach(function(rep) {{
      var row = el("div", "report-row");
      var top = el("div", "report-top");
      top.appendChild(el("strong", "", rep.name + "（共 " + rep.total + " 条）"));
      var links = el("div", "stats");
      if (rep.html_url) {{
        var ha = el("a", "btn", "HTML 报告"); ha.href = rep.html_url; ha.target = "_blank"; ha.rel = "noopener"; links.appendChild(ha);
      }}
      if (rep.pdf_url) {{
        var pa = el("a", "btn", "PDF 报告"); pa.href = rep.pdf_url; pa.target = "_blank"; pa.rel = "noopener"; links.appendChild(pa);
      }}
      if (rep.json_url) {{
        var ja = el("a", "btn", "JSON"); ja.href = rep.json_url; ja.target = "_blank"; ja.rel = "noopener"; links.appendChild(ja);
      }}
      top.appendChild(links);
      row.appendChild(top);
      var vs = el("div", "verdicts");
      DATA.verdict_order.forEach(function(k) {{
        vs.appendChild(el("span", "v-chip " + (VERDICT_CLASS[k] || "v-na"), k + "：" + (rep.counts[k] || 0)));
      }});
      row.appendChild(vs);
      sec.appendChild(row);
    }});
    filesPanel.appendChild(sec);
  }}
  var reno = DATA.sheets && DATA.sheets.renovation;
  if (reno) {{
    hasFiles = true;
    var rsec = el("div", "section-card");
    rsec.appendChild(el("h2", "", "拆改清单"));
    var rlinks = el("div", "stats");
    if (reno.html_url) {{
      var rh = el("a", "btn", "打开 HTML 清单"); rh.href = reno.html_url; rh.target = "_blank"; rh.rel = "noopener"; rlinks.appendChild(rh);
    }}
    if (reno.pdf_url) {{
      var rp = el("a", "btn", "打开 PDF 清单"); rp.href = reno.pdf_url; rp.target = "_blank"; rp.rel = "noopener"; rlinks.appendChild(rp);
    }}
    rsec.appendChild(rlinks);
    filesPanel.appendChild(rsec);
  }}
  var schedules = ((DATA.sheets && DATA.sheets.schedules) || []).filter(function(sc) {{ return !!sc.url; }});
  var ifc = DATA.sheets && DATA.sheets.ifc;
  if (schedules.length || (ifc && ifc.url)) {{
    hasFiles = true;
    var fsec = el("div", "section-card");
    fsec.appendChild(el("h2", "", "交付表格与 IFC4 唯一真源模型"));
    var flist = el("div", "file-list");
    if (ifc && ifc.url) {{
      var ia = el("a", "file-item"); ia.href = ifc.url; ia.download = ifc.name;
      ia.appendChild(el("span", "file-name", ifc.name + "（IFC4 真源）"));
      ia.appendChild(el("span", "file-size", ifc.size_text));
      flist.appendChild(ia);
    }}
    schedules.forEach(function(sc) {{
      var sa = el("a", "file-item"); sa.href = sc.url; sa.download = sc.name;
      sa.appendChild(el("span", "file-name", sc.name));
      sa.appendChild(el("span", "file-size", sc.size_text));
      flist.appendChild(sa);
    }});
    fsec.appendChild(flist);
    filesPanel.appendChild(fsec);
  }}
  if (!hasFiles) {{
    filesPanel.appendChild(el("div", "empty", "暂无规范自查报告、表格或 IFC 文件。"));
  }}

  // 页脚复核项
  var rlist = document.getElementById("review-list");
  DATA.review_items.forEach(function(txt) {{ rlist.appendChild(el("li", "", txt)); }});

  // 标签切换（懒加载全景 iframe）
  var availability = {{
    sheets: sheets.length > 0,
    tour: !!(tour && tour.html_url),
    corners: corners.length > 0,
    files: hasFiles
  }};
  var buttons = Array.prototype.slice.call(document.querySelectorAll("nav.tabs button"));
  function activate(tab) {{
    buttons.forEach(function(b) {{ b.classList.toggle("active", b.dataset.tab === tab); }});
    ["sheets", "tour", "corners", "files"].forEach(function(id) {{
      document.getElementById("panel-" + id).classList.toggle("active", id === tab);
    }});
    if (tab === "tour") {{
      var fr = tourPanel.querySelector("iframe[data-src]");
      if (fr && !fr.getAttribute("src")) {{
        fr.setAttribute("src", fr.getAttribute("data-src"));
      }}
    }}
  }}
  buttons.forEach(function(b) {{
    if (!availability[b.dataset.tab]) b.disabled = true;
    b.onclick = function() {{ if (!b.disabled) activate(b.dataset.tab); }};
  }});
  var firstTab = ["sheets", "tour", "corners", "files"].find(function(k) {{ return availability[k]; }}) || "sheets";
  activate(firstTab);
}})();
</script>
</body>
</html>
"""


def build_index(
    out_dir: str | Path,
    *,
    sheets_dir: str | Path | None = None,
    render_dir: str | Path | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """生成静态全案预览页（index.html + project-index.json）。"""
    out_p = Path(out_dir).resolve()
    s_p = Path(sheets_dir).resolve() if sheets_dir is not None else None
    r_p = Path(render_dir).resolve() if render_dir is not None else None
    data = collect_index(sheets_dir=s_p, render_dir=r_p, out_dir=out_p, title=title)
    out_p.mkdir(parents=True, exist_ok=True)
    index_json = out_p / "project-index.json"
    index_html = out_p / "index.html"
    index_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    index_html.write_text(render_html(data), encoding="utf-8")
    return {
        "output": str(out_p),
        "index_html": str(index_html),
        "index_json": str(index_json),
        "title": data["title"],
        "counts": data["counts"],
        "verdict_totals": data["verdict_totals"],
        "cross_drive_missing": data["cross_drive_missing"],
        "reports": [
            {"name": r["name"], "counts": r["counts"], "total": r["total"]}
            for r in ((data.get("sheets") or {}).get("reports") or [])
        ],
    }


def _safe_resolve(root_resolved: Path, raw_rel: str) -> tuple[int, Path | None]:
    """严格路径沙箱：仅允许访问 root_resolved 内部的常规非隐藏文件。返回 (status_code, path)。"""
    decoded = urllib.parse.unquote(raw_rel)
    if "\x00" in decoded or "\\" in decoded or ":" in decoded:
        return 403, None
    stripped = decoded.lstrip("/")
    if not stripped:
        return 403, None
    parts = stripped.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return 403, None
    candidate = root_resolved.joinpath(*parts)
    try:
        target = candidate.resolve(strict=False)
    except OSError:
        return 403, None
    if not target.is_relative_to(root_resolved):
        return 403, None
    if any(p.startswith(".") for p in target.relative_to(root_resolved).parts):
        return 403, None
    if not target.exists():
        return 404, None
    if not target.is_file():
        return 403, None
    return 200, target


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_EXTRA:
        return _MIME_EXTRA[ext]
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    *,
    sheets_dir: str | Path | None = None,
    render_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    title: str | None = None,
) -> _ThreadingHTTPServer:
    """构建只读本地预览 HTTP 服务器（不向源目录写入任何文件）。"""
    s_p = Path(sheets_dir).resolve() if sheets_dir is not None else None
    r_p = Path(render_dir).resolve() if render_dir is not None else None
    data = collect_index(
        sheets_dir=s_p,
        render_dir=r_p,
        sheets_prefix="/sheets" if s_p is not None else None,
        render_prefix="/render" if r_p is not None else None,
        title=title,
    )
    html_bytes = render_html(data).encode("utf-8")
    json_bytes = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")

    mounts: dict[str, Path] = {}
    if s_p is not None:
        mounts["/sheets/"] = s_p
    if r_p is not None:
        mounts["/render/"] = r_p

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_bytes(self, body: bytes, content_type: str, head_only: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _send_file(self, target: Path, content_type: str, head_only: bool) -> None:
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not head_only:
                try:
                    with target.open("rb") as fh:
                        shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
                except (BrokenPipeError, ConnectionResetError):
                    return

        def _handle(self, head_only: bool) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            raw_path = parsed.path or "/"
            if raw_path in ("/", "/index.html"):
                self._send_bytes(html_bytes, "text/html; charset=utf-8", head_only)
                return
            if raw_path == "/project-index.json":
                self._send_bytes(json_bytes, "application/json; charset=utf-8", head_only)
                return
            for prefix, root_dir in mounts.items():
                if raw_path.startswith(prefix):
                    code, target = _safe_resolve(root_dir, raw_path[len(prefix) :])
                    if code != 200 or target is None:
                        self.send_error(code)
                        return
                    try:
                        self._send_file(target, _guess_mime(target), head_only)
                    except OSError:
                        self.send_error(404)
                    return
            self.send_error(404)

        def do_GET(self) -> None:
            self._handle(head_only=False)

        def do_HEAD(self) -> None:
            self._handle(head_only=True)

        def do_POST(self) -> None:
            self.send_error(405)

        def do_PUT(self) -> None:
            self.send_error(405)

        def do_DELETE(self) -> None:
            self.send_error(405)

    return _ThreadingHTTPServer((host, port), Handler)


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def serve(
    *,
    sheets_dir: str | Path | None = None,
    render_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    title: str | None = None,
    open_browser: bool = False,
) -> None:
    """启动只读本地预览服务器并阻塞至 Ctrl+C。"""
    server = make_server(sheets_dir=sheets_dir, render_dir=render_dir, host=host, port=port, title=title)
    actual_port = server.server_address[1]
    show_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{show_host}:{actual_port}/"
    print(f"JZLOG serving {url}", flush=True)
    if host in ("0.0.0.0", "::", ""):
        lan = _lan_ip()
        if lan:
            print(f"JZLOG lan http://{lan}:{actual_port}/", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
