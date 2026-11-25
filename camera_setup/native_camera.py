"""Capture an identified USB camera through macOS, keeping JPEG frames in RAM."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_config import load_config

FRONT_CAMERA_ID = load_config()['cameras']['front']
WRIST_CAMERA_ID = load_config()['cameras']['wrist']


def enumerate_cameras(root=None):
    """List camera identities without opening a capture session."""
    camera = NativeCamera(Path(root or Path(__file__).parent), '')
    result = subprocess.run([str(camera._build()), '--list'], check=True,
                            capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout)


class NativeCamera:
    def __init__(self, root, camera_id=FRONT_CAMERA_ID):
        self.root = root
        self.camera_id = camera_id
        self.process = None
        self.frame = None
        self.lock = threading.Lock()
        self.info = {"state": "off", "camera_unique_id": camera_id, "sequence": 0}

    def _build(self):
        bundle = self.root.parent / ".camera" / "FrontCamera.app"
        contents = bundle / "Contents"
        executable = contents / "MacOS" / "FrontCamera"
        source = self.root / "native_camera.m"
        if executable.exists() and executable.stat().st_mtime >= source.stat().st_mtime:
            return executable
        executable.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"CFBundleIdentifier": "local.so101.roi-camera", "CFBundleExecutable": "FrontCamera", "CFBundleName": "SO101 Cameras", "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "NSCameraUsageDescription": "Display the connected SO-101 front and wrist cameras in the local Camera Setup app."}
        (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
        entitlements = self.root.parent / ".camera" / "entitlements.plist"
        entitlements.write_bytes(plistlib.dumps({"com.apple.security.device.camera": True}))
        command = ["/usr/bin/clang", "-fobjc-arc", "-fblocks"]
        for framework in ["Foundation", "AVFoundation", "CoreImage", "CoreVideo", "CoreMedia", "CoreGraphics"]:
            command.extend(["-framework", framework])
        subprocess.run(command + ["-o", str(executable), str(source)], check=True, capture_output=True)
        subprocess.run(["/usr/bin/codesign", "-f", "-s", "-", "--entitlements", str(entitlements), str(bundle)], check=True, capture_output=True)
        return executable

    def start(self):
        if self.process and self.process.poll() is None and self.status()["state"] in ("starting", "live"):
            return self.status()
        self.stop()
        with self.lock:
            self.info.update(state="starting", error=None, sequence=0)
            if not self.camera_id:
                self.info.update(state="error", error="No camera is configured. Run configure.py to select front and wrist cameras.")
                return dict(self.info)
        try:
            executable = self._build()
            process = subprocess.Popen([str(executable), self.camera_id], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.process = process
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
        except (OSError, subprocess.CalledProcessError) as error:
            with self.lock:
                self.info.update(state="error", error=f"Cannot start macOS camera capture: {error}")
        return self.status()

    def _read(self, process):
        try:
            while self.process is process:
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Camera capture stopped. Check its USB connection and reconnect.")
                header = json.loads(line)
                if "event" in header:
                    with self.lock:
                        self.info["stage"] = header["event"]
                    continue
                if "error" in header:
                    raise RuntimeError(header["error"])
                length = header["length"]
                if not 0 < length <= 20_000_000:
                    raise RuntimeError("Camera returned an invalid frame length.")
                frame = process.stdout.read(length)
                if len(frame) != length:
                    raise RuntimeError("Camera frame was incomplete.")
                with self.lock:
                    if self.process is not process:
                        break
                    self.frame = frame
                    self.camera_id = header.get("camera_unique_id", self.camera_id)
                    self.info.update(state="live", stage="streaming", camera_unique_id=self.camera_id, width=header["width"], height=header["height"], sequence=self.info["sequence"] + 1, captured_at=time.time(), captured_monotonic_ns=time.monotonic_ns(), error=None)
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            with self.lock:
                if self.process is process:
                    self.info.update(state="error", error=str(error))
        finally:
            process.stdout.close()

    def status(self):
        with self.lock:
            info = dict(self.info)
        if info["state"] == "live" and time.time() - info["captured_at"] > 5:
            info.update(state="error", error="No new camera frames. Check its USB connection and reconnect.")
        return info

    def sample(self):
        """Return one frame and its receive timestamp/sequence atomically."""
        with self.lock:
            return self.frame, dict(self.info)

    def latest(self):
        with self.lock:
            return self.frame, self.info["sequence"]

    def stop(self):
        process, self.process = self.process, None
        with self.lock:
            self.frame = None
            self.info = {"state":"off", "camera_unique_id":self.camera_id, "sequence":0, "error":None}
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
