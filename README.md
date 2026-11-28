# cn-bim-toolkit

中国建筑与家装的开源 BIM 工具链：以 IFC 为唯一数据源，基于 IfcOpenShell / Bonsai，读图 → 建模 → 出图 → 规范自查。

早期开发，输出为方案深度，不能直接用于施工或报审；规范自查只做提示。

| 环节 | 模块（`src/jiazhuang/`） | 状态 |
|---|---|---|
| 读图：打散的天正 DXF → 房间、户、墙、门窗、轴网 | `dxfplan.py` | 实验 |
| 方案与拆改（只动非承重隔墙） | `scheme.py`、`renovate.py` | 实验 |
| 家具布置：房间摘要、布置意图 → 坐标、几何校验 | `furnish.py`，约定包 `packs/cn/furniture/` | 实验 |
| 水电点位：规则表布点、回路、开关控制哪些灯、几何校验 | `mep.py`，约定包 `packs/cn/mep/` | 实验 |
| 吊顶：按房间类型给缺省、意图逐间改、净高校验 | `ceiling.py`，约定包 `packs/cn/ceiling/` | 实验 |
| 地面：材料、排砖（对中、地漏对砖中、木地板错缝）、门槛石、数量 | `flooring.py`，约定包 `packs/cn/flooring/` | 实验 |
| 墙面：每面墙的做法、高度、墙砖排砖、出哪些墙的立面和立面字母 | `wall.py`，约定包 `packs/cn/wall/` | 实验 |
| 建模 → IFC4 | `plan2ifc.py`、`symbols.py` | 可用 |
| 平面图、门窗表、图纸目录 | `drawset.py`、`annotate.py`、`openings.py`、`schedules.py`，约定包 `packs/cn/drawing/` | 可用 |
| 水电图（插座布置、开关连线、给排水点位，回路表、点位统计表）、顶面布置图、灯具定位图、地面铺装图、地面材料表 | 同上 | 实验 |
| 室内立面：每面要出立面的墙一张视图，内视符号、尺寸、标高、点位高度、材料标注、墙砖缝、踢脚线 | `elevation.py`，同上 | 实验 |
| 大样 | — | 未开始 |
| 规范自查 | `checker.py`，规则包 `packs/cn/rules/` | 可用 |
| 渲染与漫游 | `render.py`、`tour.py`，约定包 `packs/cn/render/`（Blender Cycles、Pannellum） | 实验 |
| agent 接口 | `mcp_server.py`，技能 `skills/cn-bim-toolkit/` | 实验 |

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

agent 读房间摘要，写布置意图（放什么、靠哪面墙、和谁的关系，不写坐标）；`place` 换算成坐标、做几何校验，有违规退出 1，报告里每条一行原因，改意图重来。橱柜的灶台摆在窗前时（油烟机装不上），`place` 先自己换摆法（水槽灶台对调、挪灶台、平移、换墙），换不开才报违规。格式见 [packs/cn/furniture](packs/cn/furniture/README.md)。家具类型运行时从 Bonsai 自带的家具库引进。

```bash
PYTHONPATH=src python -m jiazhuang.demo_apartment build/furnish/recog.json
python scripts/furnish.py context build/furnish/recog.json
python scripts/furnish.py place build/furnish/recog.json packs/cn/furniture/example-intent.json build/furnish
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/furnish/out --scheme build/furnish/scheme.json --title 平面布置图
```

## 水电点位

在带家具的方案上按规则表（[packs/cn/mep](packs/cn/mep/README.md)，条文号 + 自拟概括 + 参数，待原文核对）布插座、开关、灯、强弱电箱和给排水点（墙上的点位避开门洞和窗洞，压窗的顺着墙挪开，最多挪 600；冰箱这类专用插座旁边放不下时放到电器背后；厨房台面插座只放在台面上方，放不下少放、报出来），分回路（一联开关和它接的整串灯进同一路），记开关控制哪些灯，再做几何校验；有违规退出 1，没放的点位和原因列在报告的 skipped 里。个例用可选的意图增删，不写坐标。建模时回路、给排水系统和开关—灯的连接写进 IFC；`--mep` 出插座布置图、开关连线图、给排水点位图和回路表、点位统计表。

```bash
python scripts/mep.py build/furnish/recog.json build/furnish/scheme.json build/mep
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/mep/sheets --scheme build/mep/scheme.json --mep --title "{kind}"
```

## 吊顶

在家具之后、水电之前跑：按房间类型给每间房一个缺省吊顶（客厅餐厅回字形、卧室边吊加窗帘盒、厨卫铝扣板平顶……，见 [packs/cn/ceiling](packs/cn/ceiling/README.md)），意图逐间改模板和参数，不写坐标；查净高和门窗顶，有违规退出 1。吊顶建成 `IfcCovering`，灯装在吊顶底下；有吊顶时水电布点另加筒灯（沿边吊、回字形外圈）和灯带（沿灯槽），灯按组控制。

```bash
python scripts/ceiling.py build/furnish/recog.json build/furnish/scheme.json build/ceiling --intent packs/cn/ceiling/example-intent.json
python scripts/mep.py build/furnish/recog.json build/ceiling/scheme.json build/ceiling/mep
```

## 地面

在水电之后跑（要读地漏的位置）：按房间类型给每间房一种缺省材料（客餐厅、玄关过道 800×800 地砖，卧室书房木地板，厨房 600、卫生间阳台 300 防滑地砖，湿区降 20，见 [packs/cn/flooring](packs/cn/flooring/README.md)），意图逐间改材料、规格、方向、起铺。排砖是固定规则：顺着房间里多数的墙铺，取房间里最大的矩形对中，边砖对称且不小于半块；湿区把地漏放到一块砖正中；木地板逐排错开 1/3 板长；湿区、材料不同的门口和入户门放门槛石，干区同材料的门口画压条。每个区算面积、整块、切块和含损耗的合计，覆盖面积对不上、门槛石和门对不上退出 1。面层建成 `IfcCovering`（FLOORING），砖缝不建模，出图时按记下的参数现算。

```bash
python scripts/flooring.py build/furnish/recog.json build/ceiling/mep/scheme.json build/flooring --intent packs/cn/flooring/example-intent.json
```

## 墙面

在地面之后跑（要读门窗、吊顶底、点位和地面完成面）：墙边就是家具布置用的 W1..Wn，按房间类型给每面墙一种缺省做法（客厅卧室等乳胶漆到原顶加 80 踢脚线，厨房 300×600 墙砖横贴、卫生间竖贴，都贴到吊顶底以上 100（吊顶上方有一道砖缝时停在缝上），阳台乳胶漆，见 [packs/cn/wall](packs/cn/wall/README.md)），意图逐间、逐面改做法、贴法、顶高、踢脚线，点名出立面。每面墙算出墙面轮廓（减去门窗洞）、墙砖排砖网格（竖向贴进吊顶时一条缝对齐吊顶底、没吊顶时从地面完成面整砖起；沿墙两端对中或从露着的一端起；墙两头和最上、最下一排露着的非整砖不小于 100）、要不要出立面（厨卫每面、柜体背靠的墙、做法不是涂料的墙、意图点名的）和立面字母（朝北那面 A，顺时针 B、C、D）。点位落在洞口里或面层外、墙砖顶露出墙退出 1。面层建成 `IfcCovering`（CLADDING、SKIRTINGBOARD），平面类图都不画它，立面上按投影画出来（`--kinds elev`，见下）。

```bash
python scripts/wall.py build/furnish/recog.json build/flooring/scheme.json build/wall --intent packs/cn/wall/example-intent.json
```

## 图种

`--kinds` 选出哪几种图，逗号分开，按固定顺序排成一套（一个 IFC、一个比例、图号连续、一份目录，表格在后）：

| 图种 | 图 | 表 |
|---|---|---|
| `plan`（缺省） | 平面布置图 | 门窗表 |
| `floor` | 地面铺装图（方案里要有地面） | 地面材料表 |
| `ceiling` | 顶面布置图、灯具定位图（方案里要有吊顶） | — |
| `mep` | 插座布置图、开关连线图、给排水点位图 | 回路表、点位统计表 |
| `elev` | 室内立面图（方案里要有墙面），排在全部图纸和表格之后 | — |

`--mep` 等于 `--kinds mep`。

```bash
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/flooring/sheets --scheme build/wall/scheme.json --kinds plan,floor,ceiling,mep,elev --title "{kind}"
```

立面（`elev`）：墙面数据里要出立面的墙（`wall.py` 定的：厨卫每面、柜体背靠的墙、做法不是涂料的墙、意图点名的），每面一张 1:50 立面，字母和墙面数据相同（朝北那面 A，顺时针）。每张画本房间的墙、门窗、面层和离这面墙 120 以内的家具、洁具、电器、壁灯、点位（小方块加距地高度 `H300`），两道横向尺寸（洞口分段、总长）、一道竖向尺寸（楼面、窗台、洞口顶、吊顶底、原顶）、楼面和吊顶底标高、原顶粗线（平顶吊顶以上加遮罩盖住墙段接缝）、墙面材料和踢脚线引出标注、踢脚线浅灰填充（扣家具投影）以及墙砖排砖缝（扣家具和点位，遇字断开）。厨卫等多面墙房间单独成张 2×2 排在 A3 上，1～2 面墙的小房间合并排在 2×2 图纸上，各视图标题写"厨房 A 立面图 1:50"。加 `elev` 时平面布置图上补画各房间的四向内视符号（圆、实心方向三角、字母、所在立面图号），其余非立面图纸、表格和图号不变。

## 按户出图

一层有几户时，`--unit` 只出其中一户的一整套图（图种随 `--kinds`）：只画这一户的房间和围着它的墙，尺寸、轴号、门窗表、回路表都只算这一户，比例取 A3 放得下的最大一档（1:50、1:60、1:80、1:100、1:150）。1:150 是上限：三道尺寸加图例算在内还放不下的户，报一行 `unit too large`、退出 1，不出图。每户一套图号，从 A-00 起；要出所有户就循环调用。

```bash
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/floor/recog.json build/floor/U1 --unit U1 --project 示例工程
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

## 渲染与全景漫游

直接读取同一份 `model.ifc`（只读，不修改源模型），按 `packs/cn/render/materials.json` 为墙地顶面层、门窗、家具和灯具赋 PBR 材质，自动布置各房间墙角透视机位与避让家具的 360° 全景站，按门洞与开敞边计算热点角度并生成 Pannellum 静态漫游页：

```bash
blender -b --python-exit-code 1 --python scripts/render.py -- build/flooring/sheets/model.ifc build/render/demo
```

- 默认零下载即可运行（程序化 PBR 材质 + Blender 内置 Nishita 物理天空 + 官方 Pannellum CDN）；传入 `--assets <目录>`（或环境变量 `JZ_RENDER_ASSETS`）自动升级本地 CC0 贴图与 HDRI，传入 `--pannellum <目录>`（或 `JZ_PANNELLUM`）自动离线打包 Pannellum。详见 [packs/cn/render](packs/cn/render/README.md)。
- 输出 `corners/*.png`、`panos/*.png`、`tour/index.html`、`tour/tour.json`、`tour/img/*.jpg` 与 `render-manifest.json`。

## agent 接口（MCP 与技能）

仓库提供纯标准库的 MCP stdio 服务（无需额外依赖）与跨 agent 技能说明（[skills/cn-bim-toolkit/SKILL.md](skills/cn-bim-toolkit/SKILL.md)）：

- 启动服务：`python scripts/mcp_server.py`（按行 JSON-RPC 2.0；`--check` 查本地环境，`--list-tools` 列出全部 14 个工具）。
- 工具一一对应各命令行脚本（`dxfplan`、`demo_apartment`、`furnish_context`、`furnish_place`、`ceiling`、`mep`、`flooring`、`wall`、`make_plan_set`、`check_ifc`、`make_demo_set`、`render`）外加 `check_env`、`list_packs`；`packs/cn/` 下的规则表、示例意图和 README 同时作为 MCP 资源（`cn-bim://pack/...`）开放只读访问。
- 在 MCP 客户端配置（如 `.mcp.json`）中接入：

```json
{
  "mcpServers": {
    "cn-bim-toolkit": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": {
        "JZ_BLENDER": "/path/to/blender",
        "BLENDER_USER_RESOURCES": "/path/to/blender-profile"
      }
    }
  }
}
```

`JZ_BLENDER` 未设时自动在 `PATH` 和常见安装目录查找；`BLENDER_USER_RESOURCES` 未设且仓库根目录存在 `blender-profile/` 时自动使用。通过 MCP 传入的相对路径一律按仓库根目录解析，仓库外的输入输出请传绝对路径。

## 测试

公开自动测试基于标准库 `unittest`（兼容 `pytest`），不需额外安装测试包：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

无 Blender 环境时自动运行纯 Python 全链路与 MCP stdio 协议测试，并跳过 Blender 端到端用例；设置 `JZ_BLENDER`（或 `JZ_TEST_BLENDER=1`）时连同 Blender 出图与规范自查一并验证。

## 数据和隐私

仓库里没有真实图纸；真实样本、输出和私人文档放在 `samples-private/`、`build/`、`docs/private/`，都不提交。

## 许可

代码和数据为 [MIT](LICENSE)；运行时依赖的 Bonsai、Blender 为 GPL，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
