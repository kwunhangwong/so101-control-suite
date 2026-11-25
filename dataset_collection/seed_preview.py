"""Serve local preview videos with HTTP byte-range support."""
from pathlib import Path
import re


def serve(handler, cache, filename, cache_control='private, max-age=31536000, immutable'):
    """Stream a cached MP4 with single byte-range support for browser seeking."""
    if filename != 'preview.mp4' and not re.fullmatch(r'[0-9a-f]{64}\.mp4', filename):
        handler.reply(404, {'error': 'Preview not found.'})
        return
    path = Path(cache) / filename
    if path.is_symlink() or not path.is_file():
        handler.reply(404, {'error': 'Preview not found.'})
        return
    size = path.stat().st_size
    header = handler.headers.get('Range')
    start, end = 0, size-1
    if header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
        try:
            if not match or not any(match.groups()):
                raise ValueError()
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size-1) if last else size-1
            else:
                if int(last) == 0:
                    raise ValueError()
                start = max(0, size-int(last))
            if not 0 <= start <= end < size:
                raise ValueError()
        except ValueError:
            handler.send_response(416)
            handler.send_header('Cache-Control', cache_control)
            handler.send_header('Content-Range', f'bytes */{size}')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
    handler.send_response(206 if header else 200)
    handler.send_header('Content-Type', 'video/mp4')
    handler.send_header('Content-Length', str(end-start+1))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', cache_control)
    handler.send_header('X-Content-Type-Options', 'nosniff')
    if header:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    handler.end_headers()
    try:
        with path.open('rb') as video:
            video.seek(start)
            remaining = end-start+1
            while remaining:
                chunk = video.read(min(65536, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass  # The viewer switched seeds or sought elsewhere.
