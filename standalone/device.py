# -*- coding: utf-8 -*-
"""设备输出抽象与 V3/V4 共用语义。

实现：
  - V3Device：V3 Socket（本地 WS + App 直接扫码）；
  - V4Device：V4 Relay 控制方（官方 Relay 或本机中继）；
  - FakeDevice：测试用，不连接设备。

V3/V4 共用同一套 DeviceSink 语义：百分比强度、loop、clear、stop_all。
loop 语义：
  play(..., loop=True) 表示“在 clear 之前一直循环”。
  V3/V4 协议本身没有设备端无限 loop，传输层必须用本地 pump 持续补帧；
  FakeDevice 用 loop_refill 事件模拟这个过程。
  clear(channel, owner) 是唯一的停止方式。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from .templates import Template


@dataclass
class Limits:
    a: int = 200
    b: int = 200


class DeviceSink(abc.ABC):
    @property
    @abc.abstractmethod
    def limits(self) -> Limits:
        ...

    @abc.abstractmethod
    def set_strength(self, a: int, b: int, force: bool = False) -> None:
        ...

    @abc.abstractmethod
    def play(self, owner: str, channel: str, template: Template,
             duration_s: float, loop: bool = False,
             append: bool = False) -> None:
        ...

    @abc.abstractmethod
    def clear(self, channel: str, owner: str | None = None) -> None:
        ...

    @abc.abstractmethod
    def pump(self, now: float) -> None:
        """推进 loop：持续状态在 clear 前由这里补帧。"""

    @abc.abstractmethod
    def stop_all(self) -> None:
        ...


class FakeDevice(DeviceSink):
    """记录所有输出，不连接真实设备。"""

    def __init__(self, refill_s: float = 1.0) -> None:
        self.refill_s = max(0.1, float(refill_s))
        self._limits = Limits(200, 200)
        self.strength = (0, 0)
        self.events: list[dict[str, Any]] = []
        self.strengths: list[tuple[int, int]] = []
        self.plays: list[dict[str, Any]] = []
        self.clears: list[dict[str, Any]] = []
        self.loop_refills: list[dict[str, Any]] = []
        #: channel -> {owner, template, next_refill, refills}
        self.loops: dict[str, dict[str, Any]] = {}

    @property
    def limits(self) -> Limits:
        return self._limits

    def set_strength(self, a: int, b: int, force: bool = False) -> None:
        a = max(0, min(int(a), self._limits.a))
        b = max(0, min(int(b), self._limits.b))
        event = {"op": "strength", "a": a, "b": b, "force": bool(force)}
        self.events.append(event)
        self.strengths.append((a, b))
        self.strength = (a, b)

    def play(self, owner: str, channel: str, template: Template,
             duration_s: float, loop: bool = False,
             append: bool = False) -> None:
        event = {
            "op": "play",
            "owner": owner,
            "channel": channel,
            "template": template.id,
            "template_name": template.name,
            "source": template.source,
            "duration_s": round(float(duration_s), 3),
            "loop": bool(loop),
            "append": bool(append),
        }
        self.events.append(event)
        self.plays.append(event)
        if loop:
            # 新 loop 接管该通道；next_refill=0 表示下一次 pump 设定首个补帧点。
            self.loops[channel] = {
                "owner": owner,
                "template": template.id,
                "next_refill": 0.0,
                "refills": 0,
            }
        else:
            self.loops.pop(channel, None)

    def clear(self, channel: str, owner: str | None = None) -> None:
        event = {"op": "clear", "channel": channel, "owner": owner}
        self.events.append(event)
        self.clears.append(event)
        loop = self.loops.get(channel)
        if loop is not None and (owner is None or loop["owner"] == owner):
            self.loops.pop(channel, None)

    def pump(self, now: float) -> None:
        """模拟传输层的补帧：loop 激活时每 refill_s 追加一次。"""
        for channel, loop in list(self.loops.items()):
            if loop["next_refill"] <= 0:
                loop["next_refill"] = now + self.refill_s
                continue
            while now >= loop["next_refill"]:
                event = {
                    "op": "loop_refill",
                    "owner": loop["owner"],
                    "channel": channel,
                    "template": loop["template"],
                    "refill": loop["refills"] + 1,
                }
                self.events.append(event)
                self.loop_refills.append(event)
                loop["refills"] += 1
                loop["next_refill"] += self.refill_s

    def stop_all(self) -> None:
        self.events.append({"op": "stop"})
        self.clear("a")
        self.clear("b")
        self.set_strength(0, 0, force=True)

    def state(self) -> dict[str, Any]:
        return {
            "strength": {"a": self.strength[0], "b": self.strength[1]},
            "plays": len(self.plays),
            "clears": len(self.clears),
            "loop_refills": len(self.loop_refills),
            "loops": {
                channel: {
                    "owner": loop["owner"],
                    "template": loop["template"],
                    "refills": loop["refills"],
                }
                for channel, loop in self.loops.items()
            },
            "last_events": self.events[-5:],
        }

    def last_play(self, channel: str | None = None) -> dict[str, Any] | None:
        for event in reversed(self.plays):
            if channel is None or event["channel"] == channel:
                return event
        return None
