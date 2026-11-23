# -*- coding: utf-8 -*-
"""把现有 MixerEngine 的输出从 DGHub 队列改到 DeviceSink。

只覆写三个输出点：
  - _send_strength：强度 → sink
  - _play_waveform：波形交给 WaveformScheduler，不在引擎里发
  - _arbitrate_waveforms：关闭旧的 3 秒重发/优先级逻辑
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .config import apply_config
from .device import DeviceSink
from .plugin_bridge import plugin

LOG = logging.getLogger("standalone.engine")


class StandaloneEngine(plugin.MixerEngine):
    def __init__(
        self,
        config: dict[str, Any],
        sink: DeviceSink,
        now_fn: Callable[[], float] | None = None,
        base_curve_path: str | None = None,
        hit_curve_path: str | None = None,
    ) -> None:
        apply_config(config)
        super().__init__(
            now_fn=now_fn,
            base_curve_path=base_curve_path or plugin.BASE_CURVE_PATH,
            hit_curve_path=hit_curve_path or plugin.HIT_CURVE_PATH,
        )
        self.sink = sink

    def set_curve_paths(self, base_curve_path: str | None = None,
                        hit_curve_path: str | None = None) -> None:
        """运行时切换函数表路径；空字符串表示回到随包默认。"""
        if base_curve_path is not None:
            self.base_curve_path = base_curve_path or plugin.BASE_CURVE_PATH
            self._base_curve.set_path(self.base_curve_path)
        if hit_curve_path is not None:
            self.hit_curve_path = hit_curve_path or plugin.HIT_CURVE_PATH
            self._hit_curve.set_path(self.hit_curve_path)

    def curve_state(self) -> dict[str, dict[str, Any]]:
        """当前函数表路径、校验错误和采样点，供设置页预览。"""
        now = self._now()
        result: dict[str, dict[str, Any]] = {}
        for key, table in (("base", self._base_curve),
                           ("hit", self._hit_curve)):
            table.refresh(now)
            result[key] = {
                "path": table.path,
                "error": table.error,
                "points": [[float(x), float(y)]
                           for x, y in (table.points or [])],
            }
        return result

    def _send_strength(self, value_a: float, value_b: float,
                       force: bool = False) -> None:
        value_a = plugin._clamp_int(value_a, 0, 100)
        value_b = plugin._clamp_int(value_b, 0, 100)
        if (not force and self._last_sent["a"] == value_a
                and self._last_sent["b"] == value_b):
            return
        self.sink.set_strength(value_a, value_b, force=force)
        self._last_sent = {"a": value_a, "b": value_b}
        LOG.debug("sink set_strength A=%d B=%d force=%s",
                  value_a, value_b, force)

    def _play_waveform(self, event_id: str, channel: str, spec: dict[str, Any]) -> None:
        # Phase 1：波形由 WaveformScheduler 统一处理，引擎不再自己发。
        LOG.debug("engine ignored waveform %s %s", event_id, channel)

    def _arbitrate_waveforms(self, now: float) -> None:
        # 关闭 DGHub 版的 3 秒重发/优先级逻辑；
        # 清掉引擎内部的受击重触发标记，避免旧状态残留到后续阶段。
        self._hit_retrigger = False

    def begin_battle(self, ship_desc: str = "", adopt: bool = False) -> None:
        # 新战局/中途接入先强制绝对归零，避免 V4 增量强度在旧值上叠加。
        self._send_strength(0, 0, force=True)
        super().begin_battle(ship_desc, adopt)

    def end_battle(self, reason: str = "", notify: bool = True) -> None:
        # 战局结束/超时/文件重置都强制绝对归零，而不是依赖去重后的相对值。
        self._send_strength(0, 0, force=True)
        super().end_battle(reason, notify)

    def startup_reset(self) -> None:
        # DGHub 版会发 permanent -100；standalone 只把 sink 归零。
        self._last_sent = {"a": None, "b": None}
        self._send_strength(0, 0, force=True)

    def resend_strength(self) -> None:
        """设备重新连接后调用：下一次 tick 重新下发当前强度。

        tick 里的 _send_strength 有相同值去重；App 重连/中途配对时
        百分比往往没变，如果不去重，设备会一直停在绑定时的 0。
        """
        self._last_sent = {"a": None, "b": None}
