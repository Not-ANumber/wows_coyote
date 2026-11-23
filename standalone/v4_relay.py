# -*- coding: utf-8 -*-
"""V4 本地中继（Relay）：兼容 dglab-kit 的 V4 服务器帧协议。

拓扑（与官方 Relay 一致）：
  控制方  ws://host:port/v4           → hello{clientId}
  App     ws://host:port/v4?tid=<id>  → hello{clientId} + controller_attached

转发：
  控制方 → 中继 {"type":"message","clientId":<appId>,"data":{...}}
  中继 → App   {"type":"message","clientId":<appId>,"data":{...}}
  App   → 中继 {"type":"message","data":{...}}
  中继 → 控制方 {"type":"message","clientId":<appId>,"data":{...}}

服务器帧：hello / client_attached / client_disconnected /
controller_attached / controller_disconnected / heartbeat / ping-pong /
idle_timeout / error。关闭码：4000 controller_disconnected、
4001 controller_not_found、4002 idle_timeout。

本实现只做单机局域网转发，无鉴权、无持久化；与官方 Relay 的差异见
docs/standalone_V4.md。idle_timeout_s=0 表示不启用空闲超时（默认），
这样本地配对二维码不会因为长时间未接入而失效。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:  # pragma: no cover - 与 v3_device 相同的依赖提示
    raise SystemExit(
        "V4 中继需要 websockets >= 13；请先运行：\n"
        "  py -3.13 -m pip install --target .phase0deps websockets qrcode"
    ) from exc

LOG = logging.getLogger("standalone.v4.relay")

HEARTBEAT_S = 30.0


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def _request_path(ws: Any) -> str:
    request = getattr(ws, "request", None)
    if request is not None:
        path = getattr(request, "path", "") or ""
        if path:
            return path
    return getattr(ws, "path", "") or ""


def _frame(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


@dataclass
class _App:
    client_id: str
    ws: ServerConnection
    controller_id: str


@dataclass
class _Controller:
    client_id: str
    ws: ServerConnection
    apps: dict[str, _App] = field(default_factory=dict)
    idle_task: asyncio.Task[None] | None = None


class LocalV4Relay:
    """单进程 V4 中继；在调用方的 asyncio loop 里运行。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 9998,
                 idle_timeout_s: float = 0.0,
                 heartbeat_s: float = HEARTBEAT_S,
                 logger: logging.Logger | None = None) -> None:
        self.host = host
        self.port = int(port)
        self.idle_timeout_s = max(0.0, float(idle_timeout_s))
        self.heartbeat_s = max(1.0, float(heartbeat_s))
        self.log = logger or LOG
        self._server = None
        self._controllers: dict[str, _Controller] = {}
        self._apps: dict[str, _App] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def controller_count(self) -> int:
        return len(self._controllers)

    @property
    def app_count(self) -> int:
        return len(self._apps)

    async def start(self) -> int:
        if self._started:
            return self.port
        self._server = await serve(
            self._handle, self.host, self.port,
            ping_interval=20, ping_timeout=20,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        self._started = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.log.info("V4 中继已启动：ws://%s:%d/v4",
                      self.host if self.host != "0.0.0.0" else lan_ip(),
                      self.port)
        return self.port

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for controller in list(self._controllers.values()):
            self._cancel_idle(controller)
        self._controllers.clear()
        self._apps.clear()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        self.log.info("V4 中继已停止")

    # ---------------------------------------------------------------- 路由
    async def _handle(self, ws: ServerConnection) -> None:
        query = parse_qs(urlparse(_request_path(ws)).query)
        tid = str((query.get("tid") or [""])[0]).strip()
        if tid:
            await self._app_session(ws, tid)
        else:
            await self._controller_session(ws)

    async def _controller_session(self, ws: ServerConnection) -> None:
        client_id = str(uuid.uuid4())
        controller = _Controller(client_id, ws)
        self._controllers[client_id] = controller
        self.log.info("控制方已接入：%s", client_id)
        try:
            await self._send(ws, {"type": "hello", "clientId": client_id})
            self._restart_idle(controller)
            async for raw in ws:
                await self._on_controller_message(controller, raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.log.warning("控制方连接异常：%s", exc)
        finally:
            self._cancel_idle(controller)
            self._controllers.pop(client_id, None)
            for app in list(controller.apps.values()):
                self._apps.pop(app.client_id, None)
                await self._send(app.ws, {
                    "type": "controller_disconnected",
                    "clientId": client_id,
                })
                try:
                    await app.ws.close(code=4000, reason="controller_disconnected")
                except Exception:
                    pass
            controller.apps.clear()
            self.log.info("控制方已断开：%s", client_id)

    async def _on_controller_message(self, controller: _Controller,
                                     raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except Exception:
            return
        if not isinstance(frame, dict):
            return
        kind = str(frame.get("type") or "")
        if kind == "ping":
            await self._send(controller.ws, {
                "type": "pong", "ts": int(time.time() * 1000)})
            return
        if kind != "message":
            return
        target = str(frame.get("clientId") or "")
        app = controller.apps.get(target)
        if app is None:
            await self._send(controller.ws, {
                "type": "error", "code": "invalid_params",
                "message": "client_not_found",
            })
            return
        await self._send(app.ws, {
            "type": "message", "clientId": app.client_id,
            "data": frame.get("data"),
        })

    async def _app_session(self, ws: ServerConnection, tid: str) -> None:
        controller = self._controllers.get(tid)
        if controller is None:
            self.log.warning("App 使用了未知 targetId：%s", tid)
            await self._send(ws, {
                "type": "error", "code": "controller_not_found",
                "message": "controller_not_found",
            })
            try:
                await ws.close(code=4001, reason="controller_not_found")
            except Exception:
                pass
            return

        client_id = str(uuid.uuid4())
        app = _App(client_id, ws, tid)
        controller.apps[client_id] = app
        self._apps[client_id] = app
        self._cancel_idle(controller)
        self.log.info("App 已接入：%s（控制方 %s）", client_id, tid)
        try:
            await self._send(ws, {"type": "hello", "clientId": client_id})
            await self._send(ws, {
                "type": "controller_attached", "clientId": tid})
            await self._send(controller.ws, {
                "type": "client_attached", "clientId": client_id})
            async for raw in ws:
                await self._on_app_message(app, raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.log.warning("App 连接异常：%s", exc)
        finally:
            controller.apps.pop(client_id, None)
            self._apps.pop(client_id, None)
            await self._send(controller.ws, {
                "type": "client_disconnected", "clientId": client_id})
            if (not controller.apps
                    and self._controllers.get(controller.client_id) is controller):
                self._restart_idle(controller)
            self.log.info("App 已断开：%s", client_id)

    async def _on_app_message(self, app: _App, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except Exception:
            return
        if not isinstance(frame, dict):
            return
        kind = str(frame.get("type") or "")
        if kind == "ping":
            await self._send(app.ws, {
                "type": "pong", "ts": int(time.time() * 1000)})
            return
        if kind != "message":
            return
        controller = self._controllers.get(app.controller_id)
        if controller is None:
            return
        await self._send(controller.ws, {
            "type": "message", "clientId": app.client_id,
            "data": frame.get("data"),
        })

    # ---------------------------------------------------------------- 空闲
    def _restart_idle(self, controller: _Controller) -> None:
        self._cancel_idle(controller)
        if self.idle_timeout_s <= 0:
            return
        controller.idle_task = asyncio.create_task(self._idle_loop(controller))

    def _cancel_idle(self, controller: _Controller) -> None:
        task, controller.idle_task = controller.idle_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _idle_loop(self, controller: _Controller) -> None:
        try:
            await asyncio.sleep(self.idle_timeout_s)
            if controller.apps:
                return
            self.log.info("控制方空闲超时：%s", controller.client_id)
            await self._send(controller.ws, {"type": "idle_timeout"})
            await controller.ws.close(code=4002, reason="idle_timeout")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ---------------------------------------------------------------- 心跳
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            for controller in list(self._controllers.values()):
                await self._send(controller.ws, {"type": "heartbeat"})
            for app in list(self._apps.values()):
                await self._send(app.ws, {"type": "heartbeat"})

    @staticmethod
    async def _send(ws: ServerConnection, payload: dict[str, Any]) -> None:
        try:
            await ws.send(_frame(payload))
        except Exception:
            pass
