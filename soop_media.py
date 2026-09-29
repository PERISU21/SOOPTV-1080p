"""SOOP cache packets to FLV, preserving decode/composition/audio timestamps.

Packet layout: user's Android capture, corroborated with SOOP's public ALS parser.
No transcoding: H.264 Annex B and AAC ADTS are repackaged for local playback.
"""
import re
import struct


def packet(command, body=b''):
    return struct.pack('<HHIII', 0x202, command, len(body), 0x202 ^ command ^ len(body), 0) + body


def parse_header(header):
    version, command, length, checksum, _ = struct.unpack('<HHIII', header)
    if version != 0x202 or checksum != version ^ command ^ length or length > 8 * 1024 * 1024:
        raise ValueError('지원하지 않거나 손상된 SOOP 패킷 헤더')
    return command, length


class FLVMuxer:
    SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)

    def __init__(self, output):
        self.output = output
        self.origin = None
        self.sps = self.pps = self.avcc = self.asc = None
        self.video_frames = self.audio_frames = 0
        self.duplicates = 0
        self.last_sequence = {'video': -1, 'audio': -1}
        self.output.write(b'FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00')

    def tag(self, kind, timestamp, payload):
        timestamp = max(0, int(round(timestamp)))
        if timestamp >= 2**32:
            raise ValueError('FLV timestamp overflow')
        header = bytes([kind]) + len(payload).to_bytes(3, 'big') + (timestamp & 0xffffff).to_bytes(3, 'big') + bytes([timestamp >> 24]) + b'\0\0\0'
        self.output.write(header + payload + (11 + len(payload)).to_bytes(4, 'big'))

    def feed(self, body):
        if len(body) < 69:
            raise ValueError('미디어 헤더가 잘렸습니다')
        version, header_size, extension_size, raw_size = struct.unpack_from('<HHII', body)
        if version != 1:
            raise ValueError(f'지원하지 않는 미디어 버전: {version}')
        start = header_size + extension_size
        if header_size < 69 or start + raw_size > len(body):
            raise ValueError('미디어 길이가 유효하지 않습니다')
        kind = body[40]
        stream = 'video' if kind in (73, 80) else 'audio' if kind == 65 else None
        if stream is None:
            return
        sequence = struct.unpack_from('<Q', body, 20)[0]
        if sequence <= self.last_sequence[stream]:
            self.duplicates += 1
            return
        self.last_sequence[stream] = sequence
        dts = struct.unpack_from('<Q', body, 41)[0]
        cts = struct.unpack_from('<i', body, 57)[0]
        if self.origin is None:
            # Audio arrives in 512 ms bundles, sometimes preceding the first video DTS.
            self.origin = dts - 10_000_000
        timestamp = (dts - self.origin) / 10_000
        raw = body[start:start + raw_size]
        if kind in (73, 80):
            self.video(timestamp, cts / 10_000, raw)
        elif kind == 65:
            self.audio(timestamp, raw)

    def video(self, timestamp, composition, raw):
        nals = [x for x in re.split(b'\x00\x00(?:\x00)?\x01', raw) if x]
        for nal in nals:
            if nal[0] & 31 == 7:
                self.sps = nal
            elif nal[0] & 31 == 8:
                self.pps = nal
        if not self.sps or not self.pps:
            return
        if len(self.sps) < 4:
            raise ValueError('Invalid H.264 SPS')
        avcc = b'\x01' + self.sps[1:4] + b'\xff\xe1' + len(self.sps).to_bytes(2, 'big') + self.sps + b'\x01' + len(self.pps).to_bytes(2, 'big') + self.pps
        if avcc != self.avcc:
            self.tag(9, timestamp, b'\x17\x00\0\0\0' + avcc)
            self.avcc = avcc
        keyframe = any(nal[0] & 31 == 5 for nal in nals)
        data = b''.join(len(nal).to_bytes(4, 'big') + nal for nal in nals if nal[0] & 31 not in (7, 8))
        composition = int(round(composition))
        if not -(2**23) <= composition < 2**23:
            raise ValueError('Invalid H.264 composition offset')
        self.tag(9, timestamp, bytes([0x17 if keyframe else 0x27, 1]) + (composition & 0xffffff).to_bytes(3, 'big') + data)
        self.video_frames += 1

    def audio(self, timestamp, raw):
        offset = 0
        elapsed = 0
        while offset < len(raw):
            if offset + 7 > len(raw) or raw[offset] != 255 or raw[offset + 1] & 0xf6 != 0xf0:
                raise ValueError('AAC ADTS 헤더가 유효하지 않습니다')
            h = raw[offset:offset + 7]
            profile = (h[2] >> 6) + 1
            rate_index = (h[2] >> 2) & 15
            channels = ((h[2] & 1) << 2) | (h[3] >> 6)
            if rate_index >= len(self.SAMPLE_RATES):
                raise ValueError('AAC sample rate is reserved')
            rate = self.SAMPLE_RATES[rate_index]
            length = ((h[3] & 3) << 11) | (h[4] << 3) | (h[5] >> 5)
            header_size = 7 if h[1] & 1 else 9
            if length < header_size or offset + length > len(raw) or h[6] & 3:
                raise ValueError('지원하지 않거나 잘린 AAC 프레임')
            asc = ((profile << 11) | (rate_index << 7) | (channels << 3)).to_bytes(2, 'big')
            if self.asc != asc:
                self.tag(8, timestamp + elapsed, b'\xaf\x00' + asc)
                self.asc = asc
            self.tag(8, timestamp + elapsed, b'\xaf\x01' + raw[offset + header_size:offset + length])
            self.audio_frames += 1
            elapsed += 1024 * 1000 / rate
            offset += length
