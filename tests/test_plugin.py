# -*- coding: utf-8 -*-
"""v2 MixerEngine 离线测试：不连接 DGHub / 设备。"""

import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wows_coyote')
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import main as plugin


class Collector(object):
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)

    def drain(self):
        items = self.items[:]
        self.items = []
        return items


def battle_start(ship='Yamato', ship_type='BB'):
    return {'type': 'battle_start', 'ship_name': ship, 'ship_type': ship_type}


def snapshot(hp_pct=100.0, max_hp=60000, alive=True, burning=False,
             flooding=False, fires=0, floods=0):
    return {
        'type': 'snapshot',
        'health': int(round(max_hp * hp_pct / 100.0)),
        'max_health': max_hp,
        'hp_pct': hp_pct,
        'alive': alive,
        'burning': burning,
        'flooding': flooding,
        'fires': fires,
        'floods': floods,
    }


def damage(amount, source='damages'):
    return {'type': 'damage_taken', 'source': source, 'amount': amount}


def status(kind, state, count=None):
    message = {'type': 'status_change', 'kind': kind, 'state': state}
    if count is not None:
        message['count'] = count
    return message


def count_change(kind, count):
    return {'type': 'count_change', 'kind': kind, 'count': count}


class MixerEngineTests(unittest.TestCase):

    def setUp(self):
        self.collector = Collector()
        plugin.send_queue = self.collector
        plugin.plugin_cfg = dict(plugin.SCHEMA_DEFAULTS)
        plugin.plugin_cfg.update({
            'base_enable': True, 'base_min': 10, 'base_max': 40,
            'base_channel': 'both', 'base_preset': '',
            'hit_enable': True, 'hit_strength': 40, 'hit_channel': 'both',
            'hit_preset': '', 'hit_threshold_pct': 2, 'hit_full_pct': 8,
            'hit_duration_s': 2.0,
            'fire_enable': True, 'fire_strength': 12, 'fire_stack_add': 6,
            'fire_channel': 'both', 'fire_preset': '',
            'flood_enable': True, 'flood_strength': 12, 'flood_stack_add': 6,
            'flood_channel': 'both', 'flood_preset': '',
            'death_enable': True, 'death_strength': 60, 'death_channel': 'both',
            'death_preset': '', 'death_duration_s': 4.0,
        })
        self.clock = [1000.0]
        self.engine = plugin.MixerEngine(
            now_fn=lambda: self.clock[0])
        self.engine.startup_reset()

    # ---- helpers --------------------------------------------------

    def advance(self, seconds):
        self.clock[0] += seconds

    def tick(self):
        self.engine.tick(self.clock[0])

    def begin(self, hp=100.0, max_hp=60000):
        self.engine.on_message(battle_start())
        self.engine.on_message(snapshot(hp, max_hp))
        self.advance(0.1)
        self.tick()

    def feed(self, message):
        self.engine.on_message(message)

    def drain(self):
        return self.collector.drain()

    @staticmethod
    def strength(ops):
        result = {}
        for op in ops:
            if op.get('op') != 'set_strength':
                continue
            channel = str(op.get('channel', 'both'))
            pct = op.get('pct')
            if channel == 'both':
                result['a'] = pct
                result['b'] = pct
            else:
                result[channel] = pct
        return result

    @staticmethod
    def waves(ops):
        return [op for op in ops
                if op.get('op') == 'trigger' and op.get('action') == 'waveform']

    @staticmethod
    def last_strength(ops):
        values = MixerEngineTests.strength(ops)
        return values

    # ---- startup / base -------------------------------------------

    def test_startup_reset(self):
        ops = self.drain()
        self.assertTrue(any(op.get('op') == 'trigger'
                            and op.get('strength_mode') == 'permanent'
                            and op.get('delta_pct') == -100 for op in ops))
        self.assertTrue(any(op.get('op') == 'set_strength'
                            and op.get('pct') == 0 for op in ops))

    def test_base_curve_and_set_strength_dedup(self):
        self.drain()
        self.begin(100.0)
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 10, 'b': 10})
        self.assertEqual(self.waves(ops), [])

        self.tick()
        self.assertEqual(self.drain(), [])

        self.feed(snapshot(50.0))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 25, 'b': 25})

    def test_base_channel_binding(self):
        plugin.plugin_cfg['base_channel'] = 'a'
        self.drain()
        self.begin(50.0)
        self.feed(snapshot(50.0))
        self.advance(0.1)
        self.tick()
        values = self.strength(self.drain())
        self.assertEqual(values, {'a': 25, 'b': 0})

    def test_total_strength_is_fixed_capped_at_100(self):
        plugin.plugin_cfg.update({
            'base_channel': 'a', 'base_max': 40,
            'hit_channel': 'a', 'hit_strength': 40,
            'fire_channel': 'a', 'fire_strength': 60,
            'fire_stack_add': 0,
        })
        self.drain()
        self.begin(0.0)
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(0.0, burning=True, fires=1))
        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        values = self.strength(self.drain())
        self.assertEqual(values, {'a': 100, 'b': 0})

    def test_set_strength_different_channels(self):
        plugin.plugin_cfg['fire_channel'] = 'a'
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 22, 'b': 10})

    # ---- hit ------------------------------------------------------

    def test_hit_threshold_scaling_and_expiry(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.advance(1.0)
        self.feed(damage(3000))  # 5% of 60000
        self.advance(0.4)
        self.tick()
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 35, 'b': 35})  # base 10 + 25
        state = self.engine.export_state()
        self.assertAlmostEqual(state['contributions']['hit']['a'], 25.0, places=1)
        self.assertGreater(state['hit_remaining'], 1.0)

        self.advance(2.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 10, 'b': 10})

    def test_hit_window_is_fixed_from_first_damage(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(3000))  # 5%
        self.advance(0.2)
        self.feed(damage(3000))  # still within the fixed 300ms window
        self.advance(0.09)       # 290ms after the first damage
        self.tick()
        state = self.engine.export_state()
        self.assertTrue(state['hit_window_open'])
        self.assertEqual(state['contributions']['hit']['a'], 0.0)

        self.advance(0.02)       # 310ms after the first damage
        self.tick()
        state = self.engine.export_state()
        self.assertFalse(state['hit_window_open'])
        self.assertAlmostEqual(state['contributions']['hit']['a'], 40.0, places=1)
        self.assertEqual(self.strength(self.drain()), {'a': 50, 'b': 50})

    def test_hit_window_cut_starts_a_new_window(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(3000))  # 5% -> 25
        self.advance(0.31)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 35, 'b': 35})
        first = self.engine.export_state()['contributions']['hit']['a']
        self.assertAlmostEqual(first, 25.0, places=1)

        self.advance(0.1)
        self.feed(damage(600))   # 1%: new window, below threshold
        self.advance(0.31)
        self.tick()
        state = self.engine.export_state()
        self.assertAlmostEqual(state['contributions']['hit']['a'], 25.0, places=1)

    def test_long_hit_waveform_uses_configured_duration(self):
        plugin.plugin_cfg.update({
            'hit_preset': 'HitW', 'hit_duration_s': 20.0,
        })
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all(op.get('duration_s') == 20.0 for op in waves))

        self.advance(5.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

    def test_long_death_waveform_uses_configured_duration(self):
        plugin.plugin_cfg.update({
            'death_preset': 'DeathW', 'death_duration_s': 20.0,
        })
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('alive', 'ended'))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all(op.get('duration_s') == 20.0 for op in waves))

        self.advance(5.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

        self.feed({'type': 'battle_end'})
        self.advance(5.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

    def test_base_waveform_uses_fixed_three_second_block(self):
        plugin.plugin_cfg.update({
            'base_preset': 'BaseW', 'base_channel': 'a',
        })
        self.drain()
        self.begin(100.0)
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0].get('duration_s'), 3.0)
        self.assertIn('基础', waves[0].get('label', ''))

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0].get('duration_s'), 3.0)

        plugin.plugin_cfg['base_enable'] = False
        self.advance(3.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

    def test_fire_and_flood_end_force_remaining_base_waveform(self):
        plugin.plugin_cfg.update({
            'base_preset': 'BaseW', 'base_channel': 'a',
            'fire_preset': 'FireW', 'fire_channel': 'a',
            'flood_preset': 'FloodW', 'flood_channel': 'a',
        })
        self.drain()
        self.begin(100.0)
        self.assertEqual(len(self.waves(self.drain())), 1)  # base loop

        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('起火', waves[0].get('label', ''))

        self.feed(status('burning', 'ended', 0))
        self.feed(snapshot(100.0, burning=False, fires=0))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('基础', waves[0].get('label', ''))

        self.feed(status('flooding', 'started', 1))
        self.feed(snapshot(100.0, flooding=True, floods=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('进水', waves[0].get('label', ''))

        self.feed(status('flooding', 'ended', 0))
        self.feed(snapshot(100.0, flooding=False, floods=0))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('基础', waves[0].get('label', ''))

    def test_v1_preset_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v1.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'evt_fire_enable': True,
                    'telemetry_path': r'C:\wrong\wows_telemetry.jsonl',
                    'base_min': 5,
                }, handle)
            before = dict(plugin.plugin_cfg)
            self.assertFalse(plugin.import_preset(path))
            self.assertEqual(plugin.plugin_cfg, before)

    def test_preset_import_ignores_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'preset.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'base_min': 7,
                    'preset_file_path': r'C:\other\preset.json',
                    'telemetry_path': r'C:\other\wows_telemetry.jsonl',
                }, handle)
            plugin.plugin_cfg['preset_file_path'] = 'keep-preset'
            plugin.plugin_cfg['telemetry_path'] = 'keep-telemetry'
            self.assertTrue(plugin.import_preset(path))
            self.assertEqual(plugin.plugin_cfg['preset_file_path'], 'keep-preset')
            self.assertEqual(plugin.plugin_cfg['telemetry_path'], 'keep-telemetry')
            self.assertEqual(plugin.plugin_cfg['base_min'], 7)

    def test_empty_preset_path_uses_bundled_preset(self):
        plugin.plugin_cfg['preset_file_path'] = ''
        self.drain()
        plugin.ensure_preset_path()
        self.assertEqual(plugin.plugin_cfg['preset_file_path'],
                         plugin.BUNDLED_PRESET_PATH)
        self.assertTrue(any(
            op.get('op') == 'set_config'
            and op.get('key') == 'preset_file_path'
            and op.get('value') == plugin.BUNDLED_PRESET_PATH
            for op in self.drain()
        ))

    def test_preset_export_skips_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'preset.json')
            plugin.plugin_cfg['base_min'] = 7
            self.assertTrue(plugin.export_preset(path))
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            self.assertNotIn('preset_file_path', data)
            self.assertNotIn('telemetry_path', data)
            self.assertEqual(data['base_min'], 7)

    def test_reader_read_errors_disconnect_and_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_telemetry.jsonl')
            open(path, 'wb').close()
            plugin.plugin_cfg['telemetry_path'] = path
            stat = os.stat(path)

            class FailingFile(object):
                def __init__(self):
                    self.closed = False

                def seek(self, *args):
                    raise OSError('broken reader')

                def read(self, *args):
                    raise OSError('broken reader')

                def close(self):
                    self.closed = True

            failing = FailingFile()
            reader = plugin.TelemetryReader(self.engine)
            reader.file = failing
            reader.path = path
            reader.ident = (getattr(stat, 'st_dev', 0),
                            getattr(stat, 'st_ino', 0))
            reader.cfg_key = plugin._configured_telemetry_path()

            for _ in range(5):
                reader.poll()

            self.assertIsNone(reader.file)
            self.assertTrue(failing.closed)

    def test_hit_window_waits_for_max_hp(self):
        self.engine.on_message(battle_start())
        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        state = self.engine.export_state()
        self.assertTrue(state['hit_window_open'])
        self.assertAlmostEqual(state['contributions']['hit']['a'], 0.0, places=1)

        self.feed(snapshot(100.0, max_hp=60000))
        self.advance(0.1)
        self.tick()
        state = self.engine.export_state()
        self.assertFalse(state['hit_window_open'])
        self.assertAlmostEqual(state['contributions']['hit']['a'], 40.0, places=1)

    def test_hit_duration_cap_at_20_seconds(self):
        plugin.plugin_cfg.update({
            'hit_preset': 'HitW', 'hit_duration_s': 30.0,
        })
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all(op.get('duration_s') == 20.0 for op in waves))
        self.assertLessEqual(self.engine.export_state()['hit_remaining'], 20.0)

    def test_death_duration_cap_at_20_seconds(self):
        plugin.plugin_cfg.update({
            'death_preset': 'DeathW', 'death_duration_s': 30.0,
        })
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('alive', 'ended'))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all(op.get('duration_s') == 20.0 for op in waves))
        self.assertLessEqual(self.engine.export_state()['death_remaining'], 20.0)

    def test_reader_skip_history_starts_after_last_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_telemetry.jsonl')
            complete = b'{"type":"battle_end"}\n'
            with open(path, 'wb') as handle:
                handle.write(complete + b'{"type":"snapshot"')
            reader = plugin.TelemetryReader(self.engine)
            try:
                self.assertTrue(reader._open_source(path, skip_history=True))
                self.assertEqual(reader.offset, len(complete))
            finally:
                reader._close_handle()

    def test_auto_cast_rejects_null_and_nonfinite(self):
        self.assertEqual(plugin.auto_cast('base_min', None),
                         plugin.SCHEMA_DEFAULTS['base_min'])
        self.assertEqual(plugin.auto_cast('base_min', 'nan'),
                         plugin.SCHEMA_DEFAULTS['base_min'])
        self.assertEqual(plugin.auto_cast('telemetry_path', None), '')
        self.assertEqual(plugin.auto_cast('hit_duration_s', 30), 20.0)

    def test_hit_curve_file_overrides_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'hit_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [
                    [0, 0], [2, 20], [8, 60], [100, 60],
                ]}, handle)
            plugin.plugin_cfg['hit_curve_enable'] = True
            self.engine.hit_curve_path = path
            self.drain()
            self.begin(100.0)
            self.drain()
            self.feed(damage(3000))  # 5% -> 40
            self.advance(0.4)
            self.tick()
            self.assertEqual(self.strength(self.drain()), {'a': 50, 'b': 50})
            self.assertAlmostEqual(
                self.engine.export_state()['contributions']['hit']['a'],
                40.0, places=1)

    def test_hit_curve_still_uses_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'hit_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[0, 0], [2, 20], [8, 60], [100, 60]]}, handle)
            plugin.plugin_cfg['hit_curve_enable'] = True
            self.engine.hit_curve_path = path
            self.drain()
            self.begin(100.0)
            self.drain()
            self.feed(damage(600))  # 1%, below threshold
            self.advance(0.4)
            self.tick()
            self.assertEqual(self.strength(self.drain()), {})
            self.assertEqual(
                self.engine.export_state()['contributions']['hit']['a'], 0.0)

    def test_invalid_hit_curve_falls_back_to_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'hit_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[100, 10]]}, handle)
            plugin.plugin_cfg['hit_curve_enable'] = True
            self.engine.hit_curve_path = path
            self.drain()
            self.begin(100.0)
            self.drain()
            self.feed(damage(3000))
            self.advance(0.4)
            self.tick()
            self.assertEqual(self.strength(self.drain()), {'a': 35, 'b': 35})

    def test_bundled_hit_curve_is_valid(self):
        points = plugin._load_curve_file(plugin.HIT_CURVE_PATH)
        self.assertGreaterEqual(len(points), 2)
        self.assertEqual(points[0][0], 100.0)
        self.assertEqual(points[-1][0], 0.0)

    def test_base_curve_file_overrides_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'base_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'version': 1, 'points': [
                    [100, 10], [75, 20], [50, 30], [25, 40], [0, 50],
                ]}, handle)
            plugin.plugin_cfg['base_curve_enable'] = True
            self.engine.base_curve_path = path
            self.drain()
            self.begin(100.0)
            self.assertEqual(self.strength(self.drain()), {'a': 10, 'b': 10})
            self.feed(snapshot(87.5))
            self.advance(0.1)
            self.tick()
            self.assertEqual(self.strength(self.drain()), {'a': 15, 'b': 15})

    def test_invalid_base_curve_falls_back_to_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'base_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[100, 10]]}, handle)
            plugin.plugin_cfg['base_curve_enable'] = True
            self.engine.base_curve_path = path
            self.drain()
            self.begin(50.0)
            self.assertEqual(self.strength(self.drain()), {'a': 25, 'b': 25})

    def test_base_curve_hot_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'base_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[100, 10], [0, 40]]}, handle)
            plugin.plugin_cfg['base_curve_enable'] = True
            self.engine.base_curve_path = path
            self.drain()
            self.begin(100.0)
            self.assertEqual(self.strength(self.drain()), {'a': 10, 'b': 10})
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[100, 90], [0, 90]]}, handle)
            os.utime(path, (2000000000, 2000000000))
            self.advance(2.1)
            self.tick()
            self.assertEqual(self.strength(self.drain()), {'a': 90, 'b': 90})

    def test_base_curve_validation(self):
        cases = [
            {'points': [[100, 10]]},
            {'points': [[100, 10], [0, 40], [0, 50]]},
            {'points': [[100, 10], [50, 120], [0, 40]]},
            {'points': [[100, 10], [75, 20]]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'base_curve.json')
            for data in cases:
                with open(path, 'w', encoding='utf-8') as handle:
                    json.dump(data, handle)
                with self.assertRaises(ValueError):
                    plugin._load_base_curve(path)

    def test_bundled_base_curve_is_valid(self):
        points = plugin._load_base_curve(plugin.BASE_CURVE_PATH)
        self.assertGreaterEqual(len(points), 2)
        self.assertEqual(points[0][0], 100.0)
        self.assertEqual(points[-1][0], 0.0)

    def test_base_curve_switch_off_uses_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'base_curve.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'points': [[100, 90], [0, 90]]}, handle)
            plugin.plugin_cfg['base_curve_enable'] = False
            self.engine.base_curve_path = path
            self.drain()
            self.begin(50.0)
            self.assertEqual(self.strength(self.drain()), {'a': 25, 'b': 25})

    def test_hit_below_threshold(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(600))  # 1%
        self.advance(0.4)
        self.tick()
        ops = self.drain()
        self.assertEqual(self.strength(ops), {})
        self.assertEqual(self.engine.export_state()['contributions']['hit']['a'], 0.0)

    def test_hit_full_strength_at_full_pct(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000))  # 10% >= 8%
        self.advance(0.4)
        self.tick()
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 50, 'b': 50})  # 10 + 40
        self.assertAlmostEqual(
            self.engine.export_state()['contributions']['hit']['a'], 40.0, places=1)

    def test_hit_shell_source_is_ignored(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000, source='shell'))
        self.advance(0.4)
        self.tick()
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.engine.export_state()['contributions']['hit']['a'], 0.0)

    def test_hit_refresh_keeps_larger_value(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        self.drain()
        self.advance(0.2)
        self.feed(damage(1200))  # 2% -> 10, lower than current 40
        self.advance(0.4)
        self.tick()
        self.drain()
        state = self.engine.export_state()
        self.assertAlmostEqual(state['contributions']['hit']['a'], 40.0, places=1)
        self.assertGreater(state['hit_remaining'], 1.7)

    # ---- fire / flood ---------------------------------------------

    def test_fire_stack_formula(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 22, 'b': 22})

        self.feed(count_change('burning', 2))
        self.feed(snapshot(100.0, burning=True, fires=2))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 28, 'b': 28})

        self.feed(count_change('burning', 4))
        self.feed(snapshot(100.0, burning=True, fires=4))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 40, 'b': 40})

    def test_fire_and_flood_end(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(status('flooding', 'started', 1))
        self.feed(snapshot(100.0, burning=True, flooding=True, fires=1, floods=1))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 34, 'b': 34})

        self.feed(status('burning', 'ended', 0))
        self.feed(snapshot(100.0, burning=False, flooding=True, fires=0, floods=1))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 22, 'b': 22})

    # ---- death ----------------------------------------------------

    def test_death_clears_and_expires(self):
        plugin.plugin_cfg['death_preset'] = 'DeathW'
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        self.drain()

        self.feed(status('alive', 'ended'))
        self.advance(0.1)
        self.tick()
        state = self.engine.export_state()
        self.assertTrue(state['dead'])
        self.assertAlmostEqual(state['contributions']['fire']['a'], 0.0, places=1)
        self.assertAlmostEqual(state['contributions']['death']['a'], 60.0, places=1)
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 60, 'b': 60})
        waves = self.waves(ops)
        self.assertTrue(any('死亡' in op.get('label', '') for op in waves))

        self.advance(4.1)
        self.tick()
        self.assertEqual(self.strength(self.drain()), {'a': 0, 'b': 0})
        self.assertEqual(self.engine.export_state()['contributions']['death']['a'], 0.0)

    # ---- waveforms ------------------------------------------------

    def test_waveform_priority_and_fallback(self):
        plugin.plugin_cfg.update({
            'base_preset': 'BaseW',
            'hit_preset': 'HitW', 'fire_preset': 'FireW',
            'flood_preset': 'FloodW', 'death_preset': 'DeathW',
        })
        self.drain()
        self.begin(100.0)
        # 基础波形在启动时立即进入循环
        self.assertEqual(len(self.waves(self.drain())), 2)

        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火' in op.get('label', '') for op in waves))

        self.feed(damage(6000))
        self.advance(0.4)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('受击' in op.get('label', '') for op in waves))

        self.advance(2.1)  # 受击结束，火重新成为赢家
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火' in op.get('label', '') for op in waves))

        self.feed(status('flooding', 'started', 1))
        self.feed(snapshot(100.0, burning=True, flooding=True, fires=1, floods=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('进水' in op.get('label', '') for op in waves))

        self.feed(status('flooding', 'ended', 0))
        self.feed(snapshot(100.0, burning=True, flooding=False, fires=1, floods=0))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火' in op.get('label', '') for op in waves))

    def test_fire_waveform_loops_while_burning(self):
        plugin.plugin_cfg.update({'fire_preset': 'FireW'})
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火' in op.get('label', '') for op in waves))

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火' in op.get('label', '') for op in waves))

        self.advance(3.1)
        self.tick()
        self.assertEqual(len(self.waves(self.drain())), 2)

        self.feed(status('burning', 'ended', 0))
        self.feed(snapshot(100.0, burning=False, fires=0))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])
        self.advance(3.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

    def test_base_waveform_loops_and_resumes_after_fire(self):
        plugin.plugin_cfg.update({
            'base_preset': 'BaseW', 'base_channel': 'a',
            'fire_preset': 'FireW', 'fire_channel': 'a',
        })
        self.drain()
        self.begin(100.0)
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('基础', waves[0].get('label', ''))

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0].get('duration_s'), 3.0)

        # 起火覆盖基础，基础循环暂停
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        self.drain()

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('起火', waves[0].get('label', ''))

        # 灭火后基础重新成为赢家，立即恢复循环
        self.feed(status('burning', 'ended', 0))
        self.feed(snapshot(100.0, burning=False, fires=0))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 1)
        self.assertIn('基础', waves[0].get('label', ''))

    def test_adoption_does_not_fire_existing_fire_waveform(self):
        plugin.plugin_cfg.update({'fire_preset': 'FireW'})
        self.drain()
        # 不发送 battle_start，直接接入一个正在燃烧的中局
        self.feed(snapshot(50.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        ops = self.drain()
        self.assertEqual(self.waves(ops), [])
        state = self.engine.export_state()
        self.assertTrue(state['battle_active'])
        self.assertEqual(state['wave']['a'], 'fire')
        self.assertAlmostEqual(state['contributions']['fire']['a'], 12.0, places=1)

    # ---- end / timeout --------------------------------------------

    def test_flood_waveform_loops_while_flooding(self):
        plugin.plugin_cfg.update({'flood_preset': 'FloodW'})
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('flooding', 'started', 1))
        self.feed(snapshot(100.0, flooding=True, floods=1))
        self.advance(0.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('进水' in op.get('label', '') for op in waves))

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('进水' in op.get('label', '') for op in waves))

        self.feed(status('flooding', 'ended', 0))
        self.feed(snapshot(100.0, flooding=False, floods=0))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

    def test_fire_stack_does_not_retrigger_waveform(self):
        plugin.plugin_cfg.update({'fire_preset': 'FireW'})
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        self.drain()

        self.feed(count_change('burning', 2))
        self.feed(snapshot(100.0, burning=True, fires=2))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('起火 x2' in op.get('label', '') for op in waves))

    def test_flood_stack_does_not_retrigger_waveform(self):
        plugin.plugin_cfg.update({'flood_preset': 'FloodW'})
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(status('flooding', 'started', 1))
        self.feed(snapshot(100.0, flooding=True, floods=1))
        self.advance(0.1)
        self.tick()
        self.drain()

        self.feed(count_change('flooding', 2))
        self.feed(snapshot(100.0, flooding=True, floods=2))
        self.advance(0.1)
        self.tick()
        self.assertEqual(self.waves(self.drain()), [])

        self.advance(3.1)
        self.tick()
        waves = self.waves(self.drain())
        self.assertEqual(len(waves), 2)
        self.assertTrue(all('进水 x2' in op.get('label', '') for op in waves))

    def test_death_clears_pending_hit_window(self):
        plugin.plugin_cfg.update({
            'hit_preset': 'HitW', 'death_preset': 'DeathW',
        })
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed(damage(6000))
        self.advance(0.1)
        self.feed(status('alive', 'ended'))
        self.advance(0.1)
        self.tick()
        state = self.engine.export_state()
        self.assertFalse(state['hit_window_open'])
        self.assertAlmostEqual(state['contributions']['hit']['a'], 0.0, places=1)
        waves = self.waves(self.drain())
        self.assertTrue(any('死亡' in op.get('label', '') for op in waves))
        self.assertFalse(any('受击' in op.get('label', '') for op in waves))

    def test_v2_preset_imports_schema_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v2.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'base_min': 7,
                    'hit_enable': False,
                    'hit_duration_s': 20,
                }, handle)
            self.assertTrue(plugin.import_preset(path))
            self.assertEqual(plugin.plugin_cfg['base_min'], 7)
            self.assertFalse(plugin.plugin_cfg['hit_enable'])
            self.assertEqual(plugin.plugin_cfg['hit_duration_s'], 20)

    def test_battle_end_and_timeout_zero(self):
        self.drain()
        self.begin(100.0)
        self.drain()
        self.feed({'type': 'battle_end'})
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 0, 'b': 0})
        self.assertFalse(self.engine.export_state()['battle_active'])

        self.begin(100.0)
        self.drain()
        self.engine.last_msg_time = self.clock[0] - 100
        self.tick()
        ops = self.drain()
        self.assertEqual(self.strength(ops), {'a': 0, 'b': 0})
        self.assertFalse(self.engine.export_state()['battle_active'])

    def test_export_state_shape(self):
        plugin.plugin_cfg.update({'fire_preset': 'FireW'})
        self.drain()
        self.begin(100.0)
        self.feed(status('burning', 'started', 1))
        self.feed(snapshot(100.0, burning=True, fires=1))
        self.advance(0.1)
        self.tick()
        state = self.engine.export_state()
        self.assertIn('contributions', state)
        self.assertIn('totals', state)
        self.assertIn('caps', state)
        self.assertIn('wave', state)
        self.assertIn('base_next', state)
        self.assertEqual(state['caps'], {'a': 100, 'b': 100})
        self.assertAlmostEqual(state['contributions']['fire']['a'], 12.0, places=1)


class LoggingAndWatchdogTests(unittest.TestCase):

    def setUp(self):
        self.root = logging.getLogger()
        self.old_handlers = self.root.handlers[:]
        self.old_flag = getattr(
            self.root, '_wows_coyote_logging_configured', None)
        self.old_raise = logging.raiseExceptions
        try:
            delattr(self.root, '_wows_coyote_logging_configured')
        except AttributeError:
            pass
        plugin._watchdog_deactivate()
        plugin._RUNTIME_LOG_PATH = None

    def tearDown(self):
        self.root.handlers[:] = self.old_handlers
        if self.old_flag is None:
            try:
                delattr(self.root, '_wows_coyote_logging_configured')
            except AttributeError:
                pass
        else:
            setattr(self.root, '_wows_coyote_logging_configured',
                    self.old_flag)
        logging.raiseExceptions = self.old_raise
        plugin._watchdog_deactivate()
        plugin._RUNTIME_LOG_PATH = None

    def test_dghub_spawn_logs_to_file_not_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_coyote.log')
            with mock.patch.dict(
                    os.environ, {'DGHUB_TOKEN': 'test-token'}, clear=False), \
                    mock.patch.object(
                        plugin, '_runtime_log_path', return_value=path):
                plugin.setup_logging()
                handlers = self.root.handlers
                self.assertTrue(any(
                    isinstance(handler, logging.FileHandler)
                    for handler in handlers))
                self.assertFalse(any(
                    isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)
                    for handler in handlers))
                logging.info('log-probe')
                for handler in handlers:
                    handler.flush()
                for handler in handlers:
                    if isinstance(handler, logging.FileHandler):
                        handler.close()
                self.root.handlers[:] = self.old_handlers
            with open(path, encoding='utf-8') as handle:
                self.assertIn('log-probe', handle.read())

    def test_watchdog_detects_stalled_loop(self):
        with mock.patch.object(plugin.time, 'monotonic', return_value=100.0):
            plugin._watchdog_activate()
        self.assertFalse(plugin._watchdog_stale(
            100.0 + plugin.WATCHDOG_TIMEOUT_S - 0.1))
        self.assertTrue(plugin._watchdog_stale(
            100.0 + plugin.WATCHDOG_TIMEOUT_S + 0.1))
        with mock.patch.object(plugin.time, 'monotonic', return_value=105.0):
            plugin._watchdog_beat()
        self.assertFalse(plugin._watchdog_stale(
            105.0 + plugin.WATCHDOG_TIMEOUT_S))
        self.assertTrue(plugin._watchdog_stale(
            105.0 + plugin.WATCHDOG_TIMEOUT_S + 0.1))


class TelemetryReaderTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'wows_telemetry.jsonl')
        open(self.path, 'wb').close()
        self.collector = Collector()
        plugin.send_queue = self.collector
        plugin.plugin_cfg = dict(plugin.SCHEMA_DEFAULTS)
        plugin.plugin_cfg.update({
            'telemetry_path': self.path,
        })
        self.clock = [1000.0]
        self.engine = plugin.MixerEngine(now_fn=lambda: self.clock[0])
        self.reader = plugin.TelemetryReader(self.engine)
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.assertTrue(self.reader._open_source(self.path, skip_history=False))

    def tearDown(self):
        self.reader._close_handle()
        self.tmp.cleanup()

    def write(self, data):
        with open(self.path, 'ab') as handle:
            handle.write(data)

    def reopen(self):
        self.reader._close_handle()
        self.assertTrue(self.reader._open_source(self.path, skip_history=False))

    def test_partial_line_waits_for_newline(self):
        line = json.dumps(battle_start('Partial')).encode('utf-8')
        self.write(line)
        self.reader.poll()
        self.assertFalse(self.engine.battle_active)

        self.write(b'\n')
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_bom_first_line_is_parsed(self):
        message = json.dumps(battle_start('BOM')).encode('utf-8') + b'\n'
        with open(self.path, 'wb') as handle:
            handle.write(b'\xef\xbb\xbf' + message)
        self.reopen()
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_truncation_ends_battle_and_can_reconnect(self):
        messages = (
            json.dumps(battle_start('Truncated')).encode('utf-8') + b'\n'
            + json.dumps(snapshot(100.0)).encode('utf-8') + b'\n'
        )
        self.write(messages)
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

        open(self.path, 'wb').close()
        self.reader.poll()
        self.assertFalse(self.engine.battle_active)

        self.write(json.dumps(battle_start('Again')).encode('utf-8') + b'\n')
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_malformed_line_is_skipped(self):
        self.write(
            b'not-json\n'
            + json.dumps(battle_start('AfterBad')).encode('utf-8') + b'\n'
        )
        self.reader.poll()
        self.assertGreaterEqual(self.reader.malformed, 1)
        self.assertTrue(self.engine.battle_active)

    def test_directory_input_resolves_game_root_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            profile = os.path.join(root, 'profile')
            os.makedirs(profile)
            path = os.path.join(profile, 'wows_telemetry.jsonl')
            open(path, 'wb').close()

            plugin.plugin_cfg['telemetry_path'] = root
            self.assertEqual(plugin._configured_telemetry_path(), path)
            plugin.plugin_cfg['telemetry_path'] = profile
            self.assertEqual(plugin._configured_telemetry_path(), path)
            plugin.plugin_cfg['telemetry_path'] = path
            self.assertEqual(plugin._configured_telemetry_path(), path)

    def test_auto_find_fills_empty_telemetry_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_telemetry.jsonl')
            open(path, 'wb').close()
            plugin.plugin_cfg['telemetry_path'] = ''
            collector = Collector()
            plugin.send_queue = collector
            engine = plugin.MixerEngine(now_fn=lambda: 1000.0)
            reader = plugin.TelemetryReader(engine)
            reader.last_probe = 0.0
            with mock.patch.object(plugin, 'find_telemetry_path', return_value=path):
                self.assertTrue(reader._ensure_source(1000.0))
            self.assertEqual(plugin.plugin_cfg['telemetry_path'], path)
            self.assertEqual(reader.cfg_key, path)
            ops = collector.drain()
            self.assertTrue(any(
                op.get('op') == 'set_config'
                and op.get('key') == 'telemetry_path'
                and op.get('value') == path
                for op in ops
            ))
            reader._close_handle()

    def test_telemetry_path_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_telemetry.jsonl')
            open(path, 'wb').close()
            self.assertTrue(plugin._telemetry_path_is_valid(path))
            self.assertFalse(plugin._telemetry_path_is_valid(
                os.path.join(tmp, 'missing.jsonl')))
            self.assertFalse(plugin._telemetry_path_is_valid(tmp))

    def test_auto_find_replaces_invalid_telemetry_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wows_telemetry.jsonl')
            open(path, 'wb').close()
            plugin.plugin_cfg['telemetry_path'] = os.path.join(tmp, 'missing.jsonl')
            collector = Collector()
            plugin.send_queue = collector
            engine = plugin.MixerEngine(now_fn=lambda: 1000.0)
            reader = plugin.TelemetryReader(engine)
            reader.cfg_key = plugin._configured_telemetry_path()
            reader.last_probe = 0.0
            with mock.patch.object(plugin, 'find_telemetry_path', return_value=path):
                self.assertTrue(reader._ensure_source(1000.0))
            self.assertEqual(plugin.plugin_cfg['telemetry_path'], path)
            self.assertEqual(reader.cfg_key, path)
            reader._close_handle()

    def test_reads_large_file_in_chunks(self):
        message = json.dumps(battle_start('X' * 300)).encode('utf-8') + b'\n'
        self.write(message)
        old_limit = plugin.MAX_READ_BYTES
        plugin.MAX_READ_BYTES = 64
        try:
            for _ in range(20):
                self.reader.poll()
                if self.engine.battle_active:
                    break
            self.assertTrue(self.engine.battle_active)
            self.assertGreater(self.reader.offset, 64)
        finally:
            plugin.MAX_READ_BYTES = old_limit


class _FakeRegistryKey(object):
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeWinreg(object):
    HKEY_CURRENT_USER = 'HKCU'
    HKEY_LOCAL_MACHINE = 'HKLM'

    def __init__(self, values_by_key):
        self.values_by_key = values_by_key

    def OpenKey(self, hive, name):
        values = self.values_by_key.get((hive, name))
        if values is None:
            raise OSError('missing registry key')
        return _FakeRegistryKey(values)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError('missing registry value')
        return key.values[name], 1


class _PathTestBase(unittest.TestCase):
    """隔离插件路径测试会修改的全局配置、发送队列和候选缓存。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = os.path.join(
            self.tmpdir.name, '游戏 目录', 'World of Warships')
        os.makedirs(self.root)

        self.collector = Collector()
        self._old_queue = plugin.send_queue
        plugin.send_queue = self.collector
        self.addCleanup(self._restore_queue)

        self._old_cfg = plugin.plugin_cfg
        plugin.plugin_cfg = dict(plugin.SCHEMA_DEFAULTS)
        self.addCleanup(self._restore_cfg)

        self._saved_cache = (plugin._auto_cache, plugin._auto_cache_at)
        plugin._auto_cache = []
        plugin._auto_cache_at = 0.0
        self.addCleanup(self._restore_cache)

    def _restore_queue(self):
        plugin.send_queue = self._old_queue

    def _restore_cfg(self):
        plugin.plugin_cfg = self._old_cfg

    def _restore_cache(self):
        plugin._auto_cache, plugin._auto_cache_at = self._saved_cache

    def make_file(self, *parts, content=b''):
        path = os.path.join(self.root, *parts)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, 'wb') as handle:
            handle.write(content)
        return path

    def make_dir(self, *parts):
        path = os.path.join(self.root, *parts)
        os.makedirs(path)
        return path


class PathDiscoveryTests(_PathTestBase):

    def _auto_candidates(self, env=None, drives=(), steam=(), wgc=()):
        plugin._auto_cache = []
        plugin._auto_cache_at = 0.0

        def fake_exists(path):
            return path in drives

        with mock.patch.dict(plugin.os.environ, env or {}, clear=True), \
                mock.patch.object(plugin.os.path, 'exists',
                                  side_effect=fake_exists), \
                mock.patch.object(plugin, '_steam_library_roots',
                                  return_value=set(steam)), \
                mock.patch.object(plugin, '_wgc_roots',
                                  return_value=list(wgc)):
            return plugin._auto_candidates()

    def test_clean_path_strips_quotes_and_whitespace(self):
        expected = os.path.normpath(r'E:\游戏 目录\预设 文件.json')
        self.assertEqual(
            plugin._clean_path('  "E:\\游戏 目录\\预设 文件.json"  '),
            expected)
        self.assertEqual(
            plugin._clean_path("'E:\\游戏 目录\\预设 文件.json'"),
            expected)
        self.assertEqual(plugin._clean_path(''), '')
        self.assertEqual(plugin._clean_path(None), '')

    def test_clean_path_expands_environment_variables(self):
        root = r'E:\战游\根 目录'
        with mock.patch.dict(plugin.os.environ,
                             {'WOWS_TEST_ROOT': root}, clear=False):
            actual = plugin._clean_path(
                r'%WOWS_TEST_ROOT%\profile\遥测.jsonl')
        self.assertEqual(
            actual,
            os.path.normpath(os.path.join(root, 'profile', '遥测.jsonl')))

    def test_clean_path_expands_user_home(self):
        home = os.path.join(self.tmpdir.name, '用户 目录')
        os.makedirs(home)
        with mock.patch.dict(
                plugin.os.environ,
                {'USERPROFILE': home, 'HOME': home}, clear=False):
            actual = plugin._clean_path(r'~\战游\预设.json')
        self.assertEqual(
            actual,
            os.path.normpath(os.path.join(home, '战游', '预设.json')))

    def test_profile_path_from_root_handles_files_profile_and_bin(self):
        telemetry = self.make_file('profile', plugin.TELEMETRY_NAME)
        self.assertEqual(
            plugin._profile_path_from_root(telemetry),
            os.path.normpath(telemetry))

        upper = self.make_file('profile-大写', 'WOWS_TELEMETRY.JSONL')
        self.assertEqual(
            plugin._profile_path_from_root(upper),
            os.path.normpath(upper))

        other = self.make_file('profile', '其他.txt')
        self.assertEqual(
            plugin._profile_path_from_root(other),
            os.path.join(os.path.dirname(other), plugin.TELEMETRY_NAME))

        profile_dir = self.make_dir('大写档', 'PROFILE')
        self.assertEqual(
            plugin._profile_path_from_root(profile_dir),
            os.path.join(profile_dir, plugin.TELEMETRY_NAME))

        bin_dir = self.make_dir('Bin', 'x64')
        self.assertEqual(
            plugin._profile_path_from_root(bin_dir),
            os.path.join(self.root, 'profile', plugin.TELEMETRY_NAME))

        plain = self.make_dir('普通根')
        self.assertEqual(
            plugin._profile_path_from_root(plain),
            os.path.join(plain, 'profile', plugin.TELEMETRY_NAME))
        self.assertEqual(plugin._profile_path_from_root(''), '')

    def test_resolve_telemetry_path_variants(self):
        direct_root = self.make_dir('直连根')
        direct = self.make_file('直连根', plugin.TELEMETRY_NAME)
        profile_root = self.make_dir('档根')
        profile_file = self.make_file(
            '档根', 'profile', plugin.TELEMETRY_NAME)
        empty_profile_root = self.make_dir('空档根')
        self.make_dir('空档根', 'profile')
        bin_root = self.make_dir('Bin根')
        self.make_dir('Bin根', 'bin', 'x64')
        upper_bin_root = self.make_dir('大写根')
        self.make_dir('大写根', 'BIN', 'x64')
        empty_root = self.make_dir('空根')
        missing = os.path.join(self.root, '不存在', '遥测.jsonl')

        self.assertEqual(plugin._resolve_telemetry_path(direct_root), direct)
        self.assertEqual(plugin._resolve_telemetry_path(profile_root),
                         profile_file)
        self.assertEqual(plugin._resolve_telemetry_path(profile_file),
                         profile_file)
        self.assertEqual(plugin._resolve_telemetry_path(
            os.path.join(profile_root, 'profile')), profile_file)
        self.assertEqual(
            plugin._resolve_telemetry_path(empty_profile_root),
            os.path.join(empty_profile_root, 'profile',
                         plugin.TELEMETRY_NAME))
        self.assertEqual(
            plugin._resolve_telemetry_path(
                os.path.join(empty_profile_root, 'profile')),
            os.path.join(empty_profile_root, 'profile',
                         plugin.TELEMETRY_NAME))
        self.assertEqual(
            plugin._resolve_telemetry_path(
                os.path.join(bin_root, 'bin', 'x64')),
            os.path.join(bin_root, 'profile', plugin.TELEMETRY_NAME))
        self.assertEqual(
            plugin._resolve_telemetry_path(
                os.path.join(upper_bin_root, 'BIN', 'x64')),
            os.path.join(upper_bin_root, 'profile', plugin.TELEMETRY_NAME))
        self.assertEqual(
            plugin._resolve_telemetry_path(empty_root),
            os.path.join(empty_root, plugin.TELEMETRY_NAME))
        self.assertEqual(plugin._resolve_telemetry_path(missing), missing)
        self.assertEqual(plugin._resolve_telemetry_path(''), '')
        self.assertEqual(plugin._resolve_telemetry_path(None), '')
        self.assertEqual(
            plugin._resolve_telemetry_path('  "{}"  '.format(profile_root)),
            profile_file)

    def test_telemetry_path_is_valid_with_chinese_and_oserror(self):
        valid = self.make_file('有效', '遥测 文件.jsonl')
        missing = os.path.join(self.root, '有效', '缺失.jsonl')
        directory = self.make_dir('有效', '目录')

        self.assertTrue(plugin._telemetry_path_is_valid(valid))
        self.assertFalse(plugin._telemetry_path_is_valid(missing))
        self.assertFalse(plugin._telemetry_path_is_valid(directory))
        self.assertFalse(plugin._telemetry_path_is_valid(''))
        with mock.patch('builtins.open',
                        side_effect=OSError('拒绝访问')):
            self.assertFalse(plugin._telemetry_path_is_valid(valid))

    def test_find_telemetry_path_prefers_valid_manual_path(self):
        manual = self.make_file('手动', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = manual
        with mock.patch.object(plugin, '_auto_candidates') as candidates:
            self.assertEqual(plugin.find_telemetry_path(), manual)
        candidates.assert_not_called()

    def test_find_telemetry_path_returns_none_without_candidates(self):
        plugin.plugin_cfg['telemetry_path'] = ''
        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[]):
            self.assertIsNone(plugin.find_telemetry_path())

    def test_find_telemetry_path_filters_invalid_candidates(self):
        valid = self.make_file('自动', '遥测.jsonl', content=b'\n')
        missing = os.path.join(self.root, '自动', '缺失.jsonl')
        plugin.plugin_cfg['telemetry_path'] = os.path.join(
            self.root, '无效配置.jsonl')
        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[missing, valid]):
            self.assertEqual(plugin.find_telemetry_path(), valid)

    def test_find_telemetry_path_skips_empty_candidates(self):
        empty = self.make_file('自动', '空', '遥测.jsonl')
        written = self.make_file('自动', '有数据', '遥测.jsonl',
                                 content=b'\n')
        plugin.plugin_cfg['telemetry_path'] = ''

        def fake_mtime(path):
            return 200.0 if path == empty else 100.0

        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[empty, written]), \
                mock.patch.object(plugin.os.path, 'getmtime',
                                  side_effect=fake_mtime):
            self.assertEqual(plugin.find_telemetry_path(), written)

    def test_find_telemetry_path_returns_none_when_all_candidates_empty(self):
        first = self.make_file('自动', '一', '遥测.jsonl')
        second = self.make_file('自动', '二', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = ''
        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[first, second]):
            self.assertIsNone(plugin.find_telemetry_path())

    def test_find_telemetry_path_chooses_newest_candidate(self):
        first = self.make_file('自动', '一', '遥测.jsonl', content=b'\n')
        second = self.make_file('自动', '二', '遥测.jsonl', content=b'\n')
        plugin.plugin_cfg['telemetry_path'] = ''

        def fake_mtime(path):
            return 100.0 if path == first else 200.0

        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[first, second]), \
                mock.patch.object(plugin.os.path, 'getmtime',
                                  side_effect=fake_mtime):
            self.assertEqual(plugin.find_telemetry_path(), second)

    def test_find_telemetry_path_falls_back_when_mtime_fails(self):
        first = self.make_file('自动', '一', '遥测.jsonl', content=b'\n')
        second = self.make_file('自动', '二', '遥测.jsonl', content=b'\n')
        plugin.plugin_cfg['telemetry_path'] = ''
        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[first, second]), \
                mock.patch.object(plugin.os.path, 'getmtime',
                                  side_effect=OSError('时间不可用')):
            self.assertEqual(plugin.find_telemetry_path(), first)

    def test_auto_candidates_include_environment_roots(self):
        for env_name in ('WOWS_DIR', 'WOWS_ROOT',
                         'WORLD_OF_WARSHIPS_DIR'):
            with self.subTest(env_name=env_name):
                root = os.path.join(self.root, '环境', env_name)
                paths = self._auto_candidates({env_name: root})
                expected = os.path.normpath(os.path.join(
                    root, 'profile', plugin.TELEMETRY_NAME))
                self.assertIn(expected, paths)

    def test_auto_candidates_accept_file_and_profile_environment_values(self):
        telemetry = self.make_file('环境', plugin.TELEMETRY_NAME)
        profile = self.make_dir('环境', 'profile')

        file_paths = self._auto_candidates({'WOWS_DIR': telemetry})
        self.assertEqual(file_paths[0], os.path.normpath(telemetry))

        profile_paths = self._auto_candidates({'WOWS_DIR': profile})
        self.assertEqual(
            profile_paths[0],
            os.path.join(profile, plugin.TELEMETRY_NAME))

    def test_auto_candidates_include_drive_suffixes(self):
        paths = self._auto_candidates({}, drives=('C:\\',))
        expected = os.path.normpath(os.path.join(
            'C:\\', 'World_of_Warships', 'profile',
            plugin.TELEMETRY_NAME))
        nested = os.path.normpath(os.path.join(
            'C:\\', 'Games', 'World of Warships', 'profile',
            plugin.TELEMETRY_NAME))
        self.assertIn(expected, paths)
        self.assertIn(nested, paths)

    def test_auto_candidates_merge_steam_and_wgc_roots(self):
        steam_root = r'E:\Steam库\中文'
        wgc_root = r'D:\游戏\Wargaming\World_of_Warships'
        paths = self._auto_candidates(
            {}, steam=[steam_root], wgc=[wgc_root])
        self.assertIn(
            os.path.normpath(os.path.join(
                steam_root, 'steamapps', 'common', 'World of Warships',
                'profile', plugin.TELEMETRY_NAME)),
            paths)
        self.assertIn(
            os.path.normpath(os.path.join(
                wgc_root, 'profile', plugin.TELEMETRY_NAME)),
            paths)

    def test_auto_candidates_ignore_drive_probe_errors(self):
        plugin._auto_cache = []
        plugin._auto_cache_at = 0.0
        with mock.patch.dict(plugin.os.environ, {}, clear=True), \
                mock.patch.object(plugin.os.path, 'exists',
                                  side_effect=OSError('盘符不可用')), \
                mock.patch.object(plugin, '_steam_library_roots',
                                  return_value=set()), \
                mock.patch.object(plugin, '_wgc_roots',
                                  return_value=[]):
            self.assertEqual(plugin._auto_candidates(), [])

    def test_auto_candidates_skip_empty_roots(self):
        self.assertEqual(self._auto_candidates({}, wgc=['']), [])

    def test_auto_candidates_dedupe_is_case_insensitive(self):
        paths = self._auto_candidates(
            {'WOWS_DIR': r'c:\games\world of warships'},
            drives=('C:\\',))
        expected = os.path.normpath(os.path.join(
            'C:\\', 'Games', 'World of Warships', 'profile',
            plugin.TELEMETRY_NAME)).lower()
        self.assertEqual(
            sum(1 for path in paths if path.lower() == expected), 1)
        self.assertEqual(len(paths), len({path.lower() for path in paths}))

    def test_auto_candidates_cache_is_copy_and_expires(self):
        plugin._auto_cache = ['缓存\\遥测.jsonl']
        plugin._auto_cache_at = 1000.0
        with mock.patch.object(plugin.time, 'monotonic',
                               return_value=1010.0):
            cached = plugin._auto_candidates()
        self.assertEqual(cached, ['缓存\\遥测.jsonl'])
        self.assertIsNot(cached, plugin._auto_cache)
        cached.append('被调用方修改')
        self.assertEqual(plugin._auto_cache, ['缓存\\遥测.jsonl'])

        now = 1000.0 + plugin.AUTO_CACHE_SEC + 1.0
        with mock.patch.object(plugin.time, 'monotonic',
                               return_value=now), \
                mock.patch.dict(plugin.os.environ, {}, clear=True), \
                mock.patch.object(plugin.os.path, 'exists',
                                  return_value=False), \
                mock.patch.object(plugin, '_steam_library_roots',
                                  return_value=set()), \
                mock.patch.object(plugin, '_wgc_roots',
                                  return_value=[]):
            refreshed = plugin._auto_candidates()
        self.assertEqual(refreshed, [])
        self.assertEqual(plugin._auto_cache, [])
        self.assertEqual(plugin._auto_cache_at, now)

    def test_steam_library_roots_parse_chinese_and_escaped_paths(self):
        vdf = (
            '"libraryfolders"\n'
            '{\n'
            '    "0"\n'
            '    {\n'
            '        "path" "C:\\\\Program Files (x86)\\\\Steam"\n'
            '    }\n'
            '    "1"\n'
            '    {\n'
            '        "path" "D:\\\\SteamLibrary\\\\中文 游戏库"\n'
            '    }\n'
            '}\n'
        )
        with mock.patch('builtins.open', mock.mock_open(read_data=vdf)):
            roots = plugin._steam_library_roots()
        self.assertIn(r'C:\Program Files (x86)\Steam', roots)
        self.assertIn(r'D:\SteamLibrary\中文 游戏库', roots)

    def test_steam_library_roots_ignore_read_errors(self):
        with mock.patch('builtins.open',
                        side_effect=OSError('拒绝访问')):
            self.assertEqual(plugin._steam_library_roots(), set())

    def test_wgc_roots_read_registry_values_with_chinese(self):
        fake = _FakeWinreg({
            (_FakeWinreg.HKEY_CURRENT_USER,
             r'Software\Wargaming.net\GameCenter\wows'):
                {'path': r'E:\游戏\World of Warships'},
            (_FakeWinreg.HKEY_LOCAL_MACHINE,
             r'SOFTWARE\WOW6432Node\Wargaming.net\GameCenter\wows'):
                {'install_path': r'D:\战舰世界',
                 'Path': r'D:\战舰世界\bin'},
        })
        with mock.patch.dict(sys.modules, {'winreg': fake}):
            roots = plugin._wgc_roots()
        self.assertIn(r'E:\游戏\World of Warships', roots)
        self.assertIn(r'D:\战舰世界', roots)
        self.assertIn(r'D:\战舰世界\bin', roots)

    def test_wgc_roots_return_empty_without_winreg(self):
        with mock.patch.dict(sys.modules, {'winreg': None}):
            self.assertEqual(plugin._wgc_roots(), [])

    def test_ensure_preset_path_does_not_overwrite_configured_path(self):
        preset = os.path.join(self.root, '我的 预设.json')
        plugin.plugin_cfg['preset_file_path'] = preset
        plugin.ensure_preset_path()
        self.assertEqual(plugin.plugin_cfg['preset_file_path'], preset)
        self.assertEqual(self.collector.drain(), [])

    def test_ensure_preset_path_fills_chinese_bundled_preset(self):
        bundled = self.make_file('内置', '预设 文件.json')
        plugin.plugin_cfg['preset_file_path'] = ''
        with mock.patch.object(plugin, 'BUNDLED_PRESET_PATH', bundled):
            plugin.ensure_preset_path()
        self.assertEqual(plugin.plugin_cfg['preset_file_path'], bundled)
        ops = self.collector.drain()
        self.assertTrue(any(
            op.get('op') == 'set_config'
            and op.get('key') == 'preset_file_path'
            and op.get('value') == bundled
            for op in ops
        ))

    def test_ensure_preset_path_leaves_empty_when_bundled_missing(self):
        missing = os.path.join(self.root, '缺失', '预设.json')
        plugin.plugin_cfg['preset_file_path'] = ''
        with mock.patch.object(plugin, 'BUNDLED_PRESET_PATH', missing):
            plugin.ensure_preset_path()
        self.assertEqual(plugin.plugin_cfg['preset_file_path'], '')
        self.assertEqual(self.collector.drain(), [])


class TelemetryAutoPathTests(_PathTestBase):

    def setUp(self):
        super().setUp()
        self.engine = plugin.MixerEngine(now_fn=lambda: 1000.0)
        self.reader = plugin.TelemetryReader(self.engine)
        self.addCleanup(self.reader._close_handle)

    def _telemetry_ops(self):
        return [
            op for op in self.collector.drain()
            if op.get('op') == 'set_config'
            and op.get('key') == 'telemetry_path'
        ]

    def test_auto_find_fills_empty_path_with_chinese(self):
        path = self.make_file('中文 目录', 'profile',
                              plugin.TELEMETRY_NAME)
        plugin.plugin_cfg['telemetry_path'] = ''
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, 'find_telemetry_path',
                               return_value=path):
            self.assertTrue(self.reader._ensure_source(1000.0))
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], path)
        self.assertEqual(self.reader.cfg_key, path)
        self.assertEqual(self.reader.path, path)
        self.assertTrue(os.path.isfile(self.reader.path))
        ops = self._telemetry_ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]['value'], path)

    def test_auto_find_replaces_invalid_path_with_chinese(self):
        path = self.make_file('中文 目录', 'profile',
                              plugin.TELEMETRY_NAME)
        plugin.plugin_cfg['telemetry_path'] = os.path.join(
            self.root, '不存在', '遥测.jsonl')
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, 'find_telemetry_path',
                               return_value=path):
            self.assertTrue(self.reader._ensure_source(1000.0))
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], path)
        self.assertEqual(self.reader.cfg_key, path)
        self.assertEqual(self.reader.path, path)
        self.assertEqual(self._telemetry_ops()[0]['value'], path)

    def test_auto_find_through_real_candidates_with_chinese(self):
        game_root = self.make_dir('真实 游戏根')
        telemetry = self.make_file(
            '真实 游戏根', 'profile', plugin.TELEMETRY_NAME,
            content=b'\n')
        plugin.plugin_cfg['telemetry_path'] = ''
        plugin._auto_cache = []
        plugin._auto_cache_at = 0.0
        self.reader.last_probe = 0.0

        with mock.patch.dict(plugin.os.environ,
                             {'WOWS_DIR': game_root}, clear=True), \
                mock.patch.object(plugin.os.path, 'exists',
                                  return_value=False), \
                mock.patch.object(plugin, '_steam_library_roots',
                                  return_value=set()), \
                mock.patch.object(plugin, '_wgc_roots',
                                  return_value=[]):
            self.assertTrue(self.reader._ensure_source(1000.0))

        self.assertEqual(plugin.plugin_cfg['telemetry_path'], telemetry)
        self.assertEqual(self.reader.path, telemetry)
        self.assertEqual(self._telemetry_ops()[0]['value'], telemetry)

        with open(telemetry, 'ab') as handle:
            handle.write(json.dumps(
                battle_start('中文舰船')).encode('utf-8') + b'\n')
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_auto_waits_for_a_candidate_to_receive_data(self):
        stale = self.make_file('自动', '旧', '遥测.jsonl')
        actual = self.make_file('自动', '实际', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = ''
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[stale, actual]):
            self.assertFalse(self.reader._ensure_source(1000.0))
            self.assertIsNone(self.reader.file)
            self.assertEqual(self._telemetry_ops(), [])

            with open(actual, 'ab') as handle:
                handle.write(b'\n')
            self.reader.last_probe = 0.0
            self.assertTrue(self.reader._ensure_source(1000.0))

        self.assertEqual(self.reader.path, actual)
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], actual)
        with open(actual, 'ab') as handle:
            handle.write(json.dumps(
                battle_start('自动接入')).encode('utf-8') + b'\n')
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_empty_connected_source_switches_to_written_candidate(self):
        stale = self.make_file('自动', '旧空文件', '遥测.jsonl')
        actual = self.make_file('自动', '实际文件', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = stale
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.assertTrue(self.reader._open_source(stale, skip_history=False))
        with open(actual, 'ab') as handle:
            handle.write(b'\n')

        with mock.patch.object(plugin, '_auto_candidates',
                               return_value=[stale, actual]):
            self.reader.last_empty_switch = 0.0
            self.assertTrue(self.reader._ensure_source(1000.0))

        self.assertEqual(self.reader.path, actual)
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], actual)
        self.assertEqual(self._telemetry_ops()[0]['value'], actual)
        with open(actual, 'ab') as handle:
            handle.write(json.dumps(
                battle_start('切换后')).encode('utf-8') + b'\n')
        self.reader.poll()
        self.assertTrue(self.engine.battle_active)

    def test_valid_manual_path_is_not_overwritten(self):
        manual = self.make_file('手动', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = manual
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, '_auto_candidates') as candidates:
            self.assertTrue(self.reader._ensure_source(1000.0))
        candidates.assert_not_called()
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], manual)
        self.assertEqual(self.reader.path, manual)
        self.reader.poll()
        self.assertEqual(self._telemetry_ops(), [])

    def test_no_path_found_returns_false(self):
        plugin.plugin_cfg['telemetry_path'] = ''
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, 'find_telemetry_path',
                               return_value=None):
            self.assertFalse(self.reader._ensure_source(1000.0))
        self.assertIsNone(self.reader.file)
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], '')
        self.assertEqual(self._telemetry_ops(), [])

    def test_probe_interval_throttles_search(self):
        plugin.plugin_cfg['telemetry_path'] = ''
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.reader.last_probe = 999.5
        with mock.patch.object(plugin, 'find_telemetry_path') as find:
            self.assertFalse(self.reader._ensure_source(1000.0))
        find.assert_not_called()

    def test_config_change_reconnects_to_new_chinese_path(self):
        old = self.make_file('旧 路径', '遥测.jsonl')
        new = self.make_file('新 路径', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = old
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.assertTrue(self.reader._open_source(old, skip_history=False))
        old_handle = self.reader.file
        self.engine.on_message(battle_start('变更前'))
        self.assertTrue(self.engine.battle_active)

        plugin.plugin_cfg['telemetry_path'] = new
        self.reader.last_probe = 0.0
        self.assertTrue(self.reader._ensure_source(1000.0))
        self.assertFalse(self.engine.battle_active)
        self.assertTrue(old_handle.closed)
        self.assertEqual(self.reader.path, new)
        self.assertEqual(self.reader.cfg_key, new)
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], new)
        self.assertEqual(self._telemetry_ops(), [])

    def test_config_change_to_invalid_path_auto_fills(self):
        old = self.make_file('旧 路径', '遥测.jsonl')
        new = self.make_file('新 路径', '遥测.jsonl')
        plugin.plugin_cfg['telemetry_path'] = old
        self.reader.cfg_key = plugin._configured_telemetry_path()
        self.assertTrue(self.reader._open_source(old, skip_history=False))

        plugin.plugin_cfg['telemetry_path'] = os.path.join(
            self.root, '不存在', '遥测.jsonl')
        self.reader.last_probe = 0.0
        with mock.patch.object(plugin, 'find_telemetry_path',
                               return_value=new):
            self.assertTrue(self.reader._ensure_source(1000.0))
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], new)
        self.assertEqual(self.reader.cfg_key, new)
        self.assertEqual(self.reader.path, new)
        self.assertEqual(self._telemetry_ops()[0]['value'], new)

    def test_adopt_auto_path_is_noop_when_normalized_equal(self):
        path = self.make_file('回填', '遥测.jsonl')
        configured = '  "{}"  '.format(path)
        plugin.plugin_cfg['telemetry_path'] = configured
        self.reader._adopt_auto_path(path)
        self.assertEqual(plugin.plugin_cfg['telemetry_path'], configured)
        self.assertEqual(self.collector.drain(), [])

    def test_open_source_accepts_chinese_path(self):
        path = self.make_file('中文 目录', '遥测.jsonl')
        self.assertTrue(self.reader._open_source(path, skip_history=False))
        self.assertEqual(self.reader.path, path)
        self.assertEqual(self.reader.offset, 0)

    def test_open_source_failure_resets_state(self):
        self.reader.path = os.path.join(self.root, '旧路径.jsonl')
        self.reader.ident = ('旧设备', '旧文件')
        self.reader.offset = 10
        missing = os.path.join(self.root, '缺失', '遥测.jsonl')
        self.assertFalse(self.reader._open_source(
            missing, skip_history=False))
        self.assertIsNone(self.reader.file)
        self.assertIsNone(self.reader.path)
        self.assertIsNone(self.reader.ident)
        self.assertEqual(self.reader.offset, 0)

    def test_open_source_failure_with_broken_close_does_not_raise(self):
        path = self.make_file('坏句柄', '遥测.jsonl')
        handle = mock.Mock()
        handle.close.side_effect = OSError('关闭失败')
        with mock.patch('builtins.open', return_value=handle), \
                mock.patch.object(plugin.os, 'stat',
                                  side_effect=OSError('状态失败')):
            self.assertFalse(self.reader._open_source(
                path, skip_history=False))
        self.assertIsNone(self.reader.file)
        self.assertIsNone(self.reader.path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
