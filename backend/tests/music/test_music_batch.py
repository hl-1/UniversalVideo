import io
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import application
from backend.infrastructure import audio
from backend.modules.music import library
from backend.modules.music import batch as music_batch
from backend.core import paths, storage
from backend.modules.video import downloads as video_downloads
from backend.modules.video import tasks as video_tasks
from backend.modules.video import schemas as video_schemas
from fastapi import HTTPException
from backend.modules.music import sources as music
from backend.tests.support import domain_app


class MusicBatchTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.work.name)
        self.directory = self.storage_root / "users" / "1" / "files"
        self.directory.mkdir(parents=True)
        owner_patch = patch.object(storage, "owner", side_effect=lambda uid=None: uid or 1)
        owner_patch.start()
        self.addCleanup(owner_patch.stop)
        self.directory_patch = patch.object(paths, 'DOWNLOAD_DIR', self.storage_root)
        self.directory_patch.start()
        self.executor_patch = patch.object(video_tasks.music_batch_executor, "submit")
        self.submit = self.executor_patch.start()
        self.interval_patch = patch.object(video_tasks, 'MUSIC_BATCH_INTERVAL', 0)
        self.interval_patch.start()
        self.client = TestClient(domain_app(application.app))
        with video_tasks.music_batches_lock:
            video_tasks.music_batches.clear()

    def tearDown(self):
        self.interval_patch.stop()
        self.executor_patch.stop()
        self.directory_patch.stop()
        self.work.cleanup()
        with video_tasks.music_batches_lock:
            video_tasks.music_batches.clear()

    def track(self, identifier):
        return music._register(music.Track(title="../Same: Song?", artist="Artist/Name", duration=180,
                                          webpage_url=f"https://music.163.com/song?id={identifier}",
                                          provider="netease", identifier=str(identifier)))

    def create(self, tracks):
        # Construct a pre-upgrade batch; public creation is intentionally retired.
        response = music_batch._create_legacy_music_batch(video_schemas.MusicBatchRequest(
            tracks=[{"token": track.token, "webpage_url": track.webpage_url} for track in tracks], audio_format="mp3"))
        return video_tasks.music_batches[response["batch_id"]]

    def finish(self, task):
        filename = f"{task.task_id}.mp3"
        (self.directory / filename).write_bytes(b"test-audio")
        video_tasks._set_task(task, status="finished", progress=100, filename=filename)

    def test_continues_after_failure_and_archives_only_successful_unique_names(self):
        batch = self.create([self.track(1), self.track(2), self.track(3)])
        attempted = []

        def run(task):
            self.assertEqual(task.status, "running")
            self.assertFalse(any(entry["task"].status == "running" and entry["task"] is not task for entry in batch["items"]))
            attempted.append(task.music_track.identifier)
            if task.music_track.identifier == "2":
                video_tasks._set_task(task, status="failed", error="source unavailable")
            else:
                self.finish(task)

        with patch.object(video_downloads, '_run_audio_download', side_effect=run):
            music_batch._run_music_batch(batch)
        status = self.client.get(f"/api/music/batches/{batch['batch_id']}").json()
        self.assertEqual(attempted, ["1", "2", "3"])
        self.assertEqual((status["finished"], status["failed"], status["progress"]), (2, 1, 100))
        response = self.client.get(status["download_url"])
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(len(set(archive.namelist())), 2)
            for name in archive.namelist():
                self.assertFalse(any(character in name for character in '/\\:*?'))
                self.assertEqual(archive.read(name), b"test-audio")
        self.assertEqual(list((self.directory / "_batches").glob("*.zip")), [])

    def test_playlist_only_receives_successes_and_retries_without_redownload(self):
        batch = self.create([self.track(81), self.track(82)])
        batch['playlist_id'] = 'test-playlist'
        def run(task):
            if task.music_track.identifier == '81':
                self.finish(task)
            else:
                video_tasks._set_task(task, status='failed', error='source unavailable')
        with patch.object(video_downloads, '_run_audio_download', side_effect=run), patch.object(library, 'add_to_playlist', side_effect=RuntimeError('temporarily unavailable')) as add:
            music_batch._run_music_batch(batch)
        self.assertEqual(add.call_count, 1)
        self.assertTrue(batch['items'][0]['playlist_pending'])
        response = music_batch._retry_legacy_music_batch(batch['batch_id'])
        self.assertEqual(response['playlist_id'], 'test-playlist')
        with patch.object(video_downloads, '_run_audio_download', side_effect=self.finish) as download, patch.object(library, 'add_to_playlist') as add:
            music_batch._run_music_batch(batch)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(add.call_count, 2)
        self.assertFalse(batch['items'][0]['playlist_pending'])

    def test_cancel_allows_current_song_to_finish_and_skips_remaining(self):
        batch = self.create([self.track(4), self.track(5), self.track(6)])
        started, release = threading.Event(), threading.Event()

        def run(task):
            started.set()
            release.wait(3)
            self.finish(task)

        with patch.object(video_downloads, '_run_audio_download', side_effect=run) as download:
            worker = threading.Thread(target=music_batch._run_music_batch, args=(batch,))
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                cancelled = self.client.post(f"/api/music/batches/{batch['batch_id']}/cancel").json()
                self.assertEqual(cancelled["status"], "cancelling")
                self.assertEqual(cancelled["cancelled"], 2)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(download.call_count, 1)
        result = music_batch.music_batch_status(batch["batch_id"])
        self.assertEqual((result["status"], result["finished"], result["cancelled"]), ("cancelled", 1, 2))
        self.assertIsNotNone(result["download_url"])

    def test_cancel_before_start_makes_no_download_requests(self):
        batch = self.create([self.track(7)])
        music_batch.cancel_music_batch(batch["batch_id"])
        with patch.object(video_downloads, '_run_audio_download') as run:
            music_batch._run_music_batch(batch)
        run.assert_not_called()
        self.assertEqual(music_batch.music_batch_status(batch["batch_id"])["cancelled"], 1)

    def test_retry_uses_validated_snapshot_after_search_token_expiry(self):
        first, second = self.track(8), self.track(9)
        batch = self.create([first, second])
        self.finish(batch["items"][0]["task"])
        video_tasks._set_task(batch["items"][1]["task"], status="failed", error="temporary error")
        batch["status"] = "finished"
        original_id = batch["items"][1]["task"].task_id
        with music.lock:
            music.sessions[second.token].expires_at = time.time() - 1
        music_batch._retry_legacy_music_batch(batch['batch_id'])
        self.assertNotEqual(batch["items"][1]["task"].task_id, original_id)
        with patch.object(video_downloads, '_run_audio_download', side_effect=self.finish) as run:
            music_batch._run_music_batch(batch)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].music_track.identifier, "9")
        self.assertEqual(music_batch.music_batch_status(batch["batch_id"])["finished"], 2)

    def test_audio_worker_uses_snapshot_without_revalidating_expired_token(self):
        track = self.track(13)
        batch = self.create([track])
        task = batch["items"][0]["task"]
        with patch.object(music, "get_track", side_effect=AssertionError("expired token was reused")), \
                patch.object(music, "resolve", return_value=("https://cdn.example.com/song.mp3", {}, "test")), \
                patch.object(audio, 'download_audio', return_value=Path("song.mp3")):
            music_batch._run_music_batch(batch)
        self.assertEqual(task.status, "finished")

    def test_missing_audio_file_fails_archive_and_cleans_temporary_zip(self):
        batch = self.create([self.track(14)])
        video_tasks._set_task(batch["items"][0]["task"], status="finished", filename="missing.mp3")
        batch["status"] = "finished"
        response = self.client.get(f"/api/music/batches/{batch['batch_id']}/archive")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(list((self.directory / "_batches").glob("*.zip")), [])

    def test_expired_mismatched_empty_and_oversized_requests_do_not_create_tasks(self):
        track = self.track(10)
        cases = [[], [{"token": "expired", "webpage_url": track.webpage_url}],
                 [{"token": track.token, "webpage_url": "https://music.163.com/song?id=11"}],
                 [{"token": track.token, "webpage_url": track.webpage_url}] * 101]
        for items, code in zip(cases, (422, 410, 410, 422)):
            with self.subTest(code=code):
                response = self.client.post("/api/music/batches", json={"tracks": items})
                self.assertEqual(response.status_code, code)
        self.submit.assert_not_called()
        self.assertEqual(video_tasks.music_batches, {})

    def test_deduplicates_same_platform_song_and_rejects_concurrent_batch(self):
        track = self.track(12)
        batch = self.create([track, track])
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(self.client.post("/api/music/batches", json={"tracks": [{"token": track.token, "webpage_url": track.webpage_url}]}).status_code, 410)
        with self.assertRaises(HTTPException) as error:
            music_batch._create_legacy_music_batch(video_schemas.MusicBatchRequest(tracks=[{"token": track.token, "webpage_url": track.webpage_url}]))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.client.get(f"/api/music/batches/{batch['batch_id']}/archive").status_code, 409)


class MusicSearchPaginationTests(unittest.TestCase):
    def test_native_search_passes_page_and_uses_separate_cache_entries(self):
        with music.lock:
            music.search_cache.clear()
        with patch.object(music, "_json", return_value={}) as fetch:
            music._search("qq", "page-test", 1)
            music._search("qq", "page-test", 2)
            self.assertEqual(fetch.call_args.kwargs["params"]["p"], 2)
            self.assertEqual(fetch.call_count, 2)
            music._search("qq", "page-test", 2)
            self.assertEqual(fetch.call_count, 2)
            music._search("netease", "page-test", 3)
            self.assertEqual(fetch.call_args.kwargs["data"]["offset"], 40)
            music._search("kugou", "page-test", 4)
            self.assertEqual(fetch.call_args.kwargs["params"]["page"], 4)

    def test_pagination_reports_more_results_and_validates_page_limits(self):
        client = TestClient(domain_app(application.app))
        track = music.Track(title="song", webpage_url="https://music.163.com/song?id=1", provider="netease", identifier="1")
        with patch.object(music, "_search", return_value=[track] * music.SEARCH_PAGE_SIZE):
            result = client.get("/api/music/search", params={"q": "artist", "source": "netease", "page": 2}).json()
            self.assertEqual(result["page"], 2)
            self.assertTrue(result["has_more"])
            self.assertFalse(client.get("/api/music/search", params={"q": "artist", "page": 20}).json()["has_more"])
        self.assertEqual(client.get("/api/music/search", params={"q": "artist", "page": 0}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
