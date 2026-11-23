# -*- coding: utf-8 -*-
"""V4：本地中继协议 + V4Device 假 App 端到端测试（不需要手机/真实设备）。"""

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
    from websockets.exceptions import ConnectionClosed
except ImportError:
    from websockets import connect  # type: ignore
    ConnectionClosed = Exception  # type: ignore

from standalone.config import load_config  # noqa: E402
from standalone.templates import Template  # noqa: E402
from standalone.v4_device import (  # noqa: E402
    PAIRING_PREFIX, V4Device, app_socket_url, normalize_relay,
)
from standalone.v4_relay import LocalV4Relay  # noqa: E402

UNIT = "0A0A0A0A64646464"


def slot_snapshot(slot_id: str = "slot-a",
                  limit_a: int = 120, limit_b: int = 80,
                  intensity_a: int = 0, intensity_b: int = 0,
                  warm_up_a: float = 1.0, warm_up_b: float = 1.0) -> dict:
    return {
        "t": "ev",
        "ev": "devices.snapshot",
        "devices": [{
            "id": 0,
            "slotId": slot_id,
            "name": "Coyote 测试",
            "type": "COYOTE_030",
            "props": {"intensityA": intensity_a, "intensityB": intensity_b},
            "slotState": {
                "hasDevice": True,
                "channelA": {"intensityMax": limit_a,
                             "warmUpScale": warm_up_a},
                "channelB": {"intensityMax": limit_b,
                             "warmUpScale": warm_up_b},
            },
        }],
    }


class FakeV4App:
    """只实现测试需要的 App 侧：hello、设备上报、收下发。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.client_id = ""
        self.controller_id = ""
        self.seen: list[dict] = []
        self.current = {"a": 0, "b": 0}

    async def __aenter__(self) -> "FakeV4App":
        self.ws = await connect(self.url)
        hello = await self.recv_frame()
        assert hello["type"] == "hello", hello
        self.client_id = hello["clientId"]
        attached = await self.recv_frame()
        assert attached["type"] == "controller_attached", attached
        self.controller_id = attached["clientId"]
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def recv_frame(self, timeout: float = 5.0) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        return json.loads(raw)

    async def send(self, data: dict) -> None:
        await self.ws.send(json.dumps({
            "type": "message", "data": data,
        }))

    async def wait_for(self, predicate, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError("等待 App 下行消息超时")
            frame = await self.recv_frame(remaining)
            if frame.get("type") != "message":
                continue
            data = frame.get("data") or {}
            self.seen.append(data)
            if data.get("t") == "req" and data.get("m") == "device.op":
                op = data.get("data") or {}
                channel = "a" if op.get("c") == 0 else "b"
                if op.get("t") == 3:
                    self.current[channel] += int(op.get("v") or 0)
                elif op.get("t") == 7:
                    self.current[channel] = 0
            if predicate(data):
                return data

    async def answer_probe(self, intensity_a: int | None = None,
                           intensity_b: int | None = None,
                           limit_a: int = 120, limit_b: int = 80,
                           warm_up_a: float = 1.0,
                           warm_up_b: float = 1.0,
                           omit_a: bool = False,
                           update_actual: bool = True,
                           timeout: float = 5.0) -> dict:
        """响应控制器主动发起的 devices.get，模拟 App 当前真实强度。"""
        request = await self.wait_for(
            lambda d: d.get("m") == "devices.get", timeout)
        if intensity_a is None:
            intensity_a = self.current["a"]
        if intensity_b is None:
            intensity_b = self.current["b"]
        devices = slot_snapshot(
            limit_a=limit_a, limit_b=limit_b,
            intensity_a=intensity_a, intensity_b=intensity_b,
            warm_up_a=warm_up_a, warm_up_b=warm_up_b)["devices"]
        if omit_a:
            devices[0]["props"].pop("intensityA", None)
        await self.send({
            "t": "resp",
            "reqId": request.get("reqId"),
            "result": {"devices": devices},
        })
        if update_actual:
            self.current = {"a": int(intensity_a), "b": int(intensity_b)}
        return request


class V4RelayTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.relay = LocalV4Relay(host="127.0.0.1", port=0)
        await self.relay.start()

    async def asyncTearDown(self) -> None:
        await self.relay.stop()

    async def test_handshake_attach_forward_and_ping(self) -> None:
        async with connect(
                "ws://127.0.0.1:{}/v4".format(self.relay.port)) as controller:
            hello = json.loads(await asyncio.wait_for(controller.recv(), 5))
            self.assertEqual(hello["type"], "hello")
            controller_id = hello["clientId"]

            # 服务器级 ping 不转发给对端。
            await controller.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(controller.recv(), 5))
            self.assertEqual(pong["type"], "pong")
            self.assertIsInstance(pong.get("ts"), int)

            async with connect(
                    "ws://127.0.0.1:{}/v4?tid={}".format(
                        self.relay.port, controller_id)) as app:
                app_hello = json.loads(await asyncio.wait_for(app.recv(), 5))
                self.assertEqual(app_hello["type"], "hello")
                app_id = app_hello["clientId"]
                attached = json.loads(await asyncio.wait_for(app.recv(), 5))
                self.assertEqual(attached["type"], "controller_attached")
                self.assertEqual(attached["clientId"], controller_id)

                notification = json.loads(
                    await asyncio.wait_for(controller.recv(), 5))
                self.assertEqual(notification["type"], "client_attached")
                self.assertEqual(notification["clientId"], app_id)

                await controller.send(json.dumps({
                    "type": "message", "clientId": app_id,
                    "data": {"t": "req", "reqId": "r1", "m": "devices.get"},
                }))
                down = json.loads(await asyncio.wait_for(app.recv(), 5))
                self.assertEqual(down["type"], "message")
                self.assertEqual(down["clientId"], app_id)
                self.assertEqual(down["data"]["m"], "devices.get")

                await app.send(json.dumps({
                    "type": "message",
                    "data": {"t": "resp", "reqId": "r1",
                             "result": {"devices": []}},
                }))
                up = json.loads(await asyncio.wait_for(controller.recv(), 5))
                self.assertEqual(up["type"], "message")
                self.assertEqual(up["clientId"], app_id)
                self.assertEqual(up["data"]["result"], {"devices": []})

            detached = json.loads(await asyncio.wait_for(controller.recv(), 5))
            self.assertEqual(detached["type"], "client_disconnected")
            self.assertEqual(detached["clientId"], app_id)

    async def test_unknown_target_is_rejected(self) -> None:
        async with connect(
                "ws://127.0.0.1:{}/v4?tid=missing".format(
                    self.relay.port)) as app:
            frame = json.loads(await asyncio.wait_for(app.recv(), 5))
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["code"], "controller_not_found")
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(app.recv(), 5)

    async def test_idle_timeout_closes_controller(self) -> None:
        relay = LocalV4Relay(host="127.0.0.1", port=0, idle_timeout_s=0.2)
        await relay.start()
        try:
            async with connect(
                    "ws://127.0.0.1:{}/v4".format(relay.port)) as controller:
                await asyncio.wait_for(controller.recv(), 5)  # hello
                frame = json.loads(await asyncio.wait_for(controller.recv(), 5))
                self.assertEqual(frame["type"], "idle_timeout")
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(controller.recv(), 5)
        finally:
            await relay.stop()


class V4DeviceTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.device = V4Device(
            relay="local",
            host="127.0.0.1",
            port=0,
            advertise_ip="127.0.0.1",
            qr_path=os.path.join(self.tmp.name, "qr.svg"),
        )
        self.device.feedback_settle_s = 0.1
        self.device.reconcile_poll_s = 0.1
        self.device.probe_retry_s = 5.0
        self.device.anchor_grace_s = 0.1
        self.device.start()
        self.state = self.device.state()
        self.target = self.state["target_id"]
        self.assertIsInstance(self.state["local_port"], int)
        self.assertTrue(self.state["qr_payload"].startswith(PAIRING_PREFIX))
        self.assertIn("tid%3D", self.state["qr_payload"])

    async def asyncTearDown(self) -> None:
        await asyncio.to_thread(self.device.stop)
        self.tmp.cleanup()

    def _app_url(self) -> str:
        return "ws://127.0.0.1:{}/v4?tid={}".format(
            self.device.state()["local_port"], self.target)

    async def test_strength_waveform_clear_and_limits(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            zero_a = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            self.assertEqual(zero_a["data"]["v"], 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            await asyncio.sleep(0.1)
            state = self.device.state()
            self.assertTrue(state["connected"])
            self.assertEqual(state["limits"], {"a": 120, "b": 80})
            self.assertEqual(state["slot"]["slot_id"], "slot-a")
            self.assertEqual(state["app_count"], 1)

            # 25%：A=min(120,200,30)=30，B=min(80,200,20)=20。
            # 当前本地估算为 0（刚 t=7 归零），所以下发增量 30/20。
            self.device.set_strength(25, 25)
            op_a = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0)
            op_b = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1)
            self.assertEqual(op_a["data"]["v"], 30)
            self.assertEqual(op_b["data"]["v"], 20)
            self.assertEqual(self.device.state()["actual"],
                             {"a": 30, "b": 20})
            await asyncio.sleep(0.1)
            await app.answer_probe(30, 20)
            await asyncio.sleep(0.1)

            # 相同百分比不重复下发。
            self.device.set_strength(25, 25)
            await asyncio.sleep(0.2)

            # 50%：A=60（+30），B=40（+20）。
            self.device.set_strength(50, 50)
            next_a = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0 and d["data"].get("v") == 30)
            next_b = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1 and d["data"].get("v") == 20)
            self.assertEqual(next_a["data"]["v"], 30)
            self.assertEqual(next_b["data"]["v"], 20)

            template = Template(
                id="loop-test", name="loop-test", units=[UNIT] * 3,
                loop=True, source="test", default_ms=300,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            pulse = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 0)
            self.assertEqual(pulse["data"]["c"], 0)
            # 与 V3 一致：initial = max(chunk_frames=5, frames_count=3) = 5 帧
            self.assertEqual(pulse["data"]["d"], 500)
            self.assertEqual(len(pulse["data"]["v"]), 5)
            self.assertIs(pulse["data"]["im"], True)

            # 第一次 pump 只设定补帧点；第二次 pump 追加一块 im=False。
            self.device.pump(100.0)
            self.device.pump(101.0)
            refill = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 0
                and not d["data"].get("im"))
            self.assertEqual(len(refill["data"]["v"]), 5)  # period 0.5s = 5 帧

            self.device.clear("a", "base")
            clear = await app.wait_for(
                lambda d: d.get("m") == "device.op.clear")
            self.assertEqual(clear["data"], {"s": "slot-a", "c": 0})
            self.assertNotIn("a", self.device.state()["loops"])

            self.device.stop_all()
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

    async def test_slot_selection_zero_precedes_resend_strength(self) -> None:
        """选中设备后的归零必须排在引擎重发的强度之前。"""
        self.device.on_connected = lambda: self.device.set_strength(50, 50)
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            # 如果强度先于归零发出，最后会被 0 覆盖。
            self.assertEqual(app.current, {"a": 60, "b": 40})

    async def test_zero_warmup_does_not_suppress_initial_output(self) -> None:
        """App 初始 warmUpScale=0 时，首条目标仍必须下发，避免一直静默。"""
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot(warm_up_a=0.0, warm_up_b=0.0))
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.set_strength(50, 50)
            op_a = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0)
            op_b = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1)
            self.assertEqual(op_a["data"]["v"], 60)
            self.assertEqual(op_b["data"]["v"], 40)
            self.assertEqual(app.current, {"a": 60, "b": 40})
            await asyncio.sleep(0.15)
            await app.answer_probe(60, 40, warm_up_a=0.0, warm_up_b=0.0)
            await asyncio.sleep(0.15)
            state = self.device.state()["apps"][0]
            self.assertTrue(state["verified"])
            self.assertEqual(state["current"], {"a": 60, "b": 40})

    async def test_feedback_accepts_scaled_warmup_value(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)

            # 冷启动 0.5：A 上报的是缩放后的 30（=60×0.5），B 已到位 40。
            # 说明设置值已经到位，不能再补 30，否则会抵消安全爬坡。
            await app.answer_probe(30, 40, warm_up_a=0.5, warm_up_b=0.5)
            await asyncio.sleep(0.2)
            self.assertFalse(any(
                d.get("m") == "device.op"
                and (d.get("data") or {}).get("t") == 3
                and (d.get("data") or {}).get("c") == 0
                and (d.get("data") or {}).get("v") == 30
                for d in app.seen))
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_feedback_does_not_adopt_scaled_value(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.verify_window_s = 0.25

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)

            # 冷启动期间上报值既不是目标也不是缩放值，无法判断该补多少；
            # 不回正，也不把缩放值当成新基线。
            await app.answer_probe(10, 40, warm_up_a=0.5, warm_up_b=0.5)
            await asyncio.sleep(0.6)
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)
            self.assertEqual(app.current["a"], 10)

    async def test_feedback_button_estop_and_rearm(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            await app.send({"t": "ev", "ev": "custom.action", "action": 0})
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await asyncio.sleep(0.1)
            self.assertTrue(self.device.muted)
            self.assertEqual(self.device.state()["loops"], {})

            await app.send({"t": "ev", "ev": "custom.action", "action": 0})
            await asyncio.sleep(0.1)
            self.assertFalse(self.device.muted)

    async def test_slots_patch_updates_limits(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            await app.send({
                "t": "ev", "ev": "slots.patch",
                "slots": [{
                    "slotId": "slot-a",
                    "props": {"intensityA": 0, "intensityB": 0},
                    "slotState": {"channelA": {"intensityMax": 100},
                                  "channelB": {"intensityMax": 60}},
                }],
            })
            await asyncio.sleep(0.2)
            state = self.device.state()
            self.assertEqual(state["limits"], {"a": 100, "b": 60})
            self.assertEqual(state["apps"][0]["current"]["a"], 0)

    async def test_loop_started_before_app_is_primed_on_attach(self) -> None:
        template = Template(
            id="startup", name="startup", units=[UNIT],
            loop=True, source="test", default_ms=300,
        )
        self.device.play("base", "a", template, duration_s=0.3, loop=True)
        self.assertIn("a", self.device.state()["loops"])
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            pulse = await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 0)
            self.assertIs(pulse["data"].get("im"), True)
            self.assertEqual(pulse["data"]["c"], 0)

    async def test_rapid_zero_and_strength_are_not_dropped(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            # 不等待节流窗口，连续 强度 → 归零 → 强度；
            # 最后一次必须能覆盖到 App，不能因为节流/去重被丢掉。
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0 and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1 and d["data"].get("v") == 40)
            self.assertEqual(app.current, {"a": 60, "b": 40})

            self.device.set_strength(0, 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.assertEqual(app.current, {"a": 0, "b": 0})
            # 新的安全策略：非零恢复前先确认 0，避免在未知状态上叠加。
            await asyncio.sleep(0.1)
            await app.answer_probe(0, 0)
            await asyncio.sleep(0.1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0 and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1 and d["data"].get("v") == 40)
            self.assertEqual(app.current, {"a": 60, "b": 40})

    async def test_force_zero_sends_absolute_zero_like_v3(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            # 已归零后 force=True 仍应发一次绝对 0，对齐 V3 的 strength 指令。
            self.device.set_strength(0, 0, force=True)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

    async def test_reconnect_resends_current_strength(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0 and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1 and d["data"].get("v") == 40)
        await asyncio.sleep(0.4)
        self.assertFalse(self.device.connected)

        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            # 新 App 的 current 从 0 开始；相同的百分比仍应重新下发。
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0 and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1 and d["data"].get("v") == 40)

    async def test_attach_fires_connected_callback_after_slot(self) -> None:
        calls: list[int] = []
        self.device.on_connected = lambda: calls.append(1)
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            await asyncio.sleep(0.1)
            self.assertEqual(len(calls), 1)

    async def test_app_pushed_patch_confirms_inflight(self) -> None:
        """App 只推 slots.patch、不响应探测时，也不能在 4 秒后误归零。"""
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.verify_window_s = 0.5
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await app.send({
                "t": "ev", "ev": "slots.patch",
                "slots": [{
                    "slotId": "slot-a",
                    "props": {"intensityA": 60, "intensityB": 40},
                }],
            })
            # 超过校验窗口；没有探测响应，但推送已经确认。
            await asyncio.sleep(0.8)
            state = self.device.state()["apps"][0]
            self.assertEqual(state["current"], {"a": 60, "b": 40})
            self.assertEqual(state["confirmed"], {"a": 60, "b": 40})
            self.assertTrue(state["verified"])
            self.assertFalse(state["awaiting_zero"])
            self.assertEqual(app.current, {"a": 60, "b": 40})

    async def test_feedback_reconciles_lost_increment(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            self.assertEqual(app.current, {"a": 60, "b": 40})

            # 模拟 A 的 AddIntensity 丢失：App 实际仍是 0。
            app.current["a"] = 0
            await asyncio.sleep(0.15)
            await app.answer_probe(0, 40)
            # 不能再用同一个上报值补 +60；等待归零锚定。
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await asyncio.sleep(0.15)
            await app.answer_probe(0, 40)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            self.assertEqual(app.current, {"a": 60, "b": 40})
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_feedback_reconciles_lost_zero(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1)
            await asyncio.sleep(0.15)

            self.device.set_strength(0, 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            # 模拟归零命令丢失：App 实际仍上报 A=10。
            app.current["a"] = 10
            await asyncio.sleep(0.15)
            await app.answer_probe(10, 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            self.assertEqual(app.current["a"], 0)

    async def test_feedback_repeated_stale_report_does_not_over_add(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)

            # 第一次上报旧值：允许一次修正。
            await app.answer_probe(30, 40, update_actual=False)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 30)
            self.assertEqual(app.current["a"], 90)

            # 第二次仍是同一个旧值：不能继续补正向增量，转为归零锚定。
            await asyncio.sleep(0.15)
            await app.answer_probe(30, 40, update_actual=False)
            await asyncio.sleep(0.05)
            self.assertEqual(app.current["a"], 90)  # 没有被补到 120
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await asyncio.sleep(0.15)
            await app.answer_probe(0, 40)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            self.assertEqual(app.current, {"a": 60, "b": 40})
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_feedback_missing_intensity_stops_positive(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)

            # 第一次旧值触发一次修正，实际到 90。
            await app.answer_probe(30, 40, update_actual=False)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 30)
            self.assertEqual(app.current["a"], 90)

            # 完整快照缺少 intensityA：不能再沿用 30 继续加。
            await asyncio.sleep(0.15)
            await app.answer_probe(0, 40, omit_a=True, update_actual=False)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            self.assertEqual(app.current["a"], 0)
            self.assertTrue(
                self.device.state()["apps"][0]["uncertain"])

            await asyncio.sleep(0.15)
            await app.answer_probe(0, 40)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            self.assertEqual(app.current, {"a": 60, "b": 40})
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_feedback_above_target_only_corrects_down(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)

            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)
            app.current["a"] = 90
            await app.answer_probe(90, 40, update_actual=False)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == -30)
            self.assertEqual(app.current["a"], 60)
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_live_above_target_is_corrected_down(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)
            await app.answer_probe(60, 40)
            await asyncio.sleep(0.15)

            app.current["a"] = 90
            await app.send({
                "t": "ev", "ev": "slots.patch",
                "slots": [{
                    "slotId": "slot-a",
                    "props": {"intensityA": 90, "intensityB": 40},
                }],
            })
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == -30)
            self.assertEqual(app.current["a"], 60)
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)

    async def test_manual_adjustment_after_verified_is_accepted(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)
            await app.answer_probe(60, 40)
            await asyncio.sleep(0.15)

            app.current["a"] = 30
            await app.send({
                "t": "ev", "ev": "slots.patch",
                "slots": [{
                    "slotId": "slot-a",
                    "props": {"intensityA": 30, "intensityB": 40},
                }],
            })
            await asyncio.sleep(0.2)
            await asyncio.sleep(0.2)
            state = self.device.state()["apps"][0]
            self.assertEqual(state["current"]["a"], 30)
            self.assertTrue(state["verified"])
            self.assertEqual(app.current["a"], 30)  # 未被自动拉回

            self.device.set_strength(60, 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 42)  # 72 - 30
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 8)   # 48 - 40

    async def test_full_snapshot_missing_intensity_is_unknown(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            await asyncio.sleep(0.15)
            await app.answer_probe(60, 40)
            await asyncio.sleep(0.15)

            snapshot = slot_snapshot(intensity_a=60, intensity_b=40)
            snapshot["devices"][0]["props"].pop("intensityA")
            await app.send(snapshot)
            await asyncio.sleep(0.2)
            state = self.device.state()["apps"][0]
            self.assertIsNone(state["confirmed"]["a"])
            self.assertFalse(state["verified"])

            # 后续有效探测恢复，不需要再补一次目标。
            await asyncio.sleep(0.1)
            await app.answer_probe(60, 40)
            await asyncio.sleep(0.2)
            self.assertEqual(
                self.device.state()["apps"][0]["current"]["a"], 60)
            self.assertEqual(app.current["a"], 60)

    async def test_zero_unconfirmed_never_restores_positive(self) -> None:
        async with FakeV4App(self._app_url()) as app:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            self.device.verify_window_s = 0.25
            self.device.set_strength(50, 50)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 0
                and d["data"].get("v") == 60)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 3
                and d["data"].get("c") == 1
                and d["data"].get("v") == 40)
            self.device.set_strength(0, 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 0)
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            # 不回应探测：归零一直无法确认，不能恢复非零。
            await asyncio.sleep(0.9)
            state = self.device.state()["apps"][0]
            self.assertTrue(state["uncertain"])
            self.assertEqual(app.current, {"a": 0, "b": 0})

    async def test_app_disconnect_clears_loops_like_v3(self) -> None:
        app = await FakeV4App(self._app_url()).__aenter__()
        try:
            await app.send(slot_snapshot())
            await app.wait_for(
                lambda d: d.get("m") == "device.op"
                and d.get("data", {}).get("t") == 7
                and d["data"].get("c") == 1)
            template = Template(
                id="disc", name="disc", units=[UNIT],
                loop=True, source="test", default_ms=100,
            )
            self.device.play("base", "a", template, duration_s=0.3, loop=True)
            self.assertIn("a", self.device.state()["loops"])
        finally:
            await app.__aexit__(None, None, None)
        await asyncio.sleep(0.4)
        self.assertFalse(self.device.connected)
        # 与 V3 一致：最后一个 App 断开即清空 loop；下次配对后由
        # 下一个赢家变化重新起播，不在重连时自动恢复。
        self.assertEqual(self.device.state()["loops"], {})


class V4UrlTest(unittest.TestCase):

    def test_normalize_relay(self) -> None:
        self.assertEqual(normalize_relay("official"),
                         "wss://trex.dungeon-lab.cn/v4")
        self.assertEqual(normalize_relay(""), "wss://trex.dungeon-lab.cn/v4")
        self.assertEqual(normalize_relay("local"), "local")
        self.assertEqual(normalize_relay("https://example.com/v4"),
                         "wss://example.com/v4")
        with self.assertRaises(ValueError):
            normalize_relay("example.com/v4")

    def test_app_socket_url_adds_tid(self) -> None:
        url = app_socket_url("wss://example.com/v4?x=1", "abc")
        self.assertTrue(url.startswith("wss://example.com/v4?"))
        self.assertIn("tid=abc", url)
        self.assertIn("x=1", url)

    def test_user_settings_persist_v4_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "transport": "v4",
                    "v4_relay": "local",
                    "v4_slot": "slot-b",
                }, handle)
            cfg = load_config(user_path=path)
            self.assertEqual(cfg["transport"], "v4")
            self.assertEqual(cfg["v4_relay"], "local")
            self.assertEqual(cfg["v4_slot"], "slot-b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
