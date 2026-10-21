import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import application
from backend.modules.music import full_playback, sources as music
from backend.tests.support import domain_app


class MusicPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(domain_app(application.app))
        self.work = tempfile.TemporaryDirectory()
        self.track = music._register(music.Track(
            title="song", artist="artist", duration=269, provider="qq", identifier="0039MnYb0qxYhV",
            webpage_url="https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV"))
        music.playback_sessions.clear()
        self.prepare_patch = patch.object(full_playback, "prepare", side_effect=self.prepared)
        self.prepare = self.prepare_patch.start()

    def tearDown(self):
        self.prepare_patch.stop()
        self.client.close()
        self.work.cleanup()
        music.playback_sessions.clear()

    def prepared(self, track):
        folder = Path(tempfile.mkdtemp(dir=self.work.name))
        path = folder / "audio.media"
        path.write_bytes(b"0123456789" * 20)
        return full_playback.PreparedAudio(path, "audio/mpeg", 269, "validated parser",
                                          track.webpage_url, "https://cdn.example.com/song.mp3")

    def create_session(self):
        response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["playback_url"]

    def test_song_binding_and_opaque_url(self):
        mismatch = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": "https://y.qq.com/n/ryqq/songDetail/otherSongMid12"})
        self.assertEqual(mismatch.status_code, 422)
        self.prepare.assert_not_called()
        url = self.create_session()
        self.prepare.assert_called_once()
        self.assertNotIn("cdn.example.com", url)
        self.assertNotIn(self.track.token, url)

    def test_failed_validation_never_creates_a_session(self):
        self.prepare.side_effect = RuntimeError("未找到可验证的完整歌曲")
        response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.status_code, 422)
        self.assertIn("完整歌曲", response.text)
        self.assertFalse(music.playback_sessions)

    def test_response_exposes_verified_duration(self):
        response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.json()["duration"], 269)
        self.assertIs(response.json()["full"], True)

    def test_expired_song_does_not_prepare_audio(self):
        music.sessions[self.track.token].expires_at = 0
        response = self.client.post("/api/music/playback", json={"token": self.track.token, "webpage_url": self.track.webpage_url})
        self.assertEqual(response.status_code, 410)
        self.prepare.assert_not_called()

    def test_verified_file_supports_range_suffix_head_and_if_range(self):
        url = self.create_session()
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"0123456789" * 20)
        self.assertEqual(full.headers["cache-control"], "private, no-store")
        self.assertEqual(full.headers["content-type"], "audio/mpeg")
        for header in ("bytes=10-19", "bytes=-10"):
            response = self.client.get(url, headers={"Range": header})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"0123456789")
        head = self.client.head(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.headers["content-range"], "bytes 10-19/200")
        self.assertFalse(head.content)
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=10-19", "If-Range": full.headers["etag"]}).status_code, 206)
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=10-19", "If-Range": '"outdated"'}).status_code, 200)

    def test_invalid_and_out_of_bounds_ranges(self):
        url = self.create_session()
        for header in ("bytes=0-9,20-29", "bytes=-", "items=0-9", "bytes=300-310"):
            self.assertEqual(self.client.get(url, headers={"Range": header}).status_code, 416)
        self.assertIn("200", self.client.get(url, headers={"Range": "bytes=300-310"}).headers["content-range"])

    def test_expired_and_missing_files_require_new_validation(self):
        url = self.create_session()
        session = music.playback_sessions[url.split("/")[-2]]
        session.expires_at = time.time() - 1
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertFalse(session.audio.path.parent.exists())
        url = self.create_session()
        music.playback_sessions[url.split("/")[-2]].audio.path.unlink()
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_new_session_reclaims_expired_files(self):
        url = self.create_session()
        session = music.playback_sessions[url.split("/")[-2]]
        session.expires_at = 0
        self.create_session()
        self.assertFalse(session.audio.path.parent.exists())

    def test_registration_failure_reclaims_prepared_file(self):
        prepared = self.prepared(self.track)
        self.prepare.side_effect = None
        self.prepare.return_value = prepared
        with patch.object(music.accounts, "register_resource", side_effect=RuntimeError("storage unavailable")):
            with self.assertRaises(RuntimeError):
                self.create_session()
        self.assertFalse(prepared.path.parent.exists())
