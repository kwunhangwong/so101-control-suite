import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import teleoperation as t


def configuration():
    motors = {i: {'model': 777, 'range_min': 100, 'range_max': 3900, 'homing_offset': 0} for i in t.IDS}
    return {'calibrated': True, 'leader': {'port': '/leader', 'motors': motors}, 'follower': {'port': '/follower', 'motors': motors}}


class TeleoperationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'config').mkdir()
        (root / 'config/teleoperation.json').write_text(json.dumps(configuration()))
        patcher = patch.object(t, 'ROOT', root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_direct_targets_have_no_speed_cap_and_respect_stored_motor_limits(self):
        motors = configuration()['follower']['motors']
        self.assertEqual(t.calibrated_goals({i: 3800 for i in t.IDS}, motors), {i: 3800 for i in t.IDS})
        self.assertEqual(t.calibrated_goals({i: 10000 for i in t.IDS}, motors), {i: 3900 for i in t.IDS})
        self.assertEqual(t.calibrated_goals({i: -10000 for i in t.IDS}, motors), {i: 100 for i in t.IDS})

    def test_out_of_range_leader_maps_without_stopping_but_wrong_ports_remain_invalid(self):
        config = configuration()
        self.assertEqual(t.mapped_positions({i: 10000 for i in t.IDS}, config), {i: 3900 for i in t.IDS})
        self.assertIn('leader-range-1', t.range_warnings('leader', {i: 10000 for i in t.IDS}, config['leader']['motors']))
        config['follower']['port'] = '/leader'
        with self.assertRaises(ValueError): t.validate_config(config)

    def test_large_start_gap_fast_leader_and_persistent_lag_do_not_stop_following(self):
        clock = [0.0]
        stop = threading.Event()
        sent, frames = [], []
        follower = SimpleNamespace(verify=lambda: None, positions=lambda: {i: 50 for i in t.IDS},
                                   arm=lambda _: None, send=lambda p: sent.append(dict(p)),
                                   gripper_settings=lambda: {'torque_limit': 200},
                                   gripper_feedback=lambda: {'status': 0, 'torque_limit': 200})
        leader = SimpleNamespace(verify=lambda: None, positions=lambda: {i: 10000 if int(clock[0]*30)%2 else 1000 for i in t.IDS})
        def frame(values):
            frames.append(values)
            if clock[0] >= 20: stop.set()
        with tempfile.TemporaryDirectory() as d, patch.object(t, 'ROOT', Path(d)), patch.object(t.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(t.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+max(seconds,0.001))), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(t.StopRequested): t.run(leader, follower, configuration(), stop, frame)
            rows = [json.loads(line) for line in next((Path(d)/'logs').glob('*.jsonl')).read_text().splitlines()]
        self.assertGreater(len(sent), 500)
        self.assertTrue(all(frame['phase']=='follow' for frame in frames))
        self.assertGreater(max(abs(a['1']-b['1']) for a,b in zip(sent,sent[1:])), 2000)
        warnings = {key for frame in frames for key in frame['warnings']}
        self.assertTrue({'tracking-1','leader-range-1','follower-range-1','fast-target'} <= warnings)
        self.assertEqual(rows[-1]['event'], 'stopped')
        self.assertIsNone(rows[0]['max_target_degrees_per_second'])
        self.assertFalse(any(row.get('event')=='error' for row in rows))

    def test_packet_guard_blocks_all_leader_writes_and_follower_calibration_writes(self):
        for role in ('leader', 'follower'):
            written = []
            serial = SimpleNamespace(Serial=MagicMock(return_value=SimpleNamespace(reset_input_buffer=MagicMock())))
            port = SimpleNamespace(writePort=lambda p: written.append(p), baudrate=1000000)
            sdk = SimpleNamespace(PortHandler=lambda _: port, PacketHandler=lambda _: None)
            bus = t.Bus(role, configuration()[role])
            with patch.dict('sys.modules', {'scservo_sdk': sdk, 'serial': serial}):
                bus.open()
            self.assertTrue(serial.Serial.call_args.kwargs['exclusive'])
            torque = [255,255,1,4,3,40,1,0]
            with self.assertRaises(RuntimeError):
                port.writePort(torque)
            bus.allow_writes = True
            if role == 'leader':
                with self.assertRaises(RuntimeError):
                    port.writePort(torque)
            else:
                port.writePort(torque)
                with self.assertRaises(RuntimeError):
                    port.writePort([255,255,1,5,3,31,0,0,0])

    def test_gripper_log_keeps_applied_limit_and_pre_fault_feedback_without_hiding_fault(self):
        clock, sent = [0.0], []
        samples = iter([
            {'status': 0, 'torque_limit': 200, 'load': 0},
            {'status': 0, 'torque_limit': 200, 'load': 1200},
            {'status': 32, 'torque_limit': 200, 'load': 1124},
            {'status': 32, 'torque_limit': 200, 'load': 1124},
        ])
        follower = SimpleNamespace(verify=lambda: None, positions=lambda: {i: 1704 for i in t.IDS},
            arm=lambda _: None, send=lambda p: sent.append(dict(p)), retries=0,
            gripper_settings=lambda: {'torque_limit': 200, 'overload_torque': 25, 'protection_time': 200},
            gripper_feedback=lambda: next(samples), diagnostics=lambda: {'6': {'error': 'overload'}})
        leader = SimpleNamespace(verify=lambda: None, positions=lambda: {i: 3000 for i in t.IDS}, retries=0)
        with tempfile.TemporaryDirectory() as d, patch.object(t, 'ROOT', Path(d)), patch.object(t.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(t.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, clock[0]+0.11)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'fault status 32'):
                t.run(leader, follower, configuration())
            rows = [json.loads(line) for line in next((Path(d)/'logs').glob('*.jsonl')).read_text().splitlines()]
        self.assertEqual(rows[0]['gripper_settings']['torque_limit'], 200)
        self.assertEqual(rows[1]['gripper_feedback']['torque_limit'], 200)
        self.assertEqual([r['status'] for r in rows if r.get('event') == 'gripper_feedback'], [0,32])
        self.assertEqual(rows[-1]['event'], 'error')
        self.assertEqual(rows[-1]['gripper_feedback']['status'], 32)
        self.assertEqual(sent, [{i: 3000 for i in t.IDS}])

    def test_cleanup_attempts_every_motor_even_if_one_fails(self):
        bus = t.Bus('follower', configuration()['follower'])
        bus.allow_writes = True
        attempted = []
        def write(i, address, value, width):
            attempted.append((i,address,value,width))
            if i == '2':
                raise RuntimeError('Disconnected motor')
        bus.write = write
        self.assertEqual(len(bus.release()), 1)
        self.assertEqual(attempted, [(i,40,0,1) for i in t.IDS])
        self.assertFalse(bus.allow_writes)

    def test_calibration_scope_allows_only_calibration_registers_and_torque_off(self):
        serial = SimpleNamespace(Serial=MagicMock(return_value=SimpleNamespace(reset_input_buffer=MagicMock())))
        packets = []
        port = SimpleNamespace(writePort=lambda p: packets.append(p), baudrate=1000000)
        sdk = SimpleNamespace(PortHandler=lambda _: port, PacketHandler=lambda _: None)
        bus = t.Bus('leader', configuration()['leader'])
        with patch.dict('sys.modules', {'scservo_sdk': sdk, 'serial': serial}): bus.open()
        offset = [255,255,1,5,3,31,123,0,0]
        with self.assertRaises(RuntimeError): port.writePort(offset)
        bus.calibration_writes = True
        port.writePort(offset)
        port.writePort([255,255,1,4,3,40,0,0])
        for packet in ([255,255,1,4,3,40,1,0], [255,255,1,5,3,42,0,8,0], [255,255,1,4,3,5,2,0]):
            with self.assertRaises(RuntimeError): port.writePort(packet)
        self.assertEqual(len(packets),2)

    def test_calibration_write_encodes_signed_offset_and_always_clears_permission(self):
        bus = t.Bus('leader', configuration()['leader'])
        bus.read = lambda *args: 0
        writes = []
        def write(i,address,value,width):
            self.assertTrue(bus.calibration_writes)
            writes.append((i,address,value,width))
        bus.write = write
        bus.calibration_write('1','homing_offset',-247)
        self.assertEqual(writes,[('1',31,2048+247,2)])
        self.assertFalse(bus.calibration_writes)
        with self.assertRaises(ValueError): bus.calibration_write('1','torque',1)
        with self.assertRaises(ValueError): bus.calibration_write('1','homing_offset',2048)
        with self.assertRaises(ValueError): bus.calibration_write('1','position',2000)
        with patch.object(bus,'write',side_effect=RuntimeError('Lost reply')):
            with self.assertRaises(RuntimeError): bus.calibration_write('1','range_min',100)
        self.assertFalse(bus.calibration_writes)

    def test_missing_reply_is_retried_but_motor_fault_is_not(self):
        bus = t.Bus('follower', configuration()['follower'])
        bus.port = SimpleNamespace(ser=SimpleNamespace(reset_input_buffer=MagicMock()))
        bus.sdk = SimpleNamespace(getTxRxResult=str, getRxPacketError=str)
        read = MagicMock(side_effect=[(0,-6,0),(2000,0,0)])
        self.assertEqual(bus.transfer(read, 1, 56), (2000,0,0))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(bus.retries, 1)
        faulty = MagicMock(return_value=(0,0,32))
        with self.assertRaises(RuntimeError):
            bus.transfer(faulty, 1, 56)
        self.assertEqual(faulty.call_count, 1)

    def test_control_failure_holds_without_disabling_torque(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'config').mkdir()
            (root/'config/teleoperation.json').write_text(json.dumps(configuration()))
            def fail(leader, follower, config):
                follower.allow_writes = True
                raise RuntimeError('Lost acknowledgement')
            with patch.object(t, 'ROOT', root), patch('sys.argv', ['teleoperation.py']), patch('builtins.input', return_value='START'), patch.object(t.Bus, 'open'), patch.object(t.Bus, 'close'), patch.object(t.Bus, 'verify'), patch.object(t.Bus, 'read', return_value=0), patch.object(t.Bus, 'positions', return_value={i: 2000 for i in t.IDS}), patch.object(t.Bus, 'write') as write, patch.object(t, 'run', side_effect=fail), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(t.main(), 1)
                self.assertEqual(write.call_count, 6)
                for call in write.call_args_list:
                    self.assertEqual(call.args[1], 42)

    def test_cancelled_start_never_writes_to_hardware(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'config').mkdir()
            (root/'config/teleoperation.json').write_text(json.dumps(configuration()))
            with patch.object(t, 'ROOT', root), patch('sys.argv', ['teleoperation.py']), patch('builtins.input', return_value=''), patch.object(t.Bus, 'open'), patch.object(t.Bus, 'close'), patch.object(t.Bus, 'verify'), patch.object(t.Bus, 'read', return_value=0), patch.object(t.Bus, 'positions', return_value={i: 2000 for i in t.IDS}), patch.object(t.Bus, 'write') as write, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(t.main(), 0)
                write.assert_not_called()

    def test_release_cancel_never_opens_the_serial_port(self):
        with patch('sys.argv', ['teleoperation.py', '--release']), patch.object(t, 'open_release_in_app', return_value=False), patch('builtins.input', return_value=''), patch.object(t.Bus, 'open') as opened, patch.object(t.Bus, 'write') as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(t.main(), 0)
            opened.assert_not_called()
            write.assert_not_called()

    def test_release_uses_app_without_second_serial_connection_or_terminal_prompt(self):
        with patch('sys.argv', ['teleoperation.py', '--release']), patch.object(t, 'open_release_in_app', return_value=True), patch('builtins.input') as prompt, patch.object(t.Bus, 'open') as opened, patch.object(t.Bus, 'write') as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(t.main(), 0)
            prompt.assert_not_called()
            opened.assert_not_called()
            write.assert_not_called()

    def test_standalone_release_opens_only_after_confirmation(self):
        events = []
        def prompt(_): events.append('confirmed'); return 'RELEASE'
        with patch('sys.argv', ['teleoperation.py', '--release']), patch.object(t, 'open_release_in_app', return_value=False), patch('builtins.input', side_effect=prompt), patch.object(t.Bus, 'open', side_effect=lambda: events.append('opened')), patch.object(t.Bus, 'write') as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(t.main(), 0)
            self.assertEqual(events, ['confirmed','opened'])
            self.assertEqual([call.args for call in write.call_args_list], [(i,40,0,1) for i in t.IDS])

    def test_release_routes_only_to_the_app_with_the_same_follower(self):
        config = configuration()
        status = {'app': 'so101-calibration-check', 'connected': True, 'ports': {'follower': '/follower'}}
        with patch.object(t, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(json.dumps(status).encode())), patch.object(t.webbrowser, 'open') as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(t.open_release_in_app(config))
            browser.assert_called_once_with('http://127.0.0.1:8766/?release=1')
            status['ports']['follower'] = '/different-arm'
            self.assertFalse(t.open_release_in_app(config))
            self.assertEqual(browser.call_count, 1)

    def test_busy_port_is_reported_without_disabling_torque(self):
        output = io.StringIO()
        with patch('sys.argv', ['teleoperation.py', '--release']), patch.object(t, 'open_release_in_app', return_value=False), patch('builtins.input', return_value='RELEASE'), patch.object(t.Bus, 'open', side_effect=OSError(35, 'Could not exclusively lock port')), patch.object(t.Bus, 'write') as write, contextlib.redirect_stdout(output):
            self.assertEqual(t.main(), 1)
            self.assertIn('already in use', output.getvalue())
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
