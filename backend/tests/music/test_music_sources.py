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
