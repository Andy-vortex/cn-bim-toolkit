# 水电点位约定包

`points.json`：在带家具的方案上自动布点的规则。房间类型（`../furniture/room_types.json`）× 锚点 → 点位类型、个数、高度、回路。程序在 `src/jiazhuang/mep.py`，命令行 `scripts/mep.py`。

## 规则表

| 键 | 内容 |
|---|---|
| `sources` | 依据：条文号（或"惯例""推断"）、自拟的一句概括、参数、可信度（A 原文或多处转载一致，B 一处转载，C 经验文章，— 没查到）、状态。条文都来自网页转载，标"待原文核对" |
| `points` | 点位类型：名称、符号（`../drawing/symbols.json` 的键；开关写成 `{"1": 单联, "2": 双联, "3": 三联}`，按控制几盏灯选）、类别、缺省高度 `z`、回路前缀 |
| `circuits` | 回路前缀：名称、IFC 系统类型、每路上限 `max`、各点位类型占几个名额 `weight`。回路号按户编（WL1、WX1、WX2……），满了换下一路 |
| `furniture` | 家具组：`furniture.json` 的键，`键:部件` 指到水槽（sink）、灶台（hob）、台盆（basin） |
| `rooms` | 房间类型 → `min`（至少几个插座）、`rules`（规则，按顺序布）；`like` 照抄另一类。储藏间、户内过道只布一盏灯和房门开关；规则表里没有的户内房间不布点，报告 `skipped` 里记一行 |
| `checks` | 校验门槛（工具自己的，不是规范值） |

点位的位置只按实物和规则定（面板、家具、门扇的实际尺寸），和出图比例、纸面符号大小无关：同一份方案出 1:50 还是 1:100，坐标都一样。图上符号比实物大带来的压线、叠字由出图避让（挑摆法、镜像，排水点的圆圈偏开画、带短线指回实际位置）。

```json
"master_bedroom": {"min": {"socket": 2}, "src": ["S5"], "rules": [
 {"point": "light", "ceiling": "center"},
 {"point": "switch_2way", "door": "room", "gap": 150, "controls": "main", "if": "bed", "src": ["S9"]},
 {"point": "switch", "door": "room", "gap": 150, "controls": "lights", "if": "!bed", "src": ["S9"]},
 {"point": "switch_bed", "beside": "bed", "side": "left", "gap": 150, "controls": "main", "src": ["C1"]},
 {"point": "socket", "beside": "bed", "side": "both", "gap": 400, "z": 700, "src": ["C1"]},
 {"point": "socket", "beside": "desk", "side": "both", "gap": 150},
 {"point": "ac_wall", "wall": "free", "prefer": "window"},
 {"point": "data", "wall": "free"},
 {"point": "socket", "wall": "free", "fill": true, "src": ["S5", "S7"]}]}
```

一条规则：`point`（点位类型）+ 一个锚点及其参数，可选 `z`（换高度）、`if`（组名：房里有这类家具才布；`!组名`：没有才布）、`controls`（开关控制的灯：`main` 主灯，`lights` 本房的灯）、`src`（来源编号）。

## 锚点

| 锚点 | 参数 | 放在哪 |
|---|---|---|
| `beside`: 家具 | `side`（家具自己的 `left`/`right`/`both`）、`gap` | 家具靠墙线上、家具宽度以外 `gap`；单写一侧放不下换另一侧 |
| `behind`: 家具 | `dx` | 家具（或水槽、灶台、台盆）背后的墙上，沿墙偏 `dx`，面对墙向左为正（左热右冷：热水 `dx` 正、冷水负） |
| `on`: 家具 | `count`、`clear` | 台面背后的墙上均布 `count` 个：离水槽、灶台、台盆的边（按部件实际宽度）至少 `clear`（缺省 100），插座之间至少隔面板宽加 200；放不下的个数记进 `skipped`，插座数由 `fill` 规则补 |
| `above`: 家具 | — | 家具正上方（灯） |
| `floor`: 家具 | `at`（`center`/`left`/`right`）、`inset`、`dy` | 地面：离那一侧边 `inset`（负数在家具外），离墙 `dy` |
| `door`: `room` / `entrance` | `gap` | 房门（有入户门就是入户门）或入户门锁舌一侧的墙上，离门洞边 `gap`（开关放不下再放宽到 `checks.switch_gap_max`）；墙垛实物上放不下面板（`gap` + 面板宽 + 100）才放到拐角另一面墙上，房门开关还放不下放到门外 |
| `wall`: `free` | `prefer`（`long`/`window`）、`count`、`fill` | 沿墙找一段空墙（避开门窗、高家具、已有点位，插座另避开淋浴、浴缸 `shower_clear` 以内），同类点位少的墙优先；`fill` 补到 `min` |
| `ceiling`: `center` | — | 房间顶面中心（房名标注点） |

家具写组名（`bed`、`sink`……；`wet` 是淋浴房和浴缸，一间卫生间只放一个等电位端子箱）或 `furniture.json` 的键。找不到锚点的规则跳过（房里没有床就没有床头插座），家具旁、背后、台面上放不下的点位不放（插座离淋浴、浴缸不到 `shower_clear` 也算放不下，单写一侧的换另一侧），插座数由 `fill` 规则补齐；没放的都记在报告的 `skipped` 里（房间、点位类型、锚点、原因；认不出类型的房间 `point`、`anchor` 为空）。家具上方的灯离顶面中心的主灯不到 `checks.light_gap` 时，不放主灯，那盏当主灯。

## 意图（可选）

规则自动布点为主；个例用意图增删，同一套锚点，不写坐标：

```json
{"rooms": [{"room": "主卧", "add": [{"point": "data", "beside": "desk", "side": "right", "gap": 450}], "remove": ["ac_wall"]}]}
```

`remove` 写点位类型（本房这一类全删）或 `plan_id`；先删后增。删掉的灯是哪个开关控制的，控的灯都没了的开关一并去掉（记进 `skipped`）。一条写错（包括房间块里 `room`、`add`、`remove` 以外的键）只在报告 `errors` 里记一行原因，其余照做；只放下一部分的（要 8 个放下 2 个、`both` 只放了一侧）也算写错，`errors` 里写明放下几个、还差几个、为什么，放下的留着；结构不对（不是对象、`rooms` 不是对象列表、`add`/`remove` 不是列表）退出 2。带 BOM 的 UTF-8 文件也认。

`--unit U1,U2` 只重布这几户：别的户以前布的点位原样保留；户号不在识别结果里退出 2。

## 输出

点位写进方案的 `items`，比家具多两个可选字段：`circuit`（回路号）、`controls`（开关控制的灯的 `plan_id`）。`plan_id` 是 `<点位类型>-<房间 id>-<序号>`；重跑先删掉上次布的点位。`report.json` 的违规每条 `{room, room_id, item, check, value, unit, with, msg}`：

| check | 查什么 |
|---|---|
| `in_door` | 落在门洞里，或在门扇开启范围里（门后） |
| `blocked` | 离高家具不到 `block_gap`、高度又和它重叠（灯、给排水点不查） |
| `outside_room`、`in_wall` | 出房间、压墙 |
| `overlap` | 和同一间房里另一个点位叠在一起（平面外框重叠、高度也重叠） |
| `socket_count` | 插座少于这类房间的 `min` |
| `switch_reach` | 房门开关离锁舌边超过 `switch_reach`，或在门扇开启范围里 |
| `near_shower` | 插座离淋浴房、浴缸不到 `shower_clear` |
| `circuit_over` | 一路超过上限 |

建模（`plan2ifc`）时回路建成 `IfcDistributionCircuit`（按户分开），冷水、热水、排水点归各自的 `IfcDistributionSystem`，开关和它控制的灯用端口连接（`IfcRelConnectsPorts`）。回路的线径、开关容量留给电工定。

## 出图

`make_plan_set --mep` 把每个平面换成插座布置图、开关连线图、给排水点位图三张（家具、洁具淡显，样式在 `../drawing/cn-mep.css`），表格是回路表和点位统计表：

```bash
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/mep/sheets --scheme build/mep/scheme.json --mep --title "{kind}"
```

开关连线图上每个开关到它控制的每盏灯画一条弧线，按 IFC 里的端口连接每次出图重画，不存进方案。高度标注只标和图例默认高度不同的（给水点每个都标），同一面墙上挨着的同类同高点位只标一个。三张水电图不标门窗编号（建筑信息，电工、水工用不着），轴号、尺寸、房名照标。符号里的字母（K、TV、TD、LEB、L、R、P）只从图纸下方或右侧读：符号转到字会倒着时换字母正过来的画法（`../drawing/symbols.json` 的 `letter`）。 图上符号按纸面大小画，比实物大：同一面墙上互相压着、压门扇的符号沿墙排开（只沿所在的墙挪，每个最多 8mm 图纸），挪开的画一条细线指回实际位置，图例里有"引线端点为实际位置"一行；点位坐标、点位明细、IFC 都不变。

## 已知限制

- 有热水点，没有热水器点位：冷热水口要跟着热水器走，现在既没有热水器这种家具，也没有"相对另一个点位"的锚点。
- 排风机专用插座（S4 列了）没有点位：没有排风机这种家具，卫生间吊顶上的位置也没法从平面定。和热水器一样先不做。
- 开关按实物放：门锁一侧、离门洞边 `gap`（不行放宽到 `checks.switch_gap_max`），墙垛放不下面板才换拐角另一面墙、门外。出图时纸面大小的符号比实物大，可能压到门扇的开启线：出图挑摆法、沿墙排开来减少，剩下的单独计数（只许变少）；双控开关的符号最宽，门边的墙垛常常挪 8mm 也让不开门扇。
- 1:100 下厨房、卫生间点位扎堆的地方（冷热水、排水点、地漏、台面插座）沿墙排开以后仍可能挤，挪开的符号离实际位置可达 8mm（0.8m）：要看清楚建议按 1:50 出图。
- 1:100 下小房间里放不下房名时，房名可能写到房间外或贴到墙：要看清楚按 1:50 出图。
- 厨房台面上放不下的"台面插座"由 fill 补到空墙上，可能落在没有台面的墙上（名字仍叫台面插座、高度仍按台面以上）。
- 规则只按房间类型和家具，不看业主习惯；回路只按点位类型和每路上限分，不算功率。
