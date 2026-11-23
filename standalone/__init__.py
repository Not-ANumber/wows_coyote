# -*- coding: utf-8 -*-
"""wows_coyote host (Phase 1–4).

复用 wows_coyote 的 TelemetryReader/MixerEngine 事件数学，新增：
  - DeviceSink：设备输出抽象
  - WaveformScheduler：owner/priority/replace/clear/loop
  - V3Device：V3 Socket 传输（Phase 3）
  - V4Device / LocalV4Relay：V4 Relay 控制方 + 本机中继
  - UIServer：主控制台/独立设置页/overlay（Phase 4）

真实设备输出仍受安全上限约束；官方模板加载在 Phase 2 接入。
"""

__version__ = "1.0.0"
