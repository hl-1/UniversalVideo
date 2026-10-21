import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from backend.core import paths
from backend.infrastructure.runtime import executable_name
from backend.modules.music import full_playback as full, sources as music


class FullPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.full = Path(cls.fixture.name) / "full.mp3"
        cls.trial = Path(cls.fixture.name) / "trial.mp3"
        for path, seconds in ((cls.full, 8), (cls.trial, 1)):
            subprocess.run([str(paths.FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-y",
                            "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "libmp3lame",
                            "-b:a", "64k", str(path)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.track = music.Track(title="song", artist="artist", duration=8, provider="qq", identifier="0039MnYb0qxYhV",
                                 webpage_url="https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
        self.patch = patch.object(full.storage, "directory", return_value=Path(self.work.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def response(self, payload, content_type="audio/mpeg", status=200, length=None):
        response = requests.Response()
        response.status_code = status
        response._content = payload
        response._content_consumed = True
        response.headers = {"Content-Type": content_type, "Content-Length": str(len(payload) if length is None else length)}
        return response

    def test_trial_parser_falls_back_to_complete_recording(self):
        payloads = {"https://cdn.example/trial.mp3": self.trial.read_bytes(), "https://cdn.example/full.mp3": self.full.read_bytes()}
        with patch.object(music, "_json", return_value={"data": {"url": "https://cdn.example/trial.mp3"}}), \
             patch.object(music, "_at38_url", return_value="https://cdn.example/full.mp3"), \
             patch.object(full.video_network, "get", side_effect=lambda url, **kw: self.response(payloads[url])), \
             patch.object(music, "_search") as search:
            result = full.prepare(self.track)
        self.assertEqual(result.path.read_bytes(), self.full.read_bytes())
        self.assertAlmostEqual(result.duration, 8, delta=.1)
        self.assertIn("at38", result.source)
        search.assert_not_called()
        result.close()

    def test_switches_platform_only_to_same_recording(self):
        candidate = self.track.model_copy(update={"provider": "netease", "identifier": "123", "webpage_url": "https://music.163.com/song?id=123"})
        wrong = candidate.model_copy(update={"title": "song (Live)"})
        def resolve(track, options, **kwargs):
            if track.provider == "qq":
                raise RuntimeError("only trial")
            kwargs["validate_source"]("https://cdn.example/full.mp3", options, "alternate platform")
            return "https://cdn.example/full.mp3", options, "alternate platform"
        with patch.object(music, "resolve", side_effect=resolve) as resolver, \
             patch.object(music, "_search", return_value=[wrong, candidate]), \
             patch.object(full.video_network, "get", return_value=self.response(self.full.read_bytes())):
            result = full.prepare(self.track)
        self.assertEqual(result.source_url, candidate.webpage_url)
        self.assertEqual([call.args[0].title for call in resolver.call_args_list], ["song", "song"])
        result.close()

    def test_no_full_source_fails_and_deletes_trial(self):
        with patch.object(music, "_json", return_value={"data": {"url": "https://cdn.example/trial.mp3"}}), \
             patch.object(music, "_at38_url", return_value="https://cdn.example/trial.mp3"), \
             patch.object(full.video_network, "get", return_value=self.response(self.trial.read_bytes())) as get, \
             patch.object(music, "_search", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "未找到可验证的完整歌曲"):
                full.prepare(self.track)
        self.assertEqual(get.call_count, 1)
        self.assertFalse(list(Path(self.work.name).iterdir()))

    def test_unknown_duration_does_not_resolve(self):
        with patch.object(music, "resolve") as resolver:
            with self.assertRaisesRegex(RuntimeError, "时长未知"):
                full.prepare(self.track.model_copy(update={"duration": None}))
        resolver.assert_not_called()

    def test_bad_content_partial_oversized_and_truncated_transfers(self):
        cases = [("text/html", 200, 5), ("audio/mpeg", 206, 5),
                 ("audio/mpeg", 200, full.MAX_BYTES + 1), ("audio/mpeg", 200, 100)]
        for content_type, status, length in cases:
            with self.subTest(content_type=content_type, status=status, length=length), \
                 patch.object(music, "_json", return_value={"data": {"url": "https://cdn.example/bad"}}), \
                 patch.object(music, "_at38_url", side_effect=RuntimeError("missing")), \
                 patch.object(music, "_search", return_value=[]), \
                 patch.object(full.video_network, "get", return_value=self.response(b"short", content_type, status, length)):
                with self.assertRaisesRegex(RuntimeError, "完整歌曲"):
                    full.prepare(self.track)
                self.assertFalse(list(Path(self.work.name).iterdir()))

    def test_false_container_duration_fails_decoded_duration(self):
        with patch.object(music, "_json", return_value={"data": {"url": "https://cdn.example/trial.mp3"}}), \
             patch.object(music, "_at38_url", side_effect=RuntimeError("missing")), \
             patch.object(music, "_search", return_value=[]), \
             patch.object(full.video_network, "get", return_value=self.response(self.trial.read_bytes())), \
             patch.object(full, "probe_audio", return_value={"duration": 8, "codec": "mp3"}):
            with self.assertRaisesRegex(RuntimeError, "完整歌曲"):
                full.prepare(self.track)

    def test_timeout_stops_fallbacks_and_cleans_up(self):
        with patch.object(full, "PREPARATION_SECONDS", 0), patch.object(music, "resolve") as resolver:
            with self.assertRaises(full.PreparationExpired):
                full.prepare(self.track)
        resolver.assert_not_called()
        self.assertFalse(list(Path(self.work.name).iterdir()))

    def test_matching_rejects_wrong_versions_artists_and_durations(self):
        for updates in ({"title": "song Live"}, {"title": "song 伴奏"}, {"artist": "cover artist"}, {"duration": 60}, {"duration": None}):
            self.assertFalse(full.same_recording(self.track, self.track.model_copy(update=updates)))

    def test_restart_cleanup_preserves_recent_audio(self):
        root = Path(self.work.name)
        old = root / "music-playback-old"
        new = root / "music-playback-new"
        old.mkdir()
        new.mkdir()
        os.utime(old, (time.time() - 4000,) * 2)
        full._cleanup_stale(root, 1800)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
