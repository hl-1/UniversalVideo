import io
import importlib.util
import json
import secrets
import tempfile
import unittest
import wave
import zipfile
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.core import blobs, paths, storage
from backend.modules.accounts import service as accounts
from backend.modules.delivery import api, service
from backend.modules.music import sources as music


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        marker = accounts.principal.set(accounts.Principal(1, "test", "super_admin", device_id="11111111-1111-4111-8111-111111111111", client_kind="web"))
        self.addCleanup(accounts.principal.reset, marker)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(paths, "DOWNLOAD_DIR", self.root))
        self.stack.enter_context(patch.object(accounts, "active_worker"))
        self.stack.enter_context(patch.object(blobs, "mutex", side_effect=lambda identity: nullcontext()))
        self.prefix = "videodream:delivery-test:" + secrets.token_hex(8) + ":"
        self.stack.enter_context(patch.object(accounts, "redis_prefix", return_value=self.prefix))
        self.client = accounts.redis_client()
        self.addCleanup(lambda: [self.client.delete(key) for key in self.client.scan_iter(self.prefix + "*")])
        self.records = {}

        def register(kind, identity, payload=None, **kwargs):
            self.records[(kind, identity)] = {"user_id": kwargs.get("uid", accounts.owner_id()), "payload": json.dumps(payload or {}), "kind": kind, "device_id": (payload or {}).get("device_id")}

        def resource(kind, identity, **kwargs):
            row = self.records.get((kind, identity))
            if not row or row["user_id"] != accounts.owner_id():
                raise HTTPException(404, "missing")
            if kwargs.get("check", True): accounts.check_device_resource(row)
            return row

        self.stack.enter_context(patch.object(accounts, "register_resource", side_effect=register))
        self.stack.enter_context(patch.object(accounts, "resource", side_effect=resource))

    def plan(self, **extra):
        return service.save_plan({"destination": "device", "mode": "relay", "filename": "a.mp4", "page": "https://example.com/a", "url": "https://cdn.example.com/a.mp4", **extra})

    def test_same_account_other_device_cannot_read_or_complete_even_after_plan_expiry(self):
        plan = self.plan()
        marker = accounts.principal.set(accounts.Principal(1, "test", "super_admin", device_id="22222222-2222-4222-8222-222222222222", client_kind="web"))
        try:
            with self.assertRaises(HTTPException): service.load(plan["id"])
            self.client.delete(service.key("plan", 1, plan["id"]))
            with self.assertRaises(HTTPException): api.complete(plan["id"], api.CompletionRequest(status="completed", completion_source="filesystem", transferred_bytes=10))
        finally: accounts.principal.reset(marker)

    def response(self, chunks, length=None):
        return SimpleNamespace(headers={"Content-Length": str(length)} if length is not None else {},
            iter_content=lambda size: iter(chunks), close=Mock())

    def test_plan_is_private_and_terminal_updates_cannot_regress(self):
        plan = self.plan()
        self.assertGreater(self.client.ttl(service.key("plan", 1, plan["id"])), 0)
        self.assertLessEqual(self.client.ttl(service.key("plan", 1, plan["id"])), 600)
        service.update(plan, status="completed", completion_source="filesystem")
        self.assertFalse(service.update(plan, status="running", transferred_bytes=10))
        self.assertEqual(service.state(plan)["status"], "completed")
        self.assertLessEqual(self.client.ttl(service.key("state", 1, plan["id"])), 3600)
        marker = accounts.principal.set(accounts.Principal(2, "other", "super_admin"))
        try:
            with self.assertRaises(HTTPException):
                service.load(plan["id"])
        finally:
            accounts.principal.reset(marker)

    def test_attachment_filename_hides_generated_prefix_and_preserves_storage_path(self):
        name = '964ba09f4629.Here For It All.m4a'
        path = storage.path('file', name)
        path.write_bytes(b'audio')
        with patch.object(accounts, 'environment', return_value=''):
            response = storage.media_response(path, download=True)
        from urllib.parse import unquote
        self.assertEqual(response.path, path)
        self.assertIn('Here For It All.m4a', unquote(response.headers['content-disposition']))
        self.assertNotIn('964ba09f4629', response.headers['content-disposition'])
        self.assertTrue(path.is_file())
        self.assertEqual(storage.download_filename('2026. My song.mp3'), '2026. My song.mp3')
        self.assertEqual(storage.download_filename('964ba09f4629.mp3'), '964ba09f4629.mp3')
        self.assertEqual(storage.download_filename('import-964ba09f4629.歌曲.mp3'), '歌曲.mp3')
        with patch.object(accounts, 'environment', return_value='/_protected_downloads'):
            accelerated = storage.media_response(path, download=True)
        self.assertIn(name.replace(' ', '%20'), accelerated.headers['x-accel-redirect'])
        self.assertNotIn('964ba09f4629', accelerated.headers['content-disposition'])

    def test_relay_yields_before_source_finishes_and_stages_no_files(self):
        consumed = []
        def chunks():
            consumed.append(1); yield b"first"
            consumed.append(2); yield b"second"
        response = self.response([], 11)
        response.iter_content = lambda size: chunks()
        stream = service.relay(self.plan(), response)
        self.assertEqual(next(stream), b"first")
        self.assertEqual(consumed, [1])
        self.assertEqual(b"".join(stream), b"second")
        response.close.assert_called_once()
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_truncated_relay_fails_and_closes_upstream(self):
        plan = self.plan()
        response = self.response([b"short"], 100)
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            list(service.relay(plan, response))
        self.assertEqual(service.state(plan)["status"], "failed")
        response.close.assert_called_once()

    def test_cancelled_relay_stops_before_reading_more(self):
        plan = self.plan()
        service.update(plan, status="cancelled")
        response = self.response([b"a", b"b"])
        with self.assertRaises(HTTPException):
            list(service.relay(plan, response))
        response.close.assert_called_once()

    def test_browser_handoff_cannot_claim_local_completion(self):
        plan = self.plan()
        with self.assertRaises(HTTPException) as error:
            api.complete(plan["id"], api.CompletionRequest(status="completed", completion_source="browser"))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(service.state(plan)["status"], "ready")

    def test_expired_plan_still_records_completion_without_requiring_source_url(self):
        plan = self.plan()
        self.client.delete(service.key("plan", 1, plan["id"]))
        result = api.complete(plan["id"], api.CompletionRequest(status="completed", completion_source="filesystem", transferred_bytes=100))
        self.assertEqual(result["status"], "completed")

    def test_expired_redis_state_cannot_overwrite_durable_completion(self):
        plan = self.plan()
        service.update(plan, status="completed", phase="completed", completion_source="filesystem")
        self.client.delete(service.key("plan", 1, plan["id"]), service.key("state", 1, plan["id"]))
        result = api.complete(plan["id"], api.CompletionRequest(status="cancelled"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(self.records[("delivery", plan["id"])]["payload"])["status"], "completed")

    def test_failed_download_cannot_be_confirmed_as_saved_even_after_redis_expires(self):
        plan = self.plan()
        service.update(plan, status="failed", phase="failed", error="incomplete audio")
        payload = api.CompletionRequest(status="completed", completion_source="filesystem", transferred_bytes=100)
        self.assertEqual(api.complete(plan["id"], payload)["status"], "failed")
        self.client.delete(service.key("plan", 1, plan["id"]), service.key("state", 1, plan["id"]))
        self.assertEqual(api.complete(plan["id"], payload)["status"], "failed")
        self.assertEqual(json.loads(self.records[("delivery", plan["id"])]["payload"])["status"], "failed")

    def test_cloud_completion_requires_finished_cloud_task(self):
        plan = self.plan(destination="library", mode="task", task_id="cloud-task")
        accounts.register_resource("download", "cloud-task", {"status": "running"})
        with self.assertRaises(HTTPException):
            api.complete(plan["id"], api.CompletionRequest(status="library_saved"))
        accounts.register_resource("download", "cloud-task", {"status": "finished"})
        self.assertEqual(api.complete(plan["id"], api.CompletionRequest(status="library_saved"))["status"], "library_saved")
        self.assertEqual(service.state(plan)["completion_source"], "server")

    def test_streaming_zip_has_valid_crc_and_central_directory_without_disk_archive(self):
        tracks = [{"title": "same", "artist": "artist", "provider": "netease", "identifier": str(index), "webpage_url": f"https://music.163.com/song?id={index}"} for index in (1, 2)]
        plan = self.plan(mode="batch", tracks=tracks, audio_format="mp3", filename="music.zip")
        with patch.object(music, "parse_url", side_effect=lambda url: music.Track.model_validate(next(track for track in tracks if track["webpage_url"] == url))), \
             patch.object(service, "resolve", side_effect=lambda payload, **kwargs: {"id": "child", "uid": 1, "mode": "relay", "filename": "same.mp3"}), \
             patch.object(api, "media_chunks", side_effect=lambda child: iter([b"a" * 100, b"b" * 100])):
            stream = api.zip_chunks(plan)
            first = next(stream)
            self.assertTrue(first.startswith(b"PK"))
            payload = first + b"".join(stream)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read(archive.namelist()[0]), b"a" * 100 + b"b" * 100)
        self.assertEqual(list(self.root.rglob("*.zip")), [])

    def test_audio_pipe_validates_duration_and_does_not_stage_source(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as source:
            source.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            source.writeframes(b"\0\0" * 32000)
        for expected, succeeds in ((2, True), (200, False)):
            plan = self.plan(audio=True, output_format="mp3", expected_duration=expected, request={"audio_format": "mp3"})
            response = self.response([buffer.getvalue()])
            with patch.object(service, "upstream", return_value=response):
                if succeeds:
                    self.assertGreater(len(b"".join(service.audio_stream(plan))), 1000)
                else:
                    with self.assertRaises(RuntimeError):
                        list(service.audio_stream(plan))
            self.assertEqual(list(self.root.rglob("*")), [])

    def zip_fixture(self, fails):
        tracks = [{"title": f"song {index}", "artist": "artist", "provider": "qq", "identifier": str(index),
                   "webpage_url": f"https://y.qq.com/n/ryqq/songDetail/{index}"} for index in (1, 2, 3)]
        plan = self.plan(mode="batch", tracks=tracks, audio_format="mp3", filename="music.zip")
        attempted = []

        def chunks(child):
            identity = int(child["id"])
            attempted.append(identity)
            yield f"audio {identity}".encode()
            if identity in fails:
                raise RuntimeError("音轨不完整；private upstream credentials must not leak")
            yield b" complete"

        self.stack.enter_context(patch.object(music, "parse_url", side_effect=lambda url: music.Track.model_validate(next(row for row in tracks if row["webpage_url"] == url))))
        self.stack.enter_context(patch.object(service, "resolve", side_effect=lambda request, **kwargs: {
            "id": str(request.url).rsplit("/", 1)[-1], "uid": 1, "mode": "relay", "filename": "song.mp3"}))
        self.stack.enter_context(patch.object(api, "media_chunks", side_effect=chunks))
        return plan, attempted

    def test_zip_skips_a_partially_streamed_failed_song_and_preserves_later_songs(self):
        plan, attempted = self.zip_fixture({2})
        payload = b"".join(api.zip_chunks(plan))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["001. artist - song 1.mp3", "003. artist - song 3.mp3", "下载结果.txt"])
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("003. artist - song 3.mp3"), b"audio 3 complete")
            report = archive.read("下载结果.txt").decode()
            self.assertIn("成功 2 首，失败 1 首", report)
            self.assertIn("song 2", report)
            self.assertNotIn("private upstream", report)
        self.assertEqual(attempted, [1, 2, 3])
        state = service.state(plan)
        self.assertEqual((state["finished"], state["failed"], state["phase"]), ("2", "1", "delivered"))
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])

    def test_zip_with_all_songs_failed_closes_a_valid_report_only_archive(self):
        plan, attempted = self.zip_fixture({1, 2, 3})
        with zipfile.ZipFile(io.BytesIO(b"".join(api.zip_chunks(plan)))) as archive:
            self.assertEqual(archive.namelist(), ["下载结果.txt"])
            self.assertIsNone(archive.testzip())
            self.assertIn("成功 0 首，失败 3 首", archive.read("下载结果.txt").decode())
        self.assertEqual(attempted, [1, 2, 3])
        self.assertEqual(service.state(plan)["finished"], "0")
        self.assertEqual(service.state(plan)["failed"], "3")
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])

    def test_zip_cancellation_is_not_treated_as_an_individual_song_failure(self):
        plan, _ = self.zip_fixture(set())

        def cancelled(child):
            yield b"partial"
            service.update(plan, status="cancelled", phase="cancelled")
            raise RuntimeError("cancelled source")

        with patch.object(api, "media_chunks", side_effect=cancelled), self.assertRaises(HTTPException):
            b"".join(api.zip_chunks(plan))
        self.assertEqual(service.state(plan)["status"], "cancelled")
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])

    def test_zip_output_failure_is_fatal_instead_of_swallowing_a_started_entry(self):
        plan, _ = self.zip_fixture(set())
        with patch.object(zipfile._ZipWriteFile, "write", side_effect=OSError("output failed")), self.assertRaisesRegex(OSError, "output failed"):
            b"".join(api.zip_chunks(plan))
        self.assertEqual(service.state(plan)["status"], "failed")

    def test_zip_releases_each_compatibility_task_before_preparing_the_next_song(self):
        plan, _ = self.zip_fixture(set())
        released = []

        def resolve(request, **kwargs):
            identity = str(request.url).rsplit("/", 1)[-1]
            self.assertEqual(released, [str(index) for index in range(1, int(identity))])
            return {"id": identity, "uid": 1, "mode": "task", "task_id": identity, "filename": "song.mp3"}

        with patch.object(service, "resolve", side_effect=resolve), patch.object(service, "cleanup_task", side_effect=lambda identity, uid: released.append(identity)):
            payload = b"".join(api.zip_chunks(plan))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), 3)
        self.assertEqual(released, ["1", "2", "3"])


class BlobTests(unittest.TestCase):
    def test_offline_migration_keeps_independent_backups_and_can_restore_independent_files(self):
        spec = importlib.util.spec_from_file_location("deduplicate_files", paths.ROOT_DIR / "scripts" / "deduplicate-files.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory() as work, patch.object(paths, "DOWNLOAD_DIR", Path(work) / "downloads"), patch.object(blobs, "mutex", side_effect=lambda identity: nullcontext()):
            first, second = storage.path("file", "a.mp3", uid=1), storage.path("file", "b.mp3", uid=2)
            first.write_bytes(b"shared fixture"); second.write_bytes(first.read_bytes())
            cursor = SimpleNamespace(execute=Mock(), fetchall=lambda: [{"resource_key": "a.mp3", "user_id": 1}, {"resource_key": "b.mp3", "user_id": 2}])
            manifest, backup = Path(work) / "manifest.json", Path(work) / "backup"
            with patch.object(migration, "database", return_value=nullcontext(cursor)), patch("builtins.print"), patch("sys.argv", ["deduplicate-files", "--manifest", str(manifest), "--apply", "--backup", str(backup)]):
                migration.main()
            self.assertEqual(first.stat().st_ino, second.stat().st_ino)
            saved = json.loads(manifest.read_text())
            self.assertNotEqual(Path(saved["files"][0]["backup"]).stat().st_ino, first.stat().st_ino)
            with patch("builtins.print"), patch("sys.argv", ["deduplicate-files", "--manifest", str(manifest), "--rollback"]):
                migration.main()
            self.assertNotEqual(first.stat().st_ino, second.stat().st_ino)
            self.assertEqual(first.read_bytes(), b"shared fixture")
            self.assertEqual(second.read_bytes(), b"shared fixture")

    def test_exact_bytes_share_one_inode_and_user_deletion_preserves_other_alias(self):
        with tempfile.TemporaryDirectory() as work, patch.object(paths, "DOWNLOAD_DIR", Path(work)), patch.object(blobs, "mutex", side_effect=lambda identity: nullcontext()):
            a, b = storage.path("file", "a.mp3", uid=1), storage.path("file", "b.mp3", uid=2)
            a.write_bytes(b"same audio bytes"); b.write_bytes(a.read_bytes())
            digest, size = blobs.publish(a)
            self.assertEqual(blobs.publish(b), (digest, size))
            self.assertEqual((a.stat().st_dev, a.stat().st_ino), (b.stat().st_dev, b.stat().st_ino))
            a.unlink()
            self.assertEqual(b.read_bytes(), b"same audio bytes")
            self.assertEqual(storage.path("file", "b.mp3", uid=1).is_file(), False)

    def test_same_size_corrupted_pool_is_not_reused(self):
        with tempfile.TemporaryDirectory() as work, patch.object(paths, "DOWNLOAD_DIR", Path(work)), patch.object(blobs, "mutex", side_effect=lambda identity: nullcontext()):
            source = storage.path("file", "a.mp3", uid=1)
            source.write_bytes(b"good")
            digest, _ = blobs.publish(source)
            pool = Path(work) / "_blobs" / "sha256" / digest[:2] / digest
            pool.write_bytes(b"evil")
            other = storage.path("file", "b.mp3", uid=2); other.write_bytes(b"good")
            with self.assertRaises(ValueError):
                blobs.publish(other)
            self.assertEqual(other.read_bytes(), b"good")
