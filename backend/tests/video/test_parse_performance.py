import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

from backend.modules.accounts import service as accounts
from backend.modules.video import api, parse_cache, parse_runtime, parsing, schemas, thumbnails


def result(url='https://example.com/watch/123'):
    return schemas.ParseResponse(title='fixture', webpage_url=url, formats=[schemas.FormatInfo(format_id='best', label='MP4')])


class ParsePerformanceTests(unittest.TestCase):
    def test_older_extraction_cannot_overwrite_a_forced_refresh(self):
        started, release = threading.Event(), threading.Event()
        def older(url):
            started.set()
            release.wait(2)
            return result(url).model_copy(update={'title': 'older'})
        with patch.object(parse_runtime, 'flights', {}), patch.object(parse_runtime, 'epochs', {}), \
             patch.object(parse_cache, '_get_sniff_cache', return_value=None), patch.object(parse_cache, '_set_sniff_cache') as put, \
             patch('backend.modules.video.cache.remove'), patch('backend.modules.video.cache.parse_key', return_value='key'), \
             ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(parse_runtime.resolve, 'https://example.com/watch/123', older)
            self.assertTrue(started.wait(1))
            try:
                fresh = parse_runtime.resolve('https://example.com/watch/123', lambda url: result(url).model_copy(update={'title': 'fresh'}), force=True)
                self.assertEqual(fresh.title, 'fresh')
            finally:
                release.set()
            self.assertEqual(first.result().title, 'older')
            self.assertEqual([call.args[1].title for call in put.call_args_list], ['fresh'])

    def test_metadata_extractor_precedes_browser_and_never_downloads(self):
        with patch.object(parsing, 'YoutubeDL') as ydl, patch.object(parsing.video_browser, 'parse_browser_sniffed_page') as browser:
            ydl.return_value.__enter__.return_value.extract_info.return_value = dict(title='fast', formats=[])
            self.assertEqual(parsing.parse_video('https://www.youtube.com/watch?v=fixture').title, 'fast')
            ydl.return_value.__enter__.return_value.extract_info.assert_called_once_with('https://www.youtube.com/watch?v=fixture', download=False)
            self.assertEqual(ydl.call_args.args[0]['retries'], 0)
            self.assertLessEqual(ydl.call_args.args[0]['socket_timeout'], 6)
            browser.assert_not_called()

    def test_fallback_visits_each_strategy_once(self):
        with patch.object(parsing, 'YoutubeDL') as ydl, \
             patch.object(parsing.video_sniffing, 'parse_sniffed_page', side_effect=HTTPException(422, 'no HTML media')) as html, \
             patch.object(parsing.video_browser, 'parse_browser_sniffed_page', side_effect=HTTPException(422, 'no browser media')) as browser:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = DownloadError('not available')
            with self.assertRaises(HTTPException):
                parsing.parse_video('https://www.youtube.com/watch?v=fixture')
            self.assertEqual(html.call_count, 1)
            self.assertEqual(browser.call_count, 1)

    def test_same_owner_and_url_share_extraction_but_receive_independent_results(self):
        started, release = threading.Event(), threading.Event()
        def extract(url):
            started.set()
            release.wait(2)
            return result(url)
        factory = Mock(side_effect=extract)
        with patch.object(parse_runtime, 'flights', {}), patch.object(parse_cache, '_get_sniff_cache', return_value=None), \
             patch.object(parse_cache, '_set_sniff_cache'), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(parse_runtime.resolve, 'https://example.com/watch/123', factory)
            self.assertTrue(started.wait(1))
            second = executor.submit(parse_runtime.resolve, 'https://example.com/watch/123', factory)
            time.sleep(.05)
            release.set()
            one, two = first.result(), second.result()
            self.assertEqual(factory.call_count, 1)
            one.formats.clear()
            self.assertEqual(len(two.formats), 1)

    def test_wait_is_bounded_and_timed_out_work_cannot_publish_cache(self):
        release, finished = threading.Event(), threading.Event()
        def extract(url):
            release.wait(1)
            finished.set()
            return result(url)
        with patch.object(parse_runtime, 'MAX_SECONDS', .08), patch.object(parse_runtime, 'flights', {}), \
             patch.object(parse_cache, '_get_sniff_cache', return_value=None), patch.object(parse_cache, '_set_sniff_cache') as put:
            try:
                with self.assertRaises(HTTPException) as raised:
                    parse_runtime.resolve('https://example.com/watch/123', extract)
                self.assertEqual(raised.exception.status_code, 504)
                self.assertTrue(parse_runtime.flights)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                deadline = time.monotonic() + 1
                while parse_runtime.flights and time.monotonic() < deadline:
                    time.sleep(.01)
            put.assert_not_called()

    def test_owner_is_part_of_the_coalescing_identity(self):
        barrier = threading.Barrier(2)
        def extract(url):
            barrier.wait(timeout=2)
            return result(url)
        def run(uid):
            marker = accounts.principal.set(accounts.Principal(uid, f'user-{uid}', 'user'))
            try:
                return parse_runtime.resolve('https://example.com/watch/123', extract)
            finally:
                accounts.principal.reset(marker)
        with patch.object(parse_runtime, 'flights', {}), patch.object(parse_cache, '_get_sniff_cache', return_value=None), \
             patch.object(parse_cache, '_set_sniff_cache'), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, uid) for uid in (41, 42)]
            self.assertEqual([future.result().title for future in futures], ['fixture', 'fixture'])

    def test_thumbnail_io_does_not_delay_metadata_response_and_keeps_owner_context(self):
        release, finished, started = threading.Event(), threading.Event(), threading.Event()
        owners = []
        def cache_image(url):
            owners.append(accounts.owner_id())
            started.set()
            release.wait(2)
            return '/api/thumbnails/fixture.jpg'
        app = FastAPI()
        app.include_router(api.router)
        parsed = result()
        parsed.thumbnail = '/api/image-proxy?url=https%3A%2F%2Fcdn.example.com%2Fcover.jpg'
        marker = accounts.principal.set(accounts.Principal(42, 'fixture', 'user'))
        try:
            with patch.object(api.network, 'public_url'), patch.object(api.video_history, 'create_record', return_value='record'), \
                 patch.object(api.video_history, 'finish_record'), patch.object(api.video_history, 'link_legacy_results'), \
                 patch.object(parse_runtime, 'resolve', return_value=parsed), patch.object(thumbnails, '_cache_thumbnail', side_effect=cache_image), \
                 patch.object(api.video_history, 'update_thumbnail', side_effect=lambda *args: finished.set()), TestClient(app) as client:
                try:
                    before = time.monotonic()
                    response = client.post('/api/parse', json={'url': parsed.webpage_url})
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('total;dur=', response.headers['server-timing'])
                    self.assertTrue(started.wait(1))
                    self.assertFalse(finished.is_set())
                    self.assertEqual(owners, [42])
                finally:
                    release.set()
                    self.assertTrue(finished.wait(1))
        finally:
            accounts.principal.reset(marker)
