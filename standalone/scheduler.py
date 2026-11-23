# -*- coding: utf-8 -*-
"""每通道波形调度器：owner / priority / replace / clear / loop。

loop 语义：
  - base/fire/flood 这类持续状态调用 play(loop=True)；
  - 真正的补帧由 DeviceSink.pump()（传输层）负责，直到 clear；
  - 调度器只在赢家变化、状态结束或战局结束时 clear，不做 3 秒重发。

一次性模板（hit/death）仍由调度器做 pending 节流，窗口结束最多补播一次。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .device import DeviceSink
from .templates import Template, TemplateLibrary

LOG = logging.getLogger("standalone.scheduler")

LOOP_EVENTS = ("base", "fire", "flood")


@dataclass
class ActiveWave:
    owner: str
    template: Template
    channel: str
    event_start: float
    started_at: float
    ends_at: float
    loop: bool
    pending: bool = False
    expired: bool = False

    def state(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "template": self.template.id,
            "source": self.template.source,
            "loop": bool(self.loop),
            "loop_until_clear": bool(self.loop),
            "started_at": round(self.started_at, 3),
            "ends_at": round(self.ends_at, 3),
            "pending": bool(self.pending),
            "expired": bool(self.expired),
        }


class WaveformScheduler:
    def __init__(self, engine: Any, sink: DeviceSink,
                 templates: TemplateLibrary) -> None:
        self.engine = engine
        self.sink = sink
        self.templates = templates
        self.active: dict[str, ActiveWave | None] = {"a": None, "b": None}
        self._missing_templates: set[str] = set()

    # ------------------------------------------------------------------
    # 赢家解析
    # ------------------------------------------------------------------
    def _resolve(self, channel: str, now: float):
        spec = self.engine._pick_wave(channel, now)
        if not spec:
            return None
        template_id = str(spec.get("preset") or "")
        template = self.templates.get(template_id)
        if template is None:
            # 没有模板就只输出强度；不要回退到自制/社区数据。
            if template_id not in self._missing_templates:
                self._missing_templates.add(template_id)
                LOG.warning("模板不存在，跳过波形：%s (channel=%s)",
                            template_id or "(空)", channel)
            return None
        loop = bool(template.loop or spec.get("event") in LOOP_EVENTS)
        duration = max(0.1, float(spec.get("duration") or
                                  template.default_ms / 1000.0))
        return spec, template, loop, duration

    def _play(self, channel: str, spec: dict[str, Any], template: Template,
              loop: bool, duration: float, now: float) -> ActiveWave:
        owner = str(spec.get("event") or template.id)
        self.sink.play(
            owner=owner,
            channel=channel,
            template=template,
            duration_s=duration,
            loop=loop,
            append=False,
        )
        return ActiveWave(
            owner=owner,
            template=template,
            channel=channel,
            event_start=float(spec.get("start") or 0.0),
            started_at=now,
            ends_at=now + duration,
            loop=loop,
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = self.engine._now() if now is None else now
        for channel in ("a", "b"):
            self._tick_channel(channel, now)

    def _tick_channel(self, channel: str, now: float) -> None:
        resolved = self._resolve(channel, now)
        active = self.active[channel]

        if resolved is None:
            if active is not None:
                self.sink.clear(channel, owner=active.owner)
                self.active[channel] = None
            return

        spec, template, loop, duration = resolved
        owner = str(spec.get("event") or template.id)
        event_start = float(spec.get("start") or 0.0)

        if active is None:
            self.active[channel] = self._play(
                channel, spec, template, loop, duration, now)
            return

        # 不同事件/模板：先 clear 旧 owner，再播放新赢家。
        if active.owner != owner or active.template.id != template.id:
            self.sink.clear(channel, owner=active.owner)
            self.active[channel] = self._play(
                channel, spec, template, loop, duration, now)
            return

        # 持续状态：loop=True，由 sink.pump() 补帧，直到 clear。
        if loop:
            if event_start != active.event_start:
                # 重新起火/进入新状态：立即替换模板实例。
                self.sink.clear(channel, owner=active.owner)
                self.active[channel] = self._play(
                    channel, spec, template, loop, duration, now)
            # 同一 loop 内不再做任何重发；补帧交给 DeviceSink.pump()。
            return

        # 一次性模板（受击/死亡）：播放期间的新事件只标记 pending。
        if event_start != active.event_start:
            if now < active.ends_at:
                active.pending = True
                return
            self.sink.clear(channel, owner=active.owner)
            self.active[channel] = self._play(
                channel, spec, template, loop, duration, now)
            return

        if now >= active.ends_at:
            if active.pending:
                self.sink.clear(channel, owner=active.owner)
                self.active[channel] = self._play(
                    channel, spec, template, loop, duration, now)
                return
            # 播放窗口结束但引擎仍认为它是赢家：不再补发；
            # 等赢家变化/结束时由上面的 clear 分支收尾。
            active.expired = True

    # ------------------------------------------------------------------
    # 收尾/状态
    # ------------------------------------------------------------------
    def clear_all(self) -> None:
        for channel in ("a", "b"):
            active = self.active[channel]
            if active is not None:
                self.sink.clear(channel, owner=active.owner)
                self.active[channel] = None

    def state(self) -> dict[str, Any]:
        return {
            "a": self.active["a"].state() if self.active["a"] else None,
            "b": self.active["b"].state() if self.active["b"] else None,
        }
