# Bundled official templates

`official_manifest.json` 是随 standalone 分发的官方模板清单：

```text
source  = dglab-kit-python
license = GPL-3.0-only
commit  = 4cce771c0fd93d043f3e83bbb0467e4197cde62e
waves   = 24
```

来源：[dungeonlab-open/dglab-kit-python](https://github.com/dungeonlab-open/dglab-kit-python)
的 `src/dglab_kit_python/waveform/coyote.py` 中的 `COYOTE_WAVEFORMS`。
帧保持不变；本文件只是把 SDK 常量转换成带 `source/license/commit/sha256` 的只读清单。

**不要手工编辑。** 重新生成：

```bash
py -3.13 tools/build_official_manifest.py ^
  --sdk-path "E:\dglab-kit-python" ^
  --commit 4cce771c0fd93d043f3e83bbb0467e4197cde62e ^
  --out standalone/templates/official_manifest.json
```

许可说明见仓库根目录 `LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`。
