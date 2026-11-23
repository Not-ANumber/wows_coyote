# -*- coding: utf-8 -*-
"""可选：用 pywebview 打开独立的置顶 overlay 小窗。

需要额外安装：
  py -3.13 -m pip install pywebview

用法（通常由 run.py --overlay 自动拉起）：
  py -3.13 standalone/overlay.py --url http://127.0.0.1:8800/overlay
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from standalone.desktop import (
    apply_native_window_opacity, apply_window_click_through,
    shape_overlay_window,
)


class OverlayApi:
    """独立 overlay 进程的最小 JS API（关闭按钮/设置入口）。"""

    def __init__(self, settings_url: str = "") -> None:
        self._window: Any = None
        self._settings_url = settings_url

    def close_window(self) -> bool:
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        return True

    def open_settings(self) -> bool:
        if not self._settings_url:
            return False
        webbrowser.open(self._settings_url)
        return True

    def set_overlay_opacity(self, percent: float) -> bool:
        if self._window is None:
            return False
        try:
            value = int(round(float(percent)))
        except Exception:
            return False
        if value < 30 or value > 100:
            return False
        threading.Thread(
            target=apply_native_window_opacity,
            args=(self._window, value),
            name="wows-overlay-opacity",
            daemon=True,
        ).start()
        return True

    def set_overlay_locked(self, locked: bool) -> bool:
        """锁定后窗口不接收鼠标点击（穿透到游戏）。"""
        if self._window is None:
            return False
        if not isinstance(locked, bool):
            locked = str(locked or "").strip().lower() in (
                "1", "true", "yes", "on")
        threading.Thread(
            target=apply_window_click_through,
            args=(self._window, locked),
            name="wows-overlay-lock",
            daemon=True,
        ).start()
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wows_coyote overlay 小窗")
    parser.add_argument("--url", required=True, help="overlay 页面 URL")
    parser.add_argument("--width", type=int, default=400)
    parser.add_argument("--height", type=int, default=240)
    args = parser.parse_args(argv)

    try:
        import webview  # type: ignore
    except ImportError:
        print("需要 pywebview 才能打开独立置顶小窗：")
        print("  py -3.13 -m pip install pywebview")
        print("也可以直接在浏览器打开：{}".format(args.url))
        return 1

    settings_url = ""
    if "/overlay" in args.url:
        settings_url = args.url.split("/overlay", 1)[0] + "/settings"

    api = OverlayApi(settings_url)
    window = webview.create_window(
        "wows_coyote overlay",
        args.url,
        width=max(320, args.width),
        height=max(120, args.height),
        frameless=True,
        on_top=True,
        transparent=True,
        resizable=False,
        shadow=False,
        background_color="#000000",
        js_api=api,
    )
    if window is not None:
        api._window = window
        window.events.loaded += lambda: shape_overlay_window(window)
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
