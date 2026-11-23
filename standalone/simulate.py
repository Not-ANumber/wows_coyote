# -*- coding: utf-8 -*-
"""Phase 1 离线模拟：不连接游戏/设备，观察强度、owner、模板、clear。

用法：
  py -3.13 standalone/simulate.py
  py -3.13 standalone/simulate.py --preset wows_coyote/wows_preset_v2.json --events
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from standalone.config import load_config
from standalone.device import FakeDevice
from standalone.host import StandaloneHost
from standalone.templates import placeholder_library


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


def battle_start(ship: str = "SimShip", ship_type: str = "BB") -> dict[str, Any]:
    return {"type": "battle_start", "ship_name": ship, "ship_type": ship_type}


def snapshot(hp_pct: float = 100.0, max_hp: int = 60000,
             burning: bool = False, fires: int = 0) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "health": int(round(max_hp * hp_pct / 100.0)),
        "max_health": max_hp,
        "hp_pct": hp_pct,
        "alive": True,
        "burning": burning,
        "flooding": False,
        "fires": fires,
        "floods": 0,
    }


def damage(amount: float) -> dict[str, Any]:
    return {"type": "damage_taken", "source": "damages", "amount": amount}


def status(kind: str, state: str, count: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"type": "status_change", "kind": kind, "state": state}
    if count is not None:
        message["count"] = count
    return message


def print_state(host: StandaloneHost, clock: Clock, label: str) -> None:
    state = host.state()
    totals = state["totals"]
    sched = state["scheduler"]
    a = sched["a"]
    b = sched["b"]
    def fmt(item: dict[str, Any] | None) -> str:
        if not item:
            return "-"
        return "{}/{}".format(item["owner"], item["template"])
    print("t={:5.2f}s  A={:5.1f}% B={:5.1f}%  winner=A:{} B:{}  {}".format(
        clock.t, totals["a"], totals["b"], fmt(a), fmt(b), label))


def run(preset: str | None, show_events: bool) -> int:
    cfg = load_config(preset)
    templates = placeholder_library(cfg)
    sink = FakeDevice()
    clock = Clock()
    host = StandaloneHost(cfg, sink, templates, now_fn=clock)

    print("=" * 72)
    print("Phase 1 simulation · 不连接游戏/设备")
    print("模板源：{}（Phase 1 占位，Phase 2 换官方来源）".format(
        "placeholder" if templates.source == "placeholder" else templates.source))
    print("base={!r} hit={!r} fire={!r} flood={!r} death={!r}".format(
        cfg.get("base_preset"), cfg.get("hit_preset"), cfg.get("fire_preset"),
        cfg.get("flood_preset"), cfg.get("death_preset")))
    print("=" * 72)

    steps = [
        (0.05, lambda: host.on_message(battle_start()), "开局"),
        (0.00, lambda: host.on_message(snapshot(100.0)), "满血快照"),
        (0.10, lambda: host.on_message(status("burning", "started", 1)), "起火"),
        (1.05, lambda: None, "火持续，期望 loop_refill"),
        (0.00, lambda: host.on_message(damage(6000)), "受击 6000"),
        (0.31, lambda: None, "受击窗口结算"),
        (0.01, lambda: host.on_message(damage(6000)), "受击中再次受击"),
        (0.31, lambda: None, "第二个受击窗口（应 pending）"),
        (1.77, lambda: None, "第一个受击窗口结束，期望补播一次"),
        (0.30, lambda: None, "受击结束，回落到火"),
        (0.10, lambda: host.on_message(status("burning", "ended")), "灭火"),
        (0.10, lambda: host.on_message(status("alive", "ended")), "阵亡"),
        (4.10, lambda: None, "死亡持续结束"),
        (0.10, lambda: host.on_message({"type": "battle_end"}), "战局结束"),
    ]
    for advance, action, label in steps:
        clock.advance(advance)
        action()
        host.tick(clock.t)
        print_state(host, clock, label)

    host.stop()
    print("-" * 72)
    if show_events:
        for event in sink.events:
            print(json.dumps(event, ensure_ascii=False))
    else:
        print("plays={} clears={} loop_refills={} strengths={}".format(
            len(sink.plays), len(sink.clears), len(sink.loop_refills),
            len(sink.strengths)))
        print("最后 8 条：")
        for event in sink.events[-8:]:
            print("  " + json.dumps(event, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 standalone 模拟")
    parser.add_argument("--preset", default="", help="预设路径；默认用插件内置 v2 预设")
    parser.add_argument("--events", action="store_true", help="打印全部设备事件")
    args = parser.parse_args(argv)
    return run(args.preset or None, args.events)


if __name__ == "__main__":
    raise SystemExit(main())
