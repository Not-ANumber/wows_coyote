# -*- coding: utf-8 -*-
"""只读波形模板库（Phase 1 使用占位模板）。

Phase 2 会把这里替换成“官方来源清单 + 校验”的加载器：
  - dglab-kit / dglab-kit-python 的 raw（GPL-3.0）
  - 官方 Demo expectedV3（无 OSS 许可，个人/用户导入）
  - 用户导入的官方 .pulse（best-effort）
  - DGHub export-pulse（标注 DGHub，不算官方）

本阶段只验证调度器：模板是占位帧，source="placeholder"，不连接设备。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 60Hz / 50% 强度，仅用于占位；不是官方模板。
PLACEHOLDER_UNIT = "3C3C3C3C32323232"
UNIT_CHARS = 16


@dataclass
class Template:
    id: str
    name: str
    units: list[str]
    loop: bool = False
    source: str = "placeholder"
    default_ms: int = 3000
    sha256: str = ""
    license: str = ""
    commit: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    _preview: dict[str, list[float]] = field(
        default_factory=dict, repr=False, compare=False)

    def preview(self, points: int = 32) -> dict[str, list[float]]:
        """强度/频率的降采样预览，仅用于 UI 折线图。"""
        if self._preview:
            return self._preview
        strengths: list[float] = []
        freqs: list[float] = []
        for unit in self.units:
            try:
                raw = bytes.fromhex(unit)
            except ValueError:
                continue
            if len(raw) != 8:
                continue
            freqs.append(sum(raw[:4]) / 4.0)
            strengths.append(sum(raw[4:]) / 4.0)

        def downsample(values: list[float]) -> list[float]:
            if len(values) <= points:
                return [round(value, 1) for value in values]
            bucket = len(values) / points
            result: list[float] = []
            for index in range(points):
                start = int(index * bucket)
                end = max(start + 1, int((index + 1) * bucket))
                chunk = values[start:end]
                result.append(round(sum(chunk) / len(chunk), 1))
            return result

        self._preview = {
            "strength": downsample(strengths),
            "freq": downsample(freqs),
        }
        return self._preview

    def validate(self) -> bool:
        if not self.id:
            raise ValueError("模板 id 不能为空")
        if not self.units:
            raise ValueError("模板 {} 没有帧".format(self.id))
        for index, unit in enumerate(self.units, 1):
            if not isinstance(unit, str) or len(unit) != UNIT_CHARS:
                raise ValueError("{} 第 {} 帧必须是 {} 位 HEX".format(
                    self.id, index, UNIT_CHARS))
            try:
                raw = bytes.fromhex(unit)
            except ValueError as exc:
                raise ValueError("{} 第 {} 帧不是合法 HEX：{}".format(
                    self.id, index, exc)) from exc
            freqs = list(raw[:4])
            strengths = list(raw[4:])
            if not all(10 <= value <= 240 for value in freqs):
                raise ValueError("{} 第 {} 帧频率越界".format(self.id, index))
            if not all(0 <= value <= 100 for value in strengths):
                raise ValueError("{} 第 {} 帧强度越界".format(self.id, index))
        return True

    def state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "license": self.license,
            "commit": self.commit,
            "sha256": self.sha256,
            "loop": bool(self.loop),
            "default_ms": int(self.default_ms),
            "frames": len(self.units),
            "preview": self.preview(),
        }

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "frames": list(self.units),
            "sha256": self.sha256,
            "loop": bool(self.loop),
            "default_ms": int(self.default_ms),
            "source": self.source,
            "license": self.license,
        }


class TemplateLibrary:
    def __init__(self, source: str = "placeholder", license: str = "",
                 commit: str = "", rejected: list[str] | None = None) -> None:
        self.source = source
        self.license = license
        self.commit = commit
        self.templates: dict[str, Template] = {}
        self.rejected: list[str] = list(rejected or [])

    def add(self, template: Template) -> Template:
        template.validate()
        self.templates[template.id] = template
        return template

    def get(self, template_id: str | None) -> Template | None:
        if not template_id:
            return None
        return self.templates.get(str(template_id))

    def names(self) -> list[str]:
        return sorted(self.templates)

    def state(self) -> list[dict[str, Any]]:
        return [self.templates[name].state() for name in self.names()]

    def info(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "license": self.license,
            "commit": self.commit,
            "count": len(self.templates),
            "rejected": list(self.rejected),
        }

    def __len__(self) -> int:
        return len(self.templates)


def placeholder_library(cfg: dict[str, Any]) -> TemplateLibrary:
    """按配置里的 *_preset 名字创建占位模板。

    只用于 Phase 1 调度器测试；Phase 2 换成官方模板清单。
    """
    library = TemplateLibrary("placeholder")
    items: Iterable[tuple[str, str, bool, int]] = (
        ("base", "base_preset", True, 3000),
        ("hit", "hit_preset", False, _duration_ms(cfg, "hit_duration_s", 2000)),
        ("fire", "fire_preset", True, 3000),
        ("flood", "flood_preset", True, 3000),
        ("death", "death_preset", False, _duration_ms(cfg, "death_duration_s", 4000)),
    )
    for _event, key, loop, default_ms in items:
        name = str(cfg.get(key, "") or "").strip()
        if not name:
            continue
        library.add(Template(
            id=name,
            name=name,
            units=[PLACEHOLDER_UNIT],
            loop=loop,
            source="placeholder",
            default_ms=max(100, int(default_ms)),
        ))
    return library


def _duration_ms(cfg: dict[str, Any], key: str, default: float) -> int:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(100, int(round(value * 1000.0)))
