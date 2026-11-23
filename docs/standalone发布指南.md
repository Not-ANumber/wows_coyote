# standalone 发布指南

> 适用版本：`wows_coyote` 1.0.0（standalone）
> 最后更新：2026-11-22
>
> 本文只讲 **standalone**（独立桌面程序）；DGHub 插件 `wows_coyote-2.9.x.zip`
> 是另一条发布线，版本号和发布物都不同，不要混在一起发。

---

## 1. 结论

- standalone 是独立的 Windows 桌面程序，以 **GPL-3.0** 分发；DGHub 插件
  只在源码层面被复用，不是运行前置。
- 正式发布 3 个客户端文件，另加 1 个更新清单：
  - `wows_coyote-1.0.0-gpl.zip`：GPL 对应源码；
  - `wows_coyote-1.0.0-win64.zip`：免 Python 的 Windows x64 便携包；
  - `wows_coyote-1.0.0-setup.exe`：Inno Setup 安装包；
  - `releases/latest.json`：更新检查清单，上传到自建服务器，不进 GitHub Release。
- 发布前必须跑 `py -3.13 tools/audit_release.py`，当前结果应为
  `FAIL 0 / REVIEW 0`。
- 仍需人工完成的事项：干净 Windows 安装测试、真机 10 局、DG-LAB 商业授权确认、
  （可选）代码签名。自动化审计不能替代这些。

---

## 2. 项目边界

### 2.1 standalone 发布物包含什么

| 路径 | 作用 | 进源码包 |
|---|---|---|
| `standalone/` | 宿主、调度器、V3/V4 传输、UI、桌面窗口、overlay | 是 |
| `wows_coyote/main.py` | 共享事件引擎（`TelemetryReader` / `MixerEngine` / 曲线表 / 配置 schema） | 是 |
| `wows_coyote/{manifest,base,hit,wows_preset_v2}.json` | 引擎默认配置、两张函数表、v2 预设 | 是 |
| `standalone/templates/official_manifest.json` | 24 条 dglab-kit-python 官方波形（GPL-3.0） | 是 |
| `standalone/ui/` | 主控制台 / 设置 / 事件 / overlay / OBS 页面 | 是 |
| `assets/brand/` | 黄黑盾锚标、ICO、图标生成脚本 | 是（仅构建所需文件） |
| `tools/build_standalone_zip.py` | 源码包构建清单（**发布集的唯一真源**） | 是 |
| `tools/build_windows.py` | PyInstaller + 便携包构建 | 是 |
| `tools/audit_release.py` | 发布前敏感信息与包内容审计 | 是 |
| `tools/finalize_release.py` | 刷新 `.sha256`、`SHA256SUMS.txt`、发布说明 | 是 |
| `tools/make_release.py` | 版本检查、构建、校验值刷新、生成 `latest.json` | 是 |
| `tools/deploy_manifest.sh` | 服务器更新清单原子替换与公网校验 | 是 |
| `tools/build_official_manifest.py` | 从固定 SDK commit 重新生成模板清单 | 是 |
| `installer/` | Inno Setup 脚本、中文语言文件、说明 | 是 |
| `LICENSE` / `THIRD_PARTY.md` / `SOURCE_OFFER.md` | GPL 全文、第三方声明、对应源码说明 | 是 |
| `CHANGELOG.md` | 版本变更 | 是 |

`standalone/templates/official_manifest.json` 的 `source/commit/license/sha256`
必须保持完整；加载器是 fail-closed 的，任一条不合法会整份拒绝。

> `docs/standalone更新分发.md` 是维护者部署手册，包含服务器目录/宿主环境信息，
> **不随源码包分发**；`tools/deploy_manifest.sh` 的服务器路径必须通过
> `WOWS_UPDATE_PATH` 传入。

### 2.2 只留在仓库、不进入发布包的内容

- 运行时产物：`standalone/qr.svg`、`*.log`、`__pycache__/`、
  `%LOCALAPPDATA%\wows_coyote\settings.json`；
- 构建中间物：`.phase0deps/`、`build/`、`dist/`、`dist_installer/`；
- 开发/验收材料：`tests/`、`simulator/`、`ref/`、`archive/`；
- 内部工具：`tools/v3_probe/`、`tools/verify_waveform/`；
- 规划/评估文档：`docs/脱离DGHub全链路评估.md`、`docs/event_redesign.md`
  等（不在 `build_standalone_zip.py` 的 `DOC_FILES` 内）。

> 判断标准以 `tools/build_standalone_zip.py` 的 `collect_entries()` 为准。
> 不要直接“把整个目录压成 zip”。

### 2.3 与 DGHub 插件的耦合点

- `standalone/plugin_bridge.py` 在运行时用 `importlib` 加载
  `wows_coyote/main.py`；打包后路径是 `_internal/wows_coyote/main.py`。
- `standalone/engine.py` 只覆写三个输出点：
  `_send_strength`、`_play_waveform`、`_arbitrate_waveforms`。
- 共享 `base.json` / `hit.json` 函数表、`wows_preset_v2.json` 预设和配置 schema。
- 改动 `wows_coyote/main.py` 的公开入口/属性前，必须同时跑：
  `tests/test_plugin.py`、`tests/test_standalone_*.py`。
- 版本线：DGHub 插件是 `2.9.x`，standalone 是 `0.1.x`；两者独立发版。

---

## 3. 发布物与渠道

| 文件 | 内容 | 给谁 | 校验 |
|---|---|---|---|
| `releases/wows_coyote-1.0.0-gpl.zip` | 源码 + 许可证 + 构建脚本 + 随包模板 | GPL 对应源码、二次开发者 | 同名 `.sha256`、`SHA256SUMS.txt` |
| `releases/wows_coyote-1.0.0-win64.zip` | `wows_coyote.exe` + `_internal/` + `source/` + 许可证 | 不想装 Python 的用户 | 同名 `.sha256`、`SHA256SUMS.txt` |
| `dist_installer/wows_coyote-1.0.0-setup.exe` | 安装包（最低权限，装到 `%LOCALAPPDATA%\Programs\wows_coyote`） | 普通用户 | 同名 `.sha256` |
| `releases/SHA256SUMS.txt` | 源码包 + 便携包的汇总校验 | 所有下载页 | — |
| `dist_installer/GITHUB_RELEASE_NOTES.md` | GitHub Release 文案模板 | 发布者 | 内含安装包 SHA256/体积 |
| `dist/wows_overlay.exe`（可选） | 独立置顶小窗，仅浏览器模式需要 | 需要 overlay 的用户 | — |

发布渠道建议：

1. **GitHub Release**：安装包 + 便携包 + 源码包 + `SHA256SUMS.txt`；
2. 其他镜像只放安装包/便携包，并链回 GitHub 的源码包与校验值；
3. 不要把 `dist/`、`build/`、日志、`qr.svg` 或整个工作目录上传。

---

## 4. 构建

### 4.1 前置

- Windows 10/11 x64；
- Python 3.13；
- `py -3.13 -m pip install -r standalone/requirements.txt`；
- `py -3.13 -m pip install pyinstaller`；
- Inno Setup 6/7（只需构建安装包时安装）。

### 4.2 一条龙

在项目根目录执行：

```bat
py -3.13 tools/make_release.py
py -3.13 tools/audit_release.py
```

`make_release.py` 会读取 `standalone/__init__.py` 的版本，依次构建源码包、
exe、便携包和安装包，刷新校验值，并生成 `releases/latest.json`；版本号
不一致时直接失败。它不会自动上传。

手动分步的等价命令：

```bat
py -3.13 tools/build_windows.py
"C:\Program Files\Inno Setup 7\ISCC.exe" /DMyAppVersion=1.0.0 installer\wows_coyote.iss
:: Inno Setup 6 的默认路径：
:: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.0.0 installer\wows_coyote.iss
py -3.13 tools/finalize_release.py
py -3.13 tools/audit_release.py
```

`build_windows.py` 会依次：

1. 运行 `tools/build_standalone_zip.py` 生成源码包；
2. 用 PyInstaller 构建 `dist/wows_coyote/wows_coyote.exe`；
3. 把 `LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`、`CHANGELOG.md`、
   `README.md`、GPL 源码 zip 放进分发目录；
4. 生成 `releases/wows_coyote-1.0.0-win64.zip` 及 `.sha256`。

安装包编译后，`finalize_release.py` 会统一刷新：

- `releases/*.sha256`；
- `dist_installer/*.sha256`；
- `releases/SHA256SUMS.txt`；
- `dist_installer/GITHUB_RELEASE_NOTES.md` 里的 SHA256 和体积。

`make_release.py` 随后生成 `releases/latest.json`，内容包含版本、简短说明、
安装包大小/SHA256/下载地址和源码包地址。

### 4.3 只重建某一种

```bat
:: 只重建源码包
py -3.13 tools/build_standalone_zip.py

:: 只重建 exe + 便携包（不重跑源码包）
py -3.13 tools/build_windows.py --no-source-zip

:: 额外构建独立 overlay exe（需要 pywebview）
py -3.13 tools/build_windows.py --overlay

:: 安装包
"C:\Program Files\Inno Setup 7\ISCC.exe" installer\wows_coyote.iss
```

### 4.4 版本号

当前是 `1.0.0`。`standalone/__init__.py` 是唯一版本源；
`tools/build_windows.py`、`tools/build_standalone_zip.py` 和
`standalone/wows_coyote.spec` 都直接读取它。每次发版仍需人工确认：

| 文件 | 位置 |
|---|---|
| `standalone/__init__.py` | `__version__`（唯一源） |
| `installer/wows_coyote.iss` | `MyAppVersion` 默认值（`make_release.py` 会用 `/D` 覆盖） |
| `CHANGELOG.md` | `## standalone X.Y.Z` 段落 |
| `README.md` / `releases/README.md` | 发布包名 |
| `dist_installer/GITHUB_RELEASE_NOTES.md` | GitHub Release 正文 |
| `dist_installer/UPDATE_NOTES.md` | 客户端更新提示里的简短说明 |

`tools/make_release.py` 会检查这些位置一致，不一致直接失败。

---

## 5. 发布前审计

### 5.1 一键运行

```bat
py -3.13 tools/audit_release.py
```

- 退出码 `0` 表示没有 FAIL；
- `--strict` 让 REVIEW 也失败；
- `--no-archives` 只审计源码发布集；
- `--archive <zip>` 追加指定 zip。

### 5.2 它会检查什么

1. **源码发布集**：与 `build_standalone_zip.collect_entries()` 完全一致，
   避免“审计的”和“实际打包的”不是同一批文件；
2. **发布归档**：`releases/*-gpl.zip`、`releases/*-win64.zip`，
   并递归检查便携包内嵌的 GPL 源码包；
3. **安装包原始字节**：`dist_installer/wows_coyote-1.0.0-setup.exe`；
4. **禁止内容**：`qr.svg`、`*.log`、`settings.json`、`*.pyc/.pyo`、
   `__pycache__`、`.env`、私钥、数据库文件、`__pycache__` 目录等；
5. **敏感特征**：开发机绝对路径、`C:\Users\<用户名>`、邮箱、手机号、
   赋值型 token/password/secret、私钥头、二维码里的 `tid=` 配对 ID；
6. **完整性**：`LICENSE` / `THIRD_PARTY.md` / `SOURCE_OFFER.md`、
   随包模板 manifest、便携包内嵌源码 zip；
7. **校验和**：`SHA256SUMS.txt`、各 `.sha256` 与实际文件是否一致；
8. **更新清单**：`releases/latest.json` 的 version/size/sha256 与实际安装包一致。

### 5.3 当前基线（1.0.0）

最近一次完整审计：

```text
[源码发布集] 73 个文件
[归档] wows_coyote-0.1.0 … 0.1.5（历史回退包）
[归档] wows_coyote-1.0.0-gpl.zip
[归档] wows_coyote-1.0.0-win64.zip（含 2 份内嵌源码包）
[安装包] wows_coyote-1.0.0-setup.exe
[校验和] 已核对 34 个校验值
[更新清单] 1.0.0 / size / sha256 与实际安装包一致

FAIL 0   REVIEW 0   INFO 2
结论：通过
```

两条 INFO 是工作区里的 `standalone/qr.svg` 和
`wows_coyote/wows_coyote.log`；它们已被发布集排除，仅提醒不要整目录打包。

### 5.4 自动化不能替代的人工检查

- [ ] 干净 Windows（无 Python）安装、启动、扫码、卸载；
- [ ] 真机 10 局：无崩溃、无意外输出、clear 无尾音；
- [ ] 急停：F12、关闭主窗口、`Ctrl+C` 都能 clear + 归零；
- [ ] 首次使用在 App 内把通道上限调低（如 20–40）；
- [ ] GPL 合规：安装目录/发布页有 `LICENSE`、`THIRD_PARTY.md`、
      `SOURCE_OFFER.md` 和源码 zip；
- [ ] 商业发布前取得 DG-LAB 授权（GPL 不替代厂商授权）；
- [ ] 源码包不含维护者部署手册、服务器绝对路径、其他项目/宿主环境信息；
- [ ] 发布页文案包含安全提示和 SHA256；
- [ ] 如需代码签名，签名后再重新生成 `.sha256` 和发布说明；
- [ ] 人工看一眼发布页截图/图标，确认没有桌面通知、用户名、二维码等信息。

---

## 6. 本次敏感信息清理记录

| 文件 | 问题 | 处理 |
|---|---|---|
| `assets/brand/generate.py` | 硬编码开发机 `C:\Users\<用户名>\Downloads\...` 参考图路径（两处） | 改为 `WOWS_ICON_REFERENCE` 环境变量；缺失时跳过 IoU/参考图贴片 |
| `docs/官方SDK模板用法.md` | “调研文件（本机临时目录）”含用户名和临时目录 | 改为“未随仓库分发” |
| `docs/干净环境安装测试.md` | 开发机盘符路径、开发机用户名 | 改为 `<项目目录>` / “开发机用户目录” |
| `docs/本地标准用户干净环境测试.md` | 开发机盘符路径、开发机用户名、示例密码 | 改为 `<项目目录>` / `<自定义临时密码>` |
| `docs/VirtualBox干净环境测试.md` | 开发机盘符路径、示例密码 | 改为 `<项目目录>` / `<自定义临时密码>` |
| `standalone/README.md` | 文档里的二维码示例被审计误判为真实配对 ID | 改为 `tid=<targetId>` 占位写法 |

确认**没有**发现：

- API key、访问令牌、私钥、证书、`.env`；
- 真实二维码配对 ID（`qr.svg` 是运行时产物，已排除）；
- 用户设置文件、日志、缓存、崩溃转储；
- 本机用户名/临时目录/开发盘路径（已清理）；
- 埋点、遥测、第三方统计（程序只连本机 UI 与 V4 Relay）。

说明：

- `installer/ChineseSimplified.isl` 里有第三方译者的公开邮箱
  `847320916@qq.com`，属于 MIT 语言文件的署名，保留；
  审计已将它加入白名单。
- `tools/verify_waveform/main.py` 读取 `DGHUB_TOKEN` 环境变量，但该工具
  不在源码发布集内，也没有把 token 写入文件。

---

## 7. 发布到 GitHub / 分发

1. 确认工作区干净，跑完测试；
2. 运行 `py -3.13 tools/make_release.py`（构建 + 刷新校验值 + 生成 `latest.json`）；
3. 运行 `py -3.13 tools/audit_release.py`；
4. 打 tag（如 `v1.0.0`）；
5. GitHub Release 上传：
   - `dist_installer/wows_coyote-1.0.0-setup.exe`
   - `dist_installer/wows_coyote-1.0.0-setup.exe.sha256`
   - `releases/wows_coyote-1.0.0-win64.zip`
   - `releases/wows_coyote-1.0.0-gpl.zip`
   - `releases/SHA256SUMS.txt`
6. 正文直接使用 `dist_installer/GITHUB_RELEASE_NOTES.md`；
7. 下载一次发布物，重新核对 SHA256 与 `releases/latest.json` 一致；
8. 更新清单上传（维护者本地流程）：
   `WOWS_UPDATE_HOST=root@<服务器IP> WOWS_UPDATE_PATH=<站点根>/wows_coyote tools/deploy_manifest.sh releases/latest.json`；
   服务器目录按实际站点填写，不写进源码包；
9. 如果做了代码签名，重复第 2 步并重新上传；
10. 不要上传 `dist/`、`build/`、`.phase0deps/`、`*.log`、`qr.svg`。

> 当前安装包未做代码签名，SmartScreen 可能提示；发布说明已写明
> “以 SHA256 为准”。

---

## 8. 拆成独立仓库（可选）

当前是 monorepo，standalone 与 DGHub 插件共用 `wows_coyote/main.py`。
如果要把 standalone 单独发布/独立仓库：

### 方案 A：保持目录结构拆出（推荐）

新建仓库，复制以下内容并保持相对路径：

```text
standalone/
wows_coyote/main.py
wows_coyote/manifest.json
wows_coyote/base.json
wows_coyote/hit.json
wows_coyote/wows_preset_v2.json
wows_coyote/README.md
assets/brand/
tools/build_standalone_zip.py
tools/build_windows.py
tools/audit_release.py
tools/finalize_release.py
tools/build_official_manifest.py
installer/
docs/（发布指南和随包文档）
LICENSE
THIRD_PARTY.md
SOURCE_OFFER.md
CHANGELOG.md
```

这样 `plugin_bridge.py` 的 `ROOT/wows_coyote/main.py` 路径和
`build_*.py` 的 `ROOT` 假设都不需要改。

### 方案 B：保留 monorepo，只把发布物放 GitHub Release

不拆仓库，CI/本地构建后上传 Release。改动最小，但源码包和 DGHub 插件
共享同一个 GPL-3.0 仓库。

### 拆分注意事项

- `wows_coyote/main.py` 是共享引擎：拆出后要么复制并定期同步，
  要么用 git subtree/submodule；不要两边各改一套。
- 整仓库目前是 GPL-3.0；如果 DGHub 插件要闭源，必须把它拆到另一个
  仓库并单独确定许可证，不能沿用本仓库的 GPL 文本。
- 拆分后更新 `SOURCE_OFFER.md` 的仓库地址和 `THIRD_PARTY.md` 的来源链接。
- `build_standalone_zip.py` 的 `ROOT` 假设项目根目录下同时有
  `standalone/` 和 `wows_coyote/`；拆分时保持这个布局。

---

## 9. 已知限制

- 安装包和 exe 未签名；
- 窗口模式依赖 WebView2 Runtime（Win10/11 通常自带，缺失时回退浏览器）；
- 不支持 `.pulse` 直接转换；
- 无托盘、自动更新、单实例；
- 真实设备 10 局长测、官方 V4 Relay 与真实 App 的兼容性仍需真机验收；
- 商业使用需 DG-LAB 事先授权。

---

## 10. 回滚

1. 保留上一版本的 3 个发布物和 `.sha256`；
2. 回滚时重新上传旧版本，并在 Release 说明里写明原因；
3. 用户卸载不会删除 `%LOCALAPPDATA%\wows_coyote\settings.json` 和日志，
   需要时手动清理；
4. 降级前备份设置文件，避免新旧版本配置键不兼容。

---

## 11. 发布检查清单（可复制到 issue）

```text
[ ] tests/test_plugin.py 通过
[ ] tests/test_standalone_*.py 通过
[ ] 版本号在 __init__.py / build 脚本 / iss / CHANGELOG 一致
[ ] tools/build_windows.py 成功
[ ] Inno Setup 编译成功
[ ] tools/finalize_release.py 成功
[ ] tools/audit_release.py：FAIL 0 / REVIEW 0
[ ] 3 个发布物 + SHA256SUMS.txt 已上传
[ ] 发布说明里的 SHA256/体积与安装包一致
[ ] 干净 Windows 安装/启动/卸载通过
[ ] 真机 10 局通过
[ ] F12 / 关闭主窗口 / Ctrl+C 均归零
[ ] 发布页含 LICENSE/THIRD_PARTY/SOURCE_OFFER 与源码 zip
[ ] 商业发布已取得 DG-LAB 授权
```
