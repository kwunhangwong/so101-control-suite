#!/usr/bin/env python3
"""Local Camera Setup app. Requires only Python 3.9+."""
import argparse
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
import signal
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import webbrowser
from native_camera import FRONT_CAMERA_ID, WRIST_CAMERA_ID, NativeCamera
from camera_controls import CameraControls
from app_config import initialize_local_files

ROOT = Path(__file__).resolve().parent
initialize_local_files()
CONTROLS = CameraControls(ROOT)
SPECS = {
    "front": {"id": FRONT_CAMERA_ID, "size": [640, 480], "focus": "front camera; full frame until an ROI is chosen"},
    "wrist": {"id": WRIST_CAMERA_ID, "size": [640, 480], "focus": "wrist camera; full frame until an ROI is chosen"},
}
CONFIG_PATHS = {name: ROOT / "config" / f"{name}_roi.json" for name in SPECS}
CAMERAS = {name: NativeCamera(ROOT, spec["id"]) for name, spec in SPECS.items()}
CAMERA_LOCK = threading.RLock()
CAMERA_SESSIONS = set()
STATIC = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/assets/front.jpg": "assets/front.jpg", "/assets/wrist.jpg": "assets/wrist.jpg"}


def close_camera_session(session_id):
    with CAMERA_LOCK:
        if session_id not in CAMERA_SESSIONS:
            return
        CAMERA_SESSIONS.remove(session_id)
        if not CAMERA_SESSIONS:
            for camera in CAMERAS.values():
                camera.stop()


def camera_route(path):
    if path == "/api/roi":
        return "front", "roi"
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "api" and parts[1] in CAMERAS:
        return parts[1], parts[2]
    return None, None


def validate_roi(data, camera="front"):
    if camera not in SPECS:
        raise ValueError("Unknown camera.")
    spec = SPECS[camera]
    if not isinstance(data, dict):
        raise ValueError("ROI must be a JSON object.")
    size, bounds = data.get("source_size_wh"), data.get("bounds_xyxy")
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or not 1 <= v <= 16384 for v in size):
        raise ValueError("Source size must contain two positive integer pixel dimensions.")
    if not isinstance(bounds, list) or len(bounds) != 4 or any(type(v) is not int for v in bounds):
        raise ValueError("Bounds must contain four integer pixel coordinates.")
    left, top, right, bottom = bounds
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError("ROI must stay inside the source frame with positive width and height.")
    kind = data.get("source_kind", "snapshot")
    if kind not in ("snapshot", "live"):
        raise ValueError("Source must be snapshot or live.")
    if kind == "snapshot" and size != spec["size"]:
        raise ValueError(f"The {camera} reference image is {spec['size'][0]} × {spec['size'][1]} pixels.")
    driver = "reference" if kind == "snapshot" else data.get("camera_driver", "browser")
    if driver not in ("reference", "browser", "native"):
        raise ValueError("Unknown camera driver.")
    label = data.get("camera_label", f"{camera.title()} camera")
    device_id = data.get("camera_browser_device_id")
    if not isinstance(label, str) or len(label) > 300:
        raise ValueError("Camera label is invalid.")
    if device_id is not None and (not isinstance(device_id, str) or len(device_id) > 512):
        raise ValueError("Camera device ID is invalid.")
    return {
        "status": "chosen", "camera": camera, "source_kind": kind,
        "camera_driver": driver,
        "camera_unique_id": spec["id"] if kind == "snapshot" or driver == "native" else None,
        "camera_label": label,
        "camera_browser_device_id": device_id if kind == "live" else None,
        "source_file": f"assets/{camera}.jpg" if kind == "snapshot" else None,
        "source_sha256": hashlib.sha256((ROOT / "assets" / f"{camera}.jpg").read_bytes()).hexdigest() if kind == "snapshot" else None,
        "source_size_wh": size, "bounds_xyxy": bounds,
        "crop_xywh": [left, top, right - left, bottom - top],
        "coordinate_convention": "top-left origin; right and bottom exclusive",
        "focus": spec["focus"],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        _, action = camera_route(urlsplit(self.path).path)
        if self.command == "GET" and action in ("status", "frame"):
            return
        super().log_message(format, *args)

    def reply(self, status, content, content_type="application/json", headers=None):
        if content_type == "application/json":
            content = json.dumps(content).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        request = urlsplit(self.path)
        path = request.path
        name, action = camera_route(path)
        if path == "/api/session":
            session_id = parse_qs(request.query).get("session", [""])[0]
            if not session_id or len(session_id) > 100:
                self.reply(400, {"error": "Missing app session."})
                return
            with CAMERA_LOCK:
                if session_id in CAMERA_SESSIONS:
                    self.reply(409, {"error": "App session is already open."})
                    return
                CAMERA_SESSIONS.add(session_id)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    with CAMERA_LOCK:
                        if session_id not in CAMERA_SESSIONS:
                            break
                    self.wfile.write(b"data: connected\n\n")
                    self.wfile.flush()
                    time.sleep(1)
            except OSError:
                pass
            finally:
                close_camera_session(session_id)
        elif path == "/api/cameras/status":
            self.reply(200, {name: camera.status() for name, camera in CAMERAS.items()})
        elif action == "status":
            self.reply(200, CAMERAS[name].status())
        elif action == "controls":
            try:
                self.reply(200, CONTROLS.query(name))
            except Exception as error:
                self.reply(503, {"error": str(error)})
        elif action == "frame":
            camera = CAMERAS[name]
            frame, sequence = camera.latest()
            if frame and camera.status()["state"] == "live":
                self.reply(200, frame, "image/jpeg", {"X-Frame-Sequence": sequence})
            else:
                self.reply(503, {"error": f"{name.capitalize()} camera is not streaming."})
        elif action == "roi":
            try:
                self.reply(200, json.loads(CONFIG_PATHS[name].read_text()))
            except (OSError, ValueError):
                self.reply(500, {"error": f"Cannot read config/{name}_roi.json."})
        elif path in STATIC:
            file = ROOT / STATIC[path]
            self.reply(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        else:
            self.reply(404, {"error": "Not found."})

    def do_POST(self):
        request = urlsplit(self.path)
        path = request.path
        name, action = camera_route(path)
        if action not in ("roi", "start", "stop", "controls") and path not in ("/api/cameras/start", "/api/cameras/stop", "/api/session/close"):
            self.reply(404, {"error": "Not found."})
            return
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if origin and origin not in allowed:
            self.reply(403, {"error": "Save requests must come from this local app."})
            return
        session_id = parse_qs(request.query).get("session", [""])[0]
        if path == "/api/session/close":
            close_camera_session(session_id)
            self.reply(200, {"closed": True})
            return
        if action == "controls":
            try:
                with CAMERA_LOCK:
                    if session_id not in CAMERA_SESSIONS:
                        raise ValueError("Open the Camera Setup app before adjusting the camera.")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 or self.headers.get_content_type() != "application/json":
                        raise ValueError("Send camera settings as JSON.")
                    values = CONTROLS.query(name, json.loads(self.rfile.read(length)))
                self.reply(200, values)
            except Exception as error:
                self.reply(409, {"error": str(error)})
            return
        if action in ("start", "stop") or path in ("/api/cameras/start", "/api/cameras/stop"):
            with CAMERA_LOCK:
                if session_id not in CAMERA_SESSIONS:
                    self.reply(409, {"error": "Open the Camera Setup app before connecting cameras."})
                    return
                selected = CAMERAS if name is None else {name: CAMERAS[name]}
                if action == "start" or path == "/api/cameras/start":
                    result = {key: camera.start() for key, camera in selected.items()}
                else:
                    for camera in selected.values():
                        camera.stop()
                    result = {key: camera.status() for key, camera in selected.items()}
                self.reply(200, result[name] if name else result)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768 or self.headers.get_content_type() != "application/json":
                raise ValueError("Send a JSON ROI smaller than 32 KB.")
            roi = validate_roi(json.loads(self.rfile.read(length)), name)
        except (ValueError, UnicodeDecodeError) as error:
            self.reply(400, {"error": str(error)})
            return
        try:
            config_path = CONFIG_PATHS[name]
            with tempfile.NamedTemporaryFile(mode="w", dir=config_path.parent, suffix=".tmp", delete=False) as handle:
                json.dump(roi, handle, indent=2)
                handle.write("\n")
                temp_path = handle.name
            os.replace(temp_path, config_path)
        except OSError:
            self.reply(500, {"error": f"Could not write config/{name}_roi.json."})
            return
        self.reply(200, roi)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = HTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            try:
                with urlopen(url + "/api/roi", timeout=2) as response:
                    existing = json.load(response)
            except (OSError, ValueError):
                existing = None
            if isinstance(existing, dict) and existing.get("camera") == "front" and existing.get("coordinate_convention") == "top-left origin; right and bottom exclusive":
                print(f"SO-101 Camera Setup is already running: {url}", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return
        parser.exit(1, f"Cannot start local app: {error}\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"SO-101 Camera Setup: {url}\nSaving to: {ROOT / 'config'}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    def stop_server(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGHUP, stop_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with CAMERA_LOCK:
            CAMERA_SESSIONS.clear()
            for camera in CAMERAS.values():
                camera.stop()
        server.server_close()


if __name__ == "__main__":
    main()
