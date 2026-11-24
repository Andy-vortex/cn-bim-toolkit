# 吊顶约定包

`rules.json`：按房间类型给每间房一个缺省吊顶，意图可以逐间改。程序在 `src/jiazhuang/ceiling.py`（只用 shapely），命令行 `scripts/ceiling.py`。在家具之后、水电之前跑：灯具的安装高度跟着吊顶底走。

```bash
python scripts/ceiling.py build/furnish/recog.json build/furnish/scheme.json build/ceiling --intent packs/cn/ceiling/example-intent.json
python scripts/mep.py build/furnish/recog.json build/ceiling/scheme.json build/ceiling/mep
blender -b --python-exit-code 1 --python scripts/make_plan_set.py -- build/furnish/recog.json build/ceiling/sheets --scheme build/ceiling/mep/scheme.json --kinds plan,ceiling,mep --title "{kind}"
```

## 规则表

| 键 | 内容 |
|---|---|
| `sources` | 依据：条文号（或"惯例""推断"）、自拟概括、参数、可信度、状态。条文都来自网页转载，标"待原文核对" |
| `materials` | 材料：图上写的名字 `name`、建模时的材质名 `css`；`grid` 是板缝网格规格（铝扣板 300×300，C8 惯例），写了的材料在顶面布置图上画网格、名称和标高进图例 |
| `templates` | 模板：缺省参数 `params`、能做的原顶净高下限 `min_height`、做不了退成哪种 `fallback`、换成这种模板时缺省做不做窗帘盒 `curtain` |
| `rooms` | 房间类型（`../furniture/room_types.json`）→ 缺省模板和参数 |
| `checks` | 原顶缺省高度 `height`、净高门槛 `min_net`（按房间类型）、边吊和回字形中间至少剩多少 `min_center_m2`、回字形外圈除去灯槽至少多宽 `slot_margin`、中间窄于 `center_min_width` 的细长部分并入外圈、建模厚度 `thickness` |

高度都离本层楼面，毫米。原顶高度取方案里房间的 `height`，没有用 `checks.height`（2800）。

| 房间 | 缺省 | 来源 |
|---|---|---|
| 客厅、餐厅 | 回字形：外圈宽 600、下吊 150、灯槽宽 150、灯槽比外圈高 60，石膏板 | C3、C5（推断） |
| 卧室、书房 | 边吊：宽 400、下吊 100，石膏板；有窗的墙做窗帘盒，宽 200 | C1、C2（惯例） |
| 厨房、卫生间 | 平顶：铝扣板，底 2.400 | C4（惯例） |
| 玄关、过道 | 平顶：石膏板，下吊 100 | C6（推断） |
| 阳台、储藏、其他 | 原顶刷白 | — |

| 净高门槛 | 值 | 来源 |
|---|---|---|
| 卧室、起居室、餐厅、书房 | 2.40 | GB 50096-2011 第 5.5.2 条，待原文核对 |
| 厨房、卫生间及其他 | 2.20 | GB 50096-2011 第 5.5.4 条，待原文核对 |

GB 55038-2025 的净高要求还没查到原文，暂按 GB 50096。

## 模板

| 模板 | 参数 | 块 |
|---|---|---|
| `原顶` | — | 不建块，图上写"原顶刷白" |
| `平顶` | `drop` 或 `bottom`（写了 `bottom` 就不看 `drop`）、`material` | 整间一块 |
| `边吊` | `width`、`drop`、`material` | 沿墙一圈宽 `width`，中间原顶 |
| `回字形` | `width`、`drop`、`slot`、`slot_rise`、`material` | 外圈（宽 `width - slot`）+ 灯槽（宽 `slot`，底比外圈高 `slot_rise`），中间原顶 |

三种都可以加窗帘盒（`curtain`、`curtain_width`）：沿有窗的墙一条，宽 `curtain_width`，底到原顶。外圈按房间轮廓往里收，L 形、凹凸的房间也是一整圈；收完中间窄于 `checks.center_min_width`（600，C7 推断）的细长部分（进门过道）并进外圈一起吊平，不留细缝。

缺省做不了时自动退一级，报告里那间房记 `fallback`（原因），不算错：原顶净高低于模板的 `min_height`（回字形 → 边吊 → 原顶）、收完以后中间不到 `min_center_m2`、回字形外圈除去灯槽不到 `slot_margin`、吊顶底低于净高门槛或门窗顶。意图点名的模板（写了 `ceiling`）做不了时照样退，但记一条违规 `template`（写明原因和最后真实退成的模板，一路退到原顶时写"原顶（这间房没有吊顶）"并列出经过的几级）、退出 1；意图里的房间吊顶底低于门槛或门窗顶不退，算违规，回字形外圈放不下灯槽也记一条违规。

## 意图（可选）

不写坐标，一间房一条，写了的参数盖过缺省：

```json
{"rooms": [
 {"room": "客厅", "ceiling": "回字形", "width": 450, "curtain": true},
 {"room": "R008", "ceiling": "平顶", "drop": 80},
 {"room": "R001", "ceiling": "原顶"}
]}
```

`room` 写房间 id 或房名（重名用 id）。`curtain`：`true` 所有有窗的墙，`false` 不做，`"W2"` 或 `["W2", "W4"]` 指定墙（墙号同家具布置的房间摘要）。可写的键：`room`、`ceiling`、`width`、`drop`、`slot`、`bottom`、`material`（`gypsum`、`alu`）、`curtain`、`curtain_width`。

一条写错（房间找不到、模板不认识、键不认识、数不对、窗帘盒指到没窗的墙）只在报告 `errors` 里记一行，写错的那一项不用，其余照做，退出 1；结构不对退出 2。

`--unit U1,U2` 只重做这几户：别的户以前的吊顶原样保留；户号不在识别结果里退出 2。公共部分的房间不做吊顶。

## 输出

吊顶写进方案的 `ceilings`，每块 `{id, room, template, part, material, bottom, polygon, holes, width?, slot?}`，`id` 是 `C-<房间 id>-<序号>`。方案里有 `ceilings` 时，水电布点的灯装在所在吊顶块的底下（没有块就是原顶），边吊、回字形外圈和石膏板平顶加筒灯，灯槽里加灯带（见 `../mep/README.md`：一排两头到墙差不到 `end_sym_max` 取对称，同一圈相对两排可布灯长度差不到 `align_within` 个间距时灯位对齐）；没有 `ceilings` 一切照旧。

`report.json`：`rooms` 每间一行（模板、来源"规则"或"意图"、原顶高度、块数、`fallback`），`errors`，`violations`：

| check | 查什么 |
|---|---|
| `net_height` | 吊顶底低于这类房间的净高门槛 |
| `below_opening` | 吊顶底低于门窗顶（门窗高度没标时按 `../drawing/conventions.json` 的缺省） |
| `slot` | 意图里的回字形外圈宽放不下灯槽（已退成边吊） |
| `template` | 意图点名的模板做不了（写明原因，最后退成哪种；退了几级时列出"回字形 → 边吊 → 原顶"） |
| `above_slab` | 吊顶块的底不低于原顶（意图把 `bottom` 写到原顶以上、`drop` 写 0）：写出原顶高度和允许的范围 |
| `stale_lights` | 方案里已有筒灯、灯带（水电布过点位）而吊顶改了，灯对不上新吊顶（`../mep` 的 `ceiling_light` 那项检查）：重跑 `scripts/mep.py` |
| `no_rooms`（errors） | 一间房都没做：房间都没有户号（`unit`）或都是公共空间 |

建模时每块一个 `IfcCovering`（CEILING），底标高、材质、属性集 `JZ_Ceiling`（房间、模板、部位、材料、底标高、宽）。

## 出图

`make_plan_set --kinds ...,ceiling` 出顶面布置图和紧跟其后的灯具定位图（灯和定位尺寸，造型线淡显，见 `../drawing/README.md`）。顶面布置图：Bonsai 的顶面视图（REFLECTED_PLAN_VIEW），吊顶轮廓和叠级线用 `../drawing/cn-ceiling.css` 加深；每块写底标高和材料（同一间房几块分开写，窗帘盒只写名字；铝扣板不写，房里画 300×300 板缝网格，和地面排砖同一个网格函数对中，遇到灯具符号和字断开，图例里每种标高一行"铝扣板吊顶 2.400"），每间房每个造型尺寸数值标一次（字不压墙、造型线；写不下的小段用水平、竖直两段的短引线引出），灯具符号（吸顶灯、筒灯、灯带虚线）和图例，房名不带面积。家具、洁具和插座开关不画。

## 已知限制

- 只有平顶、边吊、回字形三种；没有异形、弧形、跌级多于一级的。
- 窗帘盒只按"有窗的墙"放，不看窗的宽度（整面墙一条）。
- 铝扣板网格只有对中一种排法，不按灯位、排风口调整板缝。
- 小房间里写不下的标注写到建筑外轮廓以外（带引线，只穿过本房间的外墙）；放不下名称时只写标高（名称在图例里），再不行才不写（出图日志 `ceiling mark skipped`）；写不进自己那块的标注（窄带子、"灯槽"一律）带引线：从标高线（字的下划线）一端接出，端头实心小圆点落在它所属的那块里，纸面至少 4mm；中间那块的标注居中写，带子的标注靠着自己的带子、和中间那块的标注隔开，免得被看成中间原顶的标高。标注从不写进别的房间。整间原顶的房间不逐间写，图例里一行"原顶刷白 2.800"。
