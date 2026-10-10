import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import music_lyrics as lyrics


class LyricsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "song.m4a").write_bytes(b"audio")
        self.directory = patch.object(lyrics, "DOWNLOAD_DIR", self.root)
        self.directory.start()
        self.saved = {}
        self.storage_read = patch.object(lyrics.library, "read_lyrics", side_effect=lambda name: self.saved.get(name))
        def store(name, data):
            self.saved[name] = data
            return data
        self.storage_write = patch.object(lyrics.library, "store_lyrics", side_effect=store)
        self.storage_read.start()
        self.storage_write.start()
        lyrics.next_request_at = 0
        app = FastAPI()
        app.include_router(lyrics.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.directory.stop()
        self.storage_read.stop()
        self.storage_write.stop()
        self.folder.cleanup()

    def test_multiple_timestamps_offset_and_translation_grouping(self):
        result = lyrics.parse_lrc("[offset:100]\n[00:01.23][00:03.45]Hello\n[00:01.23]你好\n[00:05.000]")
        self.assertEqual(result, [{"time": 1.33, "text": "Hello\n你好"}, {"time": 3.55, "text": "Hello"}, {"time": 5.1, "text": ""}])

    def test_import_persists_and_works_without_a_network_connection(self):
        response = self.client.put("/api/music/lyrics/song.m4a", content="[00:01.00]本地歌词".encode("gb18030"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lines"][0]["text"], "本地歌词")
        with patch.object(lyrics.requests, "get") as get:
            retrieved = self.client.get("/api/music/lyrics/song.m4a").json()
            fetched = self.client.post("/api/music/lyrics/fetch", json={"filename": "song.m4a", "title": "song", "artist": "artist"}).json()
        self.assertEqual(retrieved, fetched)
        self.assertEqual(fetched["source"], "本地 LRC")
        get.assert_not_called()

    def test_invalid_or_oversized_import_does_not_replace_valid_lyrics(self):
        self.client.put("/api/music/lyrics/song.m4a", content=b"[00:01.00]valid")
        for data, status in ((b"not an lrc", 422), (b"a" * 512001, 413)):
            response = self.client.put("/api/music/lyrics/song.m4a", content=data)
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.client.get("/api/music/lyrics/song.m4a").json()["lines"][0]["text"], "valid")

    def test_sidecar_lrc_and_missing_audio(self):
        (self.root / "song.lrc").write_text("[00:02.50]sidecar", encoding="utf-8")
        self.assertEqual(self.client.get("/api/music/lyrics/song.m4a").json()["lines"], [{"time": 2.5, "text": "sidecar"}])
        self.assertEqual(self.client.get("/api/music/lyrics/missing.m4a").status_code, 404)
        with self.assertRaises(HTTPException):
            lyrics.audio_path("../song.m4a")

    def request_lyrics(self):
        return self.client.post("/api/music/lyrics/fetch", json={"filename": "song.m4a", "title": "song", "artist": "artist"})

    def test_automatic_lookup_uses_actual_audio_duration_and_caches_result(self):
        upstream = Mock(status_code=200)
        upstream.json.return_value = {"syncedLyrics": "[00:00.00]found"}
        with patch.object(lyrics, "probe_audio", return_value={"duration": 123.4}), patch.object(lyrics.requests, "get", return_value=upstream) as get:
            first = self.request_lyrics()
            second = self.request_lyrics()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(get.call_args.kwargs["params"]["duration"], 123)
        self.assertEqual(get.call_count, 1)

    def test_not_found_is_cached_and_rate_limit_honors_retry_after(self):
        with patch.object(lyrics, "probe_audio", return_value={"duration": 123}), patch.object(lyrics.requests, "get", return_value=Mock(status_code=404)) as get:
            self.assertEqual(self.request_lyrics().status_code, 200)
            self.assertEqual(self.request_lyrics().status_code, 200)
            self.assertEqual(get.call_count, 1)
        self.saved.clear()
        lyrics.next_request_at = 0
        upstream = Mock(status_code=429, headers={"Retry-After": "30"})
        with patch.object(lyrics, "probe_audio", return_value={"duration": 123}), patch.object(lyrics.requests, "get", return_value=upstream) as get:
            self.assertEqual(self.request_lyrics().status_code, 503)
            self.assertEqual(self.request_lyrics().status_code, 429)
            self.assertEqual(get.call_count, 1)

    def test_import_during_online_lookup_is_preserved(self):
        upstream = Mock(status_code=200)
        upstream.json.return_value = {"syncedLyrics": "[00:01]online"}
        def remote(*args, **kwargs):
            lyrics.save_cached("song.m4a", "[00:01]imported", "本地 LRC")
            return upstream
        with patch.object(lyrics, "probe_audio", return_value={"duration": 123}), patch.object(lyrics.requests, "get", side_effect=remote):
            result = self.request_lyrics()
        self.assertEqual(result.json()["source"], "本地 LRC")
        self.assertEqual(result.json()["lines"][0]["text"], "imported")


if __name__ == "__main__":
    unittest.main()
