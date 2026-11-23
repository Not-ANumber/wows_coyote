# 战舰世界 × 郊狼 v2（wows_coyote 2.9.2）

DGHub 外部插件。读取 **ExportShipStatus** 输出到
`<游戏根目录>/profile/wows_telemetry.jsonl` 的遥测，按五类事件做
**每通道强度加法混合**，并按优先级播放波形。

> v2 与 v1 的事件模型不同：v1 是离散 trigger + 优先级锁；v2 是
> `set_strength` 绝对强度 + `trigger action=waveform` 波形。
> v2 预设与 v1 预设不兼容。

---

## 五类事件

| 事件 | 触发/活跃 | 强度贡献 | 默认 |
|---|---|---|---|
| 基础 | 战斗中持续，按 HP% | `base_min + (base_max-base_min) × (1-HP%)`；死亡归 0 | 10 / 40 |
| 受击 | 固定长度伤害窗口（默认 300ms） | 窗口损失 ≥ `hit_threshold_pct` 时触发；`hit_strength × min(1, loss/hit_full_pct)` | 阈值 2%，满强度 8%，最大 40 |
| 起火 | 燃烧期间 | `fire_strength + (层数-1) × fire_stack_add`；不设事件级封顶 | 12 + 6/层 |
| 进水 | 进水期间 | `flood_strength + (层数-1) × flood_stack_add` | 12 + 6/层 |
| 死亡 | 阵亡后 N 秒 | 清空基础/火/水/受击；`death_strength` 持续 N 秒 | 60 / 4s |

受击数据源固定为 **聚合流 `damage_taken(source=damages)`**：

- 包含炮弹、鱼雷、撞角、火/水跳伤；
- 不包含治疗；看的是毛伤害；
- 不要和 `source=shell` 同时计入，否则重复；
- 窗口：从第一条伤害开始固定 300ms 计时，
  窗口内伤害合并为一次受击；到点立即结算，后续伤害进入下一个窗口。
  窗口不会因为后续伤害继续延长。

---

## 强度混合

每个通道独立计算：

```
total_a = min(100, base + hit + fire + flood + death)
total_b = min(100, base + hit + fire + flood + death)
```

- 每个事件一个 **strength 百分比**；绑 A/B/A+B，A+B 时同一个值加到两边。
- 通道上限固定为 100%，A/B 分别截断，不开放配置。
- 强度用 DGHub `set_strength` 绝对写入；内部固定 100ms 节拍，整数变化才发送。
- 战局结束、退战、遥测超时、插件停止都会发 `set_strength 0`。
- 启动时先发一次旧版基线清零（`trigger strength permanent -100`），
  之后不再使用 baseline/rollback 强度。

---

## 波形与优先级

每个通道独立仲裁，优先级固定为：

```
基础(1) < 起火(2) < 进水(3) < 受击(4) < 死亡(5)
```

- 只播放当前最高优先级活跃事件的波形；强度仍然全部相加。
- 高优先级事件开始会覆盖当前波形；结束后回落到下一个活跃事件。
- 波形通过 `trigger action=waveform` 播放，不携带强度目标。
- 每个事件页的波形选择放在页尾；选中后波形预览固定跟在该字段下方，
  不再插在数值参数中间。
- 受击/死亡波形是一次性的，时长分别等于 `hit_duration_s` / `death_duration_s`；
  这两个值最多 20 秒，界面会直接显示上限。不再按 5 秒分块。
  战局结束/停止时不会发新波形，但已下发的最后一段会按设置时长播完。
- 起火/进水状态结束时，如果还有剩余波形，会立即用剩余优先级最高的波形覆盖一次；
  如果剩余的是基础，也会立即补播一次。
- **起火/进水波形在状态期间持续循环**：首次起火/进水播放一次，之后插件
  每 3 秒重发一次（内部固定步长，不设波形时长配置），直到熄灭/排水或被
  更高优先级波形覆盖。DGHub 会在每次 3 秒的 trigger 内重复/截断预设帧。
- 火/水叠层不会立即重播波形，只在下一次 3 秒循环时使用新的层数标签。
- 基础启用且选好波形后：基础成为该通道赢家时立即播放，之后每 3 秒重发一次；
  被更高优先级事件覆盖时暂停，重新成为赢家时立即恢复。
  不再单独设置“启用波形”“持续循环”或随机间隔；清空基础波形则不播放。

---

## 安装

1. DGHub → 插件中心 → 外部插件 → 导入项目 `releases/` 目录下的 `wows_coyote-2.9.2.zip`。
2. 启用插件，在「数据」区确认遥测路径；自动查找始终开启，路径无效/留空
   会查找并回填。
   也可以直接填游戏根目录、`profile` 目录或完整文件路径。
3. 预设文件默认指向插件目录内的 `wows_preset_v2.json`，执行导入；
   退出再进设置页刷新控件。导入/导出不会覆盖本机路径。
4. 进战斗。建议先低强度测试。

v2 只接受 `wows_preset_v2.json`。v1 预设含 `evt_*`/`ui_*` 等键，导入会直接报错，
不会再静默地只导入路径等少数字段。

v1 的同名插件会被 v2 覆盖（插件 ID 都是 `wows_coyote`）；
旧 `releases/wows_coyote-1.0.0.zip` 和 `archive/wows_coyote-1.0.0/` 可回滚。

---

## 配置

设置页按「数据 / 基础 / 受击 / 起火 / 进水 / 死亡」分区。

### 数据

- 预设文件：留空时会自动填入插件目录内的 `wows_preset_v2.json`；也可以填自己的 v2 预设路径。
  本区底部开关中的“导入预设”/“导出预设”对同一个文件操作。
- 遥测文件：自动查找始终开启，路径无效/留空时会查找并回填；也可以填游戏根目录、
  `profile` 目录或完整文件路径。
  填目录时自动补 `profile\wows_telemetry.jsonl`。
  已有有效路径不会被自动改写；路径为空或无效时才会回填，避免覆盖手动配置。
- 读取间隔、超时清零、调试日志也在本区；调试日志只在排查问题时开启。
  2.9.2 起运行日志写入插件目录 `wows_coyote.log`（1MB × 3 滚动）；
  如果插件目录不可写，会回退到 `%LOCALAPPDATA%\DGHub\logs\wows_coyote.log`。
  DGHub 启动时不再写 stdout/stderr，避免日志管道写满后阻塞事件循环。

### 基础

- `base_enable`：开启后同时输出基础和波形；关闭后两者都不输出；
- `base_min`（满血强度）、`base_max`（空血强度）；
- `base_channel`、`base_preset`；
- 基础波形固定每 3 秒循环一次；预设为空时只输出强度，不播放波形。
- `base_curve_enable`（使用函数表，对应文件见标签）：固定读取插件目录下的 `base.json`。开启且校验通过后，基础强度改为按表中 `[HP, 强度]` 点线性插值；关闭、文件缺失或无效时回退到满血/空血线性公式。
  文件变化后每 2 秒检查一次并自动重载；格式错误会保留上一份有效表并记录日志。
- JSON 文件格式：`{"points": [[100, 10], [75, 17.5], [50, 25], [0, 40]]}`，最多 64 点，必须包含 100 和 0 端点。
- 默认函数表随插件附带在 `wows_coyote/base.json` 和 `wows_coyote/hit.json`。

### 受击

- `hit_enable`、`hit_strength`、`hit_channel`、`hit_preset`；
- `hit_threshold_pct` 触发阈值、`hit_full_pct` 满强度损失；
- `hit_duration_s`（强度/波形共用，最多 20 秒）；
- `hit_curve_enable`（使用函数表，对应文件见标签）：固定读取插件目录下的 `hit.json`。开启且校验通过后，按窗口损失百分比插值计算受击强度；`hit_threshold_pct` 仍是触发门槛，`hit_strength` / `hit_full_pct` 作为关闭函数表时的回退配置。文件错误时保留上一份有效表，否则回退线性公式。

### 起火 / 进水

- `*_enable`、`*_strength`（首层）、`*_stack_add`（每多一层）；
- `*_channel`、`*_preset`。
- 波形不设时长；状态期间每 3 秒重发一次，灭火/排水后停止。

### 死亡

- `death_enable`、`death_strength`、`death_channel`、`death_preset`；
- `death_duration_s`（强度/波形共用，最多 20 秒）；

---

## DGHub 协议

- 强度：`{"op":"set_strength","channel":"a|b|both","pct":0-100}`
- 波形：`{"op":"trigger","action":"waveform","preset":"...","duration_s":...,
  "channel":"a|b","label":"..."}`
- `duration_s` 只覆盖本次窗口；DGHub 会在窗口内重复/截断预设帧。
  火/水每 3 秒重发一次，靠重发实现“状态期间持续循环”，而不是无限时长指令。
- 受击/死亡波形时长等于设置值，一次性发送；基础波形块时长也按设置值；
  代码不再做 5 秒隐藏截断。结束/停止后可能保留完整设置时长的尾音。
- 起火/进水结束时，若仍有剩余波形，会主动用当前最高优先级波形覆盖一次；
  没有剩余波形时无法硬取消，最后一段 3 秒仍可能播完。
- 启动：`trigger strength permanent -100` 清旧基线 + `set_strength 0`。
- 结束/停止：`set_strength 0`。
- 连接断开后按 1→30 秒退避自动重连；收到 DGHub `stop` 才退出。
- **注意**：`set_strength` 方案不会触发 DGHub 的高强度事件音效；
  如果需要，要额外设计带强度目标的 trigger，可能破坏纯加法模型。

---

## v1 → v2 变化

- 取消独立的重创、低血量、战局结束事件：
  - 重创 → 受击按损失比例缩放；
  - 低血量 → 基础曲线体现；
  - 战局结束 → 直接清零，没有结束波形。
- 受击改为聚合流窗口，不再按逐发炮弹；
- 火/水改为首层 + 每层增量，不设事件级封顶；
- 强度从“相对基础 + baseline”改为“每通道绝对值 + cap”；
- 预设文件改为 `wows_preset_v2.json`。

---

## 排障

**等待遥测文件**

- 确认 ExportShipStatus 存在且 `...\World of Warships\profile\wows_telemetry.jsonl`
  存在；退战后文件清空是正常的。

**有遥测但没强度**

- 确认已进战斗、`debug_log` 打开后能在插件目录 `wows_coyote.log` 看到 `[遥测]`；
- 查看混合器日志里的 `set_strength`；总强度可能被 cap 截断；
- 确认 DGHub 插件设置里的遥测路径与游戏一致。

**战斗中突然停用 / 停止后第一次重启超时**

- 2.9.1 及更早版本在 `debug_log` 开启时可能因 stdout 日志管道写满而阻塞事件循环：
  插件进程还在，但不再收发消息，DGHub 随后注销插件；残留进程会让下一次启动
  等到 10 秒超时。
- 2.9.2 已把日志移到 `wows_coyote.log`，并加入 8 秒看门狗；发生同类阻塞时进程会
  主动退出，下一次启动不会被旧进程占住。
- 排查时优先查看插件目录的 `wows_coyote.log`，而不是只查 DGHub 的 `app.log`；
  插件本地 logging 不再输出到 DGHub 日志面板。

**波形不响或不对**

- 受击/火/水/死亡中只有固定优先级最高的事件播放波形；
- 检查预设名字是否存在于 DGHub 波形库；
- 基础启用且预设非空时，会每 3 秒循环播放基础波形。

**高强度音效不触发**

- 这是 `set_strength` 方案的已知限制；见上面的 DGHub 协议说明。

---

## 文件

```
wows_coyote/
  manifest.json          # v2 配置 schema
  main.py                # 遥测读取 + MixerEngine + DGHub 协议
  README.md
  wows_preset_v2.json    # 随插件分发的可移植预设（不含本机路径）
  wows_coyote.log        # 运行时生成；滚动日志，不随发布包分发
```

模拟台与测试：

```
simulator/         # 离线模拟台（v2）
wows_preset_v2.json
tests/
```

测试：

```bash
py -3.13 tests/test_plugin.py
py -3.13 tests/test_simulator.py
py -3.13 tests/test_integration_dghub.py   # 需要 websockets
```
