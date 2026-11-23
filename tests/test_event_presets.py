# -*- coding: utf-8 -*-
"""事件配置方案：命名校验、文件读写、列表/删除。"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone import event_presets  # noqa: E402
from standalone.ui_server import CONFIG_KEYS  # noqa: E402


class EventPresetsTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = os.path.join(self.tmp.name, "event_presets")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_name_validation(self) -> None:
        self.assertEqual(event_presets.validate_name("  低强度 20%  "),
                         "低强度 20%")
        for name in ("", "   ", "../evil", "a/b", 'a:b', "a*b",
                     "a?b", 'a"b', "a<b", "a>b", "a|b", "a\\b",
                     "CON", "con", "nul", "LPT1", "x" * 41,
                     "trailing."):
            with self.assertRaises(ValueError, msg=repr(name)):
                event_presets.validate_name(name)

    def test_normalize_events(self) -> None:
        wrapped = event_presets.normalize_events({
            "version": 1,
            "name": "方案",
            "events": {"base_min": 5, "unknown": 1},
        })
        self.assertEqual(wrapped, {"base_min": 5})
        flat = event_presets.normalize_events({"base_max": 30, "name": "x"})
        self.assertEqual(flat, {"base_max": 30})
        for payload in ({}, {"foo": 1}, [], "text", None):
            with self.assertRaises(ValueError):
                event_presets.normalize_events(payload)

    def test_write_list_load_delete(self) -> None:
        first = event_presets.write_preset(
            self.directory, "旧方案",
            {"base_min": 5, "hit_strength": 10, "unknown": 1},
            saved_at="2026-01-01T10:00:00+08:00")
        second = event_presets.write_preset(
            self.directory, "新方案",
            {"base_min": 8, "hit_strength": 20},
            saved_at="2026-02-01T10:00:00+08:00")
        self.assertTrue(os.path.isfile(first))
        self.assertTrue(os.path.isfile(second))

        with open(second, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["version"], event_presets.PRESET_VERSION)
        self.assertEqual(payload["name"], "新方案")
        self.assertNotIn("unknown", payload["events"])
        self.assertEqual(event_presets.load_events(self.directory, "新方案"),
                         {"base_min": 8, "hit_strength": 20})

        items = event_presets.list_presets(self.directory)
        self.assertEqual([item["name"] for item in items],
                         ["新方案", "旧方案"])
        self.assertEqual(items[0]["saved_at"], "2026-02-01T10:00:00+08:00")
        self.assertEqual(items[0]["event_count"], 2)

        # 坏文件与非法文件名不影响其他方案。
        with open(os.path.join(self.directory, "bad.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        with open(os.path.join(self.directory, ("x" * 41) + ".json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"events": {"base_min": 1}}, handle)
        with open(os.path.join(self.directory, "notes.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("ignored")
        items = event_presets.list_presets(self.directory)
        self.assertEqual([item["name"] for item in items],
                         ["新方案", "旧方案"])

        event_presets.delete_preset(self.directory, "旧方案")
        self.assertFalse(os.path.exists(first))
        self.assertEqual([item["name"] for item in
                          event_presets.list_presets(self.directory)],
                         ["新方案"])

        with self.assertRaises(ValueError):
            event_presets.load_events(self.directory, "旧方案")
        with self.assertRaises(ValueError):
            event_presets.delete_preset(self.directory, "旧方案")
        with self.assertRaises(ValueError):
            event_presets.write_preset(self.directory, "空", {})

    def test_event_keys_are_managed_by_ui(self) -> None:
        self.assertTrue(set(event_presets.EVENT_KEYS) <= set(CONFIG_KEYS))
        self.assertEqual(len(event_presets.EVENT_KEYS),
                         len(set(event_presets.EVENT_KEYS)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
