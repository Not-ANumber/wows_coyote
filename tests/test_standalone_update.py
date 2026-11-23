# -*- coding: utf-8 -*-
"""standalone 更新检查：清单校验、版本比较、缓存、节流、跳过。"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone.update import (  # noqa: E402
    MAX_MANIFEST_BYTES, UpdateChecker, UpdateError, is_newer, parse_manifest,
    parse_version,
)

NOTES_URL = "https://github.com/OWNER/REPO/releases/tag/v0.1.1"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def manifest(version: str = "0.1.1", notes: str = "修复测试",
             notes_url: str = NOTES_URL, **extra) -> bytes:
    data = {
        "schema": 1,
        "version": version,
        "published_at": "2026-11-20T12:00:00Z",
        "notes": notes,
        "notes_url": notes_url,
        "installer": {
            "name": "wows_coyote-{}-setup.exe".format(version),
            "size": 100,
            "sha256": "0" * 64,
            "urls": ["https://github.com/OWNER/REPO/releases/download/"
                     "v{}/wows_coyote-{}-setup.exe".format(version, version)],
        },
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class UpdateVersionTest(unittest.TestCase):

    def test_parse_and_compare(self) -> None:
        self.assertEqual(parse_version("0.1.2"), (0, 1, 2))
        self.assertEqual(parse_version(" 10.20.30 "), (10, 20, 30))
        for bad in ("", "0.1", "0.1.2.3", "v0.1.2", "0.1.x"):
            with self.assertRaises(ValueError):
                parse_version(bad)
        self.assertTrue(is_newer("0.1.2", "0.1.0"))
        self.assertTrue(is_newer("0.1.10", "0.1.2"))
        self.assertFalse(is_newer("0.1.0", "0.1.0"))
        self.assertFalse(is_newer("0.0.9", "0.1.0"))
        self.assertFalse(is_newer("bad", "0.1.0"))


class UpdateManifestTest(unittest.TestCase):

    def test_valid_manifest(self) -> None:
        release = parse_manifest(manifest())
        self.assertEqual(release.version, "0.1.1")
        self.assertEqual(release.notes, "修复测试")
        self.assertEqual(release.notes_url, NOTES_URL)
        self.assertEqual(release.published_at, "2026-11-20T12:00:00Z")

    def test_invalid_manifests(self) -> None:
        cases = [
            b"not json",
            b"[]",
            json.dumps({"schema": 2, "version": "0.1.1",
                        "notes_url": NOTES_URL}).encode(),
            manifest(version="0.1"),
            manifest(notes_url=""),
            manifest(notes_url="http://example.com/release"),
        ]
        for raw in cases:
            with self.assertRaises(UpdateError):
                parse_manifest(raw)

    def test_notes_truncated(self) -> None:
        release = parse_manifest(manifest(notes="x" * 9000))
        self.assertLessEqual(len(release.notes), 8001)


class UpdateCheckerTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _checker(self, fetch, clock=None, enabled=True, current="0.1.0"):
        return UpdateChecker(
            current,
            state_dir=self.tmp.name,
            enabled=enabled,
            fetch=fetch,
            clock=clock or Clock(),
        )

    def _wait_done(self, checker: UpdateChecker) -> dict:
        self.assertTrue(wait_for(lambda: not checker.state()["checking"]))
        return checker.state()

    def test_check_success_and_persist(self) -> None:
        checker = self._checker(lambda url, timeout: manifest())
        checker.check_async(force=True)
        state = self._wait_done(checker)
        self.assertFalse(state["error"])
        self.assertTrue(state["available"])
        self.assertEqual(state["latest"], "0.1.1")
        self.assertEqual(state["notes"], "修复测试")
        self.assertTrue(state["checked_at"])
        self.assertTrue(os.path.isfile(checker.state_path))

        # 同一份缓存启动：24 小时内不再请求，但仍能看到新版本。
        calls: list[str] = []

        def fail(url: str, timeout: float) -> bytes:
            calls.append(url)
            raise UpdateError("offline")

        clock = Clock(1000.0 + 23 * 3600)
        cached = UpdateChecker(
            "0.1.0", state_dir=self.tmp.name, fetch=fail, clock=clock)
        cached.maybe_check_async()
        time.sleep(0.05)
        self.assertEqual(calls, [])
        state = cached.state()
        self.assertTrue(state["available"])
        self.assertEqual(state["latest"], "0.1.1")

        # 超过 24 小时后允许自动重试；失败时保留缓存。
        clock.advance(3600)
        retry = UpdateChecker(
            "0.1.0", state_dir=self.tmp.name, fetch=fail, clock=clock)
        retry.maybe_check_async()
        state = self._wait_done(retry)
        self.assertEqual(calls, [retry.manifest_url])
        self.assertIn("offline", state["error"])
        self.assertTrue(state["available"])

    def test_failure_without_cache(self) -> None:
        def fail(url: str, timeout: float) -> bytes:
            raise UpdateError("network down")

        checker = self._checker(fail)
        checker.check_async(force=True)
        state = self._wait_done(checker)
        self.assertEqual(state["error"], "network down")
        self.assertFalse(state["available"])
        self.assertEqual(state["latest"], "")

    def test_oversized_and_invalid_payload(self) -> None:
        checker = self._checker(
            lambda url, timeout: b"x" * (MAX_MANIFEST_BYTES + 1))
        checker.check_async(force=True)
        state = self._wait_done(checker)
        self.assertIn("过大", state["error"])
        self.assertFalse(state["available"])

        checker = self._checker(lambda url, timeout: b"{}")
        checker.check_async(force=True)
        state = self._wait_done(checker)
        self.assertTrue(state["error"])
        self.assertFalse(state["available"])

    def test_skip_and_unskip_persist(self) -> None:
        checker = self._checker(lambda url, timeout: manifest())
        checker.check_async(force=True)
        self._wait_done(checker)
        checker.skip("0.1.1")
        self.assertTrue(checker.state()["skipped"])

        restored = UpdateChecker(
            "0.1.0", state_dir=self.tmp.name,
            fetch=lambda url, timeout: manifest(), clock=Clock())
        self.assertTrue(restored.state()["skipped"])
        restored.unskip()
        self.assertFalse(restored.state()["skipped"])

        with self.assertRaises(ValueError):
            checker.skip("0.1.2")
        with tempfile.TemporaryDirectory() as empty_dir:
            empty = UpdateChecker(
                "0.1.0", state_dir=empty_dir,
                fetch=lambda url, timeout: manifest(), clock=Clock())
            with self.assertRaises(ValueError):
                empty.skip()

    def test_disabled_auto_check_but_manual_works(self) -> None:
        calls: list[str] = []

        def fetch(url: str, timeout: float) -> bytes:
            calls.append(url)
            return manifest()

        checker = self._checker(fetch, enabled=False)
        checker.maybe_check_async()
        time.sleep(0.05)
        self.assertEqual(calls, [])
        checker.check_async()
        time.sleep(0.05)
        self.assertEqual(calls, [])
        checker.check_async(force=True)
        state = self._wait_done(checker)
        self.assertEqual(calls, [checker.manifest_url])
        self.assertTrue(state["available"])

    def test_manifest_url_policy(self) -> None:
        UpdateChecker(
            "0.1.0", manifest_url="http://127.0.0.1:9/latest.json",
            state_dir=self.tmp.name)
        for url in ("http://example.com/latest.json",
                    "ftp://example.com/latest.json", ""):
            with self.assertRaises(ValueError):
                UpdateChecker("0.1.0", manifest_url=url,
                              state_dir=self.tmp.name)

    def test_open_notes(self) -> None:
        checker = self._checker(lambda url, timeout: manifest())
        checker.check_async(force=True)
        self._wait_done(checker)
        with mock.patch(
                "standalone.update.webbrowser.open",
                return_value=True) as opened:
            self.assertTrue(checker.open_notes())
            opened.assert_called_once_with(NOTES_URL)


if __name__ == "__main__":
    unittest.main()
