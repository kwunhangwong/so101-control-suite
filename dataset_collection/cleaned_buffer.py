"""Review and permanently re-clean local cleaned episodes, preserving raw timing."""
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import threading

import cleaning
from storage import episode_manifests

LOCK = threading.RLock()
CORE = ('episode.json', 'samples.jsonl', 'robot.jsonl')


def _no_links(path, root):
    if path != root and root not in path.parents:
        raise ValueError('Choose a folder inside cleaned_episodes.')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symbolic links are not supported in cleaned buffers.')
        if part == root:
            break


def buffer_folder(base, path=''):
    base = Path(base).absolute()
    if not isinstance(path, (str, Path)):
        raise ValueError('Choose a folder inside cleaned_episodes.')
    value = Path(path) if path else base
    target = value if value.is_absolute() else base / value
    if '..' in target.parts:
        raise ValueError('Choose a folder inside cleaned_episodes.')
    _no_links(target, base)
    if not target.is_dir():
        raise ValueError('Cleaned buffer folder not found.')
    return target


def _folder(base, buffer, name):
    root = buffer_folder(base, buffer)
    folder = cleaning.episode_folder(root, name)
    _no_links(root / name, Path(base).absolute())
    for filename in (*CORE, 'robot_terminal.json'):
        _no_links(folder / filename, folder)
    return folder


def _revision(folder):
    digest = hashlib.sha256()
    for name in (*CORE, 'robot_terminal.json'):
        path = folder / name
        if path.exists():
            if path.is_symlink():
                raise ValueError('Symbolic links are not supported in cleaned episodes.')
            digest.update(name.encode()); digest.update(path.read_bytes())
    return digest.hexdigest()


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _load(folder):
    meta = json.loads((folder / 'episode.json').read_text())
    if meta.get('status') != 'cleaned' or not meta.get('robot_finished'):
        raise ValueError('Choose a completed cleaned episode.')
    samples, controls = _rows(folder / 'samples.jsonl'), _rows(folder / 'robot.jsonl')
    if not samples or [s['index'] for s in samples] != list(range(len(samples))):
        raise ValueError('Cleaned frame index is incomplete.')
    if not controls or any(r.get('phase') != 'follow' for r in controls):
        raise ValueError('Expected cleaned leader-follower control rows.')
    if meta.get('schema_version') == 2:
        # Legacy cleaned files retain source-relative sample times. Expose the
        # same edited timeline as schema 3, keeping all raw timestamps intact.
        ranges, segments, duration = [], [], 0.
        for span in meta['cleaning']['segments']:
            selected = [row for row in samples if row['segment_id'] == span['id']]
            if not selected:
                raise ValueError('A legacy cleaned segment has no frames.')
            ranges.append({**span, 'segment_id': span['id'], 'edited_start_seconds': duration})
            length = span['end_seconds'] - span['start_seconds']
            segments.append({'id': span['id'], 'first': selected[0]['index'], 'last': selected[-1]['index'],
                             'start_seconds': duration, 'end_seconds': duration + length})
            for row in selected:
                row['source_elapsed_seconds'] = row['elapsed_seconds']
                row['elapsed_seconds'] = duration + row['elapsed_seconds'] - span['start_seconds']
            for row in controls:
                if row['segment_id'] == span['id']:
                    row['edited_elapsed_seconds'] = duration + row['observation_received_at'] - meta['started_unix'] - span['start_seconds']
            duration += length
        meta.update(schema_version=3, duration_seconds=duration)
        meta['cleaning'].update(legacy_schema_version=2, edit_mode='join_frames_v1', source_ranges=ranges, segments=segments)
    if any('edited_elapsed_seconds' not in row for row in controls):
        raise ValueError('This cleaned episode has no edited robot timeline.')
    if len(samples) != meta['frames'] or len(controls) != meta['control_steps']:
        raise ValueError('Cleaned frame/control counts do not match metadata.')
    for values in ([s['elapsed_seconds'] for s in samples], [r['edited_elapsed_seconds'] for r in controls]):
        if any(not isinstance(t, (int, float)) or not math.isfinite(t) for t in values) or any(b <= a for a, b in zip(values, values[1:])):
            raise ValueError('Cleaned timestamps must be finite and ordered.')
    if not samples[-1]['elapsed_seconds'] < meta['duration_seconds']:
        raise ValueError('Cleaned duration must extend beyond its last frame.')
    return meta, samples, controls


def _images(folder, row):
    for name in ('front', 'wrist'):
        relative = row['cameras'][name]['file']
        if len(Path(relative).parts) < 2 or Path(relative).parts[0] != name:
            raise ValueError('Recorded camera images must stay in their front/wrist folder.')
        path = cleaning.image_path(folder, relative)
        _no_links(folder / relative, folder)
        if not path.is_file():
            raise ValueError('A retained camera image is missing.')
        yield relative


def list_episodes(base, buffer=''):
    with LOCK:
        root = buffer_folder(base, buffer)
        result = []
        for manifest in reversed(episode_manifests(root)):
            try:
                name = manifest.parent.relative_to(root).as_posix()
                folder = _folder(base, buffer, name)
                _revision(folder)
                meta, samples, _ = _load(folder)
                result.append({'id': name, 'task_name': meta.get('task_name', ''), 'started_at': meta['started_at'],
                               'label': meta.get('label'), 'frames': len(samples),
                               'instruction': meta.get('context', {}).get('instruction', '')})
            except (OSError, ValueError, KeyError):
                continue
        return result


def detail(base, buffer, name):
    with LOCK:
        folder = _folder(base, buffer, name)
        revision = _revision(folder)
        meta, samples, _ = _load(folder)
        return {'id': name, 'path': str(folder), 'revision': revision, 'metadata': meta,
                'frames': [{'index': s['index'], 'elapsed_seconds': s['elapsed_seconds'],
                            'has_robot': 'robot' in s, 'segment_id': s.get('segment_id', 0)} for s in samples]}


def frame(base, buffer, name, index, camera, revision=None):
    with LOCK:
        folder = _folder(base, buffer, name)
        if revision and revision != _revision(folder):
            raise ValueError('This episode changed. Reload it before editing.')
        _, samples, _ = _load(folder)
        if camera not in ('front', 'wrist') or type(index) is not int or not 0 <= index < len(samples):
            raise ValueError('Unknown camera or frame number.')
        list(_images(folder, samples[index]))
        return cleaning.image_path(folder, samples[index]['cameras'][camera]['file']).read_bytes()


def no_ops(base, buffer, name, tolerance_ticks=20, excluded=None):
    with LOCK:
        folder = _folder(base, buffer, name)
        _, samples, _ = _load(folder)
        result = cleaning.pose_duplicate_frames(samples, tolerance_ticks, excluded)
        result['revision'] = _revision(folder)
        return result


def _verify(folder):
    meta, samples, controls = _load(folder)
    for sample in samples:
        list(_images(folder, sample))
    if controls != cleaning.paired_control_rows(samples, controls):
        raise ValueError('Robot rows must match exactly the retained frame-attached samples.')
    if meta.get('terminal_observation_valid'):
        terminal = json.loads((folder / 'robot_terminal.json').read_text())
        list(_images(folder, terminal))


def save(base, body):
    if body.get('confirm') != 'REPLACE_CLEANED_EPISODE':
        raise ValueError('Confirm permanent replacement of this cleaned episode.')
    with LOCK:
        folder = _folder(base, body.get('buffer', ''), body.get('episode'))
        previous_revision = _revision(folder)
        if body.get('revision') != previous_revision:
            raise ValueError('This episode changed. Reload it before editing.')
        meta, samples, controls = _load(folder)
        excluded, split = body.get('excluded'), body.get('split_excluded', [])
        for values in (excluded, split):
            if not isinstance(values, list) or any(type(i) is not int or not 0 <= i < len(samples) for i in values):
                raise ValueError('Excluded frames must be valid frame numbers.')
        excluded, split = set(excluded), set(split)
        if not split.issubset(excluded):
            raise ValueError('A split section must also be excluded.')
        kept = [i for i in range(len(samples)) if i not in excluded]
        if not kept:
            raise ValueError('Keep at least one frame.')
        if not excluded:
            raise ValueError('Choose frames to remove before saving in place.')
        for path in folder.rglob('*'):
            _no_links(path, folder)
        spans, duration, segment = [], 0., -1
        previous = None
        for i in kept:
            boundary = previous is None or samples[i].get('segment_id', 0) != samples[previous].get('segment_id', 0) or any(j in split for j in range(previous + 1, i))
            if boundary:
                segment += 1
            if not spans or i != spans[-1]['last'] + 1 or boundary:
                spans.append({'first': i, 'last': i, 'segment_id': segment,
                              'start': samples[i]['elapsed_seconds'], 'edited_start': duration})
            else:
                spans[-1]['last'] = i
            end = samples[i+1]['elapsed_seconds'] if i+1 < len(samples) else meta['duration_seconds']
            duration += end - samples[i]['elapsed_seconds']
            previous = i
        stamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
        temporary = Path(tempfile.mkdtemp(prefix='.cleaned-edit-', dir=folder.parent))
        staging, backup = temporary / 'edited', temporary / 'original'
        committed = False
        try:
            shutil.copytree(folder, staging)
            new_samples, referenced = [], set()
            for span in spans:
                for i in range(span['first'], span['last']+1):
                    row = copy.deepcopy(samples[i]); referenced.update(_images(folder, row))
                    row.update(index=len(new_samples), segment_id=span['segment_id'],
                               elapsed_seconds=span['edited_start'] + row['elapsed_seconds'] - span['start'])
                    if 'robot' in row:
                        row['robot']['log_path'] = str(folder / 'robot.jsonl')
                    new_samples.append(row)
            new_controls = cleaning.paired_control_rows(new_samples, controls)
            terminal_valid = kept[-1] == len(samples)-1 and meta.get('terminal_observation_valid', False)
            if terminal_valid:
                terminal = json.loads((folder / 'robot_terminal.json').read_text())
                referenced.update(_images(folder, terminal)); terminal['segment_id'] = segment
                (staging / 'robot_terminal.json').write_text(json.dumps(terminal, indent=2)+'\n')
            else:
                (staging / 'robot_terminal.json').unlink(missing_ok=True)
            for camera in ('front', 'wrist'):
                for path in (staging / camera).rglob('*.jpg'):
                    if path.relative_to(staging).as_posix() not in referenced:
                        path.unlink()
            # Derived videos contain the old frames and are invalid after clipping.
            for path in staging.rglob('*.mp4'):
                path.unlink()
            for name, rows in (('samples.jsonl', new_samples), ('robot.jsonl', new_controls)):
                (staging / name).write_text(''.join(json.dumps(row)+'\n' for row in rows))
            cleaned = copy.deepcopy(meta)
            cleaned.update(path=str(folder), frames=len(new_samples), control_steps=len(new_controls), duration_seconds=duration,
                           format='ROI pairs and exactly frame-attached robot commands; original timestamps and edited playback timeline preserved',
                           frame_action_pairing='exact_retained_samples_robot_v1',
                           terminal_observation_valid=bool(terminal_valid), data_complete=False)
            provenance = cleaned['cleaning']
            source_indices = {row['source_index'] for row in new_samples}
            provenance['excluded_indices'] = [i for i in range(provenance['source_frames']) if i not in source_indices]
            provenance['split_excluded_indices'] = sorted(set(provenance.get('split_excluded_indices', [])) | {samples[i]['source_index'] for i in split})
            ranges, segments = [], []
            for row in new_samples:
                i = row['index']; old_i = kept[i]
                length = (samples[old_i+1]['elapsed_seconds'] if old_i+1 < len(samples) else meta['duration_seconds']) - samples[old_i]['elapsed_seconds']
                if not ranges or row['source_index'] != ranges[-1]['last']+1 or row['segment_id'] != ranges[-1]['segment_id']:
                    ranges.append({'first': row['source_index'], 'last': row['source_index'], 'segment_id': row['segment_id'],
                                   'start_seconds': row['source_elapsed_seconds'], 'end_seconds': row['source_elapsed_seconds']+length,
                                   'edited_start_seconds': row['elapsed_seconds']})
                else:
                    ranges[-1].update(last=row['source_index'], end_seconds=row['source_elapsed_seconds']+length)
                if not segments or segments[-1]['id'] != row['segment_id']:
                    segments.append({'id': row['segment_id'], 'first': i, 'last': i, 'start_seconds': row['elapsed_seconds'], 'end_seconds': row['elapsed_seconds']+length})
                else:
                    segments[-1].update(last=i, end_seconds=row['elapsed_seconds']+length)
            provenance.update(saved_at=stamp, source_ranges=ranges, segments=segments,
                              timing='elapsed_seconds and edited_elapsed_seconds follow retained frames. Original camera and robot timestamps remain unchanged; asynchronous frame-attached metadata is authoritative.',
                              transitions='Ordinary exclusions join retained frames. Existing and explicit split boundaries remain separate segment_id values.')
            provenance.setdefault('in_place_edits', []).append({'saved_at': stamp, 'previous_revision': previous_revision,
                'previous_frames': len(samples), 'excluded_indices': sorted(excluded), 'split_excluded_indices': sorted(split)})
            (staging / 'episode.json').write_text(json.dumps(cleaned, indent=2)+'\n')
            _verify(staging)
            if _revision(folder) != previous_revision:
                raise ValueError('This episode changed while saving. Reload it before editing.')
            folder.rename(backup)
            try:
                staging.rename(folder)
            except BaseException:
                backup.rename(folder)
                raise
            committed = True
        finally:
            # A failed rollback keeps its recovery copy rather than destroying data.
            if backup.exists() and not folder.exists():
                raise RuntimeError(f'Episode recovery required; original retained at {backup}')
            shutil.rmtree(temporary)
        return {'path': str(folder), 'paths': [str(folder)], 'frames': len(new_samples),
                'control_steps': len(new_controls), 'excluded': len(excluded), 'segments': len(segments),
                'frames_before': len(samples), 'frames_after': len(new_samples),
                'revision': _revision(folder), 'saved_in_place': committed}
