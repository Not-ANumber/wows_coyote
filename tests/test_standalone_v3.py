# -*- coding: utf-8 -*-
"""Phase 3：V3Device 的假 App 端到端测试（不需要手机/真实设备）。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEPS = os.path.join(ROOT, ".phase0deps")
if DEPS not in sys.path:
    sys.path.insert(0, DEPS)

try:
    from websockets.asyncio.client import connect
except ImportError:
    from websockets import connect  # type: ignore

from standalone.templates import Template  # noqa: E402
from standalone.v3_device import V3Device  # noqa: E402


UNIT = "0A0A0A0A64646464"
APP_ID = "11111111-2222-3333-4444-555555555555"


class V3DeviceTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.device = V3Device(
            host="127.0.0.1", port=0,
            qr_path=os.path.join(self.tmp.name, "qr.svg"),
        )
        self.device.start()
        self.url = self.device.local_url

    async def asyncTearDown(self) -> None:
        await asyncio.to_thread(self.device.stop)
        self.tmp.cleanup()

    async def _bind(self, ws) -> str:
        first = json.loads(await asyncio.wait_for(ws.recv(), 5))
        self.assertEqual(first["type"], "bind")
        self.assertEqual(first["message"], "targetId")
        self.assertEqual(first["clientId"], self.device.client_id)
        target = first["targetId"]
        await ws.send(json.dumps({
            "type": "bind", "clientId": APP_ID,
            "targetId": target, "message": "DGLAB",
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
        self.assertEqual(ack["type"], "bind")
        self.assertEqual(ack["message"], "200")
        setup = [json.loads(await asyncio.wait_for(ws.recv(), 5))["message"]
                 for _ in range(4)]
        self.assertEqual(setup, [
            "clear-1", "clear-2", "strength-1+2+0", "strength-2+2+0",
        ])
        await ws.send(json.dumps({
            "type": "msg", "clientId": APP_ID, "targetId": target,
            "message": "strength-0+0+120+80",
        }))
        await asyncio.sleep(0.1)
        return target

    async def _recv_until(self, ws, predicate, timeout=5.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError("等待消息超时")
            raw = await asyncio.wait_for(ws.recv(), remaining)
            message = json.loads(raw)["message"]
            if predicate(message):
                return message

    async def test_bind_limits_strength_pulse_clear(self) -> None:
        async with connect(self.url) as ws:
            await self._bind(ws)
            self.assertTrue(self.device.connected)
            self.assertEqual((self.device.limits.a, self.device.limits.b), (120, 80))

            # 25% -> A: min(120,200,30)=30；B: min(80,200,20)=20
            self.device.set_strength(25, 25)
            strength_a = await self._recv_until(
                ws, lambda m: m == "strength-1+2+30")
            strength_b = await self._recv_until(
                ws, lambda m: m == "strength-2+2+20")
            self.assertEqual((strength_a, strength_b),
                             ("strength-1+2+30", "strength-2+2+20"))
            self.assertEqual(self.device.state()["actual"],
                             {"a": 30, "b": 20})

            template = Template(
                id="loop-test", name="loop-test", units=[UNIT] * 3,
                loop=True, source="test", default_ms=300,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            pulse_a = await self._recv_until(
                ws, lambda m: m.startswith("pulse-A:"))
            units = json.loads(pulse_a[len("pulse-A:"):])
            self.assertEqual(len(units), 5)  # max(chunk_frames=5, frames_count=3)
            self.assertTrue(all(unit == UNIT for unit in units))

            # 第一次 pump 只设定补帧点；第二次 pump 才补一块。
            self.device.pump(1.0)
            self.device.pump(2.0)
            pulse_a2 = await self._recv_until(
                ws, lambda m: m.startswith("pulse-A:"))
            units2 = json.loads(pulse_a2[len("pulse-A:"):])
            self.assertEqual(len(units2), 5)

            self.device.clear("a", "base")
            await self._recv_until(ws, lambda m: m == "clear-1")
            self.assertNotIn("a", self.device.state()["loops"])

            self.device.stop_all()
            await self._recv_until(ws, lambda m: m == "clear-2")
            await self._recv_until(ws, lambda m: m == "strength-1+2+0")

    async def test_emergency_feedback_button_and_rearm(self) -> None:
        async with connect(self.url) as ws:
            target = await self._bind(ws)
            await ws.send(json.dumps({
                "type": "msg", "clientId": APP_ID, "targetId": target,
                "message": "feedback-0",
            }))
            await self._recv_until(ws, lambda m: m == "strength-1+2+0")
            self.assertTrue(self.device.muted)
            self.assertEqual(self.device.state()["loops"], {})

            # 急停期间 play 不应产生波形。
            template = Template(
                id="estop", name="estop", units=[UNIT],
                loop=True, source="test", default_ms=100,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            await asyncio.sleep(0.1)
            self.assertEqual(self.device.state()["loops"], {})

            await ws.send(json.dumps({
                "type": "msg", "clientId": APP_ID, "targetId": target,
                "message": "feedback-0",
            }))
            await asyncio.sleep(0.1)
            self.assertFalse(self.device.muted)

    async def test_bind_fires_connected_callback(self) -> None:
        calls: list[int] = []
        self.device.on_connected = lambda: calls.append(1)
        async with connect(self.url) as ws:
            await self._bind(ws)
            await asyncio.sleep(0.05)
        self.assertEqual(len(calls), 1)

    async def test_stop_is_idempotent(self) -> None:
        await asyncio.to_thread(self.device.stop)
        await asyncio.to_thread(self.device.stop)
        self.assertFalse(self.device.connected)

    async def test_stop_sends_clear_and_zero(self) -> None:
        async with connect(self.url) as ws:
            await self._bind(ws)
            template = Template(
                id="exit-test", name="exit-test", units=[UNIT],
                loop=True, source="test", default_ms=100,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            await asyncio.to_thread(self.device.stop)
            await self._recv_until(ws, lambda m: m == "clear-1")
            await self._recv_until(ws, lambda m: m == "strength-1+2+0")

    async def test_disconnect_clears_loops_and_connected(self) -> None:
        async with connect(self.url) as ws:
            await self._bind(ws)
            template = Template(
                id="disc", name="disc", units=[UNIT],
                loop=True, source="test", default_ms=100,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            self.assertIn("a", self.device.state()["loops"])
        await asyncio.sleep(0.4)
        self.assertFalse(self.device.connected)
        self.assertEqual(self.device.state()["loops"], {})

    async def test_bind_primes_loop_started_before_pairing(self) -> None:
        # 先进入战斗、配对前就有 base loop；绑定后应立即补一块，与 V4 一致。
        template = Template(
            id="prebind", name="prebind", units=[UNIT],
            loop=True, source="test", default_ms=300,
        )
        self.device.play("base", "a", template, duration_s=0.3, loop=True)
        self.assertIn("a", self.device.state()["loops"])
        async with connect(self.url) as ws:
            await self._bind(ws)
            pulse = await self._recv_until(
                ws, lambda m: m.startswith("pulse-A:"))
            units = json.loads(pulse[len("pulse-A:"):])
            self.assertEqual(len(units), 5)  # chunk_frames = 0.5s

    async def test_reconnect_resends_current_strength(self) -> None:
        async with connect(self.url) as ws:
            await self._bind(ws)
            self.device.set_strength(50, 50)
            await self._recv_until(ws, lambda m: m == "strength-1+2+60")
            await self._recv_until(ws, lambda m: m == "strength-2+2+40")
        await asyncio.sleep(0.4)
        self.assertFalse(self.device.connected)

        # 重连后绑定会先归零；相同的百分比仍应重新下发当前强度，
        # 否则引擎的相同值优化会让设备一直停在 0。
        async with connect(self.url) as ws:
            await self._bind(ws)
            self.device.set_strength(50, 50)
            await self._recv_until(ws, lambda m: m == "strength-1+2+60")
            await self._recv_until(ws, lambda m: m == "strength-2+2+40")

    async def test_repeated_strength_is_absolute_not_accumulated(self) -> None:
        # V3 强度是绝对指令：重复设置/归零后再设置不会叠加。
        async with connect(self.url) as ws:
            await self._bind(ws)
            self.device.set_strength(10, 10)
            await self._recv_until(ws, lambda m: m == "strength-1+2+12")
            await self._recv_until(ws, lambda m: m == "strength-2+2+8")

            # 相同百分比：去重，不应再发；本地记录仍应是 12/8。
            self.device.set_strength(10, 10)
            await asyncio.sleep(0.2)
            self.assertEqual(self.device.state()["actual"],
                             {"a": 12, "b": 8})

            # 归零后再设置同一百分比：下发绝对值，而不是在上一次上相加。
            self.device.set_strength(0, 0, force=True)
            await self._recv_until(ws, lambda m: m == "strength-1+2+0")
            await self._recv_until(ws, lambda m: m == "strength-2+2+0")
            self.device.set_strength(10, 10)
            await self._recv_until(ws, lambda m: m == "strength-1+2+12")
            await self._recv_until(ws, lambda m: m == "strength-2+2+8")
            self.assertEqual(self.device.state()["actual"],
                             {"a": 12, "b": 8})


if __name__ == "__main__":
    unittest.main(verbosity=2)
