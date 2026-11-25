"""Review recordings and export retained segments without modifying source episodes."""
import copy
import math
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from storage import episode_manifests


def episode_folder(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or not re.fullmatch(r'(?:(?:[\w-]+/)?|[\w-]+/stage_(?:1_bc|2_offline_rl|3_round_(?:[1-9]|10))/)episode_[\w-]+', name):
        raise ValueError('Select a recorded episode.')
    folder = (root / name).resolve()
    if not folder.is_relative_to(root) or not folder.is_dir():
        raise ValueError('Episode not found.')
    return folder


def read_episode(root, name):
    folder = episode_folder(root, name)
    meta = json.loads((folder / 'episode.json').read_text())
    completed = (meta.get('status') == 'saved' or (meta.get('status') == 'cleaned'
                 and meta.get('storage_import', {}).get('schema') == 'so101_stage_buffer_import_v1'))
    if not completed or not meta.get('robot_finished'):
        raise ValueError('End and label the episode before cleaning it.')
    samples = [json.loads(line) for line in (folder / 'samples.jsonl').read_text().splitlines() if line.strip()]
    if not samples or any(row['index'] != i for i, row in enumerate(samples)):
        raise ValueError('Episode has no frames or its frame index is incomplete.')
    return folder, meta, samples


def list_episodes(root):
    episodes = []
    for manifest in reversed(episode_manifests(root)):
        try:
            episode_id = manifest.parent.relative_to(root).as_posix()
            _, meta, samples = read_episode(root, episode_id)
            episodes.append({'id': episode_id, 'task_name': meta.get('task_name', ''), 'started_at': meta['started_at'],
                             'label': meta.get('label'), 'frames': len(samples),
                             'instruction': meta.get('context', {}).get('instruction', '')})
        except (OSError, ValueError, KeyError):
            continue
    return episodes


def delete_episode(root, name):
    folder = episode_folder(root, name)
    if folder != Path(root).resolve() / name or (folder / 'episode.json').is_symlink():
        raise ValueError('Cannot delete an episode through a symbolic link.')
    meta = json.loads((folder / 'episode.json').read_text())
    if meta.get('status') != 'saved' or not meta.get('robot_finished'):
        raise ValueError('End and label the episode before deleting it.')
    shutil.rmtree(folder)
    return {'deleted': name, 'path': str(folder)}


def detail(root, name):
    folder, meta, samples = read_episode(root, name)
    return {'id': name, 'path': str(folder), 'metadata': meta,
            'frames': [{'index': s['index'], 'elapsed_seconds': s['elapsed_seconds'],
                        'has_robot': 'robot' in s} for s in samples]}


def image_path(folder, relative):
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Invalid recorded image path.')
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder) or path.suffix != '.jpg':
        raise ValueError('Invalid recorded image path.')
    return path


def frame(root, name, index, camera):
    if camera not in ('front', 'wrist'):
        raise ValueError('Unknown camera.')
    folder, _, samples = read_episode(root, name)
    if not 0 <= index < len(samples):
        raise ValueError('Frame is out of range.')
    return image_path(folder, samples[index]['cameras'][camera]['file']).read_bytes()


def no_ops(root, name, tolerance_ticks=20, excluded=None):
    """Propose consecutive stationary frames; never modify the episode."""
    _, _, samples = read_episode(root, name)
    return pose_duplicate_frames(samples, tolerance_ticks, excluded)


def pose_duplicate_frames(samples, tolerance_ticks=20, excluded=None):
    """Keep motion, gripper changes, and segment boundaries; compare the last retained pose."""
    if (type(tolerance_ticks) not in (int, float) or not math.isfinite(tolerance_ticks)
            or not 0 <= tolerance_ticks <= 4095 or tolerance_ticks != int(tolerance_ticks)):
        raise ValueError('Pose threshold must be a whole number from 0 to 4095 encoder ticks.')
    excluded = [] if excluded is None else excluded
    if not isinstance(excluded, list) or any(type(i) is not int or not 0 <= i < len(samples) for i in excluded):
        raise ValueError('Excluded frames must be valid frame numbers.')
    skipped = set(excluded)
    anchor, segment, duplicates, unscored = None, None, [], []
    for i, sample in enumerate(samples):
        if i in skipped:
            continue
        robot = sample.get('robot') or {}
        values = [(robot.get(kind) or {}).get(str(j)) for kind in ('follower', 'command') for j in range(1, 7)]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            # Missing feedback cannot establish equality; leave it for manual review.
            unscored.append(i)
            anchor = None
        elif (anchor is not None and segment == sample.get('segment_id', 0)
              and all(abs(values[j]-anchor[j]) <= tolerance_ticks for j in (0,1,2,3,4,6,7,8,9,10))
              and values[5] == anchor[5] and values[11] == anchor[11]):
            duplicates.append(i)
        else:
            anchor = values
        segment = sample.get('segment_id', 0)
    return {'excluded': duplicates, 'tolerance_ticks': tolerance_ticks, 'frames': len(samples),
            'unscored_indices': unscored, 'rule': 'consecutive_stationary_pose_v1'}


def paired_control_rows(samples, controls):
    """One original robot row per retained frame pair; no interval expansion."""
    def key(row):
        return row.get('control_step', row.get('step')), row.get('observation_received_at')
    recorded = {}
    for row in controls:
        if row.get('follower') and row.get('command'):
            recorded.setdefault(key(row), []).append(row)
    result, seen = [], {}
    for sample in samples:
        robot = sample.get('robot') or {}
        values = [(robot.get(kind) or {}).get(str(j)) for kind in ('follower', 'command') for j in range(1, 7)]
        values.append(robot.get('observation_received_at'))
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError(f"Frame {sample['index']+1} has no complete attached robot sample. Exclude it before saving.")
        candidates = recorded.get(key(robot), [])
        for original in candidates:
            if (any(original[name] != robot[name] for name in ('follower', 'command'))
                    or any(name in original and name in robot and original[name] != robot[name]
                           for name in ('observation_started_at', 'observation_monotonic_ns', 'command_sent_at', 'command_monotonic_ns'))):
                raise ValueError('Recorded robot log differs from the image-paired sample.')
        identity = key(robot), sample.get('segment_id', 0)
        if identity in seen:
            if any(seen[identity][name] != robot[name] for name in ('follower', 'command')):
                raise ValueError('Conflicting robot samples share an observation timestamp.')
            continue
        seen[identity] = robot
        row = copy.deepcopy(candidates[0] if candidates else robot)
        if not candidates:
            # Older seed exports trimmed some log rows while retaining their
            # complete frame-attached snapshot. Preserve that exact evidence.
            row.update(paired_from_frame_snapshot=True,
                       source_frame_index=sample.get('source_index', sample['index']))
        if 'log_path' in robot:
            row['log_path'] = robot['log_path']
        row.update(segment_id=sample.get('segment_id', 0), edited_elapsed_seconds=sample['elapsed_seconds'])
        result.append(row)
    return result


def export_cleaned(root, name, excluded, split_excluded=None):
    folder, meta, samples = read_episode(root, name)
    split_excluded = [] if split_excluded is None else split_excluded
    for values in (excluded, split_excluded):
        if not isinstance(values, list) or any(type(i) is not int or not 0 <= i < len(samples) for i in values):
            raise ValueError('Excluded frames must be valid frame numbers.')
    excluded, split_excluded = set(excluded), set(split_excluded)
    if not split_excluded.issubset(excluded):
        raise ValueError('A split section must also be excluded.')
    groups, current = [], []
    for i in range(len(samples)):
        if i in split_excluded:
            if current:
                groups.append(current); current = []
        elif i not in excluded:
            current.append(i)
    if current:
        groups.append(current)
    if not groups:
        raise ValueError('Keep at least one frame.')
    controls = [dict(json.loads(line), source_line=n)
                for n, line in enumerate((folder / 'robot.jsonl').read_text().splitlines()) if line.strip()]
    output_root = Path(root).parent / 'cleaned_episodes'
    output_root.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone()
    basename = f'{folder.name}_cleaned_{stamp.strftime("%Y-%m-%d_%H-%M-%S_%f")}'
    temporary = Path(tempfile.mkdtemp(prefix='.saving-', dir=output_root))
    destinations = []
    source_hashes = {p: hashlib.sha256((folder / p).read_bytes()).hexdigest() for p in ('episode.json', 'samples.jsonl', 'robot.jsonl')}
    try:
        for part, kept in enumerate(groups, 1):
            filename = basename + (f'_part_{part:02d}' if len(groups) > 1 else '')
            destination, staging = output_root / filename, temporary / filename
            staging.mkdir()
            ranges = []
            for i in kept:
                if not ranges or i != ranges[-1]['last'] + 1:
                    ranges.append({'first': i, 'last': i})
                else:
                    ranges[-1]['last'] = i
            duration, by_frame = 0., {}
            for span in ranges:
                first, last = span['first'], span['last']
                span['start_seconds'] = samples[first]['elapsed_seconds']
                span['end_seconds'] = samples[last+1]['elapsed_seconds'] if last+1 < len(samples) else meta['duration_seconds']
                span['edited_start_seconds'] = duration
                duration += span['end_seconds'] - span['start_seconds']
                by_frame.update({i: span for i in range(first, last+1)})
            def copy_images(row):
                for camera in row['cameras'].values():
                    camera.pop('raw_file', None)
                    relative = camera['file']
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(image_path(folder, relative), target)
            retained = []
            with (staging / 'samples.jsonl').open('w') as output:
                for index, i in enumerate(kept):
                    row, span = copy.deepcopy(samples[i]), by_frame[i]
                    copy_images(row)
                    row.update(index=index, source_index=i, segment_id=0,
                               source_elapsed_seconds=row['elapsed_seconds'],
                               elapsed_seconds=span['edited_start_seconds'] + row['elapsed_seconds'] - span['start_seconds'])
                    if 'robot' in row:
                        row['robot']['log_path'] = str(destination / 'robot.jsonl')
                    retained.append(row)
                    output.write(json.dumps(row) + '\n')
            commands = paired_control_rows(retained, controls)
            control_steps = len(commands)
            with (staging / 'robot.jsonl').open('w') as output:
                for row in commands:
                    output.write(json.dumps(row) + '\n')
            if not control_steps:
                raise ValueError(f'Part {part} has no aligned robot commands. Keep a longer section.')
            terminal_valid = kept[-1] == len(samples)-1 and meta.get('terminal_observation_valid', False)
            if terminal_valid:
                terminal = json.loads((folder / 'robot_terminal.json').read_text())
                copy_images(terminal); terminal['segment_id'] = 0
                (staging / 'robot_terminal.json').write_text(json.dumps(terminal, indent=2) + '\n')
            cleaned = copy.deepcopy(meta)
            cleaned.update(schema_version=3, status='cleaned', path=str(destination), frames=len(kept),
                           format='Retained ROI pairs and their exact attached robot samples; edited playback timeline.',
                           frame_action_pairing='exact_retained_samples_robot_v1',
                           control_steps=control_steps, terminal_observation_valid=terminal_valid,
                           data_complete=bool(meta.get('data_complete') and not excluded),
                           label_scope='original_episode', duration_seconds=duration)
            cleaned['cleaning'] = {'saved_at': stamp.isoformat(timespec='milliseconds'), 'source_episode': str(folder),
                'source_sha256': source_hashes, 'edit_mode': 'join_frames_v1', 'part': part, 'parts': len(groups),
                'excluded_indices': [i for i in range(len(samples)) if i not in by_frame],
                'split_excluded_indices': sorted(split_excluded), 'source_frames': len(samples), 'source_ranges': ranges,
                'segments': [{'id': 0, 'first': 0, 'last': len(kept)-1, 'start_seconds': 0., 'end_seconds': duration}],
                'unpaired_control_source_lines': [],
                'timing': 'elapsed_seconds is edited playback time. Original source_elapsed_seconds and all camera/robot timestamps are preserved; exact frame-attached commands are not interpolated or resampled.',
                'transitions': 'Retained frames and commands form one sequence. Explicit section cuts are separate episode folders, never joined for training.',
                'outcome': 'The label describes the original episode; a split part is not independently labeled successful.'}
            (staging / 'episode.json').write_text(json.dumps(cleaned, indent=2) + '\n')
            destinations.append({'path': str(destination), 'frames': len(kept), 'control_steps': control_steps})
        for item in destinations:
            (temporary / Path(item['path']).name).rename(item['path'])
    except Exception:
        for item in destinations:
            if Path(item['path']).exists():
                shutil.rmtree(item['path'])
        raise
    finally:
        shutil.rmtree(temporary)
    return {'path': destinations[0]['path'], 'paths': [item['path'] for item in destinations],
            'outputs': destinations, 'frames': sum(len(g) for g in groups), 'excluded': len(excluded), 'segments': len(groups)}
