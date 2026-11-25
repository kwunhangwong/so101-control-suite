"""One replaceable MP4 preview of the cleaner's current frame selection."""
import math
from pathlib import Path
import threading

import cleaning
import cleaned_buffer
from evaluation_video import encode

LOCK = threading.RLock()


def cache_dir(root):
    cache = Path(root) / '.cleaning_preview'
    if cache.is_symlink():
        raise ValueError('The cleaning preview directory must not be a symbolic link.')
    return cache


def timeline(meta, samples, excluded, split, playback_hz=None):
    if playback_hz is not None and (type(playback_hz) not in (int, float)
            or not math.isfinite(playback_hz) or playback_hz <= 0):
        raise ValueError('Invalid recorded control rate for the preview.')
    for values in (excluded, split):
        if not isinstance(values, list) or any(type(i) is not int or not 0 <= i < len(samples) for i in values):
            raise ValueError('Excluded frames must be valid frame numbers.')
    excluded, split = set(excluded), set(split)
    if not split.issubset(excluded):
        raise ValueError('A split section must also be excluded.')
    kept = [i for i in range(len(samples)) if i not in excluded]
    if not kept:
        raise ValueError('Keep at least one frame to preview.')
    frames, duration, segments, previous = [], 0., 0, None
    prior_segments = {s['id']: s for s in meta.get('cleaning', {}).get('segments', [])}
    for i in kept:
        start = samples[i]['elapsed_seconds']
        old = prior_segments.get(samples[i].get('segment_id', 0))
        end = (samples[i+1]['elapsed_seconds'] if i+1 < len(samples)
               and samples[i+1].get('segment_id', 0) == samples[i].get('segment_id', 0)
               else old['end_seconds'] if old else meta['duration_seconds'])
        interval = (1 / playback_hz if playback_hz is not None
                    else samples[i].get('retained_interval_seconds', end-start))
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise ValueError('Invalid frame timing for the preview.')
        if (previous is None or samples[i].get('segment_id', 0) != samples[previous].get('segment_id', 0)
                or any(j in split for j in range(previous+1, i))):
            segments += 1
        frames.append({'elapsed_seconds':duration, 'cameras':samples[i]['cameras']})
        duration += interval
        previous = i
    # Hold the retained last pair to the end; never let encode insert terminal.jpg.
    frames.append(dict(frames[-1], elapsed_seconds=duration))
    return frames, duration, segments


def prepare(root, mode, body):
    root = Path(root)
    with LOCK:
        source_revision = cleaned_buffer._revision
        if mode == 'cleaned':
            folder = cleaned_buffer._folder(root/'cleaned_episodes', body.get('buffer', ''), body.get('episode'))
            revision = cleaned_buffer._revision(folder)
            if body.get('revision') != revision:
                raise ValueError('This episode changed. Reload it before previewing.')
            meta, samples, _ = cleaned_buffer._load(folder)
        elif mode == 'seed':
            folder, meta, samples = cleaning.read_episode(root/'episodes', body.get('episode'))
            revision = cleaned_buffer._revision(folder)
        else:
            raise ValueError('Choose a seed or cleaned episode.')
        excluded, split = body.get('excluded'), body.get('split_excluded', [])
        playback_hz = None
        frames, duration, segments = timeline(meta, samples, excluded, split, playback_hz)
        for row in frames[:-1]:
            for camera in ('front', 'wrist'):
                if not cleaning.image_path(folder, row['cameras'][camera]['file']).is_file():
                    raise ValueError('A retained camera image is missing.')
        summary = {'format':'so101_cleaning_preview_v1', 'source':str(folder), 'revision':revision,
                   'excluded':sorted(set(excluded)), 'split_excluded':sorted(set(split)),
                   'frames':len(frames)-1, 'duration_seconds':duration, 'segments':segments,
                   'playback_hz':playback_hz}
        cache = cache_dir(root)
        cache.mkdir(exist_ok=True)
        output, partial = cache/'preview.mp4', cache/'preview.partial.mp4'
        try:
            encode(partial, folder, frames, summary)
            if source_revision(folder) != revision:
                raise ValueError('This episode changed while generating the preview. Reload it.')
            partial.replace(output)
            # Remove previews from the previous hashed-filename implementation.
            for path in cache.glob('*.mp4'):
                if path != output:
                    path.unlink(missing_ok=True)
        finally:
            partial.unlink(missing_ok=True)
        return {'url':'/api/cleaning-preview/video/preview.mp4', 'frames':len(frames)-1,
                'duration_seconds':duration, 'segments':segments, 'playback_hz':playback_hz}
