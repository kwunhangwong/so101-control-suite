import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import contextlib
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import controller as c
import teleoperation as t
from server import Handler, ThreadingHTTPServer
from test_controller import FakeBus
from test_error_reset import FaultMotor
from test_teleoperation import configuration


def settings(percent=50, status=0):
    return dict(percent=percent, torque_limit=percent*10, max_torque_limit=500,
                max_percent=50, overload_torque=25, protective_torque=20, protection_time=200,
                load=1140, current=28, voltage=122, goal=1588, position=1704, temperature=43, status=status, read_at=time.time())


class GripperBusTest(unittest.TestCase):
    def motor(self, fault=0):
        motor = FaultMotor(fault, persistent=True)
        motor.values.update(torque_limit=500, max_torque_limit=500, overload_torque=25,
                            load=1140, temperature=43, protective_torque=20, protection_time=200,
                            current=28, voltage=122)
        bus = motor.bus()
        def read_block(port, i, address, width):
            self.assertEqual((i, address, width), (6, 42, 29))
            data = bytearray(29)
            for name, (addr, count) in {'goal': (42, 2), **t.REGISTERS}.items():
                if 42 <= addr and addr+count <= 71:
                    value = motor.fault if name == 'status' else motor.values.get(name, 0)
                    data[addr-42:addr-42+count] = value.to_bytes(count, 'little')
            return list(data), 0, motor.fault
        bus.sdk.readTxRx = read_block
        original = motor.write
        def write(port, i, address, value):
            result = original(port, i, address, value)
            if address == 48:
                motor.values['torque_limit'] = value
            return result
        bus.sdk.write2ByteTxRx = write
        return motor, bus

    def test_applies_only_motor_6_sram_and_reads_back_even_with_overload(self):
        for fault in (0, 32):
            motor, bus = self.motor(fault)
            result = bus.set_gripper_force(20)
            self.assertEqual(result['percent'], 20)
            self.assertEqual(result['status'], fault)
            self.assertEqual(motor.fault, fault)
            self.assertEqual(motor.writes, [(6, 48, 200)])
            self.assertEqual(motor.values['torque'], 1)
            self.assertEqual(motor.values['position'], 1708)
            self.assertEqual(motor.values['overload_torque'], 25)
            self.assertFalse(bus.allow_writes)
            self.assertFalse(bus.gripper_force_write)

    def test_invalid_values_calibration_and_other_faults_never_write(self):
        for value in (0, 51, 101, -1, True, 20.5, '20', None):
            motor, bus = self.motor()
            with self.assertRaises(ValueError): bus.set_gripper_force(value)
            self.assertEqual(motor.writes, [])
        for changes in ({'model': 1}, {'mode': 1}, {'range_min': 99}):
            motor, bus = self.motor()
            motor.values.update(changes)
            with self.assertRaises(ValueError): bus.set_gripper_force(20)
            self.assertEqual(motor.writes, [])
        motor, bus = self.motor(36)
        with self.assertRaises(RuntimeError): bus.set_gripper_force(20)
        self.assertEqual(motor.writes, [])
        motor, bus = self.motor()
        bus.role = 'leader'
        with self.assertRaises(ValueError): bus.set_gripper_force(20)
        self.assertEqual(motor.writes, [])

    def test_readback_mismatch_and_transport_failure_are_errors(self):
        motor, bus = self.motor()
        bus.sdk.write2ByteTxRx = motor.write
        with self.assertRaisesRegex(RuntimeError, 'readback'): bus.set_gripper_force(20)
        self.assertFalse(bus.gripper_force_write)
        motor, bus = self.motor()
        bus.sdk.write2ByteTxRx = MagicMock(return_value=(-6, 0))
        with self.assertRaises(RuntimeError): bus.set_gripper_force(20)
        self.assertFalse(bus.gripper_force_write)

    def test_packet_guard_permits_only_follower_motor6_register48(self):
        for role in ('leader', 'follower'):
            packets = []
            port = SimpleNamespace(writePort=lambda p: packets.append(p), baudrate=1000000)
            sdk = SimpleNamespace(PortHandler=lambda _: port, PacketHandler=lambda _: None)
            serial = SimpleNamespace(Serial=MagicMock(return_value=SimpleNamespace(reset_input_buffer=MagicMock())))
            bus = t.Bus(role, configuration()[role])
            with patch.dict('sys.modules', {'scservo_sdk': sdk, 'serial': serial}): bus.open()
            packet = [255,255,6,5,3,48,200,0,0]
            with self.assertRaises(RuntimeError): port.writePort(packet)
            bus.gripper_force_write = True
            if role == 'follower': port.writePort(packet)
            else:
                with self.assertRaises(RuntimeError): port.writePort(packet)
            for i, address in [(1,48),(5,48),(6,16),(6,36),(6,42),(6,40),(6,31)]:
                with self.assertRaises(RuntimeError): port.writePort([255,255,i,5,3,address,200,0,0])
            self.assertEqual(len(packets), int(role=='follower'))

    def test_feedback_reads_through_only_overload_and_never_writes(self):
        motor, bus = self.motor(32)
        result = bus.gripper_feedback()
        self.assertEqual(result['status'], 32)
        self.assertEqual(result['torque_limit'], 500)
        self.assertEqual(result['position'], 1708)
        self.assertEqual(result['current'], 28)
        self.assertEqual(result['load'], 1140)
        self.assertEqual(motor.writes, [])
        with self.assertRaisesRegex(RuntimeError, 'fault 32'): bus.positions()
        for reply in (([0]*29, 0, 36), ([], -6, 0), ([0]*28, 0, 0)):
            bus.sdk.readTxRx = MagicMock(return_value=reply)
            with self.assertRaises(RuntimeError): bus.gripper_feedback()
        self.assertEqual(motor.writes, [])


class ForceBus(FakeBus):
    fault = False
    sent = []
    def __init__(self, role, config):
        super().__init__(role, config)
        self.percent = 50
    def verify(self):
        super().verify()
        if self.role == 'follower' and self.fault: raise RuntimeError('follower motor 6: Overload error!')
    def positions(self):
        self.record('read')
        return {i: 3750 if self.role=='leader' else 2000 for i in c.IDS}
    def gripper_settings(self):
        self.record('gripper-read')
        return settings(self.percent, 32 if self.fault else 0)
    def gripper_feedback(self):
        return settings(self.percent, 32 if self.fault else 0)
    def set_gripper_force(self, percent):
        self.record('gripper-write-6-48')
        self.percent = percent
        return self.gripper_settings()
    def send(self, goals):
        self.record('send')
        self.sent.append(dict(goals))


class GripperControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'teleoperation.json'
        self.path.write_text(json.dumps(configuration()))
        ForceBus.calls = []; ForceBus.sent = []; ForceBus.fault = False
        self.bus_patch = patch.object(c, 'Bus', ForceBus)
        self.bus_patch.start()
        self.controller = c.CalibrationController(self.path)
    def tearDown(self):
        self.controller.close()
        self.bus_patch.stop()
        self.tmp.cleanup()
    def wait(self, condition):
        end=time.monotonic()+3
        while time.monotonic()<end:
            state=self.controller.snapshot()
            if condition(state): return state
            time.sleep(.01)
        self.fail(str(self.controller.snapshot()))
    def open(self):
        self.controller.open_page('page')
        return self.wait(lambda s:s['connected'] and len(s['joints'])==6)

    def test_read_only_open_and_apply_use_existing_worker_owned_port(self):
        self.open()
        self.assertFalse(any('gripper' in action for _,action,_ in ForceBus.calls))
        opens=sum(action=='open' for _,action,_ in ForceBus.calls)
        self.controller.command('page','gripper-read')
        self.wait(lambda s:not s['gripper_busy'] and s['gripper_force'])
        self.assertFalse(any('write' in action for _,action,_ in ForceBus.calls))
        self.controller.command('page','gripper-force',{'percent':20})
        state=self.wait(lambda s:not s['gripper_busy'] and s['gripper_force']['percent']==20)
        self.assertEqual(sum(action=='open' for _,action,_ in ForceBus.calls),opens)
        self.assertTrue(any('applied and verified: 20%' in e['message'] for e in state['events']))
        self.assertEqual({owner for _,_,owner in ForceBus.calls},{self.controller.thread.ident})
        self.assertFalse(any(action in ('arm','release','send') for _,action,_ in ForceBus.calls))

    def test_live_adjustment_keeps_following_and_other_joint_commands_unchanged(self):
        self.open()
        with patch.object(t, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.controller.command('page','test')
            self.wait(lambda s:s['phase']=='following')
            opens=sum(action=='open' for _,action,_ in ForceBus.calls)
            self.controller.command('page','gripper-force',{'percent':20})
            state=self.wait(lambda s:not s['gripper_busy'] and s['gripper_force'] and s['gripper_force']['percent']==20)
            count=len(ForceBus.sent)
            self.wait(lambda s:len(ForceBus.sent)>count+2)
            self.assertEqual(state['phase'],'following')
            self.assertIsNone(state['error'])
            self.assertFalse(self.controller.stop_event.is_set())
            self.assertTrue(all(all(goals[i]==3750 for i in c.IDS[:5]) for goals in ForceBus.sent))
            self.assertEqual(sum(action=='open' for _,action,_ in ForceBus.calls),opens)
            self.controller.command('page','stop')
            self.wait(lambda s:s['phase']=='holding')
        self.assertEqual({owner for _,_,owner in ForceBus.calls},{self.controller.thread.ident})

    def test_existing_overload_can_read_and_adjust_without_clear_resume_or_arm(self):
        ForceBus.fault=True
        self.controller.open_page('page')
        self.wait(lambda s:s['phase']=='error' and not s['connected'])
        ForceBus.calls=[]
        self.controller.command('page','gripper-force',{'percent':20})
        state=self.wait(lambda s:not s['gripper_busy'] and s['gripper_force'])
        self.assertEqual(state['gripper_force']['status'],32)
        self.assertEqual(state['phase'],'error')
        self.assertIn('Overload',state['error'])
        self.assertEqual([action for _,action,_ in ForceBus.calls],['open','gripper-write-6-48','gripper-read','close'])

    def test_request_validation_and_stop_cancel_queued_apply(self):
        self.open()
        for value in (0,True,1.5,'20',None,101):
            with self.assertRaises(ValueError): self.controller.command('page','gripper-force',{'percent':value})
        with self.assertRaises(ValueError): self.controller.command('unknown','gripper-force',{'percent':20})
        with self.controller.lock:
            self.controller.command('page','gripper-force',{'percent':20})
            self.controller.command('page','stop')
        self.assertIsNone(self.controller.gripper_request)
        self.assertFalse(self.controller.snapshot()['gripper_busy'])
        self.assertFalse(any('write' in action for _,action,_ in ForceBus.calls))

    def test_http_confirmation_origin_and_body_validation(self):
        self.open()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.controller=self.controller
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        def post(body,origin=None):
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            connection.request('POST','/api/gripper-force?session=page',json.dumps(body),headers)
            response=connection.getresponse();response.read();return response.status
        try:
            self.assertEqual(post({'percent':20}),409)
            self.assertEqual(post({'confirm':'GRIPPER_FORCE','percent':True}),409)
            self.assertEqual(post({'confirm':'GRIPPER_FORCE','percent':20},'https://example.com'),403)
            self.assertEqual(post({'confirm':'GRIPPER_FORCE','percent':20}),200)
            self.wait(lambda s:not s['gripper_busy'] and s['gripper_force'])
        finally:
            connection.close();server.shutdown();server.server_close();thread.join()


if __name__=='__main__': unittest.main()
