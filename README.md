# wows_coyote

战舰世界（World of Warships）遥测驱动的 DG-LAB 郊狼控制程序，Windows x64
standalone。支持 V4 Relay（DG-LAB 4）和 V3 Socket（郊狼 App）。

> 第一次使用请按 [初次使用完整流程](docs/初次使用完整流程.md) 操作：
> 安装前置 Mod → 验证遥测 → 安装程序 → 扫码 → 低强度测试。

## 前置 Mod（必需）

本程序不直接读取游戏内存，而是读取 **ExportShipStatus** Mod 写出的
`profile/wows_telemetry.jsonl`。请先安装：

**https://github.com/Not-ANumber/ExportShipStatus-Mod**

安装后进入游戏，确认游戏根目录下 `profile/wows_telemetry.jsonl` 有更新；
再在程序设置页选择该文件、`profile` 目录或游戏根目录。

## 下载与安装

最新版本见 [Releases](https://github.com/Not-ANumber/wows_coyote/releases/latest)：

| 文件 | 说明 |
|---|---|
| `wows_coyote-0.1.1-setup.exe` | Windows x64 安装包（推荐），无需管理员权限 |
| `wows_coyote-0.1.1-win64.zip` | 免安装便携版 |
| `wows_coyote-0.1.1-gpl.zip` | GPL-3.0 对应源码 |

安装包默认安装到 `%LOCALAPPDATA%\Programs\wows_coyote`。

## 快速开始

1. 安装前置 ExportShipStatus Mod；
2. 安装/解压并启动 `wows_coyote`；
3. 设置页选择遥测文件（通常会自动查找）；
4. 选择 V4 Relay 或 V3 Socket，扫码配对；
5. 首次使用先在 App 内把通道上限调低，`F12` 为全局急停。

## 更新

0.1.1 起支持应用内更新提示（仅提示，不自动下载/安装）。
0.1.0 用户需要手动安装一次 0.1.1。

更新清单：<https://wowsdg-update.woyiba.com/wows_coyote/latest.json>

## 安全

- 输出强度以 App/设备上报的通道上限为准；
- 关闭主窗口、正常退出或 `Ctrl+C` 都会 clear 并归零；
- 请从低强度开始，遵守 DG-LAB 使用条款和当地法律法规。

## 许可

GPL-3.0。第三方组件与波形来源见发布包内 `LICENSE`、`THIRD_PARTY.md`、
`SOURCE_OFFER.md`。
