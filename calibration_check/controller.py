"""One worker owns both serial ports; HTTP requests never touch the motors."""
import copy
from datetime import datetime
import json
from pathlib import Path
import threading
import time

from teleoperation import Bus, IDS, NAMES, TICKS_PER_DEGREE, StopRequested, mapped_positions, range_warnings, run, validate_config
from motion import move_to, validate_pose
from recalibration import CalibrationHistory, Recalibration, atomic_json

ROOT = Path(__file__).resolve().parent


class CalibrationController:
    def __init__(self, config_path=ROOT / 'config/teleoperation.json', camera=None, wrist_camera=None, recorder=None, evaluation=None, bootstrap=None):
        self.recorder = recorder
        self.evaluation = evaluation
        self.bootstrap = bootstrap
        self.bootstrap_request = None
        self.bootstrap_owner = None
        self.evaluation_request = None
        self.evaluation_owner = None
        self.camera = camera
        self.wrist_camera = wrist_camera
        self.config = json.loads(Path(config_path).read_text())
        self.config_path = Path(config_path)
        validate_config(self.config)
        rest_path = self.config_path.parent / 'rest_pose.json'
        self.rest_pose = json.loads(rest_path.read_text()) if rest_path.exists() else None
        self.history = CalibrationHistory(self.config_path)
        self.history.capture(self.config, self.rest_pose)
        self.log_folder = Path(config_path).parent.parent / 'logs'
        self.lock = threading.RLock()
        self.clients = set()
        self.pending = None
        self.gripper_request = None
        self.recalibration = None
        self.calibration_owner = None
        self.restore_checkpoint = None
        self.stop_event = threading.Event()
        self.quit_event = threading.Event()
        self.wake = threading.Event()
        self.leader = self.follower = None
        self.last_diagnostics = 0
        self.event_id = 0
        self.warning_times = {}
        self.state = {'app': 'so101-calibration-check', 'phase': 'offline', 'connected': False,
                      'calibration_ok': False, 'error': None, 'message': 'Open the interface to inspect both arms.',
                      'joints': [], 'torque': {}, 'diagnostics': {}, 'hz': None, 'log_path': None,
                      'checking_connections': False, 'connections': {},
                      'recalibration': None, 'calibration_busy': False, 'warnings': {}, 'events': [],
                      'gripper_force': None, 'gripper_feedback': None, 'gripper_busy': False, 'gripper_error': None,
                      'ports': {role: self.config[role]['port'] for role in ('leader', 'follower')}}
        if self.evaluation:
            self.evaluation.on_event = self.log_event
        if self.bootstrap:
            self.bootstrap.on_event = self.log_event
        self.state.update(self.history.snapshot(self.config, self.rest_pose))
        self.log_event('INFO', 'Warning-only motion checks enabled. Calibration Test has no application speed cap; reset ramps at up to 10°/s. Pose/tracking warnings do not stop motion. Motor faults and lost communication remain errors.')
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def update(self, **values):
        with self.lock:
            error_changed = values.get('error') and values['error'] != self.state['error']
            message_changed = values.get('message') and values['message'] != self.state['message']
            self.state.update(values)
            if error_changed:
                self.log_event('ERROR', values['error'])
            if message_changed:
                self.log_event('INFO', values['message'])

    def log_event(self, level, message):
        with self.lock:
            self.event_id += 1
            self.state['events'].append({'id': self.event_id,
                'time': datetime.now().astimezone().isoformat(timespec='milliseconds'),
                'level': level, 'message': message})
            self.state['events'] = self.state['events'][-200:]

    def record_warnings(self, warnings):
        now = time.monotonic()
        with self.lock:
            self.state['warnings'] = warnings
            for code, message in warnings.items():
                if now-self.warning_times.get(code, float('-inf')) >= 5:
                    self.log_event('WARNING', message)
                    self.warning_times[code] = now

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        result['camera'] = self.camera.status() if self.camera else {'state': 'off'}
        result['wrist_camera'] = self.wrist_camera.status() if self.wrist_camera else {'state': 'off'}
        try:
            result.update(reset_pose=self._reset_target(), reset_pose_error=None)
        except ValueError as error:
            result.update(reset_pose=None, reset_pose_error=str(error))
        if self.recorder:
            result['episode'] = self.recorder.snapshot()
        if self.evaluation:
            result['evaluation'] = self.evaluation.snapshot()
            result['evaluation']['active'] = bool(self.evaluation_owner)
        if self.bootstrap:
            result['bootstrap'] = self.bootstrap.snapshot()
            result['bootstrap']['active'] = bool(self.bootstrap_owner)
        return result

    def _teach_rest(self):
        leader = self.leader.positions()
        target = validate_pose({i: round(v) for i, v in mapped_positions(leader, self.config).items()}, self.config['follower']['motors'])
        self.rest_pose = {'name':'Taught resting pose', 'source':'leader',
                         'captured_at':datetime.now().astimezone().isoformat(),
                         'leader_position':leader, 'follower_position':target,
                         'calibration':{role:copy.deepcopy(self.config[role]['motors']) for role in ('leader','follower')}}
        atomic_json(self.config_path.parent/'rest_pose.json', self.rest_pose)
        self.history.capture(self.config, self.rest_pose)
        self.update(message='Resting pose saved from the leader. The follower did not move.',
                    **self.history.snapshot(self.config, self.rest_pose))

    def _reset_target(self):
        if not self.rest_pose:
            raise ValueError('No resting pose is saved. Teach a resting pose from the leader before resetting.')
        calibration = {role: self.config[role]['motors'] for role in ('leader', 'follower')}
        if self.rest_pose.get('calibration') != calibration:
            raise ValueError('Calibration changed. Teach the resting pose again before resetting.')
        return validate_pose(self.rest_pose.get('follower_position'), self.config['follower']['motors'])

    def open_page(self, session):
        with self.lock:
            if session in self.clients:
                raise ValueError('This page session is already connected.')
            first = not self.clients
            self.clients.add(session)
            if first:
                self.pending = 'connect'
                self.state.update(phase='connecting', message='Checking stored calibration and current poses…', error=None)
                self.wake.set()

    def close_page(self, session):
        with self.lock:
            self.clients.discard(session)
            if self.bootstrap and session == self.bootstrap_owner:
                self.bootstrap.recorder.mark_stop('page_closed')
                self.stop_event.set()
                if self.pending == 'bootstrap':
                    self.pending = None
                    self.bootstrap_owner = self.bootstrap_request = None
                    self.state['phase'] = 'holding' if any(self.state['torque'].values()) else 'ready'
            if self.bootstrap and not self.clients:
                self.bootstrap.check_stop.set()
            if self.evaluation and session == self.evaluation_owner:
                self.evaluation.recorder.mark_stop('page_closed')
                self.stop_event.set()
            if self.evaluation and not self.clients:
                self.evaluation.check_stop.set()
            if self.recorder and self.recorder.owner == session:
                self.recorder.mark_stop('page_closed')
                self.stop_event.set()
            if session == self.calibration_owner and self.clients:
                self.pending = 'recalibrate-cancel'
                self.wake.set()
            if not self.clients:
                self.stop_event.set()
                self.gripper_request = None
                self.state['gripper_busy'] = False
                self.pending = 'disconnect'
                self.wake.set()

    def has_page(self, session):
        with self.lock:
            return session in self.clients

    def command(self, session, action, data=None):
        with self.lock:
            if session not in self.clients:
                raise ValueError('Open the calibration interface before operating the arm.')
            if action.startswith('bootstrap-'):
                raise ValueError('GPT policy integration is unavailable.')
            if self.bootstrap and (self.bootstrap_owner or self.bootstrap.recorder.snapshot()['status'] == 'awaiting_label') and action != 'stop':
                raise ValueError('End and label the GPT rollout before starting another operation.')
            if action.startswith('evaluation-'):
                raise ValueError('Policy inference is unavailable.')
            if self.evaluation and (self.evaluation_owner or self.evaluation.recorder.snapshot()['status'] == 'awaiting_label') and action != 'stop':
                raise ValueError('End and label the evaluation before starting another operation.')
            if action.startswith('episode-'):
                return self._episode_command(session, action, data)
            if self.recorder and self.recorder.snapshot()['status'] == 'recording' and action not in ('stop', 'gripper-read', 'gripper-force'):
                raise ValueError('End the recording episode before starting another operation.')
            if action == 'stop':
                if self.recorder:
                    self.recorder.mark_stop('stop_and_hold')
                if self.evaluation:
                    self.evaluation.recorder.mark_stop('stop_and_hold')
                if self.bootstrap:
                    self.bootstrap.recorder.mark_stop('stop_and_hold')
                self.log_event('INFO', 'Stop & hold requested by the user.')
                self.stop_event.set()
                self.gripper_request = None
                self.state['gripper_busy'] = False
                if self.pending in ('reset', 'test'):
                    self.pending = None
                    self.state['phase'] = 'error' if self.state['error'] else 'holding' if any(self.state['torque'].values()) else 'ready'
                if self.pending == 'bootstrap':
                    self.pending = None
                    self.bootstrap_owner = self.bootstrap_request = None
                    self.state['phase'] = 'holding' if any(self.state['torque'].values()) else 'ready'
                self.state.update(message='Stopping motion and retaining holding torque…')
                self.wake.set()
                return
            if action in ('gripper-read', 'gripper-force'):
                if self.pending or self.state['gripper_busy'] or self.state['checking_connections'] or self.state['calibration_busy'] or self.recalibration or self.state['phase'] in ('connecting', 'starting', 'resetting', 'releasing'):
                    raise ValueError('Wait for the current operation to finish before adjusting gripper force.')
                percent = data.get('percent') if isinstance(data, dict) else None
                if action == 'gripper-force' and (type(percent) is not int or not 1 <= percent <= 100):
                    raise ValueError('Gripper force must be a whole percentage from 1 to 100.')
                self.gripper_request = (session, percent)
                self.state.update(gripper_busy=True, gripper_error=None)
                self.wake.set()
                return
            if self.pending or self.state['gripper_busy'] or self.state['checking_connections'] or self.state['calibration_busy'] or self.state['phase'] in ('connecting', 'starting', 'following', 'evaluating', 'bootstrapping', 'resetting', 'releasing'):
                raise ValueError('Stop the current operation first.')
            recalibration_actions = {'recalibrate-center': 'centers', 'recalibrate-review': 'ranges', 'recalibrate-save': 'review', 'recalibrate-cancel': None}
            if self.recalibration:
                if session != self.calibration_owner or action not in recalibration_actions:
                    raise ValueError('Finish or cancel Re-calibration in the page that started it.')
                expected = recalibration_actions[action]
                if expected and self.recalibration.step != expected:
                    raise ValueError('This calibration step is no longer current.')
            elif action in recalibration_actions:
                raise ValueError('Start Re-calibration first.')
            if action not in ('check-connections', 'reset', 'test', 'release', 'teach-rest', 'recalibrate-begin', 'recalibrate-restore', *recalibration_actions):
                raise ValueError('Unknown action.')
            if action not in ('check-connections', 'reset', 'recalibrate-begin', 'recalibrate-restore') and not self.state['connected']:
                raise ValueError('Use Check Connection to connect and inspect the arms first.')
            if action == 'teach-rest' and (not self.state['calibration_ok'] or self.state['error']):
                raise ValueError('Calibrate and check the arms before teaching a resting pose.')
            if action == 'test' and (not self.state['calibration_ok'] or self.state['error']):
                raise ValueError('Resolve the error and use Check Connection before starting.')
            if action == 'reset':
                self._reset_target()
            if action in ('recalibrate-begin', 'recalibrate-restore'):
                if not data or data.get('supported') is not True:
                    raise ValueError('Confirm that both arms are supported before releasing torque.')
                if action == 'recalibrate-restore':
                    self.history.load(data.get('checkpoint_id'), self.config)
                    self.restore_checkpoint = data['checkpoint_id']
                self.calibration_owner = session
            if action.startswith('recalibrate-'):
                self.state.update(calibration_busy=True, error=None)
            elif action in ('reset', 'test'):
                self.stop_event.clear()
                self.state.update(phase='starting' if action == 'test' else 'resetting', message='Starting direct leader following with warning-only motion checks.' if action == 'test' else 'Checking the gripper fault before resetting to the saved resting pose…')
            elif action == 'release':
                self.state.update(phase='releasing', message='Releasing the supported follower…')
            elif action == 'check-connections':
                self.state.update(checking_connections=True, connections={}, message='Checking both cameras and both arms…')
            self.pending = action
            self.log_event('INFO', f'Requested: {action}.')
            self.wake.set()





    def _episode_command(self, session, action, data):
        if not self.recorder:
            raise ValueError('Episode recording is unavailable in this server.')
        if action == 'episode-end':
            self.log_event('INFO', 'End seed recording requested; stopping and holding the follower.')
            self.recorder.mark_stop('user_ended')
            self.stop_event.set()
            self.wake.set()
            return
        if action == 'episode-label':
            self.recorder.label(data.get('label'))
            self.log_event('INFO', f'Seed episode labeled {data.get("label")}: {self.recorder.folder}')
            return
        if action != 'episode-start':
            raise ValueError('Unknown episode action.')
        if (self.pending or self.state['phase'] not in ('ready', 'holding') or
                not self.state['connected'] or not self.state['calibration_ok'] or self.state['error'] or
                self.state['checking_connections'] or self.state['gripper_busy'] or self.recalibration):
            raise ValueError('Finish the current operation and resolve connection or motor errors before recording.')
        for key in ('instruction', 'success_condition'):
            if not isinstance(data.get(key), str) or not 1 <= len(data[key].strip()) <= 500:
                raise ValueError('Enter a task instruction and success condition (up to 500 characters each).')
        context = {'purpose': 'seed_demonstration', 'source': 'human_leader',
                   'instruction': data['instruction'].strip(), 'success_condition': data['success_condition'].strip(),
                   'calibration': copy.deepcopy(self.config), 'rest_pose': copy.deepcopy(self.rest_pose),
                   'motor_order': list(IDS), 'joint_names': list(NAMES),
                   'position_units': 'calibrated encoder ticks; 4096 ticks per revolution; not Cartesian pose',
                   'action_units': 'absolute follower joint targets in encoder ticks, after calibration mapping and bounds',
                   'require_robot_metadata': True, 'requested_control_hz': 30, 'eef_pose_available': False, 'gripper_width_m_available': False}
        self.recorder.start(session, context)
        self.stop_event.clear()
        self.pending = 'episode'
        self.update(phase='starting', message='Recording a seed demonstration and starting leader following…')
        self.wake.set()

    def _finish_episode(self, error=None):
        if not self.recorder or self.recorder.snapshot().get('robot_finished', True):
            return
        try:
            terminal = {'leader': self.leader.positions(), 'follower': self.follower.positions(),
                        'observed_at': time.time(), 'observed_monotonic_ns': time.monotonic_ns()}
        except Exception as caught:
            terminal = {'error': str(caught), 'observed_at': time.time()}
            error = error or f'Terminal joint observation unavailable: {caught}'
        self.recorder.finish_robot(terminal, error)

    def _process_gripper_request(self):
        with self.lock:
            request, self.gripper_request = self.gripper_request, None
        if request is None:
            return
        session, percent = request
        if not self.has_page(session):
            self.update(gripper_busy=False)
            return
        temporary = self.follower is None
        bus = Bus('follower', self.config['follower']) if temporary else self.follower
        try:
            if temporary:
                bus.open()
            values = bus.gripper_settings() if percent is None else bus.set_gripper_force(percent)
            self.update(gripper_force=values, gripper_error=None)
            self.log_event('INFO', f'Follower motor 6 output limit {"read" if percent is None else "applied and verified"}: {values["percent"]:g}%. Motor maximum: {values["max_percent"]}%.')
            if percent is not None:
                self.log_event('WARNING', 'An output limit does not guarantee overload-free gripping. Sustained object contact can still trigger protection; changing force does not clear an existing fault.')
        except Exception as error:
            self.update(gripper_force=None, gripper_error=str(error))
            self.log_event('ERROR', f'Gripper force: {error}')
            if not temporary and not isinstance(error, ValueError):
                raise
        finally:
            if temporary:
                bus.close()
            self.update(gripper_busy=False)

    def _recalibrate(self, action):
        try:
            if action in ('recalibrate-begin', 'recalibrate-restore'):
                if not self.state['connected']:
                    self.leader, self.follower = (Bus(role, self.config[role]) for role in ('leader', 'follower'))
                    self.follower.open()
                    self.leader.open()
                self.recalibration = Recalibration({'leader': self.leader, 'follower': self.follower}, self.config_path,
                                                   self.restore_checkpoint if action == 'recalibrate-restore' else None)
                self.update(phase='calibrating', connected=True, calibration_ok=False, torque={i: 0 for i in IDS}, joints=[],
                            message='Both arms are torque OFF. Place them in matching center poses, then capture centers.')
                if action == 'recalibrate-restore':
                    self._save_calibration(restored=True)
            elif action == 'recalibrate-center':
                self.recalibration.capture_centers()
                self.update(message='Recording ranges. Move each joint by hand through its comfortable range on both arms.')
            elif action == 'recalibrate-review':
                self.recalibration.review()
                self.update(message='Review the measured ranges. Save writes and verifies the new calibration.')
            elif action == 'recalibrate-save':
                self._save_calibration()
            elif action == 'recalibrate-cancel':
                self.recalibration = None
                self.calibration_owner = None
                self._connect()
                self.update(message='Re-calibration cancelled. Calibration was not saved. Torque remains OFF.')
        except ValueError as error:
            if action not in ('recalibrate-center', 'recalibrate-review'):
                raise
            self.update(error=str(error), message='Continue the guided calibration or cancel it.')
        finally:
            self.update(calibration_busy=False, recalibration=self.recalibration.snapshot() if self.recalibration else None,
                        **self.history.snapshot(self.config, self.rest_pose))

    def _save_calibration(self, restored=False):
        self.config, backup = self.recalibration.save()
        self.rest_pose = self.recalibration.rest_pose
        self.recalibration = None
        self.calibration_owner = None
        self.restore_checkpoint = None
        self.last_diagnostics = 0
        result = 'Selected calibration restored and verified.' if restored else 'New calibration saved and verified.'
        rest = 'Matching reset pose is available.' if self.rest_pose else 'Teach a new resting pose before using Clear Error & Reset.'
        self.update(phase='ready', calibration_ok=True, message=f'{result} Both arms remain torque OFF. {rest} Backup: {backup}')

    def _check_connections(self):
        results = {}
        cameras = {'wrist': self.wrist_camera, 'front': self.camera}
        try:
            for name, camera in cameras.items():
                if camera:
                    try:
                        camera.start()
                    except Exception as error:
                        results[name] = {'connected': False, 'error': str(error)}
                else:
                    results[name] = {'connected': False, 'error': 'Camera is not configured.'}
            for role in ('leader', 'follower'):
                bus = getattr(self, role)
                temporary = bus is None
                if temporary:
                    bus = Bus(role, self.config[role])
                try:
                    if temporary:
                        bus.open()
                    if any(bus.read(i, 'model') != 777 for i in IDS):
                        raise ValueError('Expected six SO-101 motors to respond.')
                    results[role] = {'connected': True, 'error': None}
                except Exception as error:
                    results[role] = {'connected': False, 'error': str(error)}
                finally:
                    if temporary:
                        bus.close()
                self.update(connections=copy.deepcopy(results))
            deadline = time.monotonic() + 8
            while any(name not in results for name in cameras):
                if self.quit_event.is_set() or not self.clients:
                    return
                for name, camera in cameras.items():
                    if name in results:
                        continue
                    info = camera.status()
                    if info['state'] == 'live':
                        fresh = time.time() - info.get('captured_at', 0) <= 1
                        results[name] = {'connected': fresh, 'error': None if fresh else 'No fresh camera images.'}
                    elif info['state'] in ('error', 'off') or time.monotonic() >= deadline:
                        results[name] = {'connected': False, 'error': info.get('error') or 'Camera did not deliver an image. Check USB and camera permission.'}
                self.update(connections=copy.deepcopy(results))
                if any(name not in results for name in cameras):
                    time.sleep(0.05)
            if all(results[role]['connected'] for role in ('leader', 'follower')):
                if not self.state['connected'] or self.state['error']:
                    self._connect()
            count = sum(row['connected'] for row in results.values())
            self.update(message=f'Connection check complete: {count}/4 devices responded.')
        finally:
            self.update(checking_connections=False)

    def _move(self, target):
        error = None
        stopped = False
        def frame(values):
            self._refresh(values)
            self.update(phase='resetting', log_path=values['log_path'])
        try:
            move_to(self.follower, target, self.stop_event, frame, self.log_folder, 'reset')
        except StopRequested:
            stopped = True
        except Exception as caught:
            error = str(caught)
        finally:
            hold_error = self.follower.hold()
            error = error or hold_error
            self.update(phase='error' if error else 'holding', error=error, hz=None,
                        message='Motion stopped. Torque was not switched off.' if error or stopped else 'Reset complete. Holding the saved resting pose.')

    def _reset(self):
        target = self._reset_target()
        if (self.config_path.parent / 'recalibration_pending.json').exists():
            raise ValueError('An incomplete calibration save needs recovery. Finish Re-calibration before resetting.')
        for role in ('follower', 'leader'):
            if getattr(self, role) is None:
                bus = Bus(role, self.config[role])
                setattr(self, role, bus)
                bus.open()
        self.leader.verify()
        if self.stop_event.is_set():
            self.update(phase='error' if self.state['error'] else 'holding', message='Reset cancelled. Torque was not switched off.')
            return
        self.follower.clear_gripper_overload()
        self.follower.verify()
        self.last_diagnostics = 0
        self._refresh()
        self.update(connected=True, calibration_ok=True, error=None,
                    message='Fault checks passed. Slowly resetting to the saved resting pose at up to 10°/s with a gentle start and stop.')
        self._move(target)

    def _disconnect(self):
        with self.lock:
            self.gripper_request = None
            self.bootstrap_owner = self.bootstrap_request = None
        hold_error = self.follower.hold() if self.follower else None
        self._finish_episode(hold_error or 'Recording interrupted by the page closing or a connection error.')
        for bus in (self.follower, self.leader):
            if bus:
                bus.close()
        self.leader = self.follower = None
        self.recalibration = None
        self.calibration_owner = None
        self.restore_checkpoint = None
        self.update(connected=False, phase='offline', hz=None, warnings={}, gripper_force=None, gripper_feedback=None, gripper_busy=False,
                    recalibration=None, calibration_busy=False, message='Interface closed. Motor torque was left unchanged.', error=hold_error)

    def _connect(self):
        self._disconnect()
        if self.config.get('calibrated') is False:
            raise ValueError('First-time setup: run Configure Devices, then Re-calibration. Motion is disabled until calibration is saved.')
        self.update(phase='connecting', error=None, calibration_ok=False)
        self.leader = Bus('leader', self.config['leader'])
        self.follower = Bus('follower', self.config['follower'])
        self.follower.open()
        self.leader.open()
        self.leader.verify()
        self.follower.verify()
        if (self.config_path.parent / 'recalibration_pending.json').exists():
            raise ValueError('An incomplete calibration save needs recovery. Run Re-calibration before moving either arm.')
        self.last_diagnostics = 0
        self.update(connected=True, calibration_ok=True, error=None, phase='ready',
                    message='Stored calibration matches all 12 motors. Inspection does not move the arms.')
        self._refresh()

    def _joints(self, leader, follower):
        expected = mapped_positions(leader, self.config)
        result = []
        for i, name in zip(IDS, NAMES):
            entry = {'id': i, 'name': name.replace('_', ' '),
                     'leader': (leader[i] - 2048) / TICKS_PER_DEGREE,
                     'follower': (follower[i] - 2048) / TICKS_PER_DEGREE,
                     'target': (expected[i] - 2048) / TICKS_PER_DEGREE,
                     'error': (expected[i] - follower[i]) / TICKS_PER_DEGREE}
            for role, positions in (('leader', leader), ('follower', follower)):
                cal = self.config[role]['motors'][i]
                entry[role + '_percent'] = 100 * (positions[i] - cal['range_min']) / (cal['range_max'] - cal['range_min'])
            result.append(entry)
        return result

    def _refresh(self, frame=None):
        self._process_gripper_request()
        now = time.monotonic()
        leader = frame['leader'] if frame and 'leader' in frame else self.leader.positions()
        follower = frame['follower'] if frame else self.follower.positions()
        values = {'joints': self._joints(leader, follower), 'updated_at': time.time()}
        if frame and frame.get('gripper_feedback') is not None:
            values['gripper_feedback'] = frame['gripper_feedback']
        warnings = {**range_warnings('leader', leader, self.config['leader']['motors']),
                    **range_warnings('follower', follower, self.config['follower']['motors'])}
        if frame:
            warnings.update(frame.get('warnings', {}))
        elif self.state['phase'] in ('ready', 'holding'):
            worst = max(values['joints'], key=lambda joint: abs(joint['error']))
            if abs(worst['error']) > 30:
                warnings['startup-gap'] = f'{worst["name"].capitalize()} starting gap is {abs(worst["error"]):.1f}°. Starting following can cause a sudden move; this is a warning, not a start restriction.'
        self.record_warnings(warnings)
        if now - self.last_diagnostics >= 1:
            diagnostics = self.follower.diagnostics()
            errors = [row['error'] for row in diagnostics.values() if 'error' in row]
            if errors:
                raise RuntimeError(errors[0])
            values.update(diagnostics=diagnostics, torque={i: row['torque'] for i, row in diagnostics.items()})
            self.last_diagnostics = now
        if self.snapshot()['phase'] in ('ready', 'holding'):
            torque = values.get('torque', self.snapshot()['torque'])
            values['phase'] = 'holding' if any(torque.values()) else 'ready'
        self.update(**values)

    def _test(self):
        error = None
        recording = self.recorder and not self.recorder.snapshot().get('robot_finished', True)
        def frame(values):
            if recording:
                self.recorder.observe_robot(values)
                if self.recorder.snapshot()['status'] != 'recording':
                    self.stop_event.set()
            self._refresh(values)
            self.update(phase='following', log_path=values['log_path'],
                        message='Following the leader. Motion warnings do not pause control; Stop & hold ends the test.')
        try:
            kwargs = {'log_path': self.recorder.folder / 'robot.jsonl'} if recording else {}
            run(self.leader, self.follower, self.config, self.stop_event, frame, **kwargs)
        except StopRequested:
            pass
        except Exception as caught:
            error = str(caught)
        finally:
            hold_error = self.follower.hold()
            error = error or hold_error
            self.update(phase='error' if error else 'holding', error=error,
                        message=('Episode ended. Choose Success or Failure; holding torque remains on.' if recording else 'Calibration test stopped. Holding torque remains on.') if not error else 'Control stopped after an error. Torque was not switched off.')
            if recording:
                self._finish_episode(error)

    def _worker(self):
        try:
            while not self.quit_event.is_set():
                self.wake.wait(0.1)
                self.wake.clear()
                with self.lock:
                    action, self.pending = self.pending, None
                    online = bool(self.clients)
                try:
                    if action == 'disconnect' or (not online and self.follower):
                        self._disconnect()
                        if self.camera:
                            self.camera.stop()
                        if self.wrist_camera:
                            self.wrist_camera.stop()
                    elif online:
                        self._process_gripper_request()
                        if action == 'connect':
                            if self.camera:
                                self.camera.start()
                            if self.wrist_camera:
                                self.wrist_camera.start()
                            self._connect()
                        elif action == 'check-connections':
                            self._check_connections()
                        elif action == 'reset' and not self.stop_event.is_set():
                            self._reset()
                        elif action == 'teach-rest':
                            self._teach_rest()
                        elif action == 'episode' or (action == 'test' and not self.stop_event.is_set()):
                            self._test()
                        elif action and action.startswith('recalibrate-'):
                            self._recalibrate(action)
                        elif action == 'release':
                            self.follower.allow_writes = True
                            errors = self.follower.release()
                            if errors:
                                raise RuntimeError('; '.join(errors))
                            self.update(phase='ready', error=None, message='Follower torque is OFF. Keep the arm supported.')
                            self.last_diagnostics = 0
                        if self.recalibration:
                            if self.recalibration.step == 'ranges':
                                self.recalibration.sample()
                            self.update(recalibration=self.recalibration.snapshot())
                        elif self.follower and self.snapshot()['connected']:
                            self._refresh()
                except Exception as error:
                    self._disconnect()
                    self.update(phase='error', error=str(error), calibration_ok=False,
                                message='Reset blocked. Resolve the reported fault, then try Clear Error & Reset again.' if action == 'reset' else 'Could not inspect the arms. Use Check Connection when ready.')
        finally:
            self._disconnect()
            if self.camera:
                self.camera.stop()
            if self.wrist_camera:
                self.wrist_camera.stop()

    def close(self):
        if self.bootstrap:
            self.bootstrap.check_stop.set()
            self.bootstrap.recorder.mark_stop('server_shutdown')
        if self.evaluation:
            self.evaluation.check_stop.set()
            self.evaluation.recorder.mark_stop('server_shutdown')
        if self.recorder:
            self.recorder.mark_stop('server_shutdown')
        self.stop_event.set()
        self.quit_event.set()
        self.wake.set()
        self.thread.join(timeout=10)
