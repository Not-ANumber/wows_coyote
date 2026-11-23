# -*- coding: utf-8 -*-
"""Phase 4 UI 骨架：主控制台 + 独立设置页 + 独立 overlay 页。

本地 HTTP（默认 127.0.0.1 随机端口），无鉴权；只在本机使用。
UI 只读 host/device/telemetry 状态，动作只暴露急停/重新武装/归零/退出。
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, event_presets
from .config import (
    OBS_DEFAULT_PORT, THEMES, USER_SETTINGS_PATH, apply_config,
    save_user_settings,
)
from .plugin_bridge import ROOT as APP_ROOT

LOG = logging.getLogger("standalone.ui")
UI_DIR = os.path.join(APP_ROOT, "standalone", "ui")
MAX_BODY = 64 * 1024

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/settings": ("settings.html", "text/html; charset=utf-8"),
    "/events": ("settings.html", "text/html; charset=utf-8"),
    "/overlay": ("overlay.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

CONFIG_KEYS = (
    "preset_file_path", "telemetry_path", "polling_rate", "telemetry_timeout_s",
    "theme", "transport", "v4_relay", "check_updates",
    "overlay_opacity", "obs_opacity", "overlay_detail", "obs_detail",
    "overlay_locked",
    "base_curve_path", "hit_curve_path",
    "obs_enabled", "obs_port",
    "base_enable", "base_min", "base_max", "base_preset",
    "base_curve_enable", "base_channel",
    "hit_enable", "hit_strength", "hit_preset", "hit_duration_s",
    "hit_threshold_pct", "hit_full_pct", "hit_curve_enable", "hit_channel",
    "fire_enable", "fire_strength", "fire_stack_add", "fire_preset",
    "fire_channel",
    "flood_enable", "flood_strength", "flood_stack_add", "flood_preset",
    "flood_channel",
    "death_enable", "death_strength", "death_preset", "death_duration_s",
    "death_channel",
)

BOOL_KEYS = {
    "obs_enabled", "check_updates", "overlay_detail", "obs_detail",
    "overlay_locked",
    "base_enable", "base_curve_enable", "hit_enable", "hit_curve_enable",
    "fire_enable", "flood_enable", "death_enable",
}
CHANNEL_KEYS = {
    "base_channel", "hit_channel", "fire_channel", "flood_channel",
    "death_channel",
}
TEMPLATE_KEYS = {
    "base_preset", "hit_preset", "fire_preset", "flood_preset", "death_preset",
}
# key -> (low, high, integer)
NUMERIC_RANGES = {
    "overlay_opacity": (30, 100, True),
    "obs_opacity": (20, 100, True),
    "obs_port": (1024, 65535, True),
    "polling_rate": (50, 2000, True),
    "telemetry_timeout_s": (10, 600, True),
    "base_min": (0, 100, False),
    "base_max": (0, 100, False),
    "hit_strength": (0, 100, False),
    "hit_threshold_pct": (0, 100, False),
    "hit_full_pct": (0, 100, False),
    "hit_duration_s": (0.1, 20, False),
    "fire_strength": (0, 100, False),
    "fire_stack_add": (0, 100, False),
    "flood_strength": (0, 100, False),
    "flood_stack_add": (0, 100, False),
    "death_strength": (0, 100, False),
    "death_duration_s": (0.1, 20, False),
}
CURVE_PATH_KEYS = {"base_curve_path", "hit_curve_path"}
PATH_KEYS = {"telemetry_path"} | CURVE_PATH_KEYS

# UI 事件流只显示用户能理解的事件名，不暴露 owner/模板内部 id。
EVENT_LABELS = {
    "base": "基础",
    "hit": "受击",
    "fire": "起火",
    "flood": "进水",
    "death": "死亡",
}


def _pct(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _clean_path(value: Any) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


class UIBridge:
    def __init__(self, host: Any, device: Any, runner: Any = None,
                 config: dict[str, Any] | None = None,
                 user_settings_path: str | None = None,
                 event_presets_dir: str | None = None,
                 on_exit: Any = None,
                 on_transport_change: Any = None,
                 update_checker: Any = None) -> None:
        self.host = host
        self.device = device
        self.runner = runner
        self.config = dict(config or {})
        self.user_settings_path = (
            USER_SETTINGS_PATH if user_settings_path is None
            else str(user_settings_path))
        self.event_presets_dir = (
            os.path.abspath(str(event_presets_dir)) if event_presets_dir
            else event_presets.default_event_presets_dir(
                self.user_settings_path or USER_SETTINGS_PATH))
        self.on_exit = on_exit
        self.on_transport_change = on_transport_change
        self.update_checker = update_checker
        self.hotkeys: dict[str, str] = {}
        self.started = time.monotonic()
        self.logs: deque[dict[str, str]] = deque(maxlen=80)
        self._lock = threading.RLock()
        self._last_totals: tuple[Any, Any] | None = None
        self._last_wave: dict[str, tuple[Any, Any]] = {}
        self.obs = OBSServer(self)

    # ---------------------------------------------------------------- 采样
    def _log(self, text: str) -> None:
        self.logs.appendleft({"t": time.strftime("%H:%M:%S"), "text": text})

    def _sample(self) -> dict[str, Any]:
        state = self.host.state()
        totals = state.get("totals") or {}
        key = (totals.get("a"), totals.get("b"))
        if self._last_totals is not None and key != self._last_totals:
            self._log("强度 {}% / {}%".format(_pct(key[0]), _pct(key[1])))
        self._last_totals = key

        scheduler = state.get("scheduler") or {}
        for channel in ("a", "b"):
            item = scheduler.get(channel)
            wave = ((item or {}).get("owner"), (item or {}).get("template"))
            if self._last_wave.get(channel) != wave:
                self._last_wave[channel] = wave
                if item:
                    owner = str(item.get("owner") or "")
                    self._log("{} · {}".format(
                        channel.upper(), EVENT_LABELS.get(owner, "效果")))
                else:
                    self._log("{} · 静默".format(channel.upper()))
        return state

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict[str, Any]:
        with self._lock:
            host_state = self._sample()
            device_state = self.device.state() if hasattr(self.device, "state") else {}
            telemetry_state = self.runner.state() if self.runner is not None else {}
            config = {key: self.config.get(key) for key in CONFIG_KEYS}
            # 自动发现的遥测文件路径只存在于读取器/插件配置里；把它显示出来，
            # 避免设置页把它当成空路径保存后触发“数据源配置变化”。
            effective_telemetry = str(telemetry_state.get("path") or "").strip()
            if effective_telemetry and not str(
                    config.get("telemetry_path") or "").strip():
                config["telemetry_path"] = effective_telemetry
            engine = getattr(self.host, "engine", None)
            curve_paths = {
                "base": str(getattr(engine, "base_curve_path", "") or ""),
                "hit": str(getattr(engine, "hit_curve_path", "") or ""),
            }
            curves = (
                engine.curve_state()
                if engine is not None and hasattr(engine, "curve_state")
                else {}
            )
            return {
                "time": round(time.monotonic() - self.started, 1),
                "host": host_state,
                "device": device_state,
                "telemetry": telemetry_state,
                "config": config,
                "curve_paths": curve_paths,
                "curves": curves,
                "obs": self.obs_state(),
                "update": self.update_state(),
                "hotkeys": dict(self.hotkeys),
                "templates": host_state.get("templates") or [],
                "event_presets": self.event_preset_state(),
                "logs": list(self.logs),
                "app_version": __version__,
                "ui_version": "phase4",
            }

    def update_state(self) -> dict[str, Any]:
        if self.update_checker is None:
            return {
                "enabled": False,
                "current": __version__,
                "checking": False,
                "checked_at": "",
                "error": "",
                "available": False,
                "latest": "",
                "notes": "",
                "notes_url": "",
                "published_at": "",
                "skipped": False,
            }
        return self.update_checker.state()

    def obs_state(self) -> dict[str, Any]:
        enabled = bool(self.config.get("obs_enabled", False))
        configured = int(self.config.get("obs_port", OBS_DEFAULT_PORT))
        running = self.obs.running
        port = self.obs.port if running else configured
        return {
            "enabled": enabled,
            "running": running,
            "port": port,
            "url": "http://127.0.0.1:{}/obs".format(port),
            "error": self.obs.error,
        }

    def sync_obs(self) -> None:
        enabled = bool(self.config.get("obs_enabled", False))
        port = int(self.config.get("obs_port", OBS_DEFAULT_PORT))
        if not enabled:
            self.obs.stop()
            return
        if self.obs.running and self.obs.port == port:
            return
        self.obs.start(port)

    def stop(self) -> None:
        self.obs.stop()
        if self.update_checker is not None:
            try:
                self.update_checker.shutdown()
            except Exception:
                LOG.debug("停止更新检查失败", exc_info=True)

    # ---------------------------------------------------------------- 事件方案
    def event_config(self) -> dict[str, Any]:
        """当前五类事件参数（不含设备/遥测/输出设置）。"""
        return {key: self.config.get(key)
                for key in event_presets.EVENT_KEYS}

    def event_preset_state(self) -> dict[str, Any]:
        with self._lock:
            error = ""
            try:
                items = event_presets.list_presets(self.event_presets_dir)
            except Exception as exc:  # 目录/文件异常不应拖垮整个状态轮询
                items = []
                error = str(exc)
            return {
                "directory": self.event_presets_dir,
                "items": items,
                "error": error,
            }

    def _event_updates(self, events: dict[str, Any],
                       lenient: bool) -> tuple[dict[str, Any], list[str]]:
        """校验方案中的事件参数。

        lenient=True 用于读取/导入：模板缺失或函数表路径失效时清空该字段
        并给出提示，避免换机器后整份方案不可用。
        """
        updates: dict[str, Any] = {}
        warnings: list[str] = []
        for key, value in events.items():
            if key not in event_presets.EVENT_KEYS:
                continue
            try:
                updates[key] = self._cast_setting(key, value)
            except ValueError as exc:
                if not lenient:
                    raise
                if key in TEMPLATE_KEYS:
                    updates[key] = ""
                    warnings.append("波形不存在，已改为无波形：{}".format(
                        value))
                elif key in CURVE_PATH_KEYS:
                    updates[key] = ""
                    warnings.append("函数表路径无效，已清空")
                else:
                    raise ValueError("方案参数无效（{}）：{}".format(
                        key, exc)) from exc
        return updates, warnings

    @staticmethod
    def _check_event_ranges(values: dict[str, Any]) -> None:
        if ("base_min" in values and "base_max" in values
                and float(values.get("base_min", 0))
                > float(values.get("base_max", 0))):
            raise ValueError("基础：满血强度不能大于空血强度")
        if ("hit_threshold_pct" in values and "hit_full_pct" in values
                and float(values.get("hit_threshold_pct", 0))
                > float(values.get("hit_full_pct", 0))):
            raise ValueError("受击：触发阈值不能大于满强度阈值")

    def _event_snapshot(self, payload: Any) -> dict[str, Any]:
        """保存方案时以当前已应用配置为底，再合并调用方给的事件参数。"""
        events = self.event_config()
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("事件参数必须是 JSON 对象")
            updates, _warnings = self._event_updates(payload, lenient=False)
            events.update(updates)
        self._check_event_ranges(events)
        return events

    def event_preset_action(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("请求格式错误")
        action = str(data.get("action") or "").strip().lower()
        with self._lock:
            if action == "save":
                name = event_presets.validate_name(data.get("name"))
                events = self._event_snapshot(data.get("settings"))
                event_presets.write_preset(
                    self.event_presets_dir, name, events)
                self._log("事件方案已保存：{}".format(name))
                return self.state()
            if action == "load":
                name = event_presets.validate_name(data.get("name"))
                events = event_presets.load_events(
                    self.event_presets_dir, name)
                updates, warnings = self._event_updates(events, lenient=True)
                if updates:
                    self.update_settings({"settings": updates})
                self._log("事件方案已读取：{}".format(name))
                state = self.state()
                state["warnings"] = warnings
                return state
            if action == "delete":
                name = event_presets.validate_name(data.get("name"))
                event_presets.delete_preset(self.event_presets_dir, name)
                self._log("事件方案已删除：{}".format(name))
                return self.state()
            if action == "export":
                name = event_presets.validate_name(data.get("name"))
                events = event_presets.load_events(
                    self.event_presets_dir, name)
                return {
                    "ok": True,
                    "name": name,
                    "payload": {
                        "version": event_presets.PRESET_VERSION,
                        "name": name,
                        "events": events,
                    },
                }
            if action == "import":
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("导入内容必须是 JSON 对象")
                raw_name = data.get("name")
                if not str(raw_name or "").strip():
                    raw_name = payload.get("name") or ""
                name = event_presets.validate_name(raw_name)
                events = event_presets.normalize_events(payload)
                updates, warnings = self._event_updates(events, lenient=True)
                self._check_event_ranges(updates)
                event_presets.write_preset(
                    self.event_presets_dir, name, updates)
                self._log("事件方案已导入：{}".format(name))
                state = self.state()
                state["warnings"] = warnings
                return state
        raise ValueError("未知方案操作：{}".format(action or "(空)"))

    # ---------------------------------------------------------------- 动作
    def action(self, name: str) -> dict[str, Any]:
        name = str(name or "")
        if name in ("estop", "stop", "ui_close"):
            reason = "UI 关闭" if name == "ui_close" else "UI 急停"
            if hasattr(self.device, "emergency_stop"):
                self.device.emergency_stop(reason)
            else:
                self.device.stop_all()
            self.host.scheduler.clear_all()
            self._log("{}：已归零".format(reason))
        elif name == "rearm":
            if hasattr(self.device, "rearm"):
                self.device.rearm()
            self._log("已恢复输出")
        elif name == "zero":
            self.host.scheduler.clear_all()
            self.device.stop_all()
            self._log("已归零")
        elif name == "shutdown":
            if self.on_exit is None:
                raise ValueError("退出动作未启用")
            self._log("退出程序：已归零")
            self.on_exit("UI 退出")
        else:
            raise ValueError("未知动作：{}".format(name))
        return self.state()

    def update_action(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("请求格式错误")
        if self.update_checker is None:
            raise ValueError("更新检查未启用")
        action = str(data.get("action") or "").strip().lower()
        if action == "check":
            self.update_checker.check_async(force=True)
        elif action == "skip":
            self.update_checker.skip(str(data.get("version") or ""))
        elif action == "unskip":
            self.update_checker.unskip()
        elif action == "open_notes":
            if not self.update_checker.open_notes():
                raise ValueError("没有可打开的更新说明")
        else:
            raise ValueError("未知更新动作：{}".format(action or "(空)"))
        return self.state()

    def _cast_setting(self, key: str, value: Any) -> Any:
        if key == "theme":
            text = str(value or "").strip().lower()
            if text not in THEMES:
                raise ValueError("主题必须是 dark 或 light")
            return text
        if key == "transport":
            text = str(value or "").strip().lower()
            if text not in ("v3", "v4"):
                raise ValueError("连接方式必须是 v3 或 v4")
            return text
        if key == "v4_relay":
            text = str(value or "").strip()
            lower = text.lower()
            if lower in ("official", "local"):
                return lower
            if text.startswith(("ws://", "wss://")):
                return text
            raise ValueError("V4 Relay 必须是 official/local 或 ws(s):// 地址")
        if key in BOOL_KEYS:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if key in CHANNEL_KEYS:
            text = str(value).strip().lower()
            if text in ("a+b", "ab"):
                text = "both"
            if text not in ("a", "b", "both"):
                raise ValueError("{} 必须是 a/b/both".format(key))
            return text
        if key in TEMPLATE_KEYS:
            text = str(value or "").strip()
            if text and self.host.templates.get(text) is None:
                raise ValueError("模板不存在：{}".format(text))
            return text
        if key in PATH_KEYS:
            text = _clean_path(value)
            if text and not os.path.exists(text):
                raise ValueError("路径不存在：{}".format(text))
            if key in CURVE_PATH_KEYS and text and not os.path.isfile(text):
                raise ValueError("函数表必须是 JSON 文件：{}".format(text))
            return text
        if key in NUMERIC_RANGES:
            low, high, integer = NUMERIC_RANGES[key]
            try:
                number = float(value)
            except Exception:
                raise ValueError("{} 必须是数字".format(key))
            if not math.isfinite(number) or number < low or number > high:
                raise ValueError("{} 必须在 {}–{}".format(key, low, high))
            return int(round(number)) if integer else number
        raise ValueError("未知设置：{}".format(key))

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = data.get("settings") if isinstance(data.get("settings"), dict) else data
        updates = {key: self._cast_setting(key, value)
                   for key, value in payload.items()}
        if not updates:
            return self.state()
        old_telemetry = str(self.config.get("telemetry_path") or "").strip()
        new_telemetry = str(
            updates.get("telemetry_path", old_telemetry) or "").strip()
        if (not new_telemetry and not old_telemetry
                and self.runner is not None):
            effective = str(self.runner.state().get("path") or "").strip()
            if effective:
                updates["telemetry_path"] = effective
                new_telemetry = effective
        changed = {key: value for key, value in updates.items()
                   if self.config.get(key) != value}
        if not changed:
            # 设置没有变化时不要 apply_config / 重连遥测；保存并刷新只刷新二维码。
            return self.state()
        theme_only = set(updates) == {"theme"}
        if "overlay_locked" in changed:
            self._log("悬浮窗已锁定：鼠标点击穿透" if changed["overlay_locked"]
                      else "悬浮窗已解锁")

        merged = dict(self.config)
        merged.update(updates)
        self._check_event_ranges(merged)

        old_transport = str(self.config.get("transport") or "v4").strip().lower()
        old_relay = str(self.config.get("v4_relay") or "official").strip()
        new_transport = str(updates.get("transport", old_transport)).strip().lower()
        new_relay = str(updates.get("v4_relay", old_relay)).strip()
        transport_changed = (new_transport != old_transport
                             or (new_transport == "v4" and new_relay != old_relay))
        if transport_changed:
            if self.on_transport_change is None:
                raise ValueError("运行中切换连接方式未启用；请重启程序")
            try:
                new_device = self.on_transport_change(new_transport, new_relay)
            except Exception as exc:
                raise ValueError("切换连接方式失败：{}".format(exc)) from exc
            if new_device is not None:
                self.device = new_device
            self._log("连接方式已切换：{}".format(
                "V4 Relay" if new_transport == "v4" else "V3 Socket"))
        if (self.runner is not None
                and not str(self.host.config.get("telemetry_path") or "").strip()):
            effective = str(self.runner.state().get("path") or "").strip()
            if effective:
                self.host.config["telemetry_path"] = effective
        self.config.update(updates)
        self.host.config.update(updates)
        if "check_updates" in updates and self.update_checker is not None:
            self.update_checker.set_enabled(bool(updates["check_updates"]))
        apply_config(self.host.config)

        if "telemetry_path" in updates:
            if self.runner is not None:
                if str(updates["telemetry_path"]).strip() != old_telemetry:
                    self.runner.set_path(updates["telemetry_path"])
            else:
                self._log("遥测路径已保存，下次启动生效")
        engine = getattr(self.host, "engine", None)
        if engine is not None and hasattr(engine, "set_curve_paths"):
            base = (str(self.host.config.get("base_curve_path") or "")
                    if "base_curve_path" in updates else None)
            hit = (str(self.host.config.get("hit_curve_path") or "")
                   if "hit_curve_path" in updates else None)
            if base is not None or hit is not None:
                engine.set_curve_paths(base, hit)
        if "obs_enabled" in updates or "obs_port" in updates:
            self.sync_obs()

        try:
            save_user_settings(self.user_settings_path, updates)
        except Exception as exc:
            self._log("保存用户设置失败：{}".format(exc))
        if not theme_only:
            self._log("设置更新：已保存")
        return self.state()

    def browse(self, path: str | None) -> dict[str, Any]:
        """本地文件浏览器：列目录/文件，供设置页选择遥测文件。"""
        if not path:
            entries = []
            for letter in "CDEFGH":
                root = "{}:\\".format(letter)
                if os.path.exists(root):
                    entries.append({
                        "name": root, "path": root,
                        "is_dir": True, "size": 0, "jsonl": False,
                    })
            return {"path": "", "parent": "", "entries": entries}

        current = _clean_path(path)
        if os.path.isfile(current):
            current = os.path.dirname(current)
        if not os.path.isdir(current):
            raise ValueError("目录不存在：{}".format(current))

        parent = os.path.dirname(current)
        if parent == current:
            parent = ""
        entries = []
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        is_dir = entry.is_dir()
                        size = 0 if is_dir else entry.stat().st_size
                    except OSError:
                        continue
                    name = entry.name
                    entries.append({
                        "name": name,
                        "path": entry.path,
                        "is_dir": bool(is_dir),
                        "size": int(size),
                        "jsonl": name.lower().endswith(".jsonl"),
                    })
        except OSError as exc:
            raise ValueError("无法读取目录：{}".format(exc))
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return {"path": current, "parent": parent, "entries": entries}

    def qr_path(self) -> str:
        state = self.device.state() if hasattr(self.device, "state") else {}
        # V4 在拿到控制方 ID 之前没有可用的二维码；不要继续提供旧文件。
        if "qr_payload" in state and not state.get("qr_payload"):
            return ""
        return str(state.get("qr_path") or "")


class UIHandler(BaseHTTPRequestHandler):
    server_version = "wowsCoyoteUI/0.1"

    @property
    def bridge(self) -> UIBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _local_request(self) -> bool:
        """更新动作只接受本机 Host，避免被本机网页跨站触发。"""
        host = str(self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            end = host.find("]")
            host = host[1:end] if end != -1 else host
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str, content_type: str) -> None:
        path = os.path.join(UI_DIR, name)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        if content_type.startswith("text/html"):
            # 首帧就使用已保存主题，避免窗口先闪一下默认深色。
            theme = str(self.bridge.config.get("theme") or "dark").lower()
            if theme not in ("dark", "light"):
                theme = "dark"
            body = body.replace(
                b'<html lang="zh-CN">',
                '<html lang="zh-CN" data-theme="{}">'.format(theme).encode(),
                1)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.bridge.state())
            return
        if path == "/api/event-presets":
            self._json(self.bridge.event_preset_state())
            return
        if path == "/api/browse":
            query = parse_qs(urlparse(self.path).query)
            try:
                self._json(self.bridge.browse(query.get("path", [""])[0]))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            return
        if path == "/qr.svg":
            qr = self.bridge.qr_path()
            if qr and os.path.isfile(qr):
                with open(qr, "rb") as handle:
                    body = handle.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "二维码尚未生成"}, 404)
            return
        item = STATIC.get(path)
        if item is None:
            self._json({"error": "not found"}, 404)
            return
        self._file(item[0], item[1])

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json({"error": "bad body"}, 400)
                return
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                self._json({"error": "bad json"}, 400)
                return
            if path == "/api/action":
                self._json(self.bridge.action(data.get("action", "")))
            elif path == "/api/settings":
                self._json(self.bridge.update_settings(data))
            elif path == "/api/event-presets":
                self._json(self.bridge.event_preset_action(data))
            elif path == "/api/update":
                if not self._local_request():
                    self._json({"error": "forbidden"}, 403)
                    return
                self._json(self.bridge.update_action(data))
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            LOG.warning("UI POST 异常：%s", exc)
            self._json({"error": "internal error"}, 500)


class _UIServerHTTP(ThreadingHTTPServer):
    """关闭窗口时页面轮询可能先断开；忽略这类连接错误，避免 stderr 报错。"""

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            LOG.debug("UI 客户端已断开：%s", exc)
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
                10053, 10054, 10058):
            LOG.debug("UI 连接已关闭：%s", exc)
            return
        super().handle_error(request, client_address)


class OBSHandler(UIHandler):
    """只读 OBS 浏览器源：只提供 /obs 和 /api/state。"""

    server_version = "wowsCoyoteOBS/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.bridge.state())
            return
        if path == "/obs":
            self._file("overlay.html", "text/html; charset=utf-8")
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        self._json({"error": "read only"}, 405)


class OBSServer:
    def __init__(self, bridge: UIBridge) -> None:
        self.bridge = bridge
        self.host = "127.0.0.1"
        self.port = 0
        self.url = ""
        self.error = ""
        self._server: _UIServerHTTP | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int) -> None:
        with self._lock:
            self.stop()
            self.port = int(port)
            try:
                server = _UIServerHTTP((self.host, self.port), OBSHandler)
            except OSError as exc:
                self.error = str(exc)
                self._server = None
                return
            server.daemon_threads = True
            server.bridge = self.bridge  # type: ignore[attr-defined]
            actual = int(server.server_address[1])
            self.port = actual
            self.url = "http://{}:{}/obs".format(self.host, actual)
            thread = threading.Thread(
                target=server.serve_forever, name="wows-obs", daemon=True)
            self._server = server
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            self.url = ""
            self.error = ""
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    LOG.debug("停止 OBS 服务失败", exc_info=True)
                try:
                    server.server_close()
                except Exception:
                    LOG.debug("关闭 OBS socket 失败", exc_info=True)
            if thread is not None:
                thread.join(timeout=2.0)


class UIServer:
    def __init__(self, bridge: UIBridge, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        self.bridge = bridge
        self.host = host
        self.port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> str:
        self._server = _UIServerHTTP(
            (self.host, self.port), UIHandler)
        self._server.daemon_threads = True
        self._server.bridge = self.bridge  # type: ignore[attr-defined]
        actual_port = self._server.server_address[1]
        self.base_url = "http://{}:{}".format(self.host, actual_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wows-ui", daemon=True)
        self._thread.start()
        LOG.info("UI 已启动：%s", self.base_url)
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                LOG.debug("停止 UI 服务失败", exc_info=True)
            try:
                self._server.server_close()
            except Exception:
                LOG.debug("关闭 UI socket 失败", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
