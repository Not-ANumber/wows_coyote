# -*- coding: utf-8 -*-
"""Phase 4 UI：本地 HTTP/JSON、设置校验/持久化、文件浏览测试。"""

import http.client
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone import event_presets  # noqa: E402
from standalone.config import load_config, save_user_settings  # noqa: E402
from standalone.device import FakeDevice  # noqa: E402
from standalone.host import StandaloneHost  # noqa: E402
from standalone.telemetry import TelemetryRunner  # noqa: E402
from standalone.templates import placeholder_library  # noqa: E402
from standalone.ui_server import UIBridge, UIServer  # noqa: E402


class FakeRunner:
    def __init__(self, path: str) -> None:
        self.path = path
        self.set_calls: list[str] = []

    def state(self) -> dict:
        return {"enabled": True, "path": self.path}

    def set_path(self, path: str) -> None:
        self.set_calls.append(path)
        self.path = path


class FakeUpdateChecker:
    def __init__(self) -> None:
        self.enabled = True
        self.skipped = False
        self.notes_url = (
            "https://github.com/example/repo/releases/tag/v0.1.1")
        self.calls: list[tuple] = []

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "current": "0.1.0",
            "checking": False,
            "checked_at": "2026-11-20T12:00:00+00:00",
            "error": "",
            "available": True,
            "latest": "0.1.1",
            "notes": "测试更新说明",
            "notes_url": self.notes_url,
            "published_at": "2026-11-20T12:00:00Z",
            "skipped": self.skipped,
        }

    def check_async(self, force: bool = False) -> None:
        self.calls.append(("check", force))

    def skip(self, version: str = "") -> None:
        if version and version != "0.1.1":
            raise ValueError("版本已变化，请先重新检查更新")
        self.skipped = True
        self.calls.append(("skip", version))

    def unskip(self) -> None:
        self.skipped = False
        self.calls.append(("unskip",))

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)
        self.calls.append(("enabled", self.enabled))

    def open_notes(self) -> bool:
        self.calls.append(("open_notes",))
        return bool(self.notes_url)

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))


class StandaloneUITest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(user_path="")
        templates = placeholder_library(cfg)
        self.sink = FakeDevice()
        self.host = StandaloneHost(cfg, self.sink, templates)
        self.user_settings = os.path.join(self.tmp.name, "settings.json")
        self.bridge = UIBridge(
            self.host, self.sink, runner=None, config=cfg,
            user_settings_path=self.user_settings)
        self.server = UIServer(self.bridge, host="127.0.0.1", port=0)
        self.base = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.bridge.stop()
        runner = getattr(self.bridge, "runner", None)
        reader = getattr(runner, "reader", None)
        if reader is not None:
            try:
                reader._close_handle()
            except Exception:
                pass
        self.tmp.cleanup()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_state_and_pages(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        for key in ("host", "device", "telemetry", "config", "templates",
                    "logs", "obs"):
            self.assertIn(key, state)
        self.assertEqual(state["config"]["transport"], "v4")
        self.assertEqual(state["config"]["v4_relay"], "official")
        self.assertGreaterEqual(len(state["templates"]), 1)
        self.assertEqual(state["templates"][0]["source"], "placeholder")
        self.assertIn("preview", state["templates"][0])
        self.assertIn("strength", state["templates"][0]["preview"])

        for path, needles in (("/", ("wows_coyote", "安全使用", "设置遥测",
                                     "控制台", "btnTheme")),
                              ("/settings", ("设置", "遥测文件", "浏览",
                                             "控制台", "OBS 浏览器源",
                                             "整体不透明度", "背景不透明度",
                                             "详细模式", "游戏根目录",
                                             "A/B 上限", "连接方式",
                                             'data-key="transport"',
                                             'data-key="v4_relay"',
                                             'id="refreshQr"', "保存并刷新",
                                             "btnTheme", "鼠标穿透",
                                             "overlayLock")),
                              ("/events", ("事件自定义", "hit_strength",
                                           "触发阈值", "满强度", "波形",
                                           "配置方案", "presetSelect",
                                           "保存为方案", "从文件导入",
                                           "方案文件位置", "控制台")),
                              ("/overlay", ("overlay", "关闭", "wows-theme",
                                            "set_overlay_opacity",
                                            "set_overlay_locked",
                                            "btnLock", "overlay_locked",
                                            "--obs-alpha",
                                            "overlay_detail",
                                            "obs_detail",
                                            "a-raw", "b-raw",
                                            "实际强度"))):
            status, body = self._get(path)
            self.assertEqual(status, 200)
            text = body.decode("utf-8")
            for needle in needles:
                self.assertIn(needle, text)

        for path in ("/setup", "/obs"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(path)
            self.assertEqual(ctx.exception.code, 404)

    def test_update_state_and_actions(self) -> None:
        fake = FakeUpdateChecker()
        self.bridge.update_checker = fake

        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertEqual(state["update"]["latest"], "0.1.1")
        self.assertTrue(state["update"]["available"])
        self.assertEqual(state["update"]["notes"], "测试更新说明")

        status, state = self._post("/api/update", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertIn(("check", True), fake.calls)

        status, state = self._post(
            "/api/update", {"action": "skip", "version": "0.1.1"})
        self.assertEqual(status, 200)
        self.assertTrue(state["update"]["skipped"])

        status, state = self._post("/api/update", {"action": "unskip"})
        self.assertEqual(status, 200)
        self.assertFalse(state["update"]["skipped"])

        status, state = self._post("/api/update", {"action": "open_notes"})
        self.assertEqual(status, 200)
        self.assertIn(("open_notes",), fake.calls)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/update", {"action": "bad"})
        self.assertEqual(ctx.exception.code, 400)

    def test_update_endpoint_requires_checker(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertFalse(state["update"]["enabled"])
        self.assertFalse(state["update"]["available"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/update", {"action": "check"})
        self.assertEqual(ctx.exception.code, 400)

    def test_update_endpoint_rejects_foreign_host(self) -> None:
        fake = FakeUpdateChecker()
        self.bridge.update_checker = fake
        parsed = urllib.parse.urlparse(self.base)
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=5)
        try:
            conn.request(
                "POST", "/api/update",
                body=b'{"action":"check"}',
                headers={
                    "Content-Type": "application/json",
                    "Host": "example.com",
                })
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            conn.close()
        self.assertNotIn(("check", True), fake.calls)

    def test_update_page_markup(self) -> None:
        for path, needles in (
            ("/settings", ("check_updates", "btnCheckUpdate", "updatePanel",
                           "updateNotes", "btnUpdateNotes", "btnUpdateSkip")),
            ("/", ("updateBanner", "btnUpdateGo", "btnUpdateDismiss")),
        ):
            status, body = self._get(path)
            self.assertEqual(status, 200)
            text = body.decode("utf-8")
            for needle in needles:
                self.assertIn(needle, text)

    def test_check_updates_setting(self) -> None:
        fake = FakeUpdateChecker()
        self.bridge.update_checker = fake
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["config"]["check_updates"])
        self.assertTrue(fake.enabled)
        status, state = self._post(
            "/api/settings", {"settings": {"check_updates": False}})
        self.assertEqual(status, 200)
        self.assertFalse(state["config"]["check_updates"])
        self.assertFalse(fake.enabled)
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertFalse(saved["check_updates"])

        status, state = self._post(
            "/api/settings", {"settings": {"check_updates": True}})
        self.assertEqual(status, 200)
        self.assertTrue(state["config"]["check_updates"])
        self.assertTrue(fake.enabled)

    def test_hit_fields_and_curve_preview_markup(self) -> None:
        status, body = self._get("/events")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        hit_card = text.split('data-event-card="hit"', 1)[1].split(
            'data-event-card="fire"', 1)[0]
        normal, advanced = hit_card.split("<details", 1)
        self.assertIn('data-key="hit_threshold_pct"', normal)
        self.assertIn('data-key="hit_full_pct"', normal)
        self.assertNotIn('data-key="hit_threshold_pct"', advanced)
        self.assertNotIn('data-key="hit_full_pct"', advanced)
        self.assertIn('id="hitCurvePreview" hidden', hit_card)
        self.assertIn('id="baseCurvePreview" hidden', text)

    def test_obs_server_toggle(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        status, state = self._post("/api/settings", {"settings": {
            "obs_enabled": True,
            "obs_port": port,
        }})
        self.assertEqual(status, 200)
        obs = state["obs"]
        self.assertTrue(obs["enabled"])
        self.assertTrue(obs["running"])
        self.assertEqual(obs["port"], port)
        self.assertEqual(obs["url"],
                         "http://127.0.0.1:{}/obs".format(port))
        with urllib.request.urlopen(obs["url"], timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertIn("OBS_MODE", page)
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/api/state".format(port),
                timeout=5) as response:
            obs_state = json.loads(response.read().decode("utf-8"))
        self.assertIn("device", obs_state)
        request = urllib.request.Request(
            "http://127.0.0.1:{}/api/action".format(port),
            data=b'{"action":"estop"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 405)

        status, state = self._post(
            "/api/settings", {"settings": {"obs_enabled": False}})
        self.assertEqual(status, 200)
        self.assertFalse(state["obs"]["enabled"])
        self.assertFalse(state["obs"]["running"])

    def test_actions_zero_and_estop(self) -> None:
        self.host.on_message({"type": "battle_start"})
        self.host.on_message({
            "type": "snapshot", "health": 60000, "max_health": 60000,
            "hp_pct": 100.0, "alive": True, "burning": False,
            "flooding": False, "fires": 0, "floods": 0,
        })
        self.host.tick(0.0)
        self.assertEqual(self.sink.strength, (10, 10))

        status, state = self._post("/api/action", {"action": "estop"})
        self.assertEqual(status, 200)
        self.assertEqual(state["device"]["strength"], {"a": 0, "b": 0})
        self.assertEqual(state["device"]["loops"], {})
        self.assertEqual(self.host.scheduler.active, {"a": None, "b": None})

        status, state = self._post("/api/action", {"action": "zero"})
        self.assertEqual(status, 200)
        self.assertEqual(state["device"]["strength"], {"a": 0, "b": 0})

    def test_ui_close_zeroes(self) -> None:
        self.host.on_message({"type": "battle_start"})
        self.host.on_message({
            "type": "snapshot", "health": 60000, "max_health": 60000,
            "hp_pct": 100.0, "alive": True, "burning": False,
            "flooding": False, "fires": 0, "floods": 0,
        })
        self.host.tick()
        self.assertEqual(self.sink.strength, (10, 10))
        status, state = self._post("/api/action", {"action": "ui_close"})
        self.assertEqual(status, 200)
        self.assertEqual(state["device"]["strength"], {"a": 0, "b": 0})
        self.assertEqual(self.host.scheduler.active, {"a": None, "b": None})
        self.assertTrue(any("UI 关闭" in row["text"] for row in state["logs"]))

    def test_opacity_settings(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertEqual(state["config"]["overlay_opacity"], 100)
        self.assertEqual(state["config"]["obs_opacity"], 86)

        status, state = self._post("/api/settings", {"settings": {
            "overlay_opacity": 55,
            "obs_opacity": 70,
        }})
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["overlay_opacity"], 55)
        self.assertEqual(state["config"]["obs_opacity"], 70)
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["overlay_opacity"], 55)
        self.assertEqual(saved["obs_opacity"], 70)

        for key, value in (("overlay_opacity", 29), ("obs_opacity", 19),
                           ("overlay_opacity", 101)):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/settings", {"settings": {key: value}})
            self.assertEqual(ctx.exception.code, 400)

    def test_overlay_lock_setting(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertFalse(state["config"]["overlay_locked"])
        self.assertEqual(state["hotkeys"], {})

        status, state = self._post(
            "/api/settings", {"settings": {"overlay_locked": True}})
        self.assertEqual(status, 200)
        self.assertTrue(state["config"]["overlay_locked"])
        self.assertTrue(any("鼠标点击穿透" in row["text"]
                            for row in state["logs"]))
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["overlay_locked"])

        status, state = self._post(
            "/api/settings", {"settings": {"overlay_locked": False}})
        self.assertEqual(status, 200)
        self.assertFalse(state["config"]["overlay_locked"])
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertFalse(saved["overlay_locked"])

    def test_detail_mode_settings(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertFalse(state["config"]["overlay_detail"])
        self.assertFalse(state["config"]["obs_detail"])

        status, state = self._post("/api/settings", {"settings": {
            "overlay_detail": True,
            "obs_detail": True,
        }})
        self.assertEqual(status, 200)
        self.assertTrue(state["config"]["overlay_detail"])
        self.assertTrue(state["config"]["obs_detail"])
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["overlay_detail"])
        self.assertTrue(saved["obs_detail"])

        status, state = self._post("/api/settings", {"settings": {
            "overlay_detail": False,
            "obs_detail": False,
        }})
        self.assertEqual(status, 200)
        self.assertFalse(state["config"]["overlay_detail"])
        self.assertFalse(state["config"]["obs_detail"])

    def test_theme_setting(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertEqual(state["config"]["theme"], "dark")

        status, state = self._post(
            "/api/settings", {"settings": {"theme": "light"}})
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["theme"], "light")
        self.assertFalse(any("设置更新" in row["text"] for row in state["logs"]))
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["theme"], "light")
        status, page = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b'data-theme="light"', page)
        status, overlay = self._get("/overlay")
        self.assertEqual(status, 200)
        self.assertIn(b'data-theme="light"', overlay)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/settings", {"settings": {"theme": "blue"}})
        self.assertEqual(ctx.exception.code, 400)

        status, state = self._post(
            "/api/settings", {"settings": {"theme": "dark"}})
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["theme"], "dark")

    def test_transport_settings(self) -> None:
        calls: list[tuple[str, str]] = []

        def switch(transport: str, relay: str):
            calls.append((transport, relay))
            return self.sink

        self.bridge.on_transport_change = switch
        status, state = self._post("/api/settings", {
            "settings": {"transport": "v4", "v4_relay": "local"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(calls, [("v4", "local")])
        self.assertEqual(state["config"]["transport"], "v4")
        self.assertEqual(state["config"]["v4_relay"], "local")
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["transport"], "v4")
        self.assertEqual(saved["v4_relay"], "local")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/settings", {"settings": {"transport": "v5"}})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/settings", {"settings": {"v4_relay": "bad"}})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(calls, [("v4", "local")])

    def test_auto_telemetry_path_not_cleared_by_save(self) -> None:
        path = os.path.join(self.tmp.name, "wows_telemetry.jsonl")
        open(path, "wb").close()
        self.bridge.runner = FakeRunner(path)
        self.bridge.config["telemetry_path"] = ""
        state = self.bridge.state()
        self.assertEqual(state["config"]["telemetry_path"], path)
        theme = self.bridge.config.get("theme", "dark")

        status, state = self._post("/api/settings", {
            "settings": {"telemetry_path": "", "theme": theme},
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.bridge.runner.set_calls, [path])
        self.assertEqual(self.bridge.config["telemetry_path"], path)

        # 再次保存相同设置：no-op，不能再 reset 遥测读取器。
        before = list(self.bridge.runner.set_calls)
        status, state = self._post("/api/settings", {
            "settings": {"telemetry_path": path, "theme": theme},
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.bridge.runner.set_calls, before)

    def test_save_keeps_auto_telemetry_and_battle(self) -> None:
        path = os.path.join(self.tmp.name, "auto_telemetry.jsonl")
        open(path, "wb").close()
        runner = TelemetryRunner(self.host, path)
        runner.poll()  # 打开空文件
        lines = [
            {"type": "battle_start", "ship_name": "Test", "ship_type": "BB"},
            {
                "type": "snapshot", "health": 60000, "max_health": 60000,
                "hp_pct": 100.0, "alive": True, "burning": False,
                "flooding": False, "fires": 0, "floods": 0,
            },
        ]
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for message in lines:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        runner.poll()
        self.host.tick()
        self.assertEqual(self.sink.strength, (10, 10))

        # 模拟自动发现：读取器有路径，但配置里的 telemetry_path 为空。
        self.bridge.runner = runner
        self.bridge.config["telemetry_path"] = ""
        self.host.config["telemetry_path"] = ""
        theme = "light" if self.bridge.config.get("theme") == "dark" else "dark"
        status, state = self._post("/api/settings", {
            "settings": {"telemetry_path": "", "theme": theme},
        })
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["telemetry_path"], path)
        self.assertTrue(state["host"]["battle_active"])

        # 保存后不能触发“数据源配置变化”而结束战局/重发基础强度。
        runner.poll()
        self.host.tick()
        self.assertTrue(self.host.state()["battle_active"])
        self.assertEqual(self.sink.strength, (10, 10))

    def test_event_setting_change_during_battle(self) -> None:
        status, _ = self._post("/api/settings", {"settings": {
            "base_enable": True, "base_min": 10.0, "base_max": 40.0,
            "base_curve_enable": False,
        }})
        self.assertEqual(status, 200)
        self.host.on_message({
            "type": "battle_start", "ship_name": "Test", "ship_type": "BB",
        })
        self.host.on_message({
            "type": "snapshot", "health": 60000, "max_health": 60000,
            "hp_pct": 100.0, "alive": True, "burning": False,
            "flooding": False, "fires": 0, "floods": 0,
        })
        self.host.tick()
        self.assertEqual(self.sink.strength, (10, 10))

        # 战斗中改事件参数：立即生效，但不能结束战局或重置输出。
        status, state = self._post("/api/settings", {"settings": {
            "base_enable": True, "base_min": 20.0, "base_max": 40.0,
            "base_curve_enable": False,
        }})
        self.assertEqual(status, 200)
        self.assertTrue(state["host"]["battle_active"])
        self.host.tick()
        self.assertTrue(self.host.state()["battle_active"])
        self.assertEqual(self.sink.strength, (20, 20))

    def test_software_cap_removed(self) -> None:
        # 软件强度上限设置已取消：API 拒绝该键，设置页也不再渲染输入框。
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/settings", {"max_strength": 55})
        self.assertEqual(ctx.exception.code, 400)
        self.assertNotIn("max_strength", self.bridge.config)
        status, page = self._get("/settings")
        self.assertEqual(status, 200)
        self.assertNotIn("软件强度上限".encode("utf-8"), page)
        self.assertNotIn(b'data-key="max_strength"', page)

    def test_event_settings_and_telemetry_path(self) -> None:
        telemetry = os.path.join(self.tmp.name, "wows_telemetry.jsonl")
        open(telemetry, "wb").close()
        base_template = self.host.templates.names()[0]
        payload = {
            "settings": {
                "base_min": 15,
                "base_max": 45,
                "base_channel": "a",
                "base_preset": base_template,
                "hit_strength": 35,
                "hit_threshold_pct": 3,
                "hit_duration_s": 1.5,
                "telemetry_path": telemetry,
            }
        }
        status, state = self._post("/api/settings", payload)
        self.assertEqual(status, 200)
        cfg = state["config"]
        self.assertEqual(float(cfg["base_min"]), 15.0)
        self.assertEqual(float(cfg["base_max"]), 45.0)
        self.assertEqual(cfg["base_channel"], "a")
        self.assertEqual(cfg["base_preset"], base_template)
        self.assertEqual(float(cfg["hit_strength"]), 35.0)
        self.assertEqual(float(cfg["hit_threshold_pct"]), 3.0)
        self.assertEqual(float(cfg["hit_duration_s"]), 1.5)
        self.assertEqual(state["config"]["telemetry_path"], telemetry)
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["base_preset"], base_template)
        self.assertEqual(saved["telemetry_path"], telemetry)

    def test_event_presets_roundtrip(self) -> None:
        status, state = self._post("/api/event-presets", {
            "action": "save",
            "name": "低强度",
            "settings": {
                "base_min": 5,
                "base_max": 20,
                "hit_strength": 12,
                "fire_strength": 6,
                "death_strength": 30,
            },
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["name"] for item in state["event_presets"]["items"]],
            ["低强度"])
        self.assertEqual(
            state["event_presets"]["directory"],
            self.bridge.event_presets_dir)
        # 保存方案只写方案文件，不改变当前生效设置。
        self.assertEqual(float(state["config"]["base_min"]), 10.0)

        path = os.path.join(self.bridge.event_presets_dir, "低强度.json")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["version"], event_presets.PRESET_VERSION)
        self.assertEqual(saved["name"], "低强度")
        self.assertEqual(float(saved["events"]["hit_strength"]), 12.0)
        self.assertEqual(set(saved["events"]),
                         set(event_presets.EVENT_KEYS))

        status, state = self._post("/api/settings", {"settings": {
            "base_min": 30,
            "base_max": 60,
            "hit_strength": 50,
            "fire_strength": 20,
            "death_strength": 80,
        }})
        self.assertEqual(status, 200)
        self.assertEqual(float(state["config"]["base_min"]), 30.0)

        status, state = self._post(
            "/api/event-presets", {"action": "load", "name": "低强度"})
        self.assertEqual(status, 200)
        cfg = state["config"]
        self.assertEqual(float(cfg["base_min"]), 5.0)
        self.assertEqual(float(cfg["base_max"]), 20.0)
        self.assertEqual(float(cfg["hit_strength"]), 12.0)
        self.assertEqual(float(cfg["fire_strength"]), 6.0)
        self.assertEqual(float(cfg["death_strength"]), 30.0)
        self.assertEqual(state["warnings"], [])
        with open(self.user_settings, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertEqual(float(persisted["base_min"]), 5.0)
        self.assertEqual(float(persisted["hit_strength"]), 12.0)

        status, body = self._get("/api/event-presets")
        self.assertEqual(status, 200)
        listing = json.loads(body.decode("utf-8"))
        self.assertEqual([item["name"] for item in listing["items"]],
                         ["低强度"])

        status, state = self._post(
            "/api/event-presets", {"action": "delete", "name": "低强度"})
        self.assertEqual(status, 200)
        self.assertEqual(state["event_presets"]["items"], [])
        self.assertFalse(os.path.exists(path))

    def test_event_preset_validation(self) -> None:
        invalid_names = ("", "../evil", "a/b", "CON", "x" * 41, "bad*name")
        for name in invalid_names:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/event-presets", {
                    "action": "save", "name": name, "settings": {},
                })
            self.assertEqual(ctx.exception.code, 400)
        for payload in (
            {"action": "save", "name": "坏参数",
             "settings": {"hit_channel": "x"}},
            {"action": "save", "name": "坏范围",
             "settings": {"base_min": 50, "base_max": 40}},
            {"action": "load", "name": "不存在"},
            {"action": "delete", "name": "不存在"},
            {"action": "import", "name": "空导入", "payload": {"foo": 1}},
            {"action": "export", "name": "不存在"},
            {"action": "nope", "name": "x"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/event-presets", payload)
            self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.bridge.event_preset_state()["items"], [])

    def test_event_preset_import_export(self) -> None:
        template = self.host.templates.names()[0]
        missing_curve = os.path.join(self.tmp.name, "missing.json")
        payload = {
            "version": 1,
            "name": "分享方案",
            "events": {
                "base_min": 8,
                "base_max": 28,
                "hit_strength": 22,
                "hit_preset": template,
                "hit_curve_enable": True,
                "hit_curve_path": missing_curve,
                "unknown_key": 1,
            },
        }
        status, state = self._post("/api/event-presets", {
            "action": "import", "name": "远程方案", "payload": payload,
        })
        self.assertEqual(status, 200)
        self.assertTrue(state["warnings"])
        # 导入只写方案文件，不改变当前生效设置。
        self.assertEqual(float(state["config"]["base_min"]), 10.0)
        self.assertEqual(
            [item["name"] for item in state["event_presets"]["items"]],
            ["远程方案"])

        path = os.path.join(self.bridge.event_presets_dir, "远程方案.json")
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("unknown_key", saved["events"])
        self.assertEqual(saved["events"]["hit_curve_path"], "")
        self.assertEqual(saved["events"]["hit_preset"], template)

        status, state = self._post(
            "/api/event-presets", {"action": "load", "name": "远程方案"})
        self.assertEqual(status, 200)
        self.assertEqual(float(state["config"]["base_min"]), 8.0)
        self.assertEqual(float(state["config"]["hit_strength"]), 22.0)
        self.assertEqual(state["config"]["hit_curve_path"], "")
        self.assertEqual(state["config"]["hit_preset"], template)

        status, data = self._post(
            "/api/event-presets", {"action": "export", "name": "远程方案"})
        self.assertEqual(status, 200)
        exported = data["payload"]
        self.assertEqual(exported["name"], "远程方案")
        self.assertEqual(float(exported["events"]["base_min"]), 8.0)
        self.assertNotIn("unknown_key", exported["events"])

        # 扁平格式也可以导入；名称缺省时用 payload.name。
        status, state = self._post("/api/event-presets", {
            "action": "import",
            "payload": {"name": "扁平方案", "base_min": 6, "base_max": 16},
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            {item["name"] for item in state["event_presets"]["items"]},
            {"远程方案", "扁平方案"})
        status, state = self._post(
            "/api/event-presets", {"action": "load", "name": "扁平方案"})
        self.assertEqual(status, 200)
        self.assertEqual(float(state["config"]["base_min"]), 6.0)
        self.assertEqual(float(state["config"]["base_max"]), 16.0)

    def test_curve_path_settings(self) -> None:
        base_path = os.path.join(ROOT, "wows_coyote", "base.json")
        hit_path = os.path.join(ROOT, "wows_coyote", "hit.json")
        self.assertTrue(os.path.isfile(base_path))
        self.assertTrue(os.path.isfile(hit_path))
        status, state = self._post("/api/settings", {"settings": {
            "base_curve_path": base_path,
            "hit_curve_path": hit_path,
        }})
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["base_curve_path"], base_path)
        self.assertEqual(state["config"]["hit_curve_path"], hit_path)
        self.assertEqual(state["curve_paths"]["base"], base_path)
        self.assertEqual(state["curve_paths"]["hit"], hit_path)
        self.assertEqual(state["curves"]["base"]["path"], base_path)
        self.assertGreaterEqual(len(state["curves"]["base"]["points"]), 2)
        self.assertGreaterEqual(len(state["curves"]["hit"]["points"]), 2)
        self.assertEqual(self.host.engine.base_curve_path, base_path)
        self.assertEqual(self.host.engine.hit_curve_path, hit_path)

        # 空字符串回到随包默认路径
        status, state = self._post("/api/settings", {"settings": {
            "base_curve_path": "",
            "hit_curve_path": "",
        }})
        self.assertEqual(status, 200)
        self.assertEqual(state["curve_paths"]["base"],
                         self.host.engine.base_curve_path)
        self.assertTrue(state["curve_paths"]["base"].endswith("base.json"))
        self.assertTrue(state["curve_paths"]["hit"].endswith("hit.json"))

    def test_user_settings_roundtrip(self) -> None:
        path = os.path.join(self.tmp.name, "roundtrip.json")
        curve = os.path.join(self.tmp.name, "curve.json")
        save_user_settings(path, {
            "base_min": 15.5,
            "max_strength": 33,
            "transport": "v3",
            "v4_relay": "local",
            "base_curve_path": curve,
            "theme": "light",
            "overlay_opacity": 60,
            "obs_opacity": 75,
            "overlay_detail": True,
            "obs_detail": True,
            "unknown": "ignored",
        })
        cfg = load_config(user_path=path)
        self.assertEqual(float(cfg["base_min"]), 15.5)
        self.assertNotIn("max_strength", cfg)
        self.assertEqual(cfg["transport"], "v3")
        self.assertEqual(cfg["v4_relay"], "local")
        self.assertEqual(cfg["base_curve_path"], os.path.abspath(curve))
        self.assertEqual(cfg["theme"], "light")
        self.assertEqual(cfg["overlay_opacity"], 60)
        self.assertEqual(cfg["obs_opacity"], 75)
        self.assertTrue(cfg["overlay_detail"])
        self.assertTrue(cfg["obs_detail"])
        self.assertNotIn("unknown", cfg)

        invalid = os.path.join(self.tmp.name, "invalid-theme.json")
        save_user_settings(invalid, {"theme": "blue"})
        self.assertEqual(load_config(user_path=invalid)["theme"], "dark")

    def test_onboarding_removed(self) -> None:
        status, body = self._get("/api/state")
        state = json.loads(body.decode("utf-8"))
        self.assertNotIn("onboarding_done", state)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/settings", {"settings": {"onboarding_done": True}})
        self.assertEqual(ctx.exception.code, 400)

    def test_telemetry_path_live_reconnect(self) -> None:
        telemetry = os.path.join(self.tmp.name, "live.jsonl")
        open(telemetry, "wb").close()
        runner = TelemetryRunner(self.host, enabled=False)
        self.bridge.runner = runner
        try:
            status, _state = self._post(
                "/api/settings", {"settings": {"telemetry_path": telemetry}})
            self.assertEqual(status, 200)
            self.assertTrue(runner.enabled)
            runner.poll()  # 打开空文件
            self._append(telemetry, {"type": "battle_start"})
            self._append(telemetry, {
                "type": "snapshot", "health": 60000, "max_health": 60000,
                "hp_pct": 100.0, "alive": True, "burning": False,
                "flooding": False, "fires": 0, "floods": 0,
            })
            runner.poll()
            self.host.tick()
            self.assertEqual(self.sink.strength, (10, 10))
            self.assertEqual(self.host.scheduler.active["a"].owner, "base")
        finally:
            runner.reader._close_handle()

    def test_telemetry_profile_directory(self) -> None:
        profile = os.path.join(self.tmp.name, "profile")
        os.makedirs(profile)
        telemetry = os.path.join(profile, "wows_telemetry.jsonl")
        open(telemetry, "wb").close()
        runner = TelemetryRunner(self.host, enabled=False)
        self.bridge.runner = runner
        try:
            status, state = self._post(
                "/api/settings", {"settings": {"telemetry_path": profile}})
            self.assertEqual(status, 200)
            self.assertEqual(state["config"]["telemetry_path"], profile)
            self.assertTrue(runner.enabled)
            runner.poll()
            self._append(telemetry, {"type": "battle_start"})
            self._append(telemetry, {
                "type": "snapshot", "health": 60000, "max_health": 60000,
                "hp_pct": 100.0, "alive": True, "burning": False,
                "flooding": False, "fires": 0, "floods": 0,
            })
            runner.poll()
            self.host.tick()
            self.assertEqual(runner.reader.path, telemetry)
            self.assertEqual(self.sink.strength, (10, 10))
            self.assertEqual(self.host.scheduler.active["a"].owner, "base")
        finally:
            runner.reader._close_handle()

    def _append(self, path: str, message: dict) -> None:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def test_invalid_settings(self) -> None:
        for payload in (
            {"settings": {"hit_channel": "x"}},
            {"settings": {"base_min": 101}},
            {"settings": {"base_preset": "不存在的模板"}},
            {"settings": {"telemetry_path": os.path.join(self.tmp.name, "nope")}},
            {"settings": {"base_curve_path": self.tmp.name}},
            {"settings": {"hit_curve_path": os.path.join(self.tmp.name, "nope.json")}},
            {"settings": {"base_min": 50, "base_max": 40}},
            {"settings": {"hit_threshold_pct": 10, "hit_full_pct": 5}},
            {"settings": {"obs_port": 80}},
            {"settings": {"unknown": 1}},
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/settings", payload)
            self.assertEqual(ctx.exception.code, 400)

    def test_browse(self) -> None:
        telemetry = os.path.join(self.tmp.name, "wows_telemetry.jsonl")
        open(telemetry, "wb").close()
        query = urllib.parse.urlencode({"path": self.tmp.name})
        status, body = self._get("/api/browse?" + query)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        names = [entry["name"] for entry in data["entries"]]
        self.assertIn("wows_telemetry.jsonl", names)
        self.assertEqual(data["path"], os.path.abspath(self.tmp.name))

        status, body = self._get("/api/browse?path=")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(isinstance(data.get("entries"), list))

    def test_bad_action(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/action", {"action": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_shutdown_action(self) -> None:
        calls: list[str] = []
        self.bridge.on_exit = calls.append
        status, state = self._post("/api/action", {"action": "shutdown"})
        self.assertEqual(status, 200)
        self.assertEqual(calls, ["UI 退出"])
        self.assertTrue(any("退出程序" in row["text"] for row in state["logs"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
