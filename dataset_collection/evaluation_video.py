"""Finalize one evaluation as a labeled side-by-side ROI MP4, with no episode sidecars."""
from fractions import Fraction
import json
from pathlib import Path
import shutil
import tempfile

import av
from PIL import Image

from recording import CAMERAS, local_time

FORMAT = 'so101_evaluation_video_v1'


def read_summary(path):
    with av.open(str(path)) as video:
        summary = json.loads(video.metadata['comment'])
    if summary.get('format') != FORMAT:
        raise ValueError('Not an SO-101 evaluation video.')
    return summary


def saved_videos(root):
    root = Path(root)
    paths = [*root.glob('episode_*/*.mp4'), *root.glob('*/episode_*/*.mp4')]
    for path in sorted(paths):
        # A retained manifest means finalization needs to be retried.
        if not (path.parent / 'episode.json').exists():
            yield path.parent, read_summary(path)


def encode(path, folder, samples, summary):
    frames = [([row['cameras'][name]['file'] for name in CAMERAS], row['elapsed_seconds']) for row in samples]
    terminals = [folder / name / 'terminal.jpg' for name in CAMERAS]
    if not frames:
        if not all(terminal.exists() for terminal in terminals):
            raise ValueError('Both front and wrist frames are required to save the MP4.')
        frames = [([str(terminal.relative_to(folder)) for terminal in terminals], 0)]
    origin = frames[0][1]
    frames = [(files, max(0, elapsed-origin)) for files, elapsed in frames]
    end = max(frames[-1][1], summary['duration_seconds']-origin)
    if end > frames[-1][1]+.001:
        files = [str(terminal.relative_to(folder)) if terminal.exists() else frames[-1][0][i]
                 for i, terminal in enumerate(terminals)]
        frames.append((files, end))
    sizes = []
    for file in frames[0][0]:
        with Image.open(folder / file) as first:
            sizes.append(first.size)
    # Equal tiles preserve both ROI aspect ratios. H.264 yuv420p needs even sizes.
    width, height = (max(size[axis] for size in sizes) for axis in (0, 1))
    width += width % 2
    height += height % 2
    size = (2*width, height)
    with av.open(str(path), 'w', options={'movflags': '+faststart'}) as video:
        video.metadata['comment'] = json.dumps(summary, ensure_ascii=False)
        stream = video.add_stream('libx264', rate=30)
        stream.width, stream.height = size
        stream.pix_fmt = 'yuv420p'
        stream.options = {'crf': '20', 'preset': 'veryfast'}
        stream.codec_context.time_base = Fraction(1, 1000)
        stream.time_base = Fraction(1, 1000)
        last_pts = -1
        for files, elapsed in frames:
            combined = Image.new('RGB', size)
            for i, file in enumerate(files):
                with Image.open(folder / file) as image:
                    if image.size != sizes[i]:
                        raise ValueError(f'{CAMERAS[i]} frame dimensions changed.')
                    combined.paste(image, (i*width+(width-image.width)//2, (height-image.height)//2))
            frame = av.VideoFrame.from_image(combined)
            frame.pts = max(last_pts+1, round(elapsed*1000))
            frame.time_base = Fraction(1, 1000)
            last_pts = frame.pts
            for packet in stream.encode(frame):
                video.mux(packet)
        for packet in stream.encode():
            video.mux(packet)
    verify(path, summary, len(frames), size)


def verify(path, summary, expected_count=None, expected_size=None):
    # Decode every frame before discarding any source data, one frame at a time.
    count = 0
    with av.open(str(path)) as video:
        if json.loads(video.metadata['comment']) != summary:
            raise ValueError('MP4 metadata verification failed.')
        for frame in video.decode(video=0):
            count += 1
            if expected_size and (frame.width, frame.height) != expected_size:
                raise ValueError('MP4 dimensions verification failed.')
    if not count or (expected_count is not None and count != expected_count):
        raise ValueError('MP4 frame verification failed.')


def finalize(recorder, label):
    if label not in ('success', 'failure'):
        raise ValueError('Choose Success or Failure.')
    state = recorder.snapshot()
    if state['status'] != 'awaiting_label' or not state.get('robot_finished'):
        raise ValueError('End the current episode before labeling it.')
    folder = recorder.folder
    if not folder.resolve().is_relative_to(recorder.root.resolve()) or not folder.name.startswith('episode_'):
        raise ValueError('Evaluation folder is outside the recording directory.')
    context = state['context']
    summary = {'format': FORMAT, 'status': 'saved', 'robot_finished': True, 'label': label,
               'success': label == 'success', 'labeled_at': local_time(), 'frames': state['frames'],
               'duration_seconds': state['duration_seconds'], 'error': state.get('error'),
               'source_sha256': state.get('source_sha256', {}),
               'context': {key: context.get(key) for key in ('checkpoint_id', 'checkpoint_sha256', 'instruction', 'success_condition', 'playback',
                                                          'sampler', 'eta', 'inference_steps', 'denoising_timesteps', 'seed', 'inference_source_sha256', 'starting_gripper',
                                                          'policy_family', 'policy_class', 'weights', 'action_representation', 'state_representation', 'normalization', 'stats_fit',
                                                          'image_processing', 'image_preprocessing', 'normalizer_source', 'adapter_sha256', 'camera_mapping', 'execution_controller', 'gripper_settings', 'gripper_opening_limit',
                                                          'n_obs_steps', 'observation_history', 'runtime_observation_history', 'normalization_channels', 'gripper_action_semantics',
                                                          'execution_gripper', 'model_source_root', 'model_source_sha256', 'base_sha256')}}
    task_name = state.get('task_name') or folder.parent.name
    if Path(task_name).name != task_name:
        raise ValueError('Invalid task name for the video filename.')
    name = f'{task_name}_{label}.mp4'
    output = folder / name
    if output.exists():
        summary = read_summary(output)
        if summary['label'] != label or summary['context']['checkpoint_id'] != context['checkpoint_id']:
            raise ValueError('Existing MP4 does not match the episode label/checkpoint.')
        verify(output, summary)
    else:
        samples = [json.loads(line) for line in (folder / 'samples.jsonl').read_text().splitlines() if line.strip()]
        with tempfile.TemporaryDirectory(prefix='.evaluation-mp4-', dir=folder.parent) as temporary:
            encode(Path(temporary)/name, folder, samples, summary)
            (Path(temporary)/name).replace(output)
    # Keep the manifest until last so interrupted cleanup remains retryable.
    for path in folder.iterdir():
        if path.name in (name, 'episode.json'):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    remote_log = Path(context.get('remote_log') or '')
    if (remote_log.parent == recorder.root and remote_log.name.startswith('connection_')
            and remote_log.suffix == '.log' and not state.get('error')):
        remote_log.unlink(missing_ok=True)
    (folder/'episode.json').unlink(missing_ok=True)
    with recorder.lock:
        recorder.state = {**summary, 'path': str(folder), 'videos': [str(output)]}
        recorder.owner = None
