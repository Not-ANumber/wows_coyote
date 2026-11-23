# -*- coding: utf-8 -*-
"""官方模板加载（Phase 2）。

官方 SDK 没有模板加载 API，模板是代码常量；本模块提供三种只读来源：

  1. load_from_sdk(sdk_path=None)
     从用户安装/源码目录的 dglab-kit-python 读取 COYOTE_WAVEFORMS（GPL-3.0）。
     不复制 raw 数据到本项目。
  2. load_from_manifest(path)
     读取带 source/commit/license/sha256/waves[] 的官方清单；
     manifest 任一帧/校验/许可不合法 → 整份拒绝（fail-closed）。
  3. choose_defaults(library)
     五类事件 → 官方模板 id 的启发式默认映射，可用 CLI 覆盖。

没有模板时返回空库；调度器只输出强度、不播放波形。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import sys
from typing import Any

from .templates import Template, TemplateLibrary

LOG = logging.getLogger("standalone.official_templates")

SDK_PACKAGE = "dglab-kit-python"
SDK_SOURCE = "dglab-kit-python"
SDK_LICENSE = "GPL-3.0-only"
EVENTS = ("base", "hit", "fire", "flood", "death")
PREFERENCES: dict[str, tuple[str, ...]] = {
    # 尽量贴近当前 DGHub 预设：雨水冲刷 / 连击 / 颗粒摩擦 / 潮汐 / 压缩
    "base": ("RAINFALL", "雨水", "BREATHING", "呼吸", "TIDE", "潮汐",
             "GRAINY", "颗粒", "AIR_WAVES", "电波"),
    "hit": ("PULSATING", "连击", "QUICK_RUB", "快速按捏", "GRADUALRUB",
            "按捏渐强", "PULSE", "脉冲", "EXTRUSTION", "挤压"),
    "fire": ("GRAINY", "颗粒", "RAINFALL", "雨水", "RHYTHM", "律动",
             "BUBBLE", "气泡", "AIR_WAVES", "电波"),
    "flood": ("TIDE", "潮汐", "RIPPLE", "涟漪", "AIR_WAVES", "电波",
              "SHADE", "树荫"),
    "death": ("COMPRESS", "压缩", "RHYTHMIC", "节奏", "CLIMB", "攀登",
              "SHADE", "树荫", "DANCE", "舞步"),
}


class TemplateLoadError(Exception):
    pass


def frames_sha256(frames: list[str]) -> str:
    """清单校验和：每个 16 位 HEX 帧按 \n 连接后做 sha256。"""
    return hashlib.sha256("\n".join(frames).encode("utf-8")).hexdigest()


def _normalize_frames(frames: Any) -> list[str]:
    if not isinstance(frames, (list, tuple)):
        raise TemplateLoadError("frames/expectedV3/raw 必须是数组")
    out: list[str] = []
    for item in frames:
        text = str(item).strip()
        if text.lower().startswith("0x"):
            text = text[2:]
        out.append(text.upper())
    return out


def _sdk_version() -> str:
    try:
        return importlib.metadata.version(SDK_PACKAGE)
    except Exception:
        return "source"


SDK_ENV = "DGLAB_KIT_PYTHON_PATH"


def _sdk_search_paths() -> list[str]:
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(project)
    local = os.environ.get("LOCALAPPDATA") or ""
    raw = [
        os.environ.get(SDK_ENV, ""),
        os.path.join(parent, "dglab-kit-python"),
        os.path.join(project, "vendor", "dglab-kit-python"),
        os.path.join(local, "dglab-kit-python") if local else "",
    ]
    paths: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        path = os.path.abspath(os.path.expanduser(item))
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _discover_sdk_path() -> str | None:
    for candidate in _sdk_search_paths():
        if _find_coyote_file(candidate):
            return candidate
    return None


def _find_coyote_file(sdk_path: str) -> str | None:
    path = os.path.abspath(os.path.expanduser(str(sdk_path)))
    candidates = [
        path if path.lower().endswith(".py") else "",
        os.path.join(path, "src", "dglab_kit_python", "waveform", "coyote.py"),
        os.path.join(path, "dglab_kit_python", "waveform", "coyote.py"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _load_sdk_module(sdk_path: str | None):
    if sdk_path:
        path = os.path.abspath(os.path.expanduser(str(sdk_path)))
        coyote_file = _find_coyote_file(path)
        if coyote_file:
            # 直接按文件加载，绕开包 __init__ 可能缺少的 ovc 等模块。
            spec = importlib.util.spec_from_file_location(
                "dglab_sdk_coyote", coyote_file)
            if spec is None or spec.loader is None:
                raise TemplateLoadError("无法加载 {}".format(coyote_file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        # 可能是 src 目录或已安装包的父目录。
        for candidate in (os.path.join(path, "src"), path):
            if os.path.isdir(candidate) and candidate not in sys.path:
                sys.path.insert(0, candidate)
        try:
            return importlib.import_module("dglab_kit_python.waveform.coyote")
        except ImportError as exc:
            raise TemplateLoadError(
                "指定的 SDK 路径下找不到 "
                "src/dglab_kit_python/waveform/coyote.py：{}".format(path)
            ) from exc

    # 没指定路径：先尝试已安装/已注入的包，再自动发现常见目录。
    try:
        return importlib.import_module("dglab_kit_python.waveform.coyote")
    except ImportError:
        discovered = _discover_sdk_path()
        if discovered:
            LOG.info("自动发现官方 SDK：%s", discovered)
            return _load_sdk_module(discovered)
        tried = _sdk_search_paths()
        raise TemplateLoadError(
            "未找到 dglab-kit-python。它没有发布到 PyPI。\n"
            "已尝试：\n  " + "\n  ".join(tried or ["(没有可搜索的路径)"]) + "\n"
            "请 clone 后用 --sdk-path 指向它：\n"
            "  git clone https://github.com/dungeonlab-open/dglab-kit-python.git "
            "E:\\dglab-kit-python\n"
            "  py -3.13 standalone/run.py --templates sdk "
            "--sdk-path \"E:\\dglab-kit-python\""
        ) from None


def load_from_sdk(sdk_path: str | None = None) -> TemplateLibrary:
    """从官方 Python SDK 的 COYOTE_WAVEFORMS 读取 24 条郊狼模板。"""
    module = _load_sdk_module(sdk_path)
    waveforms = getattr(module, "COYOTE_WAVEFORMS", None)
    if not isinstance(waveforms, dict) or not waveforms:
        raise TemplateLoadError("SDK 里没有 COYOTE_WAVEFORMS")

    version = _sdk_version()
    library = TemplateLibrary(
        source=SDK_SOURCE, license=SDK_LICENSE, commit=version)
    rejected: list[str] = []
    for key, record in waveforms.items():
        template_id = (key.value if hasattr(key, "value") else str(key)).strip()
        if not isinstance(record, dict):
            rejected.append("{}: 记录不是对象".format(template_id))
            continue
        label = record.get("label") if isinstance(record.get("label"), dict) else {}
        name = str(label.get("cn") or label.get("en") or template_id)
        try:
            frames = _normalize_frames(record.get("raw"))
            template = Template(
                id=template_id,
                name=name,
                units=frames,
                loop=False,
                source=SDK_SOURCE,
                default_ms=max(100, len(frames) * 100),
                sha256=frames_sha256(frames),
                license=SDK_LICENSE,
                commit=version,
            )
            template.validate()
        except Exception as exc:
            rejected.append("{}: {}".format(template_id, exc))
            LOG.warning("官方模板 %s 无效，已跳过：%s", template_id, exc)
            continue
        library.add(template)

    library.rejected = rejected
    if not len(library):
        raise TemplateLoadError("SDK 模板全部无效；fail-closed")
    LOG.info("已从官方 SDK 加载 %d 条模板（%s）", len(library), version)
    return library


def load_from_manifest(path: str) -> TemplateLibrary:
    """读取官方清单。任一帧/校验/许可不合法 → 整份拒绝。"""
    path = os.path.abspath(os.path.expanduser(str(path)))
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise TemplateLoadError("读取清单失败：{}".format(exc)) from exc
    if not isinstance(data, dict):
        raise TemplateLoadError("清单根节点必须是 JSON 对象")

    schema = data.get("schema_version", 1)
    if schema != 1:
        raise TemplateLoadError("不支持的 schema_version：{}".format(schema))

    source = str(data.get("source") or "").strip()
    license_name = str(data.get("license") or "").strip()
    commit = str(data.get("commit") or "").strip()
    if not source or not license_name:
        raise TemplateLoadError("清单必须包含 source 和 license（fail-closed）")

    raw_waves = data.get("waves")
    if isinstance(raw_waves, dict):
        entries = [dict(record or {}, id=key) if isinstance(record, dict)
                   else {"id": key, "frames": record}
                   for key, record in raw_waves.items()]
    elif isinstance(raw_waves, list):
        entries = raw_waves
    else:
        raise TemplateLoadError("清单必须包含 waves 数组或对象")
    if not entries:
        raise TemplateLoadError("清单没有 waves")

    library = TemplateLibrary(source=source, license=license_name, commit=commit)
    for index, record in enumerate(entries, 1):
        if not isinstance(record, dict):
            raise TemplateLoadError("第 {} 个模板不是对象".format(index))
        template_id = str(record.get("id") or record.get("name") or "").strip()
        if not template_id:
            raise TemplateLoadError("第 {} 个模板缺少 id/name".format(index))
        name = str(record.get("name") or template_id)
        frames_raw = (record.get("frames") or record.get("expectedV3")
                      or record.get("raw"))
        try:
            frames = _normalize_frames(frames_raw)
            expected_sha = str(record.get("sha256") or "").strip()
            actual_sha = frames_sha256(frames)
            if expected_sha and expected_sha != actual_sha:
                raise TemplateLoadError(
                    "{} sha256 不匹配（期望 {}，实际 {}）".format(
                        template_id, expected_sha, actual_sha))
            wave_license = str(record.get("license") or license_name).strip()
            wave_source = str(record.get("source") or source).strip()
            template = Template(
                id=template_id,
                name=name,
                units=frames,
                loop=bool(record.get("loop", False)),
                source=wave_source,
                default_ms=max(100, int(record.get("default_ms")
                                       or len(frames) * 100)),
                sha256=actual_sha,
                license=wave_license,
                commit=commit,
            )
            template.validate()
        except TemplateLoadError:
            raise
        except Exception as exc:
            raise TemplateLoadError(
                "{} 无效：{}".format(template_id, exc)) from exc
        library.add(template)

    LOG.info("已从清单加载 %d 条模板（source=%s, license=%s）",
             len(library), source, license_name)
    return library


def choose_defaults(library: TemplateLibrary) -> dict[str, str]:
    """按名称启发式给五类事件选默认模板；没有模板返回空。"""
    names = library.names()
    if not names:
        return {}
    used: set[str] = set()
    result: dict[str, str] = {}
    for index, event in enumerate(EVENTS):
        chosen = ""
        for token in PREFERENCES.get(event, ()):
            needle = token.upper()
            for name in names:
                if name in used:
                    continue
                template = library.templates[name]
                haystack = "{} {}".format(template.id, template.name).upper()
                if needle in haystack:
                    chosen = name
                    break
            if chosen:
                break
        if not chosen:
            for offset in range(len(names)):
                candidate = names[(index + offset) % len(names)]
                if candidate not in used:
                    chosen = candidate
                    break
        if not chosen:
            chosen = names[index % len(names)]
        used.add(chosen)
        result[event] = chosen
    return result


def format_catalog(library: TemplateLibrary) -> str:
    info = library.info()
    lines = [
        "source={} license={} count={}".format(
            info["source"], info["license"], info["count"]),
    ]
    if info["rejected"]:
        lines.append("rejected: " + "; ".join(info["rejected"]))
    for template in library.templates.values():
        lines.append("{:<18} {:<10} {:>4} frames  {:>6.1f}s  {}".format(
            template.id, template.name, len(template.units),
            template.default_ms / 1000.0, template.license or "-"))
    return "\n".join(lines)
