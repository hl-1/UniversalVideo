import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend.infrastructure import network, platform_access as access, video_network, video_proxy
from backend.infrastructure.ytdlp import YoutubeDL
from backend.modules.accounts import service as accounts
from backend.modules.video import cache, downloads, formats, parsing, tasks
from backend.tests.video.test_preview_sources import upstream


class OverseasTests(unittest.TestCase):
    def test_domains_are_exact_and_cover_mobile_short_links_and_cdns(self):
        for host in ('m.youtube.com', 'youtu.be', 'www.youtube-nocookie.com', 'r1.googlevideo.com', 'i.ytimg.com',
                     'cn.pornhub.com', 'pornhub.org', 'pornhubpremium.com', 'cv.phncdn.com', 'ei.phprcdn.com'):
            self.assertIsNotNone(access.platform('https://' + host))
        for host in ('youtube.com.evil.test', 'evilyoutube.com', 'pornhub.com.evil.test', 'bilibili.com'):
            self.assertIsNone(access.platform('https://' + host))

    def test_user_rejected_before_network_even_on_redirect(self):
        marker = accounts.principal.set(accounts.Principal(2, 'user', 'user'))
        try:
            with YoutubeDL({'quiet': True}) as ydl, patch.object(network, 'public_url'), \
                 patch.object(ydl.session, 'request', return_value=upstream('https://example.com/', status=302,
                        headers={'Location': 'https://www.youtube.com/watch?v=test'})) as send:
                with self.assertRaises(HTTPException) as caught:
                    ydl.urlopen('https://example.com/')
                self.assertEqual(caught.exception.status_code, 403)
                send.assert_called_once()
        finally:
            accounts.principal.reset(marker)

    def test_proxy_is_explicit_for_manifests_segments_and_redirects(self):
        with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_PROXY_URL': 'http://127.0.0.1:18990'}), \
             video_proxy.scope('https://www.youtube.com/watch?v=test'):
            with YoutubeDL({'quiet': True, **parsing._ydl_base_options('https://www.youtube.com/')}) as ydl, \
                 patch.object(network, 'public_url'), patch.object(ydl.session, 'request', return_value=upstream('https://shared.example/segment')):
                with ydl.urlopen('https://shared.example/segment') as result:
                    self.assertEqual(result.read(), b'test')
                self.assertFalse(ydl.session.trust_env)
                self.assertEqual(ydl.session.proxies['https'], 'http://127.0.0.1:18990')
            self.assertEqual(video_proxy.browser_options('https://cn.pornhub.com/')['server'], 'http://127.0.0.1:18990')

    def test_invalid_or_disabled_proxy_never_falls_back_to_direct(self):
        for config in ({'VIDEODREAM_OVERSEAS_ENABLED': 'false'}, {'VIDEODREAM_OVERSEAS_PROXY_URL': 'http://host/nope'}):
            with patch.dict(os.environ, config), patch.object(network, 'public_get') as get:
                with self.assertRaises(HTTPException) as caught:
                    video_network.get('https://www.youtube.com/watch?v=test')
                self.assertEqual(caught.exception.status_code, 503)
                get.assert_not_called()

    def test_native_fragment_thread_keeps_task_route_when_configuration_changes(self):
        with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_PROXY_URL': 'http://127.0.0.1:18990'}), \
             video_proxy.scope('https://www.youtube.com/watch?v=test'):
            with YoutubeDL({'quiet': True}) as ydl, patch.object(network, 'public_url'), \
                 patch.object(ydl.session, 'request', return_value=upstream('https://shared.example/segment')):
                def fragment():
                    self.assertIsNone(video_proxy._active.get())
                    with ydl.urlopen('https://shared.example/segment') as result:
                        self.assertEqual(result.read(), b'test')
                    return ydl.session.proxies['https']
                with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_PROXY_URL': 'http://127.0.0.1:18991'}), \
                     ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(pool.submit(fragment).result(), 'http://127.0.0.1:18990')

    def test_route_revision_changes_parse_identity_and_scope_freezes_route(self):
        with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_ROUTE_REVISION': 'one'}):
            first = cache.parse_key('https://youtu.be/test', 1)
            domestic = cache.parse_key('https://bilibili.com/video/test', 1)
            with video_proxy.scope('https://youtu.be/test') as original:
                with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_ROUTE_REVISION': 'two'}):
                    with video_proxy.scope('https://youtu.be/test') as nested:
                        self.assertEqual(original, nested)
                        self.assertEqual(first, cache.parse_key('https://youtu.be/test', 1))
            with patch.dict(os.environ, {'VIDEODREAM_OVERSEAS_ROUTE_REVISION': 'two'}):
                self.assertNotEqual(first, cache.parse_key('https://youtu.be/test', 1))
                self.assertEqual(domestic, cache.parse_key('https://bilibili.com/video/test', 1))

    def test_both_platforms_reuse_accelerated_video_and_audio_downloads(self):
        for site in ('youtube.com', 'pornhub.com'):
            for media in ('video', 'audio'):
                task = tasks.DownloadTask('fixture', 'https://' + site + '/watch', None, media_type=media)
                with patch.object(downloads, '_run_domestic_download') as shared:
                    downloads._run_download(task)
                    shared.assert_called_once_with(task)
                    self.assertTrue(task.route_revision)

    def test_pornhub_selection_survives_bandwidth_id_changes_without_lowering_quality(self):
        info = {'id': 'fixture', 'title': 'fixture', 'extractor': 'PornHub', 'webpage_url': 'https://www.pornhub.com/view_video.php?viewkey=fixture',
                'formats': [{'format_id': 'hls-3501', 'height': 1080, 'ext': 'mp4', 'protocol': 'm3u8_native',
                             'url': 'https://cdn.example/1080.m3u8', 'vcodec': 'avc1', 'acodec': 'aac'},
                            {'format_id': 'hls-2800', 'height': 720, 'ext': 'mp4', 'protocol': 'm3u8_native',
                             'url': 'https://cdn.example/720.m3u8', 'vcodec': 'avc1', 'acodec': 'aac'}]}
        selector = formats._extract_formats(info)[0].format_id
        self.assertNotIn('3501', selector)
        with YoutubeDL({'quiet': True, 'format': selector}) as ydl:
            selected = ydl.process_ie_result(info, download=False)
            self.assertEqual(selected['height'], 1080)
            self.assertEqual(selected['format_id'], 'hls-3501')
        with YoutubeDL({'quiet': True, 'format': formats.pornhub_selector(2160)}) as ydl:
            from yt_dlp.utils import ExtractorError
            with self.assertRaises(ExtractorError):
                ydl.process_ie_result(info, download=False)

    def test_worker_role_demotion_blocks_source_and_derived_resources(self):
        with patch.object(accounts, 'get_user', return_value={'role': 'user'}):
            with self.assertRaises(HTTPException):
                accounts.active_worker(1, source={'url': 'https://www.youtube.com/watch?v=test'})
            with patch.object(access, 'metadata_for', return_value={'access_scope': 'super_admin'}):
                with self.assertRaises(HTTPException):
                    accounts.active_worker(1, source={'source_filename': 'derived.mp4'})
