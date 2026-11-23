# -*- coding: utf-8 -*-
"""Standalone 配置：schema 默认值 + 事件默认 JSON + bundled preset + 用户设置。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import event_presets
from .plugin_bridge import ROOT as APP_ROOT, plugin

LOG = logging.getLogger("standalone.config")

DEFAULT_PRESET = os.path.join(plugin.PLUGIN_DIR, "wows_preset_v2.json")
DEFAULT_EVENTS_PATH = os.path.join(
    APP_ROOT, "standalone", "presets", "default_events.json")
CURVE_PATH_KEYS = {"base_curve_path", "hit_curve_path"}
OBS_DEFAULT_PORT = 8787
OBS_KEYS = {"obs_enabled", "obs_port"}
THEMES = ("dark", "light")
THEME_KEY = "theme"
OVERLAY_OPACITY_DEFAULT = 100
OBS_OPACITY_DEFAULT = 86
OPACITY_KEYS = {"overlay_opacity", "obs_opacity"}
DETAIL_KEYS = {"overlay_detail", "obs_detail"}
LOCK_KEYS = {"overlay_locked"}
TRANSPORTS = ("v3", "v4")
EXTRA_KEYS = ({THEME_KEY, "transport", "v4_relay", "v4_slot",
               "check_updates"}
              | CURVE_PATH_KEYS | OBS_KEYS | OPACITY_KEYS | DETAIL_KEYS
              | LOCK_KEYS)


def default_user_settings_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "wows_coyote", "settings.json")


USER_SETTINGS_PATH = default_user_settings_path()


def _cast_user_setting(key: str, value: Any) -> tuple[bool, Any]:
    if key in CURVE_PATH_KEYS:
        text = str(value or "").strip().strip('"').strip("'")
        if not text:
            return True, ""
        return True, os.path.abspath(os.path.expanduser(text))
    if key in EXTRA_KEYS:
        if key in ("obs_enabled", "check_updates") or key in LOCK_KEYS:
            if isinstance(value, bool):
                return True, value
            return True, str(value).strip().lower() in (
                "1", "true", "yes", "on")
        if key == "obs_port":
            try:
                number = int(float(value))
            except Exception:
                return False, None
            return True, max(1024, min(65535, number))
        if key == THEME_KEY:
            text = str(value or "").strip().lower()
            if text not in THEMES:
                return False, None
            return True, text
        if key == "transport":
            text = str(value or "").strip().lower()
            if text not in TRANSPORTS:
                return False, None
            return True, text
        if key == "v4_relay":
            return True, str(value or "").strip() or "official"
        if key == "v4_slot":
            return True, str(value or "").strip()
        if key in OPACITY_KEYS:
            try:
                number = int(float(value))
            except Exception:
                return False, None
            low = 30 if key == "overlay_opacity" else 20
            return True, max(low, min(100, number))
        if key in DETAIL_KEYS:
            if isinstance(value, bool):
                return True, value
            return True, str(value).strip().lower() in (
                "1", "true", "yes", "on")
        return False, None
    if key in plugin.SCHEMA_TYPES:
        return True, plugin.auto_cast(key, value)
    return False, None


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right))


def _apply_default_events(path: str, cfg: dict[str, Any]) -> set[str]:
    """把随包事件默认 JSON 合并进配置；坏文件只记录并回退 schema/预设默认值。"""
    if not path:
        return set()
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        LOG.warning("事件默认配置不存在，回退 schema/bundled 预设：%s", path)
        return set()
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            events = event_presets.normalize_events(json.load(handle))
    except Exception as exc:
        LOG.warning("事件默认配置无效，回退 schema/bundled 预设：%s（%s）",
                    path, exc)
        return set()
    applied: set[str] = set()
    for key, value in events.items():
        if (plugin.SCHEMA_TYPES and key not in plugin.SCHEMA_TYPES
                and key not in event_presets.EVENT_KEYS):
            continue
        cfg[key] = plugin.auto_cast(key, value)
        applied.add(key)
    return applied


def load_config(preset_path: str | None = None,
                user_path: str | None = None,
                events_path: str | None = None) -> dict[str, Any]:
    """schema 默认值 → 事件默认 JSON → 预设 → 用户设置。

    用户设置默认在 %LOCALAPPDATA%\\wows_coyote\\settings.json；
    `user_path=""` 可显式关闭，便于测试。
    事件默认值来自随包 `standalone/presets/default_events.json`；
    显式传入的 `preset_path` 仍可覆盖其中任意事件字段。
    """
    cfg: dict[str, Any] = dict(plugin.SCHEMA_DEFAULTS)
    cfg.setdefault("obs_enabled", False)
    cfg.setdefault("obs_port", OBS_DEFAULT_PORT)
    cfg.setdefault(THEME_KEY, "dark")
    cfg.setdefault("overlay_opacity", OVERLAY_OPACITY_DEFAULT)
    cfg.setdefault("obs_opacity", OBS_OPACITY_DEFAULT)
    cfg.setdefault("overlay_detail", False)
    cfg.setdefault("obs_detail", False)
    cfg.setdefault("overlay_locked", False)
    cfg.setdefault("check_updates", True)
    cfg.setdefault("transport", "v4")
    cfg.setdefault("v4_relay", "official")
    cfg.setdefault("v4_slot", "")

    events_path = DEFAULT_EVENTS_PATH if events_path is None else events_path
    event_defaults = _apply_default_events(events_path, cfg)

    path = preset_path or DEFAULT_PRESET
    bundled_preset = not preset_path or _same_path(preset_path, DEFAULT_PRESET)
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("预设根节点必须是 JSON 对象：{}".format(path))
            for key, value in data.items():
                if plugin.SCHEMA_TYPES and key not in plugin.SCHEMA_TYPES:
                    continue
                # 随包 v2 预设的事件字段是旧版占位名；已由
                # default_events.json 提供的事件字段不再被它覆盖。
                if bundled_preset and key in event_defaults:
                    continue
                cfg[key] = plugin.auto_cast(key, value)

    user_path = USER_SETTINGS_PATH if user_path is None else user_path
    if user_path:
        user_path = os.path.abspath(os.path.expanduser(str(user_path)))
        if os.path.isfile(user_path):
            try:
                with open(user_path, "r", encoding="utf-8-sig") as handle:
                    data = json.load(handle)
            except Exception:
                data = {}
            if isinstance(data, dict):
                for key, value in data.items():
                    ok, cast = _cast_user_setting(key, value)
                    if ok:
                        cfg[key] = cast
    return cfg


def save_user_settings(path: str, updates: dict[str, Any]) -> None:
    """合并写入用户设置（原子替换）；只写调用方给的键。"""
    if not path:
        return
    path = os.path.abspath(os.path.expanduser(str(path)))
    data: dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            data = {}
    for key, value in updates.items():
        if key in EXTRA_KEYS or key in plugin.SCHEMA_TYPES:
            data[key] = value
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def apply_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """让插件里的 cfg_num/cfg_bool/cfg_str 读到这份配置。"""
    plugin.plugin_cfg = dict(cfg)
    return plugin.plugin_cfg


def clone_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(cfg)
