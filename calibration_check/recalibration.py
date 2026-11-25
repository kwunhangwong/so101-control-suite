"""Manual center/range capture with torque off; EEPROM changes only on Save.

Feetech: Present_Position = Actual_Position - Homing_Offset.
Reference: https://github.com/huggingface/lerobot/blob/v0.4.0/src/lerobot/motors/feetech/feetech.py
"""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time

from teleoperation import IDS, NAMES, validate_config
from motion import validate_pose


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as handle:
        name = handle.name
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def matching_rest(config, rest):
    if not rest or rest.get('calibration') != {role: config[role]['motors'] for role in ('leader', 'follower')}:
        return None
    validate_pose(rest.get('follower_position'), config['follower']['motors'])
    return copy.deepcopy(rest)


class CalibrationHistory:
    def __init__(self, config_path):
        self.folder = Path(config_path).parent / 'calibration_checkpoints'

    def load(self, checkpoint_id, config):
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r'checkpoint_[0-9]+', checkpoint_id):
            raise ValueError('Choose a saved calibration checkpoint.')
        try:
            record = json.loads((self.folder / f'{checkpoint_id}.json').read_text())
            if record['version'] != 1:
                raise ValueError('Unsupported checkpoint version.')
            saved = record['config']
            validate_config(saved)
            for role in ('leader', 'follower'):
                if saved[role]['port'] != config[role]['port']:
                    raise ValueError('Checkpoint belongs to different arm USB identities.')
                for motor in saved[role]['motors'].values():
                    if set(motor) != {'model', 'homing_offset', 'range_min', 'range_max'} or any(type(v) is not int for v in motor.values()):
                        raise ValueError('Invalid checkpoint motor settings.')
            if record['rest_pose'] is not None and matching_rest(saved, record['rest_pose']) != record['rest_pose']:
                raise ValueError('Checkpoint resting pose does not match its calibration.')
            if datetime.fromisoformat(record['saved_at']).utcoffset() is None:
                raise ValueError('Checkpoint timestamp requires a time zone.')
            return record
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError(f'Cannot use {checkpoint_id}: {error}') from error

    def records(self, config):
        records, errors = [], []
        for path in sorted(self.folder.glob('checkpoint_*.json'), reverse=True):
            try:
                records.append((path.stem, self.load(path.stem, config)))
            except ValueError as error:
                errors.append(str(error))
        return records, errors

    def capture(self, config, rest):
        if config.get("calibrated") is False:
            return None
        rest = matching_rest(config, rest)
        records, _ = self.records(config)
        for checkpoint_id, record in records:
            if record['config'] == config and record['rest_pose'] == rest:
                return checkpoint_id
        self.folder.mkdir(exist_ok=True)
        checkpoint_id = f'checkpoint_{time.time_ns()}'
        atomic_json(self.folder / f'{checkpoint_id}.json', {
            'version': 1, 'saved_at': datetime.now().astimezone().isoformat(timespec='microseconds'),
            'config': config, 'rest_pose': rest})
        return checkpoint_id

    def snapshot(self, config, rest):
        records, errors = self.records(config)
        rest = matching_rest(config, rest)
        return {'checkpoints': [
            {'id': checkpoint_id, 'saved_at': record['saved_at'],
             'label': datetime.fromisoformat(record['saved_at']).astimezone().strftime('%Y-%m-%d %H:%M:%S'),
             'current': record['config'] == config and record['rest_pose'] == rest,
             'has_rest_pose': record['rest_pose'] is not None}
            for checkpoint_id, record in records], 'checkpoint_errors': errors}


def motor_calibration(bus):
    result = {}
    for i in IDS:
        if bus.read(i, 'model') != 777 or bus.read(i, 'mode') != 0:
            raise ValueError(f'{bus.role}: expected six STS3215 motors in position mode.')
        result[i] = {key: bus.read(i, key) for key in ('model', 'range_min', 'range_max', 'homing_offset')}
    return result


def write_calibration(bus, motors):
    if any(bus.read(i, 'torque') != 0 for i in IDS):
        raise ValueError('Support both arms and release torque before saving calibration.')
    for i in IDS:
        try:
            bus.calibration_write(i, 'lock', 0)
            for key in ('homing_offset', 'range_min', 'range_max'):
                bus.calibration_write(i, key, motors[i][key])
        finally:
            bus.calibration_write(i, 'lock', 1)
    if motor_calibration(bus) != motors:
        raise RuntimeError(f'{bus.role}: calibration readback did not match.')


class Recalibration:
    def __init__(self, buses, config_path, checkpoint_id=None):
        self.buses = buses
        self.path = Path(config_path)
        self.pending_path = self.path.parent / 'recalibration_pending.json'
        self.original = json.loads(self.path.read_text())
        self.history = CalibrationHistory(self.path)
        # Validate the entire selection before touching torque or EEPROM.
        checkpoint = self.history.load(checkpoint_id, self.original) if checkpoint_id is not None else None
        self.rest_path = self.path.parent / 'rest_pose.json'
        self.rest_existed = self.rest_path.exists()
        self.original_rest = json.loads(self.rest_path.read_text()) if self.rest_existed else None
        self.history.capture(self.original, self.original_rest)
        self.rest_pose = checkpoint['rest_pose'] if checkpoint else self.original_rest
        self.previous = {role: motor_calibration(bus) for role, bus in buses.items()}
        self.proposed = copy.deepcopy(checkpoint['config'] if checkpoint else self.original)
        self.centers = {}
        self.ranges = {}
        self.step = 'review' if checkpoint else 'centers'
        # Only called after the user confirms that both arms are supported.
        for bus in buses.values():
            for i in IDS:
                bus.calibration_write(i, 'torque', 0)
            bus.allow_writes = False
        self._torque_off()

    def _torque_off(self):
        for bus in self.buses.values():
            if any(bus.read(i, 'torque') != 0 for i in IDS):
                raise RuntimeError('Calibration requires both arms to remain torque OFF.')

    def _raw(self, role):
        positions = self.buses[role].positions()
        return {i: (positions[i] + self.previous[role][i]['homing_offset']) % 4096 for i in IDS}

    def capture_centers(self):
        self._torque_off()
        centers = {role: self._raw(role) for role in self.buses}
        for role in self.buses:
            for i in IDS:
                offset = centers[role][i] - 2047
                if not -2047 <= offset <= 2047:
                    raise ValueError(f'{role} {NAMES[int(i)-1]}: adjust the center slightly and capture again.')
            self.proposed[role]['motors'] = copy.deepcopy(self.previous[role])
        self.centers = centers
        self.ranges = {role: {i: [2047, 2047] for i in IDS} for role in self.buses}
        self.step = 'ranges'

    def sample(self):
        self._torque_off()
        for role in self.buses:
            raw = self._raw(role)
            for i in IDS:
                value = (raw[i] - self.centers[role][i] + 2047) % 4096
                bounds = self.ranges[role][i]
                bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)

    def review(self):
        self.sample()
        for role in self.buses:
            for i in IDS:
                low, high = self.ranges[role][i]
                if i == '5':
                    # Continuous wrist roll retains the full single-turn encoder range.
                    low, high = 0, 4095
                elif high-low < 300 or low > 1947 or high < 2147:
                    raise ValueError(f'{role} {NAMES[int(i)-1]}: record both sides of its center and its full comfortable range.')
                elif high-low > 3800:
                    raise ValueError(f'{role} {NAMES[int(i)-1]} crosses the encoder boundary. Cancel and choose a better center pose.')
                self.proposed[role]['motors'][i].update(homing_offset=self.centers[role][i]-2047, range_min=low, range_max=high)
        validate_config(self.proposed)
        self.step = 'review'

    def snapshot(self):
        return {'step': self.step, 'ranges': copy.deepcopy(self.ranges),
                'proposed': copy.deepcopy(self.proposed) if self.step == 'review' else None}

    def save(self):
        if self.step != 'review':
            raise ValueError('Capture centers and review the measured ranges before saving.')
        self._torque_off()
        # Do not overwrite a configuration changed by another program during the wizard.
        if json.loads(self.path.read_text()) != self.original:
            raise ValueError('The calibration file changed. Cancel and start again.')
        current_rest = json.loads(self.rest_path.read_text()) if self.rest_path.exists() else None
        if current_rest != self.original_rest or self.rest_path.exists() != self.rest_existed:
            raise ValueError('The resting pose file changed. Cancel and start again.')
        self.proposed["calibrated"] = True
        validate_config(self.proposed)
        rest = matching_rest(self.proposed, self.rest_pose)
        backup_dir = self.path.parent / 'calibration_backups'
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f'calibration_{time.time_ns()}.json'
        record = {'previous_config': self.original, 'previous_motors': self.previous, 'proposed': self.proposed,
                  'previous_rest_pose': self.original_rest, 'rest_existed': self.rest_existed, 'proposed_rest_pose': rest}
        atomic_json(backup, record)
        was_pending = self.pending_path.exists()
        atomic_json(self.pending_path, {'backup': str(backup)})
        try:
            for role, bus in self.buses.items():
                write_calibration(bus, self.proposed[role]['motors'])
            atomic_json(self.path, self.proposed)
            atomic_json(self.rest_path, rest)
            self.history.capture(self.proposed, rest)
            self.pending_path.unlink()
        except Exception as error:
            rollback_errors = []
            for role, bus in self.buses.items():
                try:
                    write_calibration(bus, self.previous[role])
                except Exception as caught:
                    rollback_errors.append(f'{role}: {caught}')
            try:
                atomic_json(self.path, self.original)
                if self.rest_existed:
                    atomic_json(self.rest_path, self.original_rest)
                else:
                    self.rest_path.unlink(missing_ok=True)
                if not rollback_errors and not was_pending:
                    self.pending_path.unlink()
            except Exception as caught:
                rollback_errors.append(str(caught))
            detail = 'Previous calibration restored.' if not rollback_errors else 'Recovery needed; motion remains blocked: ' + '; '.join(rollback_errors)
            raise RuntimeError(f'Calibration save failed: {error}. {detail} Backup: {backup}') from error
        for role, bus in self.buses.items():
            bus.config = copy.deepcopy(self.proposed[role])
        self.rest_pose = rest
        return copy.deepcopy(self.proposed), str(backup)
