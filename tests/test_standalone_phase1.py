# -*- coding: utf-8 -*-
"""Phase 1：standalone 适配层 + 调度器测试（不连接设备/游戏）。"""

import json
import os
import sys
import tempfile
import unittest
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone.config import load_config  # noqa: E402
from standalone.device import FakeDevice  # noqa: E402
from standalone.host import StandaloneHost  # noqa: E402
from standalone.plugin_bridge import plugin  # noqa: E402
from standalone.telemetry import TelemetryRunner  # noqa: E402
from standalone.templates import placeholder_library  # noqa: E402


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


class Collector:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_nowait(self, item: dict[str, Any]) -> None:
        self.items.append(item)


def battle_start() -> dict[str, Any]:
    return {"type": "battle_start", "ship_name": "TestShip", "ship_type": "BB"}


def snapshot(hp_pct: float = 100.0, max_hp: int = 60000) -> dict[str, Any]:
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


def damage(amount: float) -> dict[str, Any]:
    return {"type": "damage_taken", "source": "damages", "amount": amount}


def status(kind: str, state: str, count: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"type": "status_change", "kind": kind, "state": state}
    if count is not None:
        message["count"] = count
    return message


class StandalonePhase1Test(unittest.TestCase):

    def setUp(self) -> None:
        self.cfg = load_config(user_path="")
        self.cfg.update({
            "base_enable": True,
            "base_min": 10,
            "base_max": 40,
            "base_channel": "both",
            "base_preset": "base-w",
            "base_curve_enable": False,
            "hit_enable": True,
            "hit_strength": 40,
            "hit_channel": "both",
            "hit_preset": "hit-w",
            "hit_threshold_pct": 2,
            "hit_full_pct": 8,
            "hit_duration_s": 2.0,
            "hit_curve_enable": False,
            "fire_enable": True,
            "fire_strength": 12,
            "fire_stack_add": 6,
            "fire_channel": "both",
            "fire_preset": "fire-w",
            "flood_enable": True,
            "flood_strength": 12,
            "flood_stack_add": 6,
            "flood_channel": "both",
            "flood_preset": "flood-w",
            "death_enable": True,
            "death_strength": 60,
            "death_channel": "both",
            "death_preset": "death-w",
            "death_duration_s": 4.0,
            "telemetry_timeout_s": 60,
        })
        self.templates = placeholder_library(self.cfg)
        self.sink = FakeDevice()
        self.clock = Clock()
        self.host = StandaloneHost(self.cfg, self.sink, self.templates,
                                   now_fn=self.clock)
        self.collector = Collector()
        self.old_queue = plugin.send_queue
        plugin.send_queue = self.collector

    def tearDown(self) -> None:
        plugin.send_queue = self.old_queue

    def _tick(self, advance: float = 0.0) -> None:
        self.clock.advance(advance)
        self.host.tick(self.clock.t)

    def _active(self, channel: str = "a"):
        return self.host.scheduler.active[channel]

    def test_phase1_base_fire_hit_death_sequence(self) -> None:
        self.host.on_message(battle_start())
        self.host.on_message(snapshot(100.0))
        self._tick()
        self.assertEqual(self.sink.strength, (10, 10))
        self.assertEqual(self._active("a").owner, "base")
        self.assertEqual(self._active("a").template.id, "base-w")
        self.assertEqual(self._active("a").template.source, "placeholder")
        self.assertTrue(any(p["owner"] == "base" and p["template"] == "base-w"
                            for p in self.sink.plays))

        # 起火：基础被 clear，火成为赢家；强度 10 + 12。
        self.host.on_message(status("burning", "started", 1))
        self._tick(0.1)
        self.assertEqual(self.sink.strength, (22, 22))
        self.assertEqual(self._active("a").owner, "fire")
        self.assertTrue(any(c["owner"] == "base" and c["channel"] == "a"
                            for c in self.sink.clears))
        self.assertTrue(any(p["owner"] == "fire" and p["template"] == "fire-w"
                            for p in self.sink.plays))

        # 受击：火被 clear，受击成为赢家；强度 10 + 12 + 40。
        self.host.on_message(damage(6000))
        self._tick(0.31)
        self.assertEqual(self.sink.strength, (62, 62))
        self.assertEqual(self._active("a").owner, "hit")
        self.assertTrue(any(c["owner"] == "fire" and c["channel"] == "a"
                            for c in self.sink.clears))
        hit_plays_a = [p for p in self.sink.plays
                       if p["owner"] == "hit" and p["channel"] == "a"]
        self.assertEqual(len(hit_plays_a), 1)

        # 受击波形仍在播时再次受击：只 pending，不立即追加。
        self.host.on_message(damage(6000))
        self._tick(0.31)
        self.assertTrue(self._active("a").pending)
        self.assertEqual(len([p for p in self.sink.plays
                              if p["owner"] == "hit" and p["channel"] == "a"]), 1)

        # 第一个受击窗口结束后，pending 补播一次。
        self._tick(1.77)
        self.assertFalse(self._active("a").pending)
        self.assertEqual(len([p for p in self.sink.plays
                              if p["owner"] == "hit" and p["channel"] == "a"]), 2)

        # 受击强度结束，回落到火。
        self._tick(0.30)
        self.assertEqual(self._active("a").owner, "fire")
        self.assertEqual(self.sink.strength, (22, 22))
        self.assertTrue(any(c["owner"] == "hit" and c["channel"] == "a"
                            for c in self.sink.clears))

        # 灭火，回落到基础。
        self.host.on_message(status("burning", "ended"))
        self._tick(0.1)
        self.assertEqual(self._active("a").owner, "base")
        self.assertEqual(self.sink.strength, (10, 10))

        # 阵亡：清空基础/火/受击，死亡成为赢家。
        self.host.on_message(status("alive", "ended"))
        self._tick(0.1)
        self.assertEqual(self._active("a").owner, "death")
        self.assertEqual(self.sink.strength, (60, 60))
        self.assertTrue(any(p["owner"] == "death" and p["template"] == "death-w"
                            for p in self.sink.plays))

        # 死亡持续结束：无赢家，强度归零，调度器 clear。
        self._tick(4.1)
        self.assertIsNone(self._active("a"))
        self.assertIsNone(self._active("b"))
        self.assertEqual(self.sink.strength, (0, 0))
        self.assertTrue(any(c["owner"] == "death" and c["channel"] == "a"
                            for c in self.sink.clears))

        # 战局结束仍然归零。
        self.host.on_message({"type": "battle_end"})
        self._tick(0.1)
        self.assertEqual(self.sink.strength, (0, 0))

        # 不能把 DGHub 的 set_strength/trigger 又发出去。
        bad = [item for item in self.collector.items
               if item.get("op") in ("set_strength", "trigger")]
        self.assertEqual(bad, [])

    def test_loop_template_keeps_refilling_until_clear(self) -> None:
        self.host.on_message(battle_start())
        self.host.on_message(snapshot(100.0))
        self._tick()
        self.host.on_message(status("burning", "started", 1))
        self._tick(0.1)
        self.assertIn("a", self.sink.loops)
        self.assertEqual(self.sink.loops["a"]["owner"], "fire")

        fire_before = len([r for r in self.sink.loop_refills
                           if r["owner"] == "fire"])
        self._tick(1.1)
        fire_after_first = len([r for r in self.sink.loop_refills
                                if r["owner"] == "fire"])
        self.assertGreater(fire_after_first, fire_before)

        # 继续循环：不是只补一次就停。
        self._tick(1.1)
        fire_after_second = len([r for r in self.sink.loop_refills
                                 if r["owner"] == "fire"])
        self.assertGreater(fire_after_second, fire_after_first)
        self.assertEqual(self.sink.loops["a"]["owner"], "fire")

        # clear 后停止：灭火回落到基础，火 loop 被移除，不再有火补帧。
        self.host.on_message(status("burning", "ended"))
        self._tick(0.1)
        self.assertEqual(self.sink.loops["a"]["owner"], "base")
        fire_at_clear = len([r for r in self.sink.loop_refills
                             if r["owner"] == "fire"])
        self._tick(2.0)
        self.assertEqual(len([r for r in self.sink.loop_refills
                              if r["owner"] == "fire"]), fire_at_clear)
        self.assertGreaterEqual(
            len([r for r in self.sink.loop_refills if r["owner"] == "base"]), 1)

    def test_zero_strength_base_does_not_start_waveform(self) -> None:
        def run_case(base_min: float, base_max: float,
                     curve_path: str | None = None) -> None:
            self.sink.plays.clear()
            self.sink.clears.clear()
            self.sink.loops.clear()
            plugin.plugin_cfg["base_min"] = base_min
            plugin.plugin_cfg["base_max"] = base_max
            plugin.plugin_cfg["base_curve_enable"] = bool(curve_path)
            if curve_path:
                self.host.engine.base_curve_path = curve_path
                self.host.engine._base_curve.set_path(curve_path)
            self.host.on_message(battle_start())
            self.host.on_message(snapshot(100.0))
            self._tick(3.0)  # 越过函数表 2s 刷新窗口
            self.assertEqual(self.host.engine._base_strength(), 0.0)
            self.assertIsNone(self._active("a"))
            self.assertIsNone(self._active("b"))
            self.assertFalse(any(p["owner"] == "base"
                                 for p in self.sink.plays))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zero-base.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1,
                           "points": [[100, 0], [0, 0]]}, handle)
            run_case(0, 0)
            self.host = StandaloneHost(
                self.cfg, self.sink, self.templates, now_fn=self.clock)
            run_case(10, 40, path)

    def test_zero_strength_hit_does_not_start_waveform(self) -> None:
        def run_case(curve_path: str | None = None) -> None:
            self.sink.plays.clear()
            self.sink.clears.clear()
            self.sink.loops.clear()
            plugin.plugin_cfg["base_enable"] = False
            plugin.plugin_cfg["hit_strength"] = 0
            plugin.plugin_cfg["hit_curve_enable"] = bool(curve_path)
            if curve_path:
                self.host.engine.hit_curve_path = curve_path
                self.host.engine._hit_curve.set_path(curve_path)
            self.host.on_message(battle_start())
            self.host.on_message(snapshot(100.0))
            self._tick(3.0)  # 越过函数表 2s 刷新窗口
            self.host.on_message(damage(6000))
            self._tick(0.31)
            self.assertEqual(self.host.engine._hit_until, 0.0)
            self.assertEqual(self.host.engine._hit_value, 0.0)
            self.assertIsNone(self._active("a"))
            self.assertIsNone(self._active("b"))
            self.assertFalse(any(p["owner"] == "hit"
                                 for p in self.sink.plays))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zero-hit.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1,
                           "points": [[100, 0], [0, 0]]}, handle)
            run_case()
            self.host = StandaloneHost(
                self.cfg, self.sink, self.templates, now_fn=self.clock)
            run_case(path)

    def test_base_curve_falling_to_zero_clears_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "falling-base.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1,
                           "points": [[100, 10], [50, 0], [0, 0]]},
                          handle)
            plugin.plugin_cfg["base_curve_enable"] = True
            self.host.engine.base_curve_path = path
            self.host.engine._base_curve.set_path(path)
            self.host.on_message(battle_start())
            self.host.on_message(snapshot(100.0))
            self._tick(3.0)
            self.assertEqual(self.host.engine._base_strength(), 10.0)
            self.assertEqual(self._active("a").owner, "base")
            self.assertTrue(any(p["owner"] == "base"
                                for p in self.sink.plays))

            self.host.on_message(snapshot(40.0))
            self._tick(0.1)
            self.assertEqual(self.host.engine._base_strength(), 0.0)
            self.assertIsNone(self._active("a"))
            self.assertIsNone(self._active("b"))
            self.assertTrue(any(c["owner"] == "base"
                                for c in self.sink.clears))

    def test_zero_strength_fire_flood_death_do_not_start_waveform(self) -> None:
        plugin.plugin_cfg["base_enable"] = False
        plugin.plugin_cfg["hit_enable"] = False
        plugin.plugin_cfg["fire_strength"] = 0
        plugin.plugin_cfg["fire_stack_add"] = 0
        plugin.plugin_cfg["flood_strength"] = 0
        plugin.plugin_cfg["flood_stack_add"] = 0
        plugin.plugin_cfg["death_strength"] = 0
        self.host.on_message(battle_start())
        self.host.on_message(snapshot(100.0))
        self._tick()
        self.host.on_message(status("burning", "started", 1))
        self.host.on_message(status("flooding", "started", 1))
        self._tick(0.1)
        self.assertIsNone(self._active("a"))
        self.assertIsNone(self._active("b"))
        self.assertFalse(any(p["owner"] in ("fire", "flood", "death")
                             for p in self.sink.plays))

        self.host.on_message({
            "type": "snapshot", "health": 0, "max_health": 60000,
            "hp_pct": 0.0, "alive": False, "burning": False,
            "flooding": False, "fires": 0, "floods": 0,
        })
        self._tick(0.1)
        self.assertTrue(self.host.engine.dead)
        self.assertIsNone(self._active("a"))
        self.assertIsNone(self._active("b"))
        self.assertFalse(any(p["owner"] == "death" for p in self.sink.plays))

    def test_stop_clears_and_zeroes(self) -> None:
        self.host.on_message(battle_start())
        self.host.on_message(snapshot(100.0))
        self._tick()
        self.host.stop()
        self.assertEqual(self.sink.strength, (0, 0))
        self.assertIsNone(self._active("a"))
        self.assertIsNone(self._active("b"))
        self.assertEqual(self.sink.loops, {})

    def test_resend_strength_after_device_ready(self) -> None:
        self.host.on_message(battle_start())
        self.host.on_message(snapshot(100.0))
        self._tick()
        self.assertEqual(self.sink.strength, (10, 10))
        sent = len(self.sink.strengths)

        # 相同百分比：引擎去重，不再调用 sink。
        self._tick()
        self.assertEqual(len(self.sink.strengths), sent)

        # 设备重新就绪（V3 bind / V4 选中设备）后应重新下发当前强度。
        self.host.engine.resend_strength()
        self._tick()
        self.assertGreater(len(self.sink.strengths), sent)
        self.assertEqual(self.sink.strength, (10, 10))

    def test_battle_start_and_end_force_absolute_zero(self) -> None:
        self.sink.events.clear()
        self.host.on_message(battle_start())
        force_zeros = [
            event for event in self.sink.events
            if event["op"] == "strength" and event["force"]
            and event["a"] == 0 and event["b"] == 0
        ]
        self.assertTrue(force_zeros)

        self.host.on_message(snapshot(100.0))
        self._tick()
        self.assertEqual(self.sink.strength, (10, 10))
        self.sink.events.clear()
        self.host.on_message({"type": "battle_end"})
        force_zeros = [
            event for event in self.sink.events
            if event["op"] == "strength" and event["force"]
            and event["a"] == 0 and event["b"] == 0
        ]
        self.assertTrue(force_zeros)

    def test_telemetry_reader_feeds_standalone_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wows_telemetry.jsonl")
            open(path, "wb").close()
            runner = TelemetryRunner(self.host, path)
            runner.poll()  # 打开空文件
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                for message in (battle_start(), snapshot(100.0)):
                    handle.write(json.dumps(message, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            runner.poll()
            self._tick()
            self.assertEqual(self.sink.strength, (10, 10))
            self.assertEqual(self._active("a").owner, "base")
            self.assertEqual(runner.state()["malformed"], 0)
            runner.reader._close_handle()  # Windows 下先释放文件再删临时目录


if __name__ == "__main__":
    unittest.main(verbosity=2)
