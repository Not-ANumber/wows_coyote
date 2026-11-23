# -*- coding: utf-8 -*-
"""P3 真机验收用的固定事件序列（不依赖游戏/模拟台）。

run.py --test-sequence 在 App 绑定后按时间注入这些消息，
用来观察 base → 火 → 受击 → 火 → base → 死亡 → 归零。
"""

from __future__ import annotations

from typing import Any


def _battle_start() -> dict[str, Any]:
    return {"type": "battle_start", "ship_name": "P3-Test", "ship_type": "BB"}


def _snapshot(hp_pct: float = 100.0, max_hp: int = 60000) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "health": int(round(max_hp * hp_pct / 100.0)),
        "max_health": max_hp,
        "hp_pct": hp_pct,
        "alive": True,
        "burning": False,
        "flooding": False,
        "fires": 0,
        "floods": 0,
    }


def _damage(amount: float) -> dict[str, Any]:
    return {"type": "damage_taken", "source": "damages", "amount": amount}


def _status(kind: str, state: str, count: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "status_change", "kind": kind, "state": state}
    if count is not None:
        message["count"] = count
    return message


def build_test_sequence() -> list[tuple[float, dict[str, Any]]]:
    """返回 (相对秒数, 遥测消息) 列表；时间从 App 绑定后开始算。"""
    return [
        (0.5, _battle_start()),
        (0.6, _snapshot(100.0)),
        (3.5, _status("burning", "started", 1)),
        (6.5, _damage(6000)),
        (7.0, _damage(6000)),          # 受击播放中再次受击：应 pending
        (10.5, _status("burning", "ended")),
        (12.0, _status("alive", "ended")),
        (17.0, {"type": "battle_end"}),
    ]


def describe_sequence(sequence: list[tuple[float, dict[str, Any]]]) -> str:
    lines = ["P3 测试序列（绑定后自动执行）："]
    for at, message in sequence:
        kind = message.get("type")
        detail = ""
        if kind == "status_change":
            detail = "{}/{}".format(message.get("kind"), message.get("state"))
        elif kind == "damage_taken":
            detail = "amount={}".format(message.get("amount"))
        lines.append("  +{:>4.1f}s  {:<16} {}".format(at, kind, detail))
    return "\n".join(lines)
