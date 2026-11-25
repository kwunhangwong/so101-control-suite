#!/usr/bin/env python3
"""Interactive leader/follower control using existing motor calibration."""
import argparse
import json
from pathlib import Path
import signal
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
NAMES = ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper')
IDS = tuple(str(i) for i in range(1, 7))
FPS = 30
TICKS_PER_DEGREE = 4096 / 360
REGISTERS = {'model': (3, 2), 'range_min': (9, 2), 'range_max': (11, 2), 'homing_offset': (31, 2), 'mode': (33, 1), 'torque': (40, 1), 'position': (56, 2), 'load': (60, 2), 'voltage': (62, 1), 'temperature': (63, 1), 'status': (65, 1), 'current': (69, 2)}
REGISTERS.update(max_torque_limit=(16, 2), protective_torque=(34, 1), protection_time=(35, 1), overload_torque=(36, 1), torque_limit=(48, 2))


class StopRequested(Exception):
    pass


def validate_config(config):
    if config['leader']['port'] == config['follower']['port']:
        raise ValueError('Leader and follower must have different USB ports.')
    for role in ('leader', 'follower'):
        motors = config[role]['motors']
        if set(motors) != set(IDS):
            raise ValueError(f'{role}: expected motor IDs 1 through 6.')
        for motor in motors.values():
            if motor['model'] != 777 or not 0 <= motor['range_min'] < motor['range_max'] <= 4095:
                raise ValueError(f'{role}: invalid STS3215 calibration.')
            if not -2047 <= motor['homing_offset'] <= 2047:
                raise ValueError(f'{role}: invalid homing offset.')


def mapped_positions(positions, config):
    goals = {}
    for i in IDS:
        source, target = config['leader']['motors'][i], config['follower']['motors'][i]
        fraction = (positions[i] - source['range_min']) / (source['range_max'] - source['range_min'])
        fraction = max(0, min(1, fraction))
        goals[i] = target['range_min'] + fraction * (target['range_max'] - target['range_min'])
    return goals


def calibrated_goals(requested, motors):
    """Respect the stored motor limits, without an application speed cap."""
    return {i: max(motors[i]['range_min'], min(motors[i]['range_max'], round(requested[i]))) for i in IDS}


def range_warnings(role, positions, motors):
    warnings = {}
    for i, name in zip(IDS, NAMES):
        low, high = motors[i]['range_min'], motors[i]['range_max']
        distance = min(positions[i]-low, high-positions[i]) / TICKS_PER_DEGREE
        if distance < 0:
            warnings[f'{role}-range-{i}'] = f'{role.title()} {name.replace("_", " ")} is outside its calibrated range. Targets remain within the stored motor limits.'
        elif i != '5' and distance < 5:
            warnings[f'{role}-range-{i}'] = f'{role.title()} {name.replace("_", " ")} is {distance:.1f}° from a calibrated endpoint.'
    return warnings


def tracking_warnings(observed, goals):
    return {f'tracking-{i}': f'{name.replace("_", " ").capitalize()} target gap is {abs(goals[i]-observed[i])/TICKS_PER_DEGREE:.1f}°. Commands continue; check for obstruction or overload.'
            for i, name in zip(IDS, NAMES) if abs(goals[i]-observed[i]) > 10*TICKS_PER_DEGREE}


class Bus:
    def __init__(self, role, config):
        self.role, self.config = role, config
        self.port = None
        self.allow_writes = False
        self.calibration_writes = False
        self.gripper_force_write = False
        self.retries = 0

    def open(self):
        from scservo_sdk import PortHandler, PacketHandler
        from serial import Serial
        self.port = PortHandler(self.config['port'])
        write = self.port.writePort

        def guarded(packet):
            instruction = packet[4]
            if instruction not in (1, 2):
                register = (packet[5], len(packet) - 7)
                calibration = self.calibration_writes and register in ((9, 2), (11, 2), (31, 2), (55, 1), (40, 1))
                if calibration and register == (40, 1) and packet[6] != 0:
                    calibration = False
                motion = not self.calibration_writes and self.allow_writes and self.role == 'follower' and register in ((40, 1), (42, 2))
                force = self.gripper_force_write and not self.calibration_writes and self.role == 'follower' and packet[2] == 6 and register == (48, 2)
                if not (calibration or motion or force) or instruction != 3 or packet[2] not in range(1, 7):
                    raise RuntimeError('Blocked an unauthorized motor write.')
            return write(packet)

        self.port.writePort = guarded
        self.sdk = PacketHandler(0)
        # Acquire the port lock before configuring or flushing a live controller.
        self.port.ser = Serial(port=self.config['port'], baudrate=self.port.baudrate, timeout=0, exclusive=True)
        self.port.is_open = True
        self.port.tx_time_per_byte = 10000 / self.port.baudrate
        self.port.ser.reset_input_buffer()
        self.port.getCurrentTime = lambda: time.monotonic() * 1000
        # Same timeout correction used by LeRobot for the published Feetech SDK.
        def packet_timeout(length):
            self.port.packet_start_time = self.port.getCurrentTime()
            self.port.packet_timeout = self.port.tx_time_per_byte * (length + 3) + 50
        self.port.setPacketTimeout = packet_timeout

    def transfer(self, method, *args, allowed_fault=0):
        if allowed_fault and (allowed_fault != 32 or self.role != 'follower' or args[0] != 6):
            raise ValueError('Only follower gripper recovery and force settings may read through an overload flag.')
        for attempt in range(3):
            self.port.ser.reset_input_buffer()
            response = method(self.port, *args)
            result, fault = response[-2:]
            if result == 0 and (fault & ~allowed_fault) == 0:
                return response
            if result in (-6, -7) and fault == 0 and attempt < 2:
                self.retries += 1
                continue
            raise RuntimeError(f'{self.role} motor {args[0]}: {self.sdk.getTxRxResult(result)} {self.sdk.getRxPacketError(fault)}')

    def read(self, i, name, *, allowed_fault=0):
        address, width = REGISTERS[name]
        method = self.sdk.read1ByteTxRx if width == 1 else self.sdk.read2ByteTxRx
        value, _, _ = self.transfer(method, int(i), address, allowed_fault=allowed_fault)
        bit = 11 if name == 'homing_offset' else 15
        if name in ('homing_offset', 'position') and value & (1 << bit):
            value = -(value & ~(1 << bit))
        return value

    def verify(self):
        for i in IDS:
            saved = self.config['motors'][i]
            if any(self.read(i, key) != saved[key] for key in ('model', 'range_min', 'range_max', 'homing_offset')):
                raise ValueError(f'{self.role} motor {i}: calibration differs from the inspected values. Recheck it before running.')
            torque = self.read(i, 'torque')
            if self.read(i, 'mode') != 0 or torque not in (0, 1) or (self.role == 'leader' and torque != 0):
                raise ValueError(f'{self.role} motor {i}: expected position mode, with leader torque OFF.')

    def positions(self):
        return {i: self.read(i, 'position') for i in IDS}

    def write(self, i, address, value, width, *, allowed_fault=0):
        method = self.sdk.write1ByteTxRx if width == 1 else self.sdk.write2ByteTxRx
        self.transfer(method, int(i), address, value, allowed_fault=allowed_fault)

    def clear_gripper_overload(self):
        if self.role != 'follower':
            raise ValueError('Overload recovery is only available for the follower gripper.')
        # Feetech releases overload protection on a new command. Use the measured
        # position, never the old blocked goal, and never toggle torque or EEPROM.
        # https://www.feetechrc.com/20210430-56680.html
        saved = self.config['motors']['6']
        for name in ('model', 'range_min', 'range_max', 'homing_offset'):
            if self.read('6', name, allowed_fault=32) != saved[name]:
                raise ValueError('Follower gripper calibration differs; recheck before resetting.')
        if self.read('6', 'mode', allowed_fault=32) != 0 or self.read('6', 'torque', allowed_fault=32) not in (0, 1):
            raise ValueError('Follower gripper must be in position mode before resetting.')
        status = self.read('6', 'status', allowed_fault=32)
        if status & ~32:
            raise RuntimeError(f'Follower gripper has another active fault (status {status}); reset blocked.')
        if status & 32:
            position = self.read('6', 'position', allowed_fault=32)
            if not saved['range_min'] <= position <= saved['range_max']:
                raise ValueError('Follower gripper is outside its calibrated range; reset blocked.')
            previous = self.allow_writes
            self.allow_writes = True
            try:
                self.write('6', 42, position, 2, allowed_fault=32)
            finally:
                self.allow_writes = previous
        if self.read('6', 'status'):
            raise RuntimeError('Follower gripper fault remains; remove the load or obstruction before retrying.')

    def arm(self, positions):
        self.allow_writes = True
        # Set the hold targets before enabling torque; never use a stale goal.
        self.send(positions)
        for i in IDS:
            self.write(i, 40, 1, 1)

    def gripper_settings(self):
        if self.role != 'follower':
            raise ValueError('Gripper force is only available for follower motor 6.')
        for name in ('model', 'range_min', 'range_max', 'homing_offset'):
            if self.read('6', name, allowed_fault=32) != self.config['motors']['6'][name]:
                raise ValueError('Follower gripper calibration differs; check it before adjusting force.')
        if self.read('6', 'mode', allowed_fault=32) != 0:
            raise ValueError('Follower gripper must be in position mode.')
        values = {name: self.read('6', name, allowed_fault=32) for name in
                  ('max_torque_limit', 'overload_torque', 'protective_torque', 'protection_time')}
        values.update(self.gripper_feedback())
        if not 10 <= values['max_torque_limit'] <= 1000 or not 0 <= values['torque_limit'] <= 1000:
            raise ValueError('Invalid motor torque-limit reading; no force change was made.')
        return {**values, 'percent': values['torque_limit']/10,
                'max_percent': values['max_torque_limit']//10,
                'read_at': time.time()}

    def gripper_feedback(self):
        """Read motor 6 only; expose overload for diagnosis, never clear it."""
        if self.role != 'follower':
            raise ValueError('Gripper feedback is only available for the follower.')
        data, _, fault = self.transfer(self.sdk.readTxRx, 6, 42, 29, allowed_fault=32)
        if len(data) != 29:
            raise RuntimeError('Incomplete gripper feedback packet.')
        values = {}
        for name, (address, width) in {'goal': (42, 2), **{k: REGISTERS[k] for k in
                ('torque_limit', 'position', 'load', 'current', 'voltage', 'temperature', 'status')}}.items():
            value = int.from_bytes(bytes(data[address-42:address-42+width]), 'little')
            if name in ('goal', 'position') and value & 32768:
                value = -(value & 32767)
            values[name] = value
        values['status'] |= fault
        return {**values, 'read_at': time.time()}

    def set_gripper_force(self, percent):
        if type(percent) is not int or not 1 <= percent <= 100:
            raise ValueError('Gripper force must be a whole percentage from 1 to 100.')
        before = self.gripper_settings()
        if percent > before['max_percent']:
            raise ValueError(f'Gripper force exceeds the motor maximum of {before["max_percent"]}%.')
        # SRAM only: never change EEPROM, protection thresholds, targets or torque enable.
        # LeRobot STS control table: Torque_Limit = address 48, two bytes, 0..1000.
        self.gripper_force_write = True
        try:
            self.write('6', 48, percent*10, 2, allowed_fault=32)
        finally:
            self.gripper_force_write = False
        after = self.gripper_settings()
        if after['torque_limit'] != percent*10:
            raise RuntimeError('Gripper force readback did not match. Re-read the motor setting before continuing.')
        return after

    def calibration_write(self, i, name, value):
        registers = {**REGISTERS, 'lock': (55, 1)}
        if i not in IDS or type(value) is not int or name not in ('torque', 'lock', 'homing_offset', 'range_min', 'range_max'):
            raise ValueError('Invalid calibration write.')
        low, high = (-2047, 2047) if name == 'homing_offset' else (0, 1) if name == 'lock' else (0, 0) if name == 'torque' else (0, 4095)
        if not low <= value <= high:
            raise ValueError('Calibration value is outside the motor limits.')
        if name != 'torque' and self.read(i, 'torque') != 0:
            raise ValueError('Motor torque must be OFF for calibration writes.')
        encoded = abs(value) | (1 << 11) if value < 0 else value
        address, width = registers[name]
        self.calibration_writes = True
        try:
            self.write(i, address, encoded, width)
        finally:
            self.calibration_writes = False

    def send(self, positions):
        for i in IDS:
            self.write(i, 42, positions[i], 2)

    def release(self):
        errors = []
        if self.allow_writes:
            for i in IDS:
                try:
                    self.write(i, 40, 0, 1)
                except Exception as error:
                    errors.append(str(error))
            self.allow_writes = False
        return errors

    def hold(self):
        if not self.allow_writes:
            return None
        try:
            positions = self.positions()
            if any(not self.config['motors'][i]['range_min'] <= positions[i] <= self.config['motors'][i]['range_max'] for i in IDS):
                raise ValueError('Measured hold position is outside calibration.')
            self.send(positions)
        except Exception as error:
            return str(error)
        return None

    def diagnostics(self):
        result = {}
        for i in IDS:
            try:
                result[i] = {key: self.read(i, key) for key in ('position', 'torque', 'load', 'current', 'voltage', 'temperature', 'status')}
            except Exception as error:
                result[i] = {'error': str(error)}
        return result

    def close(self):
        if self.port and self.port.is_open:
            self.port.closePort()


def run(leader, follower, config, stop_event=None, on_frame=None, log_path=None):
    leader.verify()
    follower.verify()
    initial = follower.positions()
    if stop_event and stop_event.is_set():
        raise StopRequested()
    folder = ROOT / 'logs' if log_path is None else Path(log_path).parent
    folder.mkdir(exist_ok=True)
    path = folder / f'teleoperation_{time.time_ns()}.jsonl' if log_path is None else Path(log_path)
    with path.open('x') as log:
        log.write(json.dumps({'event': 'start', 'config': config, 'fps': FPS, 'units': 'encoder ticks; 4096 ticks per revolution', 'motion_checks': 'warnings_only', 'max_target_degrees_per_second': None, 'initial': initial, 'gripper_settings': follower.gripper_settings()}) + '\n')
        print('FOLLOWING with warning-only motion checks and no application speed cap. Ctrl+C stops.', flush=True)
        follower.arm(calibrated_goals(initial, config['follower']['motors']))
        previous, started, last = initial, time.monotonic(), time.monotonic()
        warned = {}
        last_feedback = float('-inf')
        try:
            log.write(json.dumps({'event': 'armed', 'gripper_feedback': follower.gripper_feedback()}) + '\n')
            while True:
                if stop_event and stop_event.is_set():
                    raise StopRequested()
                tick = time.monotonic()
                observation_started_at = time.time()
                source, observed = leader.positions(), follower.positions()
                observation_received_at = time.time()
                observation_monotonic_ns = time.monotonic_ns()
                feedback = None
                if tick-last_feedback >= 0.1:
                    feedback = follower.gripper_feedback()
                    log.write(json.dumps({'event': 'gripper_feedback', 't': tick-started, **feedback}) + '\n')
                    log.flush()
                    last_feedback = tick
                    if feedback['status']:
                        raise RuntimeError(f'Follower motor 6 reports fault status {feedback["status"]}.')
                requested = mapped_positions(source, config)
                goals = calibrated_goals(requested, config['follower']['motors'])
                warnings = {**range_warnings('leader', source, config['leader']['motors']),
                            **range_warnings('follower', observed, config['follower']['motors']),
                            **tracking_warnings(observed, goals)}
                if tick > last and max(abs(goals[i]-previous[i]) for i in IDS)/(tick-last) > 90*TICKS_PER_DEGREE:
                    warnings['fast-target'] = 'Target changes exceed 90°/s. No application speed cap is applied.'
                for code, message in warnings.items():
                    if tick-warned.get(code, float('-inf')) >= 5:
                        print(f'WARNING: {message}', flush=True)
                        warned[code] = tick
                if stop_event and stop_event.is_set():
                    raise StopRequested()
                follower.send(goals)
                frame = {'t': tick - started, 'phase': 'follow', 'leader': source, 'follower': observed, 'command': goals, 'warnings': warnings,
                         'observation_started_at': observation_started_at, 'observation_received_at': observation_received_at,
                         'observation_monotonic_ns': observation_monotonic_ns,
                         'command_sent_at': time.time(), 'command_monotonic_ns': time.monotonic_ns()}
                if feedback is not None:
                    frame['gripper_feedback'] = feedback
                log.write(json.dumps(frame) + '\n')
                if on_frame:
                    on_frame({**frame, 'log_path': str(path)})
                previous, last = goals, tick
                time.sleep(max(0, 1 / FPS - (time.monotonic() - tick)))
        except (KeyboardInterrupt, StopRequested):
            log.write(json.dumps({'event': 'stopped', 'elapsed': time.monotonic() - started}) + '\n')
            raise
        except Exception as error:
            try:
                feedback = follower.gripper_feedback()
            except Exception as diagnostic_error:
                feedback = {'error': str(diagnostic_error)}
            log.write(json.dumps({'event': 'error', 'elapsed': time.monotonic() - started, 'error': str(error), 'gripper_feedback': feedback, 'follower_diagnostics': follower.diagnostics(), 'serial_retries': {'leader': leader.retries, 'follower': follower.retries}}) + '\n')
            raise


def open_release_in_app(config):
    url = 'http://127.0.0.1:8766'
    try:
        with urlopen(url + '/api/status', timeout=1) as response:
            state = json.load(response)
        if not isinstance(state, dict) or state.get('app') != 'so101-calibration-check' or not state.get('connected') or state.get('ports', {}).get('follower') != config['follower']['port']:
            return False
    except (OSError, ValueError):
        return False
    webbrowser.open(url + '/?release=1')
    print(f'The app owns the arm connection. Open {url}/?release=1, support the follower arm, and confirm Release torque there.\nTorque has not been changed by this shortcut.')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read and verify both arms, without enabling motors.')
    parser.add_argument('--release', action='store_true', help='Explicitly release follower torque after supporting the arm.')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config/teleoperation.json').read_text())
    validate_config(config)
    if config.get('calibrated') is False:
        raise ValueError('Complete first-time calibration in the app before using this controller.')
    leader, follower = Bus('leader', config['leader']), Bus('follower', config['follower'])
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        if args.release:
            if open_release_in_app(config):
                return 0
            if input('Support the follower arm. Type RELEASE to turn its motor torque OFF: ').strip() != 'RELEASE':
                print('Cancelled. Torque unchanged.')
                return 0
            follower.open()
            follower.allow_writes = True
            errors = follower.release()
            if errors:
                raise RuntimeError('; '.join(errors))
            print('Follower motor torque is OFF.')
            return 0
        follower.open()
        leader.open()
        leader.verify()
        follower.verify()
        target, actual = mapped_positions(leader.positions(), config), follower.positions()
        torque = {i: follower.read(i, 'torque') for i in IDS}
        print(f'Both arms verified. Follower torque: {torque}. Calibration has not been changed.')
        for i, name in zip(IDS, NAMES):
            print(f'  {name:16s} start-pose difference: {(target[i]-actual[i])/TICKS_PER_DEGREE:+.1f} degrees')
        if args.check:
            return 0
        print('\nWarning-only following: no application speed cap or startup alignment gate. Ctrl+C stops.')
        print('START immediately commands the leader pose. A large pose difference can cause a sudden move; clear the reach before starting.')
        print('Ctrl+C stops motion and keeps holding torque. Support the arm before opening http://127.0.0.1:8766/?release=1 in the Calibration app.')
        if input('Type START when ready, or press Enter to cancel: ').strip() != 'START':
            print('Cancelled. Motor torque unchanged.')
            return 0
        run(leader, follower, config)
        return 0
    except KeyboardInterrupt:
        print('\nStopped.')
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        if 'exclusively lock port' in str(error):
            if args.release and open_release_in_app(config):
                return 0
            print('The arm USB port is already in use. In the app, choose Stop & hold, then Release torque. Otherwise close other arm-control or Release Arm Terminal windows and retry. Torque was not switched off.')
        else:
            print(f'Cannot continue: {error}')
        return 1
    finally:
        holding = follower.allow_writes
        hold_error = follower.hold()
        follower.close()
        leader.close()
        if holding:
            print('Motion stopped; motor torque was NOT switched off. Support the arm before opening http://127.0.0.1:8766/?release=1 in the Calibration app.')
            if hold_error:
                print(f'Could not update the hold position: {hold_error}. Motors retain their last target if power and communication are intact.')


if __name__ == '__main__':
    raise SystemExit(main())
