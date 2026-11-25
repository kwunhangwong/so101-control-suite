import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recalibration as r
from test_recalibration import CalibrationBus
from test_teleoperation import configuration


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'teleoperation.json'
        self.rest_path = self.path.parent / 'rest_pose.json'
        self.original = configuration()
        self.rest = {'follower_position': {i: 2051 for i in r.IDS},
                     'calibration': {role: self.original[role]['motors'] for role in ('leader', 'follower')}}
        r.atomic_json(self.path, self.original)
        r.atomic_json(self.rest_path, self.rest)
        self.history = r.CalibrationHistory(self.path)
        self.old_id = self.history.capture(self.original, self.rest)
        self.buses = {role: CalibrationBus(role, self.original[role]) for role in ('leader', 'follower')}

    def tearDown(self):
        self.tmp.cleanup()

    def save_new(self):
        job = r.Recalibration(self.buses, self.path)
        for bus in self.buses.values():
            bus.raw = {i: 2100 for i in r.IDS}
        job.capture_centers()
        for shift in (-600, 700):
            for bus in self.buses.values():
                bus.raw = {i: 2100 + shift for i in r.IDS}
            job.sample()
        job.review()
        new_config, _ = job.save()
        return new_config

    def test_checkpoint_is_persistent_deduplicated_and_uses_local_time(self):
        self.assertEqual(self.history.capture(self.original, self.rest), self.old_id)
        history = r.CalibrationHistory(self.path)
        record = history.load(self.old_id, self.original)
        self.assertEqual(datetime.fromisoformat(record['saved_at']).utcoffset(), datetime.now().astimezone().utcoffset())
        state = history.snapshot(self.original, self.rest)
        self.assertEqual(len(state['checkpoints']), 1)
        self.assertTrue(state['checkpoints'][0]['current'])
        self.assertTrue(state['checkpoints'][0]['has_rest_pose'])
        self.assertEqual(state['checkpoint_errors'], [])

    def test_save_then_restore_both_directions_preserves_old_reset_and_new_calibration(self):
        old_bytes = (self.history.folder / f'{self.old_id}.json').read_bytes()
        new_config = self.save_new()
        self.assertNotEqual(new_config, self.original)
        self.assertIsNone(json.loads(self.rest_path.read_text()))
        state = self.history.snapshot(new_config, None)
        self.assertEqual(len(state['checkpoints']), 2)
        new_id = next(c['id'] for c in state['checkpoints'] if c['current'])
        # A fresh transaction after the new save restores actual motor registers too.
        restore = r.Recalibration(self.buses, self.path, self.old_id)
        restored, _ = restore.save()
        self.assertEqual(restored, self.original)
        self.assertEqual(json.loads(self.rest_path.read_text()), self.rest)
        self.assertEqual(restore.rest_pose['follower_position']['6'], 2051)
        for role, bus in self.buses.items():
            self.assertEqual(bus.stored, self.original[role]['motors'])
            self.assertEqual(bus.config, self.original[role])
            self.assertEqual(bus.torque, 0)
            self.assertFalse(bus.allow_writes)
            self.assertTrue(all(key != 'torque' or value == 0 for _, key, value in bus.writes))
        self.assertEqual((self.history.folder / f'{self.old_id}.json').read_bytes(), old_bytes)
        restore_new = r.Recalibration(self.buses, self.path, new_id)
        self.assertEqual(restore_new.save()[0], new_config)
        self.assertIsNone(restore_new.rest_pose)
        self.assertEqual(len(self.history.snapshot(new_config, None)['checkpoints']), 2)

    def test_invalid_selection_is_rejected_before_any_torque_or_eeprom_write(self):
        for checkpoint_id in ('../teleoperation', 'checkpoint_123/../../teleoperation', None, 123, 'checkpoint_1'):
            with self.subTest(checkpoint_id=checkpoint_id), self.assertRaises(ValueError):
                self.history.load(checkpoint_id, self.original)
        valid = self.history.load(self.old_id, self.original)
        malformed = [None, {'version': 9}, copy.deepcopy(valid), copy.deepcopy(valid), copy.deepcopy(valid)]
        malformed[2]['config']['leader']['port'] = '/wrong-arm'
        malformed[3]['config']['follower']['motors']['6']['range_min'] = 100.5
        malformed[4]['rest_pose']['follower_position']['6'] = 4096
        for record in malformed:
            r.atomic_json(self.history.folder / 'checkpoint_1.json', record)
            with self.assertRaises(ValueError):
                r.Recalibration(self.buses, self.path, 'checkpoint_1')
        self.assertTrue(self.history.snapshot(self.original, self.rest)['checkpoint_errors'])
        self.assertTrue(all(not bus.writes for bus in self.buses.values()))

    def test_failed_restore_rolls_back_motors_config_and_rest_pose(self):
        new_config = self.save_new()
        new_rest = {'follower_position': {i: 2200 for i in r.IDS},
                    'calibration': {role: new_config[role]['motors'] for role in self.buses}}
        r.atomic_json(self.rest_path, new_rest)
        job = r.Recalibration(self.buses, self.path, self.old_id)
        atomic = r.atomic_json
        failed = False
        def fail_rest_once(path, value):
            nonlocal failed
            if path == self.rest_path and not failed:
                failed = True
                raise OSError('Injected resting pose disk failure')
            atomic(path, value)
        with patch.object(r, 'atomic_json', side_effect=fail_rest_once):
            with self.assertRaisesRegex(RuntimeError, 'Previous calibration restored'):
                job.save()
        self.assertEqual(json.loads(self.path.read_text()), new_config)
        self.assertEqual(json.loads(self.rest_path.read_text()), new_rest)
        for role, bus in self.buses.items():
            self.assertEqual(bus.stored, new_config[role]['motors'])
        self.assertFalse(job.pending_path.exists())
        self.assertEqual(len(self.history.snapshot(new_config, new_rest)['checkpoints']), 3)

    def test_missing_rest_file_remains_missing_after_failed_restore(self):
        self.save_new()
        self.rest_path.unlink()
        job = r.Recalibration(self.buses, self.path, self.old_id)
        with patch.object(job.history, 'capture', side_effect=OSError('Checkpoint disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'Previous calibration restored'):
                job.save()
        self.assertFalse(self.rest_path.exists())
        self.assertFalse(job.pending_path.exists())

    def test_failed_restore_with_failed_rollback_leaves_motion_block(self):
        self.save_new()
        job = r.Recalibration(self.buses, self.path, self.old_id)
        self.buses['follower'].fail = True
        with self.assertRaisesRegex(RuntimeError, 'Recovery needed'):
            job.save()
        self.assertTrue(job.pending_path.exists())

    def test_rest_changed_during_wizard_is_not_overwritten(self):
        job = r.Recalibration(self.buses, self.path, self.old_id)
        self.rest['follower_position']['6'] = 2060
        r.atomic_json(self.rest_path, self.rest)
        with self.assertRaisesRegex(ValueError, 'resting pose file changed'):
            job.save()
        self.assertTrue(all(key == 'torque' for bus in self.buses.values() for _, key, _ in bus.writes))


if __name__ == '__main__':
    unittest.main()
