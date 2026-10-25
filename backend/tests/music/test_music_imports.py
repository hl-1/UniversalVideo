import io
import json
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from backend import application
from backend.core import paths
from backend.modules.video import tasks as video_tasks
from backend.modules.accounts import service as accounts
from backend.infrastructure import ai
from backend.modules.music import sources as music
from backend.modules.music import imports
from backend.tests.support import domain_app


class MusicImportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        marker = accounts.principal.set(None)
        self.addCleanup(accounts.principal.reset, marker)
        self.stack.enter_context(patch.object(imports.executor, "submit"))
        self.stack.enter_context(patch.object(video_tasks.music_batch_executor, "submit"))
        self.stack.enter_context(patch.object(imports, "SEARCH_INTERVAL", 0))
        imports.jobs.clear()
        video_tasks.music_batches.clear()
        self.addCleanup(imports.jobs.clear)
        self.addCleanup(video_tasks.music_batches.clear)
        self.client = TestClient(domain_app(application.app))

    def text(self, text="晴天 - 周杰伦\nHello - Adele"):
        response = self.client.post("/api/music/imports/text", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return imports.jobs[response.json()["import_id"]]

    def track(self, title="晴天", artist="周杰伦", identifier="1", duration=180):
        return music.Track(title=title, artist=artist, duration=duration, provider="netease", identifier=identifier,
                           webpage_url=f"https://music.163.com/song?id={identifier}")

    def result(self, tracks, available=True):
        return {"tracks": tracks, "sources": [{"provider": "netease", "available": available}], "page": 1, "has_more": False}

    def test_text_deduplicates_preserves_order_and_hyphenated_titles(self):
        job = self.text("1. 晴天 - 周杰伦\n晴天 - 周杰伦\nAnti-Hero - Taylor Swift\n歌名\n")
        self.assertEqual([(row["title"], row["artist"]) for row in job["rows"]], [("晴天", "周杰伦"), ("Anti-Hero", "Taylor Swift"), ("歌名", "")])
        self.assertEqual(job["duplicates"], 1)

    def test_artist_first_and_tab_separated_text(self):
        response = self.client.post("/api/music/imports/text", json={"text": "Adele\tHello", "order": "artist_title"})
        self.assertEqual(response.json()["rows"][0]["title"], "Hello")
        self.assertEqual(response.json()["rows"][0]["artist"], "Adele")

    def test_empty_and_oversized_lists_do_not_create_jobs(self):
        for text in (" \n\n", "\n".join(f"song-{i}" for i in range(101))):
            response = self.client.post("/api/music/imports/text", json={"text": text})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(imports.jobs, {})

    def test_long_image_is_split_without_square_crop(self):
        picture = Image.new("RGB", (600, 3500), "white")
        picture.putpixel((300, 3499), (255, 0, 0))
        data = io.BytesIO()
        picture.save(data, "PNG")
        tiles = imports.image_tiles(data.getvalue())
        import base64
        decoded = [Image.open(io.BytesIO(base64.b64decode(tile.split(",", 1)[1]))) for tile in tiles]
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0].size, (600, 1800))
        self.assertEqual(decoded[1].getpixel((300, decoded[1].height - 1)), (255, 0, 0))

    def test_invalid_images_and_missing_vision_configuration(self):
        with patch.object(ai, "load_config", return_value={"api_key": "test", "model": "vision", "base_url": "https://example.com"}):
            response = self.client.post("/api/music/imports/screenshots", files=[("files", ("bad.png", b"not an image", "image/png"))])
            self.assertEqual(response.status_code, 422)
        with patch.object(ai, "load_config", side_effect=RuntimeError("请配置截图识别模型")):
            response = self.client.post("/api/music/imports/screenshots", files=[("files", ("a.png", b"data", "image/png"))])
            self.assertEqual(response.status_code, 422)
        self.assertEqual(imports.jobs, {})

    def test_multiple_screenshots_merge_overlap_and_preserve_partial_failure(self):
        job = imports.create_job("recognizing")
        songs = [imports.SongInput(title="晴天", artist="周杰伦")]
        with patch.object(imports, "recognize", side_effect=[songs, RuntimeError("服务暂不可用"), songs]):
            imports.run_recognition(job, [["a"], ["b"], ["c"]], {})
        self.assertEqual((job["status"], len(job["rows"]), job["duplicates"]), ("draft", 1, 1))
        self.assertEqual(len(job["warnings"]), 1)

    def test_vision_request_uses_selected_model_and_rejects_bad_output(self):
        client = self.stack.enter_context(patch.object(imports, "OpenAI"))
        create = client.return_value.chat.completions.create
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"songs": [{"title": "Hello", "artist": "Adele"}]}'))])
        songs = imports.recognize(["data:image/png;base64,test"], {"api_key": "test", "base_url": "https://example.com", "model": "qwen-vision"})
        self.assertEqual(songs[0].artist, "Adele")
        self.assertEqual(create.call_args.kwargs["model"], "qwen-vision")
        self.assertEqual(create.call_args.kwargs["messages"][1]["content"][1]["type"], "image_url")
        create.return_value.choices[0].message.content = '{"songs": [{"title": ""}]}'
        with self.assertRaisesRegex(RuntimeError, "有效的歌曲名单"):
            imports.recognize(["image"], {"api_key": "test", "model": "vision"})

    def test_exact_match_selects_correct_artist_and_version(self):
        tracks = [self.track(artist="翻唱歌手", identifier="2"), self.track(title="晴天 (Live)", identifier="3"), self.track()]
        with patch.object(music, "search", return_value=self.result(tracks)):
            result = imports.match_song({"title": "晴天", "artist": "周杰伦", "uncertain": False}, "all")
        self.assertEqual(result["selected_id"], "netease:1")
        self.assertEqual(result["status"], "matched")

    def test_missing_artist_uncertain_text_and_different_recordings_need_confirmation(self):
        cases = [({"title": "晴天", "artist": "", "uncertain": False}, [self.track()]),
                 ({"title": "晴天", "artist": "周杰伦", "uncertain": True}, [self.track()]),
                 ({"title": "晴天", "artist": "周杰伦", "uncertain": False}, [self.track(), self.track(identifier="2", duration=240)])]
        for song, tracks in cases:
            with patch.object(music, "search", return_value=self.result(tracks)):
                result = imports.match_song(song, "all")
            self.assertIsNone(result["selected_id"])
            self.assertEqual(result["status"], "check")

    def test_platform_outage_is_distinct_from_no_match(self):
        song = {"title": "song", "artist": "", "uncertain": False}
        for available, status in [(True, "not_found"), (False, "error")]:
            with patch.object(music, "search", return_value=self.result([], available)):
                self.assertEqual(imports.match_song(song, "all")["status"], status)

    def test_search_keeps_rows_after_one_failure_and_reports_progress(self):
        job = self.text()
        job["status"] = "searching"
        def search(q, **kwargs):
            if q.startswith("Hello"):
                raise RuntimeError("temporary")
            return self.result([self.track()])
        with patch.object(music, "search", side_effect=search):
            imports.run_search(job, "all")
        self.assertEqual((job["status"], job["progress"]), ("finished", 100))
        self.assertEqual([row["status"] for row in job["rows"]], ["matched", "error"])

    def test_cancel_then_restart_ignores_old_search_completion(self):
        job = self.text("晴天 - 周杰伦")
        job["status"] = "searching"
        old_rows = job["rows"]
        def finish_old(*args):
            self.client.post(f"/api/music/imports/{job['import_id']}/cancel")
            response = self.client.post(f"/api/music/imports/{job['import_id']}/search", json={"songs": [{"title": "Hello", "artist": "Adele"}]})
            self.assertEqual(response.status_code, 200)
            return {"status": "matched", "selected_id": "old", "candidates": []}
        with patch.object(imports, "match_song", side_effect=finish_old):
            imports.run_search(job, "all")
        self.assertEqual((job["status"], job["progress"], job["rows"][0]["title"]), ("searching", 0, "Hello"))
        self.assertIsNone(old_rows[0]["selected_id"])

    def test_cancelled_recognition_cannot_overwrite_new_search(self):
        job = imports.create_job("recognizing")
        def recognition(*args):
            self.client.post(f"/api/music/imports/{job['import_id']}/cancel")
            self.client.post(f"/api/music/imports/{job['import_id']}/search", json={"songs": [{"title": "Hello"}]})
            return [imports.SongInput(title="Old")]
        with patch.object(imports, "recognize", side_effect=recognition):
            imports.run_recognition(job, [["image"]], {})
        self.assertEqual((job["status"], job["rows"][0]["title"]), ("searching", "Hello"))

    def test_cancelled_queued_workers_do_not_process_restarted_job(self):
        job = self.text("Old")
        old_stop, old_rows = job["stop"], job["rows"]
        old_stop.set()
        job["stop"] = threading.Event()
        job["status"] = "searching"
        job["rows"] = imports.make_rows([imports.SongInput(title="New")])
        with patch.object(imports, "recognize") as recognize, patch.object(imports, "match_song") as search:
            imports.run_recognition(job, [["image"]], {}, old_stop)
            imports.run_search(job, "all", old_stop, old_rows)
            recognize.assert_not_called()
            search.assert_not_called()
        self.assertEqual(job["rows"][0]["title"], "New")

    def test_old_import_cloud_download_is_retired_after_token_expiry(self):
        job = self.text("晴天 - 周杰伦")
        with patch.object(music, "search", return_value=self.result([self.track()])):
            imports.run_search(job, "all")
        with music.lock:
            music.sessions.clear()
        row = job["rows"][0]
        response = self.client.post(f"/api/music/imports/{job['import_id']}/download", json={"selections": [{"row_id": row["row_id"], "candidate_id": "netease:1"}], "audio_format": "mp3"})
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(video_tasks.music_batches, {})

    def test_download_rejects_candidate_outside_results_and_double_selection(self):
        job = self.text("晴天 - 周杰伦")
        with patch.object(music, "search", return_value=self.result([self.track()])):
            imports.run_search(job, "all")
        selection = {"row_id": job["rows"][0]["row_id"], "candidate_id": "netease:1"}
        for selections in [[selection | {"candidate_id": "netease:999"}], [selection, selection]]:
            response = self.client.post(f"/api/music/imports/{job['import_id']}/download", json={"selections": selections})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(video_tasks.music_batches, {})

    def test_stored_task_restores_and_owner_is_checked(self):
        job = self.text("Hello")
        payload = imports.snapshot(job)
        job["user_id"] = 1
        accounts.principal.set(accounts.Principal(2, "other", "user"))
        with patch.object(accounts, "resource", return_value={"payload": json.dumps(payload), "user_id": 1}):
            with self.assertRaises(HTTPException) as failure:
                imports.get_job(job["import_id"])
            self.assertEqual(failure.exception.status_code, 404)
            accounts.principal.set(accounts.Principal(1, "owner", "user"))
            imports.jobs.clear()
            restored = imports.get_job(job["import_id"])
            self.assertEqual(restored["rows"][0]["title"], "Hello")

    def test_auth_path_checks_import_jobs_but_allows_creation_paths(self):
        with patch.object(accounts, "resource") as resource:
            accounts.check_path("/api/music/imports/text")
            accounts.check_path("/api/music/imports/screenshots")
            resource.assert_not_called()
            accounts.check_path("/api/music/imports/123456789abc/download")
            resource.assert_called_once_with("music_import", "123456789abc")


class AiModalityTests(unittest.TestCase):
    def test_text_and_vision_use_separate_connections(self):
        marker = accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        self.addCleanup(accounts.principal.reset, marker)
        config = accounts.DEFAULT_SETTINGS | {"ai_api_key": "test", "ai_model": "deepseek", "ai_vision_model": "qwen",
                                              "ai_vision_api_key": "vision-key", "ai_vision_base_url": "https://vision.example/v1"}
        with patch.object(accounts, "settings", return_value=config):
            text, vision = ai.load_config("text"), ai.load_config("vision")
        self.assertEqual((text["model"], vision["model"]), ("deepseek", "qwen"))
        self.assertEqual(vision["api_key"], "vision-key")
        self.assertEqual(vision["base_url"], "https://vision.example/v1")
        self.assertNotEqual(text["api_key"], vision["api_key"])

    def test_missing_vision_model_never_falls_back_to_text_model(self):
        marker = accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        self.addCleanup(accounts.principal.reset, marker)
        with patch.object(accounts, "settings", return_value=accounts.DEFAULT_SETTINGS | {"ai_api_key": "test", "ai_vision_api_key": "vision-key"}):
            with self.assertRaisesRegex(RuntimeError, "截图识别模型"):
                ai.load_config("vision")
            self.assertEqual(ai.load_config("text")["model"], accounts.DEFAULT_SETTINGS["ai_model"])


if __name__ == "__main__":
    unittest.main()


class ParallelImportTests(unittest.TestCase):
    def test_ten_workers_preserve_order_and_context(self):
        rows = [{"title": str(i), "artist": ""} for i in range(25)]
        job = {"status": "searching", "stop": threading.Event(), "rows": rows}
        gate, guard = threading.Barrier(10), threading.Lock()
        active = peak = 0
        from contextvars import ContextVar
        context = ContextVar("test_import_context", default=None)
        marker = context.set("owner")
        def match(row, source):
            nonlocal active, peak
            self.assertEqual(context.get(), "owner")
            with guard:
                active += 1
                peak = max(active, peak)
            try:
                if int(row["title"]) < 10:
                    gate.wait(timeout=3)
                time.sleep((25 - int(row["title"])) / 10000)
                return {"status": "matched", "result": row["title"]}
            finally:
                with guard: active -= 1
        try:
            with patch.object(imports, "persist"), patch.object(imports, "match_song", side_effect=match):
                imports.run_search(job, "all")
        finally:
            context.reset(marker)
        self.assertEqual(peak, 10)
        self.assertEqual(job["status"], "finished")
        self.assertEqual([row["result"] for row in rows], [str(i) for i in range(25)])

    def test_timing_freezes_and_retry_keeps_total(self):
        job = {"status": "searching"}
        with patch.object(imports.time, "time", return_value=100): imports.timing_phase(job, "search")
        with patch.object(imports.time, "time", return_value=105): imports.timing_phase(job, None)
        imports.tick_timing(job, 200)
        self.assertEqual(job["timing"]["total_seconds"], 5)
        self.assertEqual(job["timing"]["round_seconds"], 5)
        with patch.object(imports.time, "time", return_value=200): imports.timing_phase(job, "search")
        imports.tick_timing(job, 203)
        self.assertEqual(job["timing"]["total_seconds"], 8)
