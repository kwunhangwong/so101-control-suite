#!/usr/bin/env python3
"""Local calibration-check interface; opening it only reads the motors."""
import argparse
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_config import initialize_local_files
from controller import CalibrationController

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dataset_collection'))
import cleaning
import cleaned_buffer
import cleaning_preview
import seed_preview

ROOT = Path(__file__).resolve().parent
CLEANED_BUFFER_LOCK = threading.RLock()
STATIC = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
STATIC.update({f'/dataset/{name}': (f'../dataset_collection/{name}', kind) for name, kind in
               [('app.js', 'text/javascript'), ('roi.mjs', 'text/javascript'), ('cleaning.js', 'text/javascript'), ('styles.css', 'text/css')]})
STATIC['/dataset/'] = STATIC['/dataset'] = ('../dataset_collection/index.html', 'text/html')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if urlsplit(self.path).path not in ('/api/status', '/api/front/frame', '/api/wrist/frame', '/api/front/roi', '/api/wrist/roi'):
            super().log_message(format, *args)

    def reply(self, code, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def app_state(self):
        return {**self.server.controller.snapshot(), 'bootstrap_cleaning':{}, 'features':{'seed':True, 'evaluation':False, 'gpt':False, 'dagger':False}}

    def do_GET(self):
        request = urlsplit(self.path)
        controller = self.server.controller
        if request.path.startswith('/api/cleaned-buffer/'):
            query = parse_qs(request.query)
            base = ROOT.parent / 'dataset_collection/cleaned_episodes'
            buffer, name = (query.get(key, [''])[0] for key in ('buffer', 'episode'))
            try:
                with CLEANED_BUFFER_LOCK:
                    if request.path == '/api/cleaned-buffer/episodes':
                        folder = cleaned_buffer.buffer_folder(base, buffer)
                        self.reply(200, {'buffer': str(folder), 'episodes': cleaned_buffer.list_episodes(base, buffer)})
                    elif request.path == '/api/cleaned-buffer/episode':
                        self.reply(200, cleaned_buffer.detail(base, buffer, name))
                    elif request.path == '/api/cleaned-buffer/no-ops':
                        self.reply(200, cleaned_buffer.no_ops(base, buffer, name,
                                   float(query.get('tolerance_ticks', ['20'])[0]), json.loads(query.get('excluded', ['[]'])[0])))
                    elif request.path == '/api/cleaned-buffer/frame':
                        self.reply(200, cleaned_buffer.frame(base, buffer, name, int(query.get('index', ['-1'])[0]),
                                   query.get('camera', [''])[0], query.get('revision', [''])[0]), 'image/jpeg')
                    else:
                        self.reply(404, {'error': 'Not found.'})
            except (OSError, ValueError, KeyError) as error:
                self.reply(400, {'error': str(error)})
        elif request.path.startswith('/api/cleaning/'):
            query = parse_qs(request.query)
            root = ROOT.parent / 'dataset_collection/episodes'
            try:
                name = query.get('episode', [''])[0]
                if request.path == '/api/cleaning/episodes':
                    self.reply(200, {'episodes': cleaning.list_episodes(root)})
                elif request.path == '/api/cleaning/episode':
                    self.reply(200, cleaning.detail(root, name))
                elif request.path == '/api/cleaning/no-ops':
                    self.reply(200, cleaning.no_ops(root, name,
                               float(query.get('tolerance_ticks', ['20'])[0]), json.loads(query.get('excluded', ['[]'])[0])))
                elif request.path == '/api/cleaning/frame':
                    self.reply(200, cleaning.frame(root, name, int(query.get('index', ['-1'])[0]), query.get('camera', [''])[0]), 'image/jpeg')
                else:
                    self.reply(404, {'error': 'Not found.'})
            except (OSError, ValueError, KeyError) as error:
                self.reply(400, {'error': str(error)})
        elif request.path.startswith('/api/cleaning-preview/video/'):
            with cleaning_preview.LOCK:
                seed_preview.serve(self, cleaning_preview.cache_dir(ROOT.parent / 'dataset_collection'),
                                   request.path.rsplit('/', 1)[-1], cache_control='no-store')
        elif request.path == '/api/status':
            self.reply(200, self.app_state())
        elif request.path in ('/api/front/frame', '/api/wrist/frame'):
            camera = controller.camera if request.path == '/api/front/frame' else controller.wrist_camera
            info = camera.status() if camera else {'state': 'off'}
            frame, _ = camera.latest() if camera else (None, 0)
            if frame and info['state'] == 'live' and time.time() - info.get('captured_at', 0) <= 1:
                self.reply(200, frame, 'image/jpeg')
            else:
                self.reply(503, {'error': info.get('error') or 'No fresh camera frames. Use Check Connection to reconnect.'})
        elif request.path in ('/api/front/roi', '/api/wrist/roi'):
            name = request.path.split('/')[2]
            try:
                self.reply(200, json.loads((ROOT.parent / 'camera_setup/config' / f'{name}_roi.json').read_text()))
            except (OSError, ValueError):
                self.reply(500, {'error': f'Cannot read the saved {name} ROI. Save it again in the Camera Setup app.'})
        elif request.path == '/api/session':
            session = parse_qs(request.query).get('session', [''])[0]
            if not session or len(session) > 100:
                self.reply(400, {'error': 'Missing page session.'})
                return
            try:
                controller.open_page(session)
            except ValueError as error:
                self.reply(409, {'error': str(error)})
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                while controller.has_page(session):
                    self.wfile.write(('data: ' + json.dumps(self.app_state()) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                controller.close_page(session)
        elif request.path in STATIC:
            name, kind = STATIC[request.path]
            self.reply(200, (ROOT / name).read_bytes(), kind)
        else:
            self.reply(404, {'error': 'Not found.'})

    def do_POST(self):
        request = urlsplit(self.path)
        allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if self.headers.get('Origin') and self.headers['Origin'] not in allowed:
            self.reply(403, {'error': 'Use this local interface to operate the arms.'})
            return
        if request.path in ('/api/cleaning/preview', '/api/cleaned-buffer/preview'):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid preview request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid preview request.')
                with CLEANED_BUFFER_LOCK:
                    mode = ('cleaned' if request.path.startswith('/api/cleaned-buffer/') else 'seed')
                    result = cleaning_preview.prepare(ROOT.parent / 'dataset_collection', mode, body)
                self.reply(200, result)
            except (OSError, ValueError, KeyError) as error:
                self.reply(400, {'error':str(error)})
            return
        if request.path == '/api/cleaned-buffer/save':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid cleaned-buffer request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid cleaned-buffer request.')
                controller = self.server.controller
                with controller.lock, CLEANED_BUFFER_LOCK:
                    state = controller.snapshot()
                    if (state['phase'] not in ('ready', 'holding', 'offline', 'error') or
                            state.get('checking_connections') or state.get('gripper_busy') or state.get('calibration_busy') or
                            any(e.get('status') in ('recording', 'awaiting_label') or e.get('robot_finished') is False
                                for e in (state.get('episode', {}), state.get('evaluation', {}).get('episode', {}), state.get('bootstrap', {}).get('episode', {})))):
                        raise ValueError('End and label the current episode, and wait for movement to finish.')
                    result = cleaned_buffer.save(ROOT.parent / 'dataset_collection/cleaned_episodes', body)
                    controller.log_event('INFO', f"Cleaned episode updated in place: {result['path']}")
                self.reply(200, result)
            except (OSError, ValueError, KeyError) as error:
                self.reply(400, {'error': str(error)})
            return
        if request.path in ('/api/cleaning/save', '/api/cleaning/delete'):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid cleaning request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid cleaning request.')
                controller = self.server.controller
                root = ROOT.parent / 'dataset_collection/episodes'
                with controller.lock, CLEANED_BUFFER_LOCK:
                    if request.path == '/api/cleaning/delete':
                        if body.get('confirm') != 'DELETE_EPISODE':
                            raise ValueError('Confirm deletion of the selected episode.')
                        state = controller.snapshot()
                        if (state['phase'] not in ('ready', 'holding', 'offline', 'error') or
                                any(e.get('status') in ('recording', 'awaiting_label') or e.get('robot_finished') is False
                                    for e in (state.get('episode', {}), state.get('evaluation', {}).get('episode', {}), state.get('bootstrap', {}).get('episode', {})))):
                            raise ValueError('End and label the current episode, and wait for movement to finish.')
                        result = cleaning.delete_episode(root, body.get('episode'))
                        recorder = controller.recorder
                        with recorder.lock:
                            if recorder.folder and str(recorder.folder.resolve()) == result['path']:
                                remaining = cleaning.episode_manifests(root)
                                recorder.folder = remaining[-1].parent if remaining else None
                                recorder.state = (json.loads(remaining[-1].read_text()) if remaining else
                                                  {'status': 'idle', 'path': None, 'frames': 0, 'label': None, 'error': None})
                                recorder.owner = None
                        controller.log_event('INFO', f"Deleted recorded episode: {result['path']}")
                    else:
                        result = cleaning.export_cleaned(root, body.get('episode'), body.get('excluded'), body.get('split_excluded'))
                self.reply(200, result)
            except (OSError, ValueError, KeyError) as error:
                self.reply(400, {'error': str(error)})
            return
        session = parse_qs(request.query).get('session', [''])[0]
        controller = self.server.controller
        if request.path == '/api/session/close':
            controller.close_page(session)
            self.reply(200, {'closed': True})
            return
        action = request.path.removeprefix('/api/')
        if action.startswith(('bootstrap-', 'evaluation-')):
            self.reply(409, {'error':'Policy inference is unavailable.'})
            return
        confirmations = {'reset': 'RESET', 'test': 'TEST', 'release': 'RELEASE',
                         'gripper-force': 'GRIPPER_FORCE', 'teach-rest': 'TEACH_REST',
                         'episode-start': 'RECORD', 'episode-label': 'LABEL',
                         'bootstrap-prepare': 'PREPARE_BUFFER', 'bootstrap-start': 'BOOTSTRAP',
                         'bootstrap-label': 'LABEL', 'bootstrap-reset-count': 'RESET_COUNT',
                         'bootstrap-reasoning': 'REASONING',
                         'evaluation-reset-statistics': 'RESET_STATISTICS',
                         'evaluation-select': 'SELECT_CHECKPOINT', 'evaluation-start': 'EVALUATE', 'evaluation-label': 'LABEL',
                         'recalibrate-restore': 'RESTORE_CALIBRATION',
                         'recalibrate-begin': 'RECALIBRATE', 'recalibrate-center': 'CENTER',
                         'recalibrate-review': 'REVIEW', 'recalibrate-save': 'SAVE_CALIBRATION', 'recalibrate-cancel': 'CANCEL'}
        if action not in ('check-connections', 'stop', 'gripper-read', 'episode-end', 'evaluation-check', 'evaluation-end', 'bootstrap-end', 'bootstrap-refresh', *confirmations):
            self.reply(404, {'error': 'Not found.'})
            return
        try:
            body = None
            if action in confirmations:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Confirm this action in the interface.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or body.get('confirm') != confirmations[action]:
                    raise ValueError('Confirmation is required.')
            with controller.lock:
                controller.command(session, action, body)
            self.reply(200, self.app_state())
        except (ValueError, UnicodeError, OSError) as error:
            controller.log_event('ERROR', f'{action}: {error}')
            self.reply(409, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--view', choices=('calibration', 'dataset'), default='calibration')
    args = parser.parse_args()
    url = f'http://127.0.0.1:{args.port}'
    page_url = url + ('/dataset/' if args.view == 'dataset' else '/')
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            try:
                with urlopen(url + '/api/status', timeout=2) as response:
                    existing = json.load(response)
                if existing.get('app') == 'so101-calibration-check':
                    if not args.no_browser:
                        webbrowser.open(page_url)
                    print(f'App already running: {page_url}', flush=True)
                    return
            except (OSError, ValueError):
                pass
        parser.exit(1, f'Cannot start calibration interface: {error}\n')
    sys.path.insert(0, str(ROOT.parent / 'camera_setup'))
    from native_camera import NativeCamera, WRIST_CAMERA_ID
    from camera_controls import CameraControls
    sys.path.insert(0, str(ROOT.parent / 'dataset_collection'))
    from recording import EpisodeRecorder
    initialize_local_files()
    camera_root = ROOT.parent / 'camera_setup'
    front, wrist = NativeCamera(camera_root), NativeCamera(camera_root, WRIST_CAMERA_ID)
    recorder = EpisodeRecorder(ROOT.parent / 'dataset_collection/episodes', camera_root / 'config',
                               {'front': front, 'wrist': wrist}, controls=CameraControls(camera_root))
    server.controller = CalibrationController(camera=front, wrist_camera=wrist, recorder=recorder)
    print(f'SO-101 interface: {page_url}\nUse Check Connection to check devices; Clear Error & Reset returns to the saved resting pose.', flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(page_url)).start()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.close()
        server.server_close()


if __name__ == '__main__':
    main()
