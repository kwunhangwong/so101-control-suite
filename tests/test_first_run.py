"""First-time setup checks using temporary files and mocked hardware only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'calibration_check'), str(ROOT / 'camera_setup')]
import app_config
import controller
import native_camera
import camera_controls
from recalibration import matching_rest
from teleoperation import Bus, IDS, mapped_positions


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'calibration_check/config/teleoperation.json'
        # A controller constructor must not start its USB-owning worker in these tests.
        self.thread_patch = patch.object(controller.threading, 'Thread')
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.usb_patch = patch.object(Bus, 'open', side_effect=AssertionError('Real USB access is forbidden in tests'))
        self.usb_open = self.usb_patch.start()
        self.addCleanup(self.usb_patch.stop)

    def configure(self):
        config = {'cameras': {'front': '0x1111000012345678', 'wrist': '0x2222000012345678'},
                  'arms': {'leader': '/synthetic/leader', 'follower': '/synthetic/follower'}}
        app_config.write_json(self.root / 'config.local.json', config)
        app_config.initialize_local_files(self.root)
        return config

    def calibrated_config(self):
        self.configure()
        config = json.loads(self.config_path.read_text())
        config['calibrated'] = True
        for motor in config['leader']['motors'].values():
            motor.update(range_min=100, range_max=3100)
        for motor in config['follower']['motors'].values():
            motor.update(range_min=300, range_max=3900)
        app_config.write_json(self.config_path, config)
        return config

    def test_clean_install_has_unusable_placeholders_and_no_checkpoint(self):
        app_config.initialize_local_files(self.root)
        control = controller.CalibrationController(self.config_path)
        self.assertIs(control.config['calibrated'], False)
        self.assertNotEqual(control.config['leader']['port'], control.config['follower']['port'])
        self.assertIsNone(control.rest_pose)
        self.assertEqual(control.state['checkpoints'], [])
        self.assertFalse((self.config_path.parent / 'calibration_checkpoints').exists())
        for role in ('front', 'wrist'):
            roi = json.loads((self.root / f'camera_setup/config/{role}_roi.json').read_text())
            self.assertEqual(roi['bounds_xyxy'], [0, 0, 640, 480])
            self.assertEqual(roi['camera_unique_id'], '')
        self.usb_open.assert_not_called()

    def test_connect_refuses_uncalibrated_selected_arms_before_usb_open(self):
        self.configure()
        control = controller.CalibrationController(self.config_path)
        with self.assertRaisesRegex(ValueError, 'Motion is disabled until calibration is saved'):
            control._connect()
        self.usb_open.assert_not_called()
        self.assertIsNone(control.leader)
        self.assertIsNone(control.follower)

    def test_changing_either_arm_invalidates_calibration(self):
        for role in ('leader', 'follower'):
            with self.subTest(role=role):
                original = self.calibrated_config()
                devices = app_config.load_config(self.root)
                devices['arms'][role] += '-replacement'
                app_config.write_json(self.root / 'config.local.json', devices)
                app_config.initialize_local_files(self.root)
                updated = json.loads(self.config_path.read_text())
                self.assertIs(updated['calibrated'], False)
                self.assertEqual(updated[role]['port'], devices['arms'][role])
                for arm in ('leader', 'follower'):
                    self.assertEqual(updated[arm]['motors'], original[arm]['motors'])

    def test_unchanged_setup_preserves_calibration_rest_and_edited_roi(self):
        config = self.calibrated_config()
        rest_path = self.config_path.parent / 'rest_pose.json'
        rest = {'follower_position': {i: 2000 for i in IDS},
                'calibration': {role: config[role]['motors'] for role in ('leader', 'follower')}}
        app_config.write_json(rest_path, rest)
        roi_path = self.root / 'camera_setup/config/front_roi.json'
        roi = json.loads(roi_path.read_text())
        roi['bounds_xyxy'] = [10, 20, 600, 450]
        app_config.write_json(roi_path, roi)
        before = {path: path.read_bytes() for path in (self.config_path, rest_path, roi_path)}
        app_config.initialize_local_files(self.root)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(matching_rest(config, json.loads(rest_path.read_text())), rest)

    def test_teach_rest_maps_measured_leader_without_motor_writes(self):
        self.calibrated_config()
        control = controller.CalibrationController(self.config_path)
        positions = {i: 100 + int(i) * 400 for i in IDS}
        control.leader = Mock(spec=Bus)
        control.follower = Mock(spec=Bus)
        control.leader.positions.return_value = positions
        expected = {i: round(value) for i, value in mapped_positions(positions, control.config).items()}
        self.assertNotEqual(expected, positions)
        control._teach_rest()
        rest = json.loads((self.config_path.parent / 'rest_pose.json').read_text())
        self.assertEqual(rest['leader_position'], positions)
        self.assertEqual(rest['follower_position'], expected)
        self.assertEqual(matching_rest(control.config, rest), rest)
        self.assertEqual(control._reset_target(), expected)
        control.leader.positions.assert_called_once_with()
        self.assertEqual([call[0] for call in control.leader.method_calls], ['positions'])
        self.assertEqual(control.follower.method_calls, [])
        self.assertTrue(any(record['has_rest_pose'] for record in control.state['checkpoints']))
        self.usb_open.assert_not_called()


class CameraIdentityTests(unittest.TestCase):
    def test_capture_passes_exact_selected_id(self):
        selected = '0x2222000012345678'
        camera = native_camera.NativeCamera(ROOT / 'camera_setup', selected)
        with patch.object(camera, '_build', return_value=Path('/synthetic/capture')), \
                patch.object(native_camera.subprocess, 'Popen') as spawn, \
                patch.object(native_camera.threading, 'Thread'):
            self.assertEqual(camera.start()['state'], 'starting')
            self.assertEqual(spawn.call_args.args[0], ['/synthetic/capture', selected])

    def test_empty_capture_id_does_not_build_or_spawn(self):
        camera = native_camera.NativeCamera(ROOT / 'camera_setup', '')
        with patch.object(camera, '_build') as build, patch.object(native_camera.subprocess, 'Popen') as spawn:
            self.assertEqual(camera.start()['state'], 'error')
            build.assert_not_called()
            spawn.assert_not_called()

    def test_enumeration_only_dispatches_list(self):
        inventory = [{'id': '0x1111000012345678', 'name': 'Synthetic camera'}]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(inventory))
        with patch.object(native_camera.NativeCamera, '_build', return_value=Path('/synthetic/capture')), \
                patch.object(native_camera.subprocess, 'run', return_value=result) as run, \
                patch.object(native_camera.subprocess, 'Popen') as spawn:
            self.assertEqual(native_camera.enumerate_cameras(ROOT / 'camera_setup'), inventory)
            self.assertEqual(run.call_args.args[0], ['/synthetic/capture', '--list'])
            spawn.assert_not_called()

    def test_native_lookup_has_no_product_fallback(self):
        # Native capture is not executed: verify the identity/permission boundary in its source.
        source = (ROOT / 'camera_setup/native_camera.m').read_text()
        capture = source[source.index('pid_t parent'):]
        self.assertIn('[AVCaptureDevice deviceWithUniqueID:requestedID]', capture)
        self.assertNotIn('devicesWithMediaType:', capture)
        self.assertNotIn('hasSuffix:', capture)
        self.assertLess(source.index('isEqualToString:@"--list"'), source.index('requestAccessForMediaType:'))
        controls = (ROOT / 'camera_setup/camera_controls.m').read_text()
        for key in ('locationID', 'idVendor', 'idProduct'):
            self.assertIn(f'CFSTR("{key}")', controls)

    def test_controls_use_selected_identity_and_reject_empty_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'camera_setup'
            root.mkdir()
            (root / 'camera_controls.m').touch()
            executable = root.parent / '.camera/UVCControls'
            executable.parent.mkdir()
            executable.touch()
            controls = camera_controls.CameraControls(root)
            config = {'cameras': {'front': '0x1111000012345678', 'wrist': '0x2222000012345678'}}
            result = subprocess.CompletedProcess([], 0, stdout='{}')
            with patch.object(camera_controls, 'load_config', return_value=config), \
                    patch.object(camera_controls.subprocess, 'run', return_value=result) as run:
                controls.query('wrist')
                self.assertEqual(run.call_args.args[0], [str(executable), 'wrist', config['cameras']['wrist']])
                config['cameras']['wrist'] = ''
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, 'No camera is configured'):
                    controls.query('wrist')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
