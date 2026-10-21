import time
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from backend import application
from backend.core import paths
from backend.modules.music import sources as music
from backend.tests.support import domain_app


class MusicPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(domain_app(application.app))
        self.track = music._register(music.Track(
            title="song", artist="artist", duration=269, provider="qq", identifier="0039MnYb0qxYhV",
            webpage_url="https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV",
        ))
        with music.lock:
            music.playback_sessions.clear()

    def tearDown(self):
        self.client.close()

    def create_session(self):
        with patch.object(music, "resolve", return_value=("https://cdn.example.com/song.mp3", {"http_headers": {"Referer": self.track.webpage_url}}, "parser")):
            response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.status_code, 200)
        return response.json()["playback_url"]

    def upstream(self, status=206, content_type="audio/mpeg"):
        response = Mock(status_code=status)
        response.headers = {"Content-Type": content_type, "Content-Length": "10", "Content-Range": "bytes 10-19/200", "Accept-Ranges": "bytes"}
        response.iter_content.return_value = iter([b"01234", b"56789"])
        return response

    def test_playback_is_bound_to_the_searched_song_and_uses_an_opaque_url(self):
        with patch.object(music, "resolve", return_value=("https://cdn.example.com/song.mp3", {}, "parser")) as resolve:
            mismatch = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": "https://y.qq.com/n/ryqq/songDetail/otherSongMid12"})
            self.assertEqual(mismatch.status_code, 422)
            resolve.assert_not_called()
            success = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(success.status_code, 200)
        self.assertTrue(resolve.call_args.kwargs["public_only"])
        self.assertNotIn("cdn.example.com", success.text)
        self.assertNotIn(self.track.token, success.json()["playback_url"])

    def test_expired_song_information_does_not_resolve_a_source(self):
        with music.lock:
            music.sessions[self.track.token].expires_at = 0
        with patch.object(music, "resolve") as resolve:
            response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.status_code, 410)
        resolve.assert_not_called()

    def test_stream_forwards_range_and_media_headers_and_closes_the_connection(self):
        url = self.create_session()
        upstream = self.upstream()
        with patch.object(music.video_network, "get", return_value=upstream) as get:
            response = self.client.get(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"0123456789")
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/200")
        self.assertEqual(response.headers["Content-Type"], "audio/mpeg")
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=10-19")
        self.assertEqual(get.call_args.kwargs["headers"]["Referer"], self.track.webpage_url)
        upstream.close.assert_called()

    def test_full_stream_and_suffix_ranges_are_supported(self):
        url = self.create_session()
        for headers in ({}, {"Range": "bytes=-10"}):
            with self.subTest(headers=headers), patch.object(music.video_network, "get", return_value=self.upstream(status=200)):
                response = self.client.get(url, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"0123456789")

    def test_multiple_or_malformed_ranges_are_rejected_before_connecting(self):
        url = self.create_session()
        for header in ("bytes=0-9,20-29", "bytes=-", "items=0-9"):
            with self.subTest(header=header), patch.object(music.video_network, "get") as get:
                response = self.client.get(url, headers={"Range": header})
            self.assertEqual(response.status_code, 416)
            get.assert_not_called()

    def test_expired_playback_session_does_not_connect_to_the_cdn(self):
        url = self.create_session()
        token = url.split("/")[-2]
        with music.lock:
            music.playback_sessions[token].expires_at = time.time() - 1
        with patch.object(music.video_network, "get") as get:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 410)
        get.assert_not_called()

    def test_error_pages_and_failed_media_responses_are_not_streamed(self):
        url = self.create_session()
        for status, content_type in ((200, "text/html"), (200, "application/json"), (403, "audio/mpeg")):
            upstream = self.upstream(status, content_type)
            with self.subTest(status=status, content_type=content_type), patch.object(music.video_network, "get", return_value=upstream):
                response = self.client.get(url)
            self.assertEqual(response.status_code, 502)
            upstream.close.assert_called()
            upstream.iter_content.assert_not_called()

    def test_source_timeout_returns_a_retryable_error(self):
        url = self.create_session()
        with patch.object(music.video_network, "get", side_effect=requests.Timeout("timeout")):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 502)

    def test_upstream_unsatisfiable_range_preserves_the_total_length(self):
        url = self.create_session()
        upstream = self.upstream(status=416)
        upstream.headers["Content-Range"] = "bytes */200"
        with patch.object(music.video_network, "get", return_value=upstream):
            response = self.client.get(url, headers={"Range": "bytes=999-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */200")
        upstream.close.assert_called()


if __name__ == "__main__":
    unittest.main()
