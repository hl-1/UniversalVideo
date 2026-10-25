import unittest
from unittest.mock import Mock, patch

import requests

from backend.core import paths
from backend.modules.music import sources as music


class AT38SourceTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch.object(music.requests, "Session")
        self.session_factory = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.session = self.session_factory.return_value.__enter__.return_value
        self.track = music.Track(title="song", artist="artist", duration=269, provider="qq",
                                 identifier="0039MnYb0qxYhV",
                                 webpage_url="https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
        self.media_url = "https://cdn.example.com/song.mp3"

    def response(self, data):
        response = Mock()
        response.json.return_value = data
        return response

    def challenge(self):
        response = self.response(None)
        response.json.side_effect = ValueError("HTML visitor page")
        response.text = '<script>document.cookie="mk_visit=123-abcdef;path=/;max-age=43200";location.replace("/");</script>'
        return response

    def test_existing_public_sources_take_priority(self):
        for provider, data in (("qq", {"data": {"url": self.media_url}}), ("netease", {"url": self.media_url})):
            with self.subTest(provider=provider), patch.object(music, "_json", return_value=data):
                track = self.track.model_copy(update={"provider": provider})
                url, _, source = music.resolve(track, {}, public_only=True)
            self.assertEqual(url, self.media_url)
            self.assertNotIn("at38", source)
        self.session_factory.assert_not_called()

    def test_original_platform_source_takes_priority(self):
        with patch.object(music, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = {
                "formats": [{"url": self.media_url, "vcodec": "none", "abr": 320}]}
            url, _, source = music.resolve(self.track, {})
        self.assertEqual((url, source), (self.media_url, "原平台"))
        self.session_factory.assert_not_called()

    def test_falls_back_after_missing_source_or_parser_timeout(self):
        for provider, identifier, source_type in (("qq", self.track.identifier, "qq"), ("netease", "5257138", "wy")):
            for failure in ({}, requests.Timeout("parser timeout")):
                with self.subTest(provider=provider, failure=failure):
                    self.session.get.reset_mock()
                    self.session.get.return_value = self.response({"code": 1, "url": self.media_url})
                    track = self.track.model_copy(update={"provider": provider, "identifier": identifier})
                    options = {"http_headers": {"User-Agent": "test-agent"}, "quiet": True}
                    with patch.object(music, "_json", side_effect=failure if isinstance(failure, Exception) else None,
                                      return_value=failure):
                        url, resolved, source = music.resolve(track, options, public_only=True)
                    self.assertEqual(url, self.media_url)
                    self.assertIn("at38.cn", source)
                    self.assertEqual(resolved["http_headers"]["Referer"], music.AT38_URL)
                    self.assertEqual(resolved["http_headers"]["User-Agent"], "test-agent")
                    self.assertTrue(resolved["quiet"])
                    self.assertNotIn("Referer", options["http_headers"])
                    self.assertNotIn("Cookie", resolved["http_headers"])
                    params = self.session.get.call_args.kwargs["params"]
                    self.assertEqual((params["action"], params["type"], params["id"]), ("geturl", source_type, identifier))

    def test_visitor_cookie_is_applied_only_to_at38_and_request_is_retried(self):
        self.session.get.side_effect = [self.challenge(), self.response({"code": 1, "url": self.media_url})]
        self.assertEqual(music._at38_url(self.track), self.media_url)
        self.session.cookies.set.assert_called_once_with("mk_visit", "123-abcdef", domain="www.at38.cn", path="/")
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args_list[0], self.session.get.call_args_list[1])
        self.session_factory.return_value.__exit__.assert_called_once()

    def test_repeated_challenge_stops_after_one_retry(self):
        self.session.get.return_value = self.challenge()
        with self.assertRaisesRegex(RuntimeError, "at38"):
            music._at38_url(self.track)
        self.assertEqual(self.session.get.call_count, 2)

    def test_unrecognized_html_is_not_executed_or_retried(self):
        response = self.challenge()
        response.text = '<script>document.cookie="other=value";location.replace("/");</script>'
        self.session.get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "at38"):
            music._at38_url(self.track)
        self.session.get.assert_called_once()
        self.session.cookies.set.assert_not_called()

    def test_failed_responses_and_invalid_urls_are_rejected(self):
        for data in ({"code": 0, "url": self.media_url}, [], {"code": 1}, {"code": 1, "url": {}},
                     {"code": 1, "url": "javascript:alert(1)"}, {"code": 1, "url": "https:///song.mp3"},
                     {"code": 1, "url": "/song.mp3"}):
            with self.subTest(data=data):
                self.session.get.return_value = self.response(data)
                with self.assertRaisesRegex(RuntimeError, "at38"):
                    music._at38_url(self.track)

    def test_at38_timeout_keeps_the_existing_music_error(self):
        self.session.get.side_effect = requests.Timeout("timeout")
        with patch.object(music, "_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "未取得完整音源"):
                music.resolve(self.track, {}, public_only=True)
        self.session.get.assert_called_once()

    def test_kugou_does_not_use_at38(self):
        track = self.track.model_copy(update={"provider": "kugou"})
        with patch.object(music, "_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "酷狗"):
                music.resolve(track, {}, public_only=True)
        self.session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class SearchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        for state in music.provider_health.values():
            state.update(failures=0, empty=0, success=False, until=0., next=0.)
        self.addCleanup(lambda: [state.update(failures=0, empty=0, success=False, until=0., next=0.) for state in music.provider_health.values()])

    def test_provider_has_at_most_two_requests(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        active = peak = 0
        guard = threading.Lock()
        def search(*args):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(active, peak)
            time.sleep(.02)
            with guard: active -= 1
            return ["track"]
        with patch.object(music, "_search_impl", side_effect=search), patch.object(music, "SEARCH_REQUEST_INTERVAL", 0), ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda i: music._search("qq", str(i)), range(20)))
        self.assertEqual(peak, 2)
        self.assertEqual(len(results), 20)

    def test_failures_open_circuit(self):
        with patch.object(music, "_search_impl", side_effect=RuntimeError("405")) as search, patch.object(music, "SEARCH_REQUEST_INTERVAL", 0):
            for i in range(5):
                with self.assertRaises(RuntimeError): music._search("netease", str(i))
            self.assertEqual(search.call_count, 3)

    def test_business_errors_are_not_cached_as_empty_success(self):
        for provider, payload in [('netease', {'code': 405}), ('qq', {'code': 1}), ('kugou', {'status': 0, 'error_code': 300})]:
            with self.subTest(provider=provider), patch.object(music, '_json', return_value=payload):
                with self.assertRaises(RuntimeError):
                    music._search_impl(provider, 'business-error-test', 1)
                self.assertNotIn((provider, 'business-error-test', 1), music.search_cache)

    def test_identical_inflight_queries_share_one_request(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        entered, release, follower = threading.Event(), threading.Event(), threading.Event()
        original_pause = music.search_pause
        def pause(seconds):
            if seconds == .05: follower.set()
            original_pause(seconds)
        def search(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return ['track']
        with patch.object(music, '_search_impl', side_effect=search) as request, patch.object(music, 'search_pause', side_effect=pause), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(music._search, 'qq', 'same-query')
            self.assertTrue(entered.wait(3))
            second = pool.submit(music._search, 'qq', 'same-query')
            try:
                self.assertTrue(follower.wait(3))
            finally:
                release.set()
            self.assertEqual(first.result(), second.result())
            self.assertEqual(request.call_count, 1)
