"""Save paired camera crops and timestamps; an episode must be labeled before reuse."""
import copy
from datetime import datetime
from io import BytesIO
import json
import hashlib
import math
from pathlib import Path
import threading
import time

from PIL import Image

from storage import episode_manifests, task_folder
from rl_replay import finalize_episode

CAMERAS = ('front', 'wrist')


def local_time():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def crop_bounds(roi, width, height):
    size, bounds = roi.get('source_size_wh'), roi.get('bounds_xyxy')
    if (not isinstance(size, list) or len(size) != 2 or
            any(type(v) is not int or v <= 0 for v in size) or
            not isinstance(bounds, list) or len(bounds) != 4 or
            any(type(v) is not int for v in bounds) or
            not (0 <= bounds[0] < bounds[2] <= size[0] and 0 <= bounds[1] < bounds[3] <= size[1])):
        raise ValueError('Invalid saved ROI. Save a valid region in Camera Setup.')
    # Same round-half-up and exclusive right/bottom convention as roi.mjs.
    p = [math.floor(v * (width, height)[i % 2] / size[i % 2] + .5) for i, v in enumerate(bounds)]
    left, top = max(0, min(width - 1, p[0])), max(0, min(height - 1, p[1]))
    return [left, top, max(left + 1, min(width, p[2])), max(top + 1, min(height, p[3]))]


class EpisodeRecorder:
    def __init__(self, root, roi_root, cameras, controls=None, collection_mode=None):
        self.root, self.roi_root = Path(root), Path(roi_root)
        self.cameras = cameras
        self.controls = controls
        self.collection_mode = collection_mode
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.owner = None
        self.folder = None
        self.robot_frame = None
        self.state = {'status': 'idle', 'path': None, 'frames': 0, 'label': None, 'error': None}
        manifests = self.manifests()
        if manifests:
            self.folder = manifests[-1].parent
            self.state = json.loads(manifests[-1].read_text())
            self.state['path'] = str(self.folder)
            if self.state['status'] == 'recording' or (self.state['status'] == 'awaiting_label' and not self.state.get('robot_finished')):
                self.state.update(status='awaiting_label', robot_finished=True, ended_at=local_time(),
                                  error='Recording was interrupted by the app closing. Review and label the partial episode.')
                samples = self.folder / 'samples.jsonl'
                self.state['frames'] = sum(1 for line in samples.read_text().splitlines() if line.endswith('}')) if samples.exists() else 0
                self._save()

    def manifests(self):
        return episode_manifests(self.root, self.collection_mode)

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            if result['status'] == 'recording':
                result['duration_seconds'] = max(0, time.time() - result['started_unix'])
            return result

    def _save(self):
        temp = self.folder / 'episode.json.tmp'
        temp.write_text(json.dumps(self.state, indent=2) + '\n')
        temp.replace(self.folder / 'episode.json')

    def _frame(self, name):
        camera = self.cameras[name]
        frame, info = camera.sample() if camera else (None, {})
        if not frame or info.get('state') != 'live' or not 0 <= time.time() - info.get('captured_at', 0) <= 1:
            raise ValueError(f'No fresh {name} camera frame. Use Check Connection before recording.')
        return frame, info

    def start(self, owner, context=None):
        with self.lock:
            if self.state['status'] in ('recording', 'awaiting_label'):
                raise ValueError('End and label the current episode before starting another.')
            camera_meta = {}
            for name in CAMERAS:
                _, info = self._frame(name)
                roi = json.loads((self.roi_root / f'{name}_roi.json').read_text())
                camera_meta[name] = {'camera_unique_id': info.get('camera_unique_id'),
                    'source_size_wh': [info['width'], info['height']], 'roi': roi,
                    'crop_bounds_xyxy': crop_bounds(roi, info['width'], info['height'])}
            if self.controls:
                for name in CAMERAS:
                    try:
                        camera_meta[name]['settings_at_start'] = self.controls.query(name)
                    except Exception as error:
                        camera_meta[name]['settings_at_start'] = {'error': str(error)}
            stamp = datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S_%f')
            parent = task_folder(self.root, (context or {}).get('instruction', ''))
            task_name = parent.name
            self.folder = parent / f'episode_{stamp}'
            self.folder.mkdir(parents=True)
            for name in CAMERAS:
                (self.folder / name).mkdir()
            self.owner = owner
            self.robot_frame = None
            self.state = {'schema_version': 1, 'status': 'recording', 'path': str(self.folder),
                'started_at': local_time(), 'started_unix': time.time(), 'ended_at': None,
                'duration_seconds': 0, 'frames': 0, 'label': None, 'error': None,
                'robot_finished': False, 'action_representation': 'so101_absolute_joint_encoder_ticks_v1',
                'controller_owner': (context or {}).get('controller_owner', 'human_leader'), 'native_ik_compatible': False,
                'cameras': camera_meta, 'context': context or {}, 'task_name': task_name,
                'timing': 'Host receive times in Unix seconds. Each pair uses the latest new frame from each camera; cameras are not hardware synchronized.',
                'format': 'ROI-cropped JPEG pairs only, samples.jsonl and robot.jsonl; variable camera frame rate, no duplicated frames.'}
            sources = [Path(__file__), Path(__file__).parent / 'storage.py', Path(__file__).parent.parent / 'calibration_check/teleoperation.py',
                       Path(__file__).parent.parent / 'calibration_check/controller.py',
                       Path(__file__).parent.parent / 'camera_setup/native_camera.py']
            self.state['source_sha256'] = {str(p.relative_to(Path(__file__).parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
            self._save()
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._record, daemon=True)
            self.thread.start()

    def observe_robot(self, frame):
        with self.lock:
            self.robot_frame = {**copy.deepcopy(frame), 'received_at': time.time()}

    def _write_frame(self, name, jpeg, info, stem):
        meta = self.state['cameras'][name]
        if [info['width'], info['height']] != meta['source_size_wh'] or info.get('camera_unique_id') != meta['camera_unique_id']:
            raise ValueError(f'{name.capitalize()} camera changed during recording.')
        relative = f'{name}/{stem}.jpg'
        with Image.open(BytesIO(jpeg)) as image:
            if list(image.size) != meta['source_size_wh']:
                raise ValueError(f'{name.capitalize()} image dimensions do not match the camera metadata.')
            image.crop(meta['crop_bounds_xyxy']).save(self.folder / relative, quality=95)
        return {'file': relative, 'captured_at': info['captured_at'],
                'captured_monotonic_ns': info.get('captured_monotonic_ns'), 'sequence': info['sequence']}

    def mark_stop(self, reason):
        with self.lock:
            if self.state['status'] == 'recording':
                self.state['stop_reason'] = reason

    def _record(self):
        previous = {}
        command_recording = self.state['controller_owner'] == 'gpt' and self.state['context'].get('record_control_steps', False)
        previous_group = None
        try:
            with (self.folder / 'samples.jsonl').open('x') as samples:
                while not self.stop_event.is_set():
                    with self.lock:
                        robot = copy.deepcopy(self.robot_frame)
                    if self.state['context'].get('require_robot_metadata') and robot is None:
                        self.stop_event.wait(.02)
                        continue
                    group = (('hold', robot['recording_sample']) if 'recording_sample' in robot
                             else ('command', robot.get('control_step', 0))) if robot is not None else None
                    if command_recording and (group is None or group == previous_group):
                        self.stop_event.wait(.02)
                        continue
                    frames = {name: self._frame(name) for name in CAMERAS}
                    if any(info['sequence'] == previous.get(name) for name, (_, info) in frames.items()):
                        self.stop_event.wait(.02)
                        continue
                    with self.lock:
                        index = self.state['frames']
                        if not command_recording:
                            robot = copy.deepcopy(self.robot_frame)
                    row = {'index': index, 'elapsed_seconds': time.time() - self.state['started_unix'], 'cameras': {}}
                    for name, (jpeg, info) in frames.items():
                        row['cameras'][name] = self._write_frame(name, jpeg, info, f'{index:06d}')
                        previous[name] = info['sequence']
                    row['camera_skew_seconds'] = abs(frames['front'][1]['captured_at'] - frames['wrist'][1]['captured_at'])
                    if robot is not None:
                        row['robot'] = robot
                    samples.write(json.dumps(row) + '\n')
                    samples.flush()
                    with self.lock:
                        if command_recording:
                            with (self.folder / 'robot.jsonl').open('a') as robot_log:
                                robot_log.write(json.dumps({**robot, 'sample_index': index}, allow_nan=False) + '\n')
                            previous_group = group
                        self.state['frames'] += 1
        except Exception as error:
            with self.lock:
                self.state['error'] = f'Recording stopped: {error}'
        finally:
            with self.lock:
                self.state.update(status='awaiting_label', ended_at=local_time(),
                                  duration_seconds=max(0, time.time() - self.state['started_unix']))
                try:
                    self._save()
                except OSError as error:
                    self.state['error'] = f'Cannot save episode metadata: {error}. Free disk space and label again.'

    def end(self, reason=None):
        with self.lock:
            if self.state['status'] != 'recording':
                return
            if reason:
                self.state['error'] = reason
            self.stop_event.set()
            thread = self.thread
        thread.join()

    def finish_robot(self, terminal, error=None):
        self.end(error)
        terminal['cameras'] = {}
        for name in CAMERAS:
            try:
                deadline = time.monotonic() + 1
                while True:
                    jpeg, info = self._frame(name)
                    if info['captured_at'] >= terminal.get('observed_at', 0):
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError('No new terminal camera frame.')
                    time.sleep(.02)
                terminal['cameras'][name] = self._write_frame(name, jpeg, info, 'terminal')
            except Exception as caught:
                terminal['cameras'][name] = {'error': str(caught)}
                error = error or f'Terminal {name} camera observation unavailable: {caught}'
        with self.lock:
            self.state['robot_finished'] = True
            if error:
                self.state['error'] = self.state['error'] or error
            self.state['terminal_observation_valid'] = not terminal.get('error') and not error
            try:
                if (self.state['context'].get('record_terminal_sample') and not terminal.get('error')
                        and terminal.get('follower') and terminal.get('command')
                        and all('file' in terminal['cameras'].get(name, {}) for name in CAMERAS)):
                    # Include the real final image/state pair in the same selection used
                    # by cleaning and BC. Existing edited episodes are never backfilled.
                    index = self.state['frames']
                    robot = {key: copy.deepcopy(terminal[key]) for key in ('follower', 'command')}
                    robot.update(phase='bootstrap_hold', recording_phase='terminal',
                                 control_step=terminal['control_steps'], step=terminal['control_steps'],
                                 observation_received_at=terminal['observed_at'])
                    elapsed = max(terminal['observed_at'], *(c['captured_at'] for c in terminal['cameras'].values())) - self.state['started_unix']
                    row = {'index': index, 'elapsed_seconds': elapsed, 'terminal': True,
                           'cameras': copy.deepcopy(terminal['cameras']), 'robot': robot,
                           'camera_skew_seconds': abs(terminal['cameras']['front']['captured_at']-terminal['cameras']['wrist']['captured_at'])}
                    with (self.folder / 'samples.jsonl').open('a') as samples:
                        samples.write(json.dumps(row, allow_nan=False) + '\n')
                    with (self.folder / 'robot.jsonl').open('a') as robot_log:
                        robot_log.write(json.dumps({**robot, 'sample_index': index}, allow_nan=False) + '\n')
                    self.state.update(frames=index+1, ended_at=local_time(),
                                      duration_seconds=max(self.state['duration_seconds'], time.time()-self.state['started_unix']))
                (self.folder / 'robot_terminal.json').write_text(json.dumps(terminal, indent=2) + '\n')
                robot_log = self.folder / 'robot.jsonl'
                self.state['control_steps'] = sum(1 for line in robot_log.read_text().splitlines()
                                                  if json.loads(line).get('phase') in ('follow', 'evaluation', 'bootstrap')) if robot_log.exists() else 0
                if self.state['context'].get('record_control_steps'):
                    self.state['recorded_robot_samples'] = self.state['frames']
                    self.state['control_steps'] = terminal.get('control_steps', 0)
                    self.state['budget_steps'] = terminal.get('budget_steps', 0)
                self._save()
            except (OSError, ValueError) as caught:
                self.state['error'] = f'Could not finish episode files: {caught}'

    def label(self, label, label_source='human'):
        with self.lock:
            if label not in ('success', 'failure'):
                raise ValueError('Choose Success or Failure.')
            if self.state['status'] != 'awaiting_label' or not self.state.get('robot_finished'):
                raise ValueError('End the current episode before labeling it.')
            previous = dict(self.state)
            self.state.update(label=label, success=label == 'success', label_source=label_source,
                              status='saved', labeled_at=local_time(),
                              termination='technical_error' if self.state['error'] else self.state.get('stop_reason', 'user_ended'),
                              data_complete=not self.state['error'] and bool(self.state['frames']) and bool(self.state.get('control_steps')) and self.state.get('terminal_observation_valid', False))
            try:
                if self.state['context'].get('collection_mode') == 'dagger':
                    self.state['rl_replay'] = finalize_episode(self.folder, self.state)
                self._save()
            except (OSError, ValueError):
                self.state = previous
                raise
            self.owner = None
