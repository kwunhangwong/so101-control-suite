"""Slow, interruptible resets to measured joint poses."""
import json
from pathlib import Path
import time

from teleoperation import FPS, IDS, TICKS_PER_DEGREE, StopRequested, range_warnings, tracking_warnings

RESET_DEGREES_PER_SECOND = 10


def validate_pose(pose, motors):
    if not isinstance(pose, dict) or set(pose) != set(IDS):
        raise ValueError('A pose must contain all six joints.')
    if any(type(pose[i]) is not int or not motors[i]['range_min'] <= pose[i] <= motors[i]['range_max'] for i in IDS):
        raise ValueError('Pose is outside the follower calibration.')
    return dict(pose)


def move_to(follower, target, stop_event, on_frame, folder, purpose):
    motors = follower.config['motors']
    target = validate_pose(target, motors)
    follower.verify()
    initial = validate_pose(follower.positions(), motors)
    if stop_event.is_set():
        raise StopRequested()
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    path = folder / f'move_{time.time_ns()}.jsonl'
    # Cubic smoothstep has peak slope 1.5 and zero velocity at both ends.
    duration = max(0.5, 1.5 * max(abs(target[i]-initial[i]) for i in IDS)
                   / (RESET_DEGREES_PER_SECOND*TICKS_PER_DEGREE))
    with path.open('x') as log:
        log.write(json.dumps({'event': 'start', 'purpose': purpose, 'initial': initial, 'target': target,
                              'config': follower.config, 'units': 'encoder ticks; 4096 ticks per revolution',
                              'motion_checks': 'warnings_only', 'max_target_degrees_per_second': RESET_DEGREES_PER_SECOND,
                              'trajectory': 'smoothstep', 'planned_seconds': duration}) + '\n')
        previous = initial
        started = time.monotonic()
        progress = 0.0
        target_since = None
        try:
            follower.arm(initial)
            last_tick = time.monotonic()
            while True:
                if stop_event.is_set():
                    raise StopRequested()
                tick = time.monotonic()
                observed = follower.positions()
                if all(abs(observed[i] - target[i]) <= TICKS_PER_DEGREE for i in IDS):
                    # Finish at the requested target, rather than leaving a one-degree offset.
                    if previous == target and progress >= duration:
                        log.write(json.dumps({'event': 'complete', 'elapsed': tick - started, 'observed': observed}) + '\n')
                        return
                # Delayed USB/UI updates lengthen the reset instead of skipping ahead.
                progress = min(duration, progress + min(max(0, tick-last_tick), 1/FPS))
                last_tick = tick
                u = progress / duration
                blend = u*u*(3-2*u)
                goals = {i: round(initial[i] + (target[i]-initial[i])*blend) for i in IDS}
                warnings = {**range_warnings('follower', observed, motors), **tracking_warnings(observed, goals)}
                if progress >= duration and target_since is None:
                    target_since = tick
                if target_since is not None and tick-target_since > 8:
                    warnings['reset-duration'] = 'Reset has not settled 8 seconds after the slow ramp. Motion continues; check the arm or use Stop & hold.'
                if stop_event.is_set():
                    raise StopRequested()
                follower.send(goals)
                frame = {'t': tick - started, 'follower': observed, 'command': goals, 'log_path': str(path), 'warnings': warnings}
                log.write(json.dumps(frame) + '\n')
                on_frame(frame)
                previous = goals
                time.sleep(max(0, 1 / FPS - (time.monotonic() - tick)))
        except Exception as error:
            log.write(json.dumps({'event': 'stopped' if isinstance(error, StopRequested) else 'error',
                                  'error': str(error), 'elapsed': time.monotonic() - started}) + '\n')
            raise
