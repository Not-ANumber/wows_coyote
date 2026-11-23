# -*- coding: utf-8 -*-
"""Phase 1/4: 复用现有 TelemetryReader，把 JSONL 喂给 StandaloneHost。

Phase 4 支持运行时改遥测路径/启停：
  - runner.set_path(path) 会更新配置、触发 reader 重新探测；
  - runner.set_enabled(False) 停止轮询（不会清空当前战局）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .config import apply_config
from .host import StandaloneHost
from .plugin_bridge import plugin


class TelemetryRunner:
    def __init__(self, host: StandaloneHost, path: str | None = None,
                 enabled: bool = True) -> None:
        self.host = host
        self.enabled = bool(enabled)
        if path:
            self.host.config["telemetry_path"] = self._clean(path)
            apply_config(self.host.config)
        self.reader = plugin.TelemetryReader(host.engine)

    @staticmethod
    def _clean(path: str | None) -> str:
        text = str(path or "").strip().strip('"').strip("'")
        if not text:
            return ""
        return os.path.abspath(os.path.expanduser(text))

    def poll(self) -> None:
        if not self.enabled:
            return
        self.reader.poll()
        # 自动发现的路径只写在读取器/插件配置里；同步到 host 配置，
        # 避免下次 apply_config 把空路径写回插件、被误判为“数据源变化”。
        path = str(self.reader.path or "").strip()
        if path and not str(self.host.config.get("telemetry_path") or "").strip():
            self.host.config["telemetry_path"] = path
            apply_config(self.host.config)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled:
            self.reader.last_probe = 0.0

    def set_path(self, path: str | None) -> None:
        cleaned = self._clean(path)
        self.host.config["telemetry_path"] = cleaned
        apply_config(self.host.config)
        self.enabled = True
        self.reader.last_probe = 0.0
        self.reader.wait_started = time.monotonic()

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "path": self.reader.path,
            "offset": self.reader.offset,
            "malformed": self.reader.malformed,
            "found_once": self.reader.found_once,
        }
