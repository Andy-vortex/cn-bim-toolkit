---
name: cn-bim-toolkit
description: 驱动 cn-bim-toolkit 开源中国家装 BIM 工具链：从打散 DXF 识别户型，按“房间摘要 → 家具 → 吊顶 → 水电 → 地面 → 墙面 → IFC4 与全套 A3 施工图 → 规范自查 → Cycles 渲染与 Pannellum 全景漫游”全流程出图与迭代纠偏。适用于任何室内方案排布、施工图生成、IFC 规范自查或全景漫游任务。
---

# cn-bim-toolkit Agent 技能

本技能通过 MCP 服务（`scripts/mcp_server.py`）或等价命令行脚本驱动整条 BIM 流水线。**唯一真源是 IFC4**；不要让模型直接猜坐标，所有布局都通过结构化意图 JSON 经确定性引擎换算和校验。

## 1. 先检查环境与约定包

开工前先调一次（或跑一次）环境与约定包发现：

- MCP：`check_env`、`list_packs`
- CLI：`python scripts/mcp_server.py --check`

通过 MCP 调用时，所有相对路径按仓库根目录解析（如 `packs/cn/...`），仓库外的输入输出目录请传绝对路径。需要查看某类规则、可选键名或示例意图时，优先读取 MCP 资源 `cn-bim://pack/<相对路径>`（或直接读 `packs/cn/` 下对应 `README.md` 与 `example-intent.json`），详见 [references/intents.md](references/intents.md)。

## 2. 固定流水线顺序

```text
DXF ──► dxfplan（或 demo_apartment）──► 识别.json
                                            │
  furnish_context（读墙边 W1..Wn 与门窗）◄────┤
        │
        ▼
  furnish_place ──► ceiling ──► mep ──► flooring ──► wall ──► make_plan_set ──► check_ifc / render ──► build_viewer
```

| 阶段 | MCP 工具 | CLI 命令 | 产出 |
|---|---|---|---|
| 识别 | `dxfplan` | `PYTHONPATH=src python -m jiazhuang.dxfplan <平面.dxf> <识别.json> [检查图.svg] --region X0 Y0 X1 Y1` | `识别.json`、`检查图.svg` |
| 示例户型 | `demo_apartment` | `PYTHONPATH=src python -m jiazhuang.demo_apartment <识别.json>` | `识别.json` |
| 房间摘要 | `furnish_context` | `python scripts/furnish.py context <识别.json> [--scheme …] [--room …] [-o 摘要.json]` | 墙边 `W1..Wn` 与门窗摘要 |
| 家具布置 | `furnish_place` | `python scripts/furnish.py place <识别.json> <意图.json> <输出目录>` | `scheme.json`、`report.json` |
| 吊顶 | `ceiling` | `python scripts/ceiling.py <识别.json> <方案.json> <输出目录> [--intent …] [--unit …]` | `scheme.json`、`report.json` |
| 水电点位 | `mep` | `python scripts/mep.py <识别.json> <方案.json> <输出目录> [--intent …] [--unit …]` | `scheme.json`、`report.json` |
| 地面铺装 | `flooring` | `python scripts/flooring.py <识别.json> <方案.json> <输出目录> [--intent …] [--unit …]` | `scheme.json`、`report.json` |
| 墙面立面数据 | `wall` | `python scripts/wall.py <识别.json> <方案.json> <输出目录> [--intent …] [--unit …]` | `scheme.json`、`report.json` |
| 建模与出图 | `make_plan_set` | `blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- <识别.json> <输出目录> [选项]` | `model.ifc`、`*.pdf`、`*.png`、`*.xlsx` |
| 规范自查 | `check_ifc` | `blender -b --python-exit-code 1 --python scripts/check_ifc.py -- <model.ifc> <规则.json> <输出目录> [工程名]` | `*-report.json/html/pdf` |
| 渲染与全景漫游 | `render` | `blender -b --python-exit-code 1 --python scripts/render.py -- <model.ifc> <输出目录> [--mode all\|corner\|pano]` | `corners/*.png`、`tour/index.html` |
| 全案预览站 | `build_viewer` | `python scripts/viewer.py build <输出目录> [--sheets …] [--render …]`（浏览用 `serve`） | `index.html`、`project-index.json` |
| 示例户型一键出图 | `make_demo_set` | `blender -b --python-exit-code 1 --python scripts/make_demo_set.py [-- 输出目录]` | `JZ-00..04`、`model.ifc` |

### 依赖与过时规则

- **必须先 `furnish_context` 再写家具/墙面意图**：墙边编号 `W1..Wn` 沿房间轮廓逆时针、从最南（再最西）角起编，门窗位置与开向均以摘要为准。
- **修改上游必须重跑下游**：
  - 改家具后需重跑 `ceiling` → `mep` → `flooring` → `wall`；
  - 改吊顶后需重跑 `mep`（灯具标高与筒灯/灯带随吊顶走）→ `flooring` → `wall`；
  - 改水电后需重跑 `flooring`（湿区地漏对砖中）→ `wall`（点位避洞口/面层）；
  - 方案过时时，`ceiling` / `make_plan_set` 会直接报错退出 `1`。
- **整层按户出图**：`make_plan_set` 传 `unit: "U1"`（CLI `--unit U1`）只出该户整套图（1:50～1:150 自动取最大档，图号从 `A-00` 起）。全套图种传 `kinds: ["plan", "floor", "ceiling", "mep", "elev"]`。

## 3. 退出码与迭代纠偏闭环

- **退出码 `0`**：成功，最后一条 `JZLOG` 为 `JZLOG done <输出路径>`。
- **退出码 `1`**：业务校验未通过或打印失败（最后一条 `JZLOG failed …`）。此时 `scheme.json` 与 `report.json` 照常写出：
  1. 读取返回结果中的 `errors` 和 `violations`（不要编造未发生的错误）；
  2. 只针对报错的房间/构件修改意图 JSON（不要靠把家具挪离墙面留缝等不合理摆法绕过检查）；
  3. 重跑本阶段直到 `ok: true`（`returncode == 0`），再推进下一阶段。
- **退出码 `2`**：参数、文件、JSON 结构或约定包缺失错误，不建输出目录；先修路径或参数。
- **规范自查结论必须原样透传**：`通过` / `不通过` / `待确认` / `信息不足` / `不涉及` / `人工复核` 六种结论不得合并或改写；读数据只读 `*-report.json`（或 HTML 末尾 `<script type="application/json" id="data">`），不要解析 HTML 表格。

## 4. 交付与人工复核须知

本工具链输出为**方案深度**，不能直接用于施工或报审；规范自查与规则表条文均标有“待原文核对”，只作提示。交付给用户时务必提醒以下六项必须由专业人员现场或按正式图复核：

1. **承重墙与结构安全判定**（以结构图及物业核定为准，程序仅按墙厚推断）
2. **配电回路容量、保护电器与线径**（电工复核）
3. **防水做法与排水坡度**
4. **顶面及梁位实测标高**
5. **定制柜体按厂家工艺拆单**
6. **开工前全屋现场复尺**
