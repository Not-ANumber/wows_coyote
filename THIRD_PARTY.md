# Third-Party Notices / 第三方声明

本项目以 **GNU GPL v3.0** 分发；完整许可证见根目录 `LICENSE`。
Corresponding source 见 `SOURCE_OFFER.md`。

## 随包模板数据

| 组件 | 许可 | 用法 |
|---|---|---|
| [dglab-kit-python](https://github.com/dungeonlab-open/dglab-kit-python) @ `4cce771c0fd93d043f3e83bbb0467e4197cde62e` | **GPL-3.0-only** | `standalone/templates/official_manifest.json` 的 24 条 `COYOTE_WAVEFORMS` 数据；帧未修改，仅转换为带 `source/license/commit/sha256` 的清单。 |

以下内容**没有**随本项目分发：

- 官方归档 Demo 的 16 条 `.pulse` / `expectedV3`（无 OSS 许可）；
- 官方协议仓库的 2 条 V3 示例（无 OSS 许可，商用需授权）；
- DGHub 内置/用户波形库（DGHub AGPL-3.0，导出不等于再分发许可）；
- HD2-DG-LAB 的预设（MIT，但非官方；本项目未使用）。

## 运行时依赖

| 组件 | 许可 | 说明 |
|---|---|---|
| CPython 3.13 | PSF License | 运行时 |
| `websockets` | BSD-3-Clause | V3 WebSocket |
| `qrcode` | BSD-3-Clause | 二维码 |
| `colorama` | BSD-3-Clause | `qrcode` 依赖 |
| `pywebview`（可选） | BSD-3-Clause | 独立置顶 overlay 小窗 |
| Microsoft Edge WebView2 Runtime（可选） | Microsoft 条款 | pywebview 在 Windows 上的运行时 |
| `installer/ChineseSimplified.isl` | MIT | Inno Setup 简体中文语言文件，来源 [kira-96/Inno-Setup-Chinese-Simplified-Translation](https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation) |
| Inno Setup 6（仅构建时） | Inno Setup License | 安装包编译器；产物可免费分发 |

## 本仓库内的 DGHub 插件

仓库同时包含 `wows_coyote/`（DGHub 外部插件）和 `standalone/`。
按根目录 `LICENSE`，整个仓库以 GPL-3.0 分发；如果希望 DGHub 插件单独闭源分发，
请把该插件从本仓库拆出并单独确定许可证，不要把本仓库的 GPL 文本套到拆分后的闭源包上。

## 商业使用

DG-LAB 官方开源资料注明商业使用需事先授权。GPL-3.0 只解决开源/再分发合规，
**不替代 DG-LAB 的厂商授权**；商业发布前请联系 DG-LAB 确认。
