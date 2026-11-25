"""User-owned device configuration. No default hardware identities or remote services."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_config(root=ROOT):
    path = Path(root) / 'config.local.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    result = {section: {role: config.get(section, {}).get(role, '') for role in roles}
              for section, roles in [('cameras', ('front', 'wrist')), ('arms', ('leader', 'follower'))]}
    for section, devices in result.items():
        if any(not isinstance(value, str) for value in devices.values()):
            raise ValueError(f'{section}: device identifiers must be strings.')
        assigned = [value for value in devices.values() if value]
        if len(set(assigned)) != len(assigned):
            raise ValueError(f'{section}: select a different device for each role.')
    return result


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def initialize_local_files(root=ROOT):
    """Create first-run placeholders; they are never valid motion calibration."""
    root = Path(root)
    config = load_config(root)
    path = root / 'calibration_check/config/teleoperation.json'
    ports = {role: port or f'UNCONFIGURED_{role.upper()}' for role, port in config['arms'].items()}
    if path.exists():
        current = json.loads(path.read_text())
        if any(current[role]['port'] != ports[role] for role in ports):
            # Preserve calibration values/history, but require calibration of newly selected arms.
            current['calibrated'] = False
            for role, port in ports.items():
                current[role]['port'] = port
            write_json(path, current)
    else:
        current = {'calibrated': False, **{role: {'port': port, 'motors': {
            str(i): {'model': 777, 'range_min': 0, 'range_max': 4095, 'homing_offset': 0}
            for i in range(1, 7)}} for role, port in ports.items()}}
        write_json(path, current)
    for role, camera_id in config['cameras'].items():
        roi_path = root / f'camera_setup/config/{role}_roi.json'
        previous = json.loads(roi_path.read_text()) if roi_path.exists() else {}
        if not previous or previous.get('camera_unique_id') != camera_id:
            write_json(roi_path, {'status': 'default', 'camera': role,
                'source_kind': 'snapshot', 'camera_driver': 'reference',
                'camera_unique_id': camera_id, 'camera_label': f'{role.title()} camera',
                'source_size_wh': [640, 480], 'bounds_xyxy': [0, 0, 640, 480],
                'crop_xywh': [0, 0, 640, 480], 'source_file': f'assets/{role}.jpg',
                'coordinate_convention': 'top-left origin; right and bottom exclusive'})
    return config
