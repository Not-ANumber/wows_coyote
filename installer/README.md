# Windows 安装包

## 构建 exe

```bash
py -3.13 -m pip install pyinstaller
py -3.13 tools/build_windows.py
```

输出：

```text
dist/wows_coyote/wows_coyote.exe
dist/wows_coyote/_internal/...
dist/wows_coyote/_internal/source/wows_coyote-1.0.0-gpl.zip
```

如果需要独立置顶 overlay：

```bash
py -3.13 -m pip install pywebview
py -3.13 tools/build_windows.py --overlay
```

输出 `dist/wows_overlay.exe`，安装脚本会把它复制到主程序目录；主程序 `--overlay` 会启动它。

## 构建安装包

推荐直接运行 `py -3.13 tools/make_release.py`：它会构建源码包、exe、便携包，
并按 `standalone/__init__.py` 的版本编译安装包、刷新校验值和生成
`releases/latest.json`。

手工编译：

1. 安装 [Inno Setup 6 或更新版本](https://jrsoftware.org/isinfo.php)（本机使用
   Inno Setup 7）；
   `installer/ChineseSimplified.isl` 已随仓库提供（MIT，来源
   [kira-96/Inno-Setup-Chinese-Simplified-Translation](https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation)），
   编译时直接使用，不依赖 Inno 自带语言包。该文件标注 Inno Setup 6.5.0+；
   如果编译器更旧，请用对应版本的 `.isl` 替换。
2. 打开 `installer/wows_coyote.iss`，或命令行（版本默认取 iss 内的
   `MyAppVersion`，可用 `/D` 覆盖）：
   ```bat
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.1.1 installer\wows_coyote.iss
   :: Inno Setup 7（本机）：
   "C:\Program Files\Inno Setup 7\ISCC.exe" /DMyAppVersion=0.1.1 installer\wows_coyote.iss
   ```
3. 输出：
   ```text
   dist_installer/wows_coyote-1.0.0-setup.exe
   ```

安装包会包含：

- `wows_coyote.exe` 和 `_internal`；
- 随包 24 条官方模板；
- `LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`；
- `source/wows_coyote-1.0.0-gpl.zip`（GPL 对应源码）；
- 可选 `wows_overlay.exe`。

程序、窗口、快捷方式和卸载项使用黄黑盾锚标（嵌入 exe）；安装包本身也通过
`SetupIconFile=..\assets\brand\wows-icon.ico` 使用同一图标。

安装到 `%LOCALAPPDATA%\Programs\wows_coyote`，默认最低权限，不需要管理员。

## 干净 Windows 验收

详见 [`docs/干净环境安装测试.md`](../docs/干净环境安装测试.md)（当前 Home 版用新标准用户）；
方案 A 完整步骤见 [`docs/本地标准用户干净环境测试.md`](../docs/本地标准用户干净环境测试.md)；
VirtualBox 完整设置见 [`docs/VirtualBox干净环境测试.md`](../docs/VirtualBox干净环境测试.md)。
简版：

- 无 Python / 无开发环境的 Win10/11；
- 安装 → 启动 → 扫码 → 进游戏 → F12 急停 → 关闭 UI 归零 → 卸载；
- 检查防火墙弹窗、中文/空格路径、多网卡、手机重连；
- 检查安装目录内 `source`、`LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`。
