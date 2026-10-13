import io
import secrets
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import accounts, library
from backend import music_library_media as media, music_lyrics as lyrics
from PIL import Image


class MySQLMusicLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.principal_marker = accounts.principal.set(accounts.Principal(1, 'admin', 'super_admin'))
        cls.original_ready = library.schema_ready
        cls.config = library.database_config()
        cls.database_name = "videodream_test_" + secrets.token_hex(6)
        admin_config = {key: value for key, value in cls.config.items() if key != "database"}
        cls.administrator = pymysql.connect(**admin_config)
        with cls.administrator.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.database_name}` CHARACTER SET utf8mb4")
        cls.config["database"] = cls.database_name
        cls.config_patch = patch.object(library, "database_config", return_value=cls.config)
        cls.config_patch.start()
        library.schema_ready = False
        cls.work = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.work.name)
        cls.directory_patch = patch.object(library, "DOWNLOAD_DIR", cls.directory)
        cls.directory_patch.start()
        cls.lyrics_directory_patch = patch.object(lyrics, "DOWNLOAD_DIR", cls.directory)
        cls.lyrics_directory_patch.start()
        app = FastAPI()
        app.include_router(library.router)
        app.include_router(media.router)
        app.include_router(lyrics.router)
        cls.client = TestClient(app)
        cls.client.get("/api/library").raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.directory_patch.stop()
        cls.lyrics_directory_patch.stop()
        cls.work.cleanup()
        cls.config_patch.stop()
        library.schema_ready = cls.original_ready
        with cls.administrator.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.database_name}`")
        cls.administrator.close()
        accounts.principal.reset(cls.principal_marker)

    def setUp(self):
        with library.database() as cursor:
            for table in ("music_playlist_tracks", "music_playlists", "music_history", "music_favorites", "music_downloads", "music_tracks"):
                cursor.execute(f"DELETE FROM {table}")
        self.track = {"title": "晴天", "artist": "周杰伦", "provider": "qq", "identifier": "song1",
                      "webpage_url": "https://y.qq.com/n/ryqq/songDetail/song1", "duration": 269}

    def test_download_playlist_destination_and_duplicate_membership(self):
        from fastapi import HTTPException
        self.assertIsNone(library.download_playlist(None, None))
        key = library.download_playlist(None, '  游戏收藏  ')
        self.assertEqual(library.download_playlist(key, None), key)
        for destination in ((key, 'conflict'), (None, '   '), ('missing', None)):
            with self.assertRaises(HTTPException):
                library.download_playlist(*destination)
        payload = library.TrackInput.model_validate(self.track)
        library.add_to_playlist(key, payload)
        library.add_to_playlist(key, payload)
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) AS n FROM music_playlist_tracks WHERE playlist_id=%s', (key,))
            self.assertEqual(cursor.fetchone()['n'], 1)

    def favorite(self, liked=True):
        response = self.client.put("/api/library/favorites", json={"track": self.track, "liked": liked})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_favorite_is_idempotent_and_survives_reconnection(self):
        key = self.favorite()
        self.favorite()
        library.schema_ready = False
        overview = self.client.get("/api/library").json()
        self.assertEqual(overview["counts"]["favorites"], 1)
        saved = self.client.get("/api/library/tracks?view=favorites").json()["tracks"]
        self.assertEqual((saved[0]["id"], saved[0]["title"], saved[0]["liked"]), (key, "晴天", True))
        self.favorite(False)
        self.assertEqual(self.client.get("/api/library").json()["counts"]["favorites"], 0)

    def test_temporary_tokens_and_source_urls_are_not_persisted(self):
        self.track.update(token="temporary-secret", playback_url="https://cdn.example.com/expiring")
        self.favorite()
        row = self.client.get("/api/library/tracks?view=favorites").json()["tracks"][0]
        self.assertNotIn("token", row)
        self.assertNotIn("playback_url", row)

    def test_history_updates_play_count_and_trial_status(self):
        for preview in (False, True):
            response = self.client.post("/api/library/history", json={"track": self.track, "preview": preview})
            self.assertEqual(response.status_code, 200)
        row = self.client.get("/api/library/tracks?view=trials").json()["tracks"][0]
        self.assertEqual(row["play_count"], 2)
        self.assertEqual(self.client.get("/api/library").json()["counts"]["trials"], 1)
        self.client.delete("/api/library/history")
        self.assertEqual(self.client.get("/api/library/tracks?view=history").json()["tracks"], [])

    def test_playlist_membership_deduplicates_and_cascades(self):
        response = self.client.post("/api/library/playlists", json={"name": "我的歌单", "description": ""})
        self.assertEqual(response.status_code, 201)
        key = response.json()["id"]
        for _ in range(2):
            self.client.post(f"/api/library/playlists/{key}/tracks", json=self.track).raise_for_status()
        self.assertEqual(self.client.get("/api/library").json()["playlists"][0]["count"], 1)
        self.client.patch(f"/api/library/playlists/{key}", json={"name": "新名称", "description": "简介"}).raise_for_status()
        self.assertEqual(self.client.get("/api/library").json()["playlists"][0]["name"], "新名称")
        self.client.delete(f"/api/library/playlists/{key}").raise_for_status()
        self.assertEqual(self.client.get(f"/api/library/tracks?view=playlist&playlist_id={key}").status_code, 404)
        with library.database() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM music_playlist_tracks")
            self.assertEqual(cursor.fetchone()["n"], 0)

    def test_remove_song_keeps_other_playlist_and_favorite(self):
        track_key = self.favorite()
        keys = [self.client.post("/api/library/playlists", json={"name": name}).json()["id"] for name in ("A", "B")]
        for key in keys:
            self.client.post(f"/api/library/playlists/{key}/tracks", json=self.track).raise_for_status()
        self.client.delete(f"/api/library/playlists/{keys[0]}/tracks/{track_key}").raise_for_status()
        self.assertEqual(self.client.get("/api/library").json()["counts"]["favorites"], 1)
        self.assertEqual(len(self.client.get(f"/api/library/tracks?view=playlist&playlist_id={keys[1]}").json()["tracks"]), 1)

    def test_profile_persists_and_rejects_blank_names(self):
        response = self.client.patch("/api/library/profile", json={"nickname": "本地听众", "bio": "音乐", "avatar": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/library").json()["profile"]["nickname"], "本地听众")
        self.assertEqual(self.client.patch("/api/library/profile", json={"nickname": "   "}).status_code, 422)
        self.assertEqual(self.client.post("/api/library/playlists", json={"name": " "}).status_code, 422)

    def test_metadata_validation_rejects_non_web_links(self):
        self.track["webpage_url"] = "javascript:alert(1)"
        self.assertEqual(self.client.put("/api/library/favorites", json={"track": self.track, "liked": True}).status_code, 422)

    def test_download_keeps_platform_identity_and_missing_file_is_detected(self):
        path = self.directory / "test.mp3"
        path.write_bytes(b"fixture")
        try:
            library.record_download(path.name, self.track)
            row = self.client.get("/api/library/tracks?view=downloads").json()["tracks"][0]
            self.assertEqual((row["provider"], row["identifier"], row["local_url"]), ("qq", "song1", "/api/files/test.mp3"))
            path.unlink()
            row = self.client.get("/api/library/tracks?view=downloads").json()["tracks"][0]
            self.assertIsNone(row["local_url"])
            self.assertFalse(row["file_available"])
        finally:
            path.unlink(missing_ok=True)

    def test_import_validates_audio_and_preserves_file_on_repeated_scan(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 8000)
        response = self.client.post("/api/library/import", files={"file": ("测试音频.wav", buffer.getvalue(), "audio/wav")})
        self.assertEqual(response.status_code, 201, response.text)
        path = self.directory / response.json()["filename"]
        try:
            for _ in range(2):
                self.assertEqual(self.client.get("/api/library").json()["counts"]["downloads"], 1)
            self.assertTrue(path.is_file())
            row = self.client.get("/api/library/tracks?view=downloads").json()["tracks"][0]
            self.assertEqual(row["title"], "测试音频")
            self.assertAlmostEqual(row["duration"], 1, places=2)
        finally:
            path.unlink(missing_ok=True)

    def test_multiple_downloads_of_same_song_count_as_one_library_track(self):
        paths = [self.directory / name for name in ("first.mp3", "second.mp3")]
        try:
            for path in paths:
                path.write_bytes(b"fixture")
                library.record_download(path.name, self.track)
            self.assertEqual(self.client.get("/api/library").json()["counts"]["downloads"], 1)
            rows = self.client.get("/api/library/tracks?view=downloads").json()["tracks"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["filename"], "second.mp3")
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_invalid_import_removes_partial_file_and_does_not_record_it(self):
        response = self.client.post("/api/library/import", files={"file": ("invalid.mp3", b"not audio", "audio/mpeg")})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(list(self.directory.iterdir()), [])
        with library.database() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM music_downloads")
            self.assertEqual(cursor.fetchone()["n"], 0)

    def test_content_merge_preserves_relations_and_old_identity(self):
        paths = [self.directory / "legacy.mp3", self.directory / "platform.m4a"]
        for path in paths:
            path.write_bytes(b"same recording")
        local = {**self.track, "provider": "local", "identifier": paths[0].name,
                 "webpage_url": "/api/files/legacy.mp3", "thumbnail": None}
        try:
            library.record_download(paths[0].name, local)
            old = library.track_id(library.TrackInput(**local))
            playlist = self.client.post("/api/library/playlists", json={"name": "merge test"}).json()["id"]
            for metadata in (local, self.track):
                self.client.put("/api/library/favorites", json={"track": metadata, "liked": True}).raise_for_status()
                self.client.post("/api/library/history", json={"track": metadata}).raise_for_status()
                self.client.post(f"/api/library/playlists/{playlist}/tracks", json=metadata).raise_for_status()
            with library.database() as cursor:
                cursor.execute("UPDATE music_tracks SET custom_thumbnail='/api/custom',cover_updated_at=10 WHERE id=%s", (old,))
            library.store_lyrics(paths[0].name, {"text": "[00:01]manual", "source": "本地 LRC"})
            library.record_download(paths[1].name, self.track)
            overview = self.client.get("/api/library").json()
            self.assertEqual(overview["counts"]["downloads"], 1)
            self.assertEqual(overview["counts"]["favorites"], 1)
            self.assertEqual(overview["playlists"][0]["count"], 1)
            row = self.client.get("/api/library/tracks?view=downloads").json()["tracks"][0]
            self.assertEqual((row["provider"], row["play_count"], row["thumbnail"]), ("qq", 2, "/api/custom"))
            self.client.post("/api/library/history", json={"track": local}).raise_for_status()
            library.sync_downloads()
            self.assertEqual(self.client.get("/api/library").json()["counts"]["downloads"], 1)
            self.assertEqual(library.read_lyrics(paths[1].name)["text"], "[00:01]manual")
            self.assertTrue(all(path.exists() for path in paths))
            paths[1].unlink()
            self.assertEqual(self.client.get("/api/library/tracks?view=downloads").json()["tracks"][0]["filename"], paths[0].name)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_same_title_with_different_content_remains_separate(self):
        paths = [self.directory / "studio.mp3", self.directory / "live.mp3"]
        try:
            for index, path in enumerate(paths):
                path.write_bytes(f"different-{index}".encode())
                library.record_download(path.name, {**self.track, "identifier": str(index)})
            self.assertEqual(self.client.get("/api/library").json()["counts"]["downloads"], 2)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_legacy_lyrics_migrate_and_manual_import_survives_reconnection(self):
        path = self.directory / "lyrics.mp3"
        path.write_bytes(b"lyrics recording")
        cached = self.directory / "_lyrics" / (library.hashlib.sha256(path.name.encode()).hexdigest() + ".json")
        cached.parent.mkdir(exist_ok=True)
        cached.write_text(library.json.dumps({"text": "[00:02]old lyrics", "source": "LRCLIB"}), encoding="utf-8")
        try:
            library.record_download(path.name, self.track)
            self.assertEqual(library.read_lyrics(path.name)["text"], "[00:02]old lyrics")
            self.client.put(f"/api/music/lyrics/{path.name}", content=b"[00:03]uploaded lyrics").raise_for_status()
            library.store_lyrics(path.name, {"text": "[00:04]online replacement", "source": "LRCLIB"})
            library.schema_ready = False
            self.assertEqual(self.client.get(f"/api/music/lyrics/{path.name}").json()["lines"][0]["text"], "uploaded lyrics")
        finally:
            path.unlink(missing_ok=True)
            cached.unlink(missing_ok=True)
            cached.parent.rmdir()

    def test_image_upload_validation_overrides_and_restores_cover(self):
        key = self.favorite()
        playlist = self.client.post("/api/library/playlists", json={"name": "image test"}).json()["id"]
        buffer = io.BytesIO()
        Image.new("RGB", (80, 60), "red").save(buffer, "PNG")
        try:
            for endpoint in (f"/api/library/tracks/{key}/cover", f"/api/library/playlists/{playlist}/cover", "/api/library/profile/avatar"):
                self.assertEqual(self.client.post(endpoint, files={"file": ("fake.png", b"invalid", "image/png")}).status_code, 422)
                uploaded = self.client.post(endpoint, files={"file": ("cover.png", buffer.getvalue(), "image/png")})
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                url = uploaded.json().get("thumbnail") or uploaded.json()["avatar"]
                self.assertEqual(self.client.get(url).status_code, 200)
                self.client.delete(endpoint).raise_for_status()
            row = self.client.get("/api/library/tracks?view=favorites").json()["tracks"][0]
            self.assertIsNone(row["custom_thumbnail"])
            self.assertIsNone(self.client.get("/api/library").json()["profile"]["avatar"])
        finally:
            folder = self.directory / "_images"
            for path in folder.glob("*"):
                path.unlink()
            if folder.exists():
                folder.rmdir()

    def test_theme_persists_and_validates_colors_and_background(self):
        theme = media.Theme(mode="dark", background="#181b1f", text="#f4f5f6", accent="#55c7a4").model_dump()
        self.client.put("/api/library/theme", json=theme).raise_for_status()
        library.schema_ready = False
        self.assertEqual(self.client.get("/api/library/theme").json(), theme)
        self.assertEqual(self.client.put("/api/library/theme", json={**theme, "text": "#181b1f"}).status_code, 422)
        self.assertEqual(self.client.put("/api/library/theme", json={**theme, "background_image": "/api/library/images/" + "a" * 32 + ".webp"}).status_code, 422)
        self.client.delete("/api/library/theme").raise_for_status()

    def test_cover_lookup_requires_matching_artist_title_and_duration(self):
        candidates = [music_track for music_track in (
            {**self.track, "artist": "Other", "thumbnail": "https://example.com/wrong-artist.jpg"},
            {**self.track, "duration": 190, "thumbnail": "https://example.com/wrong-version.jpg"},
            {**self.track, "thumbnail": "https://example.com/matched.jpg"},
        )]
        objects = [media.music.Track(**metadata) for metadata in candidates]
        with patch.object(media.music, "search", return_value={"tracks": objects}) as search:
            self.assertEqual(media.matching_cover(self.track), "https://example.com/matched.jpg")
            self.assertIsNone(media.matching_cover({**self.track, "artist": ""}))
            self.assertEqual(search.call_count, 1)


if __name__ == "__main__":
    unittest.main()
