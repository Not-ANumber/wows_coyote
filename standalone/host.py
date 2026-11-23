# -*- coding: utf-8 -*-
"""Phase 1 宿主：引擎 tick + 调度器 tick，不连接设备/游戏。"""

from __future__ import annotations

from typing import Any, Callable

from .device import DeviceSink
from .engine import StandaloneEngine
from .scheduler import WaveformScheduler
from .templates import TemplateLibrary


class StandaloneHost:
    def __init__(
        self,
        config: dict[str, Any],
        sink: DeviceSink,
        templates: TemplateLibrary,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.config = dict(config)
        self.sink = sink
        self.templates = templates
        base_curve = str(config.get("base_curve_path") or "").strip() or None
        hit_curve = str(config.get("hit_curve_path") or "").strip() or None
        self.engine = StandaloneEngine(
            config, sink, now_fn=now_fn,
            base_curve_path=base_curve,
            hit_curve_path=hit_curve,
        )
        self.scheduler = WaveformScheduler(self.engine, sink, templates)
        self.engine.startup_reset()

    def on_message(self, message: dict[str, Any]) -> None:
        self.engine.on_message(message)

    def tick(self, now: float | None = None) -> None:
        now = self.engine._now() if now is None else now
        self.engine.tick(now)
        self.scheduler.tick(now)
        # loop 模板的补帧由设备/传输层负责；FakeDevice 用它模拟 loop_refill。
        self.sink.pump(now)

    def state(self) -> dict[str, Any]:
        state = self.engine.export_state()
        state["scheduler"] = self.scheduler.state()
        state["templates"] = self.templates.state()
        state["template_info"] = self.templates.info()
        state["device"] = self.sink.state() if hasattr(self.sink, "state") else {}
        return state

    def stop(self) -> None:
        self.scheduler.clear_all()
        self.sink.stop_all()
