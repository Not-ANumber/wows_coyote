# -*- coding: utf-8 -*-
"""V3 Socket 传输：本地 WebSocket 服务 + QR + bind + strength/pulse/clear。

Phase 0 已验证的 wire format：
  {"type":"bind","clientId":...,"targetId":...,"message":"targetId"}
  {"type":"msg",...,"message":"strength-1+2+10"}
  {"type":"msg",...,"message":"pulse-A:[\"HEX\",...]"}
  {"type":"msg",...,"message":"clear-1"}
心跳：{"type":"heartbeat",...,"message":"200"}，25s。
反馈：message "strength-A强度+B强度+A上限+B上限"。

loop 语义：
  play(loop=True) 只发首块；pump(now) 按 period 持续补帧，直到 clear。
  这是 V3 协议没有设备端无限 loop 的本地实现。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS = os.path.join(PROJECT, ".phase0deps")
if os.path.isdir(DEPS) and DEPS not in sys.path:
    sys.path.insert(0, DEPS)

try:
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:
    raise SystemExit(
        "V3Device 需要 websockets >= 13；请先运行：\n"
        "  py -3.13 -m pip install --target .phase0deps websockets qrcode"
    ) from exc

from .device import DeviceSink, Limits
from .templates import Template

LOG = logging.getLogger("standalone.v3")

QR_PREFIX = "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"
MAX_FRAME_CHARS = 1950
SAFE_FRAME_CHARS = 1880
CHANNEL_INDEX = {"a": 1, "b": 2}
CHANNEL_NAME = {"a": "A", "b": "B"}
MODE_SET = 2
HEARTBEAT_S = 25.0


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def clamp_int(value: Any, low: int, high: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = low
    return max(low, min(high, value))


def cmd_strength(channel: str, value: int, mode: int = MODE_SET) -> str:
    return "strength-{}+{}+{}".format(
        CHANNEL_INDEX[channel.lower()], int(mode), clamp_int(value, 0, 200))


def cmd_clear(channel: str) -> str:
    return "clear-{}".format(CHANNEL_INDEX[channel.lower()])


def cmd_pulse(channel: str, units: list[str]) -> str:
    body = ",".join('"{}"'.format(unit) for unit in units)
    return "pulse-{}:[{}]".format(CHANNEL_NAME[channel.lower()], body)


@dataclass
class LoopState:
    owner: str
    channel: str
    template: Template
    units: list[str]
    duration_s: float
    period_s: float
    chunk_frames: int
    offset: int
    next_refill: float = 0.0
    refills: int = 0
    sent_frames: int = 0

    def state(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "template": self.template.id,
            "source": self.template.source,
            "duration_s": round(self.duration_s, 3),
            "period_s": round(self.period_s, 3),
            "chunk_frames": self.chunk_frames,
            "refills": self.refills,
            "sent_frames": self.sent_frames,
        }


class V3Device(DeviceSink):
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9999,
        advertise_ip: str = "",
        qr_path: str = "",
        on_state: Callable[[dict[str, Any]], None] | None = None,
        on_emergency: Callable[[], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.advertise_ip = advertise_ip or None
        if qr_path:
            self.qr_path = qr_path
        elif getattr(sys, "frozen", False):
            # 安装目录可能只读；冻结包默认写到用户目录。
            base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
            self.qr_path = os.path.join(base, "wows_coyote", "qr.svg")
        else:
            self.qr_path = os.path.join(os.path.dirname(__file__), "qr.svg")
        self.on_state = on_state
        self.on_emergency = on_emergency
        self.on_connected = on_connected
        self.log = logger or LOG

        self.client_id = str(uuid.uuid4())
        self._actual_port = self.port
        self._limits = Limits(0, 0)
        self._connected = False
        self._muted = False
        self._stopped = False
        self._last_strength = (0, 0)
        self._last_pct = (0, 0)

        self._clients: dict[Any, str] = {}
        self._bound: set[Any] = set()
        self._loops: dict[str, LoopState] = {}
        self._gen = {"a": 0, "b": 0}
        self._lock = threading.RLock()
        self._outbox: queue.PriorityQueue = queue.PriorityQueue(maxsize=5000)
        self._seq = itertools.count()
        self.sent: list[str] = []

        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._stop_event: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- 展示
    @property
    def limits(self) -> Limits:
        with self._lock:
            return Limits(self._limits.a, self._limits.b)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def ws_url(self) -> str:
        ip = self.advertise_ip or lan_ip()
        return "ws://{}:{}/{}".format(ip, self._actual_port, self.client_id)

    @property
    def local_url(self) -> str:
        return "ws://127.0.0.1:{}/{}".format(self._actual_port, self.client_id)

    @property
    def qr_payload(self) -> str:
        return QR_PREFIX + self.ws_url

    def state(self) -> dict[str, Any]:
        with self._lock:
            loops = {channel: loop.state() for channel, loop in self._loops.items()}
            limits = {"a": self._limits.a, "b": self._limits.b}
            actual = {"a": int(self._last_strength[0]),
                      "b": int(self._last_strength[1])}
        return {
            "connected": self._connected,
            "muted": self._muted,
            "transport": "v3",
            "limits": limits,
            "last_pct": {"a": self._last_pct[0], "b": self._last_pct[1]},
            "actual": actual,
            "loops": loops,
            "outbox": self._outbox.qsize(),
            "ws_url": self.ws_url,
            "qr_path": self.qr_path,
            "client_id": self.client_id,
        }

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("V3Device 已停止，不能再次启动")
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._start_error = None
        self._stop_event = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="dglab-v3", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("V3 WebSocket 服务未能启动（端口 {} 可能被占用）".format(self.port))
        if self._start_error is not None:
            raise RuntimeError("V3 WebSocket 服务启动失败：{}".format(self._start_error))

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_all()
        time.sleep(0.15)  # 给 sender 一点时间把 clear/0 发出去
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._connected = False
        self._notify()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self._start_error = exc
            self.log.error("V3 WebSocket 服务异常退出：%s", exc)
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self) -> None:
        self._server = await serve(
            self._handle, self.host, self.port,
            ping_interval=20, ping_timeout=20,
        )
        self._actual_port = self._server.sockets[0].getsockname()[1]
        self._write_qr()
        self.log.info("V3 服务已启动：%s", self.ws_url)
        self._ready.set()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._sender_task = asyncio.create_task(self._sender_loop())
        assert self._stop_event is not None
        try:
            await self._stop_event.wait()
        finally:
            for task in (self._heartbeat_task, self._sender_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *[task for task in (self._heartbeat_task, self._sender_task)
                  if task is not None],
                return_exceptions=True,
            )
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
        self._notify()

    # ---------------------------------------------------------------- 二维码
    def _write_qr(self) -> None:
        try:
            import qrcode
            import qrcode.image.svg
            image = qrcode.make(
                self.qr_payload, image_factory=qrcode.image.svg.SvgPathImage)
            directory = os.path.dirname(self.qr_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            image.save(self.qr_path)
            self.log.info("二维码已写入：%s", self.qr_path)
        except Exception as exc:
            self.log.warning("二维码生成失败：%s", exc)

    # ---------------------------------------------------------------- 连接
    async def _handle(self, ws: ServerConnection) -> None:
        path = ""
        request = getattr(ws, "request", None)
        if request is not None:
            path = getattr(request, "path", "") or ""
        if not path:
            path = getattr(ws, "path", "") or ""
        token = path.rsplit("/", 1)[-1] if path else ""
        if token and token != self.client_id:
            self.log.warning("未知 clientId 的连接：%s（仍接受）", token)

        target_id = str(uuid.uuid4())
        self._clients[ws] = target_id
        self.log.info("App 已接入：%s", path or "(无路径)")
        try:
            await self._send_raw(ws, {
                "type": "bind", "clientId": self.client_id,
                "targetId": target_id, "message": "targetId",
            })
            async for raw in ws:
                await self._on_message(ws, raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.log.warning("连接异常结束：%s", exc)
        finally:
            self._clients.pop(ws, None)
            self._bound.discard(ws)
            if not self._bound:
                with self._lock:
                    self._loops.clear()
                self._connected = False
                self._drain_outbox()
                self._notify()
                self.log.info("App 已断开，loop 清空；等待重新扫码")
            else:
                self.log.info("App 已断开（剩余 %d）", len(self._bound))

    async def _on_message(self, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            self.log.warning("收到非 JSON：%r", str(raw)[:120])
            return
        mtype = str(data.get("type", ""))
        message = str(data.get("message", ""))

        if mtype == "bind":
            if message == "DGLAB":
                app_target = str(data.get("targetId") or self._clients.get(ws, ""))
                self._clients[ws] = app_target
                await self._send_raw(ws, {
                    "type": "bind", "clientId": self.client_id,
                    "targetId": app_target, "message": "200",
                })
                was_empty = not self._bound
                self._bound.add(ws)
                self._connected = True
                self._drain_outbox()
                # 与 V4 一致：先清掉 App 里可能残留的旧波形，再归零强度；
                # 新连接从 0 开始，下一次引擎 tick 会重新下发当前期望强度。
                await self._send_msg(ws, cmd_clear("a"))
                await self._send_msg(ws, cmd_clear("b"))
                await self._send_msg(ws, cmd_strength("A", 0))
                await self._send_msg(ws, cmd_strength("B", 0))
                # 首个 App 绑定且配对前已有 loop 时立即补一块，让波形接上。
                with self._lock:
                    self._last_strength = (0, 0)
                    loops = ([(channel, loop.offset, loop.chunk_frames,
                               loop.units)
                              for channel, loop in self._loops.items()]
                             if was_empty else [])
                    for channel, offset, chunk_frames, units in loops:
                        self._send_pulse(
                            channel, self._repeat(units, offset, chunk_frames))
                if loops:
                    self.log.info("绑定后恢复 %d 个 loop", len(loops))
                self._notify_connected()
                self._notify()
                self.log.info("✅ App 已绑定 targetId=%s", app_target)
            return

        if mtype == "msg":
            if message.startswith("strength-"):
                self._parse_strength_feedback(message)
            elif message.startswith("feedback-"):
                try:
                    index = int(message.split("-", 1)[1])
                except (IndexError, ValueError):
                    index = -1
                self._on_feedback(index)
            else:
                self.log.debug("App 上行：%s", message)
            return

        if mtype == "heartbeat":
            if message != "200":
                await self._send_raw(ws, {
                    "type": "heartbeat", "clientId": self.client_id,
                    "targetId": self._clients.get(ws, ""), "message": "200",
                })
            return

        if mtype in ("break", "error"):
            self.log.warning("App 通知 %s：%s", mtype, message)
            self._bound.discard(ws)
            if not self._bound:
                self._connected = False
                with self._lock:
                    self._loops.clear()
                self._notify()

    def _parse_strength_feedback(self, message: str) -> None:
        try:
            parts = message.split("-", 1)[1].split("+")
            if len(parts) != 4:
                return
            _a, _b, a_lim, b_lim = (int(part) for part in parts)
        except Exception:
            return
        with self._lock:
            self._limits = Limits(clamp_int(a_lim, 0, 200), clamp_int(b_lim, 0, 200))
            pct_a, pct_b = self._last_pct
        self.log.info("通道反馈：上限 A=%d B=%d", self._limits.a, self._limits.b)
        # 上限变化后按当前期望强度重算一次；与 V4 的 intensityMax 更新一致。
        self.set_strength(pct_a, pct_b)
        self._notify()

    def _on_feedback(self, index: int) -> None:
        self.log.info("App 反馈按钮 #%d", index)
        if index == 0:
            if self._muted:
                self.rearm()
            else:
                self.emergency_stop("App 反馈按钮")
        self._notify()

    # ---------------------------------------------------------------- 发送
    def _enqueue(self, message: str, priority: int = 2,
                 channel: str | None = None, gen: int | None = None) -> None:
        try:
            self._outbox.put_nowait(
                (priority, next(self._seq), message, channel, gen))
        except queue.Full:
            self.log.warning("发送队列已满，丢弃：%s", message[:80])

    def _drain_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    async def _sender_loop(self) -> None:
        while True:
            try:
                _priority, _seq, message, channel, gen = await asyncio.to_thread(
                    self._outbox.get, True, 0.5)
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.debug("sender 取队列失败：%s", exc)
                continue
            # clear 之后，队列里同通道的旧 pulse 已经过期，直接丢弃。
            if (message.startswith("pulse-") and channel is not None
                    and gen is not None and gen != self._gen.get(channel)):
                continue
            if not self._bound:
                continue
            for ws in list(self._bound):
                try:
                    await self._send_msg(ws, message)
                except Exception:
                    self._bound.discard(ws)

    async def _send_raw(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self.sent.append(text)
        if len(self.sent) > 500:
            del self.sent[:250]
        await ws.send(text)

    async def _send_msg(self, ws: ServerConnection, message: str) -> None:
        await self._send_raw(ws, {
            "type": "msg", "clientId": self.client_id,
            "targetId": self._clients.get(ws, ""), "message": message,
        })

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            for ws in list(self._bound):
                try:
                    await self._send_raw(ws, {
                        "type": "heartbeat", "clientId": self.client_id,
                        "targetId": self._clients.get(ws, ""), "message": "200",
                    })
                except Exception:
                    self._bound.discard(ws)

    def _max_units(self) -> int:
        payload = {
            "type": "msg", "clientId": self.client_id,
            "targetId": "x" * 36, "message": "",
        }
        overhead = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        units = min(100, max(1, (SAFE_FRAME_CHARS - overhead) // 21))
        while units > 1:
            payload["message"] = cmd_pulse("A", ["0A0A0A0A64646464"] * units)
            size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
            if size <= MAX_FRAME_CHARS - 20:
                break
            units -= 1
        return units

    def _send_pulse(self, channel: str, units: list[str]) -> None:
        if not units:
            return
        step = self._max_units()
        gen = self._gen.get(channel)
        for index in range(0, len(units), step):
            self._enqueue(
                cmd_pulse(channel, units[index:index + step]),
                priority=3, channel=channel, gen=gen)

    # ---------------------------------------------------------------- DeviceSink
    def set_strength(self, a: int, b: int, force: bool = False) -> None:
        pct_a = clamp_int(a, 0, 100)
        pct_b = clamp_int(b, 0, 100)
        with self._lock:
            self._last_pct = (pct_a, pct_b)
            if self._muted:
                pct_a = pct_b = 0
            raw_a = min(self._limits.a,
                        int(round(self._limits.a * pct_a / 100.0)))
            raw_b = min(self._limits.b,
                        int(round(self._limits.b * pct_b / 100.0)))
        if (not force and self._last_strength == (raw_a, raw_b)):
            return
        self._last_strength = (raw_a, raw_b)
        self._enqueue(cmd_strength("A", raw_a), priority=0)
        self._enqueue(cmd_strength("B", raw_b), priority=0)
        self.log.debug("set_strength raw A=%d B=%d", raw_a, raw_b)

    def play(self, owner: str, channel: str, template: Template,
             duration_s: float, loop: bool = False,
             append: bool = False) -> None:
        if self._muted:
            return
        channel = channel.lower()
        if channel not in CHANNEL_INDEX:
            return
        units = list(template.units)
        if not units:
            return

        duration_s = max(0.1, float(duration_s))
        frames_count = max(1, int(round(duration_s * 10)))
        period_s = max(0.5, min(2.0, duration_s / 2.0))
        chunk_frames = max(1, int(round(period_s * 10)))
        initial_frames = max(chunk_frames, frames_count)

        with self._lock:
            if not loop:
                self._loops.pop(channel, None)
                self._send_pulse(channel, self._repeat(units, 0, frames_count))
                return

            old = self._loops.get(channel)
            if append and old is not None:
                offset = old.offset
                self._send_pulse(channel, self._repeat(units, offset, chunk_frames))
                old.offset = (offset + chunk_frames) % len(units)
                old.sent_frames += chunk_frames
                return

            offset = 0
            self._send_pulse(channel, self._repeat(units, offset, initial_frames))
            offset = (offset + initial_frames) % len(units)
            self._loops[channel] = LoopState(
                owner=owner,
                channel=channel,
                template=template,
                units=units,
                duration_s=duration_s,
                period_s=period_s,
                chunk_frames=chunk_frames,
                offset=offset,
                sent_frames=initial_frames,
            )
        self.log.info("play %s %s template=%s loop=%s initial=%d",
                      owner, channel, template.id, loop, initial_frames)

    def pump(self, now: float) -> None:
        if self._muted:
            return
        with self._lock:
            for channel, loop in list(self._loops.items()):
                if loop.next_refill <= 0:
                    loop.next_refill = now + loop.period_s
                    continue
                if now < loop.next_refill:
                    continue
                # 一次只补一块，避免长时间停顿后疯狂追帧。
                self._send_pulse(
                    channel, self._repeat(loop.units, loop.offset, loop.chunk_frames))
                loop.offset = (loop.offset + loop.chunk_frames) % len(loop.units)
                loop.sent_frames += loop.chunk_frames
                loop.refills += 1
                loop.next_refill = now + loop.period_s

    def clear(self, channel: str, owner: str | None = None) -> None:
        channel = channel.lower()
        if channel not in CHANNEL_INDEX:
            return
        with self._lock:
            loop = self._loops.get(channel)
            if loop is not None and (owner is None or loop.owner == owner):
                self._loops.pop(channel, None)
            self._gen[channel] += 1
            gen = self._gen[channel]
        self._enqueue(cmd_clear(channel), priority=0, channel=channel, gen=gen)

    def stop_all(self) -> None:
        with self._lock:
            self._loops.clear()
            self._gen["a"] += 1
            self._gen["b"] += 1
            gen_a, gen_b = self._gen["a"], self._gen["b"]
        self._enqueue(cmd_clear("a"), priority=0, channel="a", gen=gen_a)
        self._enqueue(cmd_clear("b"), priority=0, channel="b", gen=gen_b)
        self._enqueue(cmd_strength("A", 0), priority=0)
        self._enqueue(cmd_strength("B", 0), priority=0)
        self._last_strength = (0, 0)

    def emergency_stop(self, reason: str = "急停") -> None:
        self._muted = True
        with self._lock:
            self._loops.clear()
            self._gen["a"] += 1
            self._gen["b"] += 1
            gen_a, gen_b = self._gen["a"], self._gen["b"]
        self._enqueue(cmd_clear("a"), priority=0, channel="a", gen=gen_a)
        self._enqueue(cmd_clear("b"), priority=0, channel="b", gen=gen_b)
        self._enqueue(cmd_strength("A", 0), priority=0)
        self._enqueue(cmd_strength("B", 0), priority=0)
        self._last_strength = (0, 0)
        self.log.warning("急停：%s", reason)
        callback = self.on_emergency
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
        self._notify()

    def rearm(self) -> None:
        self._muted = False
        self.log.info("已重新武装")
        self._notify()

    # ---------------------------------------------------------------- 工具

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _repeat(units: list[str], offset: int, count: int) -> list[str]:
        return [units[(offset + index) % len(units)] for index in range(count)]

    def _notify_connected(self) -> None:
        callback = self.on_connected
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def _notify(self) -> None:
        callback = self.on_state
        if callback is None:
            return
        try:
            callback(self.state())
        except Exception:
            pass
