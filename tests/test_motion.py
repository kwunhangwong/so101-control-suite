import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

import motion as m
from test_teleoperation import configuration


class MotionTest(unittest.TestCase):
    def test_reset_ramps_slowly_to_exact_target_with_gentle_start_and_stop(self):
        clock = [0.0]
        position = {i: 2000 for i in m.IDS}
        commands = []
        def send(goal):
            commands.append(dict(goal))
            position.update(goal)
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=lambda: dict(position), arm=send, send=send)
        with tempfile.TemporaryDirectory() as folder, patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(m.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + max(seconds, .001))):
            target = dict(zip(m.IDS, (3200, 1400, 2015, 800, 2300, 2200)))
            m.move_to(bus, target, threading.Event(), lambda _: None, folder, 'rest')
            self.assertEqual(position, target)
            self.assertEqual(commands[0], {i: 2000 for i in m.IDS})
            self.assertEqual(commands[1], commands[0])
            steps = [max(abs(b[i]-a[i]) for i in m.IDS) for a,b in zip(commands,commands[1:])]
            self.assertLessEqual(max(steps), m.RESET_DEGREES_PER_SECOND*m.TICKS_PER_DEGREE/m.FPS+1)
            self.assertLess(max(steps[:5]), max(steps))
            self.assertLess(max(steps[-5:]), max(steps))
            self.assertGreater(clock[0], 1200/(10*m.TICKS_PER_DEGREE))
            self.assertTrue(all(min(2000,target[i])<=p[i]<=max(2000,target[i]) for p in commands for i in m.IDS))
            rows = [json.loads(line) for line in next(Path(folder).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'complete')
            self.assertEqual(rows[0]['max_target_degrees_per_second'], 10)

    def test_stalled_reset_warns_past_timeout_and_continues_until_user_stops(self):
        clock = [0.0]
        stop = threading.Event()
        sent, frames = [], []
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=lambda: {i: 2000 for i in m.IDS}, arm=lambda _: None,
                              send=lambda p: sent.append(dict(p)))
        def frame(values):
            frames.append(values)
            if clock[0] >= 30: stop.set()
        with tempfile.TemporaryDirectory() as folder, patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(m.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+max(seconds,.001))):
            with self.assertRaises(m.StopRequested):
                m.move_to(bus, {i: 3000 for i in m.IDS}, stop, frame, folder, 'reset')
            rows = [json.loads(line) for line in next(Path(folder).glob('*.jsonl')).read_text().splitlines()]
        self.assertGreater(len(sent), 500)
        self.assertEqual(sent[-1], {i:3000 for i in m.IDS})
        self.assertLess(sent[1]['1'], 3000)
        self.assertFalse(any('reset-duration' in f['warnings'] for f in frames if f['t']<13))
        self.assertIn('reset-duration', frames[-1]['warnings'])
        self.assertIn('tracking-1', frames[-1]['warnings'])
        self.assertEqual(rows[-1]['event'], 'stopped')
        self.assertFalse(any(row.get('event')=='error' for row in rows))

    def test_invalid_target_and_pre_cancelled_move_never_enable_torque(self):
        armed = []
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=lambda: {i: 2000 for i in m.IDS}, arm=armed.append)
        stop = threading.Event(); stop.set()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                m.move_to(bus, {i: 9999 for i in m.IDS}, stop, lambda _: None, folder, 'rest')
            with self.assertRaises(m.StopRequested):
                m.move_to(bus, {i: 2200 for i in m.IDS}, stop, lambda _: None, folder, 'rest')
        self.assertEqual(armed, [])

    def test_delayed_read_does_not_jump_ahead_in_reset(self):
        clock, position, sent = [0.0], {i: 2000 for i in m.IDS}, []
        def read():
            if len(sent) == 4:
                clock[0] += 3
            return dict(position)
        def send(goal):
            sent.append(dict(goal))
            position.update(goal)
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=read, arm=send, send=send)
        with tempfile.TemporaryDirectory() as folder, patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(m.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+max(seconds,.001))):
            m.move_to(bus, {i: 2400 for i in m.IDS}, threading.Event(), lambda _: None, folder, 'reset')
        self.assertEqual(position, {i:2400 for i in m.IDS})
        self.assertTrue(all(abs(b[i]-a[i]) <= 10*m.TICKS_PER_DEGREE/m.FPS+1
                            for a,b in zip(sent,sent[1:]) for i in m.IDS))
        # A delayed iteration may advance by one normal frame, never by 3 seconds.
        self.assertGreaterEqual(clock[0], 3+1.5*400/(10*m.TICKS_PER_DEGREE)-1/m.FPS)

    def test_invalid_initial_pose_does_not_snap_to_calibrated_endpoint(self):
        armed = []
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=lambda: {i: 0 for i in m.IDS}, arm=armed.append)
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            m.move_to(bus, {i:2000 for i in m.IDS}, threading.Event(), lambda _: None, folder, 'reset')
        self.assertEqual(armed, [])

    def test_stop_during_motion_sends_no_further_goals(self):
        stop = threading.Event()
        sent = []
        bus = SimpleNamespace(config=configuration()['follower'], verify=lambda: None,
                              positions=lambda: {i: 2000 for i in m.IDS}, arm=lambda _: None,
                              send=lambda p: sent.append(p))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(m.StopRequested):
                m.move_to(bus, {i: 2200 for i in m.IDS}, stop, lambda _: stop.set(), folder, 'rest')
            self.assertEqual(len(sent), 1)
            rows = [json.loads(line) for line in next(Path(folder).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'stopped')
