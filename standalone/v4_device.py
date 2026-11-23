# -*- coding: utf-8 -*-
"""V4 传输：作为“控制方”连接 V4 Relay（官方或本机中继）。

V4 wire format（来自 dglab-kit README_CN「V4 协议参考」）：
  控制方 → Relay: {"type":"message","clientId":<app>,"data":{...}}
  App   → Relay: {"type":"message","data":{...}}
  Relay → 控制方: {"type":"message","clientId":<app>,"data":{...}}

应用层 RPC：
  {"t":"req","reqId":...,"m":"devices.get"|"ping"|"device.op"|"device.op.clear"}
  device.op: {s:slotId, t:0 AppendPulseData | 3 AddIntensity |
             7 SetIntensity(只接受 0), c:0=A/1=B, p:1, d:ms, im:bool, v:...}
  {"t":"resp","reqId":...,"result"|"error"}
  {"t":"ev","ev":"devices.snapshot"|"devices.patch"|"slots.patch"|"custom.action"}

与 V3 的关键差异：
  - 强度不是绝对设置：V4 只有 AddIntensity（增量）和 SetIntensity(0)；
    这里维护每 App/通道的当前强度，按差值下发；每个目标变化后用
    `devices.get` 主动读取 props.intensityA/B 校验。App 主动推送的
    slots.patch/完整快照如果带来新的强度值，也可用于确认在途增量；
    同一个旧值不会连续补正向增量，必要时归零锚定。
  - 波形靠 `im:true` 替换同通道同类型任务；clear 用 device.op.clear。
  - 设备由 App 上报（devices.snapshot/patch），操作必须带 slotId。
  - 支持 1 控制方 : N App；本实现对每个已接入 App 广播同一输出。

本模块不依赖 dglab-kit-python；直接按协议实现，避免运行时依赖 SDK。
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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS = os.path.join(PROJECT, ".phase0deps")
if os.path.isdir(DEPS) and DEPS not in sys.path:
    sys.path.insert(0, DEPS)

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:
    raise SystemExit(
        "V4Device 需要 websockets >= 13；请先运行：\n"
        "  py -3.13 -m pip install --target .phase0deps websockets qrcode"
    ) from exc

from .device import DeviceSink, Limits
from .templates import Template
from .v4_relay import LocalV4Relay

LOG = logging.getLogger("standalone.v4")

DEFAULT_RELAY_URL = "wss://trex.dungeon-lab.cn/v4"
PAIRING_PREFIX = "https://dungeon-lab.cn/s/?v=1&action=socket&url="
LOCAL_RELAY_PORT = 9998
CHANNEL_INDEX = {"a": 0, "b": 1}
CHANNEL_LIMIT_KEY = {"a": "channelA", "b": "channelB"}
CHANNEL_INTENSITY_KEY = {"a": "intensityA", "b": "intensityB"}
MAX_CHUNK_FRAMES = 100
FEEDBACK_SETTLE_S = 0.75
RECONCILE_POLL_S = 0.5
VERIFY_WINDOW_S = 4.0
ANCHOR_GRACE_S = 0.75
WARMUP_READY = 0.99
PING_INTERVAL_S = 10.0
MAX_MISSED_PONGS = 3
RECONNECT_MAX_S = 30.0


def clamp_int(value: Any, low: int, high: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = low
    return max(low, min(high, value))


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def normalize_relay(value: str | None) -> str:
    """official/local/ws(s)://... → 具体 Relay URL 或 "local"。"""
    text = str(value or "").strip()
    if not text or text.lower() in ("official", "default", "cloud"):
        return DEFAULT_RELAY_URL
    if text.lower() in ("local", "self", "relay"):
        return "local"
    if text.startswith("https://"):
        text = "wss://" + text[len("https://"):]
    elif text.startswith("http://"):
        text = "ws://" + text[len("http://"):]
    if not text.startswith(("ws://", "wss://")):
        raise ValueError(
            "V4 Relay 必须是 official/local 或 ws(s):// 地址：{}".format(value))
    return text


def app_socket_url(relay_url: str, target_id: str) -> str:
    """App 侧连接地址：保持 Relay 地址并把 tid 放进 query。"""
    parts = urlsplit(relay_url)
    query = [(key, value) for key, value in
             parse_qsl(parts.query, keep_blank_values=True) if key != "tid"]
    query.append(("tid", target_id))
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path or "/",
        urlencode(query), parts.fragment))


def pairing_qr_payload(relay_url: str, target_id: str) -> str:
    return PAIRING_PREFIX + quote(app_socket_url(relay_url, target_id), safe="")


def _merge_patch(current: Any, patch: Any) -> dict[str, Any]:
    result = dict(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return result
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = value
    return result


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


class V4Device(DeviceSink):
    def __init__(
        self,
        relay: str = "official",
        host: str = "0.0.0.0",
        port: int = LOCAL_RELAY_PORT,
        advertise_ip: str = "",
        qr_path: str = "",
        slot_id: str = "",
        idle_timeout_s: float = 0.0,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        on_emergency: Callable[[], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
        connect_timeout: float = 12.0,
    ) -> None:
        mode = normalize_relay(relay)
        self.relay_mode = "local" if mode == "local" else "remote"
        self.relay_url = "" if mode == "local" else mode
        self.host = host
        self.port = int(port)
        self.advertise_ip = advertise_ip or ""
        self.configured_slot = str(slot_id or "").strip()
        self.idle_timeout_s = max(0.0, float(idle_timeout_s))
        self.connect_timeout = max(3.0, float(connect_timeout))
        if qr_path:
            self.qr_path = qr_path
        elif getattr(sys, "frozen", False):
            base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
            self.qr_path = os.path.join(base, "wows_coyote", "qr.svg")
        else:
            self.qr_path = os.path.join(os.path.dirname(__file__), "qr.svg")
        self.on_state = on_state
        self.on_emergency = on_emergency
        self.on_connected = on_connected
        self.log = logger or LOG

        self._session_token = uuid.uuid4().hex[:8]
        self._req_seq = itertools.count(1)
        self._target_id = ""
        self._controller_url = ""
        self._app_base_url = ""
        self._local_port = 0
        self._ws = None
        self._apps: dict[str, dict[str, Any]] = {}
        self._loops: dict[str, LoopState] = {}
        # (clientId, channel) -> 递增代次；clear/替换波形后丢弃旧帧。
        self._pulse_gen: dict[tuple[str, str], int] = {}
        self._muted = False
        self._stopped = False
        self._connected = False
        self._last_pct = (0, 0)
        self._last_strength = (0, 0)
        self.feedback_settle_s = FEEDBACK_SETTLE_S
        self.reconcile_poll_s = RECONCILE_POLL_S
        self.verify_window_s = VERIFY_WINDOW_S
        self.anchor_grace_s = ANCHOR_GRACE_S
        self.probe_retry_s = 2.0
        self._ping_rtt_ms = 0.0
        self._missed_pongs = 0
        self._reconnects = 0
        self._last_error = ""

        self._lock = threading.RLock()
        self._outbox: queue.PriorityQueue = queue.PriorityQueue(maxsize=8000)
        self._seq = itertools.count()
        self.sent: list[str] = []

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._stop_event: asyncio.Event | None = None
        self._sender_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._relay: LocalV4Relay | None = None

    # ---------------------------------------------------------------- 展示
    @property
    def limits(self) -> Limits:
        with self._lock:
            values = [app["limits"] for app in self._apps.values()]
        if not values:
            return Limits(0, 0)
        return Limits(
            min(int(item[0]) for item in values),
            min(int(item[1]) for item in values))

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def app_url(self) -> str:
        if not self._target_id or not self._app_base_url:
            return ""
        return app_socket_url(self._app_base_url, self._target_id)

    @property
    def qr_payload(self) -> str:
        if not self._target_id or not self._app_base_url:
            return ""
        return pairing_qr_payload(self._app_base_url, self._target_id)

    @property
    def ws_url(self) -> str:
        return self._controller_url

    @property
    def local_url(self) -> str:
        return "ws://127.0.0.1:{}/v4".format(self._local_port)

    def state(self) -> dict[str, Any]:
        with self._lock:
            apps = []
            for client_id, app in self._apps.items():
                slot = app.get("slot") or {}
                slot_state = slot.get("slotState") or {}
                apps.append({
                    "client_id": client_id,
                    "slot_id": app.get("slot_id") or "",
                    "name": str(slot.get("name") or ""),
                    "type": str(slot.get("type") or ""),
                    "has_device": bool(slot_state.get("hasDevice", True)),
                    "limits": {"a": int(app["limits"][0]),
                               "b": int(app["limits"][1])},
                    "current": {"a": int(app["current"][0]),
                                "b": int(app["current"][1])},
                    "confirmed": {
                        "a": app["confirmed"][0],
                        "b": app["confirmed"][1],
                    },
                    "verified": bool(app["verified"]),
                    "uncertain": bool(any(app["uncertain"])),
                    "awaiting_zero": bool(any(app["awaiting_zero"])),
                })
            limits = {
                "a": min([item["limits"]["a"] for item in apps], default=0),
                "b": min([item["limits"]["b"] for item in apps], default=0),
            }
            current = {
                "a": min([item["current"]["a"] for item in apps], default=0),
                "b": min([item["current"]["b"] for item in apps], default=0),
            }
            loops = {channel: loop.state()
                     for channel, loop in self._loops.items()}
            first = apps[0] if apps else None
            return {
                "connected": self._connected,
                "muted": self._muted,
                "transport": "v4",
                "relay_mode": self.relay_mode,
                "relay_url": self._controller_url,
                "local_port": self._local_port,
                "target_id": self._target_id,
                "client_id": self._target_id,
                "app_url": self.app_url,
                "ws_url": self._controller_url,
                "qr_payload": self.qr_payload,
                "qr_path": self.qr_path,
                "limits": limits,
                "last_pct": {"a": self._last_pct[0], "b": self._last_pct[1]},
                "current": current,
                "actual": current,
                "loops": loops,
                "outbox": self._outbox.qsize(),
                "apps": apps,
                "app_count": len(apps),
                "slot": ({
                    "slot_id": first["slot_id"],
                    "name": first["name"],
                    "type": first["type"],
                    "has_device": first["has_device"],
                } if first else None),
                "reconnects": self._reconnects,
                "ping_rtt_ms": round(self._ping_rtt_ms, 1),
                "last_error": self._last_error,
            }

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("V4Device 已停止，不能再次启动")
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._start_error = None
        self._stop_event = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="dglab-v4", daemon=True)
        self._thread.start()
        timeout = max(8.0, self.connect_timeout + 3.0)
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(
                "V4 Relay 未能在 {:.0f}s 内完成握手：{}".format(
                    timeout, self._controller_url or "(本地中继)"))
        if self._start_error is not None:
            raise RuntimeError("V4 连接失败：{}".format(self._start_error))

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_all()
        time.sleep(0.35)  # 给 sender 一点时间把 clear/0 发出去
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._ws = None
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
            self.log.error("V4 服务异常退出：%s", exc)
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self) -> None:
        if self.relay_mode == "local":
            self._relay = LocalV4Relay(
                host=self.host, port=self.port,
                idle_timeout_s=self.idle_timeout_s, logger=self.log)
            self._local_port = await self._relay.start()
            self._controller_url = self.local_url
            ip = self.advertise_ip or lan_ip()
            if ip.startswith("127.") and not self.advertise_ip:
                self.log.warning(
                    "本机 V4 中继未找到局域网 IP；手机可能无法连接。"
                    "请用 --advertise-ip 指定本机局域网地址。")
            self._app_base_url = "ws://{}:{}/v4".format(ip, self._local_port)
        else:
            self._controller_url = self.relay_url
            self._app_base_url = self.relay_url
        assert self._stop_event is not None
        self._sender_task = asyncio.create_task(self._sender_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._sync_task = asyncio.create_task(self._sync_loop())
        connect_task = asyncio.create_task(self._connect_loop())
        try:
            await self._stop_event.wait()
        finally:
            for task in (connect_task, self._sender_task, self._ping_task,
                         self._sync_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *[task for task in (connect_task, self._sender_task,
                                    self._ping_task, self._sync_task)
                  if task is not None],
                return_exceptions=True)
            ws, self._ws = self._ws, None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._relay is not None:
                try:
                    await self._relay.stop()
                except Exception:
                    self.log.debug("停止本地 V4 中继失败", exc_info=True)
                self._relay = None
            self._on_link_lost()
        self._notify()

    # ---------------------------------------------------------------- 连接
    async def _connect_loop(self) -> None:
        delay = 0.5
        first = True
        while not self._stopped:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self.log.warning("V4 Relay 连接失败：%s", exc)
                if first and not self._ready.is_set():
                    self._start_error = exc
                    self._ready.set()
            first = False
            if self._stopped:
                break
            self._on_link_lost()
            if self._stop_event is not None and self._stop_event.is_set():
                break
            self._reconnects += 1
            self._notify()
            try:
                assert self._stop_event is not None
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(RECONNECT_MAX_S, delay * 2.0)

    async def _session(self) -> None:
        self.log.info("连接 V4 Relay：%s", self._controller_url)
        async with ws_connect(
            self._controller_url,
            ping_interval=20, ping_timeout=20,
            open_timeout=self.connect_timeout, max_size=None,
        ) as ws:
            self._ws = ws
            self.log.info("V4 Relay 已连接")
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    self.log.debug("收到非 JSON 帧：%r", str(raw)[:120])
                    continue
                if isinstance(frame, dict):
                    await self._handle_frame(frame)
        if self._ws is not None:
            self._ws = None
        self.log.info("V4 Relay 连接已关闭")

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "hello":
            client_id = str(frame.get("clientId") or "")
            with self._lock:
                self._target_id = client_id
                self._last_error = ""
            self._start_error = None
            self._write_qr()
            self._ready.set()
            self.log.info("✅ V4 控制方 ID：%s", client_id)
            self._notify()
        elif kind == "client_attached":
            await self._on_app_attached(str(frame.get("clientId") or ""))
        elif kind == "client_disconnected":
            self._on_app_detached(str(frame.get("clientId") or ""))
        elif kind == "message":
            await self._on_app_message(
                str(frame.get("clientId") or ""), frame.get("data"))
        elif kind == "heartbeat":
            return
        elif kind == "pong":
            self._missed_pongs = 0
            ts = frame.get("ts")
            if isinstance(ts, (int, float)) and ts > 1_000_000_000_000:
                self._ping_rtt_ms = max(0.0, time.time() * 1000.0 - ts)
        elif kind == "idle_timeout":
            self._last_error = "idle_timeout"
            self.log.warning("V4 Relay 空闲超时；将自动重连并刷新二维码")
        elif kind == "error":
            message = str(frame.get("message") or frame.get("code") or "V4 Relay 错误")
            self._last_error = message
            if message == "client_not_found":
                # App 刚断开/重连窗口内的正常竞态。
                self.log.debug("V4 Relay 错误：%s", message)
            else:
                self.log.warning("V4 Relay 错误：%s", message)

    def _on_link_lost(self) -> None:
        with self._lock:
            self._ws = None
            self._apps.clear()
            self._loops.clear()
            self._pulse_gen.clear()
            self._target_id = ""
            self._connected = False
        self._drain_outbox()
        self._notify()

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            ws = self._ws
            if ws is None:
                continue
            if self._missed_pongs >= MAX_MISSED_PONGS:
                self.log.warning("V4 Relay ping 连续丢失，重连")
                try:
                    await ws.close()
                except Exception:
                    pass
                continue
            self._missed_pongs += 1
            try:
                await self._send_raw(ws, {"type": "ping"})
            except Exception:
                pass

    async def _sync_loop(self) -> None:
        """周期性向 App 要一次设备快照，补上丢失的 slots.patch。

        增量强度一旦丢失一条就无法靠后续增量自愈；这里只为尚未确认的
        目标发 `devices.get`，确认后立即停止，避免长期覆盖 App 手动调整。
        """
        while True:
            await asyncio.sleep(self.reconcile_poll_s)
            if self._ws is None:
                continue
            changed = False
            probes: list[tuple[str, str, str]] = []
            with self._lock:
                now = time.monotonic()
                for client_id, app in list(self._apps.items()):
                    if not app.get("slot_id"):
                        continue
                    for index in (0, 1):
                        if (app["needs_anchor"][index]
                                and not app["in_flight"][index]
                                and now >= float(app["anchor_at"][index])):
                            app["needs_anchor"][index] = False
                            if self._enqueue_zero_locked(
                                    app, index, now, wait=True):
                                app["zero_retries"][index] = 0
                                changed = True
                    target = self._verification_target(app)
                    if (not app["verified"] and not app["safety_latched"]
                            and now > app["verify_until"]):
                        if self._expire_verification_locked(app, now):
                            changed = True
                    self._recompute_verified_locked(app, target)
                    if self._should_probe_locked(app, now):
                        req_id = self._req_id()
                        app["probes"][req_id] = (int(app["cmd_seq"]), now)
                        app["last_probe_at"] = now
                        probes.append((client_id, req_id,
                                       str(app.get("slot_id") or "")))
            if changed:
                self._notify()
            for client_id, req_id, slot_id in probes:
                self._enqueue("sync", {"reqId": req_id}, priority=0,
                              client=client_id, slot=slot_id)

    def _should_probe_locked(self, app: dict[str, Any], now: float) -> bool:
        if app["verified"] or app["safety_latched"] or now > app["verify_until"]:
            return False
        if int(app["pending_cmds"]) > 0:
            return False
        if app["probes"]:
            oldest = min(sent_at for _gen, sent_at in app["probes"].values())
            if now - oldest < self.probe_retry_s:
                return False
            # 探测本身丢失（或 App 没回）：允许重发；迟到的旧响应按 stale 处理。
            app["probes"].clear()
        return (now - float(app["last_cmd_sent_at"] or 0.0)
                >= self.feedback_settle_s)

    # ---------------------------------------------------------------- App
    @staticmethod
    def _new_app(client_id: str) -> dict[str, Any]:
        return {
            "client_id": client_id,
            "devices": [],
            "slot": None,
            "slot_id": "",
            "limits": [0, 0],
            "current": [0, 0],
            "reported": [None, None],
            "confirmed": [None, None],
            "confirmed_at": [0.0, 0.0],
            "controllable": [True, True],
            "in_flight": [False, False],
            "sent_baseline": [None, None],
            "sent_delta": [0, 0],
            "sending": ["", ""],
            "uncertain": [False, False],
            "awaiting_zero": [False, False],
            "needs_anchor": [False, False],
            "anchor_at": [0.0, 0.0],
            "zero_retries": [0, 0],
            "last_strength": (0, 0),
            "last_strength_at": 0.0,
            "verify_until": 0.0,
            "verified": True,
            "safety_latched": False,
            "manual_hold": False,
            "warm_up": [1.0, 1.0],
            "pending_cmds": 0,
            "last_cmd_sent_at": 0.0,
            "cmd_seq": 0,
            "probes": {},
            "last_probe_at": 0.0,
        }

    async def _on_app_attached(self, client_id: str) -> None:
        if not client_id:
            return
        with self._lock:
            if client_id in self._apps:
                return
            self._apps[client_id] = self._new_app(client_id)
            self._connected = True
        self.log.info("✅ V4 App 已接入：%s", client_id)
        self._notify()
        await self._bootstrap_app(client_id)

    async def _bootstrap_app(self, client_id: str) -> None:
        # 先清空该 App 的残留任务，再拉设备列表；避免上一次会话的输出继续。
        await self._send_rpc(client_id, "device.op.clear", None)
        await self._send_rpc(client_id, "devices.get")

    def _on_app_detached(self, client_id: str) -> None:
        with self._lock:
            existed = self._apps.pop(client_id, None) is not None
            for channel in ("a", "b"):
                self._pulse_gen.pop((client_id, channel), None)
            if not self._apps:
                self._connected = False
                self._pulse_gen.clear()
                # 与 V3 一致：最后一个 App 断开即清空 loop；
                # 下次配对后要在下一个赢家变化时重新起播。
                self._loops.clear()
        if existed:
            self.log.info("V4 App 已断开：%s", client_id)
        if not self._apps:
            self._drain_outbox()
        self._notify()

    # ---------------------------------------------------------------- 上行
    async def _on_app_message(self, client_id: str, data: Any) -> None:
        if not isinstance(data, dict) or client_id not in self._apps:
            return
        kind = str(data.get("t") or "")
        if kind == "ev":
            event = str(data.get("ev") or "")
            if event == "devices.snapshot":
                await self._replace_devices(
                    client_id, data.get("devices") or [], mode="snapshot")
            elif event == "devices.patch":
                await self._patch_devices(
                    client_id, data.get("added") or [], data.get("removed") or [])
            elif event == "slots.patch":
                self._patch_slots(client_id, data.get("slots") or [])
            elif event == "custom.action":
                action = data.get("action")
                if isinstance(action, int) and 0 <= action <= 9:
                    self._on_action(action)
        elif kind == "resp":
            mode = "live"
            req_id = str(data.get("reqId") or "")
            probe = None
            with self._lock:
                app = self._apps.get(client_id)
                if app is not None and req_id:
                    probe = app["probes"].pop(req_id, None)
                    if probe is not None:
                        # 探测请求之后又下发了新强度：这份快照相对新目标已过期，
                        # 只能用于刷新设备结构，不能拿来判错/回正。
                        mode = ("probe" if int(probe[0]) == app["cmd_seq"]
                                else "stale")
            error = data.get("error")
            result = data.get("result")
            if (app is not None and app.get("slot_id") and req_id
                    and probe is None and not error
                    and isinstance(result, dict)
                    and isinstance(result.get("devices"), list)):
                # 设备已选中后，未登记的 devices.get 响应只可能是目标变化后
                # 被作废的旧探测；整份丢弃，避免旧上限/旧强度覆盖新状态。
                self.log.debug("忽略过期 V4 响应：%s", req_id)
                return
            if error:
                self.log.warning("V4 App 返回错误：%s", error)
                return
            if isinstance(result, dict) and isinstance(result.get("devices"), list):
                await self._replace_devices(
                    client_id, result["devices"], mode=mode)
            elif isinstance(result, dict) and result.get("reason"):
                self.log.debug("V4 device.op 完成：%s", result.get("reason"))

    def _on_action(self, action: int) -> None:
        self.log.info("V4 App 反馈动作：%d", action)
        if action != 0:
            return
        if self._muted:
            self.rearm()
        else:
            self.emergency_stop("App 反馈按钮")
        self._notify()

    async def _replace_devices(self, client_id: str,
                               devices: list[Any],
                               mode: str = "live") -> None:
        if mode == "stale":
            # 探测响应生成后已有新指令；整份快照都不能再用于判错/选设备。
            return
        cleaned = [dict(device) for device in devices
                   if isinstance(device, dict) and device.get("slotId")]
        with self._lock:
            app = self._apps.get(client_id)
            if app is None:
                return
            app["devices"] = cleaned
        await self._select_slot(client_id, mode)
        self._notify()

    async def _patch_devices(self, client_id: str, added: list[Any],
                             removed: list[Any]) -> None:
        with self._lock:
            app = self._apps.get(client_id)
            if app is None:
                return
            devices: list[dict[str, Any]] = app["devices"]
            by_slot = {str(device.get("slotId")): device for device in devices}
            for device in added:
                if not isinstance(device, dict) or not device.get("slotId"):
                    continue
                by_slot[str(device["slotId"])] = dict(device)
            for slot_id in removed:
                by_slot.pop(str(slot_id), None)
            app["devices"] = list(by_slot.values())
        await self._select_slot(client_id, "live")
        self._notify()

    def _patch_slots(self, client_id: str, slots: list[Any]) -> None:
        now = time.monotonic()
        changed = False
        with self._lock:
            app = self._apps.get(client_id)
            if app is None:
                return
            devices: list[dict[str, Any]] = app["devices"]
            by_slot = {str(device.get("slotId")): device for device in devices}
            for item in slots:
                if not isinstance(item, dict):
                    continue
                slot_id = str(item.get("slotId") or "")
                device = by_slot.get(slot_id)
                if device is None:
                    continue
                device["props"] = _merge_patch(
                    device.get("props") or {}, item.get("props") or {})
                device["slotState"] = _merge_patch(
                    device.get("slotState") or {}, item.get("slotState") or {})
                if slot_id == app.get("slot_id"):
                    props_patch = item.get("props") or {}
                    feedback = set()
                    if isinstance(props_patch, dict):
                        if "intensityA" in props_patch:
                            feedback.add(0)
                        if "intensityB" in props_patch:
                            feedback.add(1)
                    changed = (self._refresh_slot_locked(
                        app, device, now, mode="live",
                        feedback_channels=feedback) or changed)
            if app.get("slot_id"):
                app["slot"] = by_slot.get(app["slot_id"], app.get("slot"))
        if changed:
            self._notify()

    def _verification_target(self, app: dict[str, Any]) -> tuple[int, int]:
        """当前目标，按 App 上限截断；静音时一律 0。"""
        if self._muted:
            return 0, 0
        limits = app["limits"]
        target = app["last_strength"]
        return (
            min(int(target[0]), int(limits[0])),
            min(int(target[1]), int(limits[1])),
        )

    def _begin_verification_locked(self, app: dict[str, Any],
                                   desired: tuple[int, int],
                                   now: float) -> None:
        app["last_strength"] = (int(desired[0]), int(desired[1]))
        app["last_strength_at"] = now
        app["verified"] = False
        app["safety_latched"] = False
        app["manual_hold"] = False
        app["verify_until"] = now + self.verify_window_s
        app["zero_retries"] = [0, 0]

    @staticmethod
    def _warm_up_ready(app: dict[str, Any]) -> bool:
        return all(float(value) >= WARMUP_READY
                   for value in app["warm_up"])

    def _scaled_match(self, app: dict[str, Any], index: int,
                      desired: int, observed: int) -> bool:
        scale = float(app["warm_up"][index])
        if scale <= 0.0 or scale >= WARMUP_READY:
            return False
        if int(desired) <= 0 or int(observed) <= 0:
            # 0 × 任何系数 = 0；不能把“还没有输出”误判成“已到位”。
            return False
        return abs(int(observed) - int(round(desired * scale))) <= 1

    def _settled_locked(self, app: dict[str, Any], index: int,
                        target: tuple[int, int]) -> bool:
        if (app["in_flight"][index]
                or app["awaiting_zero"][index]
                or app["needs_anchor"][index]
                or app["uncertain"][index]):
            return False
        confirmed = app["confirmed"][index]
        if confirmed is None:
            return False
        desired = int(target[index])
        observed = int(confirmed)
        if desired == 0:
            return observed == 0
        if observed == desired:
            return True
        return (not self._warm_up_ready(app)
                and observed < desired
                and self._scaled_match(app, index, desired, observed))

    def _recompute_verified_locked(self, app: dict[str, Any],
                                   target: tuple[int, int]) -> None:
        pending = any(
            app["in_flight"][index]
            or app["awaiting_zero"][index]
            or app["needs_anchor"][index]
            or app["uncertain"][index]
            for index in (0, 1))
        if app["manual_hold"] and not pending:
            app["verified"] = True
            app["probes"].clear()
            return
        verified = all(self._settled_locked(app, index, target)
                       for index in (0, 1))
        app["verified"] = verified
        if verified:
            app["probes"].clear()

    def _enqueue_add_locked(self, app: dict[str, Any], index: int,
                            desired: int, now: float) -> bool:
        slot = str(app.get("slot_id") or "")
        client_id = str(app.get("client_id") or "")
        base = app["confirmed"][index]
        if not slot or not client_id or base is None:
            return False
        delta = int(desired) - int(base)
        if delta == 0:
            app["current"][index] = int(desired)
            return False
        if (delta > 0
                and not self._warm_up_ready(app)
                and self._scaled_match(app, index, desired, int(base))):
            # 冷启动期间上报的是缩放值：设置值已经到位，不能再补。
            app["current"][index] = int(desired)
            return False
        if not self._enqueue("strength", {
                "s": slot, "t": 3, "c": index, "p": 1, "v": delta,
        }, priority=0, channel="a" if index == 0 else "b",
                client=client_id, slot=slot):
            return False
        app["in_flight"][index] = True
        app["manual_hold"] = False
        app["sent_baseline"][index] = int(base)
        app["sent_delta"][index] = int(delta)
        app["sending"][index] = "add"
        app["current"][index] = int(desired)
        app["verified"] = False
        app["needs_anchor"][index] = False
        app["verify_until"] = max(
            float(app["verify_until"]), now + self.verify_window_s)
        self.log.debug("V4 下发 %s/%s：目标 %d，确认 %d，差值 %d",
                       client_id, "a" if index == 0 else "b",
                       desired, int(base), delta)
        return True

    def _enqueue_zero_locked(self, app: dict[str, Any], index: int,
                             now: float, wait: bool = True,
                             reset_retries: bool = True) -> bool:
        slot = str(app.get("slot_id") or "")
        client_id = str(app.get("client_id") or "")
        if not slot or not client_id:
            return False
        if wait and app["in_flight"][index] and app["sending"][index] == "zero":
            return False
        if not self._enqueue("zero", {
                "s": slot, "t": 7, "c": index, "p": 1, "v": 0,
        }, priority=0, channel="a" if index == 0 else "b",
                client=client_id, slot=slot):
            return False
        if reset_retries:
            app["zero_retries"][index] = 0
        app["manual_hold"] = False
        app["sending"][index] = "zero" if wait else ""
        app["sent_baseline"][index] = app["reported"][index]
        app["sent_delta"][index] = 0
        app["needs_anchor"][index] = False
        app["current"][index] = 0
        if wait:
            app["in_flight"][index] = True
            app["awaiting_zero"][index] = True
        app["verified"] = False
        app["verify_until"] = max(
            float(app["verify_until"]), now + self.verify_window_s)
        return True

    def _mark_unknown_locked(self, app: dict[str, Any], index: int) -> None:
        was_zero = (app["in_flight"][index]
                    and app["sending"][index] == "zero")
        app["in_flight"][index] = False
        app["confirmed"][index] = None
        app["confirmed_at"][index] = 0.0
        app["manual_hold"] = False
        app["needs_anchor"][index] = False
        if not was_zero and not app["awaiting_zero"][index]:
            app["uncertain"][index] = True

    def _maybe_sync_channel_locked(self, app: dict[str, Any], index: int,
                                   now: float, force: bool = False) -> bool:
        slot = str(app.get("slot_id") or "")
        if not slot:
            return False
        target = self._verification_target(app)
        desired = int(target[index])
        if desired <= 0:
            if app["in_flight"][index] and app["sending"][index] == "zero":
                return False
            if app["in_flight"][index]:
                # 已有增量在途；目标变成 0 时立即在它后面补绝对归零。
                return self._enqueue_zero_locked(
                    app, index, now, wait=True, reset_retries=False)
            if (force or app["uncertain"][index]
                    or app["awaiting_zero"][index]
                    or app["confirmed"][index] != 0
                    or app["reported"][index] != 0):
                if (app["awaiting_zero"][index]
                        and app["confirmed"][index] == 0):
                    app["current"][index] = 0
                    return False
                return self._enqueue_zero_locked(
                    app, index, now, wait=True)
            app["current"][index] = 0
            return False
        if (app["in_flight"][index]
                or app["awaiting_zero"][index]
                or app["needs_anchor"][index]):
            return False
        if app["uncertain"][index]:
            # 状态不可信：先绝对归零，等 0 确认后再恢复目标。
            return self._enqueue_zero_locked(app, index, now, wait=True)
        if not app["controllable"][index]:
            return False
        base = app["confirmed"][index]
        if base is None:
            return False
        base = int(base)
        if base == desired:
            app["current"][index] = desired
            return False
        if base > desired:
            return self._enqueue_add_locked(app, index, desired, now)
        if not self._warm_up_ready(app):
            if (base > 0
                    and self._scaled_match(app, index, desired, base)):
                app["current"][index] = desired
                return False
            if base > 0:
                # 已经有一定输出时，冷启动期间不再继续加，避免抵消爬坡。
                return False
            # 初始输出必须发出去；App 会用自己的 warmUpScale 安全爬坡。
        return self._enqueue_add_locked(app, index, desired, now)

    def _process_probe_locked(self, app: dict[str, Any], now: float,
                              channels: set[int] | None = None) -> bool:
        target = self._verification_target(app)
        changed = False
        if channels is None:
            channels = {0, 1}
        for index in channels:
            reported = app["reported"][index]
            if reported is None:
                self._mark_unknown_locked(app, index)
                changed = True
                self._maybe_sync_channel_locked(app, index, now)
                continue
            app["confirmed"][index] = int(reported)
            app["confirmed_at"][index] = now
            if app["in_flight"][index]:
                baseline = app["sent_baseline"][index]
                kind = app["sending"][index]
                delta = int(app["sent_delta"][index])
                app["in_flight"][index] = False
                if kind == "zero":
                    if int(reported) == 0:
                        app["awaiting_zero"][index] = False
                        app["uncertain"][index] = False
                        app["needs_anchor"][index] = False
                        app["zero_retries"][index] = 0
                        app["current"][index] = 0
                        changed = True
                    else:
                        if int(app["zero_retries"][index]) < 1:
                            app["zero_retries"][index] = int(
                                app["zero_retries"][index]) + 1
                            self._enqueue_zero_locked(
                                app, index, now, wait=True,
                                reset_retries=False)
                        else:
                            app["uncertain"][index] = True
                            app["awaiting_zero"][index] = True
                        changed = True
                        self._maybe_sync_channel_locked(app, index, now)
                        continue
                elif int(reported) == baseline:
                    # 发出增量后上报值没有变化：旧值或增量丢失，
                    # 不能再用同一个值继续补正向增量。
                    app["needs_anchor"][index] = True
                    app["anchor_at"][index] = now + self.anchor_grace_s
                    changed = True
                    self._maybe_sync_channel_locked(app, index, now)
                    continue
                else:
                    app["uncertain"][index] = False
                    app["needs_anchor"][index] = False
                    app["awaiting_zero"][index] = False
                    app["zero_retries"][index] = 0
                    changed = True
            else:
                if app["awaiting_zero"][index]:
                    if int(reported) == 0:
                        app["awaiting_zero"][index] = False
                        app["uncertain"][index] = False
                        app["needs_anchor"][index] = False
                        app["zero_retries"][index] = 0
                        app["current"][index] = 0
                        changed = True
                    else:
                        if int(app["zero_retries"][index]) < 1:
                            app["zero_retries"][index] = int(
                                app["zero_retries"][index]) + 1
                            self._enqueue_zero_locked(
                                app, index, now, wait=True,
                                reset_retries=False)
                        else:
                            app["uncertain"][index] = True
                            app["awaiting_zero"][index] = True
                        changed = True
                        self._maybe_sync_channel_locked(app, index, now)
                        continue
                if app["needs_anchor"][index]:
                    if int(reported) != app["sent_baseline"][index]:
                        app["needs_anchor"][index] = False
                        app["uncertain"][index] = False
                        changed = True
                    else:
                        self._maybe_sync_channel_locked(app, index, now)
                        continue
            self._maybe_sync_channel_locked(app, index, now)
        self._recompute_verified_locked(app, target)
        return changed

    def _expire_verification_locked(self, app: dict[str, Any],
                                    now: float) -> bool:
        if app["verified"]:
            return False
        target = self._verification_target(app)
        if target != (0, 0) and not self._warm_up_ready(app):
            # 冷启动期间上报值可能是缩放后的输出；不回正、不归零，
            # 也不把缩放值当成基线。
            app["verified"] = True
            app["probes"].clear()
            self.log.info(
                "V4 冷启动未结束 %s：暂停校验 A=%s B=%s",
                app["client_id"], *app["reported"])
            return False
        changed = False
        sent_anchor = False
        for index in (0, 1):
            if self._settled_locked(app, index, target):
                continue
            if int(app["zero_retries"][index]) >= 1:
                app["uncertain"][index] = True
                app["in_flight"][index] = False
                app["awaiting_zero"][index] = True
                app["needs_anchor"][index] = False
                changed = True
                continue
            app["zero_retries"][index] = 1
            app["in_flight"][index] = False
            app["needs_anchor"][index] = False
            if self._enqueue_zero_locked(
                    app, index, now, wait=True, reset_retries=False):
                sent_anchor = True
            else:
                app["uncertain"][index] = True
                changed = True
        if sent_anchor:
            app["verify_until"] = now + self.verify_window_s
            app["verified"] = False
            self.log.warning(
                "V4 强度未确认 %s：目标 A=%d B=%d，上报 A=%s B=%s；"
                "先绝对归零，等 0 确认后再恢复",
                app["client_id"], target[0], target[1],
                *app["reported"])
        else:
            app["verified"] = True
            app["safety_latched"] = True
            app["verify_until"] = 0.0
            app["probes"].clear()
            self.log.warning(
                "V4 强度归零未确认 %s：目标 A=%d B=%d，上报 A=%s B=%s；"
                "保持安全状态，等待后续目标变化",
                app["client_id"], target[0], target[1],
                *app["reported"])
        self._recompute_verified_locked(app, target)
        return changed

    def _refresh_slot_locked(self, app: dict[str, Any],
                             slot: dict[str, Any], now: float,
                             mode: str = "live",
                             feedback_channels: set[int] | None = None) -> bool:
        if mode not in ("probe", "snapshot", "live", "stale"):
            mode = "live"
        if mode == "stale":
            return False
        if feedback_channels is None:
            feedback_channels = ({0, 1}
                                 if mode in ("probe", "snapshot")
                                 else set())
        feedback_channels = {index for index in feedback_channels
                             if index in (0, 1)}
        changed = False
        props = slot.get("props") or {}
        state = slot.get("slotState") or {}
        has_device = state.get("hasDevice") is not False
        for channel, key in CHANNEL_INTENSITY_KEY.items():
            index = CHANNEL_INDEX[channel]
            value = props.get(key)
            if isinstance(value, (int, float)):
                app["reported"][index] = clamp_int(value, 0, 200)
                if mode == "probe":
                    app["confirmed"][index] = app["reported"][index]
                    app["confirmed_at"][index] = now
            elif mode in ("probe", "snapshot"):
                # 完整快照缺字段 = 未知，不能沿用上一次值继续补差值。
                app["reported"][index] = None
                app["confirmed"][index] = None
                app["confirmed_at"][index] = 0.0
            channel_state = state.get(CHANNEL_LIMIT_KEY[channel]) or {}
            muted = bool(channel_state.get("isMuted", False))
            app["controllable"][index] = has_device and not muted
        warm_up = self._slot_warm_up(slot)
        for index, value in enumerate(warm_up):
            if value is not None:
                app["warm_up"][index] = value
        limits = self._slot_limits(slot)
        if limits != app["limits"]:
            app["limits"] = limits
            changed = True
            if not self._muted:
                desired = self._desired(app, self._last_pct)
                if desired != app["last_strength"]:
                    self._begin_verification_locked(app, desired, now)
                    for index in (0, 1):
                        self._maybe_sync_channel_locked(app, index, now)
        target = self._verification_target(app)
        feedback_processed = False
        if (mode in ("live", "snapshot") and feedback_channels
                and not app["safety_latched"]
                and int(app["pending_cmds"]) <= 0):
            live_channels: set[int] = set()
            for index in feedback_channels:
                reported = app["reported"][index]
                if app["in_flight"][index]:
                    if reported is None:
                        if mode == "snapshot":
                            live_channels.add(index)
                    elif (app["sending"][index] == "zero"
                          or int(reported) != app["sent_baseline"][index]):
                        # App 推送的变化可以确认在途增量；同一旧值不确认，
                        # 仍交给主动探测/归零锚定。
                        live_channels.add(index)
                elif app["awaiting_zero"][index] or app["needs_anchor"][index]:
                    if reported is not None or mode == "snapshot":
                        live_channels.add(index)
            if live_channels:
                if self._process_probe_locked(app, now, live_channels):
                    changed = True
                feedback_processed = True
        if (not app["verified"] and not app["safety_latched"]
                and now > app["verify_until"]):
            if self._expire_verification_locked(app, now):
                changed = True
        if mode == "probe":
            if (int(app["pending_cmds"]) > 0
                    or now - float(app["last_cmd_sent_at"] or 0.0)
                    < self.feedback_settle_s):
                return changed
            if self._process_probe_locked(app, now):
                changed = True
        else:
            if app["safety_latched"]:
                return changed
            manual_accepted = False
            if feedback_processed:
                self._recompute_verified_locked(app, target)
                return changed
            if (int(app["pending_cmds"]) <= 0
                    and now - float(app["last_cmd_sent_at"] or 0.0)
                    >= self.feedback_settle_s):
                if app["verified"]:
                    accepted = True
                    for index in (0, 1):
                        reported = app["reported"][index]
                        if reported is None:
                            accepted = False
                            continue
                        if target[index] == 0:
                            if int(reported) != 0:
                                app["confirmed"][index] = int(reported)
                                self._maybe_sync_channel_locked(
                                    app, index, now)
                                accepted = False
                            else:
                                app["current"][index] = 0
                                app["confirmed"][index] = 0
                        elif int(reported) <= int(target[index]):
                            # 手动调低是安全方向，接受为新的实际基线。
                            app["current"][index] = int(reported)
                            app["confirmed"][index] = int(reported)
                        else:
                            # 高于目标永远不作为基线，只向下修正。
                            app["confirmed"][index] = int(reported)
                            self._maybe_sync_channel_locked(
                                app, index, now)
                            accepted = False
                    app["manual_hold"] = bool(accepted)
                    if accepted:
                        app["probes"].clear()
                    manual_accepted = accepted
                else:
                    for index in (0, 1):
                        self._maybe_sync_channel_locked(app, index, now)
            if not manual_accepted:
                self._recompute_verified_locked(app, target)
        return changed

    @staticmethod
    def _slot_warm_up(slot: dict[str, Any]) -> list[float | None]:
        state = slot.get("slotState") or {}
        result: list[float | None] = []
        for channel in ("a", "b"):
            channel_state = state.get(CHANNEL_LIMIT_KEY[channel]) or {}
            value = channel_state.get("warmUpScale")
            if isinstance(value, (int, float)):
                result.append(max(0.0, min(1.0, float(value))))
            else:
                result.append(None)
        return result

    @staticmethod
    def _slot_limits(slot: dict[str, Any]) -> list[int]:
        state = slot.get("slotState") or {}
        result = []
        for channel in ("a", "b"):
            channel_state = state.get(CHANNEL_LIMIT_KEY[channel]) or {}
            value = channel_state.get("intensityMax")
            if isinstance(value, (int, float)):
                result.append(clamp_int(value, 0, 200))
            else:
                # 上限未上报时不输出，避免按协议最大值 200 误算。
                result.append(0)
        return result

    async def _select_slot(self, client_id: str, mode: str = "live") -> None:
        old_slot = ""
        new_slot = ""
        same_changed = False
        with self._lock:
            app = self._apps.get(client_id)
            if app is None:
                return
            devices: list[dict[str, Any]] = app["devices"]
            chosen = None
            if self.configured_slot:
                chosen = next(
                    (device for device in devices
                     if str(device.get("slotId")) == self.configured_slot), None)
            if chosen is None:
                coyotes = [
                    device for device in devices
                    if str(device.get("type") or "").startswith("COYOTE_")]
                chosen = next(
                    (device for device in coyotes
                     if (device.get("slotState") or {}).get("hasDevice") is not False),
                    None)
                if chosen is None and coyotes:
                    chosen = coyotes[0]
            if chosen is None:
                chosen = next(
                    (device for device in devices
                     if (device.get("slotState") or {}).get("hasDevice") is not False),
                    None)
            if chosen is None and devices:
                chosen = devices[0]
            new_slot = str(chosen.get("slotId") or "") if chosen else ""
            old_slot = str(app.get("slot_id") or "")
            if new_slot == old_slot:
                if chosen is not None:
                    app["slot"] = chosen
                    same_changed = self._refresh_slot_locked(
                        app, chosen, time.monotonic(), mode=mode)
            else:
                now = time.monotonic()
                app["slot"] = chosen
                app["slot_id"] = new_slot
                app["limits"] = self._slot_limits(chosen) if chosen else [0, 0]
                app["warm_up"] = [1.0, 1.0]
                app["current"] = [0, 0]
                app["reported"] = [None, None]
                app["confirmed"] = [None, None]
                app["confirmed_at"] = [0.0, 0.0]
                app["controllable"] = [True, True]
                app["in_flight"] = [False, False]
                app["sent_baseline"] = [None, None]
                app["sent_delta"] = [0, 0]
                app["sending"] = ["", ""]
                app["uncertain"] = [False, False]
                app["awaiting_zero"] = [False, False]
                app["needs_anchor"] = [False, False]
                app["anchor_at"] = [0.0, 0.0]
                app["zero_retries"] = [0, 0]
                app["manual_hold"] = False
                app["pending_cmds"] = 0
                app["last_cmd_sent_at"] = 0.0
                app["probes"].clear()
                app["cmd_seq"] = 0
                if chosen is not None:
                    props = chosen.get("props") or {}
                    state = chosen.get("slotState") or {}
                    has_device = state.get("hasDevice") is not False
                    for channel, key in CHANNEL_INTENSITY_KEY.items():
                        index = CHANNEL_INDEX[channel]
                        value = props.get(key)
                        if isinstance(value, (int, float)):
                            app["reported"][index] = clamp_int(value, 0, 200)
                            app["confirmed"][index] = app["reported"][index]
                        channel_state = state.get(
                            CHANNEL_LIMIT_KEY[channel]) or {}
                        app["controllable"][index] = (
                            has_device
                            and not bool(channel_state.get("isMuted", False)))
                    for index, value in enumerate(self._slot_warm_up(chosen)):
                        if value is not None:
                            app["warm_up"][index] = value
                for channel in ("a", "b"):
                    self._bump_pulse_gen(client_id, channel)
                if new_slot:
                    self._begin_verification_locked(app, (0, 0), now)
                    # confirmed==0 时归零不会阻塞后续目标；否则先等 0 确认。
                    for index in (0, 1):
                        wait = app["confirmed"][index] != 0
                        self._enqueue_zero_locked(
                            app, index, now, wait=wait)
                    self._recompute_verified_locked(app, (0, 0))
                else:
                    app["last_strength"] = (0, 0)
                    app["last_strength_at"] = now
                    app["verified"] = True
                    app["verify_until"] = 0.0
        if new_slot == old_slot:
            if same_changed:
                self._notify()
            return
        if old_slot:
            await self._send_rpc(
                client_id, "device.op.clear", {"s": old_slot})
            # 切换到别的设备后旧设备不再被控制器管理；clear 只清波形，
            # 强度必须再显式归零，避免旧设备残留输出。
            for channel in ("a", "b"):
                await self._send_rpc(client_id, "device.op", {
                    "s": old_slot, "t": 7,
                    "c": CHANNEL_INDEX[channel], "p": 1, "v": 0,
                })
            self.log.info("V4 设备切换：%s → %s", old_slot, new_slot or "(无)")
        if new_slot and chosen is not None:
            with self._lock:
                app = self._apps.get(client_id)
                if app is not None and self._loops:
                    for channel, loop in self._loops.items():
                        units = self._repeat(
                            loop.units, loop.offset, loop.chunk_frames)
                        gen = self._bump_pulse_gen(client_id, channel)
                        self._enqueue_pulse_for_app_locked(
                            app, channel, units, gen, replace=True)
            self.log.info("V4 使用设备：%s（%s）", new_slot,
                          (chosen.get("name") or chosen.get("type") or "?"))
            # 设备刚归零；让引擎下一次 tick 重新下发当前强度
            # （引擎会对相同百分比去重，不通知的话会停在 0）。
            self._notify_connected()
        self._notify()

    # ---------------------------------------------------------------- 下发
    def _req_id(self) -> str:
        return "wows-{}-{}".format(self._session_token, next(self._req_seq))

    async def _send_rpc(self, client_id: str, method: str,
                        data: Any = None) -> None:
        payload: dict[str, Any] = {
            "t": "req", "reqId": self._req_id(), "m": method}
        if data is not None:
            payload["data"] = data
        await self._send_message(client_id, payload)

    async def _send_message(self, client_id: str, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        await self._send_raw(ws, {
            "type": "message", "clientId": client_id, "data": payload,
        })

    async def _send_raw(self, ws: Any, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self.sent.append(text)
        if len(self.sent) > 500:
            del self.sent[:250]
        await ws.send(text)

    def _enqueue(self, kind: str, payload: dict[str, Any], priority: int,
                 channel: str | None = None, gen: int | None = None,
                 client: str | None = None, slot: str = "") -> bool:
        with self._lock:
            app = self._apps.get(str(client)) if client else None
            strength = kind in ("strength", "zero")
            if strength and app is None:
                return False
            if strength:
                app["pending_cmds"] = int(app["pending_cmds"]) + 1
            try:
                self._outbox.put_nowait((priority, next(self._seq), {
                    "kind": kind,
                    "payload": payload,
                    "channel": channel,
                    "gen": gen,
                    "client": client,
                    "slot": slot,
                }))
            except queue.Full:
                if strength:
                    app["pending_cmds"] = max(
                        0, int(app["pending_cmds"]) - 1)
                self.log.warning("V4 发送队列已满，丢弃：%s", kind)
                return False
            if strength:
                # 新指令发出后，之前发出的探测结果都相对新目标过期。
                app["cmd_seq"] = int(app["cmd_seq"]) + 1
                app["probes"].clear()
            return True

    def _mark_strength_sent(self, item: dict[str, Any]) -> None:
        with self._lock:
            app = self._apps.get(str(item.get("client") or ""))
            if app is None or item.get("slot") != app.get("slot_id"):
                return
            app["pending_cmds"] = max(0, int(app["pending_cmds"]) - 1)
            app["last_cmd_sent_at"] = time.monotonic()

    def _drain_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    def _item_valid(self, item: dict[str, Any]) -> bool:
        with self._lock:
            client_id = item.get("client")
            app = self._apps.get(str(client_id)) if client_id else None
            if app is None:
                return False
            kind = item.get("kind")
            if kind == "pulse":
                return (item.get("gen") == self._pulse_gen.get(
                    (str(client_id), str(item.get("channel"))))
                        and item.get("slot") == app.get("slot_id"))
            if kind in ("strength", "zero", "sync"):
                return item.get("slot") == app.get("slot_id")
            return True

    async def _sender_loop(self) -> None:
        while True:
            try:
                _priority, _seq, item = await asyncio.to_thread(
                    self._outbox.get, True, 0.5)
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.debug("V4 sender 取队列失败：%s", exc)
                continue
            if not self._item_valid(item):
                continue
            ws = self._ws
            if ws is None:
                continue
            kind = str(item.get("kind") or "")
            if kind == "raw":
                request = item["payload"]
            elif kind == "clear":
                request = {
                    "t": "req", "reqId": self._req_id(),
                    "m": "device.op.clear", "data": item["payload"],
                }
            elif kind == "sync":
                request = {
                    "t": "req", "reqId": item["payload"]["reqId"],
                    "m": "devices.get",
                }
            else:
                request = {
                    "t": "req", "reqId": self._req_id(),
                    "m": "device.op", "data": item["payload"],
                }
            try:
                await self._send_message(item["client"], request)
            except ConnectionClosed:
                # Relay 断开时 sender 必须继续存活；重连后还要继续发送。
                continue
            except Exception as exc:
                self.log.debug("V4 发送失败：%s", exc)
            finally:
                if kind in ("strength", "zero"):
                    self._mark_strength_sent(item)

    # ---------------------------------------------------------------- 强度
    def _desired(self, app: dict[str, Any], pct: tuple[int, int]) -> tuple[int, int]:
        limits = app["limits"]
        result = []
        for index in range(2):
            pct_value = 0 if self._muted else pct[index]
            limit = int(limits[index])
            raw = min(limit,
                      int(round(limit * max(0, min(100, pct_value)) / 100.0)))
            result.append(max(0, raw))
        return result[0], result[1]

    def set_strength(self, a: int, b: int, force: bool = False) -> None:
        pct = (clamp_int(a, 0, 100), clamp_int(b, 0, 100))
        now = time.monotonic()
        changed = False
        with self._lock:
            self._last_pct = pct
            for app in self._apps.values():
                if not app.get("slot_id"):
                    continue
                desired = self._desired(app, pct)
                if desired == app["last_strength"] and not force:
                    continue
                self._begin_verification_locked(app, desired, now)
                for index in (0, 1):
                    self._maybe_sync_channel_locked(
                        app, index, now, force=force)
                self._last_strength = desired
                changed = True
        if changed:
            self.log.debug("V4 set_strength %s", pct)

    # ---------------------------------------------------------------- 波形
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

        with self._lock:
            if not loop:
                self._loops.pop(channel, None)
                self._enqueue_pulse_all_locked(
                    channel, self._repeat(units, 0, frames_count),
                    replace=True)
                self.log.info("play %s %s template=%s loop=False frames=%d",
                              owner, channel, template.id, frames_count)
                return
            old = self._loops.get(channel)
            if append and old is not None:
                chunk = self._repeat(units, old.offset, chunk_frames)
                self._enqueue_pulse_all_locked(
                    channel, chunk, replace=False)
                old.offset = (old.offset + chunk_frames) % len(units)
                old.sent_frames += chunk_frames
                return
            # 与 V3 相同的初始长度：至少一个 period，短事件至少覆盖 duration。
            initial = max(chunk_frames, frames_count)
            self._enqueue_pulse_all_locked(
                channel, self._repeat(units, 0, initial), replace=True)
            self._loops[channel] = LoopState(
                owner=owner,
                channel=channel,
                template=template,
                units=units,
                duration_s=duration_s,
                period_s=period_s,
                chunk_frames=chunk_frames,
                offset=initial % len(units),
                sent_frames=initial,
            )
        self.log.info("play %s %s template=%s loop=True initial=%d",
                      owner, channel, template.id, initial)

    def _bump_pulse_gen(self, client_id: str, channel: str) -> int:
        key = (client_id, channel)
        value = int(self._pulse_gen.get(key, 0)) + 1
        self._pulse_gen[key] = value
        return value

    def _pulse_gen_for(self, client_id: str, channel: str) -> int:
        return int(self._pulse_gen.get((client_id, channel), 0))

    def _enqueue_pulse_all_locked(self, channel: str, units: list[str],
                                  replace: bool) -> None:
        for app in self._apps.values():
            client_id = str(app.get("client_id") or "")
            if not app.get("slot_id") or not client_id:
                continue
            gen = (self._bump_pulse_gen(client_id, channel)
                   if replace else self._pulse_gen_for(client_id, channel))
            self._enqueue_pulse_for_app_locked(
                app, channel, units, gen, replace)

    def _enqueue_pulse_for_app_locked(self, app: dict[str, Any], channel: str,
                                      units: list[str], gen: int,
                                      replace: bool) -> None:
        slot = str(app.get("slot_id") or "")
        client_id = str(app.get("client_id") or "")
        if not slot or not client_id or not units:
            return
        index = CHANNEL_INDEX[channel]
        for start in range(0, len(units), MAX_CHUNK_FRAMES):
            chunk = units[start:start + MAX_CHUNK_FRAMES]
            payload: dict[str, Any] = {
                "s": slot, "t": 0, "c": index, "p": 1,
                "d": len(chunk) * 100, "v": chunk,
            }
            if replace and start == 0:
                payload["im"] = True
            self._enqueue("pulse", payload, priority=3, channel=channel,
                          gen=gen, client=client_id, slot=slot)

    def pump(self, now: float) -> None:
        if self._muted:
            return
        with self._lock:
            # 与 V3 一致：即使暂无 App 也推进 loop 的 offset 和补帧节奏；
            # 没有 App 时 _enqueue_pulse_all_locked 不会产生任何下发。
            for channel, loop in list(self._loops.items()):
                if loop.next_refill <= 0:
                    loop.next_refill = now + loop.period_s
                    continue
                if now < loop.next_refill:
                    continue
                chunk = self._repeat(loop.units, loop.offset, loop.chunk_frames)
                self._enqueue_pulse_all_locked(
                    channel, chunk, replace=False)
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
            for app in self._apps.values():
                slot = str(app.get("slot_id") or "")
                client_id = str(app.get("client_id") or "")
                if not slot or not client_id:
                    continue
                self._bump_pulse_gen(client_id, channel)
                self._enqueue("clear", {
                    "s": slot, "c": CHANNEL_INDEX[channel],
                }, priority=0, channel=channel,
                    client=client_id, slot=slot)

    def stop_all(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._loops.clear()
            for app in self._apps.values():
                app["current"] = [0, 0]
                app["last_strength_at"] = now
                slot = str(app.get("slot_id") or "")
                client_id = str(app.get("client_id") or "")
                if not client_id:
                    continue
                if slot:
                    self._begin_verification_locked(app, (0, 0), now)
                    for channel in ("a", "b"):
                        self._bump_pulse_gen(client_id, channel)
                        self._enqueue("clear", {
                            "s": slot, "c": CHANNEL_INDEX[channel],
                        }, priority=0, channel=channel,
                            client=client_id, slot=slot)
                    for index in (0, 1):
                        app["in_flight"][index] = False
                        app["needs_anchor"][index] = False
                        app["uncertain"][index] = False
                        self._enqueue_zero_locked(
                            app, index, now, wait=True)
                else:
                    app["last_strength"] = (0, 0)
                    app["verified"] = True
                    app["verify_until"] = 0.0
                    app["probes"].clear()
                    app["in_flight"] = [False, False]
                    app["uncertain"] = [False, False]
                    app["awaiting_zero"] = [False, False]
                    app["needs_anchor"] = [False, False]
                    self._enqueue("raw", {
                        "t": "req", "reqId": self._req_id(),
                        "m": "device.op.clear",
                    }, priority=0, client=client_id)
            self._last_strength = (0, 0)

    def emergency_stop(self, reason: str = "急停") -> None:
        self._muted = True
        self.stop_all()
        self.log.warning("V4 急停：%s", reason)
        callback = self.on_emergency
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
        self._notify()

    def rearm(self) -> None:
        self._muted = False
        self.log.info("V4 已重新武装")
        self._notify()

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _repeat(units: list[str], offset: int, count: int) -> list[str]:
        return [units[(offset + index) % len(units)] for index in range(count)]

    def _write_qr(self) -> None:
        payload = self.qr_payload
        if not payload:
            return
        try:
            import qrcode
            import qrcode.image.svg
            image = qrcode.make(
                payload, image_factory=qrcode.image.svg.SvgPathImage)
            directory = os.path.dirname(self.qr_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            image.save(self.qr_path)
            self.log.info("V4 二维码已写入：%s", self.qr_path)
        except Exception as exc:
            self.log.warning("V4 二维码生成失败：%s", exc)

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
