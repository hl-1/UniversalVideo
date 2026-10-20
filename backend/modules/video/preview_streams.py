"""Bounded MP4 metadata reads and local pipe feeds for early preview playback."""
from __future__ import annotations

import errno
import os
import re
import struct
import threading
import time
from contextvars import copy_context
from pathlib import Path

from backend.infrastructure import video_network
from backend.modules.video import preview_sources as sources

MAX_METADATA = 4 * 1024 * 1024


class Unavailable(Exception):
    """The source needs the ordinary guarded download/conversion path."""


def atoms(data, start=0, end=None):
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise Unavailable()
            size, header = struct.unpack_from('>Q', data, start + 8)[0], 16
        if size == 0:
            size = end - start
        if size < header or start + size > end:
            raise Unavailable()
        yield start, size, kind, header
        start += size
    if start != end:
        raise Unavailable()


def relocate_moov(data, insertion, original):
    result = bytearray(data)
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}

    def walk(start, end, depth=0):
        if depth > 16:
            raise Unavailable()
        for pos, size, kind, header in atoms(result, start, end):
            if kind in containers:
                walk(pos + header, pos + size, depth + 1)
            elif kind in {b'stco', b'co64'}:
                width, fmt = (4, '>I') if kind == b'stco' else (8, '>Q')
                if size < header + 8:
                    raise Unavailable()
                count = struct.unpack_from('>I', result, pos + header + 4)[0]
                if header + 8 + count * width > size:
                    raise Unavailable()
                for offset in range(pos + header + 8, pos + header + 8 + count * width, width):
                    value = struct.unpack_from(fmt, result, offset)[0]
                    shifted = value + (len(data) if value >= insertion else 0) - (len(data) if value >= original + len(data) else 0)
                    if shifted >= 1 << (width * 8):
                        raise Unavailable()
                    struct.pack_into(fmt, result, offset, shifted)
    walk(0, len(result))
    return bytes(result)


class RemoteMP4:
    def __init__(self, job, url, check, receive):
        self.job, self.url, self.check, self.receive = job, url, check, receive
        self.total = None
        self.moov = self.ftyp = b''
        self.moov_offset = self.insertion = 0
        self.relocated = False

    def open(self, start, end):
        headers = {**sources.headers(self.job.value['referer']), 'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}
        response = sources.retry_network(lambda: video_network.get(self.url, referer=self.job.value['referer'],
            headers=headers, stream=True, timeout=(5, 5)), self.check, wait=self.job.cancelled.wait)
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
        if response.status_code != 206 or not match or int(match[1]) != start or int(match[2]) != min(end, int(match[3]) - 1):
            response.close()
            raise Unavailable()
        total = int(match[3])
        if (self.total is not None and total != self.total) or total > 1024 ** 3:
            response.close()
            raise Unavailable()
        self.total = total
        return response

    def chunks(self, start, end):
        if start > end:
            return
        response = self.open(start, end)
        received = 0
        try:
            for chunk in response.iter_content(65536):
                self.check()
                self.receive(len(chunk))
                received += len(chunk)
                if received > end - start + 1:
                    raise Unavailable()
                yield chunk
            if received != end - start + 1:
                raise sources.PreparationError('network_connection', '视频源连接中断，请重试播放。')
        finally:
            response.close()

    def read(self, start, length):
        if length > MAX_METADATA:
            raise Unavailable()
        return b''.join(self.chunks(start, start + length - 1))

    def inspect(self, target: Path):
        offset, mdat = 0, None
        for _ in range(32):
            header = self.read(offset, 16)
            if len(header) < 8:
                raise Unavailable()
            size, kind = struct.unpack_from('>I4s', header)
            if size == 1:
                size = struct.unpack_from('>Q', header, 8)[0]
            if size == 0:
                size = self.total - offset
            if size < 8 or offset + size > self.total:
                raise Unavailable()
            if kind == b'ftyp':
                self.ftyp = self.read(offset, size)
                self.insertion = offset + size
            elif kind == b'moov':
                self.moov, self.moov_offset = self.read(offset, size), offset
            elif kind == b'mdat':
                mdat = offset
            if self.ftyp and self.moov and mdat is not None:
                break
            offset += size
            if offset >= self.total:
                raise Unavailable()
        if not self.moov or not self.ftyp or mdat is None or b'encv' in self.moov or b'enca' in self.moov:
            raise Unavailable()
        self.relocated = self.moov_offset > mdat
        if self.relocated:
            self.moov = relocate_moov(self.moov, self.insertion, self.moov_offset)
        # A bounded first sample lets ffprobe confirm H.264 pixel depth as well.
        prefix, limit = bytearray(), len(self.moov) + 65536
        feed = self.feed(limit)
        try:
            for chunk in feed:
                prefix.extend(chunk[:max(0, limit - len(prefix))])
                if len(prefix) >= limit:
                    break
        finally:
            feed.close()
        target.write_bytes(prefix)

    def feed(self, limit=None):
        remaining = self.total if limit is None else min(limit, self.total)
        if not self.relocated:
            yield from self.chunks(0, remaining - 1)
            return
        sections = [(0, self.insertion - 1), self.moov, (self.insertion, self.moov_offset - 1),
                    (self.moov_offset + len(self.moov), self.total - 1)]
        for section in sections:
            if remaining <= 0:
                break
            if isinstance(section, bytes):
                chunk = section[:remaining]
                remaining -= len(chunk)
                yield chunk
            else:
                start, end = section
                end = min(end, start + remaining - 1)
                if end >= start:
                    yield from self.chunks(start, end)
                    remaining -= end - start + 1


class LocalPipe:
    """FFmpeg reads only local file/pipe inputs on both Windows and POSIX."""
    def __init__(self, directory: Path, name: str, check):
        self.check, self.handle = check, None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.ctypes, self.wintypes = ctypes, wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.CreateNamedPipeW.restype = wintypes.HANDLE
            self.kernel.CreateEventW.restype = wintypes.HANDLE
            self.path = rf'\\.\pipe\videodream-{name}'
            self.handle = self.kernel.CreateNamedPipeW(self.path, 2 | 0x40000000, 0, 1, 65536, 65536, 0, None)
            if self.handle in (None, ctypes.c_void_p(-1).value):
                raise OSError(ctypes.get_last_error(), 'CreateNamedPipe failed')
        else:
            self.path = str(directory / f'{name}.pipe')
            os.mkfifo(self.path, 0o600)

    def _windows_io(self, operation, data=None):
        c, w, k = self.ctypes, self.wintypes, self.kernel

        class Overlapped(c.Structure):
            _fields_ = [('internal', c.c_size_t), ('high', c.c_size_t), ('offset', w.DWORD), ('offset_high', w.DWORD), ('event', w.HANDLE)]

        event = k.CreateEventW(None, True, False, None)
        overlapped, count = Overlapped(event=event), w.DWORD()
        handle = w.HANDLE(self.handle)
        pending = False
        try:
            success = k.ConnectNamedPipe(handle, c.byref(overlapped)) if operation == 'connect' else k.WriteFile(handle, data, len(data), c.byref(count), c.byref(overlapped))
            error = c.get_last_error() if not success else 0
            if operation == 'connect' and error == 535:
                return 0
            if not success and error != 997:
                raise OSError(error, 'Local preview pipe failed')
            if not success:
                pending = True
                while k.WaitForSingleObject(w.HANDLE(event), 100) == 258:
                    self.check()
                if not k.GetOverlappedResult(handle, c.byref(overlapped), c.byref(count), False):
                    raise OSError(c.get_last_error(), 'Local preview pipe failed')
                pending = False
            return count.value
        finally:
            if pending:
                k.CancelIoEx(handle, c.byref(overlapped))
                k.WaitForSingleObject(w.HANDLE(event), 1000)
            k.CloseHandle(w.HANDLE(event))

    def send(self, chunks):
        try:
            if os.name == 'nt':
                self._windows_io('connect')
                for chunk in chunks:
                    self.check()
                    offset = 0
                    while offset < len(chunk):
                        offset += self._windows_io('write', chunk[offset:])
            else:
                while True:
                    self.check()
                    try:
                        self.handle = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                        break
                    except OSError as exc:
                        if exc.errno != errno.ENXIO:
                            raise
                        threading.Event().wait(0.05)
                for chunk in chunks:
                    offset = 0
                    while offset < len(chunk):
                        self.check()
                        try:
                            offset += os.write(self.handle, chunk[offset:])
                        except BlockingIOError:
                            threading.Event().wait(0.05)
        finally:
            self.close()

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if os.name == 'nt':
                self.kernel.CloseHandle(self.wintypes.HANDLE(handle))
            else:
                os.close(handle)
        if os.name != 'nt':
            Path(self.path).unlink(missing_ok=True)


def first_fragment(path):
    if not path.is_file():
        return False
    length, seen = path.stat().st_size, False
    with path.open('rb') as stream:
        for _ in range(16):
            data = stream.read(8)
            if len(data) != 8:
                return False
            size, kind = struct.unpack('>I4s', data)
            if size < 8 or stream.tell() + size - 8 > length:
                return False
            if kind == b'moof':
                seen = True
            if kind == b'mdat' and seen:
                return True
            stream.seek(size - 8, 1)
    return False


def start_feed(pipe, chunks, errors):
    def work():
        try:
            pipe.send(chunks)
        except Exception as exc:
            # A demuxer may stop after its declared samples, before trailing indexes.
            if not isinstance(exc, OSError) or exc.errno not in {errno.EPIPE, 109, 232}:
                errors.append(exc)
    thread = threading.Thread(target=copy_context().run, args=(work,), daemon=True, name='preview-input')
    thread.start()
    return thread


def follow(member, check):
    """Tail native downloader output without letting FFmpeg fetch remote URLs."""
    offset = 0
    while True:
        check()
        if member['error'] is not None:
            raise member['error']
        path = member['path']
        partial = path.with_name(path.name + '.part')
        active = partial if partial.is_file() else path
        try:
            # Close between chunks so Windows can rename .part to its final name.
            with active.open('rb') as stream:
                stream.seek(offset)
                chunk = stream.read(65536)
        except FileNotFoundError:
            if active != path and path.is_file():
                continue
            chunk = b''
        if chunk:
            offset += len(chunk)
            yield chunk
        elif member['done'].is_set():
            return
        else:
            time.sleep(.05)
