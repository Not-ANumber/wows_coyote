# -*- coding: utf-8 -*-
"""pywebview 窗口生命周期测试：主窗口关闭归零/退出，子窗口独立关闭。"""

import os
import sys
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone import desktop as desktop_module  # noqa: E402
from standalone.desktop import DesktopShell, WindowApi  # noqa: E402


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in list(self.handlers):
            handler()


class FakeWindow:
    def __init__(self, title: str, url: str, **kwargs) -> None:
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.events = types.SimpleNamespace(
            closing=FakeEvent(), closed=FakeEvent(), loaded=FakeEvent(),
            before_show=FakeEvent())
        self.destroyed = False
        self.destroy_count = 0
        self.show_count = 0

    def destroy(self) -> None:
        self.destroy_count += 1
        self.destroyed = True
        self.events.closed.fire()

    def show(self) -> None:
        self.show_count += 1

    def restore(self) -> None:
        pass

    def load_url(self, url: str) -> None:
        self.url = url
        self.events.loaded.fire()


class FakeWebview(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self.windows = []
        self.started = False

    def create_window(self, title, url, **kwargs):
        window = FakeWindow(title, url, **kwargs)
        self.windows.append(window)
        return window

    def start(self, debug=False):
        self.started = True
        for window in self.windows:
            window.events.loaded.fire()
        # 模拟 GUI 循环：所有窗口销毁后返回。
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if all(window.destroyed for window in self.windows):
                return
            time.sleep(0.01)


class ExitRaisingWebview(FakeWebview):
    """模拟用户关闭后 WebView2 在退出阶段抛异常。"""

    def start(self, debug=False):
        self.started = True
        for window in self.windows:
            window.events.loaded.fire()
        self.windows[0].events.closing.fire()
        raise RuntimeError("shutdown glitch")


class BrokenStartWebview(FakeWebview):
    """模拟 WebView2 启动失败。"""

    def start(self, debug=False):
        self.started = True
        raise RuntimeError("webview2 missing")


def wait_for(predicate, timeout=2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class DesktopShellTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reasons: list[str] = []
        self.shell = DesktopShell(
            "http://127.0.0.1:12345",
            on_shutdown=self.reasons.append,
        )
        self.fake = FakeWebview()
        self.shell._webview = self.fake
        self.shell._create_main(self.fake)

    def test_run_returns_and_shuts_down_when_main_closes(self) -> None:
        reasons: list[str] = []
        shell = DesktopShell(
            "http://127.0.0.1:12345", on_shutdown=reasons.append)
        fake = FakeWebview()
        shell._webview = fake

        def close_main() -> None:
            shell.main.events.closing.fire()
            shell.main.destroy()  # 模拟原生窗口在 closing 后真正销毁

        threading.Timer(0.05, close_main).start()
        old = sys.modules.get("webview")
        sys.modules["webview"] = fake
        try:
            shell.run()
        finally:
            if old is None:
                sys.modules.pop("webview", None)
            else:
                sys.modules["webview"] = old
        self.assertTrue(fake.started)
        self.assertEqual(reasons, ["主窗口关闭"])
        self.assertIsNone(shell.main)
        self.assertTrue(fake.windows[0].destroyed)

    def _run_with(self, shell: DesktopShell, fake: FakeWebview) -> None:
        old = sys.modules.get("webview")
        sys.modules["webview"] = fake
        try:
            shell.run()
        finally:
            if old is None:
                sys.modules.pop("webview", None)
            else:
                sys.modules["webview"] = old

    def test_exit_time_exception_is_swallowed(self) -> None:
        reasons: list[str] = []
        shell = DesktopShell(
            "http://127.0.0.1:12345", on_shutdown=reasons.append)
        fake = ExitRaisingWebview()
        shell._webview = fake
        self._run_with(shell, fake)
        self.assertEqual(reasons, ["主窗口关闭"])
        self.assertTrue(shell.shutdown_requested)

    def test_startup_exception_propagates(self) -> None:
        reasons: list[str] = []
        shell = DesktopShell(
            "http://127.0.0.1:12345", on_shutdown=reasons.append)
        fake = BrokenStartWebview()
        shell._webview = fake
        with self.assertRaises(RuntimeError):
            self._run_with(shell, fake)
        self.assertEqual(reasons, [])
        self.assertFalse(shell.shutdown_requested)

    def test_main_close_destroys_overlay(self) -> None:
        self.shell._create_overlay(self.fake)
        main = self.shell.main
        overlay = self.shell.overlay
        main.events.closing.fire()
        self.assertTrue(wait_for(lambda: overlay.destroyed))
        self.assertEqual(self.reasons, ["主窗口关闭"])
        # 正在关闭的主窗口不应被再次 destroy()，避免 pywebview 重复销毁报错。
        self.assertFalse(main.destroyed)
        self.assertEqual(main.destroy_count, 0)

    def test_overlay_close_keeps_main_running(self) -> None:
        self.shell._create_overlay(self.fake)
        overlay = self.shell.overlay
        overlay.events.closed.fire()
        self.assertIsNone(self.shell.overlay)
        self.assertEqual(self.reasons, [])
        self.assertFalse(self.shell.main.destroyed)
        self.assertFalse(overlay.destroyed)

    def test_api_set_theme(self) -> None:
        api = WindowApi(self.shell)
        api._window = self.shell.main
        self.assertTrue(api.set_theme("light"))
        self.assertEqual(self.shell.theme, "light")
        self.assertFalse(api.set_theme("blue"))
        self.assertEqual(self.shell.theme, "light")

    def test_api_set_overlay_opacity(self) -> None:
        api = WindowApi(self.shell)
        self.assertFalse(api.set_overlay_opacity(50))
        self.shell._create_overlay(self.fake)
        api._window = self.shell.overlay
        self.assertTrue(api.set_overlay_opacity(55))
        self.assertFalse(api.set_overlay_opacity(10))
        self.assertFalse(api.set_overlay_opacity("bad"))

    def test_api_set_overlay_locked(self) -> None:
        calls: list[bool] = []
        original = desktop_module.apply_window_click_through
        desktop_module.apply_window_click_through = (
            lambda window, locked: calls.append(locked))
        try:
            api = WindowApi(self.shell)
            # 没有悬浮窗时不做任何事。
            self.assertFalse(api.set_overlay_locked(True))
            self.shell._create_overlay(self.fake)
            api._window = self.shell.overlay
            self.assertTrue(api.set_overlay_locked(True))
            self.assertTrue(api.set_overlay_locked("false"))
            self.assertTrue(wait_for(lambda: len(calls) == 2))
        finally:
            desktop_module.apply_window_click_through = original
        self.assertEqual(calls, [True, False])

    def test_initial_light_theme_window_background(self) -> None:
        shell = DesktopShell(
            "http://127.0.0.1:12345",
            on_shutdown=lambda reason: None,
            theme="light",
        )
        fake = FakeWebview()
        shell._webview = fake
        shell._create_main(fake)
        self.assertEqual(shell.theme, "light")
        self.assertEqual(
            fake.windows[0].kwargs["background_color"], "#f3eee2")

    def test_api_close_main_triggers_shutdown(self) -> None:
        api = WindowApi(self.shell)
        api._window = self.shell.main
        self.assertTrue(api.close_window())
        self.assertTrue(wait_for(lambda: bool(self.reasons)))
        self.assertEqual(self.reasons, ["主窗口关闭"])

    def test_open_settings_loads_main_window(self) -> None:
        self.assertTrue(self.shell.open_settings())
        self.assertTrue(wait_for(
            lambda: self.fake.windows[0].url.endswith("/settings?app=1")))
        self.assertEqual(len(self.fake.windows), 1)
        self.assertGreaterEqual(self.fake.windows[0].show_count, 1)
        self.assertTrue(self.shell.open_settings())
        self.assertEqual(len(self.fake.windows), 1)

    def test_open_events_loads_main_window(self) -> None:
        self.assertTrue(self.shell.open_events())
        self.assertTrue(wait_for(
            lambda: self.fake.windows[0].url.endswith("/events?app=1")))
        self.assertEqual(len(self.fake.windows), 1)
        self.assertGreaterEqual(self.fake.windows[0].show_count, 1)

    def test_open_overlay_reuses_existing_window(self) -> None:
        self.assertTrue(self.shell.open_overlay())
        first = self.shell.overlay
        self.assertIsNotNone(first)
        self.assertTrue(self.shell.open_overlay())
        self.assertIs(self.shell.overlay, first)
        self.assertEqual(len(self.fake.windows), 2)

    def test_shutdown_is_idempotent(self) -> None:
        self.shell.request_shutdown("第一次")
        self.shell.request_shutdown("第二次")
        self.assertTrue(wait_for(lambda: bool(self.reasons)))
        self.assertEqual(self.reasons, ["第一次"])
        self.assertEqual(self.fake.windows[0].destroy_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
