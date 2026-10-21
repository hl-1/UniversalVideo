import asyncio
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import requests
from urllib3.response import HTTPResponse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import paths
from backend.modules.video import preview, preview_assets as assets, preview_streams as streams
from backend.modules.video import preview_sources as sources


class PreviewDiskUsageTests(unittest.TestCase):
    def test_renamed_fragment_does_not_fail_disk_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragment = root / 'segment.part'
            fragment.write_bytes(b'old')
            (root / 'preview.mp4').write_bytes(b'preview')
            (root / 'subdirectory').mkdir()
            original_stat = Path.stat

            def rename_before_stat(path, *args, **kwargs):
                if path == fragment:
                    path.unlink()
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, 'stat', rename_before_stat):
                self.assertEqual(assets._disk_usage(root), 7)
            self.assertEqual(assets._disk_usage(root / 'removed'), 0)

    def test_disk_accounting_does_not_hide_permission_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / 'private.part'
            item.write_bytes(b'private')
            original_stat = Path.stat

            def denied(path, *args, **kwargs):
                if path == item:
                    raise PermissionError('unreadable preview file')
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, 'stat', denied), self.assertRaises(PermissionError):
                assets._disk_usage(root)


class StreamingPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = paths.FFMPEG_DIR / ('ffmpeg.exe' if (paths.FFMPEG_DIR / 'ffmpeg.exe').exists() else 'ffmpeg')
        if not cls.ffmpeg.exists():
            raise unittest.SkipTest('FFmpeg unavailable')
        for name, audio in [('video', False), ('combined', True)]:
            command = [str(cls.ffmpeg), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=20:d=20']
            if audio:
                command += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=20']
            command += ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-g', '20']
            if audio:
                command += ['-c:a', 'aac']
            subprocess.run(command + [str(cls.root / f'{name}.mp4')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=20',
                        '-c:a', 'aac', str(cls.root / 'audio.mp4')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-i', str(cls.root / 'combined.mp4'), '-c', 'copy',
                        '-hls_time', '1', '-hls_list_size', '0', '-hls_segment_filename', str(cls.root / 'segment-%03d.ts'),
                        str(cls.root / 'playlist.m3u8')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-i', str(cls.root / 'combined.mp4'), '-c', 'copy',
                        '-seg_duration', '1', '-f', 'dash', str(cls.root / 'manifest.mpd')], cwd=cls.root, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(assets, 'ROOT', Path(self.output)))
        self.stack.enter_context(patch.object(assets, 'jobs', {}))
        self.stack.enter_context(patch.object(assets, '_start'))
        self.get = self.stack.enter_context(patch.object(streams.video_network, 'get', side_effect=self.range_get))
        self.requests, self.closed = [], []
        self.pause = False
        self.continue_feed = threading.Event()
        self.addCleanup(self.continue_feed.set)
        app = FastAPI()
        app.include_router(preview.router)
        self.client = TestClient(app)

    def range_get(self, url, **kwargs):
        data = (self.root / Path(url).name).read_bytes()
        value = kwargs['headers'].get('Range', f'bytes=0-{len(data)-1}')
        self.requests.append(value)
        parts = value[6:].split('-')
        start, end = int(parts[0]), min(int(parts[1] or len(data)-1), len(data)-1)
        owner = self

        class Response:
            status_code = 206
            headers = {'Content-Type': 'video/mp4', 'Content-Range': f'bytes {start}-{end}/{len(data)}',
                       'Content-Length': str(end-start+1), 'Accept-Ranges': 'bytes'}

            def iter_content(self, chunk_size):
                for pos in range(start, end + 1, chunk_size):
                    if owner.pause and end-start > 100000 and pos > len(data) * .7:
                        owner.continue_feed.wait(8)
                    yield data[pos:min(pos+chunk_size, end+1)]

            def close(self):
                owner.closed.append(value)
        return Response()

    def create(self, combined=False):
        result = assets.create('https://cdn.example.com/combined.mp4' if combined else 'https://cdn.example.com/video.mp4',
                               None if combined else 'https://cdn.example.com/audio.mp4', 'https://example.com/video/123', None)
        return assets.jobs[result['preview_id']]

    def test_single_compatible_mp4_reads_only_metadata_then_proxies_range(self):
        job = self.create(combined=True)
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'ready', job.value)
        self.assertEqual(job.value['delivery'], 'proxy')
        self.assertTrue(job.value['has_audio'])
        self.assertLess(job.received, (self.root / 'combined.mp4').stat().st_size // 2)
        self.assertLess(job.received, 512 * 1024)
        self.assertFalse((job.directory / 'preview.mp4').exists())
        response = self.client.get(f'/api/previews/{job.token}/content', headers={'Range': 'bytes=500-599'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, (self.root / 'combined.mp4').read_bytes()[500:600])
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(job.readers, 0)

    def test_tail_moov_pair_publishes_decodable_fragment_before_download_completes(self):
        job = self.create()
        self.pause = True
        worker = threading.Thread(target=assets._prepare, args=(job,), daemon=True)
        worker.start()
        deadline = time.monotonic() + 8
        while job.value['status'] == 'preparing' and time.monotonic() < deadline:
            time.sleep(.02)
        try:
            self.assertEqual(job.value['status'], 'ready', job.value)
            self.assertEqual(job.value['delivery'], 'fragmented')
            self.assertFalse(job.value['complete'])
            self.assertTrue(worker.is_alive())
            self.assertTrue(streams.first_fragment(job.directory / 'preview.mp4'))
        finally:
            self.continue_feed.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.value['status'], 'ready', job.value)
        self.assertTrue(job.value['complete'])
        decoded = subprocess.run([str(self.ffmpeg), '-v', 'error', '-i', str(job.directory / 'preview.mp4'),
                                  '-map', '0:a:0', '-f', 's16le', '-'], capture_output=True, check=True).stdout
        self.assertGreater(len(decoded), 10000)
        self.assertTrue(any(decoded))
        self.assertEqual(self.client.get(f'/api/previews/{job.token}/content', headers={'Range': 'bytes=0-31'}).status_code, 206)

    def test_source_without_range_falls_back_and_closes_metadata_response(self):
        job = self.create(combined=True)
        job.directory.mkdir()
        response = self.range_get('https://cdn.example.com/combined.mp4', headers={'Range': 'bytes=0-15'})
        response.status_code = 200
        with patch.object(streams.video_network, 'get', return_value=response):
            self.assertFalse(assets._try_stream(job))
        self.assertTrue(self.closed)

    def test_cancelling_stream_terminates_producer_and_removes_session(self):
        job = self.create()
        self.pause = True
        worker = threading.Thread(target=assets._prepare, args=(job,), daemon=True)
        worker.start()
        deadline = time.monotonic() + 6
        while job.value['status'] == 'preparing' and time.monotonic() < deadline:
            time.sleep(.02)
        assets.cancel(job.token)
        self.continue_feed.set()
        worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(job.process)

    def test_failed_consumer_stops_native_input_and_preserves_failure(self):
        response = assets.create('https://cdn.example.com/playlist.m3u8', None, 'https://example.com/video/123', None)
        job = assets.jobs[response['preview_id']]
        stopped = threading.Event()

        def download(job, url, target, check, receive, *, format_id, on_selected):
            on_selected([dict(path=target, done=stopped, error=None, video=True, audio=True, duration=20)])
            try:
                while not stopped.wait(.01):
                    check()
            finally:
                stopped.set()

        with patch.object(sources, 'download', side_effect=download), \
             patch.object(assets, '_run', side_effect=assets.PreparationError('audio_missing', '配套音频缺少有效音轨。')):
            assets._prepare(job)
        self.assertTrue(stopped.is_set())
        self.assertEqual(job.producers, 0)
        self.assertEqual(job.value['status'], 'failed')
        self.assertEqual(job.value['error_code'], 'audio_missing')
        self.assertFalse(job.cancelled.is_set())

    def native_transport(self, session, method, url, **kwargs):
        path = self.root / Path(urlparse(url).path).name
        data = path.read_bytes()
        if self.pause and (path.name == 'segment-010.ts' or '00011.m4s' in path.name):
            self.continue_feed.wait(8)
        response = requests.Response()
        response.status_code, response.url, response.reason = 200, url, 'OK'
        response.headers.update({'Content-Type': 'application/vnd.apple.mpegurl' if path.suffix == '.m3u8' else 'application/dash+xml' if path.suffix == '.mpd' else 'video/mp4',
                                 'Content-Length': str(len(data))})
        response.raw = HTTPResponse(body=io.BytesIO(data), headers=response.headers, preload_content=False)
        return response

    def test_hls_and_dash_play_before_all_segments_have_downloaded(self):
        for manifest in ('playlist.m3u8', 'manifest.mpd'):
            with self.subTest(manifest=manifest):
                self.continue_feed.clear()
                self.pause = True
                response = assets.create(f'https://cdn.example.com/{manifest}', None, 'https://example.com/video/123', None)
                job = assets.jobs[response['preview_id']]
                with patch.object(sources.video_network, 'validate'), \
                     patch('requests.Session.request', autospec=True, side_effect=self.native_transport):
                    worker = threading.Thread(target=assets._prepare, args=(job,), daemon=True)
                    worker.start()
                    deadline = time.monotonic() + 7
                    while job.value['status'] == 'preparing' and time.monotonic() < deadline:
                        time.sleep(.02)
                    try:
                        self.assertEqual(job.value['status'], 'ready', job.value)
                        self.assertEqual(job.value['delivery'], 'fragmented')
                        self.assertFalse(job.value['complete'])
                        self.assertTrue(job.value['has_audio'])
                        self.assertTrue(worker.is_alive())
                    finally:
                        self.continue_feed.set()
                        worker.join(12)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(job.value['status'], 'ready', job.value)
                    self.assertTrue(job.value['complete'])
                assets.cancel(job.token)
