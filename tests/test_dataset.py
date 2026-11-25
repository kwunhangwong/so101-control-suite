"""Synthetic local recording/editing checks; no USB devices or network calls."""
import copy
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import av
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dataset_collection')]
import cleaning
import cleaned_buffer
import cleaning_preview
from recording import EpisodeRecorder
from examples.read_dataset import load_episode, REPRESENTATION


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def robot_row(index, gripper=2000):
    measured = {str(i): 1000 + index * 100 + i for i in range(1, 7)}
    commanded = {str(i): measured[str(i)] + 7 for i in range(1, 7)}
    measured['6'] = commanded['6'] = gripper
    return {'phase': 'follow', 'step': index, 'follower': measured, 'command': commanded,
            'observation_received_at': 1000. + index * .2, 'command_sent_at': 1000.01 + index * .2}


class FakeCamera:
    def __init__(self, name):
        self.name, self.sequence = name, 0
        image = Image.new('RGB', (16, 12), 'green' if name == 'front' else 'blue')
        stream = BytesIO(); image.save(stream, 'JPEG'); self.jpeg = stream.getvalue()

    def sample(self):
        self.sequence += 1
        return self.jpeg, {'state': 'live', 'camera_unique_id': self.name, 'width': 16,
                           'height': 12, 'captured_at': time.time(), 'sequence': self.sequence}


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'episodes/synthetic-task/episode_2000-01-01_00-00-00'
        self.folder.mkdir(parents=True)
        self.samples = []
        for index in range(8):
            cameras = {}
            for camera in ('front', 'wrist'):
                relative = f'{camera}/{index:06d}.jpg'
                path = self.folder / relative; path.parent.mkdir(exist_ok=True)
                Image.new('RGB', (16, 12), (index * 25, 30 if camera == 'front' else 90, 50)).save(path)
                cameras[camera] = {'file': relative, 'captured_at': 1000. + index * .2}
            self.samples.append({'index': index, 'elapsed_seconds': index * .2,
                                 'cameras': cameras, 'robot': robot_row(index)})
        self.meta = {'schema_version': 1, 'status': 'saved', 'label': 'success', 'robot_finished': True,
                     'started_at': '2000-01-01T00:00:00+00:00', 'started_unix': 1000.,
                     'duration_seconds': 1.6, 'frames': 8, 'control_steps': 8,
                     'data_complete': True, 'terminal_observation_valid': False,
                     'action_representation': REPRESENTATION, 'context': {'instruction': 'Move the synthetic object.'}}
        self.save_fixture()

    def save_fixture(self):
        write_json(self.folder / 'episode.json', self.meta)
        write_rows(self.folder / 'samples.jsonl', self.samples)
        write_rows(self.folder / 'robot.jsonl', [row['robot'] for row in self.samples])

    def export(self, excluded, split=None):
        return cleaning.export_cleaned(self.root / 'episodes', 'synthetic-task/' + self.folder.name, excluded, split)

    def reclean(self, folder, excluded, split=None):
        return cleaned_buffer.save(self.root / 'cleaned_episodes', {
            'episode': folder.name, 'buffer': '', 'revision': cleaned_buffer._revision(folder),
            'confirm': 'REPLACE_CLEANED_EPISODE', 'excluded': excluded, 'split_excluded': split or []})

    def test_reader_uses_attached_targets_not_nearby_log_rows(self):
        write_rows(self.folder / 'robot.jsonl', [robot_row(99)])
        _, segments = load_episode(self.folder)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][3]['action_ticks'], [self.samples[3]['robot']['command'][str(i)] for i in range(1, 7)])
        self.assertNotEqual(segments[0][3]['state_ticks'], segments[0][3]['action_ticks'])

    def test_reader_rejects_invalid_ticks_and_missing_pair(self):
        for field in ('follower', 'command'):
            for value in (float('nan'), float('inf'), True):
                with self.subTest(field=field, value=value):
                    rows = copy.deepcopy(self.samples); rows[0]['robot'][field]['6'] = value
                    write_rows(self.folder / 'samples.jsonl', rows)
                    with self.assertRaises(ValueError): load_episode(self.folder)
            rows = copy.deepcopy(self.samples); del rows[0]['robot'][field]['6']
            write_rows(self.folder / 'samples.jsonl', rows)
            with self.assertRaises(ValueError): load_episode(self.folder)
        rows = copy.deepcopy(self.samples); del rows[0]['robot']
        write_rows(self.folder / 'samples.jsonl', rows)
        with self.assertRaises(ValueError): load_episode(self.folder)

    def test_reader_rejects_image_escape_and_missing_image(self):
        for relative in ('../outside.jpg', str(self.root / 'outside.jpg'), 'front/missing.jpg'):
            with self.subTest(path=relative):
                rows = copy.deepcopy(self.samples); rows[0]['cameras']['front']['file'] = relative
                write_rows(self.folder / 'samples.jsonl', rows)
                with self.assertRaises(ValueError): load_episode(self.folder)
        outside = self.root / 'outside.jpg'; outside.write_bytes(b'outside')
        link = self.folder / 'front/link.jpg'; link.symlink_to(outside)
        rows[0]['cameras']['front']['file'] = 'front/link.jpg'; write_rows(self.folder / 'samples.jsonl', rows)
        with self.assertRaises(ValueError): load_episode(self.folder)

    def test_ordinary_exclusions_join_and_preserve_exact_pair_data(self):
        original = (self.folder / 'samples.jsonl').read_bytes()
        result = self.export([0, 2, 3, 7]); output = Path(result['path'])
        meta, segments = load_episode(output)
        self.assertEqual(len(segments), 1)
        self.assertEqual([row['source_index'] for row in segments[0]], [1, 4, 5, 6])
        self.assertFalse(meta['data_complete'])
        for row in read_rows(output / 'samples.jsonl'):
            source = self.samples[row['source_index']]
            for key in ('follower', 'command', 'observation_received_at', 'command_sent_at'):
                self.assertEqual(row['robot'][key], source['robot'][key])
            for camera in ('front', 'wrist'):
                name = row['cameras'][camera]['file']
                self.assertEqual((output / name).read_bytes(), (self.folder / name).read_bytes())
        self.assertEqual((self.folder / 'samples.jsonl').read_bytes(), original)
        self.assertFalse((output / 'front/000002.jpg').exists())

    def test_explicit_cut_exports_separate_episodes(self):
        result = self.export([2, 3, 4], [2, 3, 4])
        self.assertEqual(result['segments'], 2)
        self.assertEqual([[r['source_index'] for r in load_episode(path)[1][0]] for path in result['paths']], [[0, 1], [5, 6, 7]])
        for path in result['paths']:
            metadata, _ = load_episode(path)
            self.assertEqual(metadata['label_scope'], 'original_episode')

    def test_reclean_preserves_exclusions_and_split_boundaries(self):
        output = Path(self.export([])['path'])
        self.reclean(output, [2, 3], [2, 3])
        _, segments = load_episode(output)
        self.assertEqual([[r['source_index'] for r in s] for s in segments], [[0, 1], [4, 5, 6, 7]])
        self.reclean(output, [0, 3])
        _, segments = load_episode(output)
        self.assertEqual([[r['source_index'] for r in s] for s in segments], [[1], [4, 6, 7]])
        self.assertFalse((output / 'front/000003.jpg').exists())
        self.assertFalse((output / 'front/000005.jpg').exists())

    def test_noops_keep_gripper_changes_and_later_return(self):
        poses = [robot_row(0, 2000), robot_row(0, 2000), robot_row(0, 1500), robot_row(0, 1500), robot_row(0, 2000)]
        rows = [dict(self.samples[i], robot=pose) for i, pose in enumerate(poses)]
        proposed = cleaning.pose_duplicate_frames(rows, 20)
        self.assertEqual(proposed['excluded'], [1, 3])
        # Missing feedback is never evidence that a frame is a duplicate.
        rows.append({'index': 5})
        self.assertEqual(cleaning.pose_duplicate_frames(rows, 20)['unscored_indices'], [5])

    def test_noops_preserve_segment_boundaries_and_missing_feedback(self):
        rows = [dict(self.samples[i], robot=robot_row(0)) for i in range(5)]
        rows[1]['segment_id'] = rows[2]['segment_id'] = 1
        rows[3] = {'index': 3, 'segment_id': 1}
        rows[4]['segment_id'] = 1
        result = cleaning.pose_duplicate_frames(rows, 20)
        self.assertEqual(result['excluded'], [2])
        self.assertEqual(result['unscored_indices'], [3])

    def test_noops_use_arm_threshold_but_preserve_any_gripper_change(self):
        rows = [dict(self.samples[i], robot=robot_row(0)) for i in range(4)]
        rows[1]['robot']['follower']['1'] += 20
        rows[1]['robot']['command']['1'] += 20
        rows[2]['robot']['follower']['6'] += 1
        rows[3]['robot']['follower']['6'] += 1
        rows[3]['robot']['command']['6'] += 1
        self.assertEqual(cleaning.pose_duplicate_frames(rows, 20)['excluded'], [1])

    def test_noop_exclusions_survive_export_and_reclean(self):
        self.samples[1]['robot']['follower'] = copy.deepcopy(self.samples[0]['robot']['follower'])
        self.samples[1]['robot']['command'] = copy.deepcopy(self.samples[0]['robot']['command'])
        self.save_fixture()
        excluded = cleaning.no_ops(self.root / 'episodes', 'synthetic-task/' + self.folder.name, 20)['excluded']
        self.assertEqual(excluded, [1])
        output = Path(self.export(excluded)['path']); self.reclean(output, [1])
        _, segments = load_episode(output)
        self.assertEqual([r['source_index'] for r in segments[0]], [0, 3, 4, 5, 6, 7])

    def test_seed_preview_uses_current_exclusions(self):
        body = {'episode': 'synthetic-task/' + self.folder.name, 'excluded': [0, 1, 6, 7], 'split_excluded': []}
        result = cleaning_preview.prepare(self.root, 'seed', body)
        self.assertEqual(result['frames'], 4)
        with av.open(str(cleaning_preview.cache_dir(self.root) / 'preview.mp4')) as movie:
            summary = json.loads(movie.metadata['comment'])
            self.assertEqual(summary['excluded'], body['excluded'])
            self.assertEqual(sum(1 for _ in movie.decode(video=0)), 5)
        next_result = cleaning_preview.prepare(self.root, 'seed', dict(body, excluded=[0, 1, 2, 3, 4, 7]))
        self.assertEqual(next_result['frames'], 2)
        with av.open(str(cleaning_preview.cache_dir(self.root) / 'preview.mp4')) as movie:
            summary = json.loads(movie.metadata['comment'])
            self.assertEqual(summary['excluded'], [0, 1, 2, 3, 4, 7])
            self.assertEqual(sum(1 for _ in movie.decode(video=0)), 3)

    def test_cleaned_preview_honors_existing_segments(self):
        output = Path(self.export([])['path']); self.reclean(output, [2, 3], [2, 3])
        body = {'episode': output.name, 'buffer': '', 'revision': cleaned_buffer._revision(output), 'excluded': [1], 'split_excluded': []}
        result = cleaning_preview.prepare(self.root, 'cleaned', body)
        self.assertEqual((result['frames'], result['segments']), (5, 2))
        with av.open(str(cleaning_preview.cache_dir(self.root) / 'preview.mp4')) as movie:
            self.assertEqual(sum(1 for _ in movie.decode(video=0)), 6)

    def test_recording_waits_for_robot_metadata_and_requires_label(self):
        roi = self.root / 'config'; roi.mkdir()
        for camera in ('front', 'wrist'):
            write_json(roi / f'{camera}_roi.json', {'source_size_wh': [16, 12], 'bounds_xyxy': [2, 2, 14, 10]})
        recorder = EpisodeRecorder(self.root / 'captured', roi, {name: FakeCamera(name) for name in ('front', 'wrist')})
        recorder.start('fixture', {'instruction': 'Synthetic recording.', 'require_robot_metadata': True})
        self.addCleanup(lambda: recorder.end() if recorder.thread and recorder.thread.is_alive() else None)
        time.sleep(.05)
        self.assertEqual(recorder.snapshot()['frames'], 0)
        row = robot_row(0); recorder.observe_robot(row)
        deadline = time.monotonic() + 2
        while recorder.snapshot()['frames'] < 2 and time.monotonic() < deadline: time.sleep(.01)
        self.assertGreaterEqual(recorder.snapshot()['frames'], 2)
        recorder.mark_stop('user_ended'); recorder.end()
        with self.assertRaises(ValueError): recorder.label('success')
        write_rows(recorder.folder / 'robot.jsonl', [row])
        recorder.finish_robot({'follower': row['follower'], 'observed_at': time.time()})
        self.assertEqual(recorder.snapshot()['status'], 'awaiting_label')
        with self.assertRaises(ValueError): recorder.start('fixture', {})
        recorder.label('success')
        metadata, segments = load_episode(recorder.folder)
        self.assertTrue(metadata['data_complete'])
        self.assertEqual(sum(map(len, segments)), metadata['frames'])
        for pair in segments[0]:
            self.assertEqual(pair['action_ticks'], [row['command'][str(i)] for i in range(1, 7)])
            with Image.open(pair['front']) as image: self.assertEqual(image.size, (12, 8))


if __name__ == '__main__':
    unittest.main()
