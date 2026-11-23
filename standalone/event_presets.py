# -*- coding: utf-8 -*-
"""事件配置方案：五类事件参数的命名保存/读取。

方案是普通 JSON 文件，默认放在 `settings.json` 同级的 `event_presets/` 下，
只包含事件行为参数，不包含遥测路径、设备/输出设置，便于备份和分享。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

EVENT_KEYS = (
    "base_enable", "base_min", "base_max", "base_preset",
    "base_curve_enable", "base_curve_path", "base_channel",
    "hit_enable", "hit_strength", "hit_preset", "hit_duration_s",
    "hit_threshold_pct", "hit_full_pct", "hit_curve_enable",
    "hit_curve_path", "hit_channel",
    "fire_enable", "fire_strength", "fire_stack_add", "fire_preset",
    "fire_channel",
    "flood_enable", "flood_strength", "flood_stack_add", "flood_preset",
    "flood_channel",
    "death_enable", "death_strength", "death_preset", "death_duration_s",
    "death_channel",
)
CURVE_PATH_KEYS = {"base_curve_path", "hit_curve_path"}
PRESET_VERSION = 1
NAME_LIMIT = 40
_INVALID_NAME_CHARS = set('<>:"/\\|?*')
_RESERVED_NAMES = ({"con", "prn", "aux", "nul"}
                   | {"com{}".format(index) for index in range(1, 10)}
                   | {"lpt{}".format(index) for index in range(1, 10)})


def default_event_presets_dir(settings_path: str) -> str:
    """方案目录：与 settings.json 同级，便于整体备份。"""
    base = os.path.dirname(os.path.abspath(
        os.path.expanduser(str(settings_path or "."))))
    return os.path.join(base, "event_presets")


def validate_name(name: Any) -> str:
    text = str(name if name is not None else "").strip()
    if not text:
        raise ValueError("方案名称不能为空")
    if len(text) > NAME_LIMIT:
        raise ValueError("方案名称不能超过 {} 个字符".format(NAME_LIMIT))
    if any(ch in _INVALID_NAME_CHARS or ord(ch) < 32 for ch in text):
        raise ValueError('方案名称不能包含 \\ / : * ? " < > | 或控制字符')
    if text in (".", "..") or text.endswith((".", " ")):
        raise ValueError("方案名称不能以空格或句点结尾")
    if text.split(".")[0].strip().lower() in _RESERVED_NAMES:
        raise ValueError("方案名称与系统保留名冲突：{}".format(text))
    return text


def preset_path(directory: str, name: Any) -> str:
    return os.path.join(os.path.abspath(directory),
                        validate_name(name) + ".json")


def normalize_events(data: Any) -> dict[str, Any]:
    """从方案文件/导入内容中提取事件参数。

    接受 `{"events": {...}}` 包装格式，也接受扁平的事件字典；
    未知字段（版本、名称、时间戳等）会被忽略。
    """
    if not isinstance(data, dict):
        raise ValueError("方案必须是 JSON 对象")
    raw = data.get("events")
    if not isinstance(raw, dict):
        raw = data
    events = {key: raw[key] for key in EVENT_KEYS if key in raw}
    if not events:
        raise ValueError("方案中没有可识别的事件参数")
    return events


def write_preset(directory: str, name: Any, events: dict[str, Any],
                 saved_at: str | None = None) -> str:
    name = validate_name(name)
    clean = {key: events[key] for key in EVENT_KEYS if key in events}
    if not clean:
        raise ValueError("方案中没有可保存的事件参数")
    payload = {
        "version": PRESET_VERSION,
        "name": name,
        "saved_at": saved_at or datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "events": clean,
    }
    path = preset_path(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def read_preset_file(path: str) -> dict[str, Any]:
    """读取方案文件，返回 events 与展示用元数据；文件名是方案身份。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("方案文件读取失败：{}".format(exc)) from exc
    events = normalize_events(data)
    saved_at = ""
    if isinstance(data, dict):
        saved_at = str(data.get("saved_at") or "").strip()
    if not saved_at:
        try:
            saved_at = datetime.fromtimestamp(
                os.path.getmtime(path)).astimezone().isoformat(
                    timespec="seconds")
        except OSError:
            saved_at = ""
    return {"events": events, "saved_at": saved_at}


def list_presets(directory: str) -> list[dict[str, Any]]:
    """列出有效方案；坏文件或非法文件名跳过，不阻断整个列表。"""
    if not os.path.isdir(directory):
        return []
    items: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.lower().endswith(".json"):
            continue
        try:
            if not entry.is_file():
                continue
            name = validate_name(entry.name[:-5])
            data = read_preset_file(entry.path)
            size = entry.stat().st_size
        except (OSError, ValueError):
            continue
        items.append({
            "name": name,
            "saved_at": data["saved_at"],
            "event_count": len(data["events"]),
            "size": int(size),
        })
    items.sort(key=lambda item: item["name"].lower())
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    return items


def load_events(directory: str, name: Any) -> dict[str, Any]:
    path = preset_path(directory, name)
    if not os.path.isfile(path):
        raise ValueError("方案不存在：{}".format(validate_name(name)))
    return read_preset_file(path)["events"]


def delete_preset(directory: str, name: Any) -> str:
    path = preset_path(directory, name)
    if not os.path.isfile(path):
        raise ValueError("方案不存在：{}".format(validate_name(name)))
    try:
        os.remove(path)
    except OSError as exc:
        raise ValueError("删除方案失败：{}".format(exc)) from exc
    return path
