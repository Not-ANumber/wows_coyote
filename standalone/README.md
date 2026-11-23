# standalone（Phase 1–4）：核心适配层 + 调度器 + V3/V4 传输 + UI

目标：不连接 DGHub，把“现有事件数学”接到新的输出抽象上，
用 **owner / priority / replace / clear / loop** 替代 DGHub 的 3 秒重发/波形覆盖；
Phase 3 接 V3 Socket，Phase 4 接主控制台 + 设置标签页 + overlay；
V4 在 Phase 3 传输层之上新增 Relay 控制方与本机中继。

```text
wows_coyote/main.py（现有引擎，不改）
  └─ StandaloneEngine（只覆写输出点）
       ├─ DeviceSink.set_strength
       └─ WaveformScheduler
            ├─ 每通道选赢家（复用 MixerEngine._pick_wave）
       ├─ 赢家变化 → clear 旧 owner + play 新模板
       ├─ loop 模板 → play(loop=True)，由 DeviceSink.pump() 补帧直到 clear
       └─ 一次性模板 → pending 节流，窗口结束最多补播一次
```

> 模板默认来源：**随包 `standalone/templates/official_manifest.json`**
> （来源 dglab-kit-python，GPL-3.0）。没有可用模板时 fail-closed，只输出强度。
> V3 传输见 Phase 3，V4 见 [`docs/standalone_V4.md`](../docs/standalone_V4.md)，
> UI 见 Phase 4。

---

## 官方模板加载（Phase 2 / 分发方案 B）

`standalone/official_templates.py` 支持四种只读来源：

1. **bundled**（默认）：随包的 `standalone/templates/official_manifest.json`；
2. **sdk**：用户提供的 `dglab-kit-python` 源码目录；
3. **manifest**：外部 `source/commit/license/sha256/waves[]` 清单；
4. **placeholder/none**：开发用占位或完全关闭波形。

项目以 **GPL-3.0** 分发，随包 manifest 含 24 条官方模板（commit 见
`standalone/templates/README.md`）。许可/源码声明见根目录 `LICENSE`、
`THIRD_PARTY.md`、`SOURCE_OFFER.md`。

```bash
# 默认随包（不需要 clone；桌面窗口需要 pywebview）
py -3.13 standalone/run.py
py -3.13 standalone/run.py --list-templates

# 可选：直接用 SDK 源码（更新/对比时）
git clone https://github.com/dungeonlab-open/dglab-kit-python.git E:\dglab-kit-python
py -3.13 standalone/run.py --templates sdk

# 可选：外部 manifest
py -3.13 standalone/run.py --templates manifest \
  --template-manifest official_manifest.json

# 覆盖某个事件的模板 id
--base-template RAINFALL --hit-template PULSATING --fire-template GRAINY \
--flood-template TIDE --death-template COMPRESS
```

默认映射（按官方模板名称启发式）：

```text
base=RAINFALL(雨水冲刷)  hit=PULSATING(连击)  fire=GRAINY(颗粒摩擦)
flood=TIDE(潮汐)        death=COMPRESS(压缩)
```

随包事件默认值统一放在 `standalone/presets/default_events.json`
（五类事件的启用、强度、阈值、时长、通道、波形和函数表开关）。
`load_config()` 的初始化顺序：

```text
manifest schema 默认值
  → standalone/presets/default_events.json（事件参数）
  → bundled / --preset 预设（显式自定义预设可覆盖事件参数）
  → %LOCALAPPDATA%\wows_coyote\settings.json（用户设置，最高优先级）
```

`wows_preset_v2.json` 继续提供轮询/超时等基础参数；它的旧版中文波形名
不再覆盖 `default_events.json` 已定义的事件字段。默认 JSON 缺失或损坏时
回退到 schema + bundled preset，并按原启发式映射模板。

fail-closed 规则：

- manifest 缺 `source`/`license`、任一帧非法、`sha256` 不匹配 → 整份拒绝；
- SDK 单条非法 → 跳过并记录；全部无效 → 空库；
- 空库时只输出强度，不播放波形。

**重新生成随包 manifest：**

```bash
py -3.13 tools/build_official_manifest.py ^
  --sdk-path "E:\dglab-kit-python" ^
  --commit 4cce771c0fd93d043f3e83bbb0467e4197cde62e ^
  --out standalone/templates/official_manifest.json
```

**构建分发 zip：**

```bash
py -3.13 tools/build_standalone_zip.py
# releases/wows_coyote-1.0.0-gpl.zip (+ .sha256)
```

发布包包含 `LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`、随包 manifest、
standalone 源码和它依赖的 `wows_coyote` 引擎文件。

---

## 1. 运行模拟台

```bash
py -3.13 standalone/simulate.py
py -3.13 standalone/simulate.py --events       # 打印全部 play/clear/strength
```

输出示例（节选）：

```text
t= 0.05s  A= 10.0% B= 10.0%  winner=A:base/RAINFALL B:base/RAINFALL  满血快照
t= 0.15s  A= 22.0% B= 22.0%  winner=A:fire/GRAINY B:fire/GRAINY  起火
t= 1.51s  A= 62.0% B= 62.0%  winner=A:hit/PULSATING B:hit/PULSATING  受击窗口结算
t= 3.90s  A= 22.0% B= 22.0%  winner=A:fire/GRAINY B:fire/GRAINY  受击结束，回落到火
t= 4.00s  A= 10.0% B= 10.0%  winner=A:base/RAINFALL B:base/RAINFALL  灭火
t= 4.10s  A= 60.0% B= 60.0%  winner=A:death/COMPRESS B:death/COMPRESS  阵亡
t= 8.20s  A=  0.0% B=  0.0%  winner=A:- B:-  死亡持续结束
```

（示例假设没有 `settings.json` 用户覆盖；已有用户设置时以设置文件为准。）

它展示 Phase 1 的验收点：每通道 total、当前 owner、当前模板、clear 时机。

---

## 2. 测试

```bash
py -3.13 tests/test_official_templates.py
py -3.13 tests/test_standalone_phase1.py
py -3.13 tests/test_standalone_v3.py
py -3.13 tests/test_standalone_v4.py
py -3.13 tests/test_standalone_parity.py
py -3.13 tests/test_standalone_ui.py
py -3.13 tests/test_event_presets.py
py -3.13 tests/test_config_defaults.py
py -3.13 tests/test_standalone_desktop.py
```

覆盖：

- 基础 → 起火 → 受击 → 火 → 基础 → 死亡 → 归零；
- 起火模板的 loop：`play(loop=True)` 后 sink 持续 `loop_refill`，灭火/被抢占时 clear 停止；
- 受击播放期间再次受击只 pending，窗口结束后最多补播一次；
- 赢家变化时 clear 旧 owner；
- 战局结束/stop 归零；
- `TelemetryReader` 从临时 JSONL 读取 `battle_start`/`snapshot` 后驱动引擎；
- 事件方案命名校验、保存/读取/删除、导入导出与跨机器缺失资源降级；
- 事件默认 JSON 完整性、初始化顺序、用户设置/自定义预设覆盖、坏文件回退；
- 确保没有发出 DGHub 的 `set_strength` / `trigger`。

---

## 3. 文件

| 文件 | 作用 |
|---|---|
| `plugin_bridge.py` | 用 importlib 加载现有 `wows_coyote/main.py`，不启动 DGHub |
| `config.py` | schema 默认值 + 事件默认 JSON + bundled preset + 用户设置 |
| `presets/default_events.json` | 随包五类事件默认值（启用/强度/阈值/时长/通道/波形） |
| `engine.py` | `StandaloneEngine`：覆写 `_send_strength`、`_play_waveform`、`_arbitrate_waveforms` |
| `device.py` | `DeviceSink` 接口 + `FakeDevice` |
| `templates.py` | `Template` / `TemplateLibrary`；只读、校验 |
| `event_presets.py` | 事件配置方案：命名、校验、读写、列表、删除 |
| `official_templates.py` | 官方 SDK/manifest 加载、fail-closed、默认事件映射 |
| `scheduler.py` | 每通道 owner/priority/replace/clear/loop/pending |
| `host.py` | 引擎 tick + 调度器 tick |
| `telemetry.py` | 复用现有 `TelemetryReader`，把 JSONL 喂给 standalone 引擎 |
| `v3_device.py` | V3 Socket 传输：WS 服务/QR/bind/strength/pulse/clear/心跳/反馈/急停 |
| `v4_device.py` | V4 Relay 控制方：hello/attach/device.op/device.op.clear/重连 |
| `v4_relay.py` | 本机 V4 中继：控制方/App 路由、hello、ping、heartbeat、idle timeout |
| `hotkeys.py` | Windows 全局热键（急停默认 F12，悬浮窗锁定默认 END，GetAsyncKeyState 轮询） |
| `run.py` | Phase 3/4 宿主入口：V3/V4 设备 + TelemetryRunner + 调度器 + UI |
| `desktop.py` | pywebview 主窗口（控制台/设置/事件标签）/悬浮窗；主窗口关闭=归零退出 |
| `ui_server.py` | 本地 HTTP/JSON：主控制台、设置页、overlay 页 |
| `ui/` | `index.html` / `settings.html` / `overlay.html` / `style.css` |
| `overlay.py` | 可选独立 overlay 进程（浏览器模式/单独启动） |
| `simulate.py` | 离线 CLI 模拟台 |

---

## 4. Phase 3：V3 传输（已实现）

`standalone/v3_device.py` 实现了 `DeviceSink` 的 V3 版本：

- 本地 WebSocket 服务 + 官方二维码前缀；
- `bind` 握手、clear 残留波形、初始归零、重连重发当前强度、
  `strength`/`pulse`/`clear`、25s 心跳；
- 解析 App 的 `strength-A+B+上限A+上限B` 反馈；
- `play(loop=True)` 首块 + `pump()` 持续补帧，`clear` 停止；
  配对前已开始的 loop 在绑定后立即补一块（与 V4 一致）；
- 优先级队列 + 通道 generation：clear 后丢弃同通道旧 pulse，避免 clear 后还冒出旧波形；
- `feedback-0` 急停/恢复；急停期间 `play`/`pump` 不输出；
- App 断开时清空 loop、清空发送队列、等待重新扫码。

宿主入口（首次运行先安装依赖）：

```bash
py -3.13 -m pip install -r standalone/requirements.txt
py -3.13 standalone/run.py --transport v3      # 桌面主窗口（V3）
py -3.13 standalone/run.py --transport v3 --browser   # 浏览器模式
py -3.13 standalone/run.py --transport v3 --no-telemetry  # 只测试配对/页面
```

输出强度以 App/设备上报的通道上限为准（V3 绑定反馈 / V4 intensityMax），不再有软件强度上限设置。**第一次真机请先在 App 内把通道上限调低（如 20–40），确认体感后再提高。** App 上报上限前不输出强度，设置页会显示“等待 App 上报”。
主窗口需要 `pywebview`（`requirements.txt` 已包含）和 Windows WebView2 Runtime；
缺失时自动回退浏览器模式。

测试：

```bash
py -3.13 tests/test_standalone_v3.py
```

覆盖：bind/clear 残留/初始归零/上限反馈/强度映射、Pulse 分片、loop pump、clear、`feedback-0` 急停/恢复、断连清 loop、重连重发强度、配对前 loop 补块。

真机快速验收（不需要开游戏/模拟台）：

```bash
py -3.13 standalone/run.py --transport v3 --templates sdk \
  --test-sequence --log-file standalone\p3_sequence.log
```

App 扫码绑定后自动执行：

```text
+0.5s  base
+3.5s  起火
+6.5s  受击
+7.0s  第二次受击（应 pending）
+10.5s 灭火
+12.0s 阵亡
+17.0s 战局结束
```

重点看：base 是否 loop、火/受击是否立即抢占并 clear 旧 owner、第二次受击是否不堆队列、死亡后是否归零、UI/overlay 急停是否可用。

### 4.1 V4 传输（已实现）

`standalone/v4_device.py` 是 V4 控制方，`standalone/v4_relay.py` 是可选的
本机中继。详细协议/强度/波形/限制见
[`docs/standalone_V4.md`](../docs/standalone_V4.md)。

```bash
# 默认就是 V4 官方 Relay（控制方连 wss://trex.dungeon-lab.cn/v4）
py -3.13 standalone/run.py

# 本机中继（局域网；手机连 ws://<本机IP>:9998/v4?tid=...）
py -3.13 standalone/run.py --v4-relay local

# 指定 slot / 自建 Relay / 多网卡
py -3.13 standalone/run.py --v4-relay local \
  --advertise-ip 192.168.1.3 --v4-slot slot-a
```

要点：

- 控制方连接后拿到 `targetId`，二维码内容为
  `https://dungeon-lab.cn/s/?v=1&action=socket&url=<...?tid=<targetId>>`；
- 强度是增量语义：`t=3 AddIntensity`，归零用 `t=7 SetIntensity(v=0)`；
  每 App/通道维护 App 确认值，一次只发一条增量；下发后用 `devices.get`
  读取 `props.intensityA/B` 校验。同一个旧值不再连续补正向增量，改为
  “归零 → 确认 0 → 重新施加目标”；高于目标只向下修正，缺字段/不可控/静音
  视为未知，归零无法确认时进入安全锁定；
- 波形 `t=0 AppendPulseData`，首块 `im=true` 替换，loop 由 `pump()` 追加；
- clear 用 `device.op.clear` `{s,c}`；
- 多 App 时广播输出，每个 App 独立选择设备与上限；
- 断线指数退避重连并刷新二维码；App 断开清空 loop/队列（与 V3 一致），
  配对前已开始的 loop 在绑定/接入后立即补一块；
- 保存设置不会重置战局/遥测；自动发现的遥测路径会保留；战局开始/结束、
  遥测超时或文件重置会强制绝对归零；不做 120ms 强度节流，确认后立即继续；
- 安全：接入即 clear + 归零，急停/退出 clear + 强度 0；
- 绑定/选中设备后通过 `device.on_connected` 让引擎重置强度去重，
  重连或中途配对后相同百分比也会重新下发。

测试：

```bash
py -3.13 tests/test_standalone_v4.py
py -3.13 tests/test_standalone_parity.py   # V3/V4 行为对照
```

覆盖：本机中继握手/转发/ping/未知 target/空闲超时；V4Device 设备上报、
slot 选择、上限、单条增量确认、循环补帧、clear、归零、急停/恢复、
旧值不重复补增量、归零锚定、完整快照缺字段视为未知、高于目标只向下修正、
归零未确认安全锁定、手动调低接受、设备切换先归零再重发强度、
断线清 loop、重连重发强度、先 play 后接入；
V3/V4 对照测试用同一序列比较归一化后的强度、帧列表和 clear/归零结果。

> 官方 Relay 的 `hello`/`client_attached`/双向 `message` 已用假 App
> 实测通过；真实 App/设备与 `ws://` 本机中继的兼容性仍需真机验收。

## 5. Phase 4：UI（桌面主窗口）

界面结构：**pywebview 主窗口（控制台 `/` + 设置 `/settings` + 事件 `/events` 三个标签页）+ 置顶悬浮窗 `/overlay`**。
没有 pywebview 时自动回退到浏览器，页面与行为保持一致（除关闭/退出语义）。

- 首次启动直接进入主控制台，不再有单独向导：
  - 配对每次启动在设置页扫码（V3 每次启动生成新二维码；
    V4 使用 Relay 的 targetId，断线重连后二维码会刷新）；
  - 遥测文件在设置页「遥测文件」选择；主控制台没有数据时会给出入口；
  - 强度上限以 App/设备通道上限为准，软件上限设置已取消；
- 主控制台（黑黄配色，信息精简）：A/B 强度、战局状态、当前效果、事件流、急停；
  - 不再展示 owner/模板 id、五类贡献表、遥测 offset/坏行等内部细节；
  - 输出关闭（急停/未连接）时强度归零显示，避免误读；
  - 底部可展开「安全使用」，保留电极位置/低强度/急停等要点；
- 设置标签：设备/QR、输出窗口（OBS/悬浮窗）、遥测文件；
  - 设备卡顶部可选择连接方式（默认 V4；V3 兼容）和 V4 Relay
    （官方/本机中继），保存后自动切换，二维码会随之刷新；
    二维码旁提供「保存并刷新」按钮：保存设置（含连接方式切换）
    并强制重新加载二维码；
  - 设备卡按传输方式显示配对文案：V3「连接郊狼 App」需同一局域网；
    V4「连接 DG-LAB 4 App」本机中继需同一局域网、远程 Relay 不要求；
    同时显示当前连接方式、App 数量和设备名；
  - 设备卡显示手机 App 的 A/B 通道上限（V3 绑定反馈 / V4 intensityMax）；
    文案说明它表示 App 内允许的最高强度，不是当前输出，
    实际输出以该上限为准；
  - OBS 与悬浮窗合并为「输出窗口」双列卡片，窄屏自动堆叠；
    详细模式只在卡片头统一说明“额外显示当前触发事件”；
    端口、背景/整体不透明度只保留输入控件，不再写默认值和额外解释；
  - 详细模式的实际效果：追加当前触发的五类事件及贡献强度，
    固定顺序原地更新；HP 始终显示，基础事件有贡献时常驻；
  - 遥测路径可输入或点“浏览…”选择 `.jsonl`；也可以只选到 `profile`
    文件夹或游戏根目录；
  - 战斗中修改事件参数/模板/通道会即时生效，不结束战局；
    更换遥测路径会结束当前战局并重新接入，切换连接方式会断开当前 App，
    静默超时会结束本局；设置页已给出对应提示；
  - 高级「读取间隔」用于 standalone 轮询遥测文件的频率；
  - 修改保存后实时应用，并保存到 `%LOCALAPPDATA%\wows_coyote\settings.json`；
  - `--no-telemetry` 会先禁用轮询，设置页选好路径后自动启用；
- 事件标签：基础/受击/起火/进水/死亡五类事件（与设备/遥测设置分开）；
  - 顶部「配置方案」可将五类事件参数保存为命名方案，支持读取、删除、
    导入/导出 JSON；方案存放在
    `%LOCALAPPDATA%\wows_coyote\event_presets\`，只含事件参数，
    不影响遥测、设备和输出设置；
  - 「保存为方案」不改变当前生效设置；「读取」立即应用并写入
    `settings.json`；跨机器导入时缺失的波形/函数表会清空并给出提示；
  - 基础/受击高级参数会显示当前函数表路径，可自定义 `base.json`/`hit.json`；
    留空则使用随包默认路径，浏览对话框默认打开函数表所在程序目录；
  - 受击的触发阈值/满强度放在普通参数区，不再收进高级参数；
  - 波形下拉下方显示 DGHub 风格条状预览（柱高=强度，竖向条密度=频率）；
    函数表高级区显示当前曲线预览，未启用函数表时隐藏预览；
  - 曲线预览带简单坐标轴和横/纵轴数值刻度；基础横轴为血量 100%→0%
    （左高右低），受击为损失 0%→100%；
  - 基础/受击/起火/进水/死亡均可改启用、强度/阈值/时长/通道/模板；
  - 任一事件当前贡献强度为 0 时不参与波形仲裁；原本在播的波形会 clear，
    不再继续补帧；受击线性/函数表强度为 0 时也不会建立受击事件；
- 亮/暗主题：控制台/设置/事件顶栏按钮切换，即时保存到设置文件的 `theme`；
  - 服务端在 HTML 首帧注入已保存主题，重启/切页不闪深色；Windows 标题栏同步；
  - overlay 与 OBS `/obs` 跟随主题，OBS 页面仍保持透明背景；
- 更新检查（仅提示）：
  - 启动后约 10 秒检查一次，之后每 24 小时一次；设置页“更新”卡片和控制台顶部横幅；
  - 发现新版本只显示说明和“前往 GitHub 下载”，不在应用内下载/安装；
  - “跳过此版本”会持久化，可在同一卡片取消跳过；
  - `--no-update-check` 关闭本次自动检查，`--update-url` 用于联调；
  - 清单地址：`https://wowsdg-update.woyiba.com/wows_coyote/latest.json`；
    更新分发维护流程见仓库维护文档（不随源码包分发）；
- overlay：置顶小窗（400×240）；连接/急停状态、A/B 圆形仪表（百分比居中）、HP/战局、停/恢复/关闭；
  - 顶部「关闭」或 Esc：只关闭悬浮窗，不归零，输出继续；窗口模式内嵌，浏览器模式独立进程；
  - Windows/WebView2 下用圆角窗口区域裁剪，视觉上只有卡片，不显示矩形背景；
  - 禁止滚动条（overflow hidden），卡片铺满窗口；
  - 整体不透明度通过 Windows 分层窗口设置，能让桌面/游戏从悬浮窗后透出；
  - 锁定（鼠标穿透）：悬浮窗右上角 🔒、设置页开关或全局 END 热键切换；
    锁定后窗口使用 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`，
    鼠标点击/拖动直接落到下层游戏；解锁时只清掉透明/不激活位，保留透明度和置顶；
    锁定状态写入用户设置，重启后保持；
  - 详细模式显示当前事件行（基础/受击/起火/进水/死亡及强度），HP 常驻底部；
  - A/B 仪表圆心显示百分比，下方显示「实际强度/通道上限」，例如 `45% 45/100`；
    V3 用已下发的原始值，V4 用 App 回报的 current；断连/急停时归零显示；
- OBS 浏览器源：
  - 设置页「OBS 浏览器源」卡片：启用开关、端口（默认 8787）、背景不透明度、
    运行状态和可复制地址；
  - 保存后启动独立的只读 OBS 服务，不依赖主 UI 端口；
  - `/obs` 是透明只读页面：无控制按钮、无滚动条，适合 OBS「浏览器源」；
    背景透明度只影响卡片底色，状态文字和仪表保持清晰；
  - OBS 添加 `http://127.0.0.1:8787/obs`（端口可改），源尺寸建议 400×240；
  - 端口被占用时设置页显示错误；关闭开关即停止 OBS 服务；
  - 应用未运行时 OBS 显示空白/连接失败，启动后刷新源即可；
- 关闭行为（主窗口 = 程序生命周期）：
  - **主窗口 X / Alt+F4**：立即 clear 双通道 + 强度 0 + 静音，关闭悬浮窗，
    停止热键/UI 服务/设备，然后退出；不做二次确认（安全优先）；
  - **控制台 / 设置 / 事件标签切换**：同一窗口内切换，不归零、不退出；
  - **设置页「退出程序」**：归零并退出（浏览器回退模式用它退出主程序）；
  - **悬浮窗关闭**：只关闭悬浮窗，不归零，输出继续；
  - **最小化主窗口**：不归零，继续输出，避免游戏中被误停；
  - **浏览器回退模式**：关闭标签页仍会 `ui_close` 归零，但 Python 进程继续；
    `Ctrl+C` 退出；刷新页面也会归零，这是浏览器模式的既有行为；
  - 强制结束进程（任务管理器）、崩溃或断电无法保证指令送达，请在 App/设备侧确认。
- 安全：
  - **F12 全局急停**（`--hotkey` 可改，`--no-hotkey` 禁用）；
  - **END 全局切换悬浮窗锁定**（`--lock-hotkey` 可改，`--no-lock-hotkey` 禁用）；
  - 主窗口关闭由 Python 侧归零；浏览器模式由 `sendBeacon` 发送 `ui_close`；
  - Ctrl+C、正常退出、未捕获异常时 `atexit`/`finally` 执行 clear + 强度 0。
- `run.py` 默认启动桌面窗口；`--browser`/`--open-browser` 强制浏览器；
  `--window` 强制窗口；`--no-ui` 关闭 UI；`--overlay` 窗口模式内嵌悬浮窗。

```bash
py -3.13 standalone/run.py                  # 桌面主窗口
py -3.13 standalone/run.py --browser        # 浏览器
py -3.13 standalone/run.py --overlay        # 主窗口 + 悬浮窗
```

界面定稿与线框见 [`docs/standalone界面定稿.md`](../docs/standalone界面定稿.md)。

## 6. 当前不包含

- `.pulse` 直接转换（官方 Demo 请走 `expectedV3`/manifest）；
- 托盘/自动安装/单实例（更新检查已实现，仅提示并跳转 GitHub；安装包/overlay exe 脚本已提供）；
- 真实设备上的 10 局长测（代码已接，待真机验收）；
- 设置页暂不编辑预设文件路径（固定为随包/默认路径）；曲线函数表路径可在高级参数里自定义。

---

## 7. 设计说明

- **强度**仍由现有 `MixerEngine` 计算：五类事件相加、按通道 cap 截断；`StandaloneEngine._send_strength` 只负责把它送到 `DeviceSink`。
- **波形**不再由引擎的 `_arbitrate_waveforms` 发；调度器复用 `_pick_wave(channel, now)` 拿每通道赢家，再按模板执行。
- **持续状态**（base/fire/flood）是 loop 模板：`play(loop=True)` 后由 `DeviceSink.pump()` 持续补帧，直到 clear；赢家变化/状态结束才 clear。V3/V4 没有设备端无限 loop，Phase 3 的传输层会用 100 帧/块、提前 1–2s 的方式实现同样的语义。
- **一次性**（hit/death）是 one-shot 模板：播放期间的新事件只标记 `pending`，当前窗口结束后最多补播一次，避免 DGHub 时代每 300ms 追加造成的队列增长。
- **FakeDevice** 用 `loop_refill` 事件模拟传输层补帧，测试里验证“多轮循环 + clear 后停止”。
- **模板缺失**时只输出强度，不回退占位/社区数据（Phase 2 的 fail-closed 规则在调度器层已经生效）。
- **官方模板**：默认使用随包 `standalone/templates/official_manifest.json`（24 条 dglab-kit-python，commit `4cce771c0fd93d043f3e83bbb0467e4197cde62e`，GPL-3.0）；见 `LICENSE`、`THIRD_PARTY.md`、`SOURCE_OFFER.md`。manifest 需 `source/license/sha256`，任一不合法整份拒绝。

---

## 8. Windows 可执行包 / 安装包

```bash
py -3.13 -m pip install -r standalone/requirements.txt
py -3.13 -m pip install pyinstaller
py -3.13 tools/make_release.py
```

输出：

```text
dist/wows_coyote/wows_coyote.exe
dist/wows_coyote/_internal/...   # 模板/UI/wows_coyote/许可证/源码 zip
releases/wows_coyote-1.0.0-win64.zip  # 便携包
releases/latest.json                  # 更新清单（上传到自建服务器）
```

- 主 EXE 无控制台（`console=False`），默认日志写入
  `%LOCALAPPDATA%\wows_coyote\logs\standalone.log`（滚动 2 MB × 2）；
- 主窗口依赖 pywebview + WebView2 Runtime；WebView2 缺失时提示并可用 `--browser` 回退；
- 关闭主窗口 = 归零 + 退出，不需要额外“停止”操作。

可选独立 overlay exe（浏览器模式的置顶小窗；窗口模式已内嵌，不需要）：

```bash
py -3.13 tools/build_windows.py --overlay
```

安装包（Inno Setup 6/7）：

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\wows_coyote.iss
```

输出 `dist_installer/wows_coyote-1.0.0-setup.exe`，安装到
`%LOCALAPPDATA%\Programs\wows_coyote`，无需管理员，并包含 GPL 源码 zip。
安装向导支持简体中文（`installer/ChineseSimplified.isl` 随仓库提供）。

发布前刷新校验并审计：

```bash
py -3.13 tools/finalize_release.py
py -3.13 tools/audit_release.py
```

完整发布流程（构建、审计、GitHub Release、独立仓库拆分）见
[`docs/standalone发布指南.md`](../docs/standalone发布指南.md)。

完整验收步骤见 [`docs/P0发布检查清单.md`](../docs/P0发布检查清单.md)；
当前机器上的干净用户/VM 测试见
[`docs/干净环境安装测试.md`](../docs/干净环境安装测试.md)。

更新清单托管与客户端提示流程见仓库维护文档（不随源码包分发）。
