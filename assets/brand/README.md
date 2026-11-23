# wowsDG 品牌图标 · 盾锚标（黄黑配色）

参考原图（蓝底白色盾锚标）重绘的矢量图标资产。全部几何按 0.1px 精度从原图测量，
并强制左右完全对称；与参考图叠加校验 IoU = 0.956（差异仅来自 JPEG 边缘抗锯齿）。

## 配色

| 名称 | HEX | RGB |
| --- | --- | --- |
| 主黄 Yellow | `#FFC400` | 255, 196, 0 |
| 墨黑 Black | `#0C0C0C` | 12, 12, 12 |

## 文件

| 文件 | 说明 |
| --- | --- |
| `mark-yellow.svg` | **独立标（透明底，黄色）**，首选；四周留 10 单位安全边 |
| `mark-black.svg` | 独立标（透明底，近黑），用于浅色背景 |
| `icon-black-yellow.svg` | 黑底黄标方形图标（圆角），1024 视图 |
| `icon-yellow-black.svg` | 黄底黑标方形图标（圆角） |
| `wows-icon.ico` | Windows 多尺寸图标 16–256（黑底黄标，圆角） |
| `png/mark-*-{1024,512,256}.png` | 透明底独立标，画布内高 82% |
| `png/icon-*-{1024,512,256}.png` | 圆角方形图标 |
| `png/icon-*-square-1024.png` | 全出血方形（与原始构图一致，无圆角） |
| `preview.png` | 全部变体的对照预览 |
| `generate.py` | 生成脚本（Pillow），颜色/比例/圆角等常量在文件头 |

## 用法

**SVG（推荐）**——透明底矢量，任意尺寸清晰：

```html
<img src="assets/brand/mark-yellow.svg" alt="wowsDG" height="40">
```

**Windows 应用图标**——`standalone/wows_coyote.spec` 中把

```python
    icon=None,
```

改为：

```python
    icon=os.path.join(ROOT, "assets", "brand", "wows-icon.ico"),
```

然后重新构建即可让 exe / 任务栏 / 卸载项使用新图标。

**深色/浅色背景**——深色 UI 用 `mark-yellow`，浅色 UI 用 `mark-black`；
需要方形图标时用 `icon-black-yellow`（黑底黄标）或 `icon-yellow-black`（黄底黑标）。

## 重新生成

```bash
py -3 assets/brand/generate.py
```

需要 Pillow（已安装）。改色时修改脚本顶部 `YELLOW` / `BLACK` / `MARK_RATIO` /
`SQ_RADIUS` 常量即可。图标比例：方形图标内标高 66%，透明图画布内标高 82%。

> 本仓库不包含 `preview.png` 和 `png/` 生成物；运行 `generate.py` 可重新生成。
