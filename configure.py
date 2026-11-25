#!/usr/bin/env python3
"""Select devices without opening serial ports, enabling torque, or capturing images."""
import sys
from pathlib import Path

from app_config import ROOT, initialize_local_files, load_config, write_json


def select(label, entries, current, excluded=''):
    print(f'\n{label} (current: {current or "not configured"})')
    available = [entry for entry in entries if entry['id'] != excluded]
    for index, entry in enumerate(available, 1):
        print(f'  {index}. {entry["name"]} — {entry["id"]}')
    print('  Enter = keep current; 0 = leave unconfigured')
    while True:
        value = input('Selection: ').strip()
        if not value and current != excluded:
            return current
        if not value and not current:
            return ''
        if value == '0':
            return ''
        if value.isdigit() and 1 <= int(value) <= len(available):
            return available[int(value)-1]['id']
        print('Select a listed number.')


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This release uses macOS camera capture. Windows/Linux capture is not included.')
    print('Close all three app servers before changing devices. No motor writes occur here.')
    config = load_config()
    from serial.tools import list_ports
    sys.path.insert(0, str(ROOT / 'camera_setup'))
    from native_camera import enumerate_cameras
    cameras = enumerate_cameras(ROOT / 'camera_setup')
    ports = [{'id': port.device, 'name': port.description} for port in list_ports.comports()
             if port.device.startswith('/dev/cu.')]
    print('\nIdentify the leader by unplugging ONLY its USB cable and comparing the port list,\nthen reconnect and rerun this tool if necessary. Do not guess the roles.')
    for section, roles, entries in [('cameras', ('front', 'wrist'), cameras),
                                    ('arms', ('leader', 'follower'), ports)]:
        for n, role in enumerate(roles):
            config[section][role] = select(f'{role.title()} {section}', entries,
                config[section][role], config[section][roles[0]] if n else '')
    print('\nSelected devices:\n' + __import__('json').dumps(config, indent=2))
    if input('Save these device roles? [y/N] ').strip().lower() != 'y':
        return
    write_json(ROOT / 'config.local.json', config)
    initialize_local_files()
    print('Saved. Open Camera Setup to check views/ROIs, then Calibration Check to calibrate.\nChanging arm USB identities requires calibration before motion can start.')


if __name__ == '__main__':
    main()
