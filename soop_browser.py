#!/usr/bin/env python3
"""Loopback-only browser player: SOOP native receiver -> FFmpeg HLS -> Brave."""
import argparse
import functools
import http.server
import json
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parent


class Playback:
    def __init__(self, runtime, session_id):
        self.runtime = runtime
        self.session_id = session_id
        self.lock = threading.RLock()
        self.receiver = self.ffmpeg = None
        self.logs = []
        self.generation = None
        self.url = ''
        self.quality = '1080p'
        self.samples = []
        self.started = None

    def stop(self):
        with self.lock:
            for process in (self.receiver, self.ffmpeg):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
            self.receiver = self.ffmpeg = None
            for log in self.logs:
                log.close()
            self.logs = []

    def start(self, url, quality='1080p'):
        import re
        if not re.fullmatch(r'https?://play\.(?:sooplive\.com|sooplive\.co\.kr|afreecatv\.com)/[A-Za-z0-9_]+(?:/\d+)?/?', url):
            raise ValueError('SOOP 공개 생방송 URL을 입력해 주세요.')
        if quality not in ('1080p', '720p'):
            raise ValueError('고화질은 1080p 또는 720p를 선택해 주세요.')
        with self.lock:
            self.stop()
            # Delete only obsolete media created in this instance's private runtime.
            for old in (self.runtime / 'streams').glob('*'):
                if old.is_dir():
                    shutil.rmtree(old)
            self.generation = secrets.token_hex(6)
            folder = self.runtime / 'streams' / self.generation
            folder.mkdir(parents=True)
            diagnostics = self.runtime / 'diagnostics' / self.generation
            diagnostics.mkdir(parents=True)
            source_log = (diagnostics / 'receiver.log').open('wb')
            mux_log = (diagnostics / 'ffmpeg.log').open('wb')
            self.logs = [source_log, mux_log]
            self.receiver = subprocess.Popen([sys.executable, str(ROOT / 'soop_player.py'), url, '--quality', quality, '--no-player', '--flv-stdout',
                '--report-dir', str(diagnostics)], stdout=subprocess.PIPE, stderr=source_log, cwd=ROOT)
            try:
                self.ffmpeg = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-i', 'pipe:0',
                    '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '8',
                    '-hls_delete_threshold', '2', '-hls_flags', 'delete_segments+independent_segments+temp_file',
                    '-hls_segment_filename', str(folder / 'segment-%08d.ts'), str(folder / 'index.m3u8')],
                    stdin=self.receiver.stdout, stdout=subprocess.DEVNULL, stderr=mux_log, cwd=ROOT)
            except Exception:
                self.stop()
                raise
            finally:
                self.receiver.stdout.close()
            self.url = url
            self.quality = quality
            self.samples = []
            self.started = time.time()
            return self.status()

    def status(self):
        with self.lock:
            live = self.receiver is not None and self.receiver.poll() is None and self.ffmpeg is not None and self.ffmpeg.poll() is None
            playlist = self.runtime / 'streams' / (self.generation or '-') / 'index.m3u8'
            message = ''
            if self.receiver is not None and not live:
                log = self.runtime / 'diagnostics' / self.generation / 'receiver.log'
                message = log.read_text(errors='replace')[-1500:] if log.exists() else '재생 프로세스가 종료되었습니다.'
            return {'running': live, 'ready': live and playlist.exists(), 'generation': self.generation,
                'playlist': f'/sessions/{self.session_id}/streams/{self.generation}/index.m3u8', 'url': self.url,
                'quality': self.quality, 'error': message,
                'elapsed': round(time.time() - self.started, 1) if self.started else 0,
                'latest_browser': self.samples[-1] if self.samples else None}


class Playbacks:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.items = {}
        self.last_seen = {}

    def get(self, session_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session_id):
            raise ValueError('재생 세션 이름이 올바르지 않습니다.')
        return self.items.get(session_id)

    def status(self, session_id):
        with self.lock:
            player = self.get(session_id)
            return player.status() if player else {'running': False, 'ready': False, 'generation': None,
                'playlist': None, 'url': '', 'quality': None, 'error': '', 'elapsed': 0, 'latest_browser': None}

    def start(self, session_id, url, quality='1080p'):
        with self.lock:
            player = self.get(session_id)
            if player is None:
                if sum(bool(p.status()['running']) for p in self.items.values()) >= 3:
                    raise ValueError('동시에 재생할 수 있는 방송은 세 개까지입니다.')
                folder = self.runtime / 'sessions' / session_id
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'streams').mkdir(exist_ok=True)
                player = self.items[session_id] = Playback(folder, session_id)
            if session_id.startswith('tab-'):
                self.last_seen[session_id] = time.monotonic()
            return player.start(url, quality)

    def ping(self, session_id):
        with self.lock:
            player = self.get(session_id)
            if player is None:
                raise ValueError('재생 세션이 없습니다.')
            if session_id.startswith('tab-'):
                self.last_seen[session_id] = time.monotonic()
            return {'ok': True}

    def stop(self, session_id):
        with self.lock:
            player = self.get(session_id)
            if player:
                player.stop()
                del self.items[session_id]
            self.last_seen.pop(session_id, None)
            return self.status(session_id)

    def reap(self):
        with self.lock:
            expired = [sid for sid, seen in self.last_seen.items() if time.monotonic() - seen > 120]
            for sid in expired:
                self.stop(sid)

    def stop_all(self):
        with self.lock:
            for player in self.items.values():
                player.stop()
            self.items.clear()
            self.last_seen.clear()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        if self.path.startswith('/sessions/') and self.headers.get('Origin') == 'https://play.sooplive.com':
            self.send_header('Access-Control-Allow-Origin', 'https://play.sooplive.com')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def local_request(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def json_response(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.local_request():
            return self.send_error(403)
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/token':
            return self.json_response({'token': self.server.token})
        if path == '/api/status':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                return self.json_response(self.server.playbacks.status(query.get('session', ['local'])[0]))
            except ValueError as error:
                return self.json_response({'error': str(error)}, 400)
        if path == '/':
            raw = (ROOT / 'web/index.html').read_text().replace('__TOKEN__', self.server.token).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path == '/vendor/hls.min.js':
            self.directory = str(ROOT / 'web')
        elif path.startswith('/sessions/'):
            parts = Path(urllib.parse.unquote(path)).parts
            if (len(parts) != 6 or parts[1] != 'sessions' or parts[3] != 'streams'
                or not re.fullmatch(r'(?:index\.m3u8|segment-\d{8}\.ts)', parts[5])):
                return self.send_error(404)
            try:
                player = self.server.playbacks.get(parts[2])
            except ValueError:
                return self.send_error(404)
            if player is None or player.generation != parts[4]:
                return self.send_error(404)
            self.directory = str(self.server.playbacks.runtime)
        else:
            return self.send_error(404)
        return super().do_GET()

    def do_OPTIONS(self):
        if (not self.local_request() or not self.path.startswith('/sessions/')
            or self.headers.get('Origin') != 'https://play.sooplive.com'):
            return self.send_error(403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if not self.local_request() or self.headers.get('X-Local-Token') != self.server.token:
            return self.send_error(403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                return self.send_error(400)
            data = json.loads(self.rfile.read(size))
            session_id = str(data.get('session', 'local'))
            if self.path == '/api/start':
                return self.json_response(self.server.playbacks.start(session_id, str(data.get('url', '')).strip(), str(data.get('quality', '1080p'))))
            if self.path == '/api/stop':
                return self.json_response(self.server.playbacks.stop(session_id))
            if self.path == '/api/ping':
                return self.json_response(self.server.playbacks.ping(session_id))
            if self.path == '/api/telemetry':
                allow = ('width', 'height', 'currentTime', 'paused', 'muted', 'volume', 'readyState', 'totalVideoFrames', 'droppedVideoFrames', 'browser', 'bufferSeconds')
                sample = {k: data[k] for k in allow if k in data}
                sample['observed_at'] = time.time()
                player = self.server.playbacks.get(session_id)
                if player is None:
                    return self.json_response({'error': '재생 세션이 없습니다.'}, 404)
                with player.lock:
                    player.samples.append(sample)
                    player.samples = player.samples[-120:]
                    (player.runtime / 'browser-telemetry.json').write_text(json.dumps(player.samples, ensure_ascii=False, indent=2))
                return self.json_response({'ok': True})
            return self.send_error(404)
        except (ValueError, KeyError, OSError) as error:
            return self.json_response({'error': str(error)}, 400)


def main():
    p = argparse.ArgumentParser(description='SOOP 브라우저 재생 시제품')
    p.add_argument('url', nargs='?')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--open', action='store_true', help='Brave 재생 페이지 열기')
    p.add_argument('--runtime-dir')
    args = p.parse_args()
    if not shutil.which('ffmpeg'):
        p.error('ffmpeg가 필요합니다: sudo apt install ffmpeg')
    runtime = Path(args.runtime_dir).resolve() if args.runtime_dir else Path(tempfile.mkdtemp(prefix='soop-browser-'))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    playbacks = Playbacks(runtime)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.playbacks = playbacks
    server.token = secrets.token_hex(24)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'브레이브 재생 주소: {url}\n진단 폴더: {runtime}', flush=True)
    (runtime / 'server.json').write_text(json.dumps({'url': url, 'pid': __import__('os').getpid()}))
    if args.url:
        playbacks.start('local', args.url)
    def reap_loop():
        while True:
            time.sleep(10)
            playbacks.reap()
    threading.Thread(target=reap_loop, daemon=True).start()
    if args.open:
        browser = shutil.which('brave-browser')
        if not browser:
            p.error('브레이브를 찾지 못했습니다. brave-browser 설치를 확인해 주세요')
        subprocess.Popen([browser, '--new-window', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        playbacks.stop_all()
        server.server_close()


if __name__ == '__main__':
    main()
