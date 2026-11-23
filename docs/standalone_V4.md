# standalone V4 支持

> 状态：已实现（2026-11-15）；官方 Relay 握手/配对/转发已用假 App 验证，
> 真机（DG-LAB 4 App + 设备）验收待做。
> 相关：`standalone/v4_device.py`、`standalone/v4_relay.py`、
> `docs/官方SDK模板用法.md`、`docs/脱离DGHub全链路评估.md`。

---

## 1. 目标与范围

standalone 原来只有 **V3 Socket**（本机 WebSocket + App 扫码直连）。
本次新增 **V4**：

- 作为“控制方”连接 **V4 Relay**，使用协议规定的
  `req` / `resp` / `ev`、`device.op`、`device.op.clear`；
- 可选启动 **本机 V4 中继**，让手机 App 通过二维码连到本机，
  保持“局域网、不经过 DGHub”的使用方式；
- 复用现有 `DeviceSink` / `WaveformScheduler` / `TelemetryRunner` / UI，
  不改事件数学和模板。

不包含：

- V2 BLE；
- 多控制方；
- 自定义模板/波形编辑器；
- 对官方 Relay 的账号/鉴权（Relay 本身不需要）。

---

## 2. 传输选择

```bash
# 默认 V4（官方 Relay）
py -3.13 standalone/run.py

# V3 Socket（郊狼 App）
py -3.13 standalone/run.py --transport v3

# V4 + 本机中继（局域网；手机与电脑同一网络）
py -3.13 standalone/run.py --v4-relay local

# 自建 Relay
py -3.13 standalone/run.py --v4-relay ws://192.168.1.10:9998/v4
```

| 参数 | 说明 |
|---|---|
| `--transport v3\|v4` | 选择设备传输；默认 `v4` |
| `--v4-relay official` | 官方 `wss://trex.dungeon-lab.cn/v4`（默认） |
| `--v4-relay local` | 启动本机中继，控制方连 `ws://127.0.0.1:<port>/v4` |
| `--v4-relay ws(s)://...` | 自建 Relay 地址 |
| `--v4-port` | 本机中继端口，默认 `9998` |
| `--v4-slot` | 指定 App 暴露的 `slotId`；默认自动选郊狼设备 |
| `--advertise-ip` | 本机中继二维码使用的局域网 IP（多网卡时指定） |

> 官方 Relay 会把控制消息经过第三方服务器转发；本机中继只走局域网。
> 介意隐私或需要离线时用 `--v4-relay local`。两种方式都不经过 DGHub。

配置也可写入 `%LOCALAPPDATA%\wows_coyote\settings.json` 的
`transport` / `v4_relay` / `v4_slot`；命令行参数优先。
设置页“设备”卡片顶部可选择连接方式（默认 V4 / V3 兼容）和
V4 Relay（官方/本机中继），保存后自动切换设备并刷新二维码；
“当前：”行显示正在运行的连接方式、App 数量、设备名和通道上限，
并按 V3/V4 动态切换配对标题与局域网提示（V4 远程 Relay 不要求同一局域网）。

---

## 3. 架构

```text
游戏遥测 → TelemetryRunner → StandaloneEngine
                                   │ set_strength / play / clear
                                   ▼
                            WaveformScheduler
                                   │ DeviceSink
                                   ▼
                              V4Device
                        ┌──────────┴──────────┐
                        │                     │
              official Relay            LocalV4Relay
        wss://trex.dungeon-lab.cn/v4   ws://0.0.0.0:9998/v4
                        ▲                     ▲
                        │                     │
                   DG-LAB 4 App          DG-LAB 4 App
                        │                     │
                        └──────── BLE ────────┘
                                  设备
```

### 3.1 控制方

`V4Device` 在独立线程/事件循环里：

1. `ws(s)://.../v4` 连接 Relay，收到 `{"type":"hello","clientId":...}`，
   这就是 `targetId`；
2. 生成二维码/配对链接；
3. 等待 `client_attached`，为每个 App 建立状态；
4. 维护 `devices.snapshot` / `devices.patch` / `slots.patch`；
5. 把 `set_strength`/`play`/`clear` 转成 `device.op` /
   `device.op.clear`；
6. 断线后指数退避重连（0.5s → 30s），刷新二维码；
7. 退出/急停时 clear + 强度 0。

### 3.2 本机中继

`LocalV4Relay` 复刻官方 Relay 的最小行为：

- 无 `tid` 的连接 = 控制方；发 `hello{clientId}`；
- `?tid=<控制方 ID>` = App；发 `hello` + `controller_attached`，
  并通知控制方 `client_attached`；
- `{"type":"message"}` 双向转发，App → 控制方时补 `clientId`；
- `ping` → `pong{ts}`，`heartbeat` 每 30s；
- App 断开 → 控制方收到 `client_disconnected`；
- 控制方断开 → App 收到 `controller_disconnected`，以 4000 关闭；
- 未知 `tid` → `error{controller_not_found}` + 4001；
- 空闲超时默认关闭（`idle_timeout_s=0`），避免二维码 5 分钟后失效；
  如需对齐官方行为可设为 300 秒；
- 无鉴权、无持久化，仅建议本机/可信局域网使用。

---

## 4. 配对与二维码

V4 的 App 连接地址是：

```text
<Relay 地址>?tid=<targetId>
```

二维码内容是官方配对链接：

```text
https://dungeon-lab.cn/s/?v=1&action=socket&url=<URL 编码后的 App 地址>
```

- 官方 Relay：`wss://trex.dungeon-lab.cn/v4?tid=...`
  （实测官方 Relay **只接受 `/v4`，`/v4/` 会 404**，所以不要补末尾斜杠）；
- 本机中继：`ws://<局域网 IP>:9998/v4?tid=...`。

运行时终端会同时打印：

- 控制方 ID；
- App 连接地址（可手动粘贴）；
- 二维码内容；
- 二维码文件路径；设置页也会显示二维码。

手机端：**DG-LAB 4 App → Socket 控制 → 扫码**。
V4 需要 App 4.x；支持 Coyote 3.0（以及 App 暴露的其他 V4 设备）。

> 注意：本机中继使用 `ws://`（无 TLS）。如果手机 App 拒绝非 `wss://`
> 的 V4 地址，请改用官方 Relay，或自建带 TLS 的反向代理。
> 这是 V4 真机验收前最大的未知项。

---

## 5. 强度映射

V4 没有 V3 的绝对强度命令：

| 操作 | V4 动作 |
|---|---|
| 设置非零强度 | `device.op` `t=3` AddIntensity，`v` 为 **增量** |
| 归零 | `device.op` `t=7` SetIntensity，`v=0`（协议只接受 0） |
| 清波形/任务 | `device.op.clear` |

`V4Device` 为每个 App/通道维护确认状态：

```text
raw       = min(intensityMax, round(intensityMax * pct / 100))
confirmed = 最近一次完整 App 快照里的实际强度
delta     = raw - confirmed
```

- `intensityMax` 来自 `slotState.channelA/B.intensityMax`；
- 输出上限就是 `intensityMax`；软件强度上限设置已取消；
- **一次只发一条增量**：发出后标记 `in_flight`，在收到这条指令之后的
  `devices.get`/完整快照确认前，不再基于乐观值发下一条；目标在此期间变化
  只更新期望值，确认后从 App 实际值重新计算差值；
- **主动探测**：每次下发后等 0.75 秒，通过 `devices.get` 读取
  `props.intensityA/B`；确认到位立即停止探测，不长期轮询；
- **App 推送也参与确认**：`slots.patch`/完整快照中的新强度值可以确认
  在途增量，不要求 App 必须响应探测；只接受与发送前不同的值，
  同一个旧值仍交给探测/归零锚定，避免只推不答探测时 4 秒误归零；
- **同一个上报值不连续补正向增量**：如果发出增量后 App 仍上报发送前的
  同一个值，不能判断是旧值还是增量丢失；此时不继续加，经过短暂宽限后
  执行“归零锚定”；
- **归零锚定**：发绝对 `SetIntensity(0)`，等 App 确认 0，再从 0 重新施加
  完整目标；确认后才恢复正常输出；
- **高于目标只向下修正**：App 上报值高于目标时只发负向差值或绝对 0，
  永远不会把高于目标的值采用为新基线；
- **目标 0 只归零**：不会把非零上报值当基线；多次无法确认归零时进入
  `uncertain` 安全锁定，停止正向输出，等后续目标变化或重新连接再恢复；
- **缺字段视为未知**：完整探测快照缺少 `intensityA/B`、`hasDevice=false`
  或通道 `isMuted=true` 时，不沿用上一次上报值补差值；目标为 0 仍归零，
  非零目标等待下一次有效确认/归零锚定；
- **手动调整**：已确认到位后，App 手动调低会被接受为新的实际基线；
  手动调高高于目标时仍按“高于目标只向下修正”处理；
- **不抵消冷启动爬坡**：`warmUpScale < 1` 时只接受“等于目标”或
  “等于目标×warmUpScale”的反馈；不回正、不归零、不把缩放值当基线，
  冷启动未结束时暂停校验；
- 探测请求带请求序号并排在强度指令之后发送；新指令会作废旧探测响应，
  避免把旧快照当成新状态；
- App 上报的强度是“当前值”，不是“上限”；上限只取 `intensityMax`。

> 新策略一次只发一条增量，确认后才继续；强度跟随会受一次探测确认影响
> （通常约 1 秒），换来的是不会基于旧值连续补增量、不会把过冲值当基线。
> 这是有意的安全取舍。

> 战局开始/结束、遥测超时或文件重置时，引擎会强制下发绝对 0
> （V4 为 `SetIntensity(0)`），避免增量在旧值上叠加。
> 保存设置不会重置遥测/战局；自动发现的遥测路径会保留并显示。

> 软件强度上限已取消，`raw` 直接按 App 的 `intensityMax` 折算。
> 第一次真机请先在 App 内把通道上限调低（如 20–40），
> 或先把五类事件强度调低，确认体感后再提高。
> 如果 App 尚未上报 `intensityMax`，本实现按 0 处理（不输出），
> 直到 `slots.patch` 或探测快照补上上限；V3 同样在收到绑定反馈前不输出。

与 V3 的差异：

- V3 的 `strength-A+B+limitA+limitB` 反馈直接给上限；
  V4 上限来自 `intensityMax`，没有同样的四元组反馈；
- V4 强度是增量语义，**确认值 + 单条增量 + 主动探测 + 归零锚定**，
  不是绝对设置；
- 已确认到位后，App 手动调低会被接受；手动调高高于目标只向下修正；
  目标为 0 时只归零，不会把非零值当基线。

---

## 6. 波形映射

| 需求 | V4 实现 |
|---|---|
| 普通一次性波形 | `device.op` `t=0` AppendPulseData，`im=true` 首块 |
| 循环（base/fire/flood） | 首块 `im=true`，之后由 `pump()` 按 period 追加 `im=false` 块 |
| 抢占/clear | `device.op.clear` `{s,c}` |
| 分块 | 每块 ≤100 帧；`d = len(chunk) * 100` ms |
| 帧格式 | 16 位 HEX（8 字节），与现有模板一致；省略 `ver` 即 V3 帧 |

- 初始长度与 V3 相同：`max(chunk_frames, frames_count)`；
- `pump()` 每次只补一块，长时间停顿后不会疯狂追帧；
- `im=true` 会替换同设备/通道/类型任务，对应调度器的 replace 语义；
- 每个 App 有独立的脉冲代次 `(clientId, channel)`；clear/换模板只丢弃
  该 App 的旧帧，不会影响其他 App。

---

## 7. 多 App / 多设备

V4 支持 `1 控制方 : N App`。本实现：

- **广播输出**：所有已接入 App 都收到相同的强度/波形/clear；
- 每个 App 独立选择设备：优先 `--v4-slot`，否则第一个
  已连接的郊狼设备（`COYOTE_*`），再否则第一个已连接设备；
- 每个 App 的 `intensityMax`、当前强度、slot 独立跟踪；
- UI 的通道上限显示所有 App 的**最小值**（保守展示）；
- 控制方 ID 是唯一配对凭据；不要把它发给不可信的人。

设备选择可用 `--v4-slot` 固定。App 内切换设备时，
`devices.patch`/`devices.snapshot` 会触发重新选择并归零新设备。

---

## 8. 安全行为

| 事件 | 行为 |
|---|---|
| App 接入 | 先 `device.op.clear` 清残留任务，再拉设备列表；选中设备后 SetIntensity 0 |
| App 断开 | 停止发送；最后一个 App 断开时清空 loop/队列（与 V3 一致）；下次配对后由下一个赢家变化重新起播 |
| Relay 断线 | 清 App/loop/队列，指数退避重连；新 targetId + 新二维码 |
| 全局急停 F12 / UI 急停 / App 动作 0 | clear 双通道 + SetIntensity 0 + 静音；同时清调度器 |
| 退出 | clear + 强度 0（best-effort，依赖发送窗口） |
| 断网/崩溃/断电 | 无法保证归零指令送达；需在 App/设备侧确认 |

`device.op` 的响应是 **任务完成后才返回**，所以本实现不等待
`device.op` 的响应，只使用唯一 `reqId` 并忽略完成/清理响应；
错误响应会记录日志。

### 8.1 V3/V4 行为对照

两边共用同一个 `WaveformScheduler`、`StandaloneEngine`、事件数学和 UI
状态契约；差异只来自协议本身。2026-11-15 已用
`tests/test_standalone_parity.py` 用同一输入序列同时驱动两套传输，
把 V4 的增量强度累加后与 V3 的绝对值比较。

| 行为 | V3 | V4 | 结论 |
|---|---|---|---|
| 强度公式 | `min(limit, round(limit*pct/100))` | 同左 | 一致 |
| 强度下发 | 绝对 `strength-N+2+raw` | `AddIntensity` 增量 / `SetIntensity(0)` | 协议不同，最终值一致 |
| 强度上限来源 | App `strength-A+B+limA+limB` 反馈 | `slotState.channelA/B.intensityMax` | 协议不同 |
| App 内手动改强度 | 下一次期望变化时覆盖 | 反馈校准后下一次变化时覆盖 | 效果一致 |
| loop 初始/补帧 | `initial=max(chunk, frames)`，`pump()` 每 period 补一块 | 同左 | 一致 |
| 非 loop | 铺满 `duration*10` 帧 | 同左 | 一致 |
| clear | `clear-1/2` | `device.op.clear {s,c}` | 协议不同，结果一致 |
| 抢占 | 调度器先 clear 再 play | clear + 首块 `im=true` | 结果一致 |
| 断线 | 清 loop/队列 | 清 loop/队列 | 一致 |
| 重连强度 | 绑定后重置 `_last_strength`，并通知引擎重发 | 新 App 状态从 0 开始，选中设备后通知引擎重发 | 一致 |
| 配对前已开始的 loop | 绑定后补一块 | 接入后补一块 | 一致 |
| 急停/恢复/stop | clear + 强度 0 + mute | clear + SetIntensity 0 + mute | 一致 |
| UI 状态键 | `connected/muted/limits/...` | 同左 + V4 扩展字段 | 向后兼容 |
| 强度节流 | 无（每次变化都发） | 同左 | 一致 |
| 多 App | 单 App | 1:N 广播 | V4 扩展 |
| 设备选择 | 无 slot 概念 | `slotId` + `intensityMax` | V4 协议要求 |

有意保留的协议差异：

- V4 需要 Relay（官方/本机），V3 是 App 直连本机 WS；
- V4 的 `device.op` 响应延迟到任务结束，V3 的 pulse 无响应；
- V4 波形按 100 帧分块并带 `d`/`im`，V3 按消息大小分块并靠 clear 抢占；
- V4 支持 Coyote 2.0/3.0、负鼠等 App 暴露的设备，V3 只面向 V3 Socket 设备。

> 强度去重：`StandaloneEngine._send_strength` 对相同百分比会跳过。
> `run.py` 把 `device.on_connected` 接到 `host.engine.resend_strength()`，
> 设备绑定/选中后下一次 tick 会重新下发当前强度；V3/V4 共用这条路径，
> 避免“重连后百分比没变、输出停在 0”。

---

## 9. 与官方 Relay 的实测结论

2026-11-15 用本机代码验证（不是真机输出）：

- `wss://trex.dungeon-lab.cn/v4` 返回 `hello{clientId}`（8 位十六进制）；
- `/v4/`（带末尾斜杠）返回 HTTP 404；`/v4` 正常；
- App 侧 `wss://trex.dungeon-lab.cn/v4?tid=<targetId>` 能收到
  `hello` + `controller_attached`；
- 控制方收到 `client_attached`；
- 双向 `message` 转发正常；
- 本实现的 `devices.get`、`devices.snapshot`、`t=7` 归零、
  `t=3` 增量、`t=0` 波形、`device.op.clear` 均能通过官方 Relay
  到达假 App。

未验证：真实 DG-LAB 4 App 对 `ws://` 本机中继的接受度、真实设备的
强度/波形体感、长时间稳定性、省电/锁屏行为。

---

## 10. 排障

| 现象 | 检查 |
|---|---|
| 启动报“V4 Relay 未能在 … 内完成握手” | 网络/代理/防火墙；`--v4-relay official` 是否可访问；自建地址是否正确 |
| 二维码扫了没反应 | App 是否 4.x；是否用的是控制方当前 `targetId`；官方 Relay 5 分钟无 App 会 `idle_timeout`，重连后二维码会变 |
| 二维码不刷新/仍是旧码 | 设置页设备卡点「保存并刷新」；确认“当前：”与连接方式选择一致；V4 重连/切换后 targetId 会变 |
| 保存设置后强度累加 | 旧版本问题：更新到当前版本；新版无变化保存为 no-op，自动遥测路径不会写空，战局开始/结束强制归零 |
| 强度与 App 不一致/漂移 | 新版一次只发一条增量，确认后继续；同一旧值不会连续补正向增量，会走归零锚定；检查 App 是否上报 `props.intensityA/B`；缺字段时状态视为未知 |
| 强度跟随有约 1 秒延迟 | 有意取舍：等待 App 确认后才发下一条；确认后立即继续，探测窗口内不会长期轮询 |
| App 里手动调低后又被拉高 | 已确认到位后手动调低会被接受；若在校验窗口内或高于目标，仍会按目标修正/向下修正 |
| 归零后不恢复输出 | 先看日志是否“归零未确认”；确认 App 是否上报 0 且 `hasDevice=true`、未静音；安全锁定需等后续目标变化或重连 |
| 战斗中改设置 | 事件参数/模板/通道即时生效；更换遥测路径会结束当前战局；切换连接方式会断开 App；静默超时会结束本局 |
| 本机中继手机连不上 | 同一局域网；Windows 防火墙放行 `--v4-port`；`--advertise-ip` 选对网卡；手机是否接受 `ws://` |
| 有强度没波形 | App 没有默认/保持波形；确认 `device.op` t=0 已发出；模板是否有效（fail-closed） |
| 波形结束后静音 | V4 的 pulse 是有限帧；loop 由本机 pump 持续追加，非 loop 结束后依赖 App 默认波形 |
| 强度比预期小 | App 的 `intensityMax` 更低；设置页会显示 App 通道上限 |
| 换设备后无输出 | `--v4-slot` 是否指向新 slot；`slots.patch` 是否上报 `hasDevice` |

日志：`%LOCALAPPDATA%\wows_coyote\logs\standalone.log`（冻结包）；
源码运行默认输出到控制台，`--debug` 可看 `device.op` 调试信息。

---

## 11. 测试

```bash
py -3.13 tests/test_standalone_v4.py
```

覆盖：

- 本机中继 hello / attach / 双向转发 / ping-pong /
  未知 target 4001 / 空闲超时 4002；
- V4Device 本机中继端到端：设备上报、slot 选择、上限、
  增量强度、循环波形、补帧、clear、归零、急停/恢复、
  slots.patch 校准、断线清 loop、重连重发强度、先 play 后接入。

V3/V4 行为对照：

```bash
py -3.13 tests/test_standalone_parity.py
```

同一模板/同一强度序列同时驱动 V3 与 V4，比较归一化后的
强度、首块/补帧帧列表、clear 和 stop_all 归零结果。

全量回归：

```bash
py -3.13 tests/test_standalone_phase1.py
py -3.13 tests/test_standalone_v3.py
py -3.13 tests/test_standalone_v4.py
py -3.13 tests/test_standalone_parity.py
py -3.13 tests/test_standalone_ui.py
py -3.13 tests/test_standalone_test_sequence.py
py -3.13 tests/test_standalone_desktop.py
```

---

## 12. 文件

```text
standalone/v4_device.py     V4 控制方（DeviceSink 实现）
standalone/v4_relay.py      本机 V4 中继
standalone/run.py           --transport / --v4-*
standalone/config.py        transport / v4_relay / v4_slot 持久化键
standalone/ui/settings.html 连接方式/设备/二维码刷新
tests/test_standalone_v4.py 中继 + 设备测试
```
