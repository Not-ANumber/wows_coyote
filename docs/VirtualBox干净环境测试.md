# VirtualBox 干净 Windows 安装测试

> 适用：当前机器（Windows 11 Home，x64），用 VirtualBox 建一个全新 Windows，
> 测试 `wows_coyote-1.0.0-setup.exe` 的安装、首次启动、扫码、F11/F12、卸载。
>
> 目标不是“虚拟机里能跑”，而是验证：**无 Python、无开发环境、普通用户权限、全新用户配置**。

---

## 1. 安装 VirtualBox

### 1.1 安装

```powershell
winget install --id Oracle.VirtualBox -e
```

或从 <https://www.virtualbox.org/wiki/Downloads> 下载 Windows 版。

- 建议 VirtualBox **7.1.x**；
- 需要管理员权限；
- 安装后如果提示重启，重启；
- Extension Pack 不是必需（本项目不用 USB/VRDP）。

### 1.2 与 Hyper-V / 内存完整性冲突

当前机器 `HypervisorPresent=True`，可能启用了 VBS/内存完整性。
VirtualBox 7.1 可以用 Windows Hypervisor Platform 运行，但性能会下降；如果报
`VERR_NEM_MISSING_KERNEL_API` / `0x80004005`：

1. Windows 安全中心 → 设备安全性 → 内核隔离 → **内存完整性** 关闭；
2. 管理员 PowerShell：
   ```powershell
   bcdedit /enum {current} | findstr hypervisorlaunchtype
   ```
   如果显示 `auto`，且你不需要 WSL2/Hyper-V，可临时：
   ```powershell
   bcdedit /set hypervisorlaunchtype off
   ```
   重启后再试；测试完恢复 `auto`。
3. 仍失败就换 VMware Workstation Player。

---

## 2. 准备 Windows ISO

推荐先用 **Windows 10 22H2 x64**，安装最简单；如果要验证 Win11 专属行为，再用
Windows 11 24H2（需要 EFI + TPM 2.0）。

- 只用微软官方 ISO，不要“精简版/Ghost”；
- 记录 ISO 来源和 SHA256；
- 测试不需要激活，桌面水印不影响。

---

## 3. 创建虚拟机

VirtualBox 主界面 → **新建**：

| 项 | 建议 |
|---|---|
| 名称 | `wows-clean-win10` 或 `wows-clean-win11` |
| 类型/版本 | Microsoft Windows / Windows 10 (64-bit) 或 Windows 11 (64-bit) |
| 内存 | 4096 MB（最低 3072） |
| 处理器 | 2–4 vCPU |
| 硬盘 | 50 GB，VDI，动态分配 |
| 网络 | **桥接网卡**（见第 4 节） |

创建后 → **设置**：

### 系统

- Motherboard：Windows 11 勾选 **Enable EFI**；Windows 10 用默认 BIOS 即可；
- Processor：Enable PAE/NX 默认；2–4 CPU；
- TPM：Windows 11 选 **TPM 2.0**；Windows 10 不需要；
- 硬件虚拟化：默认开启。

### 显示

- 显存 128 MB；
- 图形控制器 VMSVGA；
- 3D 加速关闭（安装测试不需要）。

### 网络

- Adapter 1：**Bridged Adapter**；
- Name：选当前上网的网卡（Wi-Fi 或以太网；有线更稳）；
- Advanced → Cable Connected 勾选；
- Promiscuous Mode：Allow All（部分 Wi-Fi 驱动需要）。

> 桥接后虚拟机会从路由器拿一个和手机同网段的 IP，手机才能访问虚拟机的
> V3 WebSocket 服务。
> 如果 Wi-Fi 桥接失败，用第 6 节的 NAT 端口转发。

---

## 4. 安装 Windows

1. 设置 → Storage → 光驱 → 选择 Windows ISO；
2. 启动虚拟机，按提示安装；
3. Windows 10：创建本地账户；Windows 11：OOBE 阶段可用 `Shift+F10` 打开命令行，
   尝试 `oobe\bypassnro` 或 `start ms-cxh:localonly` 创建本地账户；
   如果 24H2 不允许，先用 Microsoft 账户完成 OOBE，再建本地标准用户；
4. 进入桌面后：
   - 确认网络为“专用网络”：
     ```powershell
     Get-NetConnectionProfile
     Set-NetConnectionProfile -NetworkCategory Private
     ```
   - 确认没有 Python：
     ```powershell
     where.exe python
     where.exe py
     ```
     干净 Windows 应该都没有输出。

> 先不要装任何开发工具、Python、Git、VS、WSL。
> 可选安装 Guest Additions（见第 5 节），只为共享文件夹/剪贴板，不是运行依赖。

---

## 5. 把安装包放进虚拟机

### 方式 A：Guest Additions + 共享文件夹（推荐）

1. 虚拟机菜单 → Devices → Insert Guest Additions CD image；
2. 在虚拟机里运行 `VBoxWindowsAdditions.exe`，重启；
3. 虚拟机设置 → Shared Folders → 添加：
   ```text
   Folder Path: <项目目录>\dist_installer
   Folder Name: installer
   Auto-mount: 勾选
   Read-only: 勾选
   ```
4. 虚拟机里打开：
   ```text
   \\VBOXSVR\installer
   ```
   或映射网络驱动器。

### 方式 B：宿主机 HTTP（不装 Guest Additions）

宿主机：

```powershell
cd <项目目录>
py -3.13 -m http.server 8000 --bind 0.0.0.0 --directory dist_installer
```

Windows 防火墙允许 Python 专用网络。虚拟机 Edge 打开：

```text
http://<宿主机局域网IP>:8000/
```

下载 `wows_coyote-1.0.0-setup.exe`。

---

## 6. 手机访问虚拟机的网络

### 6.1 桥接（首选）

虚拟机里：

```powershell
ipconfig
```

确认 IPv4 与手机/宿主机同网段（例如都是 `192.168.1.x`）。
手机 App 扫码后直接连虚拟机 IP；二维码由程序自动生成。

如果手机连不上：

- 虚拟机 Windows 防火墙：允许 `wows_coyote.exe` 的 **专用网络** 入站；
  标准用户可能无法点“允许”，可先用管理员账户放行一次；
- 确认虚拟机网络配置文件是 Private，不是 Public；
- 确认路由器没有开启 AP 隔离/访客隔离；
- Wi-Fi 桥接对部分网卡不稳定，换有线网卡或改用 NAT 转发。

管理员 PowerShell 预放行示例：

```powershell
New-NetFirewallRule -DisplayName "wows_coyote" `
  -Direction Inbound -Action Allow -Profile Private `
  -Program "C:\Users\<用户>\AppData\Local\Programs\wows_coyote\wows_coyote.exe"
```

### 6.2 NAT 端口转发（桥接失败时）

VirtualBox → 设置 → 网络 → Adapter 1 → 高级 → 端口转发：

| 名称 | 协议 | 主机端口 | 子系统端口 |
|---|---|---:|---:|
| dglab | TCP | 19999 | 19999 |
| ui | TCP | 18888 | 18888 |

虚拟机里启动时指定固定端口和宿主机 IP：

```bat
wows_coyote.exe --port 19999 --ui-port 18888 --advertise-ip <宿主机局域网IP>
```

- 二维码里是 `宿主机IP:19999`，手机会连宿主机，由 VirtualBox 转发到虚拟机；
- 宿主防火墙要允许 19999/18888 入站；
- UI 在宿主机 `http://<宿主IP>:18888`。

---

## 7. 干净用户测试

Windows 里默认第一个账户是管理员。为了验证“不需要管理员安装”，在虚拟机里再建标准用户：

```powershell
net user wows_clean "<自定义临时密码>" /add
net localgroup Users wows_clean /add
```

注销，用 `wows_clean` 登录，然后：

```powershell
where.exe python
where.exe py
Test-Path "$env:LOCALAPPDATA\wows_coyote\settings.json"
```

- 没有 Python；
- settings.json 不存在；
- 安装包从 `\\VBOXSVR\installer` 或下载目录运行。

安装：

- 不弹 UAC；
- 中文向导；
- 安装到：
  ```text
  C:\Users\wows_clean\AppData\Local\Programs\wows_coyote
  ```
- 检查 `source\wows_coyote-1.0.0-gpl.zip`、`LICENSE`、`THIRD_PARTY.md`。

首次启动：

- 直接进入主控制台（无 `/setup` 向导）；
- 设置页「遥测文件」指定路径；每次启动重新扫码配对；
- 主控制台底部「安全使用」可展开；强度上限以 App/设备通道上限为准；
- 手机与虚拟机同一局域网；
- F12 全局急停；
- 关闭主窗口归零并退出；控制台/设置/事件标签切换不归零；关闭悬浮窗只关窗口、不归零；
- 设置页改遥测路径/事件参数，重启仍保留。

---

## 8. 测试完成/清理

1. 卸载 `wows_coyote`；
2. 确认程序目录删除，`%LOCALAPPDATA%\wows_coyote` 保留（可手动清理）；
3. 关闭虚拟机 → 可拍快照/回滚：
   - 安装前：`clean-windows` 快照；
   - 测试后：恢复快照，保证下次仍是干净 Windows；
4. 不需要虚拟机时：VirtualBox 删除虚拟机 → 删除所有文件；
5. 如果改过 `hypervisorlaunchtype`，恢复：
   ```powershell
   bcdedit /set hypervisorlaunchtype auto
   ```
   重启。

---

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `VERR_NEM_MISSING_KERNEL_API` | Hyper-V/VBS 冲突；关闭内存完整性/Hyper-V，或换 VMware |
| 虚拟机无法从 ISO 启动 | 检查 Storage 光驱、启动顺序、EFI/TPM 设置 |
| 虚拟机没有网络 | 桥接选错网卡；Cable Connected；换 NAT 测试 |
| 手机连不上虚拟机 | 虚拟机防火墙、Private 网络、AP 隔离、桥接 Wi-Fi 不稳定；用 NAT 转发 |
| 标准用户不能放行防火墙 | 管理员先建入站规则；再切回标准用户 |
| 安装包 SmartScreen 拦截 | 未签名；VM 里“更多信息 → 仍要运行”；正式发布考虑签名 |
| 共享文件夹看不到 | Guest Additions 没装；改用 HTTP 下载 |
| 时间/剪贴板不同步 | 装 Guest Additions；测试不影响 |
