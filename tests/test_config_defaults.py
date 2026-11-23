# -*- coding: utf-8 -*-
"""默认配置来源：事件默认 JSON、bundled preset、用户设置的三层覆盖。"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone import event_presets  # noqa: E402
from standalone.config import (  # noqa: E402
    DEFAULT_EVENTS_PATH, DEFAULT_PRESET, load_config,
)
from standalone.plugin_bridge import plugin  # noqa: E402

TEMPLATE_IDS = ("RAINFALL", "PULSATING", "GRAINY", "TIDE", "COMPRESS")


class ConfigDefaultsTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, data) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        return path

    def _default_events(self) -> dict:
        with open(DEFAULT_EVENTS_PATH, "r", encoding="utf-8") as handle:
            return event_presets.normalize_events(json.load(handle))

    def test_default_events_json_is_complete_and_valid(self) -> None:
        events = self._default_events()
        self.assertEqual(set(events), set(event_presets.EVENT_KEYS))

        with open(os.path.join(
                ROOT, "standalone", "templates", "official_manifest.json"),
                "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        available = {wave["id"] for wave in manifest["waves"]}
        for event in ("base", "hit", "fire", "flood", "death"):
            preset = events["{}_preset".format(event)]
            self.assertTrue(not preset or preset in available, preset)
        self.assertLessEqual(float(events["hit_threshold_pct"]),
                             float(events["hit_full_pct"]))
        self.assertLessEqual(float(events["base_min"]),
                             float(events["base_max"]))

    def test_default_events_initialize_config(self) -> None:
        cfg = load_config(user_path="")
        events = self._default_events()
        self.assertEqual(set(event_presets.EVENT_KEYS) - set(cfg), set())
        for key, value in events.items():
            self.assertEqual(cfg[key], plugin.auto_cast(key, value), key)
        self.assertEqual(cfg["telemetry_timeout_s"], 60)
        self.assertEqual(cfg["polling_rate"], 100)

    def test_user_settings_override_default_events(self) -> None:
        path = self._write("settings.json", {
            "base_min": 1,
            "base_max": 2,
            "base_preset": "TIDE",
            "hit_strength": 7,
            "hit_duration_s": 3.5,
        })
        cfg = load_config(user_path=path)
        self.assertEqual(float(cfg["base_min"]), 1.0)
        self.assertEqual(float(cfg["base_max"]), 2.0)
        self.assertEqual(cfg["base_preset"], "TIDE")
        self.assertEqual(float(cfg["hit_strength"]), 7.0)
        self.assertEqual(float(cfg["hit_duration_s"]), 3.5)
        # 未被用户设置覆盖的事件仍来自默认 JSON。
        self.assertEqual(cfg["fire_preset"],
                         self._default_events()["fire_preset"])

    def test_custom_preset_overrides_default_events(self) -> None:
        path = self._write("custom.json", {
            "base_min": 30,
            "base_max": 60,
            "hit_preset": "",
            "polling_rate": 250,
        })
        cfg = load_config(path, user_path="")
        self.assertEqual(float(cfg["base_min"]), 30.0)
        self.assertEqual(float(cfg["base_max"]), 60.0)
        self.assertEqual(cfg["hit_preset"], "")
        self.assertEqual(cfg["polling_rate"], 250)
        # 自定义预设没写的字段继续使用默认 JSON。
        defaults = self._default_events()
        self.assertEqual(float(cfg["hit_strength"]),
                         float(defaults["hit_strength"]))
        self.assertEqual(cfg["fire_preset"], defaults["fire_preset"])

    def test_bundled_preset_does_not_override_event_defaults(self) -> None:
        cfg = load_config(preset_path=DEFAULT_PRESET, user_path="")
        defaults = self._default_events()
        self.assertEqual(cfg["base_preset"], defaults["base_preset"])
        self.assertEqual(cfg["hit_preset"], defaults["hit_preset"])
        self.assertEqual(cfg["polling_rate"], 100)

    def test_missing_or_bad_events_file_falls_back_to_bundled_preset(
            self) -> None:
        missing = os.path.join(self.tmp.name, "missing.json")
        for events_path in (missing, "", self._write("bad.json", [1, 2])):
            cfg = load_config(user_path="", events_path=events_path)
            # 回退到 bundled v2 预设的旧版占位名；run.py 会再映射到官方模板。
            self.assertEqual(cfg["base_preset"], "雨水冲刷")
            self.assertEqual(cfg["hit_preset"], "连击")

    def test_partial_events_file_only_overrides_listed_keys(self) -> None:
        path = self._write("partial.json", {
            "events": {"base_min": 25, "base_max": 65},
        })
        cfg = load_config(user_path="", events_path=path)
        self.assertEqual(float(cfg["base_min"]), 25.0)
        self.assertEqual(float(cfg["base_max"]), 65.0)
        # 未列出的键仍由 bundled v2 预设兜底。
        self.assertEqual(cfg["base_preset"], "雨水冲刷")
        self.assertEqual(cfg["hit_preset"], "连击")

    def test_base_curve_defaults(self) -> None:
        cfg = load_config(user_path="")
        self.assertFalse(cfg["base_curve_enable"])
        self.assertFalse(cfg["hit_curve_enable"])
        self.assertEqual(cfg["base_curve_path"], "")
        self.assertEqual(cfg["hit_curve_path"], "")

    def test_overlay_lock_default_and_user_override(self) -> None:
        self.assertFalse(load_config(user_path="")["overlay_locked"])
        path = self._write("lock_on.json", {"overlay_locked": "yes"})
        self.assertTrue(load_config(user_path=path)["overlay_locked"])
        path = self._write("lock_off.json", {"overlay_locked": "off"})
        self.assertFalse(load_config(user_path=path)["overlay_locked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
