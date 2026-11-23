# 官方 SDK 的波形模板用法（dglab-kit / dglab-kit-python）

> 状态：调研结论（2026-11-09）
> 目的：确认官方 SDK 如何定义、加载、发送、循环和停止波形模板
> 相关：`docs/官方波形模板调研.md`、`docs/脱离DGHub全链路评估.md`、`standalone/`

---

## 0. TL;DR

- 官方 **没有模板库/加载 API**；模板是编译进代码的常量字典，值是 **24 条 V3 帧数组**（16 个 hex 字符 = 8 字节 = 100ms）。
- **V3**：`sendPulse({channel,time,data})`；`time` 单位秒，`data` 是 JSON 数组字符串；单包 ≤100 帧（10s），App 队列 500 帧（50s）。
- **V4**：`device.op` 的 `t=0 AppendPulseData`；`s/c/p/d/im/v/ver/seq`；每 100ms 消费一帧。
- **没有内置循环**；所谓循环是客户端 `frames[index % len]` 平铺/重发。
- **停止**只能靠 `clear`：V3 `clearPulse`（`type:4`），V4 `device.op.clear`；`t=7` 只把强度归零，不清波形队列。
- 两个仓库都是 **GPL-3.0**；复制 `raw` 数据进非 GPL 项目会让分发受 GPL 约束。

---

## 1. 模板数据结构

`dglab-kit/src/waveform/coyote.ts:1-31`：

- `COYOTE_WAVEFORM`：24 个名字的字符串枚举（`EXTRUSTION` … `TEASE_2`）；
- `COYOTE_WAVEFORMS`：`Record<name, {label: {cn, en}, raw: string[]}>`。

Python 对应：`dglab-kit-python/src/dglab_kit_python/waveform/coyote.py:6-29`。

`raw` 每项：

```text
16 个 hex 字符 = 8 字节 = 一帧 = 100ms
前 4 字节：4 组频率（每组 25ms），10–240
后 4 字节：4 组强度，0–100
```

例如 `BUBBLE`（`coyote.ts:39-45`）：

```text
["2D2D2D2D00000000", "2D2D2D2D64646464"]
```

`0A`=10（最低频），`64`=100（满强度）。

**命名陷阱：** 归档 Demo `dglab-websocket-simple/socket/DG_WAVES_V2_V3_simple.js:1-36` 的 `raw` 是 `.pulse` DSL 字符串；`expectedV3` 才是帧数组。Demo 的 16 条 `expectedV3` 与 kit 的前 16 条 `raw` 完全一致；kit 额外多 8 条（挤压、气泡、律动、电波、舞步、攀登、树荫、脉冲）。

---

## 2. V3 怎么发

`dglab-kit/src/socket/v3/index.ts:126-138`、`types.ts:115-119`：

```js
sendPulse({
  channel: "A",          // A/B
  time: 3,               // 秒
  data: ["0A0A0A0A64646464", ...],  // 会被 JSON.stringify
})
```

最终 message 形如：

```text
A:["0A0A0A0A64646464",...]
```

即 **JSON 数组字符串**，不是 hex 拼串。协议上限：

- 单条 pulse ≤100 帧（约 10s）；
- App 队列 ≤500 帧（约 50s）；
- 官方 relay 会按 `time * sendsPerSecond` 拆包（`dglab-websocket-server/v3-server.ts:946-983,1024-1029`）。

**注意：** 本项目 Phase 0 真机验证使用的是 HD2/社区形式 `{type:"msg", message:"pulse-A:[...]"}` + `clear-1/2`；官方 SDK 的 V3 用 `clientMsg`/`type:4`。接入时以真机验证过的 wire format 为准，必要时做适配层，不要把两种格式混用。

---

## 3. V4 怎么发

`dglab-kit/src/socket/v4/index.ts:243-264`、`types.ts:240-289`、`README_CN.md:770-836`：

```json
{
  "s": "<slotId>",
  "c": 0,
  "p": 1,
  "d": 3000,
  "im": true,
  "t": 0,
  "v": ["0A0A0A0A64646464", "..."],
  "ver": 3,
  "seq": 1
}
```

- `t=0` AppendPulseData；
- `c`：0=A，1=B；
- `d`：最长播放时间（ms），0 = 不按时间结束，但帧耗尽仍会结束；
- `p`：优先级 0/1/2，默认 1；
- `im:true`：只替换同设备/通道/类型任务；
- `ver=3`（或省略）：8 字节帧；`ver=2`：`[a,b,interval]`；
- 每 100ms 消费一帧。

---

## 4. 没有模板加载 API

官方 SDK 没有 `list/get/by-name` 之类的模板加载接口，也没有 JSON/文件加载：

- `dglab-kit/README_CN.md:420-451`：直接 `COYOTE_WAVEFORMS[COYOTE_WAVEFORM.X].raw`；
- Python README:489：同一套常量；
- 全部编译进代码。

所以“官方模板库”不是一个可查询/可导入的运行时资源，而是代码常量。

---

## 5. 循环与停止

**官方 SDK 不含循环。** 官方 relay 的“循环”是平铺：

`dglab-websocket-server/v3-server.ts:1014-1022`：

```text
frames[index % frames.length]
```

填满 `time * 10` 帧。

**V3 停止/抢占：**

- `clearPulse` = `{type:4, channel, message:"clear"}`（`v3/index.ts:112-119`）；
- 官方 relay 的 `queuePulse` 会先发 `clear-1/2`，等 150ms 再发新波形（`v3-server.ts:631-670`）；
- 本项目 Phase 0 已验证 `clear-1/2` 形式。

**V4 停止：**

- `device.op.clear`：`{s,c}` 清指定通道，`{s}` 清设备，无参清全部（`v4/index.ts:284-298`）；
- `t=7 SetIntensity` 只把强度归零，**不清波形队列**。

---

## 6. 对 wows_coyote 的映射建议

### 6.1 Template

```python
@dataclass(frozen=True)
class Template:
    id: str
    name_cn: str
    name_en: str
    frames: tuple[str, ...]   # 16 hex chars, 100ms
    source: str = "dglab-kit@<commit>"
```

Phase 1 的 `standalone/templates.py` 用 `units` 命名，语义相同；Phase 2 可改为 `frames` 或保留别名。

### 6.2 play(owner, channel, template, duration, loop)

```text
n = ceil(duration * 10)
seq = [frames[i % len(frames)] for i in range(n)]
loop=False 时按 n 截断，或补末帧
```

**V3：**

- 每 100 帧切一块，`time = 10`；
- 提前 1–2s 发下一块，预排 ≤5 块（≤500 帧）；
- 循环由本地 Scheduler/传输层重发，不依赖设备端；Phase 1 已用 `play(loop=True)` + `DeviceSink.pump()` 表达“clear 前一直循环”；
- `stop()` 只做 `clearPulse(channel)`。

**V4：**

- 每块 ≤100 帧；
- `d = len(chunk) * 100`，`p=1`；
- 首块 `im=True`，后续 `False`；
- `stop()` 走 `device.op.clear`。

> 本仓库已按上述规则实现 V4：`standalone/v4_device.py`（控制方）+
> `standalone/v4_relay.py`（本机中继）；强度用 `t=3 AddIntensity`
> 增量 + `t=7 SetIntensity(0)` 归零。细节、限制与真机验收项见
> [`docs/standalone_V4.md`](standalone_V4.md)。

### 6.3 与 Phase 1 调度器的关系

Phase 1 调度器已经按 owner/loop/one-shot 做本地调度；Phase 3 传输层只需要：

- 把 `Template.frames` 按 `duration/loop` 展开成 seq；
- 按 V3/V4 分片规则发送；
- 在 owner 变化/结束时执行对应 clear；
- 保持 1–2s 预排，不要一次性塞满 500 帧。

---

## 7. 许可

- `dglab-kit/LICENSE`、`dglab-kit-python/LICENSE`：**GNU GPL v3**；
- `raw` 帧数组是该仓库的受版权保护内容；
- 复制进 wows_coyote 会让分发整体受 GPL 约束；
- 如果项目不是 GPL：
  - 不要内嵌 `raw` 数据；
  - 只存模板 id，运行时从用户提供的 `dglab-kit-python` 源码目录导入（作为依赖而非复制）；
  - 或让用户自备 JSON/`.pulse`；
  - 不要用“官方模板”名义分发社区/自制数据。
- 如果确实要随包分发：保留版权声明与 GPL 全文、标注修改、提供对应源码。

---

## 8. 本仓库实现（Phase 2）

`standalone/official_templates.py`：

- `load_from_sdk(sdk_path=None)`：从 `dglab_kit_python.waveform.coyote` 读取 `COYOTE_WAVEFORMS`；
  支持已安装包，也支持 `--sdk-path` 指向源码（直接按文件加载，绕开包 `__init__`）；
  未指定路径时自动查找常见目录 / `DGLAB_KIT_PYTHON_PATH`。
- `load_from_manifest(path)`：读取 `source/commit/license/sha256/waves[]`；
  缺 `source`/`license`、任一帧非法、`sha256` 不匹配 → 整份拒绝。
- `choose_defaults(library)`：五类事件 → 官方模板 id 的启发式映射；
  默认 `base=RAINFALL / hit=PULSATING / fire=GRAINY / flood=TIDE / death=COMPRESS`。
- `format_catalog(library)`：`--list-templates` 使用。

**分发方案 B（已采用）**：项目以 GPL-3.0 分发，随包
`standalone/templates/official_manifest.json` 含 24 条官方模板
（dglab-kit-python commit `4cce771c0fd93d043f3e83bbb0467e4197cde62e`）。
默认 `--templates bundled`，不需要 clone SDK：

```bash
py -3.13 standalone/run.py --open-browser
py -3.13 standalone/run.py --list-templates
```

可选：直接用 SDK 源码（更新/对比时）；`dglab-kit-python` **没有发布到 PyPI**：

```bash
git clone https://github.com/dungeonlab-open/dglab-kit-python.git E:\dglab-kit-python
py -3.13 standalone/run.py --templates sdk --list-templates   # 自动发现
py -3.13 standalone/run.py --templates sdk --sdk-path "E:\dglab-kit-python"

# 重新生成随包 manifest
py -3.13 tools/build_official_manifest.py --sdk-path "E:\dglab-kit-python" ^
  --commit 4cce771c0fd93d043f3e83bbb0467e4197cde62e ^
  --out standalone/templates/official_manifest.json

# 构建 GPL 源码分发包
py -3.13 tools/build_standalone_zip.py
```

**未实现**：`.pulse` 直接转换。官方 Demo 的 `expectedV3` 应走 manifest；不要复刻未公开的
滑块映射。**许可**：随包 manifest 的数据来自 GPL-3.0 的 dglab-kit-python；
对应源码见 `SOURCE_OFFER.md`，第三方声明见 `THIRD_PARTY.md`。

---

## 参考

- [dglab-kit](https://github.com/dungeonlab-open/dglab-kit)
- [dglab-kit-python](https://github.com/dungeonlab-open/dglab-kit-python)
- [dglab-websocket-simple（归档 Demo）](https://github.com/dungeonlab-open/dglab-websocket-simple)
- [dglab-bluetooth-protocol](https://github.com/dungeonlab-open/dglab-bluetooth-protocol)
- 调研笔记：未随仓库分发（原稿引用的本机临时目录已移除）
