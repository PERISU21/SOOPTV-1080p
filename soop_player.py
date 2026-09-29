#!/usr/bin/env python3
"""Ubuntu SOOP public-live player. Python standard library + mpv.

Usage: python3 soop_player.py https://play.sooplive.com/channel/broadcast
"""
import argparse
import collections
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from soop_media import FLVMuxer, packet, parse_header


def log(message):
    print(message, flush=True, file=sys.stderr)


def http(url, data=None, referer=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None,
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer or 'https://play.sooplive.com/', 'Origin': 'https://play.sooplive.com'})
    with urllib.request.urlopen(req, timeout=15) as response:
        content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError('서버 응답이 예상보다 큽니다')
        return content.decode('utf-8')


def resolve(url):
    match = re.fullmatch(r'https?://play\.(?:sooplive\.com|sooplive\.co\.kr|afreecatv\.com)/([A-Za-z0-9_]+)(?:/(\d+))?/?', url)
    if not match:
        raise ValueError('SOOP 생방송 URL을 입력해 주세요')
    channel, broadcast = match.groups()
    if not broadcast:
        page = http(f'https://play.sooplive.com/{channel}')
        number = re.search(r'nBroadNo\s*=\s*["\']?(\d+)', page)
        if not number:
            raise ValueError('현재 방송번호를 찾지 못했습니다. 생방송의 전체 주소를 넣어 주세요')
        broadcast = number[1]
    canonical = f'https://play.sooplive.com/{channel}/{broadcast}'
    info = json.loads(http('https://live.sooplive.com/afreeca/player_live_api.php',
        {'bid': channel, 'bno': broadcast, 'type': 'live', 'pwd': '', 'from_api': '0', 'mode': 'landing', 'player_type': 'html5', 'stream_type': 'common'}, canonical)).get('CHANNEL', {})
    if int(info.get('RESULT', 0)) != 1:
        raise ValueError(f'공개 방송 정보 조회 실패: RESULT={info.get("RESULT")} (방송 종료/로그인 필요 여부 확인)')
    if info.get('BPWD') == 'Y':
        raise ValueError('이 시제품은 비밀번호 방송을 지원하지 않습니다')
    broadcast = int(info['BNO'])
    query = urllib.parse.urlencode({'bj': channel, 'bno': broadcast, 'stype': 'org'})
    assignment = urllib.parse.parse_qs(http(f'http://affslb.sooplive.com:10035/getfc?{query}'))
    if assignment.get('err_code') != ['0']:
        raise ValueError(f'미디어 서버 할당 실패: {assignment.get("err_code")}')
    host = str(ipaddress.ip_address(assignment['fc_ip'][0]))
    port = int(assignment['fc_port'][0])
    if not ipaddress.ip_address(host).is_global or not 1 <= port <= 65535:
        raise ValueError('미디어 서버 주소가 유효하지 않습니다')
    return broadcast, host, port, info.get('BJNICK', channel)


class Reader:
    def __init__(self, sock):
        self.sock = sock
        self.received = self.sent = 0
        self.pending = bytearray()

    def send(self, command, body):
        data = packet(command, body)
        self.sock.sendall(data)
        self.sent += len(data)

    def next_packet(self):
        """Return a complete packet or None after a short idle interval.

        Keep partial TCP payloads across timeouts so control requests can be sent
        even while the server is silent.
        """
        if len(self.pending) >= 16:
            command, length = parse_header(self.pending[:16])
            if len(self.pending) >= 16 + length:
                body = bytes(self.pending[16:16 + length])
                del self.pending[:16 + length]
                return command, body
        try:
            chunk = self.sock.recv(256 * 1024)
        except socket.timeout:
            return None
        if not chunk:
            raise EOFError('미디어 서버가 연결을 종료했습니다')
        self.received += len(chunk)
        self.pending.extend(chunk)
        return self.next_packet()


class Tee:
    def __init__(self, *outputs):
        self.outputs = [o for o in outputs if o is not None]

    def write(self, data):
        for output in self.outputs:
            remaining = memoryview(data)
            while remaining:
                size = output.write(remaining)
                if not size:
                    raise BrokenPipeError('플레이어가 입력을 닫았습니다')
                remaining = remaining[size:]


def mpv_properties(path):
    names = ['width', 'height', 'time-pos', 'pause', 'eof-reached', 'audio-pts', 'estimated-vf-fps', 'decoder-frame-drop-count', 'frame-drop-count', 'avsync', 'video-codec', 'audio-codec-name']
    result = {}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ipc:
            ipc.settimeout(0.3)
            ipc.connect(str(path))
            for i, name in enumerate(names):
                ipc.sendall((json.dumps({'command': ['get_property', name], 'request_id': i}) + '\n').encode())
            f = ipc.makefile('rb')
            for _ in range(80):
                line = f.readline()
                if not line:
                    break
                item = json.loads(line)
                i = item.get('request_id')
                if isinstance(i, int) and 0 <= i < len(names):
                    result[names[i]] = item.get('data')
                    if len(result) == len(names):
                        break
    except (OSError, ValueError):
        pass
    return result


def receive(args, report_dir):
    broadcast, host, port, nickname = resolve(args.url)
    # These are quality bit flags. 10 (2 | 8) requested 720p and 360p together,
    # causing mid-stream H.264 resolution changes that break browser MSE playback.
    quality_code = {'1080p': 1, '720p': 2}[args.quality]
    log(f'방송 {broadcast} ({nickname}): 현재 서버 할당 완료')
    start = time.monotonic()
    counts = collections.Counter()
    observations = []
    player = record = player_log = None
    reader = mux = None
    try:
        if args.record:
            # Refuse to overwrite an existing recording.
            record = open(args.record, 'xb')
        if not args.no_player:
            player_log = open(report_dir / 'mpv.log', 'wb')
            player = subprocess.Popen(['mpv', '--no-config', '--cache=yes', '--cache-secs=3', '--demuxer-readahead-secs=2',
                '--demuxer-max-bytes=64MiB', '--force-window=yes', '--keep-open=no', '--hwdec=auto-safe',
                '--terminal=no', '--title=SOOP Ubuntu 1080p', f'--input-ipc-server={report_dir / "mpv.sock"}', '-'],
                stdin=subprocess.PIPE, stdout=player_log, stderr=subprocess.STDOUT, bufsize=0)
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(1)
            reader = Reader(sock)
            mux = FLVMuxer(Tee(player.stdin if player else None, record, sys.stdout.buffer if args.flv_stdout else None))
            reader.send(0xcb2e, struct.pack('<III', broadcast, 0, quality_code))
            reader.send(0xcb2f, struct.pack('<I', 1))
            reader.send(0xcb30, struct.pack('<IIII', quality_code, 0, 0, 2))
            log('미디어 수신 시작. 플레이어 창을 닫거나 터미널에서 Ctrl+C를 누르면 종료합니다.')
            checkpoint = start
            refresh = start
            last_media = start
            next_frame = 0
            while not args.seconds or time.monotonic() - start < args.seconds:
                if player and player.poll() is not None:
                    if player.returncode:
                        raise RuntimeError(f'mpv 종료 코드 {player.returncode}; {report_dir / "mpv.log"} 확인')
                    break
                now = time.monotonic()
                if now - refresh >= 4 and next_frame:
                    # Ask slightly behind the live edge. Requesting an unseen future
                    # frame can leave this cache connection idle indefinitely.
                    reader.send(0xcb30, struct.pack('<IQI', quality_code, max(0, next_frame - 30), 6))
                    refresh = now
                item = reader.next_packet()
                if item:
                    command, body = item
                    counts[f'0x{command:04x}'] += 1
                    if command == 0xc748:
                        mux.feed(body)
                        next_frame = max(next_frame, struct.unpack_from('<Q', body, 12)[0] + 1)
                        last_media = time.monotonic()
                    elif command == 0x1bc4:
                        reader.send(command, body)
                now = time.monotonic()
                if now - last_media > 16:
                    raise TimeoutError('영상 서버가 16초 동안 새 프레임을 보내지 않았습니다')
                if now - checkpoint >= 5:
                    stats = mpv_properties(report_dir / 'mpv.sock') if player else {}
                    stats['elapsed'] = round(now - start, 2)
                    observations.append(stats)
                    resolution = f'{stats.get("width", "?")}×{stats.get("height", "?")}'
                    log(f'{now-start:.0f}초 | 디코더 {resolution} | 수신 {reader.received/1048576:.1f} MiB | 영상 {mux.video_frames} 프레임')
                    checkpoint = now
    finally:
        if player:
            if player.stdin:
                try:
                    player.stdin.close()
                except OSError:
                    pass
            try:
                player.wait(timeout=6)
            except subprocess.TimeoutExpired:
                player.terminate()
                try:
                    player.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    player.kill(); player.wait()
        if player_log:
            player_log.close()
        if record:
            record.close()
        summary = {'broadcast': broadcast, 'quality': args.quality, 'elapsed_seconds': round(time.monotonic()-start, 3),
            'received_application_bytes': reader.received if reader else 0,
            'sent_application_bytes': reader.sent if reader else 0,
            'video_frames': mux.video_frames if mux else 0,
            'audio_frames': mux.audio_frames if mux else 0,
            'duplicate_packets_skipped': mux.duplicates if mux else 0,
            'commands': dict(counts), 'playback': observations}
        (report_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        log(f'실행 기록: {report_dir / "summary.json"}')


def main():
    p = argparse.ArgumentParser(description='SOOP 공개 생방송 네이티브 재생 시제품')
    p.add_argument('url', nargs='?', help='SOOP 생방송 URL')
    p.add_argument('--seconds', type=int, default=0, help='제한 시간; 기본은 창을 닫을 때까지')
    p.add_argument('--quality', choices=('1080p', '720p'), default='1080p', help='SOOP 원본 고화질 선택')
    p.add_argument('--record', help='동일 화질 FLV 저장 경로 (기존 파일 덮어쓰기 금지)')
    p.add_argument('--no-player', action='store_true', help='화면 없이 수신/기록')
    p.add_argument('--flv-stdout', action='store_true', help='FLV를 stdout으로 출력 (진단은 stderr)')
    p.add_argument('--report-dir', help='진단 기록 폴더')
    args = p.parse_args()
    if not args.url:
        args.url = input('SOOP 생방송 URL: ').strip()
    if args.seconds < 0:
        p.error('--seconds는 0 이상이어야 합니다')
    if not args.no_player and not shutil.which('mpv'):
        p.error('mpv가 필요합니다: sudo apt install mpv')
    report_dir = Path(args.report_dir) if args.report_dir else Path(tempfile.mkdtemp(prefix='soop-player-'))
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        receive(args, report_dir)
    except KeyboardInterrupt:
        log('종료했습니다.')
    except (OSError, EOFError, ValueError, KeyError, RuntimeError) as error:
        log(f'재생 실패: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
