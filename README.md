# cn-bim-toolkit：面向中国建筑与家装的开源 BIM 工具链（早期）

**目标**：以 IFC 作为唯一的数据源，把成熟的开源软件（IfcOpenShell、Bonsai、Blender、ezdxf、shapely 等）串成一条中国建筑和家装设计的全流程：

> 读图（DWG/DXF）→ 户型与房间识别 → 方案布置与拆改 → 水电点位 → 全案施工图 → 写实渲染与全景漫游 → 规范自查

最终会包装成 agent 能直接调用的工具（技能 + MCP），让 AI 助手可以端到端地完成"拿到图纸 → 出一套能看懂的中文施工图"。Python 包名为 `jiazhuang`。

> **状态：早期开发中，没有正式版本。** 目前的输出只到方案深度，**不是施工图，不能直接用于施工或报审**；规范自查只做设计提示，不替代建筑、结构、消防等专业审查。

## 路线图和现状

| 环节 | 模块 | 现状 |
|---|---|---|
| 读图 | `dxfplan.py`：从"天正对象已打散"的 DXF 里识别房间、户、墙段、门窗（含凸窗、推拉门和开启方向）、轴网、标高 | 实验性，已用一套真实住宅图纸验证 |
| 方案与拆改 | `renovate.py`：只动非承重隔墙的拆改操作（门洞加宽、新开/封堵门洞、新砌卫生间），输出拆改清单；`scheme.py`：方案数据格式 | 实验性；家具布置方案未开始 |
| 模型 | `plan2ifc.py`：墙、空间、门窗、轴网（IfcGrid）、设施、拆除构件 → IFC4；`symbols.py`、`demo_apartment.py` | 可用 |
| 施工图 | 约定包 `packs/cn/drawing`（中文样式、平面符号、A3 图框）；`annotate.py`（房间名、轴号、尺寸、点位高度、吊顶标高）；`drawset.py`（视图、比例、按分区出图、图例、PDF） | 平面图可用；水电全套、顶面、立面、大样待补 |
| 规范自查 | `checker.py`：通用检查引擎，规则包是纯数据（`packs/cn/rules/*.json`） | 引擎可用；目前只附示例规则包 |
| 渲染与漫游 | Blender Cycles、Pannellum 全景 | 已做可行性验证，未接入流程 |
| agent 接口 | 技能说明 + MCP 工具 | 未开始 |

## 环境

- Python 3.11 及以上，`pip install -r requirements.txt`（ezdxf、shapely），用于识别和排方案。
- Blender 4.2 及以上，并安装 [Bonsai](https://bonsaibim.org/)（IfcOpenShell 的 Blender 插件，GPL-3.0，需自行安装，本仓库不包含）。生成 IFC、规范自查和出图都在 Blender 里运行，用 Bonsai 自带的 ifcopenshell 和 shapely。
- 出 PDF 目前调用 Windows 上的 Microsoft Edge 无头打印（路径写在 `drawset.py`、`checker.py` 里）。

## 快速开始

示例户型（合成数据）出一套 A3 施工图，并用示例规则包自查：

```bash
blender -b --python scripts/make_demo_set.py
# 输出在 build/demo/：平面布置图、水电点位图、顶面布置图的 SVG 和 PDF
blender -b --python scripts/check_ifc.py -- build/demo/demo.ifc packs/cn/rules/example-home.json build/demo
```

从你自己的图纸出现状平面（DXF 需要先把天正对象打散，见 `docs/开发须知.md`）：

```bash
python -m jiazhuang.dxfplan plan.dxf x0 y0 x1 y1 recog.json recog.svg   # x0..y1：要识别的那一层在图上的范围（毫米）
blender -b --python scripts/make_plan_set.py -- recog.json out --project "工程名" --short "图框短名"
# 有自己的方案时加 --scheme scheme.json；要自查时加 --rules a.json,b.json
```

运行 Python 模块前把 `src` 加进 `PYTHONPATH`（Blender 脚本会自己加）。

## 规范规则包

检查引擎不认识任何具体标准，规则包是纯数据：

```json
{"id": "ex-2", "clause": "示例 2", "summary": "卫生间里的插座应为防溅型",
 "check": "auto", "type": "wet_room_sockets", "params": {"usages": ["卫生间", "主卫", "公卫"]}}
```

`type` 取自 `checker.CHECKS`：房间最小面积、每床面积、外窗、门开启净宽、走廊净宽、湿区插座、房间必须有某种设施、分区必设用房、楼层标高上限等通用检查。结论分六种：通过 / 不通过 / 待确认 / 信息不足 / 不涉及 / 人工复核。

本仓库只附 `example-home.json`（示例阈值，不对应任何标准条文）。为具体标准编写规则包时，只写条文号和自己的概括，不要抄录标准原文；结论一律以标准原文和专业审查为准。

## 数据和隐私

仓库里没有任何真实图纸。真实样本、输出和含个人信息的文档放在 `samples-private/`、`build/`、`docs/private/`，已加入 `.gitignore`。

## 许可

本仓库代码和数据文件使用 [MIT 许可](LICENSE)。运行时依赖的第三方软件各有其许可（其中 Bonsai、Blender 为 GPL），见 [THIRD_PARTY.md](THIRD_PARTY.md)。
