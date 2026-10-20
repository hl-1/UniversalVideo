import hashlib
import functools
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from unittest.mock import Mock, patch

import requests

from backend.infrastructure import network, video_network, video_transfer as transfer
from backend.modules.video import parsing, preview_sources, downloads, tasks
from backend.core import paths
from backend.infrastructure.runtime import executable_name


class RoutingTests(unittest.TestCase):
    def test_exact_platform_and_cdn_boundaries(self):
        for host in ('www.bilibili.com', 'b23.tv', 'v.douyinvod.com', 'v.iesdouyin.com',
                     'xhslink.com', 'sns-video.xhscdn.com', 'weibo.cn', 'f.video.weibocdn.com'):
            with self.subTest(host=host):
                self.assertTrue(video_network.domestic('https://' + host))
                self.assertEqual(parsing._ydl_base_options('https://' + host)['proxy'], '')
        for host in ('evilbilibili.com', 'bilibili.com.evil.test', 'example.com'):
            self.assertFalse(video_network.domestic('https://' + host))
            self.assertNotIn('proxy', parsing._ydl_base_options('https://' + host))
        self.assertTrue(video_network.domestic('https://shared.example/media', 'https://weibo.com/123'))

    def test_domestic_session_bypasses_registry_and_environment_proxy(self):
        session = Mock()
        response = Mock(status_code=200)
        session.get.return_value = response
        with patch.object(video_network.requests, 'Session', return_value=session), \
             patch.object(network, 'public_url', side_effect=lambda url: url):
            video_network.get('https://cdn.example/file', referer='https://www.douyin.com/video/123').close()
        self.assertIs(session.trust_env, False)
        session.close.assert_called_once()

    def test_overseas_session_uses_explicit_proxy(self):
        with patch.object(network, 'public_get') as get, patch.object(video_network.requests, 'Session') as session:
            video_network.get('https://www.youtube.com/watch?v=test')
        get.assert_called_once()
        session.assert_called_once()
        self.assertFalse(session.return_value.trust_env)
        session.return_value.proxies.update.assert_called_once_with(http="http://127.0.0.1:7890", https="http://127.0.0.1:7890")
        with preview_sources.PreviewYoutubeDL({'proxy': '', 'quiet': True}, lambda: None, lambda _: None) as ydl:
            self.assertFalse(ydl.session.trust_env)

    def test_redirect_validates_destination_and_strips_credentials(self):
        session = Mock()
        first = Mock(status_code=302, headers={'Location': 'https://other.example/file'})
        last = Mock(status_code=200)
        session.get.side_effect = [first, last]
        with patch.object(network, 'public_url', side_effect=lambda url: url) as validate:
            network.public_get('https://www.bilibili.com/file', session=session,
                               headers={'Cookie': 'secret', 'Authorization': 'secret', 'Referer': 'page'})
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs['headers'], {'Referer': 'page'})

    def test_domestic_video_and_audio_tasks_use_accelerated_entry(self):
        for media in ('video', 'audio'):
            task = tasks.DownloadTask('unit', 'https://weibo.com/123', None, media_type=media)
            with patch.object(downloads, '_run_domestic_download') as domestic:
                downloads._run_download(task)
            domestic.assert_called_once_with(task)

    def test_exception_for_bilibili_media_ports_remains_domain_and_protocol_scoped(self):
        with patch.object(network, 'public_url') as validate:
            video_network.validate('https://peer.mcdn.bilivideo.cn:8082/media')
            self.assertIn(8082, validate.call_args.kwargs['allowed_ports'])
            for url in ('https://mcdn.bilivideo.cn.evil.example:8082/media', 'http://peer.mcdn.bilivideo.cn:8082/media'):
                video_network.validate(url)
                self.assertEqual(validate.call_args.kwargs, {})


class NativeDownloadTests(unittest.TestCase):
    def test_sniffed_hls_uses_native_selection_and_locally_muxes_audio(self):
        ffmpeg, ffprobe = (paths.FFMPEG_DIR / executable_name(name) for name in ('ffmpeg', 'ffprobe'))
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest('FFmpeg is not installed')
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            hls, output = work / 'hls', work / 'output'
            hls.mkdir()
            output.mkdir()
            subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=128x72:rate=12:duration=4',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            '-g', '12', '-c:a', 'aac', '-hls_time', '1', '-hls_list_size', '0', str(hls / 'index.m3u8')],
                           capture_output=True, check=True)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(hls)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/index.m3u8'
                task = tasks.DownloadTask('native-hls', 'https://weibo.com/video/fixture', 'sniff:' + quote(url, safe=''))
                with patch.object(network, 'public_url', side_effect=lambda url, **kwargs: url), \
                     patch.object(paths, 'DOWNLOAD_DIR', output):
                    downloads._run_download(task)
                self.assertEqual(task.status, 'finished', task.error)
                result = subprocess.run([str(ffprobe), '-v', 'error', '-show_entries', 'stream=codec_type:format=duration,format_name',
                                         '-of', 'json', str(output / task.filename)], capture_output=True, text=True, check=True)
                info = json.loads(result.stdout)
                self.assertEqual({stream['codec_type'] for stream in info['streams']}, {'video', 'audio'})
                self.assertIn('mp4', info['format']['format_name'])
                self.assertAlmostEqual(float(info['format']['duration']), 4, delta=0.2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.data = bytes(range(256)) * 8192
        self.etag = '"version-1"'
        self.mode = 'normal'
        self.requests = []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.delay = 0.001
        self.failed = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range', ''))
        start, end = map(int, match.groups()) if match else (0, len(server.data) - 1)
        probe = match and start == end == 0
        with server.lock:
            server.requests.append((start, end, bool(match), self.headers.get('If-Range')))
            server.active += 1
            server.peak = max(server.peak, server.active)
        try:
            ignore = server.mode in {'ignore', 'single_fail'}
            if ignore:
                start, end = 0, len(server.data) - 1
            self.send_response(206 if match and not ignore else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('ETag', '"changed"' if server.mode == 'change' and not probe else server.etag)
            if match and not ignore:
                self.send_header('Content-Range', f'bytes {start + (1 if server.mode == "wrong" and not probe else 0)}-{end}/{len(server.data)}')
            self.end_headers()
            data = server.data[start:end + 1]
            if server.mode == 'single_fail':
                data = data[:transfer.CHUNK]
                self.close_connection = True
            if server.mode in {'fail', 'retry'} and not probe:
                if server.mode == 'fail':
                    data = data[:transfer.CHUNK] if start % (len(server.data) // 4) == 0 else b''
                elif start not in server.failed:
                    server.failed.add(start)
                    data = data[:transfer.CHUNK]
                if len(data) < end - start + 1:
                    self.close_connection = True
            for offset in range(0, len(data), 32 * 1024):
                self.wfile.write(data[offset:offset + 32 * 1024])
                self.wfile.flush()
                time.sleep(server.delay if not probe else 0)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            with server.lock:
                server.active -= 1


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.server = Fixture()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.root = self.work / 'parts'
        self.url = f'http://127.0.0.1:{self.server.server_port}/file'
        # Loopback is allowed ONLY for this controlled fixture.
        self.guard = patch.object(network, 'public_url', side_effect=lambda url: url)
        self.guard.start()
        self.threshold = patch.object(transfer, 'THRESHOLD', 512 * 1024)
        self.threshold.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.guard.stop()
        self.threshold.stop()

    def download(self, **kwargs):
        return transfer.download(self.url, self.work / 'video.mp4', referer='https://www.bilibili.com/video/test',
                                 headers={}, root=self.root, owner=7, key='video:720p', task_id='task', **kwargs)

    def assertComplete(self, target):
        self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), hashlib.sha256(self.server.data).digest())

    def test_parallel_integrity_and_connection_ceiling(self):
        reports = []
        self.assertComplete(self.download(progress=reports.append))
        self.assertGreater(self.server.peak, 1)
        self.assertLessEqual(self.server.peak, 4)
        self.assertEqual(reports[-1]['downloaded_bytes'], len(self.server.data))
        self.assertGreater(reports[-1]['speed'], 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsupported_and_malformed_ranges_fall_back_to_complete_get(self):
        for mode in ('ignore', 'wrong'):
            with self.subTest(mode=mode):
                self.server.mode = mode
                self.assertComplete(self.download())
                self.assertFalse(self.server.requests[-1][2])

    def test_truncated_ranges_retry_from_validated_offset(self):
        self.server.mode = 'retry'
        self.assertComplete(self.download())
        self.assertTrue(any(start == transfer.CHUNK for start, end, ranged, validator in self.server.requests))

    def test_failed_parts_resume_and_metadata_contains_no_urls_or_cookies(self):
        self.server.mode = 'fail'
        with self.assertRaises(requests.RequestException):
            self.download()
        state = next(self.root.glob('*/state.json'))
        text = state.read_text()
        self.assertNotIn('http', text)
        self.assertNotIn('Cookie', text)
        self.assertEqual(json.loads(text)['owner'], 7)
        self.server.mode = 'normal'
        self.server.requests.clear()
        self.assertComplete(self.download())
        self.assertFalse(any(start == 0 and end > 0 for start, end, ranged, validator in self.server.requests))

    def test_changed_validator_invalidates_retained_parts(self):
        self.server.mode = 'fail'
        with self.assertRaises(requests.RequestException):
            self.download()
        self.server.mode, self.server.etag = 'normal', '"version-2"'
        self.server.data = b'Z' * len(self.server.data)
        self.server.requests.clear()
        self.assertComplete(self.download())
        self.assertTrue(any(start == 0 and end > 0 for start, end, ranged, validator in self.server.requests))

    def test_mid_transfer_identity_change_is_never_assembled(self):
        self.server.mode = 'change'
        with self.assertRaises(transfer.SourceChanged):
            self.download()
        self.assertFalse((self.work / 'video.mp4').exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_cleans_partial_state(self):
        cancel = threading.Event()
        def report(data):
            cancel.set()
        with self.assertRaisesRegex(RuntimeError, '停止'):
            self.download(cancel=cancel, progress=report)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_owner_scoped_cleanup_and_expiry(self):
        self.server.mode = 'fail'
        with self.assertRaises(requests.RequestException):
            self.download()
        transfer.cleanup(self.root, owner=8, task_id='task')
        self.assertEqual(len(list(self.root.iterdir())), 1)
        transfer.cleanup(self.root, owner=7, task_id='task')
        self.assertEqual(list(self.root.iterdir()), [])
        expired = self.root / 'expired'
        expired.mkdir()
        (expired / 'state.json').write_text(json.dumps({'updated': time.time() - transfer.TTL - 1}))
        transfer.cleanup(self.root)
        self.assertFalse(expired.exists())

    def test_small_file_uses_one_connection(self):
        self.server.data = self.server.data[:128 * 1024]
        self.assertComplete(self.download())
        self.assertEqual(self.server.peak, 1)

    def test_failed_non_resumable_get_does_not_retain_useless_parts(self):
        self.server.mode = 'single_fail'
        with self.assertRaises(requests.RequestException):
            self.download()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.work / 'video.mp4').exists())
