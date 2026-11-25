import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import controller as c
import recalibration as r
from test_controller import FakeBus
from test_teleoperation import configuration


class CalibrationBus(FakeBus):
    def __init__(self, role, config):
        super().__init__(role, config)
        self.stored = copy.deepcopy(config['motors'])
        self.raw = {i: 2047 for i in c.IDS}
        self.writes = []
        self.fail = False
    def read(self, i, key):
        self.record('read')
        if key == 'torque': return self.torque
        if key == 'mode': return 0
        if key == 'position': return (self.raw[i] - self.stored[i]['homing_offset']) % 4096
        return self.stored[i][key]
    def positions(self): return {i: self.read(i,'position') for i in c.IDS}
    def verify(self):
        self.record('verify')
        if self.stored != self.config['motors']: raise ValueError('Calibration mismatch')
    def calibration_write(self, i, key, value):
        self.record('calibration-write')
        self.writes.append((i,key,value))
        if self.fail and key=='homing_offset': raise RuntimeError('USB lost during save')
        if key=='torque': self.torque=value
        elif key!='lock': self.stored[i][key]=value
    def send(self, goals):
        self.record('send')
        self.raw.update({i: goals[i]+self.stored[i]['homing_offset'] for i in c.IDS})


class RecalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'teleoperation.json'
        self.config=configuration()
        # Exercise signed existing offsets rather than assuming uncalibrated motors.
        self.config=copy.deepcopy(self.config)
        for role, offset in [('leader',400),('follower',-300)]:
            for motor in self.config[role]['motors'].values(): motor['homing_offset']=offset
        self.path.write_text(json.dumps(self.config))
        self.buses={role: CalibrationBus(role,self.config[role]) for role in ('leader','follower')}
        self.buses['leader'].raw={i: 2500 for i in c.IDS}
        self.buses['follower'].raw={i: 1800 for i in c.IDS}
        self.job=r.Recalibration(self.buses,self.path)
    def tearDown(self): self.tmp.cleanup()
    def ready(self):
        self.job.capture_centers()
        for shift in (-600,700):
            for role,bus in self.buses.items():
                bus.raw={i:self.job.centers[role][i]+shift for i in c.IDS}
            self.job.sample()
        self.job.review()
    def test_capture_is_read_only_until_save_and_uses_actual_encoder_centers(self):
        original=self.path.read_bytes()
        self.ready()
        for role,bus in self.buses.items():
            self.assertTrue(all(key=='torque' and value==0 for _,key,value in bus.writes))
            self.assertEqual(bus.torque,0)
            self.assertEqual(self.job.proposed[role]['motors']['1']['homing_offset'],453 if role=='leader' else -247)
            self.assertEqual(self.job.proposed[role]['motors']['1']['range_min'],1447)
            self.assertEqual(self.job.proposed[role]['motors']['1']['range_max'],2747)
            self.assertEqual(self.job.proposed[role]['motors']['5']['range_min'],0)
            self.assertEqual(self.job.proposed[role]['motors']['5']['range_max'],4095)
        self.assertEqual(self.path.read_bytes(),original)
    def test_save_verifies_both_arms_and_creates_backup_without_enabling_torque(self):
        self.ready()
        config,backup=self.job.save()
        self.assertEqual(json.loads(self.path.read_text()),config)
        saved=json.loads(Path(backup).read_text())
        self.assertEqual(saved['previous_config'],self.config)
        for role,bus in self.buses.items():
            self.assertEqual(bus.stored,config[role]['motors'])
            self.assertEqual(bus.torque,0)
            self.assertFalse(bus.allow_writes)
        self.assertFalse(self.job.pending_path.exists())
    def test_failed_save_rolls_back_partial_hardware_and_keeps_original_file(self):
        self.ready()
        follower=self.buses['follower'];original=follower.calibration_write;failed=[False]
        def fail_once(i,key,value):
            if key=='homing_offset' and not failed[0]:
                failed[0]=True
                raise RuntimeError('Injected acknowledgement failure')
            original(i,key,value)
        follower.calibration_write=fail_once
        with self.assertRaisesRegex(RuntimeError,'Previous calibration restored'): self.job.save()
        self.assertEqual(json.loads(self.path.read_text()),self.config)
        for role,bus in self.buses.items(): self.assertEqual(bus.stored,self.config[role]['motors'])
        self.assertFalse(self.job.pending_path.exists())
    def test_unrecoverable_save_leaves_persistent_motion_block(self):
        self.ready();self.buses['follower'].fail=True
        with self.assertRaisesRegex(RuntimeError,'Recovery needed'): self.job.save()
        self.assertTrue(self.job.pending_path.exists())
        self.assertTrue(Path(json.loads(self.job.pending_path.read_text())['backup']).exists())
        self.assertEqual(json.loads(self.path.read_text()),self.config)
    def test_ranges_must_cover_both_sides_and_cancel_does_not_change_calibration(self):
        self.job.capture_centers()
        with self.assertRaisesRegex(ValueError,'both sides'): self.job.review()
        self.assertEqual(self.job.step,'ranges')
        for role,bus in self.buses.items(): self.assertEqual(bus.stored,self.config[role]['motors'])
        self.assertEqual(json.loads(self.path.read_text()),self.config)
    def test_changed_file_and_torque_on_refuse_save_before_writes(self):
        self.ready()
        self.buses['follower'].torque=1
        with self.assertRaisesRegex(RuntimeError,'torque OFF'): self.job.save()
        self.buses['follower'].torque=0
        changed=copy.deepcopy(self.config);changed['leader']['port']='/different'
        self.path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError,'file changed'): self.job.save()
        self.assertFalse(self.job.pending_path.exists())


class NewControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'teleoperation.json';self.path.write_text(json.dumps(configuration()))
        self.patch=patch.object(c,'Bus',CalibrationBus);self.patch.start()
        FakeBus.calls=[]
        self.controller=c.CalibrationController(self.path)
        self.controller.open_page('page')
        self.wait(lambda s:s['connected'] and len(s['joints'])==6)
    def tearDown(self):
        self.controller.close();self.patch.stop();self.tmp.cleanup()
    def wait(self,predicate):
        end=time.monotonic()+3
        while time.monotonic()<end:
            state=self.controller.snapshot()
            if predicate(state): return state
            time.sleep(.01)
        self.fail(str(self.controller.snapshot()))
    def begin(self):
        self.controller.command('page','recalibrate-begin',{'supported':True})
        return self.wait(lambda s:s['recalibration'] and not s['calibration_busy'])
    def test_recalibration_requires_support_and_locks_out_other_pages_and_movement(self):
        with self.assertRaises(ValueError): self.controller.command('page','recalibrate-begin')
        self.assertFalse(any(a=='calibration-write' for _,a,_ in FakeBus.calls))
        self.begin()
        self.controller.open_page('other')
        for action in ('reset','test','check-connections','release'):
            with self.assertRaises(ValueError): self.controller.command('page',action)
        with self.assertRaises(ValueError): self.controller.command('other','recalibrate-center')
        with self.assertRaises(ValueError): self.controller.command('page','recalibrate-save')
        self.controller.close_page('page')
        self.wait(lambda s:not s['recalibration'] and not s['calibration_busy'])
        self.assertEqual(json.loads(self.path.read_text()),configuration())
        self.assertEqual({thread for _,_,thread in FakeBus.calls},{self.controller.thread.ident})
    def test_close_before_begin_cancels_release_and_resets_busy_state(self):
        with self.controller.lock:
            self.controller.command('page','recalibrate-begin',{'supported':True})
            self.controller.close_page('page')
        self.wait(lambda s:s['phase']=='offline')
        self.assertFalse(self.controller.snapshot()['calibration_busy'])
        self.assertFalse(any(a=='calibration-write' for _,a,_ in FakeBus.calls))
    def test_wizard_collects_reviews_and_saves_without_motion(self):
        self.begin();self.controller.command('page','recalibrate-center')
        self.wait(lambda s:s['recalibration']['step']=='ranges' and not s['calibration_busy'])
        for value in (1400,2700):
            self.controller.leader.raw={i:value for i in c.IDS}
            self.controller.follower.raw={i:value for i in c.IDS}
            self.wait(lambda s:s['recalibration']['ranges']['follower']['1'][0 if value==1400 else 1]==value)
        self.controller.command('page','recalibrate-review')
        self.wait(lambda s:s['recalibration']['step']=='review' and not s['calibration_busy'])
        self.controller.command('page','recalibrate-save')
        state=self.wait(lambda s:not s['recalibration'] and not s['calibration_busy'])
        self.assertTrue(state['calibration_ok']);self.assertFalse(any(state['torque'].values()))
        self.assertEqual(json.loads(self.path.read_text()),self.controller.config)
        self.assertFalse(any(a in ('arm','send') for _,a,_ in FakeBus.calls))
    def test_pending_recovery_blocks_reconnect_but_allows_guided_calibration(self):
        (self.path.parent/'recalibration_pending.json').write_text('{}')
        self.controller.update(error='Pending recovery')
        self.controller.command('page','check-connections')
        self.wait(lambda s:s['phase']=='error' and not s['connected'])
        with self.assertRaises(ValueError): self.controller.command('page','test')
        self.begin()
        self.assertEqual(self.controller.snapshot()['recalibration']['step'],'centers')
    def test_restore_requires_support_and_applies_checkpoint_on_the_serial_worker(self):
        original_id = self.controller.snapshot()['checkpoints'][0]['id']
        selected = copy.deepcopy(self.controller.config)
        selected['leader']['motors']['1']['homing_offset'] = 10
        selected['follower']['motors']['6']['homing_offset'] = -10
        rest = {'follower_position': {i: 2051 for i in c.IDS},
                'calibration': {role: selected[role]['motors'] for role in ('leader', 'follower')}}
        checkpoint_id = self.controller.history.capture(selected, rest)
        data = {'supported': True, 'checkpoint_id': checkpoint_id}
        for invalid in ({'checkpoint_id': checkpoint_id}, {'supported': True}, {'supported': True, 'checkpoint_id': '../teleoperation'}):
            with self.assertRaises(ValueError): self.controller.command('page', 'recalibrate-restore', invalid)
        self.assertFalse(any(a=='calibration-write' for _,a,_ in FakeBus.calls))
        with self.controller.lock:
            self.controller.command('page', 'recalibrate-restore', data)
            with self.assertRaises(ValueError): self.controller.command('page', 'test')
            with self.assertRaises(ValueError): self.controller.command('page', 'recalibrate-begin', {'supported': True})
        state = self.wait(lambda s:not s['calibration_busy'] and 'restored and verified' in s['message'])
        self.assertTrue(state['calibration_ok'])
        self.assertEqual(state['reset_pose'], rest['follower_position'])
        self.assertEqual(self.controller.leader.stored, selected['leader']['motors'])
        self.assertEqual(self.controller.follower.stored, selected['follower']['motors'])
        self.assertEqual([v['id'] for v in state['checkpoints'] if v['current']], [checkpoint_id])
        self.assertFalse(any(a in ('arm', 'send') for _,a,_ in FakeBus.calls))
        self.assertEqual({thread for _,_,thread in FakeBus.calls}, {self.controller.thread.ident})
        self.controller.close()
        self.controller = c.CalibrationController(self.path)
        self.assertEqual(self.controller.snapshot()['reset_pose'], rest['follower_position'])
        self.assertEqual(len(self.controller.snapshot()['checkpoints']), 2)
        self.controller.open_page('page');self.wait(lambda s:s['connected'] and len(s['joints'])==6)
        self.controller.command('page', 'recalibrate-restore', {'supported': True, 'checkpoint_id': original_id})
        state = self.wait(lambda s:not s['calibration_busy'] and 'restored and verified' in s['message'])
        self.assertEqual(self.controller.config, configuration())
        self.assertIsNone(state['reset_pose'])
    def test_closing_page_before_restore_does_not_release_or_write_motors(self):
        data = {'supported': True, 'checkpoint_id': self.controller.snapshot()['checkpoints'][0]['id']}
        with self.controller.lock:
            self.controller.command('page', 'recalibrate-restore', data)
            self.controller.close_page('page')
        self.wait(lambda s:s['phase']=='offline')
        self.assertFalse(any(a=='calibration-write' for _,a,_ in FakeBus.calls))
        self.assertFalse(self.controller.snapshot()['calibration_busy'])
    def test_continuous_test_follows_until_stop_or_last_page_close_and_holds(self):
        def follow(leader,follower,config,stop,on_frame):
            follower.arm(follower.positions())
            while not stop.wait(.01):
                on_frame({'leader':leader.positions(),'follower':follower.positions(),'phase':'follow','log_path':'test-log'})
            raise c.StopRequested()
        with patch.object(c,'run',side_effect=follow):
            for close in (False,True):
                self.controller.command('page','test')
                self.wait(lambda s:s['phase']=='following')
                with self.assertRaises(ValueError): self.controller.command('page','recalibrate-begin',{'supported':True})
                if close:
                    self.controller.close_page('page');self.wait(lambda s:s['phase']=='offline')
                else:
                    self.controller.command('page','stop');self.wait(lambda s:s['phase']=='holding')
        self.assertIn('hold',[a for _,a,_ in FakeBus.calls])
        self.assertNotIn('release',[a for _,a,_ in FakeBus.calls])
        self.assertFalse(any(role=='leader' and a in ('arm','send') for role,a,_ in FakeBus.calls))
    def test_following_error_holds_without_releasing(self):
        def fail(leader,follower,*args):
            follower.arm(follower.positions());raise RuntimeError('Follower fault')
        with patch.object(c,'run',side_effect=fail):
            self.controller.command('page','test')
            state=self.wait(lambda s:s['phase']=='error')
        self.assertEqual(state['error'],'Follower fault')
        self.assertIn('hold',[a for _,a,_ in FakeBus.calls])
        self.assertNotIn('release',[a for _,a,_ in FakeBus.calls])

if __name__=='__main__': unittest.main()
