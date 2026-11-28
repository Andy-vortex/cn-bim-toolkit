# 渲染与全景漫游约定包

`materials.json` 定义 Cycles 渲染的缺省机位、灯光、采样参数和各 IFC 构件表面材质映射。实现位于 `src/jiazhuang/tour.py`（纯 Python：机位规划、门洞热点角度计算、Pannellum 静态网页打包）与 `src/jiazhuang/render.py`（Blender Cycles 渲染），命令行入口为 `scripts/render.py`。

## 用法

```bash
blender -b --python-exit-code 1 --python scripts/render.py -- build/flooring/sheets/model.ifc build/render/demo
```

常用选项：

- `--mode corner|pano|all`：只出角落透视图、只出 360° 全景与漫游页，或全部（缺省 `all`）。
- `--rooms 客厅,卫生间`：只渲指定房间（房名或房间 id，逗号分隔）。
- `--samples 128` / `--pano-samples 128`：角落图与全景图 Cycles 采样数。
- `--size 1600x1000` / `--pano-size 3072x1536`：分辨率（全景固定 2:1 等距圆柱投影）。
- `--device auto|optix|cuda|cpu`：缺省 `auto` 按 OPTIX → CUDA → CPU 顺序选可用设备。
- `--assets <目录>`（或环境变量 `JZ_RENDER_ASSETS`）：可选本地 CC0 素材目录（如 Poly Haven 的 `hdri/*.hdr` 与 `textures/<名>/*_{diff,rough,nor_gl}_2k.jpg`）；不给或目录缺失时自动用零下载的程序化 PBR 材质和 Blender 内置 Nishita 物理天空。
- `--pannellum <目录>`（或环境变量 `JZ_PANNELLUM`）：可选本地 Pannellum 2.5.7 目录（含 `pannellum.js` 与 `pannellum.css`），给入后拷进 `tour/pannellum/` 供离线打开；不给时使用 `materials.json` 中的官方 CDN 链接。
- `--save-blend`：额外保存组装好的 `scene.blend` 便于在 Blender 中手工微调。

## 材质映射规则（只读 IFC，不修改源模型）

- `IfcCovering(FLOORING)`：按 `JZ_Flooring.Kind` 与 `Size` 分木地板、大砖、防滑砖、湿区小砖和门槛石。
- `IfcCovering(CLADDING)` / `SKIRTINGBOARD`：按 `JZ_WallFinish.Kind` 分乳胶漆、壁纸、墙砖（按 `Size` 和 `Dir` 匹配砖缝比例）与踢脚线。
- `IfcCovering(CEILING)`：按 `JZ_Ceiling.Material` 分石膏板与铝扣板；`Part == "窗帘盒"` 标记块自动隐藏。
- `IfcWindow` 与 `cn_shower`：用不挡阳光阴影的建筑玻璃着色器（`Is Shadow Ray` 透光）。
- `IfcSpace`、`IfcOpeningElement`、`IfcAnnotation`、`IfcGridAxis`、`IfcTypeObject` 及拆除构件在渲染场景中自动隐藏，并自动补原顶结构顶板与底板防止漏光。

## 输出结构

```text
<输出目录>/
  render-manifest.json      机位、采样、耗时与产出文件清单
  corners/R1_客厅.png       各房间自动最佳墙角透视图
  panos/R1.png              360° 全景原图（PNG）
  tour/
    index.html              Pannellum 漫游页（内置房间切换与门洞热点）
    tour.json               站点与热点数据（yaw/pitch）
    img/R1.jpg              压缩后的全景图（JPG）
    pannellum/              传 --pannellum 时离线拷贝
```

## 已知限制

- 手机浏览器使用陀螺仪（转动手机看全景）要求页面通过 HTTPS 访问；本地 `file://` 直接打开受浏览器跨源策略限制，需用任意本地静态服务器（如 `python -m http.server -d <输出目录>/tour 8765`）打开查看。
- 家具网格直接取自 IFC 模型（Bonsai 库家具与参数化 `cn_*` 家具），不做跨尺寸替换，保证与施工图位置和尺寸严格一致。
