"""Read and verify hardware exposure/saturation/contrast without taking over video capture."""
import json
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_config import load_config


class CameraControls:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()

    def query(self, camera, settings=None):
        if camera not in ('front', 'wrist'):
            raise ValueError('Unknown camera.')
        if settings is not None:
            if (not isinstance(settings, dict) or set(settings) != {'auto_exposure', 'exposure', 'saturation', 'contrast'}
                    or type(settings['auto_exposure']) is not bool
                    or any(type(settings[key]) is not int for key in ('exposure', 'saturation', 'contrast'))):
                raise ValueError('Send auto exposure, exposure time, saturation and contrast as valid camera settings.')
        camera_id = load_config()['cameras'][camera]
        if not camera_id:
            raise ValueError('No camera is configured. Run configure.py to select front and wrist cameras.')
        with self.lock:
            source = self.root / 'camera_controls.m'
            executable = self.root.parent / '.camera/UVCControls'
            if not executable.exists() or executable.stat().st_mtime < source.stat().st_mtime:
                executable.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['/usr/bin/clang', '-fobjc-arc', '-framework', 'Foundation', '-framework', 'IOKit',
                                '-o', str(executable), str(source)], check=True, capture_output=True, timeout=30)
            args = [str(executable), camera, camera_id]
            if settings is not None:
                args.append(json.dumps(settings, allow_nan=False))
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            try:
                values = json.loads(result.stdout)
            except ValueError:
                raise ValueError('Could not read camera controls. Reconnect the camera and try again.') from None
            if result.returncode or 'error' in values:
                raise ValueError(values.get('error', 'Camera control failed.'))
            return values
