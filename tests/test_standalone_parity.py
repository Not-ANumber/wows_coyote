# -*- coding: utf-8 -*-
"""V3/V4 行为对照测试：同一输入序列在两种传输下应产生等价的
强度、波形、clear 和归零结果。

强度表示不同（V3 绝对指令，V4 AddIntensity 增量 + SetIntensity 0），
这里把 V4 的增量累加成绝对值后比较。
"""

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
from standalone.v4_device import V4Device  # noqa: E402

UNIT_A = "0A0A0A0A64646464"
UNIT_B = "1414141450505050"
UNIT_C = "1E1E1E1E32323232"
APP_ID = "11111111-2222-3333-4444-555555555555"


def snapshot(limit_a: int = 120, limit_b: int = 80) -> dict:
    return {
        "t": "ev",
        "ev": "devices.snapshot",
        "devices": [{
            "slotId": "slot-a",
            "name": "Coyote parity",
            "type": "COYOTE_030",
            "props": {"intensityA": 0, "intensityB": 0},
            "slotState": {
                "hasDevice": True,
                "channelA": {"intensityMax": limit_a},
                "channelB": {"intensityMax": limit_b},
            },
        }],
    }


class V3Peer:
    async def __aenter__(self) -> "V3Peer":
        self.ws = await connect(self.url)
        first = json.loads(await self.ws.recv())
        assert first["type"] == "bind"
        target = first["targetId"]
        await self.ws.send(json.dumps({
            "type": "bind", "clientId": APP_ID,
            "targetId": target, "message": "DGLAB",
        }))
        ack = json.loads(await self.ws.recv())
        assert ack["message"] == "200"
        setup = [json.loads(await self.ws.recv())["message"] for _ in range(4)]
        assert setup == [
            "clear-1", "clear-2", "strength-1+2+0", "strength-2+2+0",
        ], setup
        await self.ws.send(json.dumps({
            "type": "msg", "clientId": APP_ID, "targetId": target,
            "message": "strength-0+0+120+80",
        }))
        await asyncio.sleep(0.1)
        return self

    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None

    async def __aexit__(self, *exc_info) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def message(self, predicate, timeout: float = 5.0) -> str:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError("等待 V3 消息超时")
            raw = await asyncio.wait_for(self.ws.recv(), left)
            message = json.loads(raw)["message"]
            if predicate(message):
                return message

    async def strength(self, channel: str, value: int) -> None:
        prefix = "strength-1+2+" if channel == "a" else "strength-2+2+"
        await self.message(lambda m: m == prefix + str(value))

    async def feedback(self, limit_a: int, limit_b: int) -> None:
        await self.ws.send(json.dumps({
            "type": "msg", "clientId": APP_ID,
            "targetId": "",  # 服务端不校验
            "message": "strength-0+0+{}+{}".format(limit_a, limit_b),
        }))

    async def pulse(self, channel: str) -> list[str]:
        prefix = "pulse-A:" if channel == "a" else "pulse-B:"
        message = await self.message(lambda m: m.startswith(prefix))
        return json.loads(message[len(prefix):])

    async def clear(self, channel: str) -> None:
        await self.message(
            lambda m: m == ("clear-1" if channel == "a" else "clear-2"))


class V4Peer:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.current = {"a": 0, "b": 0}
        self.limits = {"a": 120, "b": 80}

    async def _respond_devices(self, request: dict) -> None:
        await self.ws.send(json.dumps({
            "type": "message",
            "data": {
                "t": "resp",
                "reqId": request.get("reqId"),
                "result": {"devices": [{
                    "slotId": "slot-a",
                    "name": "Coyote parity",
                    "type": "COYOTE_030",
                    "props": {
                        "intensityA": self.current["a"],
                        "intensityB": self.current["b"],
                    },
                    "slotState": {
                        "hasDevice": True,
                        "channelA": {"intensityMax": self.limits["a"]},
                        "channelB": {"intensityMax": self.limits["b"]},
                    },
                }]},
            },
        }))

    async def __aenter__(self) -> "V4Peer":
        self.ws = await connect(self.url)
        hello = json.loads(await self.ws.recv())
        assert hello["type"] == "hello"
        attached = json.loads(await self.ws.recv())
        assert attached["type"] == "controller_attached"
        await self.ws.send(json.dumps({
            "type": "message", "data": snapshot(),
        }))
        # 选中设备后的 A/B 绝对归零
        await self.zero("a")
        await self.zero("b")
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def op(self, predicate, timeout: float = 5.0) -> dict:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError("等待 V4 消息超时")
            raw = await asyncio.wait_for(self.ws.recv(), left)
            frame = json.loads(raw)
            if frame.get("type") != "message":
                continue
            data = frame.get("data") or {}
            if not isinstance(data, dict):
                continue
            if data.get("t") == "req" and data.get("m") == "devices.get":
                await self._respond_devices(data)
                continue
            if data.get("t") == "req" and data.get("m") == "device.op":
                op = data.get("data") or {}
                channel = "a" if op.get("c") == 0 else "b"
                if op.get("t") == 3:
                    self.current[channel] += int(op.get("v") or 0)
                elif op.get("t") == 7:
                    self.current[channel] = 0
                if predicate(op):
                    return op
            elif (data.get("t") == "req"
                  and data.get("m") == "device.op.clear"):
                clear = data.get("data") or {}
                if predicate({"clear": True, **clear}):
                    return clear

    async def zero(self, channel: str) -> None:
        index = 0 if channel == "a" else 1
        await self.op(lambda o: o.get("t") == 7 and o.get("c") == index)

    async def patch_limits(self, limit_a: int, limit_b: int) -> None:
        self.limits = {"a": limit_a, "b": limit_b}
        await self.ws.send(json.dumps({
            "type": "message",
            "data": {
                "t": "ev", "ev": "slots.patch",
                "slots": [{
                    "slotId": "slot-a",
                    "slotState": {
                        "channelA": {"intensityMax": limit_a},
                        "channelB": {"intensityMax": limit_b},
                    },
                }],
            },
        }))

    async def delta(self, channel: str, value: int) -> None:
        index = 0 if channel == "a" else 1
        await self.op(lambda o: o.get("t") == 3 and o.get("c") == index
                      and o.get("v") == value)

    async def pulse(self, channel: str) -> list[str]:
        index = 0 if channel == "a" else 1
        op = await self.op(lambda o: o.get("t") == 0 and o.get("c") == index)
        return list(op.get("v") or [])

    async def clear(self, channel: str) -> None:
        index = 0 if channel == "a" else 1
        await self.op(lambda o: o.get("clear") and o.get("c") == index)


class V3V4ParityTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.v3 = V3Device(
            host="127.0.0.1", port=0,
            qr_path=os.path.join(self.tmp.name, "v3.svg"))
        self.v3.start()
        self.v4 = V4Device(
            relay="local", host="127.0.0.1", port=0,
            advertise_ip="127.0.0.1",
            qr_path=os.path.join(self.tmp.name, "v4.svg"))
        await asyncio.to_thread(self.v4.start)

    async def asyncTearDown(self) -> None:
        await asyncio.to_thread(self.v3.stop)
        await asyncio.to_thread(self.v4.stop)
        self.tmp.cleanup()

    def _v4_url(self) -> str:
        state = self.v4.state()
        return "ws://127.0.0.1:{}/v4?tid={}".format(
            state["local_port"], state["target_id"])

    async def test_strength_waveform_clear_parity(self) -> None:
        async with V3Peer(self.v3.local_url) as peer3, \
                V4Peer(self._v4_url()) as peer4:
            self.assertEqual((self.v3.limits.a, self.v3.limits.b), (120, 80))
            self.assertEqual(self.v4.state()["limits"], {"a": 120, "b": 80})

            # 25%：两边都应到 A=30 / B=20。
            self.v3.set_strength(25, 25)
            self.v4.set_strength(25, 25)
            await peer3.strength("a", 30)
            await peer3.strength("b", 20)
            await peer4.delta("a", 30)
            await peer4.delta("b", 20)
            self.assertEqual(peer4.current, {"a": 30, "b": 20})

            # 50%：V3 绝对 60/40；V4 增量 +30/+20 后也是 60/40。
            await asyncio.sleep(0.15)
            self.v3.set_strength(50, 50)
            self.v4.set_strength(50, 50)
            await peer3.strength("a", 60)
            await peer3.strength("b", 40)
            await peer4.delta("a", 30)
            await peer4.delta("b", 20)
            self.assertEqual(peer4.current, {"a": 60, "b": 40})

            # App 上限降到 60/40：A=30，B=20。
            await peer3.feedback(60, 40)
            await peer4.patch_limits(60, 40)
            await peer3.strength("a", 30)
            await peer3.strength("b", 20)
            await peer4.delta("a", -30)
            await peer4.delta("b", -20)
            self.assertEqual(peer4.current, {"a": 30, "b": 20})

            # 归零：V3 绝对 0；V4 SetIntensity(0)。
            await asyncio.sleep(0.15)
            self.v3.set_strength(0, 0, force=True)
            self.v4.set_strength(0, 0, force=True)
            await peer3.strength("a", 0)
            await peer3.strength("b", 0)
            await peer4.zero("a")
            await peer4.zero("b")
            self.assertEqual(peer4.current, {"a": 0, "b": 0})

            # 同一个 loop 模板：首块帧列表应完全一致。
            template = Template(
                id="parity", name="parity",
                units=[UNIT_A, UNIT_B, UNIT_C],
                loop=True, source="test", default_ms=300)
            self.v3.play("base", "a", template, duration_s=0.3, loop=True)
            self.v4.play("base", "a", template, duration_s=0.3, loop=True)
            first3 = await peer3.pulse("a")
            first4 = await peer4.pulse("a")
            self.assertEqual(first3, first4)
            self.assertEqual(len(first3), 5)  # max(chunk=5, frames=3)

            # 补帧：同样的 period、offset 和帧列表。
            self.v3.pump(100.0)
            self.v4.pump(100.0)
            self.v3.pump(101.0)
            self.v4.pump(101.0)
            refill3 = await peer3.pulse("a")
            refill4 = await peer4.pulse("a")
            self.assertEqual(refill3, refill4)
            self.assertEqual(len(refill3), 5)

            # clear：两边都停 A 通道并移除 loop。
            self.v3.clear("a", "base")
            self.v4.clear("a", "base")
            await peer3.clear("a")
            await peer4.clear("a")
            self.assertNotIn("a", self.v3.state()["loops"])
            self.assertNotIn("a", self.v4.state()["loops"])

            # stop_all：两边都 clear A/B 并归零 A/B。
            self.v3.stop_all()
            self.v4.stop_all()
            await peer3.clear("a")
            await peer3.clear("b")
            await peer3.strength("a", 0)
            await peer3.strength("b", 0)
            await peer4.clear("a")
            await peer4.clear("b")
            await peer4.zero("a")
            await peer4.zero("b")
            self.assertEqual(peer4.current, {"a": 0, "b": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
