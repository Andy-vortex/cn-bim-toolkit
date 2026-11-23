# cn-bim-toolkit

中国建筑与家装的开源 BIM 工具链：以 IFC 为唯一数据源，基于 IfcOpenShell / Bonsai，读图 → 建模 → 出图 → 规范自查。

早期开发，输出为方案深度，不能直接用于施工或报审；规范自查只做提示。

| 环节 | 模块（`src/jiazhuang/`） | 状态 |
|---|---|---|
| 读图：打散的天正 DXF → 房间、户、墙、门窗、轴网 | `dxfplan.py` | 实验 |
| 方案与拆改（只动非承重隔墙） | `scheme.py`、`renovate.py` | 实验 |
| 家具布置：房间摘要、布置意图 → 坐标、几何校验 | `furnish.py`，约定包 `packs/cn/furniture/` | 实验 |
| 水电点位：规则表布点、回路、开关控制哪些灯、几何校验 | `mep.py`，约定包 `packs/cn/mep/` | 实验 |
| 建模 → IFC4 | `plan2ifc.py`、`symbols.py` | 可用 |
| 平面图、门窗表、图纸目录 | `drawset.py`、`annotate.py`、`openings.py`、`schedules.py`，约定包 `packs/cn/drawing/` | 可用 |
| 水电图（插座布置、开关连线、给排水点位，回路表、点位统计表）、顶面图 | 同上 | 实验 |
| 立面、大样 | — | 未开始 |
| 规范自查 | `checker.py`，规则包 `packs/cn/rules/` | 可用 |
| 渲染与漫游 | Blender Cycles、Pannellum | 未接入 |
| agent 接口 | 技能 + MCP | 未开始 |

## 环境

- Python ≥3.11，`pip install -r requirements.txt`（识别用）。
- Blender 5.2.2 + Bonsai 0.9（实测版本）。Bonsai 为 GPL，需自行安装，本仓库不含；装在单独的 Blender 用户目录时，先设 `BLENDER_USER_RESOURCES` 指向它。建模、出图、自查都在 Blender 里跑。
- 出 PDF、PNG 用 Edge 的无头模式：`JZ_EDGE` 指向 msedge，不设时在 PATH 和 Program Files 里找。

## 运行

示例户型出图、自查；合成的示例标准层从 DXF 识别到出图。换成自己的 DXF 时先打散天正对象（见 [开发须知](docs/开发须知.md)），`--region` 是那一层在图上的外包（毫米，`make_demo_floor.py` 打印的 `JZLOG region` 就是它）：

```bash
blender -b --python-exit-code 1 --python scripts/make_demo_set.py
blender -b --python-exit-code 1 --python scripts/check_ifc.py -- build/demo/model.ifc packs/cn/rules/example-home.json build/demo
python tools/make_demo_floor.py build/floor
PYTHONPATH=src python -m jiazhuang.dxfplan build/floor/floor.dxf build/floor/recog.json build/floor/recog.svg --region 18000 6000 76000 24000
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/floor/recog.json build/floor/out --project 示例工程 --storey-name 二～十八层 --rules packs/cn/rules/example-home.json
```

其余选项（方案、图号前缀等）：`blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- --help`

## 家具布置

agent 读房间摘要，写布置意图（放什么、靠哪面墙、和谁的关系，不写坐标）；`place` 换算成坐标、做几何校验，有违规退出 1，报告里每条一行原因，改意图重来。格式见 [packs/cn/furniture](packs/cn/furniture/README.md)。家具类型运行时从 Bonsai 自带的家具库引进。

```bash
PYTHONPATH=src python -m jiazhuang.demo_apartment build/furnish/recog.json
python scripts/furnish.py context build/furnish/recog.json
python scripts/furnish.py place build/furnish/recog.json packs/cn/furniture/example-intent.json build/furnish
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/furnish/out --scheme build/furnish/scheme.json --title 平面布置图
```

## 水电点位

在带家具的方案上按规则表（[packs/cn/mep](packs/cn/mep/README.md)，条文号 + 自拟概括 + 参数，待原文核对）布插座、开关、灯、强弱电箱和给排水点，分回路，记开关控制哪些灯，再做几何校验；有违规退出 1，没放的点位和原因列在报告的 skipped 里。个例用可选的意图增删，不写坐标。建模时回路、给排水系统和开关—灯的连接写进 IFC；`--mep` 出插座布置图、开关连线图、给排水点位图和回路表、点位统计表。

```bash
python scripts/mep.py build/furnish/recog.json build/furnish/scheme.json build/mep
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/mep/sheets --scheme build/mep/scheme.json --mep --title "{kind}"
```

## 输出

```text
build/floor/out/
  A-00.pdf  A-00.png  A-01.pdf …                   每张图一份，A-00 是图纸目录
  A-00a_example-home-report.pdf  example-home-report.json  example-home-report.html
  A-00b_renovation-list.pdf  renovation-list.html  方案有拆改时
  门窗表.xlsx  图纸目录.xlsx
  model.ifc
  work/                                            中间文件（视图、图纸 SVG，喂给 Bonsai 的表格）
```

## 规则包

```json
{"id": "ex-2", "clause": "示例 2", "summary": "卫生间里的插座应为防溅型", "check": "auto", "type": "wet_room_sockets", "params": {"usages": ["卫生间"]}}
```

`type` 见 `checker.CHECKS`。结论六种：不通过 / 待确认 / 信息不足 / 人工复核 / 通过 / 不涉及。只写条文号和自己的概括，不抄标准原文。仓库只附 `packs/cn/rules/example-home.json`，阈值是示例，不对应任何标准。

## 数据和隐私

仓库里没有真实图纸；真实样本、输出和私人文档放在 `samples-private/`、`build/`、`docs/private/`，都不提交。

## 许可

代码和数据为 [MIT](LICENSE)；运行时依赖的 Bonsai、Blender 为 GPL，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
