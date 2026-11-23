# -*- coding: utf-8 -*-
"""Phase 2：官方模板加载器测试（合成 manifest / fake SDK，不含 GPL 数据）。"""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone.official_templates import (  # noqa: E402
    TemplateLoadError, choose_defaults, format_catalog, frames_sha256,
    load_from_manifest, load_from_sdk,
)
from standalone.templates import TemplateLibrary  # noqa: E402

UNIT_A = "0A0A0A0A00000000"
UNIT_B = "0A0A0A0A64646464"
FRAMES = [UNIT_A, UNIT_B]


def _fake_sdk_modules():
    pkg = types.ModuleType("dglab_kit_python")
    pkg.__path__ = []
    waveform = types.ModuleType("dglab_kit_python.waveform")
    waveform.__path__ = []
    coyote = types.ModuleType("dglab_kit_python.waveform.coyote")
    coyote.COYOTE_WAVEFORMS = {
        "BREATHING": {"label": {"cn": "呼吸", "en": "Breathing"},
                      "raw": list(FRAMES)},
        "EXTRUSTION": {"label": {"cn": "挤压", "en": "Extrustion"},
                       "raw": list(FRAMES)},
        "BUBBLE": {"label": {"cn": "气泡", "en": "Bubble"},
                   "raw": list(FRAMES)},
        "TIDE": {"label": {"cn": "潮汐", "en": "Tide"},
                 "raw": list(FRAMES)},
        "SHADE": {"label": {"cn": "树荫", "en": "Shade"},
                  "raw": list(FRAMES)},
    }
    return {
        "dglab_kit_python": pkg,
        "dglab_kit_python.waveform": waveform,
        "dglab_kit_python.waveform.coyote": coyote,
    }


class OfficialTemplateTest(unittest.TestCase):

    def test_manifest_roundtrip_and_sha(self) -> None:
        sha = frames_sha256(FRAMES)
        manifest = {
            "schema_version": 1,
            "source": "official-demo",
            "license": "unofficial-personal-use",
            "commit": "test-commit",
            "waves": [{
                "id": "W1", "name": "测试波形",
                "frames": list(FRAMES), "sha256": sha,
                "loop": True, "default_ms": 200,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False)
            library = load_from_manifest(path)
        self.assertEqual(len(library), 1)
        template = library.get("W1")
        self.assertIsNotNone(template)
        self.assertEqual(template.units, FRAMES)
        self.assertEqual(template.sha256, sha)
        self.assertTrue(template.loop)
        self.assertEqual(library.source, "official-demo")
        self.assertEqual(library.license, "unofficial-personal-use")
        self.assertIn("official-demo", format_catalog(library))

    def test_manifest_sha_mismatch_rejects_whole_file(self) -> None:
        manifest = {
            "schema_version": 1, "source": "official", "license": "test",
            "waves": [{
                "id": "W1", "frames": list(FRAMES),
                "sha256": "0" * 64,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaises(TemplateLoadError):
                load_from_manifest(path)

    def test_manifest_invalid_frame_rejects_whole_file(self) -> None:
        manifest = {
            "schema_version": 1, "source": "official", "license": "test",
            "waves": [
                {"id": "OK", "frames": list(FRAMES)},
                {"id": "BAD", "frames": ["ZZZZZZZZZZZZZZZZ"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaises(TemplateLoadError):
                load_from_manifest(path)

    def test_manifest_requires_source_and_license(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"waves": [{"id": "W1", "frames": FRAMES}]}, handle)
            with self.assertRaises(TemplateLoadError):
                load_from_manifest(path)

    def test_sdk_load_and_default_mapping(self) -> None:
        with patch.dict(sys.modules, _fake_sdk_modules()):
            library = load_from_sdk()
        self.assertEqual(len(library), 5)
        self.assertEqual(library.source, "dglab-kit-python")
        self.assertEqual(library.license, "GPL-3.0-only")
        template = library.get("BREATHING")
        self.assertEqual(template.name, "呼吸")
        self.assertEqual(template.units, FRAMES)
        self.assertEqual(template.sha256, frames_sha256(FRAMES))

        mapping = choose_defaults(library)
        self.assertEqual(mapping, {
            "base": "BREATHING",
            "hit": "EXTRUSTION",
            "fire": "BUBBLE",
            "flood": "TIDE",
            "death": "SHADE",
        })

    def test_sdk_invalid_wave_is_skipped(self) -> None:
        modules = _fake_sdk_modules()
        coyote = modules["dglab_kit_python.waveform.coyote"]
        coyote.COYOTE_WAVEFORMS["BAD"] = {"label": {"cn": "坏"}, "raw": ["x"]}
        with patch.dict(sys.modules, modules):
            library = load_from_sdk()
        self.assertIsNone(library.get("BAD"))
        self.assertEqual(len(library.rejected), 1)

    def test_bundled_manifest(self) -> None:
        path = os.path.join(
            ROOT, "standalone", "templates", "official_manifest.json")
        self.assertTrue(os.path.isfile(path), path)
        library = load_from_manifest(path)
        self.assertEqual(len(library), 24)
        self.assertEqual(library.source, "dglab-kit-python")
        self.assertEqual(library.license, "GPL-3.0-only")
        self.assertNotEqual(library.commit, "source")
        self.assertTrue(all(
            template.license == "GPL-3.0-only"
            for template in library.templates.values()))

    def test_empty_library_mapping(self) -> None:
        self.assertEqual(choose_defaults(TemplateLibrary("none")), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
