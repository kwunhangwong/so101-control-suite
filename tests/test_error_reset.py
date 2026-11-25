import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"calibration_check"))
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from teleoperation import Bus, REGISTERS
from test_teleoperation import configuration


class FaultMotor:
    def __init__(self, fault=32, persistent=False):
        self.fault, self.persistent = fault, persistent
        self.writes = []
        self.values = {'model': 777, 'range_min': 100, 'range_max': 3900,
                       'homing_offset': 0, 'mode': 0, 'torque': 1, 'position': 1708}

    def read(self, port, i, address):
        name = next(name for name, (register, _) in REGISTERS.items() if register == address)
        return (self.fault if name == 'status' else self.values[name], 0, self.fault)

    def write(self, port, i, address, value):
        old_fault = self.fault
        self.writes.append((i, address, value))
        if not self.persistent:
            self.fault = 0
        return (0, old_fault)

    def bus(self):
        bus = Bus('follower', configuration()['follower'])
        bus.port = SimpleNamespace(ser=SimpleNamespace(reset_input_buffer=MagicMock()))
        bus.sdk = SimpleNamespace(read1ByteTxRx=self.read, read2ByteTxRx=self.read,
                                  write1ByteTxRx=self.write, write2ByteTxRx=self.write,
                                  getTxRxResult=str, getRxPacketError=lambda fault: f'fault {fault}')
        return bus


class ErrorResetTest(unittest.TestCase):
    def test_overload_recovers_only_gripper_at_measured_position(self):
        motor = FaultMotor()
        bus = motor.bus()
        with self.assertRaisesRegex(RuntimeError, 'fault 32'):
            bus.read('6', 'position')
        bus.clear_gripper_overload()
        self.assertEqual(motor.writes, [(6, 42, 1708)])
        self.assertFalse(bus.allow_writes)
        self.assertEqual(bus.read('6', 'status'), 0)
        self.assertEqual(motor.values['torque'], 1)

    def test_persistent_overload_is_not_hidden(self):
        motor = FaultMotor(persistent=True)
        bus = motor.bus()
        with self.assertRaisesRegex(RuntimeError, 'fault 32'):
            bus.clear_gripper_overload()
        self.assertEqual(motor.writes, [(6, 42, 1708)])
        self.assertFalse(bus.allow_writes)

    def test_other_faults_and_invalid_pose_or_calibration_block_writes(self):
        for fault, change in [(36, {}), (0, {'model': 1}), (32, {'mode': 1}),
                              (32, {'position': 50}), (32, {'homing_offset': 1})]:
            with self.subTest(fault=fault, change=change):
                motor = FaultMotor(fault)
                motor.values.update(change)
                with self.assertRaises((RuntimeError, ValueError)):
                    motor.bus().clear_gripper_overload()
                self.assertEqual(motor.writes, [])

    def test_healthy_gripper_has_no_writes_and_off_gripper_stays_off(self):
        motor = FaultMotor(0)
        motor.bus().clear_gripper_overload()
        self.assertEqual(motor.writes, [])
        motor = FaultMotor()
        motor.values['torque'] = 0
        motor.bus().clear_gripper_overload()
        self.assertEqual(motor.writes, [(6, 42, 1708)])
        self.assertEqual(motor.values['torque'], 0)

    def test_recovery_mask_cannot_ignore_other_motors_or_communication_errors(self):
        bus = FaultMotor().bus()
        for role, i, mask in [('leader', 6, 32), ('follower', 5, 32), ('follower', 6, 36)]:
            bus.role = role
            with self.assertRaises(ValueError):
                bus.read(str(i), 'position', allowed_fault=mask)
        bus.role = 'follower'
        failed = MagicMock(return_value=(0, -6, 32))
        with self.assertRaises(RuntimeError):
            bus.transfer(failed, 6, 56, allowed_fault=32)
        self.assertEqual(failed.call_count, 1)


if __name__ == '__main__':
    unittest.main()
