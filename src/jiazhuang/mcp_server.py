"""cn-bim-toolkit 的 MCP stdio 服务（纯标准库，JSON-RPC 2.0，按行分隔 JSON）。

一个脚本对应一个工具，参数与脚本 argparse 同名同义；工具只返回落盘路径、计数、报告里的原始错误/违规和
原样透传的六种规范自查结论，不返回说明句。所有运行期日志写 stderr，stdout 只输出 JSON-RPC 报文。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import conventions, printing

SERVER_NAME = "cn-bim-toolkit"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-03-26"

ROOT = Path(__file__).resolve().parents[2]
PACKS_DIR = ROOT / "packs" / "cn"
STATUSES = ("不通过", "待确认", "信息不足", "人工复核", "通过", "不涉及")

PY_TIMEOUT = int(os.environ.get("JZ_MCP_TIMEOUT", "120"))
BLENDER_TIMEOUT = int(os.environ.get("JZ_MCP_BLENDER_TIMEOUT", "600"))


class ToolParamError(ValueError):
    """工具参数不合法（对应 JSON-RPC -32602）。"""


# ------------------------------------------------------------------ 环境与子进程

def find_blender() -> str | None:
    """找 Blender 可执行文件：JZ_BLENDER → PATH → 各平台常见安装路径。找不到返回 None。"""
    env = os.environ.get("JZ_BLENDER")
    if env:
        return env if os.path.isfile(env) else None
    found = shutil.which("blender")
    if found:
        return found
    patterns: list[str] = []
    if sys.platform == "win32":
        for key, dflt in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            base = os.environ.get(key, dflt)
            patterns.append(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
    elif sys.platform == "darwin":
        patterns.append("/Applications/Blender.app/Contents/MacOS/Blender")
    else:
        patterns.extend(["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"])
    hits = sorted((p for pat in patterns for p in glob.glob(pat) if os.path.isfile(p)), reverse=True)
    return hits[0] if hits else None


def _blender_user_resources() -> str | None:
    if os.environ.get("BLENDER_USER_RESOURCES"):
        return os.environ["BLENDER_USER_RESOURCES"]
    prof = ROOT / "blender-profile"
    return str(prof) if prof.is_dir() else None


def _sub_env(for_blender: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    src = str(ROOT / "src")
    cur_pp = env.get("PYTHONPATH", "")
    parts = [p for p in cur_pp.split(os.pathsep) if p] if cur_pp else []
    if src not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src, *parts])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if for_blender:
        bur = _blender_user_resources()
        if bur:
            env["BLENDER_USER_RESOURCES"] = bur
    return env


def _last_jzlog(text: str) -> str:
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s.startswith("JZLOG"):
            return s
    return ""


def _first_error_line(stderr: str, stdout: str) -> str:
    streams = [
        [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith("Blender ")]
        for s in (stderr, stdout)
    ]
    for lines in streams:
        for ln in reversed(lines):
            if ln.startswith("JZLOG failed") or "error:" in ln.lower() or "缺少" in ln or "不存在" in ln:
                return ln
    for lines in streams:
        if lines:
            return lines[-1]
    return ""


def _run_cmd(cmd: list[str], *, for_blender: bool = False, timeout: int | None = None) -> tuple[int, str, str, str]:
    """运行子进程，返回 (returncode, stdout, stderr, last_jzlog)。不经过 shell。"""
    t = timeout or (BLENDER_TIMEOUT if for_blender else PY_TIMEOUT)
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), env=_sub_env(for_blender=for_blender),
            capture_output=True, timeout=t, shell=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {t}s", ""
    except OSError as e:
        return 2, "", f"{type(e).__name__}: {e}", ""
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    jzlog = _last_jzlog(out) or _last_jzlog(err)
    return proc.returncode, out, err, jzlog


def _resolve_path(p: str) -> Path:
    path = Path(p)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _read_json_if_exists(path: Path) -> Any:
    if not path.is_file():
        return None
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _csv(val: Any) -> str:
    if isinstance(val, list):
        return ",".join(str(x).strip() for x in val if str(x).strip())
    return str(val).strip()


# ------------------------------------------------------------------ 校验入参

def _check_args(args: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolParamError("arguments must be an object")
    props = schema.get("properties", {})
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolParamError(f"unknown argument(s): {', '.join(unknown)}")
    for req in schema.get("required", []):
        if req not in args or args[req] is None:
            raise ToolParamError(f"missing required argument: {req}")
    for k, v in args.items():
        if v is None:
            continue
        spec = props[k]
        t = spec.get("type")
        if t == "string" and not isinstance(v, str):
            raise ToolParamError(f"{k} must be a string")
        elif t == "boolean" and not isinstance(v, bool):
            raise ToolParamError(f"{k} must be a boolean")
        elif t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            raise ToolParamError(f"{k} must be an integer")
        elif t == "array":
            if not isinstance(v, list):
                raise ToolParamError(f"{k} must be an array")
            if "minItems" in spec and len(v) < spec["minItems"]:
                raise ToolParamError(f"{k} requires at least {spec['minItems']} items")
            if "maxItems" in spec and len(v) > spec["maxItems"]:
                raise ToolParamError(f"{k} accepts at most {spec['maxItems']} items")
            item_t = spec.get("items", {}).get("type")
            if item_t == "number" and not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
                raise ToolParamError(f"{k} items must be numbers")
            if item_t == "string" and not all(isinstance(x, str) for x in v):
                raise ToolParamError(f"{k} items must be strings")
            item_enum = spec.get("items", {}).get("enum")
            if item_enum is not None:
                bad = [x for x in v if x not in item_enum]
                if bad:
                    raise ToolParamError(f"{k} items must be in {item_enum}, got {bad}")
        if "enum" in spec and v not in spec["enum"]:
            raise ToolParamError(f"{k} must be one of {spec['enum']}")
    return args


# ------------------------------------------------------------------ 约定包资源与发现

def _pack_resource_paths() -> list[Path]:
    out: list[Path] = []
    if not PACKS_DIR.is_dir():
        return out
    for p in sorted(PACKS_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".json", ".md"):
            out.append(p)
    return out


def list_resources() -> list[dict[str, Any]]:
    res = []
    for p in _pack_resource_paths():
        rel = p.relative_to(PACKS_DIR).as_posix()
        mime = "application/json" if p.suffix.lower() == ".json" else "text/markdown"
        res.append({
            "uri": f"cn-bim://pack/{rel}",
            "name": f"packs/cn/{rel}",
            "mimeType": mime,
        })
    return res


def read_resource(uri: str) -> dict[str, Any]:
    prefix = "cn-bim://pack/"
    if not isinstance(uri, str) or not uri.startswith(prefix):
        raise ToolParamError(f"unsupported resource URI: {uri}")
    rel = uri[len(prefix):]
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        raise ToolParamError(f"invalid resource path: {uri}")
    base = PACKS_DIR.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise ToolParamError(f"resource outside pack directory: {uri}") from e
    if not target.is_file() or target.suffix.lower() not in (".json", ".md"):
        raise ToolParamError(f"resource not found: {uri}")
    mime = "application/json" if target.suffix.lower() == ".json" else "text/markdown"
    return {
        "contents": [{
            "uri": uri,
            "mimeType": mime,
            "text": target.read_text(encoding="utf-8-sig"),
        }]
    }


def _strip_meta(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ------------------------------------------------------------------ 工具实现

def _tool_check_env(_args: dict[str, Any]) -> dict[str, Any]:
    pkgs: dict[str, bool] = {}
    for mod in ("ezdxf", "shapely"):
        try:
            __import__(mod)
            pkgs[mod] = True
        except ImportError:
            pkgs[mod] = False
    pack_err = conventions.check_pack()
    blender = find_blender()
    edge = printing.EDGE
    edge_ok = bool(shutil.which(edge) or os.path.isfile(edge))
    bur = _blender_user_resources()
    return {
        "ok": all(pkgs.values()) and not pack_err,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "packages": pkgs,
        "drawing_pack": conventions.pack_dir(),
        "drawing_pack_ok": not pack_err,
        **({"drawing_pack_error": pack_err} if pack_err else {}),
        "blender": blender,
        "blender_ok": bool(blender),
        "blender_user_resources": bur,
        "edge": edge,
        "edge_ok": edge_ok,
    }


def _tool_list_packs(_args: dict[str, Any]) -> dict[str, Any]:
    furn = _read_json_if_exists(PACKS_DIR / "furniture" / "furniture.json") or {}
    rtypes = _read_json_if_exists(PACKS_DIR / "furniture" / "room_types.json") or {}
    ceil = _read_json_if_exists(PACKS_DIR / "ceiling" / "rules.json") or {}
    mep_pts = _read_json_if_exists(PACKS_DIR / "mep" / "points.json") or {}
    floor = _read_json_if_exists(PACKS_DIR / "flooring" / "rules.json") or {}
    wall = _read_json_if_exists(PACKS_DIR / "wall" / "rules.json") or {}
    rend = _read_json_if_exists(PACKS_DIR / "render" / "materials.json") or {}
    types = _strip_meta(furn.get("types", {}))
    cn_furn = {k: list(v.get("params", {}).keys()) for k, v in types.items() if k.startswith("cn_")}
    lib_furn = sorted(k for k in types if k.startswith("lib:"))
    rules_files = sorted(str(p.relative_to(ROOT).as_posix()) for p in (PACKS_DIR / "rules").glob("*.json")) if (PACKS_DIR / "rules").is_dir() else []
    intents = sorted(str(p.relative_to(ROOT).as_posix()) for p in PACKS_DIR.rglob("example*intent.json"))
    return {
        "ok": True,
        "packs_dir": str(PACKS_DIR),
        "room_types": list(_strip_meta(rtypes.get("types", {})).keys()),
        "furniture": {"cn": cn_furn, "lib": lib_furn},
        "ceiling": {
            "templates": list(_strip_meta(ceil.get("templates", {})).keys()),
            "materials": list(_strip_meta(ceil.get("materials", {})).keys()),
        },
        "mep": {"points": list(_strip_meta(mep_pts.get("points", {})).keys())},
        "flooring": {"materials": list(_strip_meta(floor.get("materials", {})).keys())},
        "wall": {"materials": list(_strip_meta(wall.get("materials", {})).keys())},
        "render": {"materials": list(_strip_meta(rend.get("materials", {})).keys())},
        "rules": rules_files,
        "example_intents": intents,
        "resources": [r["uri"] for r in list_resources()],
    }


def _tool_demo_apartment(args: dict[str, Any]) -> dict[str, Any]:
    out = args["out"]
    rc, stdout, stderr, jzlog = _run_cmd([sys.executable, "-m", "jiazhuang.demo_apartment", out])
    p = _resolve_path(out)
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc, "jzlog": jzlog, "output": str(p)}
    rec = _read_json_if_exists(p) if rc == 0 else None
    if isinstance(rec, dict):
        res["counts"] = {
            "rooms": len(rec.get("rooms", [])),
            "units": len(rec.get("units", [])),
            "walls": len(rec.get("walls", [])),
            "openings": len(rec.get("openings", [])),
        }
    elif rc != 0:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_dxfplan(args: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "jiazhuang.dxfplan", args["dxf"], args["out"]]
    if args.get("svg"):
        cmd.append(args["svg"])
    cmd.extend(["--region", *(str(x) for x in args["region"])])
    if args.get("verbose"):
        cmd.append("--verbose")
    rc, stdout, stderr, jzlog = _run_cmd(cmd)
    out_p = _resolve_path(args["out"])
    files: dict[str, str] = {}
    if rc == 0 and out_p.is_file():
        files["recognition"] = str(out_p)
    if rc == 0 and args.get("svg") and _resolve_path(args["svg"]).is_file():
        files["svg"] = str(_resolve_path(args["svg"]))
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": files}
    rec = _read_json_if_exists(out_p) if rc == 0 else None
    if isinstance(rec, dict):
        ops = rec.get("openings", [])
        res["counts"] = {
            "rooms": len(rec.get("rooms", [])),
            "units": len(rec.get("units", [])),
            "walls": len(rec.get("walls", [])),
            "doors": sum(1 for o in ops if o.get("kind") == "door"),
            "windows": sum(1 for o in ops if o.get("kind") == "window"),
        }
        if args.get("verbose"):
            stats_lines = [ln for ln in stdout.splitlines() if ln.strip() and not ln.startswith("JZLOG")]
            if stats_lines:
                res["stats"] = stats_lines
    elif rc != 0:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_furnish_context(args: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / "scripts" / "furnish.py"), "context", args["recognition"]]
    if args.get("scheme"):
        cmd.extend(["--scheme", args["scheme"]])
    if args.get("room"):
        cmd.extend(["--room", _csv(args["room"])])
    if args.get("out"):
        cmd.extend(["-o", args["out"]])
    rc, stdout, stderr, jzlog = _run_cmd(cmd)
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc}
    if jzlog:
        res["jzlog"] = jzlog
    if rc == 0:
        if args.get("out"):
            p = _resolve_path(args["out"])
            res["output"] = str(p)
            res["context"] = _read_json_if_exists(p)
        else:
            res["context"] = json.loads(stdout)
    else:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _stage_result(rc: int, stdout: str, stderr: str, jzlog: str, out_dir: str, count_keys: tuple[str, ...]) -> dict[str, Any]:
    out_p = _resolve_path(out_dir)
    files: dict[str, str] = {}
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": files}
    wrote_report = rc == 0 or (rc == 1 and jzlog.startswith("JZLOG failed"))
    if not wrote_report:
        res["error"] = _first_error_line(stderr, stdout)
        return res
    sch_p = out_p / "scheme.json"
    rep_p = out_p / "report.json"
    if sch_p.is_file():
        files["scheme"] = str(sch_p)
    if rep_p.is_file():
        files["report"] = str(rep_p)
    rep = _read_json_if_exists(rep_p)
    if isinstance(rep, dict):
        counts: dict[str, Any] = {}
        for k in count_keys:
            if k in rep:
                v = rep[k]
                counts[k] = len(v) if isinstance(v, list) else v
        counts["errors"] = len(rep.get("errors", []))
        counts["violations"] = len(rep.get("violations", []))
        res["counts"] = counts
        if rep.get("errors"):
            res["errors"] = rep["errors"]
        if rep.get("violations"):
            res["violations"] = rep["violations"]
        if rep.get("arranged"):
            res["arranged"] = rep["arranged"]
    if rc != 0 and not (res.get("errors") or res.get("violations")):
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_furnish_place(args: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / "scripts" / "furnish.py"), "place", args["recognition"], args["intent"], args["out"]]
    if args.get("scheme"):
        cmd.extend(["--scheme", args["scheme"]])
    rc, stdout, stderr, jzlog = _run_cmd(cmd)
    return _stage_result(rc, stdout, stderr, jzlog, args["out"], ("items", "arranged"))


def _run_plan_stage(script: str, args: dict[str, Any], count_keys: tuple[str, ...]) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / "scripts" / script), args["recognition"], args["scheme"], args["out"]]
    if args.get("intent"):
        cmd.extend(["--intent", args["intent"]])
    if args.get("unit"):
        cmd.extend(["--unit", _csv(args["unit"])])
    rc, stdout, stderr, jzlog = _run_cmd(cmd)
    return _stage_result(rc, stdout, stderr, jzlog, args["out"], count_keys)


def _tool_ceiling(args: dict[str, Any]) -> dict[str, Any]:
    return _run_plan_stage("ceiling.py", args, ("ceilings", "rooms"))


def _tool_mep(args: dict[str, Any]) -> dict[str, Any]:
    return _run_plan_stage("mep.py", args, ("points", "circuits", "skipped"))


def _tool_flooring(args: dict[str, Any]) -> dict[str, Any]:
    return _run_plan_stage("flooring.py", args, ("zones", "thresholds", "strips", "notes"))


def _tool_wall(args: dict[str, Any]) -> dict[str, Any]:
    return _run_plan_stage("wall.py", args, ("rooms", "walls", "open", "elevations", "notes"))


def _summarize_rule_report(data: dict[str, Any]) -> dict[str, Any]:
    results = data.get("results", [])
    status_counts = {st: 0 for st in STATUSES}
    for r in results:
        st = r.get("status")
        if st in status_counts:
            status_counts[st] += 1
    return {
        "facts": data.get("facts", {}),
        "status_counts": status_counts,
        "results": [
            {
                "id": r.get("id"),
                "clause": r.get("clause"),
                "summary": r.get("summary"),
                "status": r.get("status"),
                "items": r.get("items", []),
            }
            for r in results
        ],
    }


def _collect_draw_outputs(out_p: Path) -> tuple[dict[str, Any], dict[str, int], list[dict[str, Any]]]:
    files: dict[str, Any] = {}
    rule_reports: list[dict[str, Any]] = []
    if not out_p.is_dir():
        return files, {"pdfs": 0, "pngs": 0, "xlsx": 0, "reports": 0}, rule_reports
    model = out_p / "model.ifc"
    if model.is_file():
        files["model"] = str(model)
    pdfs = sorted(str(p) for p in out_p.glob("*.pdf"))
    pngs = sorted(str(p) for p in out_p.glob("*.png"))
    xlsx = sorted(str(p) for p in out_p.glob("*.xlsx"))
    reports = sorted(out_p.glob("*-report.json"))
    if pdfs:
        files["pdfs"] = pdfs
    if pngs:
        files["pngs"] = pngs
    if xlsx:
        files["xlsx"] = xlsx
    if reports:
        files["reports"] = [str(p) for p in reports]
        for rp in reports:
            d = _read_json_if_exists(rp)
            if isinstance(d, dict) and "results" in d:
                rule_reports.append({"file": str(rp), **_summarize_rule_report(d)})
    reno = out_p / "renovation-list.html"
    if reno.is_file():
        files["renovation_list"] = str(reno)
    counts = {"pdfs": len(pdfs), "pngs": len(pngs), "xlsx": len(xlsx), "reports": len(reports)}
    return files, counts, rule_reports


def _missing_blender(out: str) -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": 2,
        "output": str(_resolve_path(out)) if out else "",
        "error": "找不到 Blender：请设置环境变量 JZ_BLENDER 或把 blender 加入 PATH",
    }


def _blender_produced_outputs(rc: int, jzlog: str) -> bool:
    """Blender 脚本成功或仅在打印环节部分失败（JZLOG failed <张数> <目录>）时才扫描输出目录，避免超时或提前退出读到旧产物。"""
    return rc == 0 or (rc == 1 and bool(re.match(r"^JZLOG failed \d+\b", jzlog)))


def _tool_make_plan_set(args: dict[str, Any]) -> dict[str, Any]:
    blender = find_blender()
    if not blender:
        return _missing_blender(args["out"])
    cmd = [blender, "-b", "--python-exit-code", "1", "--python", str(ROOT / "scripts" / "make_plan_set.py"), "--",
           args["recognition"], args["out"]]
    str_opts = (
        ("scheme", "--scheme"),
        ("project", "--project"),
        ("short", "--short"),
        ("building", "--building"),
        ("storey_name", "--storey-name"),
        ("labels", "--labels"),
        ("title", "--title"),
        ("zone_title", "--zone-title"),
        ("prefix", "--prefix"),
        ("split", "--split"),
        ("unit", "--unit"),
    )
    for key, flag in str_opts:
        if args.get(key):
            cmd.extend([flag, str(args[key])])
    if args.get("rules"):
        cmd.extend(["--rules", _csv(args["rules"])])
    if args.get("kinds"):
        cmd.extend(["--kinds", _csv(args["kinds"])])
    if args.get("mep"):
        cmd.append("--mep")
    rc, stdout, stderr, jzlog = _run_cmd(cmd, for_blender=True)
    out_p = _resolve_path(args["out"])
    if not _blender_produced_outputs(rc, jzlog):
        return {"ok": False, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": {}, "error": _first_error_line(stderr, stdout)}
    files, counts, rule_reports = _collect_draw_outputs(out_p)
    res: dict[str, Any] = {
        "ok": rc == 0,
        "returncode": rc,
        "jzlog": jzlog,
        "output": str(out_p),
        "files": files,
        "counts": counts,
    }
    if rule_reports:
        res["rule_reports"] = rule_reports
    if rc != 0:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_check_ifc(args: dict[str, Any]) -> dict[str, Any]:
    blender = find_blender()
    if not blender:
        return _missing_blender(args["out"])
    cmd = [blender, "-b", "--python-exit-code", "1", "--python", str(ROOT / "scripts" / "check_ifc.py"), "--",
           args["ifc"], args["rules"], args["out"]]
    if args.get("project"):
        cmd.append(args["project"])
    rc, stdout, stderr, jzlog = _run_cmd(cmd, for_blender=True)
    out_p = _resolve_path(args["out"])
    if not _blender_produced_outputs(rc, jzlog):
        return {"ok": False, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": {}, "error": _first_error_line(stderr, stdout)}
    stem = Path(args["rules"]).stem + "-report"
    files = {}
    for ext in ("json", "html", "pdf"):
        fp = out_p / f"{stem}.{ext}"
        if fp.is_file():
            files[ext] = str(fp)
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": files}
    data = _read_json_if_exists(out_p / f"{stem}.json")
    if isinstance(data, dict) and "results" in data:
        res.update(_summarize_rule_report(data))
    if rc != 0:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_make_demo_set(args: dict[str, Any]) -> dict[str, Any]:
    blender = find_blender()
    out = args.get("out") or str(ROOT / "build" / "demo")
    if not blender:
        return _missing_blender(out)
    cmd = [blender, "-b", "--python-exit-code", "1", "--python", str(ROOT / "scripts" / "make_demo_set.py"), "--", out]
    rc, stdout, stderr, jzlog = _run_cmd(cmd, for_blender=True)
    out_p = _resolve_path(out)
    if not _blender_produced_outputs(rc, jzlog):
        return {"ok": False, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": {}, "error": _first_error_line(stderr, stdout)}
    files, counts, _ = _collect_draw_outputs(out_p)
    res: dict[str, Any] = {"ok": rc == 0, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": files, "counts": counts}
    if rc != 0:
        res["error"] = _first_error_line(stderr, stdout)
    return res


def _tool_build_viewer(args: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / "scripts" / "viewer.py"), "build", args["out"]]
    if "sheets" in args and args["sheets"] is not None:
        cmd.extend(["--sheets", str(args["sheets"])])
    if "render" in args and args["render"] is not None:
        cmd.extend(["--render", str(args["render"])])
    if args.get("title"):
        cmd.extend(["--title", str(args["title"])])
    rc, stdout, stderr, jzlog = _run_cmd(cmd)
    out_p = _resolve_path(args["out"])
    if rc != 0:
        return {"ok": False, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": {}, "error": _first_error_line(stderr, stdout)}
    idx_p = out_p / "project-index.json"
    html_p = out_p / "index.html"
    idx = _read_json_if_exists(idx_p) if idx_p.is_file() else None
    serve_parts = ["python", "scripts/viewer.py", "serve"]
    _meta = set('&()|;$"<>\x27')
    q = lambda s: f'"{s.replace(chr(34), chr(92) + chr(34))}"' if any(c.isspace() or c in _meta for c in s) else s
    if args.get("sheets"):
        serve_parts.extend(["--sheets", q(str(_resolve_path(args["sheets"])))])
    if args.get("render"):
        serve_parts.extend(["--render", q(str(_resolve_path(args["render"])))])
    if args.get("title"):
        serve_parts.extend(["--title", q(str(args["title"]))])
    reports = []
    if isinstance(idx, dict) and isinstance(idx.get("sheets"), dict):
        reports = [
            {"name": r.get("name"), "counts": r.get("counts"), "total": r.get("total")}
            for r in idx["sheets"].get("reports", [])
            if isinstance(r, dict)
        ]
    return {
        "ok": True,
        "returncode": rc,
        "jzlog": jzlog,
        "output": str(out_p),
        "files": {
            "index_html": str(html_p) if html_p.is_file() else None,
            "index_json": str(idx_p) if idx_p.is_file() else None,
        },
        "counts": idx.get("counts", {}) if isinstance(idx, dict) else {},
        "verdict_totals": idx.get("verdict_totals", {}) if isinstance(idx, dict) else {},
        "reports": reports,
        "serve_command": " ".join(serve_parts),
    }


def _tool_render(args: dict[str, Any]) -> dict[str, Any]:
    blender = find_blender()
    if not blender:
        return _missing_blender(args["out"])
    cmd = [blender, "-b", "--python-exit-code", "1", "--python", str(ROOT / "scripts" / "render.py"), "--",
           args["ifc"], args["out"]]
    for key, flag in (
        ("mode", "--mode"),
        ("rooms", "--rooms"),
        ("size", "--size"),
        ("pano_size", "--pano-size"),
        ("device", "--device"),
        ("assets", "--assets"),
        ("pannellum", "--pannellum"),
        ("project", "--project"),
    ):
        if args.get(key):
            cmd.extend([flag, str(args[key])])
    for key, flag in (("samples", "--samples"), ("pano_samples", "--pano-samples")):
        if args.get(key) is not None:
            cmd.extend([flag, str(args[key])])
    if args.get("save_blend"):
        cmd.append("--save-blend")
    rc, stdout, stderr, jzlog = _run_cmd(cmd, for_blender=True)
    out_p = _resolve_path(args["out"])
    if rc != 0:
        return {"ok": False, "returncode": rc, "jzlog": jzlog, "output": str(out_p), "files": {}, "error": _first_error_line(stderr, stdout)}
    man_p = out_p / "render-manifest.json"
    man = _read_json_if_exists(man_p) if man_p.is_file() else None
    files: dict[str, Any] = {}
    counts: dict[str, Any] = {"corners": 0, "panos": 0, "hotspots": 0}
    if isinstance(man, dict):
        files["manifest"] = str(man_p)
        if man.get("corners"):
            files["corners"] = man["corners"]
            counts["corners"] = len(man["corners"])
        if man.get("panos"):
            files["panos"] = man["panos"]
            counts["panos"] = len(man["panos"])
        if man.get("tour"):
            files["tour_html"] = man["tour"].get("index_html")
            files["tour_json"] = man["tour"].get("tour_json")
            counts["hotspots"] = man["tour"].get("hotspots", 0)
        if man.get("blend"):
            files["blend"] = man["blend"]
    return {
        "ok": True,
        "returncode": 0,
        "jzlog": jzlog,
        "output": str(out_p),
        "files": files,
        "counts": counts,
        **({"device": man.get("device"), "sky": man.get("sky")} if isinstance(man, dict) else {}),
    }


# ------------------------------------------------------------------ 工具目录（参数与脚本 argparse 同名同义）

def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_env",
        "description": "检查 Python、ezdxf/shapely、出图约定包、Blender（JZ_BLENDER / BLENDER_USER_RESOURCES）和 Edge 是否就绪。",
        "inputSchema": _schema({}),
    },
    {
        "name": "list_packs",
        "description": "列出 packs/cn 下可用的房间类型、家具类型、吊顶模板、水电点位、地墙材料、示例规则包、示例意图和 MCP 资源 URI。",
        "inputSchema": _schema({}),
    },
    {
        "name": "demo_apartment",
        "description": "把合成示例户型写成识别结果 JSON（dxfplan 同格式），供后续布置和出图调试使用。",
        "inputSchema": _schema({"out": {"type": "string", "description": "输出识别结果 JSON 路径"}}, ["out"]),
    },
    {
        "name": "dxfplan",
        "description": "从打散的天正 DXF 识别房间、户、墙、门窗和轴网，写出识别结果 JSON（可选识别检查图 SVG）。",
        "inputSchema": _schema({
            "dxf": {"type": "string", "description": "打散后的 DXF 文件路径"},
            "out": {"type": "string", "description": "输出识别结果 JSON 路径"},
            "svg": {"type": "string", "description": "可选：识别检查图 SVG 输出路径"},
            "region": {
                "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                "description": "这一层在图上的外包 [xmin, ymin, xmax, ymax]（图纸毫米）",
            },
            "verbose": {"type": "boolean", "description": "是否打印详细识别统计"},
        }, ["dxf", "out", "region"]),
    },
    {
        "name": "furnish_context",
        "description": "输出房间摘要 JSON（房间类型、面积、墙边 W1..Wn 和门窗位置），供编写家具/吊顶/墙面意图。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "scheme": {"type": "string", "description": "可选：已有方案 JSON 路径"},
            "room": {"type": "string", "description": "可选：只输出这些房间（房名或 id，逗号分隔）"},
            "out": {"type": "string", "description": "可选：写入文件路径（不给直接返回 JSON）"},
        }, ["recognition"]),
    },
    {
        "name": "furnish_place",
        "description": "按布置意图排家具并做几何校验，写出 scheme.json 与 report.json；灶台压窗时自动换摆法，有违规退出码 1。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "intent": {"type": "string", "description": "家具布置意图 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "scheme": {"type": "string", "description": "可选：在此方案上追加家具"},
        }, ["recognition", "intent", "out"]),
    },
    {
        "name": "ceiling",
        "description": "按吊顶规则表和可选意图生成吊顶方案并校验净高，写出 scheme.json 与 report.json。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "scheme": {"type": "string", "description": "带家具的方案 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "intent": {"type": "string", "description": "可选：吊顶意图 JSON 路径"},
            "unit": {"type": "string", "description": "可选：只重做这些户（逗号分隔）"},
        }, ["recognition", "scheme", "out"]),
    },
    {
        "name": "mep",
        "description": "按水电规则表和可选意图布插座、开关、灯具与给排水点位并分回路，写出 scheme.json 与 report.json。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "scheme": {"type": "string", "description": "带家具/吊顶的方案 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "intent": {"type": "string", "description": "可选：水电点位意图 JSON 路径"},
            "unit": {"type": "string", "description": "可选：只重布这些户（逗号分隔）"},
        }, ["recognition", "scheme", "out"]),
    },
    {
        "name": "flooring",
        "description": "按地面规则表和可选意图生成铺装区、排砖、门槛石与压条并校验，写出 scheme.json 与 report.json。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "scheme": {"type": "string", "description": "带家具/吊顶/水电的方案 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "intent": {"type": "string", "description": "可选：地面意图 JSON 路径"},
            "unit": {"type": "string", "description": "可选：只重做这些户（逗号分隔）"},
        }, ["recognition", "scheme", "out"]),
    },
    {
        "name": "wall",
        "description": "按墙面规则表和可选意图生成每面墙做法、墙砖排砖网格、立面筛选和立面字母，写出 scheme.json 与 report.json。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "scheme": {"type": "string", "description": "带家具/吊顶/水电/地面的方案 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "intent": {"type": "string", "description": "可选：墙面意图 JSON 路径"},
            "unit": {"type": "string", "description": "可选：只重做这些户（逗号分隔）"},
        }, ["recognition", "scheme", "out"]),
    },
    {
        "name": "make_plan_set",
        "description": "在 Blender/Bonsai 里建模 IFC4 并出全套 A3 中文施工图（plan/floor/ceiling/mep/elev）、表格与可选规范自查报告。",
        "inputSchema": _schema({
            "recognition": {"type": "string", "description": "识别结果 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "scheme": {"type": "string", "description": "可选：方案 JSON 路径"},
            "rules": {"type": "array", "items": {"type": "string"}, "description": "可选：规范规则包 JSON 路径列表"},
            "project": {"type": "string", "description": "工程名"},
            "short": {"type": "string", "description": "图框标题栏工程名"},
            "building": {"type": "string", "description": "楼栋名"},
            "storey_name": {"type": "string", "description": "层次名，如“二～十八层”"},
            "labels": {"type": "string", "description": "可选：房间简称表 JSON 路径"},
            "title": {"type": "string", "description": "总图名模板，可用 {storey}、{unit}、{kind}"},
            "zone_title": {"type": "string", "description": "分区/分段图名模板，可用 {storey}、{id}、{kind}"},
            "prefix": {"type": "string", "description": "图号前缀，缺省 A"},
            "mep": {"type": "boolean", "description": "等同在 kinds 中加入 mep"},
            "kinds": {
                "type": "array", "items": {"type": "string", "enum": ["plan", "floor", "ceiling", "mep", "elev"]},
                "description": "出图图种列表：plan、floor、ceiling、mep、elev",
            },
            "split": {"type": "string", "enum": ["auto", "none"], "description": "整层放不下时是否自动分段"},
            "unit": {"type": "string", "description": "可选：只出指定户号的整套图（如 U1）"},
        }, ["recognition", "out"]),
    },
    {
        "name": "check_ifc",
        "description": "在 Blender/Bonsai 里按规则包检查 IFC4 模型，出 JSON/HTML/PDF 报告并原样返回六种规范自查结论。",
        "inputSchema": _schema({
            "ifc": {"type": "string", "description": "IFC4 模型文件路径"},
            "rules": {"type": "string", "description": "规范规则包 JSON 路径"},
            "out": {"type": "string", "description": "输出目录"},
            "project": {"type": "string", "description": "可选：报告抬头工程名"},
        }, ["ifc", "rules", "out"]),
    },
    {
        "name": "make_demo_set",
        "description": "在 Blender/Bonsai 里给示例户型出一套 A3 图（图纸目录、平面、水电、顶面、门窗表与 model.ifc）。",
        "inputSchema": _schema({"out": {"type": "string", "description": "可选：输出目录（缺省 build/demo）"}}),
    },
    {
        "name": "render",
        "description": "在 Blender Cycles 中从 IFC4 渲染各房间角落透视图与 360° 全景图，并生成 Pannellum 全景漫游页。",
        "inputSchema": _schema({
            "ifc": {"type": "string", "description": "输入 IFC4 模型路径"},
            "out": {"type": "string", "description": "输出目录"},
            "mode": {"type": "string", "enum": ["corner", "pano", "all"], "description": "渲染模式：corner / pano / all（缺省 all）"},
            "rooms": {"type": "string", "description": "可选：只渲指定房间（房名或房间 id，逗号分隔）"},
            "samples": {"type": "integer", "description": "可选：角落图 Cycles 采样数"},
            "pano_samples": {"type": "integer", "description": "可选：全景图 Cycles 采样数"},
            "size": {"type": "string", "description": "可选：角落图分辨率（如 1600x1000）"},
            "pano_size": {"type": "string", "description": "可选：全景图分辨率（2:1，如 3072x1536）"},
            "device": {"type": "string", "enum": ["auto", "optix", "cuda", "cpu"], "description": "Cycles 设备（缺省 auto）"},
            "assets": {"type": "string", "description": "可选：本地 CC0 贴图与 HDRI 目录"},
            "pannellum": {"type": "string", "description": "可选：本地 Pannellum 目录（拷入离线使用）"},
            "save_blend": {"type": "boolean", "description": "是否保存组装后的 scene.blend"},
            "project": {"type": "string", "description": "可选：全景漫游页标题前缀"},
        }, ["ifc", "out"]),
    },
    {
        "name": "build_viewer",
        "description": "汇总施工图与渲染漫游目录生成零依赖全案预览页（index.html + project-index.json），并返回本地只读浏览命令。",
        "inputSchema": _schema({
            "out": {"type": "string", "description": "输出目录"},
            "sheets": {"type": "string", "description": "可选：施工图输出目录（与 render 至少给一个）"},
            "render": {"type": "string", "description": "可选：Cycles 渲染与漫游输出目录（与 sheets 至少给一个）"},
            "title": {"type": "string", "description": "可选：预览页标题"},
        }, ["out"]),
    },
]

_TOOL_INDEX = {t["name"]: t for t in TOOLS}
_HANDLERS = {
    "check_env": _tool_check_env,
    "list_packs": _tool_list_packs,
    "demo_apartment": _tool_demo_apartment,
    "dxfplan": _tool_dxfplan,
    "furnish_context": _tool_furnish_context,
    "furnish_place": _tool_furnish_place,
    "ceiling": _tool_ceiling,
    "mep": _tool_mep,
    "flooring": _tool_flooring,
    "wall": _tool_wall,
    "make_plan_set": _tool_make_plan_set,
    "check_ifc": _tool_check_ifc,
    "make_demo_set": _tool_make_demo_set,
    "render": _tool_render,
    "build_viewer": _tool_build_viewer,
}


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    spec = _TOOL_INDEX.get(name)
    if spec is None:
        raise ToolParamError(f"unknown tool: {name}")
    checked = _check_args(arguments, spec["inputSchema"])
    payload = _HANDLERS[name](checked)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": not bool(payload.get("ok")),
    }


# ------------------------------------------------------------------ JSON-RPC 2.0 调度

def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_message(msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid JSON-RPC 2.0 request")
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        return None if is_notification else _err(req_id, -32600, "missing method")
    if is_notification or method.startswith("notifications/"):
        return None
    raw_params = msg.get("params")
    params = {} if raw_params is None else raw_params
    if not isinstance(params, dict):
        return _err(req_id, -32602, "params must be an object")
    try:
        if method == "initialize":
            req_ver = params.get("protocolVersion")
            ver = req_ver if req_ver in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            return _ok(req_id, {
                "protocolVersion": ver,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _ok(req_id, {})
        if method == "tools/list":
            return _ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise ToolParamError("missing tool name")
            return _ok(req_id, call_tool(name, params.get("arguments")))
        if method == "resources/list":
            return _ok(req_id, {"resources": list_resources()})
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise ToolParamError("missing resource uri")
            return _ok(req_id, read_resource(uri))
        return _err(req_id, -32601, f"method not found: {method}")
    except ToolParamError as e:
        return _err(req_id, -32602, str(e))
    except Exception as e:  # noqa: BLE001
        return _err(req_id, -32603, f"{type(e).__name__}: {e}")


def serve_stdio(stdin=None, stdout=None) -> None:
    """按行读写 UTF-8 JSON-RPC 2.0 报文，直到 EOF。"""
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for raw in inp:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            resp: dict[str, Any] | None = _err(None, -32700, f"parse error: {e}")
        else:
            resp = handle_message(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
