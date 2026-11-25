import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import controller as c
from server import Handler, ThreadingHTTPServer


class FakeBus:
    calls = []
    def __init__(self, role, config):
        self.role, self.config = role, config
        self.allow_writes = False
        self.torque = 1 if role == 'follower' else 0
        self.retries = 0
    def record(self, action):
        self.calls.append((self.role, action, threading.get_ident()))
    def open(self): self.record('open')
    def close(self): self.record('close')
    def verify(self): self.record('verify')
    def clear_gripper_overload(self): self.record('clear-gripper-overload')
    def positions(self):
        self.record('read')
        return {i: 2000 for i in c.IDS}
    def read(self, i, name):
        self.record('read-model')
        return 777
    def diagnostics(self):
        return {i: {'position': 2000, 'torque': self.torque, 'load': 0, 'current': 0, 'voltage': 124, 'temperature': 40, 'status': 0} for i in c.IDS}
    def gripper_feedback(self):
        return {**self.diagnostics()['6'], 'goal': 2000, 'torque_limit': 200, 'read_at': time.time()}
    def gripper_settings(self):
        return {**self.gripper_feedback(), 'max_torque_limit': 500, 'overload_torque': 25,
                'protective_torque': 20, 'protection_time': 200}
    def arm(self, positions):
        self.allow_writes = True
        self.torque = 1
        self.record('arm')
    def hold(self):
        if self.allow_writes: self.record('hold')
    def release(self):
        self.record('release')
        self.torque = 0
        self.allow_writes = False
        return []


class FakeCamera:
    camera_id = 'test-camera'
    def __init__(self): self.live = False; self.stale = False
    def start(self): self.live = True
    def stop(self): self.live = False
    def status(self):
        return {'state': 'live' if self.live else 'off', 'width': 1920, 'height': 1080,
                'captured_at': time.time()-(10 if self.stale else 0)}



class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = {role: {'port': '/'+role, 'motors': {i: {'model': 777, 'range_min': 100, 'range_max': 3900, 'homing_offset': 0} for i in c.IDS}} for role in ('leader', 'follower')}
        config['calibrated'] = True
        path = Path(self.directory.name)/'config.json'
        path.write_text(json.dumps(config))
        (path.parent/'rest_pose.json').write_text(json.dumps({'follower_position': {i: 2100 for i in c.IDS}, 'calibration': {role: config[role]['motors'] for role in ('leader','follower')}}))
        FakeBus.calls = []
        self.bus_patch = patch.object(c, 'Bus', FakeBus)
        self.bus_patch.start()
        self.controller = c.CalibrationController(path)

    def tearDown(self):
        self.controller.close()
        self.bus_patch.stop()
        self.directory.cleanup()

    def wait_for(self, predicate):
        end = time.monotonic()+3
        while time.monotonic()<end:
            state = self.controller.snapshot()
            if predicate(state): return state
            time.sleep(0.01)
        self.fail(str(self.controller.snapshot()))

    def open_page(self, name='first'):
        self.controller.open_page(name)
        return self.wait_for(lambda s:s['connected'] and len(s['joints'])==6)

    def test_open_is_read_only_and_all_io_has_one_owner(self):
        self.assertEqual(FakeBus.calls, [])
        self.open_page()
        self.assertFalse(any(action in ('arm','release') for _,action,_ in FakeBus.calls))
        self.assertEqual({thread for _,_,thread in FakeBus.calls}, {self.controller.thread.ident})
        self.controller.close_page('first')
        self.wait_for(lambda s:not s['connected'])
        self.assertFalse(any(action=='release' for _,action,_ in FakeBus.calls))

    def test_live_log_warns_without_changing_control_or_setting_stop(self):
        self.controller.update(phase='following')
        self.controller.record_warnings({'tracking-6':'Gripper target gap is 42 degrees.'})
        state = self.controller.snapshot()
        self.assertEqual(state['phase'], 'following')
        self.assertFalse(self.controller.stop_event.is_set())
        self.assertIsNone(state['error'])
        warning_events = [event for event in state['events'] if event['level']=='WARNING']
        self.assertEqual(len(warning_events), 1)
        self.assertIsNotNone(__import__('datetime').datetime.fromisoformat(warning_events[0]['time']).utcoffset())
        for _ in range(20): self.controller.record_warnings({'tracking-6':'Gripper target gap is 43 degrees.'})
        self.assertEqual(len([e for e in self.controller.snapshot()['events'] if e['level']=='WARNING']), 1)
        self.controller.update(error='Motor reports overload')
        self.assertEqual(self.controller.snapshot()['events'][-1]['level'], 'ERROR')
        for n in range(205): self.controller.log_event('INFO', str(n))
        events = self.controller.snapshot()['events']
        self.assertEqual(len(events), 200)
        self.assertEqual(events[-1]['message'], '204')
        self.assertTrue(all(b['id']>a['id'] for a,b in zip(events,events[1:])))
        self.assertEqual(FakeBus.calls, [])

    def test_connection_check_starts_unchecked_reuses_ports_and_closes_both_cameras(self):
        self.controller.camera = FakeCamera()
        self.controller.wrist_camera = FakeCamera()
        self.open_page()
        self.assertEqual(self.controller.snapshot()['connections'], {})
        opens = sum(action=='open' for _,action,_ in FakeBus.calls)
        self.controller.command('first', 'check-connections')
        state = self.wait_for(lambda s:not s['checking_connections'] and len(s['connections'])==4)
        self.assertTrue(all(row['connected'] for row in state['connections'].values()))
        self.assertEqual(sum(action=='open' for _,action,_ in FakeBus.calls), opens)
        self.assertFalse(any(action in ('arm','release') for _,action,_ in FakeBus.calls))
        self.assertEqual({thread for _,_,thread in FakeBus.calls}, {self.controller.thread.ident})
        self.controller.close_page('first')
        self.wait_for(lambda s:not s['connected'] and s['wrist_camera']['state']=='off')
        self.assertFalse(self.controller.camera.live)

    def test_dataset_and_calibration_share_ports_and_camera_lifetime(self):
        self.controller.camera = FakeCamera()
        self.controller.wrist_camera = FakeCamera()
        self.open_page('calibration')
        self.assertTrue(self.controller.camera.live)
        self.assertTrue(self.controller.wrist_camera.live)
        opens = sum(action=='open' for _,action,_ in FakeBus.calls)
        self.open_page('dataset')
        self.controller.close_page('calibration')
        time.sleep(0.15)
        self.assertTrue(self.controller.snapshot()['connected'])
        self.assertTrue(self.controller.wrist_camera.live)
        self.assertEqual(sum(action=='open' for _,action,_ in FakeBus.calls), opens)
        self.controller.close_page('dataset')
        self.wait_for(lambda s:not s['connected'] and s['wrist_camera']['state']=='off')
        self.assertFalse(self.controller.camera.live)
        self.assertFalse(any(action in ('arm','release') for _,action,_ in FakeBus.calls))

    def test_connection_check_reports_other_devices_when_follower_and_wrist_fail(self):
        self.controller.camera = FakeCamera()
        self.controller.wrist_camera = MagicMock()
        self.controller.wrist_camera.status.return_value = {'state':'error','error':'Camera unplugged'}
        original_open = FakeBus.open
        def open_bus(bus):
            if bus.role=='follower': raise OSError('Follower USB is missing')
            original_open(bus)
        with patch.object(FakeBus, 'open', open_bus):
            self.controller.open_page('first')
            self.wait_for(lambda s:s['phase']=='error')
            self.controller.command('first', 'check-connections')
            state = self.wait_for(lambda s:not s['checking_connections'] and len(s['connections'])==4)
        checks = state['connections']
        self.assertTrue(checks['leader']['connected'])
        self.assertTrue(checks['front']['connected'])
        self.assertFalse(checks['follower']['connected'])
        self.assertIn('USB is missing', checks['follower']['error'])
        self.assertFalse(checks['wrist']['connected'])
        self.assertEqual(checks['wrist']['error'], 'Camera unplugged')
        self.assertFalse(any(action in ('arm','release') for _,action,_ in FakeBus.calls))

    def test_connection_check_rejects_stale_camera(self):
        self.controller.camera = FakeCamera()
        self.controller.wrist_camera = FakeCamera()
        self.open_page()
        self.controller.camera.stale = True
        self.controller.command('first','check-connections')
        state = self.wait_for(lambda s:not s['checking_connections'] and len(s['connections'])==4)
        self.assertFalse(state['connections']['front']['connected'])
        self.assertTrue(state['connections']['wrist']['connected'])

    def test_inactive_page_cannot_start(self):
        with self.assertRaises(ValueError):self.controller.command('unknown','reset')
        self.assertEqual(FakeBus.calls,[])

    def test_reset_reaches_saved_pose_independently_of_leader_and_holds(self):
        positions = {'leader': {i: 2100 for i in c.IDS}, 'follower': {i: 2000 for i in c.IDS}}
        target = dict(self.controller.rest_pose['follower_position'])
        positions['leader'] = {i: 2500 for i in c.IDS}
        sent = []
        def read(bus):
            bus.record('read')
            return dict(positions[bus.role])
        def send(bus, goals):
            bus.record('send')
            sent.append(dict(goals))
            positions['follower'].update(goals)
            positions['leader'].update({i: 2300 for i in c.IDS})
        move_to = c.move_to
        def move(follower, target, stop, frame, folder, purpose):
            return move_to(follower, target, stop, frame, Path(self.directory.name)/'logs', purpose)
        with patch.object(FakeBus,'positions',read), patch.object(FakeBus,'send',send,create=True), patch.object(c,'move_to',side_effect=move):
            self.open_page()
            self.controller.command('first','reset')
            state = self.wait_for(lambda s:s['phase']=='holding')
            self.assertEqual(positions['follower'],target)
            self.assertEqual(sent[-1],target)
            self.assertIn('Reset complete',state['message'])
            self.assertTrue(all(state['torque'].values()))
            count = len(sent)
            time.sleep(0.15)
            self.assertEqual(len(sent),count)
        self.assertIn('hold',[a for _,a,_ in FakeBus.calls])
        self.assertNotIn('release',[a for _,a,_ in FakeBus.calls])
        self.assertEqual({thread for _,_,thread in FakeBus.calls},{self.controller.thread.ident})
        rows = [json.loads(line) for line in next((Path(self.directory.name)/'logs').glob('*.jsonl')).read_text().splitlines()]
        self.assertEqual(rows[0]['purpose'],'reset')
        self.assertEqual(rows[0]['target'],target)
        self.assertEqual(rows[-1]['event'],'complete')

    def test_reset_refuses_changed_calibration_before_any_recovery_or_motion(self):
        self.open_page()
        self.controller.config['follower']['motors']['6']['homing_offset'] = 1
        with self.assertRaisesRegex(ValueError, 'Calibration changed'):
            self.controller.command('first','reset')
        self.assertIsNone(self.controller.snapshot()['reset_pose'])
        self.assertFalse(any(a in ('arm','release','clear-gripper-overload') for _,a,_ in FakeBus.calls))

    def test_reset_stop_and_last_page_close_hold_without_following(self):
        entered = threading.Event()
        def move(follower,target,stop,frame,*args):
            follower.arm(follower.positions())
            entered.set()
            while not stop.wait(0.01):
                frame({'log_path':'logs/simulated-alignment.jsonl'})
            raise c.StopRequested()
        with patch.object(c,'move_to',side_effect=move):
            for close_page in (False,True):
                with self.subTest(close_page=close_page):
                    if not self.controller.has_page('first'): self.open_page()
                    entered.clear()
                    self.controller.command('first','reset')
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(ValueError): self.controller.command('first','check-connections')
                    with self.assertRaises(ValueError): self.controller.command('first','reset')
                    if close_page:
                        self.controller.close_page('first')
                        self.wait_for(lambda s:not s['connected'])
                    else:
                        self.controller.command('first','stop')
                        self.wait_for(lambda s:s['phase']=='holding')
                    self.assertIn('hold',[a for _,a,_ in FakeBus.calls])
                    self.assertNotIn('release',[a for _,a,_ in FakeBus.calls])

    def test_pending_reset_can_be_cancelled_before_motor_writes(self):
        self.open_page()
        with self.controller.lock:
            self.controller.command('first','reset')
            self.controller.command('first','stop')
            self.assertIsNone(self.controller.pending)
        time.sleep(0.15)
        self.assertFalse(any(a in ('arm','release') for _,a,_ in FakeBus.calls))

    def test_reset_error_holds_and_never_releases(self):
        self.open_page()
        def failed_move(follower,*args):
            follower.arm(follower.positions())
            raise RuntimeError('Lost acknowledgement')
        with patch.object(c,'move_to',side_effect=failed_move):
            self.controller.command('first','reset')
            state = self.wait_for(lambda s:s['phase']=='error')
        self.assertEqual(state['error'],'Lost acknowledgement')
        self.assertIn('hold',[a for _,a,_ in FakeBus.calls])
        self.assertNotIn('release',[a for _,a,_ in FakeBus.calls])

    def test_check_connection_recovers_reconnected_arm_and_clears_control_error(self):
        original_open = FakeBus.open
        def open_bus(bus):
            if bus.role=='follower': raise OSError('Follower USB is missing')
            original_open(bus)
        with patch.object(FakeBus,'open',open_bus):
            self.controller.open_page('first')
            self.wait_for(lambda s:s['phase']=='error')
        self.controller.command('first','check-connections')
        state=self.wait_for(lambda s:s['connected'] and not s['checking_connections'])
        self.assertTrue(state['calibration_ok'])
        self.assertIsNone(state['error'])
        self.controller.update(phase='error',error='Prior motion failed')
        self.controller.command('first','check-connections')
        state=self.wait_for(lambda s:not s['checking_connections'] and s['error'] is None)
        self.assertTrue(state['connected'])
        self.assertFalse(any(a in ('arm','release') for _,a,_ in FakeBus.calls))

    def test_http_requires_same_origin_and_explicit_confirmation(self):
        self.open_page()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.controller=self.controller
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
            def post(action,body=None,origin=None):
                headers={'Content-Type':'application/json'}
                if origin:headers['Origin']=origin
                connection.request('POST',f'/api/{action}?session=first',json.dumps(body),headers)
                result=connection.getresponse();result.read();return result.status
            for action in ('start','rest','save-rest','point','teach','clear-area','connect','align'):
                self.assertEqual(post(action,{'confirm':'START'}),404)
                with self.assertRaises(ValueError): self.controller.command('first',action)
            self.assertEqual(post('reset',{}),409)
            for action in ('test','recalibrate-begin','recalibrate-center','recalibrate-review','recalibrate-save','recalibrate-cancel','recalibrate-restore'):
                self.assertEqual(post(action,{}),409)
            self.assertEqual(post('recalibrate-restore',{'confirm':'RESTORE_CALIBRATION'}),409)
            self.assertEqual(post('recalibrate-restore',{'confirm':'RESTORE_CALIBRATION','supported':True,'checkpoint_id':'../teleoperation'}),409)
            self.assertEqual(post('recalibrate-restore',{'confirm':'RESTORE_CALIBRATION','supported':True},'https://example.com'),403)
            self.assertEqual(post('recalibrate-begin',{'confirm':'RECALIBRATE'}),409)
            self.assertEqual(post('recalibrate-begin',{'confirm':'RECALIBRATE','supported':True},'https://example.com'),403)
            self.assertEqual(post('reset',{'confirm':'RESET'},'https://example.com'),403)
            with patch.object(c,'move_to') as move:
                self.assertEqual(post('reset',{'confirm':'RESET'}),200)
                self.wait_for(lambda s:s['phase']=='holding')
                move.assert_called_once()
            connection.request('POST','/api/check-connections?session=unknown')
            response=connection.getresponse();self.assertEqual(response.status,409);response.read()
            self.assertEqual(post('check-connections'),200)
            self.wait_for(lambda s:not s['checking_connections'] and len(s['connections'])==4)
            self.assertEqual(post('release',{'confirm':'START'}),409)
            self.assertEqual(post('release',{'confirm':'RELEASE'}),200)
            self.wait_for(lambda s:s['phase']=='ready' and not any(s['torque'].values()))
            connection.close()
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_reset_recovers_disconnected_error_then_moves_without_restarting_test(self):
        fault = [True]
        def verify(bus):
            bus.record('verify')
            if bus.role == 'follower' and fault[0]:
                raise RuntimeError('follower motor 6: Overload error!')
        def recover(bus):
            bus.record('clear-gripper-overload')
            fault[0] = False
        with patch.object(FakeBus, 'verify', verify), patch.object(FakeBus, 'clear_gripper_overload', recover):
            self.controller.open_page('first')
            self.wait_for(lambda s:s['phase']=='error' and not s['connected'])
            with patch.object(c, 'move_to') as move, patch.object(c, 'run') as follow:
                self.controller.command('first', 'reset')
                state = self.wait_for(lambda s:s['phase']=='holding' and s['error'] is None)
                self.assertTrue(state['connected'])
                move.assert_called_once()
                self.assertEqual(move.call_args.args[1], self.controller.rest_pose['follower_position'])
                follow.assert_not_called()
        self.assertNotIn('release', [action for _, action, _ in FakeBus.calls])
        self.assertEqual({thread for _,_,thread in FakeBus.calls}, {self.controller.thread.ident})

    def test_reset_persistent_fault_blocks_motion_and_keeps_error(self):
        self.open_page()
        with patch.object(FakeBus, 'clear_gripper_overload', side_effect=RuntimeError('follower motor 6: Overload error!')), patch.object(c, 'move_to') as move:
            self.controller.command('first', 'reset')
            state = self.wait_for(lambda s:s['phase']=='error')
            self.assertIn('Overload error', state['error'])
            self.assertIn('Reset blocked', state['message'])
            self.assertFalse(state['calibration_ok'])
            move.assert_not_called()
        self.assertFalse(any(a in ('arm','release') for _,a,_ in FakeBus.calls))

    def test_pending_calibration_save_blocks_recovery_and_reset(self):
        self.open_page()
        (self.controller.config_path.parent/'recalibration_pending.json').write_text('{}')
        with patch.object(c, 'move_to') as move:
            self.controller.command('first', 'reset')
            state = self.wait_for(lambda s:s['phase']=='error')
            self.assertIn('incomplete calibration', state['error'])
            move.assert_not_called()
        self.assertNotIn('clear-gripper-overload', [action for _,action,_ in FakeBus.calls])


if __name__=='__main__':unittest.main()
