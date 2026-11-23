# -*- coding: utf-8 -*-
"""pywebview 主窗口：主控制台、设置标签页、悬浮窗。

关闭语义：
  - 主窗口关闭：触发 on_shutdown（归零 + 标记退出），并关闭悬浮窗；
  - 主窗口内的控制台/设置标签切换：不归零；
  - 悬浮窗关闭：只关闭悬浮窗，不归零，主程序继续。

主窗口关闭不做二次确认：这是安全设备的默认动作，先归零，再退出。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from typing import Any, Callable

LOG = logging.getLogger("standalone.desktop")

MAIN_SIZE = (820, 820)
OVERLAY_SIZE = (400, 240)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def available() -> bool:
    """pywebview 是否已安装（不代表 WebView2 一定可用）。"""
    try:
        return importlib.util.find_spec("webview") is not None
    except Exception:
        return False


def apply_native_title_theme(window: Any, theme: str = "dark") -> None:
    """Windows: 标题栏/边框跟随亮色或暗色主题。"""
    if os.name != "nt":
        return
    light = str(theme or "").strip().lower() == "light"
    try:
        native = getattr(window, "native", None)
        for _ in range(20):
            if native is not None:
                break
            time.sleep(0.05)
            native = getattr(window, "native", None)
        if native is None:
            return
        handle = native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)
        import ctypes
        from ctypes import wintypes
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dark = ctypes.c_int(0 if light else 1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), attr,
                    ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break
        # Windows 11: 边框/标题栏/文字颜色（COLORREF = 0x00BBGGRR）
        colors = (
            (34, 0x00BACFD8),  # DWMWA_BORDER_COLOR #d8cfba
            (35, 0x00E9F3F7),  # DWMWA_CAPTION_COLOR #f7f3e9
            (36, 0x00161B1C),  # DWMWA_TEXT_COLOR #1c1b16
        ) if light else (
            (34, 0x001C1C1C),  # DWMWA_BORDER_COLOR #1c1c1c
            (35, 0x00070A0A),  # DWMWA_CAPTION_COLOR #0a0a07
            (36, 0x00E4EFF2),  # DWMWA_TEXT_COLOR #f2efe4
        )
        for attr, color in colors:
            value = ctypes.c_uint(color)
            dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr,
                ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        LOG.debug("主窗口标题栏主题设置失败", exc_info=True)


def apply_native_window_opacity(window: Any, percent: int) -> None:
    """Windows: 整个悬浮窗按百分比半透明（圆角区域保持不变）。"""
    if os.name != "nt":
        return
    try:
        value = max(30, min(100, int(round(float(percent)))))
        alpha = int(round(255 * value / 100.0))
        native = getattr(window, "native", None)
        for _ in range(20):
            if native is not None:
                break
            time.sleep(0.05)
            native = getattr(window, "native", None)
        if native is None:
            return

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [
            wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
        user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND

        ws_ex_layered = 0x80000
        lwa_alpha = 0x2
        handle = native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)

        def apply(target: Any) -> None:
            style = user32.GetWindowLongW(target, -20)
            if not style & ws_ex_layered:
                user32.SetWindowLongW(
                    target, -20, style | ws_ex_layered)
            user32.SetLayeredWindowAttributes(
                target, 0, alpha, lwa_alpha)

        # 只作用于顶层窗口：子 WebView2 会被父窗口一起合成；
        # 父子都设 alpha 会变成平方后更透明。
        apply(hwnd)
    except Exception:
        LOG.debug("悬浮窗透明度设置失败", exc_info=True)


WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20


def apply_window_click_through(window: Any, locked: bool = True) -> None:
    """Windows: 锁定悬浮窗后让鼠标点击穿透到下层窗口（游戏）。

    WS_EX_LAYERED + WS_EX_TRANSPARENT 让窗口及其子 WebView2 都不参与命中测试；
    解锁时只清掉透明/不激活位，保留已有透明度和置顶属性。
    """
    if os.name != "nt":
        return
    try:
        native = getattr(window, "native", None)
        for _ in range(20):
            if native is not None:
                break
            time.sleep(0.05)
            native = getattr(window, "native", None)
        if native is None:
            return

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [
            wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long

        handle = native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if locked:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        LOG.debug("悬浮窗鼠标穿透设置失败", exc_info=True)


def shape_overlay_window(window: Any, radius_css: int = 14) -> None:
    """Windows: 把悬浮窗裁成圆角卡片，避免透明窗口露出矩形底。

    WebView2/WinForms 的 transparent 在 Windows 上不可靠（会露白/黑底）。
    这里让卡片铺满窗口，再用窗口区域裁出圆角；视觉上只剩卡片，没有背景框。
    """
    if os.name != "nt":
        return
    try:
        native = getattr(window, "native", None)
        for _ in range(20):
            if native is not None:
                break
            time.sleep(0.1)
            native = getattr(window, "native", None)
        if native is None:
            return

        # 非客户区也填黑，避免圆角裁剪后露出系统白色边。
        try:
            import clr
            clr.AddReference("System.Drawing")
            from System.Drawing import Color
            native.BackColor = Color.Black
        except Exception:
            LOG.debug("设置悬浮窗底色失败", exc_info=True)

        import ctypes
        from ctypes import wintypes
        handle = native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        try:
            dpi = int(user32.GetDpiForWindow(hwnd)) or 96
        except Exception:
            dpi = 96
        radius = max(6, int(round(radius_css * dpi / 96.0)))
        region = gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if region:
            user32.SetWindowRgn(hwnd, region, True)
    except Exception:
        LOG.debug("悬浮窗圆角裁剪失败", exc_info=True)


class WindowApi:
    """暴露给页面 JS 的最小 API；每个窗口一个实例。"""

    def __init__(self, shell: "DesktopShell") -> None:
        self._shell = shell
        self._window: Any = None

    def open_settings(self) -> bool:
        return self._shell.open_settings()

    def open_events(self) -> bool:
        return self._shell.open_events()

    def open_overlay(self) -> bool:
        return self._shell.open_overlay()

    def set_theme(self, theme: str) -> bool:
        return self._shell.set_theme(theme)

    def set_overlay_opacity(self, percent: float) -> bool:
        return self._shell.set_overlay_opacity(percent)

    def set_overlay_locked(self, locked: bool) -> bool:
        return self._shell.set_overlay_locked(locked)

    def close_window(self) -> bool:
        return self._shell.close_window(self._window)


class DesktopShell:
    def __init__(
        self,
        ui_url: str,
        on_shutdown: Callable[[str], None],
        open_overlay: bool = False,
        debug: bool = False,
        theme: str = "dark",
    ) -> None:
        self.ui_url = ui_url.rstrip("/")
        self.on_shutdown = on_shutdown
        self.open_overlay_at_start = bool(open_overlay)
        self.debug = bool(debug)
        value = str(theme or "").strip().lower()
        self.theme = value if value in ("dark", "light") else "dark"
        self.main: Any = None
        self.overlay: Any = None
        self._webview: Any = None
        self._lock = threading.RLock()
        self._shutdown_requested = False
        self._close_main = True
        self._main_closing = False
        self._main_loaded = False

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown_requested

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> None:
        """创建主窗口并进入 GUI 循环；关闭主窗口后返回。"""
        import webview

        self._webview = webview
        self._create_main(webview)
        if self.open_overlay_at_start:
            self.open_overlay()
        try:
            webview.start(debug=self.debug)
        except Exception:
            if self._shutdown_requested or self._main_loaded:
                # 页面已经加载过；退出阶段的库异常不应再当成启动失败弹窗。
                LOG.debug("主窗口退出时异常（已忽略）", exc_info=True)
                return
            raise
        else:
            if not self._shutdown_requested:
                self.request_shutdown("主窗口已退出", close_main=False)

    def request_shutdown(self, reason: str,
                         close_main: bool = True) -> None:
        """归零并关闭窗口；可被主窗口关闭/退出动作重复调用。

        close_main=False 用于“用户正在关主窗口”：只清理子窗口，
        不再对正在关闭的主窗口调用 destroy()，避免 pywebview 重复销毁报错。
        """
        with self._lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
            self._close_main = bool(close_main)
        try:
            self.on_shutdown(reason)
        except Exception:
            LOG.exception("关闭时归零失败")
        LOG.info("主窗口关闭：%s", reason)
        threading.Thread(
            target=self._destroy_windows,
            name="wows-desktop-close",
            daemon=True,
        ).start()

    def _destroy_windows(self) -> None:
        with self._lock:
            windows = [self.overlay] if self.overlay is not None else []
            if self._close_main and self.main is not None:
                windows.append(self.main)
            self.overlay = None
            if self._close_main:
                self.main = None
        for window in windows:
            try:
                window.destroy()
            except Exception:
                LOG.debug("关闭窗口失败", exc_info=True)

    def _on_main_closing(self) -> bool:
        with self._lock:
            if self._main_closing:
                return True
            self._main_closing = True
        self.request_shutdown("主窗口关闭", close_main=False)
        return True

    def _on_main_closed(self) -> None:
        with self._lock:
            self.main = None
        if not self._shutdown_requested:
            self.request_shutdown("主窗口已关闭", close_main=False)

    def _on_child_closed(self, name: str) -> None:
        with self._lock:
            if name == "overlay":
                self.overlay = None

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------
    def _create_main(self, webview: Any) -> None:
        api = WindowApi(self)
        window = webview.create_window(
            "wows_coyote",
            self.ui_url + "/?app=1",
            width=MAIN_SIZE[0],
            height=MAIN_SIZE[1],
            min_size=(620, 520),
            background_color=(
                "#f3eee2" if self.theme == "light" else "#070706"),
            text_select=True,
            js_api=api,
        )
        if window is None:
            raise RuntimeError("主窗口初始化被取消")
        api._window = window
        with self._lock:
            self.main = window
        window.events.closing += self._on_main_closing
        window.events.closed += self._on_main_closed
        window.events.loaded += self._on_main_loaded
        window.events.before_show += lambda: apply_native_title_theme(
            window, self.theme)

    def _on_main_loaded(self) -> None:
        self._main_loaded = True

    def open_settings(self) -> bool:
        """在主窗口内切到设置标签页（不再另开设置窗口）。"""
        return self._open_tab("/settings?app=1", "设置")

    def open_events(self) -> bool:
        """在主窗口内切到事件标签页。"""
        return self._open_tab("/events?app=1", "事件")

    def _open_tab(self, path: str, label: str) -> bool:
        with self._lock:
            if self._shutdown_requested:
                return False
            window = self.main
        if window is None:
            return False
        self._focus(window)
        # 延后导航，让 pywebview 的 JS API Promise 先返回，避免回调失效报错。
        threading.Thread(
            target=self._load_page,
            args=(window, path, label),
            name="wows-tab-{}".format(label),
            daemon=True,
        ).start()
        return True

    def _load_page(self, window: Any, path: str, label: str) -> None:
        time.sleep(0.1)
        with self._lock:
            if self._shutdown_requested:
                return
        try:
            window.load_url(self.ui_url + path)
        except Exception:
            LOG.debug("切换到%s标签页失败", label, exc_info=True)

    def open_overlay(self) -> bool:
        with self._lock:
            if self._shutdown_requested:
                return False
            window = self.overlay
        if window is not None:
            self._focus(window)
            return True
        with self._lock:
            if self._shutdown_requested:
                return False
            if self.overlay is not None:
                self._focus(self.overlay)
                return True
            self._create_overlay(self._webview)
        return True

    def _create_overlay(self, webview: Any) -> None:
        api = WindowApi(self)
        window = webview.create_window(
            "wows_coyote overlay",
            self.ui_url + "/overlay?app=1",
            width=OVERLAY_SIZE[0],
            height=OVERLAY_SIZE[1],
            frameless=True,
            easy_drag=True,
            on_top=True,
            transparent=True,
            resizable=False,
            shadow=False,
            background_color="#000000",
            js_api=api,
        )
        if window is None:
            return
        api._window = window
        self.overlay = window
        window.events.closed += lambda: self._on_child_closed("overlay")
        window.events.loaded += lambda: shape_overlay_window(window)

    def set_theme(self, theme: str) -> bool:
        """页面切换主题时同步原生标题栏；主题本身由前端保存。"""
        value = str(theme or "").strip().lower()
        if value not in ("dark", "light"):
            return False
        with self._lock:
            if self._shutdown_requested:
                return False
            self.theme = value
            window = self.main
        if window is None:
            return False
        threading.Thread(
            target=apply_native_title_theme,
            args=(window, value),
            name="wows-title-theme",
            daemon=True,
        ).start()
        return True

    def set_overlay_opacity(self, percent: Any) -> bool:
        """调整悬浮窗整体透明度；仅影响已打开的置顶窗。"""
        try:
            value = int(round(float(percent)))
        except Exception:
            return False
        if value < 30 or value > 100:
            return False
        with self._lock:
            if self._shutdown_requested:
                return False
            window = self.overlay
        if window is None:
            return False
        threading.Thread(
            target=apply_native_window_opacity,
            args=(window, value),
            name="wows-overlay-opacity",
            daemon=True,
        ).start()
        return True

    def set_overlay_locked(self, locked: Any) -> bool:
        """锁定悬浮窗：开启后鼠标点击/拖动都穿透到下层窗口（游戏）。"""
        value = _as_bool(locked)
        with self._lock:
            if self._shutdown_requested:
                return False
            window = self.overlay
        if window is None:
            return False
        threading.Thread(
            target=apply_window_click_through,
            args=(window, value),
            name="wows-overlay-lock",
            daemon=True,
        ).start()
        return True

    def close_window(self, window: Any) -> bool:
        """关闭调用方窗口：主窗口=退出，悬浮窗=只关自己。"""
        if window is None:
            return False
        if window is self.main:
            self.request_shutdown("主窗口关闭")
            return True
        with self._lock:
            if window is self.overlay:
                self.overlay = None
        try:
            window.destroy()
        except Exception:
            LOG.debug("关闭子窗口失败", exc_info=True)
        return True

    @staticmethod
    def _focus(window: Any) -> None:
        try:
            window.restore()
            window.show()
        except Exception:
            LOG.debug("聚焦窗口失败", exc_info=True)
