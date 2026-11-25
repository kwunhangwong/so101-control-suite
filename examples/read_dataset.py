#!/usr/bin/env python3
"""Read frame-aligned SO-101 training pairs without changing their labels."""
import argparse
import json
import math
from pathlib import Path

MOTOR_IDS = tuple(str(i) for i in range(1, 7))
REPRESENTATION = 'so101_absolute_joint_encoder_ticks_v1'


def _finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number.')
    return value


def _ticks(robot, key):
    values = robot.get(key)
    if not isinstance(values, dict) or set(values) != set(MOTOR_IDS):
        raise ValueError(f'Each frame needs all six robot.{key} motor values.')
    return [_finite(values[motor], f'{key}[{motor}]') for motor in MOTOR_IDS]


def _image(folder, relative):
    if not isinstance(relative, str):
        raise ValueError('Each camera needs a relative image path.')
    name = Path(relative)
    path = (folder / name).resolve()
    if name.is_absolute() or '..' in name.parts or not path.is_relative_to(folder):
        raise ValueError('Camera image path escapes the episode folder.')
    if not path.is_file():
        raise ValueError(f'Recorded image is missing: {relative}')
    return path


def load_episode(folder):
    """Return (metadata, segments); every segment is an ordered list of BC pairs.

    Build action chunks separately inside each returned segment. State and action
    are raw calibrated ticks, including the continuous gripper on channel six.
    """
    folder = Path(folder).resolve()
    metadata = json.loads((folder / 'episode.json').read_text())
    if (metadata.get('status') not in ('saved', 'cleaned')
            or not metadata.get('robot_finished')
            or metadata.get('label') not in ('success', 'failure')):
        raise ValueError('Choose an ended and labeled episode or cleaned copy.')
    if metadata.get('action_representation') != REPRESENTATION:
        raise ValueError('This reader expects absolute six-motor encoder-tick targets.')
    samples = [json.loads(line) for line in (folder / 'samples.jsonl').read_text().splitlines() if line.strip()]
    if not samples or metadata.get('frames') != len(samples):
        raise ValueError('Recorded frame count does not match samples.jsonl.')
    segments, last_segment, previous_time = [], None, -1.
    for index, sample in enumerate(samples):
        if type(sample.get('index')) is not int or sample['index'] != index:
            raise ValueError('Frame indices must be contiguous and ordered.')
        elapsed = _finite(sample.get('elapsed_seconds'), 'elapsed_seconds')
        if elapsed < 0 or elapsed < previous_time:
            raise ValueError('Frame times must be nonnegative and ordered.')
        segment = sample.get('segment_id', 0)
        if type(segment) is not int or segment < 0:
            raise ValueError('segment_id must be a nonnegative integer.')
        robot = sample.get('robot') or {}
        cameras = sample.get('cameras') or {}
        pair = {
            'index': index, 'source_index': sample.get('source_index', index),
            'segment_id': segment, 'elapsed_seconds': elapsed,
            'source_elapsed_seconds': sample.get('source_elapsed_seconds', elapsed),
            'front': _image(folder, cameras.get('front', {}).get('file')),
            'wrist': _image(folder, cameras.get('wrist', {}).get('file')),
            'state_ticks': _ticks(robot, 'follower'),
            'action_ticks': _ticks(robot, 'command'),
            'robot_observed_at': _finite(robot.get('observation_received_at'), 'observation_received_at'),
            'command_sent_at': robot.get('command_sent_at'),
            'camera_timestamps': {name: cameras[name].get('captured_at') for name in ('front', 'wrist')},
        }
        if not segments or segment != last_segment:
            segments.append([])
        segments[-1].append(pair)
        last_segment, previous_time = segment, elapsed
    return metadata, segments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('episode', type=Path, help='One saved or cleaned episode folder')
    args = parser.parse_args()
    metadata, segments = load_episode(args.episode)
    print(json.dumps({'frames': sum(map(len, segments)), 'segment_lengths': list(map(len, segments)),
                      'label': metadata['label'], 'label_scope': metadata.get('label_scope', 'episode'),
                      'state_channels': 6, 'action_channels': 6, 'units': 'calibrated encoder ticks'}, indent=2))


if __name__ == '__main__':
    main()
