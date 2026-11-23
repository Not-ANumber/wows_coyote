# -*- coding: utf-8 -*-
"""P3 固定事件序列的纯数据测试。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone.test_sequence import build_test_sequence, describe_sequence  # noqa: E402


class TestSequenceTest(unittest.TestCase):

    def test_sequence_order(self) -> None:
        sequence = build_test_sequence()
        times = [at for at, _ in sequence]
        self.assertEqual(times, sorted(times))
        kinds = [message.get("type") for _, message in sequence]
        self.assertEqual(kinds[:3], ["battle_start", "snapshot", "status_change"])
        self.assertEqual(kinds[-1], "battle_end")
        self.assertEqual(kinds.count("damage_taken"), 2)

    def test_sequence_contains_fire_hit_death(self) -> None:
        sequence = build_test_sequence()
        text = repr(sequence)
        self.assertIn("burning", text)
        self.assertIn("damage_taken", text)
        self.assertIn("alive", text)
        self.assertIn("battle_end", text)

    def test_describe(self) -> None:
        text = describe_sequence(build_test_sequence())
        self.assertIn("P3 测试序列", text)
        self.assertIn("damage_taken", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
