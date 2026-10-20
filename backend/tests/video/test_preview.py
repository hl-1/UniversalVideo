import asyncio
import unittest
import time
import shutil
import subprocess
import tempfile
import io
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

import requests
from urllib3.response import HTTPResponse
from fastapi import HTTPException

from backend.core import paths
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import preview as video_preview
from backend.modules.video import preview_assets as assets
from backend.modules.video import preview_sources as sources
from backend.modules.video import schemas as video_schemas
from backend.modules.video import tasks as video_tasks
from backend.modules.video import urls as video_urls
from backend.modules.accounts import service as accounts
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeUpstreamResponse:
    def __init__(self, status_code=206, content_type="video/mp4", body=b"test"):
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body) * 2}",
            "Accept-Ranges": "bytes",
        }
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        yield self.body

    def close(self):
        self.closed = True


class PreviewSessionTests(unittest.TestCase):
    def setUp(self):
        with video_parse_cache.sniff_cache_lock:
            video_parse_cache.sniff_cache.clear()
        if hasattr(video_preview, "preview_sessions_lock"):
            with video_preview.preview_sessions_lock:
                video_preview.preview_sessions.clear()

    def _cache_format(self, page_url: str, media_url: str, ext: str = "mp4") -> str:
        format_id = f"sniff:{quote(media_url, safe='')}"
        video_parse_cache._set_sniff_cache(
            video_urls.normalize_url(page_url),
            video_schemas.ParseResponse(
                title="抖音预览测试",
                webpage_url=page_url,
                formats=[
                    video_schemas.FormatInfo(
                        format_id=format_id,
                        label=f"{ext.upper()} · 高清线路",
                        ext=ext,
                        resolution="自动",
                    )
                ],
            ),
        )
        return format_id

    def test_create_preview_accepts_cached_douyin_mp4(self):
        page_url = "https://www.douyin.com/video/123456789"
        media_url = "https://v3-dy-o-abtest.zjcdn.com/video/tos/test.mp4"
        format_id = self._cache_format(page_url, media_url)

        with patch.object(assets, 'create', return_value=dict(preview_url=None, status='preparing', status_url='/api/previews/test-token', expires_in=600)) as prepare:
            response = video_preview.create_preview(video_schemas.PreviewRequest(url=page_url, format_id=format_id))

        self.assertIsNone(response.preview_url)
        self.assertEqual(response.status, 'preparing')
        prepare.assert_called_once_with(media_url, None, page_url, None, False, format_id=None)
        self.assertEqual(response.expires_in, 600)

    def test_preview_accepts_cached_sources_without_a_platform_whitelist(self):
        for page in ('https://example.com/video/123', 'https://www.youtube.com/watch?v=123',
                     'https://www.bilibili.com/video/BV123', 'https://cn.pornhub.com/view_video.php?viewkey=test'):
            identifier = self._cache_format(page, 'https://cdn.example.com/video.webm', 'webm')
            self.assertEqual(sources.resolve(page, identifier).media_url, 'https://cdn.example.com/video.webm')

    def test_complete_pair_is_accepted_and_preferred_for_an_ordinary_line(self):
        page = 'https://www.douyin.com/video/123456789'
        video, audio = 'https://v3.douyinvod.com/video.mp4', 'https://v3.douyinvod.com/media-audio-test.mp4'
        single = self._cache_format(page, video)
        cached = video_parse_cache._get_sniff_cache(page)
        pair = 'sniffpair:' + quote(video, safe='') + '|' + quote(audio, safe='')
        cached.formats.append(video_schemas.FormatInfo(format_id=pair, label='MP4 · 完整音视频', ext='mp4'))
        self.assertEqual(sources.resolve(page, pair), sources.PreviewSource(video, audio, page, False))
        self.assertEqual(sources.resolve(page, single), sources.PreviewSource(video, audio, page, True))

    def test_hls_mislabeled_as_mp4_uses_the_shared_source_path(self):
        page = 'https://www.douyin.com/video/123456789'
        identifier = self._cache_format(page, 'https://v3.douyinvod.com/playlist.m3u8')
        self.assertTrue(sources.is_manifest(sources.resolve(page, identifier).media_url))

    def test_rejects_uncached_pair(self):
        with self.assertRaises(HTTPException) as caught:
            sources.resolve('https://www.douyin.com/video/123456789', 'sniffpair:video|audio')
        self.assertEqual(caught.exception.status_code, 404)

    def test_create_preview_rejects_uncached_sniff_format(self):
        request = video_schemas.PreviewRequest(
            url="https://www.douyin.com/video/123456789",
            format_id="sniff:https%3A%2F%2Fv3-dy-o-abtest.zjcdn.com%2Fvideo%2Funknown.mp4",
        )

        with self.assertRaises(HTTPException) as raised:
            video_preview.create_preview(request)

        self.assertEqual(raised.exception.status_code, 404)

    def test_create_preview_accepts_hls_format(self):
        page_url = "https://www.douyin.com/video/123456789"
        media_url = "https://v3-dy-o-abtest.zjcdn.com/video/tos/test.m3u8"
        format_id = self._cache_format(page_url, media_url, ext="m3u8")

        with patch.object(assets, 'create', return_value=dict(status='preparing', expires_in=600)) as prepare:
            video_preview.create_preview(video_schemas.PreviewRequest(url=page_url, format_id=format_id))
        self.assertEqual(prepare.call_args.args[0], media_url)

    def test_ordinary_format_ids_are_validated_before_background_resolution(self):
        page = 'https://www.bilibili.com/video/BV123'
        result = video_schemas.ParseResponse(title='test', webpage_url=page, formats=[
            video_schemas.FormatInfo(format_id='80+bestaudio/best', label='1080p', ext='mp4', has_video=True),
            video_schemas.FormatInfo(format_id='audio', label='音频', ext='webm', has_video=False)])
        video_parse_cache._set_sniff_cache(page, result)
        self.assertEqual(sources.resolve(page, '80+bestaudio/best').format_id, '80+bestaudio/best')
        for identifier, status in [('forged', 404), ('audio', 422)]:
            with self.assertRaises(HTTPException) as caught:
                sources.resolve(page, identifier)
            self.assertEqual(caught.exception.status_code, status)

    def test_headers_follow_the_page_platform(self):
        self.assertEqual(sources.headers('https://www.bilibili.com/video/BV123')['Origin'], 'https://www.bilibili.com')
        self.assertEqual(sources.headers('https://www.youtube.com/watch?v=123')['Origin'], 'https://www.youtube.com')
        self.assertNotIn('douyin.com', str(sources.headers('https://example.com/video/123')))


class PreviewContentTests(unittest.TestCase):
    def setUp(self):
        with video_preview.preview_sessions_lock:
            video_preview.preview_sessions.clear()

    def _store_session(self, token="preview-token", expires_at=None):
        with video_preview.preview_sessions_lock:
            video_preview.preview_sessions[token] = video_preview.PreviewSession(
                media_url="https://v3-dy-o-abtest.zjcdn.com/video/tos/test.mp4",
                referer="https://www.douyin.com/video/123456789",
                expires_at=expires_at if expires_at is not None else time.time() + 60,
            )
        return token

    def test_preview_content_forwards_range_and_streams_partial_video(self):
        token = self._store_session()
        upstream = FakeUpstreamResponse()

        with patch.object(video_preview.video_network, "get", return_value=upstream) as get:
            response = video_preview.preview_content(token, range_header="bytes=0-3")

        async def read_body():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-3/8")
        self.assertEqual(asyncio.run(read_body()), b"test")
        self.assertTrue(upstream.closed)
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=0-3")

    def test_preview_content_rejects_unknown_token(self):
        with self.assertRaises(HTTPException) as raised:
            video_preview.preview_content("missing-token")

        self.assertEqual(raised.exception.status_code, 404)

    def test_preview_content_rejects_expired_token(self):
        token = self._store_session(expires_at=time.time() - 1)

        with self.assertRaises(HTTPException) as raised:
            video_preview.preview_content(token)

        self.assertEqual(raised.exception.status_code, 410)

    def test_preview_content_rejects_multiple_ranges(self):
        token = self._store_session()

        with self.assertRaises(HTTPException) as raised:
            video_preview.preview_content(token, range_header="bytes=0-3,8-11")

        self.assertEqual(raised.exception.status_code, 416)

    def test_preview_content_rejects_non_video_upstream(self):
        token = self._store_session()
        upstream = FakeUpstreamResponse(status_code=200, content_type="image/png")

        with patch.object(video_preview.video_network, "get", return_value=upstream):
            with self.assertRaises(HTTPException) as raised:
                video_preview.preview_content(token)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertTrue(upstream.closed)

class PreparedPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.fixtures.name)
        cls.ffmpeg = paths.FFMPEG_DIR / 'ffmpeg.exe' if (paths.FFMPEG_DIR / 'ffmpeg.exe').exists() else paths.FFMPEG_DIR / 'ffmpeg'
        if not cls.ffmpeg.exists():
            cls.fixtures.cleanup()
            raise unittest.SkipTest('FFmpeg unavailable')
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.fixture_dir / 'video.mp4')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                        '-c:a', 'aac', str(cls.fixture_dir / 'audio.m4a')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-i', str(cls.fixture_dir / 'video.mp4'),
                        '-i', str(cls.fixture_dir / 'audio.m4a'), '-c', 'copy', '-hls_time', '0.5', '-hls_list_size', '0',
                        '-hls_segment_filename', str(cls.fixture_dir / 'segment-%03d.ts'),
                        str(cls.fixture_dir / 'playlist.m3u8')], check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-i', str(cls.fixture_dir / 'video.mp4'),
                        '-i', str(cls.fixture_dir / 'audio.m4a'), '-c', 'copy', '-seg_duration', '0.5', '-f', 'dash',
                        str(cls.fixture_dir / 'manifest.mpd')], cwd=cls.fixture_dir, check=True, capture_output=True)
        subprocess.run([str(cls.ffmpeg), '-y', '-v', 'error', '-i', str(cls.fixture_dir / 'video.mp4'),
                        '-i', str(cls.fixture_dir / 'audio.m4a'), '-c:v', 'libvpx', '-c:a', 'libvorbis',
                        str(cls.fixture_dir / 'source.webm')], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(assets, 'ROOT', Path(folder)))
        self.stack.enter_context(patch.object(assets, 'jobs', {}))
        self.stack.enter_context(patch.object(assets, '_start'))
        self.stack.enter_context(patch.object(assets, '_try_stream', return_value=False))
        self.download = self.stack.enter_context(patch.object(assets, '_download', side_effect=self.copy_source))
        app = FastAPI()
        app.include_router(video_preview.router)
        self.client = TestClient(app)

    def copy_source(self, job, url, target):
        shutil.copyfile(self.fixture_dir / ('audio.m4a' if 'audio' in url else 'video.mp4'), target)

    def create(self, paired=True):
        data = assets.create('https://v3.douyinvod.com/video.mp4', 'https://v3.douyinvod.com/audio.m4a' if paired else None,
                             'https://www.douyin.com/video/123', None)
        return assets.jobs[data['preview_id']]

    def test_real_merge_contains_decodable_non_silent_audio_and_supports_range(self):
        job = self.create()
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'ready', job.value)
        self.assertTrue(job.value['has_audio'])
        self.assertGreaterEqual(job.value['expires_at'] - time.time(), 1790)
        decoded = subprocess.run([str(self.ffmpeg), '-v', 'error', '-i', str(assets.file_path(job.token, job.value)),
                                  '-map', '0:a:0', '-f', 's16le', '-'], capture_output=True, check=True).stdout
        self.assertGreater(len(decoded), 1000)
        self.assertTrue(any(decoded))
        url = f'/api/previews/{job.token}/content'
        response = self.client.get(url, headers={'Range': 'bytes=0-31'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 32)
        self.assertIn('bytes 0-31/', response.headers['content-range'])
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(job.readers, 0)
        self.assertEqual(self.client.get(url, headers={'Range': 'bytes=999999999-'}).status_code, 416)
        self.assertEqual(job.readers, 0)

    def test_video_only_result_is_identified_instead_of_claiming_to_have_sound(self):
        job = self.create(paired=False)
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'ready', job.value)
        self.assertFalse(job.value['has_audio'])
        self.assertIn('仅含画面', job.value['message'])

    def test_missing_paired_audio_fails_without_publishing_silent_output(self):
        job = self.create()
        self.download.side_effect = lambda job, url, target: shutil.copyfile(self.fixture_dir / 'video.mp4', target)
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'failed')
        self.assertEqual(job.value['error_code'], 'audio_missing')
        self.assertFalse((job.directory / 'preview.mp4').exists())

    def test_preparing_content_is_unavailable_and_cancel_prevents_network_work(self):
        job = self.create()
        self.assertEqual(self.client.get(f'/api/previews/{job.token}/content').status_code, 409)
        self.client.delete(f'/api/previews/{job.token}').raise_for_status()
        with self.assertRaises(HTTPException) as caught:
            self.create()
        self.assertEqual(caught.exception.status_code, 429)
        assets._prepare(job)
        self.download.assert_not_called()
        self.assertEqual(job.value['status'], 'failed')

    def test_expired_file_is_retained_for_current_reader_then_removed(self):
        job = self.create()
        assets._prepare(job)
        assets.acquire(job.token, job.value)
        job.value['expires_at'] = time.time() - 1
        assets.cleanup()
        self.assertTrue((job.directory / 'preview.mp4').exists())
        self.assertEqual(self.client.get(f'/api/previews/{job.token}').status_code, 410)
        assets.release(job.token)
        assets.cleanup()
        self.assertFalse(job.directory.exists())

    def test_preparation_limits_and_source_expiry(self):
        self.create()
        with self.assertRaises(HTTPException) as caught:
            self.create()
        self.assertEqual(caught.exception.status_code, 429)
        with patch.object(assets.cache, 'media_ttl', return_value=0):
            with self.assertRaises(HTTPException) as caught:
                self.create()
        self.assertEqual(caught.exception.status_code, 410)

    def test_deleted_record_cancels_its_job(self):
        job = self.create()
        job.value['parse_record_id'] = 'record'
        assets.cancel_record(0, 'other')
        self.assertFalse(job.cancelled.is_set())
        assets.cancel_record(0, 'record')
        self.assertTrue(job.cancelled.is_set())

    def test_missing_redis_key_cannot_be_resurrected_by_worker(self):
        job = self.create()
        job.authenticated = True
        with patch.object(assets.cache, 'get', return_value=None), patch.object(assets.cache, 'put') as put:
            self.assertFalse(assets._write(job))
            put.assert_not_called()
        self.assertTrue(job.cancelled.is_set())

    def test_other_user_cannot_read_or_cancel_preview(self):
        job = self.create()
        marker = accounts.principal.set(accounts.Principal(7, 'other', 'user'))
        try:
            with patch.object(assets.cache, 'get', return_value=job.value):
                for method in (self.client.get, self.client.delete):
                    self.assertEqual(method(f'/api/previews/{job.token}').status_code, 404)
        finally:
            accounts.principal.reset(marker)
        self.assertFalse(job.cancelled.is_set())

    def test_download_rejects_html_and_closes_upstream(self):
        job = self.create()
        job.directory.mkdir()
        upstream = FakeUpstreamResponse(status_code=200, content_type='text/html')
        with patch.object(assets.network, 'public_get', return_value=upstream):
            with self.assertRaises(assets.PreparationError):
                # Use the real downloader rather than the fixture copier.
                original_download(job, 'https://v3.douyinvod.com/video.mp4', job.directory / 'input')
        self.assertTrue(upstream.closed)

    def test_direct_download_recovers_from_tls_eof(self):
        job = self.create()
        job.directory.mkdir()
        upstream = FakeUpstreamResponse(status_code=200, content_type='video/mp4', body=b'complete')
        with patch.object(assets.video_network, 'get', side_effect=[requests.exceptions.SSLError('UNEXPECTED_EOF'), upstream]) as get, \
             patch.object(job.cancelled, 'wait', return_value=False):
            original_download(job, 'https://example.com/video.mp4', job.directory / 'input')
        self.assertEqual(get.call_count, 2)
        self.assertEqual((job.directory / 'input').read_bytes(), b'complete')
        self.assertTrue(upstream.closed)

    def test_preparation_exposes_a_specific_network_failure(self):
        job = self.create()
        self.download.side_effect = requests.exceptions.SSLError('UNEXPECTED_EOF https://example.com/?secret=token')
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'failed')
        self.assertEqual(job.value['error_code'], 'network_tls')
        self.assertIn('安全连接中断', job.value['message'])
        self.assertNotIn('token', job.value['message'])

    def upstream_request(self, method, url, **kwargs):
        name = url.rsplit('/', 1)[-1].split('?', 1)[0]
        path = self.fixture_dir / name
        if not path.is_file():
            raise AssertionError(f'Unexpected media request: {method} {url}')
        body = path.read_bytes()
        response = requests.Response()
        response.url, response.status_code, response.reason = url, 200, 'OK'
        response.headers.update({'Content-Type': {'.m3u8': 'application/vnd.apple.mpegurl', '.mpd': 'application/dash+xml',
                                                  '.ts': 'video/mp2t', '.webm': 'video/webm'}.get(path.suffix, 'video/mp4'),
                                 'Content-Length': str(len(body))})
        response.raw = HTTPResponse(body=io.BytesIO(b'' if method == 'HEAD' else body), headers=response.headers,
                                    status=200, preload_content=False)
        return response

    def test_hls_and_dash_are_prepared_with_real_native_downloaders(self):
        for name in ('playlist.m3u8', 'manifest.mpd'):
            with self.subTest(manifest=name):
                initial = assets.create(f'https://media.example.test/{name}', None, 'https://example.com/watch/123', None)
                job = assets.jobs[initial['preview_id']]
                with patch.object(sources.network, 'public_url', side_effect=lambda value: value) as validate, \
                     patch.object(requests.Session, 'request', side_effect=self.upstream_request) as request:
                    assets._prepare(job)
                self.assertEqual(job.value['status'], 'ready', job.value)
                self.assertTrue(job.value['has_audio'])
                self.assertTrue(any('.ts' in call.args[1] or '.m4s' in call.args[1] for call in request.call_args_list))
                self.assertEqual(validate.call_count, request.call_count)
                self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in request.call_args_list))
                self.client.delete(f'/api/previews/{job.token}').raise_for_status()

    def test_extractor_format_downloads_both_tracks_before_merging(self):
        initial = assets.create('https://example.com/watch/123', None, 'https://example.com/watch/123', None,
                                format_id='137+bestaudio/best')
        job = assets.jobs[initial['preview_id']]
        info = {'id': 'test', 'title': 'test', 'ext': 'mp4', 'requested_formats': [
            {'url': 'https://media.example.test/video.mp4', 'format_id': '137', 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'h264', 'acodec': 'none'},
            {'url': 'https://media.example.test/audio.m4a', 'format_id': '140', 'ext': 'm4a', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'aac'}]}
        with patch.object(sources.PreviewYoutubeDL, 'extract_info', return_value=info) as extract, \
             patch.object(sources.network, 'public_url', side_effect=lambda value: value), \
             patch.object(requests.Session, 'request', side_effect=self.upstream_request) as request:
            assets._prepare(job)
        self.assertEqual(job.value['status'], 'ready', job.value)
        self.assertTrue(job.value['has_audio'])
        self.assertEqual({call.args[1].rsplit('/', 1)[-1] for call in request.call_args_list}, {'video.mp4', 'audio.m4a'})
        extract.assert_called_once_with('https://example.com/watch/123', download=False)

    def test_cancelled_segment_download_closes_files_before_cleanup(self):
        for name in ('playlist.m3u8', 'manifest.mpd'):
            with self.subTest(manifest=name):
                initial = assets.create(f'https://media.example.test/{name}', None, 'https://example.com/watch/123', None)
                job = assets.jobs[initial['preview_id']]
                def interrupt(method, url, **kwargs):
                    if url.endswith(('.ts', '.m4s')):
                        raise sources.PreparationError('cancelled', 'cancelled')
                    return self.upstream_request(method, url, **kwargs)
                with patch.object(sources.network, 'public_url', side_effect=lambda value: value), \
                     patch.object(requests.Session, 'request', side_effect=interrupt):
                    assets._prepare(job)
                self.assertEqual(job.value['error_code'], 'cancelled', job.value)
                self.assertEqual(list(job.directory.glob('*')), [])

    def test_webm_is_transcoded_to_h264_and_aac(self):
        job = self.create(paired=False)
        self.download.side_effect = lambda job, url, target: shutil.copyfile(self.fixture_dir / 'source.webm', target)
        assets._prepare(job)
        self.assertEqual(job.value['status'], 'ready', job.value)
        codecs = {s['codec_name'] for s in assets._probe(job, assets.file_path(job.token, job.value))['streams']}
        self.assertEqual(codecs, {'h264', 'aac'})

    def test_manifest_content_type_handles_urls_without_extensions(self):
        job = self.create(paired=False)
        job.directory.mkdir()
        upstream = FakeUpstreamResponse(status_code=200, content_type='application/vnd.apple.mpegurl')
        result = (job.directory / 'video', None)
        with patch.object(assets.network, 'public_get', return_value=upstream), \
             patch.object(sources, 'download', return_value=result) as download:
            self.assertEqual(original_download(job, 'https://media.example.test/play?id=123', job.directory / 'input'), result)
        download.assert_called_once()
        self.assertTrue(upstream.closed)


original_download = assets._download


if __name__ == "__main__":
    unittest.main()
