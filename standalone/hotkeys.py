# -*- coding: utf-8 -*-
"""Windows 全局急停热键。

使用 GetAsyncKeyState 轮询，不需要管理员权限；非 Windows 平台只记警告，
UI/overlay/App feedback-0 的急停仍然可用。
"""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
import time
from typing import Callable

LOG = logging.getLogger("standalone.hotkeys")

VK_CODES = {
    "ESC": 0x1B,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "PAUSE": 0x13,
    "HOME": 0x24, "END": 0x23,
}


class HotkeyWatcher(threading.Thread):
    def __init__(self, bindings: dict[str, Callable[[], None]],
                 poll_s: float = 0.05) -> None:
        super().__init__(name="wows-hotkeys", daemon=True)
        self.bindings = {
            str(name).upper(): callback
            for name, callback in bindings.items()
        }
        self.poll_s = max(0.02, float(poll_s))
        self._stop_event = threading.Event()
        self._down: dict[int, bool] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if platform.system() != "Windows":
            LOG.warning("非 Windows 平台：全局急停热键不可用，请用 UI/overlay/App 急停")
            return
        try:
            user32 = ctypes.windll.user32
        except Exception as exc:
            LOG.warning("无法访问 user32，全局急停热键不可用：%s", exc)
            return

        vks: dict[int, str] = {}
        for name in self.bindings:
            vk = VK_CODES.get(name)
            if vk is None:
                LOG.warning("不认识的急停键：%s", name)
                continue
            vks[vk] = name

        while not self._stop_event.is_set():
            for vk, name in vks.items():
                try:
                    pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                except Exception:
                    continue
                if pressed and not self._down.get(vk):
                    self._down[vk] = True
                    try:
                        self.bindings[name]()
                    except Exception as exc:
                        LOG.error("热键 %s 回调异常：%s", name, exc)
                elif not pressed:
                    self._down[vk] = False
            time.sleep(self.poll_s)
