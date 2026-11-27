# 意图 JSON 速查（references/intents.md）

所有意图均为 UTF-8 JSON（允许 BOM），**不写绝对坐标**，只写房间名（或房间 id）、墙边编号 `W1..Wn` 和相对关系。完整说明见 `packs/cn/*/README.md`（或 MCP 资源 `cn-bim://pack/...`）。

## 1. 家具布置意图（`furnish_place`）

```json
{
 "rooms": [
  {
   "room": "客厅",
   "items": [
    {"id": "sofa", "key": "cn_sofa", "params": {"width": 2400}, "wall": "W3", "align": "center"},
    {"id": "table", "key": "cn_coffee_table", "front_of": "sofa", "gap": 400},
    {"id": "tv", "key": "cn_tv_cabinet", "wall": "W1", "facing": "sofa"},
    {"id": "chair", "key": "lib:Generic 1-Seater Sofa", "wall": "W4", "facing": "table"}
   ]
  }
 ]
}
```

- 靠墙：`wall`（`W1..Wn`）、`align`（`start`/`center`/`end`）、`offset`（毫米）、`gap`（离墙毫米）。
- 相对定位：`front_of` / `beside` / `facing` 引用同房间前面已声明的 `id`。
- 可用 `key` 与 `params` 通过 `list_packs` 或 `cn-bim://pack/furniture/furniture.json` 查询。
- 橱柜灶台落窗前时，`furnish_place` 会自动尝试对调、沿台面平移或换墙并在 `arranged` 里回报。

## 2. 吊顶意图（`ceiling`，可选）

```json
{
 "rooms": [
  {"room": "客厅", "ceiling": "回字形", "width": 450, "curtain": true}
 ]
}
```

- `ceiling` 可选模板：`原顶`、`平顶`、`边吊`、`回字形`（缺省按 `packs/cn/ceiling/rules.json` 的房间类型）。
- 常用参数：`drop`（下吊毫米）、`bottom`（底标高毫米）、`width`（边吊/外圈宽）、`slot`（灯槽宽）、`material`（`gypsum`/`alu`）、`curtain`（布尔值，窗帘盒）。

## 3. 水电点位意图（`mep`，可选）

```json
{
 "rooms": [
  {
   "room": "主卧",
   "add": [{"point": "data", "beside": "desk", "side": "right", "gap": 450}],
   "remove": ["ac_wall"]
  }
 ]
}
```

- `remove` 先执行，可写点位类型键（删本房该类全部）或具体 `plan_id`。
- `add` 后执行，锚点与规则表一致（`beside`/`behind`/`above`/`on`/`wall` 等）；点位类型键通过 `list_packs` 或 `cn-bim://pack/mep/points.json` 查询。

## 4. 地面意图（`flooring`，可选）

```json
{
 "rooms": [
  {"room": "玄关", "start": "corner"},
  {"room": "卫生间", "size": [300, 600]}
 ]
}
```

- 常用字段：`material`（见 `packs/cn/flooring/rules.json`，如 `tile800`、`wood`、`tile600_anti`、`tile300_anti`）、`size`（`[短边, 长边]` 毫米）、`dir`（铺贴方向）、`start`（起铺方式，如 `center`/`corner`）。

## 5. 墙面意图（`wall`，可选）

```json
{
 "rooms": [
  {
   "room": "客厅",
   "walls": {
    "W1": {"finish": "wallpaper", "note": "电视背景墙"},
    "W3": {"elev": true, "note": "沙发背景墙"}
   }
  },
  {"room": "卫生间", "walls": {"W3": {"top": "full"}}}
 ]
}
```

- 可整间覆盖或按 `walls.<W1..Wn>` 逐面覆盖：`finish`（`paint`/`tile300x600`/`wallpaper`）、`dir`（墙砖 `h` 横贴/`v` 竖贴）、`top`（`"full"` / `"ceiling+100"` / 毫米数值）、`skirting`（`"skirting"` 或 `false`）、`elev`（布尔值，是否点名出立面）、`note`（说明）。

## 6. 出图参数（`make_plan_set`）

- `kinds`：`["plan", "floor", "ceiling", "mep", "elev"]`（顺序固定：平面 → 地面 → 顶面 → 水电 → 表格 → 立面）。
- `unit`：单户出图时传户号（如 `"U1"`）；整层不传。
- `rules`：规范自查规则包路径列表，例如 `["packs/cn/rules/example-home.json"]`。
- 环境配置（未加 PATH 时）：
  - `JZ_BLENDER`：Blender 可执行文件路径
  - `BLENDER_USER_RESOURCES`：装有 Bonsai 插件的独立用户目录（若仓库根存在 `blender-profile/` 会自动回退使用）
  - `JZ_EDGE`：Edge 可执行文件路径（出 PDF / PNG）
  - `JZ_DRAWING_PACK`：可选，整包替换出图约定包目录
