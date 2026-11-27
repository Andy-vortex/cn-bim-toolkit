# 家具约定包

- `furniture.json`：支持的家具类型、校验门槛（`rules`）和门槛的依据（`sources`：条文号或"惯例""推断"、自拟概括、参数、可信度、状态）。
  - `lib:<类型名>`：Bonsai 自带家具库 `IFC4 Furniture Library.ifc` 里的类型，建模时引进（库随 Bonsai 分发，GPL，不进本仓库）。写沿墙宽、进深、高，库里哪一边靠墙（`back`；`any` 是前后对称、朝向无所谓），使用面（`use`，局部坐标矩形），挂墙件的离地高度（`mount_z`）。库里的橱柜写 `parts: ["sink", "hob"]`：没有位置参数，水槽、灶台按画法的缺省位置算（灶台避窗、水电点位用）。
  - `cn_<类型>`：库里没有的中国常用家具，按尺寸参数生成（`src/jiazhuang/furngen.py`）。`params` 的数字参数写 `[最小, 最大, 缺省]`，文字参数写可选值（第一个是缺省）。
  - `use_any`：几块使用面有一块能用就行（床的两侧）；`fixed`：固定家具，1:200 及更小的总图照画（洁具也照画），活动家具不画。
  - `back_height`、`back_depth`（可选，`cn_` 类型）：靠墙一条更高的部分（床头板）的高和进深。建模建成两个实体，挡窗、碰撞按两块各自的高算；库家具按整件外包高。
- `room_types.json`：房名 → 标准类型，以及这类房间的通道净宽。
- `example-intent.json`：示例户型（`demo_apartment`）的布置意图，`make_demo_set` 出图也用它。
- `example-floor-intent.json`：合成标准层（`tools/make_demo_floor.py`）四户的布置意图。

| 键 | 画法 | 参数 |
|---|---|---|
| `cn_sofa` | 分座沙发，`return` > 0 带转角 | width、depth、return、side |
| `cn_coffee_table` | 素面 | width、depth |
| `cn_tv_cabinet`、`cn_shoe_cabinet`、`cn_bookcase` | 带门线的柜体 | width、depth |
| `cn_wardrobe` | 柜体加挂衣杆 | width、depth |
| `cn_bed` | 床头板、枕头、被子折线 | width、depth |
| `cn_kitchen` | 一字或 L 形台面，画水槽和灶台 | width、depth、return、side、sink、hob |
| `cn_vanity` | 台面加台盆 | width、depth、basin |
| `cn_shower` | 方形淋浴房 | width、depth |

`sink`、`hob`、`basin` 都从台面左端（家具自己的左）量到中心，一字、L 形一样，转角在哪一侧都一样；不写时台盆居中，水槽、灶台分在正面那一段的两头。水电布点（灶台背后的油烟机插座、水槽背后的给水点）和灶台避窗都按这个画出来的位置（`furngen.part_x`）。`side` 是转角在哪一侧。橱柜正面一段（L 形去掉转角段）至少 1400，短了报"台面太短"。`cn_bed` 床身高 450，床头板（靠墙 100）高 1000：床头顶着窗报挡窗，长边贴窗墙不报。

## 坐标约定

家具的 `(x, y)` 是靠墙那条边的中点，`rot` 让局部 +y 指向室内，局部 +x 沿墙。左右是家具自己的左右（背靠墙、面朝室内）。

## 房间摘要

`scripts/furnish.py context` 给每间房：类型、面积、外包尺寸、墙边 `W1..Wn`。墙边沿房间轮廓逆时针编号，从最南（再最西）的角起，共线的边合并，伸进门洞的窄条不算；站在房内面朝这面墙，起点在右手边，`start` 写起点在房间的哪个角（如 `SE`，按外包半宽、半高归一后定，狭长房间也分得出四个角）。墙边上的门窗写沿墙起止（从起点量），门向内开（`in`）、向外开（`out`）或推拉（`slide`），识别结果带窗台高的窗写 `sill`。

## 布置意图

```json
{"rooms": [{"room": "客厅", "items": [
  {"id": "sofa",  "key": "cn_sofa", "params": {"width": 2400}, "wall": "W3", "align": "center"},
  {"id": "table", "key": "cn_coffee_table", "front_of": "sofa", "gap": 400},
  {"id": "tv",    "key": "cn_tv_cabinet", "wall": "W1", "facing": "sofa"},
  {"id": "chair", "key": "lib:Generic 1-Seater Sofa", "wall": "W4", "facing": "table"}]}]}
```

| 摆法 | 字段 | 含义 |
|---|---|---|
| 靠墙 | `wall`、`align`（`start`/`center`/`end`）、`offset`、`gap` | 从墙的起点/终点量到家具边，或中心偏移；`gap` 离开墙 |
| 靠墙对中 | `wall`、`facing`、`offset` | 中心对准另一件的中心 |
| 在前面 | `front_of`、`gap`、`offset` | 隔 `gap` 面朝那一件，中心对齐 |
| 在旁边 | `beside`、`side`（`left`/`right`）、`gap` | 同一条靠墙线，贴在那一件的左边或右边 |

`room` 写房名或房间 id（房名重名时写 id）；`name` 可选，图上的名字；`params` 只给 `cn_` 类型；参照的那件写在前面；距离单位毫米。每种摆法只认表里的字段，多写的报一行。不搜索、不打分：一件摆不了就在报告的 `errors` 里给一行原因，其余照摆；整份意图结构不对（不是对象、`rooms` 不是对象列表）时 `place` 退出 2。唯一的例外是下面的灶台避窗。

## 灶台避窗

灶台上方要装油烟机，灶台不放在窗前：油烟机挡窗装不上，窗口进风吹灶火；水槽宜在窗前（`sources.F1`，惯例，待原文核对）。规则：油烟机宽按 `rules.hood_width_mm`（900）算、对着灶台中线，沿墙的范围不进窗洞宽度、离洞边至少 `rules.hood_window_gap_mm`（100，和水电约定包 `checks.window_gap_mm` 同值）。只看橱柜靠着的那面墙上的窗，只看沿墙的范围，不看窗台、窗顶高低。

意图摆出来的橱柜（`cn_kitchen`、库里的橱柜）满足规则就不动。不满足时 `place` 按代价从小到大换一种摆法，取第一个满足的：

1. 同一个位置：水槽、灶台对调；整件左右镜像（水槽、灶台从另一端量，L 形转角跟着翻到另一侧，转角段还要靠着墙）
2. 灶台沿台面挪到最近的满足处，水槽不动（仍在正面那一段上、离端头 350、和水槽隔 700）
3. 整件沿墙平移，离原位置由近到远（只一字形：L 形的转角离不开墙角）
4. 换一面墙：L 形转角放墙角，先试转角留在原来那个墙角（换到这个墙角的另一面墙，两条腿换一下，占的地方差不多），再按墙边编号试别的墙角（先原来那一侧，再整件镜像到另一侧），水槽、灶台先照原样再对调；一字形按墙边编号，靠起点、靠终点、居中，水槽、灶台照原样、对调、镜像

每一种都要参数合法、灶台离开那面墙上所有的窗、这间房的家具校验（碰撞、挡门、通道……）不多出违规。换了的写进报告的 `arranged`（`{room, room_id, item, how, msg}`，`how` 是 `swap`、`mirror`、`hob`、`slide`、`wall`），没换的报告里没有这一项。都换不开就照意图摆，校验报 `hob_window`。别的家具不跟着动：墙角放着冰箱、门口要留空的，换墙常常换不开，这时改意图（挪冰箱、换橱柜尺寸）。

## 校验

`place` 写 `scheme.json`（方案 `items` 加上家具，带 `plan_id`）和 `report.json`。违规每条 `{room, room_id, item, check, value, unit, with, msg}`：

| check | 查什么 |
|---|---|
| `outside_room` | 伸出房间 |
| `in_wall` | 压墙 |
| `collision` | 家具互相重叠（高度区间不重叠的不算，挂墙件按高度分层） |
| `door_swing` | 压平开门的开启扇形 |
| `door_front` | 占了门洞两侧 `door_front_mm` 进深的通行区 |
| `window_blocked` | 顶高过窗台、又占了窗前 `window_front_mm` 进深（窗台高识别结果没有时用 `window_sill_mm`；凸窗按它在墙边上的洞口起止往室内量，两侧不外扩） |
| `aisle` | 从房门到使用面没有通道净宽（`value` 是实际最宽约多少） |
| `use_blocked` | 使用面被别的家具或墙占了，或伸出房间（`use_any` 的每块都不能用才报） |
| `hob_window` | 灶台上方的油烟机沿墙进了窗洞、或离洞边不到 `hood_window_gap_mm`（`with` 是哪樘窗，`value` 是灶台还要沿墙挪多少） |

只查二维（识别结果的毫米坐标，纯 shapely）；门扇扇形和出图避让用的是同一个函数（`openings.opening_zones`）。

报了违规就挪位置、换墙或换尺寸，不要拿 `gap`、`offset` 让家具离墙、离角留一条几百毫米的缝来躲校验：图上是死角，现场也没人这么摆。

## 已知限制

- 型号只做了全屋布置用到的；蹲便器、榻榻米、餐边柜等还没有。
- 不切功能区：客厅、餐厅合成一间的大房间就是多几条墙边。
- 水槽、灶台只能在橱柜正面那一段上：L 形的转角段（常靠着没有窗的那面墙）放不了灶台，所以窗在中间的小厨房，灶台避窗常常只能换墙。
