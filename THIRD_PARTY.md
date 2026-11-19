# 第三方软件和资料

本仓库只包含本项目自己写的代码和数据（MIT 许可），**不包含**下列第三方软件的任何代码或文件。它们需要使用者自行安装，并遵守各自的许可。

| 软件 | 用途 | 许可 | 说明 |
|---|---|---|---|
| [Blender](https://www.blender.org/) | 运行 Bonsai、出图、渲染 | GPL-2.0-or-later | 运行时依赖 |
| [Bonsai](https://bonsaibim.org/)（IfcOpenShell 的 Blender 插件） | 出图、排图（`drawset.py`、`annotate.py` 调用它的接口） | GPL-3.0 | 运行时依赖。出图时会读取本机 Bonsai 自带的 `default.css`，与本仓库的 `packs/cn/drawing/cn.css` 拼成一份样式表写到输出目录；这份合并文件属于输出物，不进仓库 |
| [IfcOpenShell](https://ifcopenshell.org/) | 读写 IFC | LGPL-3.0 | 随 Bonsai 安装 |
| [ezdxf](https://github.com/mozman/ezdxf) | 读 DXF | MIT | `pip install ezdxf` |
| [Shapely](https://github.com/shapely/shapely) | 平面几何运算 | BSD-3-Clause | `pip install shapely` |
| Microsoft Edge | 把 HTML/SVG 无头打印成 PDF | 专有软件 | 只作为外部程序调用 |

## 标准和规范

约定包（`packs/cn/drawing/`）参考了下列国家标准的常用画法，没有复制标准原文。规则包（`packs/cn/rules/`）也只写条文号和自拟概括。标准的著作权归各自的发布机构和出版单位。

- GB/T 50001-2017《房屋建筑制图统一标准》、GB/T 50104-2010《建筑制图标准》
- GB/T 50786-2012《建筑电气制图标准》、GB/T 50106-2010《建筑给水排水制图标准》
