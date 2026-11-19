# 中国家装施工图约定包

让 Bonsai（IfcOpenShell 的 Blender 插件）出的图，**看起来像国内家装施工图**。只放数据和薄胶水，不改 Bonsai 本身。

## 包里有什么

| 文件 | 作用 |
|---|---|
| `cn.css` | 样式表，只含本包的覆盖规则（出图时自动接在本机 Bonsai 的 default.css 后面，见 `drawset.configure_project`）：长仿宋字体，字号按"汉字 ≥3.5mm、数字 ≥2.5mm"，线宽组 b=0.5mm，承重墙涂黑，新砌隔墙斜线填充，水电和洁具符号统一线宽 |
| `symbols.json` | 平面符号库：五孔插座、防溅插座、单联和双联开关、吸顶灯、筒灯、坐便器、台盆、地漏。每个符号带图例名称和默认安装高度 |
| `titleblocks/A3-CN.svg` | 中文 A3 横式图框。左侧装订边 25mm；右侧竖向标题栏有工程名称、图名、图号、比例、日期、设计/校对/审核、说明、指北针。由 `tools/make_titleblock.py` 生成，中文用数字字符引用，避免编码问题 |

## 怎么接到 Bonsai 上

- **样式表和图框目录**：写进 IfcProject 的 `BBIM_Documentation` 属性集（`StylesheetPath`、`TitleblocksDir`），见 `src/jiazhuang/drawset.py: configure_project`。
- **符号**：做成 IFC 类型（IfcOutletType 等）。类型带两种表达：
  - `Model/Body`：盒子，给三维渲染用。
  - `Plan/Body/PLAN_VIEW` 或 `REFLECTED_PLAN_VIEW`：二维符号。

  构件用 `type.assign_type` 关联类型。Bonsai 出平面或顶面图时优先用 Plan/Body，所以图上画的是符号，不是盒子投影。见 `src/jiazhuang/symbols.py`。
- **墙的画法靠材质名区分**：`cn-bearing` 按承重墙涂黑，`cn-partition` 按新砌隔墙填斜线。注意 Bonsai 生成样式类名时会**去掉连字符**，CSS 里要写 `material-cnbearing`。

## 自动标注（`src/jiazhuang/annotate.py`）

- **国标三道尺寸**：由里到外依次是洞口/墙垛、轴线、总尺寸。四个方向各算一次，和外一道重复的自动省略。
- **房间名和净面积**。
- **水电点位旁的安装高度**，写成 `H300` 这样。
- **顶面图**：吊顶名称和标高（米，三位小数）；没吊顶的房间写"原顶刷白"。

## 依据和复核状态

参考了下面四本标准的常用画法：

- GB/T 50001-2017《房屋建筑制图统一标准》
- GB/T 50104-2010《建筑制图标准》
- GB/T 50786-2012《建筑电气制图标准》
- GB/T 50106-2010《建筑给水排水制图标准》

**所有数值和符号形状都没有逐条对照规范原文，要请设计师或电工复核。**

## v0.1 已知限制

- 标注不会避让：文字、符号、门的开启线可能互相压住。
- 尺寸只画尺寸线和起止符，没有尺寸界线。
- 只支持横平竖直的墙和矩形房间，用的是包围框算法。
- 还没有：给水点（冷水、热水）、强弱电箱、空调和网络电视插座的符号；立面图；拆改图；地面铺装图。
- Bonsai 自带的视图标题（"1 平面布置图 1:50"）还不是仿宋。

## 怎么跑

```powershell
# Bonsai 装在项目专用的 Blender 用户目录里时，先指定它
$env:BLENDER_USER_RESOURCES = '<项目目录>\blender-profile'
blender -b --python scripts\make_demo_set.py
```

输出在 `build\demo\`：`JZ-01/02/03.pdf`（A3），以及对应的 SVG 和 IFC。示例户型从生成到出 PDF 共约 7 秒。
