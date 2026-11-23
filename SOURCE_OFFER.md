# Corresponding Source / 对应源码

本项目以 GNU GPL v3.0 分发。

## 本项目源码

- 本仓库/源码 zip 中的 `standalone/`、`wows_coyote/`、`tools/`、`docs/` 等即为对应源码。
- 如果分发的是打包后的二进制（例如 PyInstaller exe），必须同时提供本仓库源码，
  或提供 GPL-3.0 §6 允许的书面要约（有效期至少 3 年）。

## 随包模板数据的上游源码

`standalone/templates/official_manifest.json` 的数据来自：

- 仓库：https://github.com/dungeonlab-open/dglab-kit-python
- commit：`4cce771c0fd93d043f3e83bbb0467e4197cde62e`
- 文件：`src/dglab_kit_python/waveform/coyote.py`（`COYOTE_WAVEFORMS`）
- 许可：GPL-3.0-only

获取对应源码：

```bash
git clone https://github.com/dungeonlab-open/dglab-kit-python.git
git -C dglab-kit-python checkout 4cce771c0fd93d043f3e83bbb0467e4197cde62e
```

## 重新生成清单

```bash
py -3.13 tools/build_official_manifest.py ^
  --sdk-path "E:\dglab-kit-python" ^
  --commit 4cce771c0fd93d043f3e83bbb0467e4197cde62e ^
  --out standalone/templates/official_manifest.json
```
