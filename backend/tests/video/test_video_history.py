from __future__ import annotations

from urllib.parse import quote
import json
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from fastapi import HTTPException
import requests
import redis

from backend.modules.video import parse_cache as video_parse_cache
from backend.core import paths, storage
from backend.modules.video import parsing as video_parsing
from backend.modules.video import preview as video_preview
from backend.modules.video import schemas as video_schemas
from backend.modules.video import tasks as video_tasks
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.video import cache as video_cache
from backend.modules.video import history as video_history
from backend.modules.video import store as video_store
from backend.tests.accounts import test_accounts as fixtures
from backend.tests.video.test_preview import FakeUpstreamResponse


class VideoCacheTests(unittest.TestCase):
    def test_paired_stream_uses_earliest_media_expiry(self):
        with patch.object(video_cache.time, "time", return_value=2000000000):
            video = quote("https://cdn.example.com/video?expire=2000000500", safe="")
            audio = quote("https://cdn.example.com/audio?expires=2000000080000", safe="")
            self.assertEqual(video_cache.parse_ttl({"formats": [{"format_id": f"sniffpair:{video}|{audio}"}]}), 65)
            self.assertEqual(video_cache.parse_ttl({"formats": [{"format_id": f"sniffpair:{audio}|{video}"}]}), 65)

    def test_expired_stream_cannot_extend_cache_and_unknown_expiry_is_bounded(self):
        with patch.object(video_cache.time, "time", return_value=2000000000):
            identifier = "sniff:" + quote("https://cdn.example.com/video?expire=1999999999", safe="")
            self.assertEqual(video_cache.parse_ttl({"formats": [{"format_id": identifier}]}), 0)
            self.assertEqual(video_cache.parse_ttl({"formats": [{"format_id": "best"}]}), 600)


class VideoHistoryIntegrationTests(fixtures.AccountIntegrationTests):
    url = "https://www.douyin.com/video/123456789"
    media = "https://cdn.example.com/video.mp4?expire=4102444800&signature=private"

    def parse(self, client=None):
        result = video_schemas.ParseResponse(title="持久化测试视频", webpage_url=self.url, duration=90,
            formats=[video_schemas.FormatInfo(format_id="sniff:" + quote(self.media, safe=""), label="MP4 高清", ext="mp4")])
        with patch.object(video_parsing, 'parse_video', return_value=result):
            response = (client or self.a).post("/api/parse", json={"url": self.url})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def summary(self, record_id):
        response = self.a.post("/api/summaries", json={"url": self.url, "parse_record_id": record_id})
        self.assertEqual(response.status_code, 200, response.text)
        task = video_tasks.summary_tasks[response.json()["task_id"]]
        segments = [video_schemas.SubtitleSegment(start=0, timestamp="00:00", text="字幕内容")]
        result = video_schemas.SummaryResult(title="测试总结", webpage_url=self.url, language="zh", source="字幕",
            summary_markdown="测试摘要", transcript=segments)
        video_tasks._set_summary_task(task, status="finished", progress=100, result=result, transcript=segments)
        return task

    def finish_map(self, task_id):
        task = next(task for task in video_tasks.mind_map_tasks.values() if task.task_id == task_id)
        result = video_schemas.MindMapResult(title="测试导图", generated_at="2026-10-11T00:00:00Z",
            nodes=[video_schemas.MindMapNode(id="node-1", title="节点", timestamp=0, segment_ids=[0], children=[])])
        video_tasks._set_mind_map_task(task, status="finished", progress=100, result=result)
        return task

    def test_parse_history_is_durable_and_does_not_store_media_signatures(self):
        parsed = self.parse()
        record_id = parsed["record_id"]
        video_parse_cache.sniff_cache.clear()
        detail = self.a.get(f"/api/videos/records/{record_id}").json()
        self.assertEqual(detail["result"]["title"], "持久化测试视频")
        self.assertEqual(detail["result"]["formats"][0]["format_id"], "saved:0")
        self.assertNotIn("signature", json.dumps(detail))
        self.assertEqual(self.b.get(f"/api/videos/records/{record_id}").status_code, 404)
        self.assertEqual(self.b.get("/api/videos/records").json()["total"], 0)
        self.assertEqual(self.a.get("/api/videos/records?q=持久化&platform=抖音&status=finished").json()["total"], 1)

    def test_failed_parse_is_saved_and_recent_links_survive_logout(self):
        with patch.object(video_parsing, 'parse_video', side_effect=HTTPException(422, "解析失败")):
            response = self.a.post("/api/parse", json={"url": self.url, "force": True})
        self.assertEqual(response.status_code, 422)
        record_id = response.json()["detail"]["record_id"]
        detail = self.a.get(f"/api/videos/records/{record_id}").json()
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error"], "解析失败")
        self.a.post("/api/auth/logout").raise_for_status()
        again = self.client("user_a", "Test-user-123!")
        self.assertEqual(again.get("/api/videos/recent-links").json()["items"][0]["url"], self.url)

    def test_recent_links_migrate_deduplicate_limit_and_clear_independently(self):
        self.parse()
        links = [f"https://example.com/video/{index}" for index in range(5)]
        response = self.a.post("/api/videos/recent-links/migrate", json={"links": links})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["url"], self.url)
        self.a.delete("/api/videos/recent-links/" + items[0]["id"]).raise_for_status()
        self.assertEqual(len(self.a.get("/api/videos/recent-links").json()["items"]), 4)
        self.a.delete("/api/videos/recent-links").raise_for_status()
        self.assertEqual(self.a.get("/api/videos/recent-links").json()["items"], [])
        self.assertEqual(self.a.get("/api/videos/records").json()["total"], 1)

    def test_view_restores_and_rejects_foreign_or_nonfinite_state(self):
        record_id = self.parse()["record_id"]
        summary = self.summary(record_id)
        map_id = self.a.post(f"/api/summaries/{summary.task_id}/mind-map").json()["task_id"]
        self.finish_map(map_id)
        view = {"tab": "mind-map", "summary_task_id": summary.task_id, "mind_map_id": map_id, "playback_time": 42,
                "map_view": {"scale": 1.2, "x": 0.1, "y": 0.3, "collapsed": ["node-1", "unknown"],
                             "layout": "right", "resolved_layout": "right", "framing_valid": True}}
        path = f"/api/videos/records/{record_id}/view"
        self.a.put(path, json=view).raise_for_status()
        restored = self.a.get(f"/api/videos/records/{record_id}").json()["view"]
        self.assertEqual(restored["map_view"]["collapsed"], ["node-1"])
        self.assertEqual(restored["map_view"]["layout"], 'right')
        self.assertEqual(restored["map_view"]["resolved_layout"], 'right')
        self.assertEqual(restored["playback_time"], 42)
        self.assertEqual(self.a.get("/api/videos/last-record").json()["record_id"], record_id)
        self.assertEqual(self.b.put(path, json=view).status_code, 404)
        self.assertEqual(self.a.put(path, json=view | {"summary_task_id": "missing"}).status_code, 422)
        self.assertEqual(self.a.put(path, content='{"playback_time": NaN}', headers={"Content-Type": "application/json"}).status_code, 422)

    def test_maps_reuse_after_memory_loss_and_failed_regeneration_keeps_old_result(self):
        record_id = self.parse()["record_id"]
        summary = self.summary(record_id)
        path = f"/api/summaries/{summary.task_id}/mind-map"
        first = self.a.post(path).json()["task_id"]
        self.finish_map(first)
        video_tasks.summary_tasks.clear(); video_tasks.mind_map_tasks.clear()
        self.assertEqual(self.a.post(path).json()["task_id"], first)
        second = self.a.post(path + "?regenerate=true").json()["task_id"]
        task = video_tasks.mind_map_tasks[summary.task_id]
        self.assertEqual(task.version, 2)
        video_tasks._set_mind_map_task(task, status="failed", error="生成失败")
        self.assertEqual(self.a.get(path).json()["task_id"], first)
        self.assertEqual(self.a.get(path + "?map_id=" + second).json()["status"], "failed")
        self.assertEqual(self.b.get(path + "?map_id=" + first).status_code, 404)

    def test_concurrent_map_creation_reuses_one_task(self):
        summary = self.summary(self.parse()["record_id"])
        path = f"/api/summaries/{summary.task_id}/mind-map"
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.a.post(path), range(4)))
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len({response.json()["task_id"] for response in responses}), 1)

    def test_combined_generation_exposes_live_timing_and_restores_frozen_results(self):
        record_id = self.parse()["record_id"]
        created = self.a.post("/api/summaries", json={"url": self.url, "parse_record_id": record_id,
                                                   "generate_mind_map": True})
        self.assertEqual(created.status_code, 200, created.text)
        identifiers = created.json()
        task = video_tasks.summary_tasks[identifiers["task_id"]]
        map_task = task.map_task
        self.assertEqual(identifiers["mind_map_task_id"], map_task.task_id)
        path = f"/api/summaries/{task.task_id}/mind-map?map_id={map_task.task_id}"
        # The durable snapshot intentionally predates this stage. Polling must use the live timer.
        map_task.timing.start("live_stage", "实时阶段")
        live = self.a.get(path).json()
        self.assertIn("live_stage", [stage["id"] for stage in live["timing"]["stages"]])
        self.assertEqual(self.b.get(path).status_code, 404)
        self.assertEqual(self.b.get(f"/api/summaries/{task.task_id}").status_code, 404)
        segments = [video_schemas.SubtitleSegment(start=0, timestamp="00:00", text="字幕内容")]
        result = video_schemas.SummaryResult(title="联合任务", webpage_url=self.url, language="zh", source="字幕",
                                            summary_markdown="摘要", transcript=segments)
        video_tasks._set_summary_task(task, status="finished", progress=100, result=result, transcript=segments)
        self.finish_map(map_task.task_id)
        finished_summary = self.a.get(f"/api/summaries/{task.task_id}").json()
        finished_map = self.a.get(path).json()
        video_tasks.summary_tasks.clear()
        video_tasks.mind_map_tasks.clear()
        restored_summary = self.a.get(f"/api/summaries/{task.task_id}").json()
        restored_map = self.a.get(path).json()

        def assert_frozen_timing(actual, expected, path="timing"):
            if isinstance(expected, dict):
                self.assertEqual(actual.keys(), expected.keys(), path)
                for key in expected:
                    assert_frozen_timing(actual[key], expected[key], path + "." + key)
            elif isinstance(expected, list):
                self.assertEqual(len(actual), len(expected), path)
                for index, (left, right) in enumerate(zip(actual, expected)):
                    assert_frozen_timing(left, right, f"{path}[{index}]")
            elif isinstance(expected, float):
                # MySQL JSON can change the last float bit of epoch milliseconds.
                # Allow one microsecond; all fields and non-float values stay exact.
                self.assertAlmostEqual(actual, expected, delta=0.001, msg=path)
            else:
                self.assertEqual(actual, expected, path)

        assert_frozen_timing(restored_summary["timing"], finished_summary["timing"])
        assert_frozen_timing(restored_map["timing"], finished_map["timing"])
        self.assertEqual(restored_summary["mind_map_task_id"], restored_map["task_id"])
        self.assertIsNotNone(restored_map["timing"]["finished_at_ms"])


    def test_redis_parse_and_preview_survive_memory_loss_and_are_private(self):
        from backend.modules.video import preview_assets as assets
        parsed = self.parse()
        cached = video_cache.get(video_cache.parse_key(self.url, self.a_id))
        self.assertIsNotNone(cached)
        self.assertIsNone(video_cache.get(video_cache.parse_key(self.url, self.b_id)))
        with patch.object(assets, '_start'), patch.object(assets, 'ROOT', self.root / 'preview-test'), patch.object(assets, 'jobs', {}):
            created = self.a.post("/api/previews", json={"url": self.url, "format_id": parsed["formats"][0]["format_id"], "parse_record_id": parsed["record_id"]})
            self.assertEqual(created.status_code, 200, created.text)
            token = created.json()['preview_id']
            job = assets.jobs[token]
            job.directory.mkdir(parents=True)
            (job.directory / 'preview.mp4').write_bytes(b'abcdefgh')
            job.value.update(status='ready', file_id=token, has_audio=True)
            cache_key = video_cache.key('preview', token, self.a_id)
            video_cache.put(cache_key, job.value)
            url = f'/api/previews/{token}/content'
            assets.jobs.clear()
            video_preview.preview_sessions.clear(); video_parse_cache.sniff_cache.clear()
            self.assertEqual(self.a.get(created.json()['status_url']).json()['status'], 'ready')
            response = self.a.get(url, headers={"Range": "bytes=0-3"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b'abcd')
            self.assertEqual(self.b.get(url).status_code, 404)
            self.assertEqual(self.b.delete(created.json()['status_url']).status_code, 404)

    def test_delete_record_preserves_results_by_default_and_cascade_tombstones_tasks(self):
        first = self.parse()["record_id"]
        summary = self.summary(first)
        self.a.delete(f"/api/videos/records/{first}").raise_for_status()
        self.assertEqual(self.a.get(f"/api/summaries/{summary.task_id}").status_code, 200)
        second = self.parse()["record_id"]
        summary = self.summary(second)
        map_id = self.a.post(f"/api/summaries/{summary.task_id}/mind-map").json()["task_id"]
        map_task = self.finish_map(map_id)
        self.assertEqual(self.b.delete(f"/api/videos/records/{second}?cascade=true").status_code, 404)
        self.a.delete(f"/api/videos/records/{second}?cascade=true").raise_for_status()
        video_tasks._persist_task(map_task, "mind_map", force=True)
        self.assertEqual(self.a.get(f"/api/summaries/{summary.task_id}").status_code, 404)
        self.assertEqual(self.a.get(f"/api/videos/records/{second}").status_code, 404)
        self.assertEqual(self.a.get("/api/workspace/resources?kind=mind_map").json()["total"], 0)

    def test_worker_recovery_does_not_interrupt_live_workers(self):
        record_id = self.parse()["record_id"]
        with library.database() as cursor:
            cursor.execute("UPDATE video_parse_records SET status='running',worker_id='other-worker' WHERE record_id=%s", (record_id,))
        worker_key = accounts.redis_prefix() + "video:worker:other-worker"
        video_cache.put(worker_key, {"alive": True}, 45)
        video_history.restore_interrupted()
        self.assertEqual(self.a.get(f"/api/videos/records/{record_id}").json()["status"], "running")
        video_cache.remove(worker_key)
        video_history.restore_interrupted()
        self.assertEqual(self.a.get(f"/api/videos/records/{record_id}").json()["status"], "failed")

    def test_worker_recovery_does_not_overwrite_concurrent_completion(self):
        record_id = self.parse()["record_id"]
        with library.database() as cursor:
            cursor.execute("UPDATE video_parse_records SET status='running',worker_id='lost-worker' WHERE record_id=%s", (record_id,))
        client = accounts.redis_client()
        def completed_during_check(key):
            with library.database() as cursor:
                cursor.execute("UPDATE video_parse_records SET status='finished',updated_at=updated_at+1 WHERE record_id=%s", (record_id,))
            return False
        with patch.object(accounts, "redis_client", return_value=client), patch.object(client, "exists", side_effect=completed_during_check):
            video_history.restore_interrupted()
        self.assertEqual(self.a.get(f"/api/videos/records/{record_id}").json()["status"], "finished")

    def test_worker_recovery_does_not_treat_redis_failure_as_missing_lease(self):
        record_id = self.parse()["record_id"]
        with library.database() as cursor:
            cursor.execute("UPDATE video_parse_records SET status='running',worker_id='unknown-worker' WHERE record_id=%s", (record_id,))
        client = accounts.redis_client()
        with patch.object(accounts, "redis_client", return_value=client), patch.object(client, "exists", side_effect=redis.ConnectionError("offline")):
            with self.assertRaises(redis.ConnectionError):
                video_history.restore_interrupted()
        self.assertEqual(self.a.get(f"/api/videos/records/{record_id}").json()["status"], "running")

    def test_refresh_formats_reuses_record_and_force_bypasses_cache(self):
        record_id = self.parse()["record_id"]
        path = f"/api/videos/records/{record_id}/formats"
        fresh = video_schemas.ParseResponse(title="新格式", webpage_url=self.url,
            formats=[video_schemas.FormatInfo(format_id="fresh", label="MP4", ext="mp4")])
        with patch.object(video_parsing, 'parse_video', return_value=fresh) as parser:
            self.assertEqual(self.a.post(path).json()["record_id"], record_id)
            parser.assert_not_called()
            self.assertEqual(self.a.post(path + "?force=true").json()["title"], "新格式")
            parser.assert_called_once()
        self.assertEqual(self.a.get("/api/videos/records").json()["total"], 1)
        with patch.object(video_parsing, 'parse_video', return_value=fresh) as parser:
            self.a.post("/api/parse", json={"url": self.url, "force": True}).raise_for_status()
            parser.assert_called_once()
        self.assertEqual(self.a.get("/api/videos/records").json()["total"], 2)

    def test_legacy_results_are_backed_up_and_linked_only_when_unambiguous(self):
        summary = self.summary(None)
        map_id = self.a.post(f"/api/summaries/{summary.task_id}/mind-map").json()["task_id"]
        self.finish_map(map_id)
        filename = f"{summary.task_id}.summary.md"
        (storage.path("summary_file", filename, uid=self.a_id)).write_text("original", encoding="utf-8")
        summary.result.markdown_url = f"/api/summary-files/{filename}"
        video_tasks._persist_task(summary, "summary", force=True)
        backup_dir = self.root / "legacy-backups"
        with patch.object(video_history, "BACKUP_DIR", backup_dir):
            record_id = self.parse()["record_id"]
        tasks = self.a.get(f"/api/videos/records/{record_id}").json()["tasks"]
        self.assertEqual({item["task_id"] for item in tasks}, {summary.task_id, map_id})
        snapshots = list(backup_dir.glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
        self.assertTrue(all(not json.loads(row["payload"]).get("parse_record_id") for row in snapshot))
        with zipfile.ZipFile(snapshots[0].with_suffix(".files.zip")) as archive:
            self.assertEqual(archive.read(filename), b"original")
        newer = self.summary(None)
        second = self.parse()["record_id"]
        self.assertEqual(self.a.get(f"/api/videos/records/{second}").json()["tasks"], [])
        with library.database() as cursor:
            cursor.execute("SELECT content AS payload FROM video_summaries WHERE task_id=%s", (newer.task_id,))
            self.assertIsNone(json.loads(cursor.fetchone()["payload"]).get("parse_record_id"))

    def test_cascade_deletes_physical_video_and_summary_files(self):
        parsed = self.parse()
        record_id = parsed["record_id"]
        summary = self.summary(record_id)
        summary_file = storage.path("summary_file", f"{summary.task_id}.summary.md", uid=self.a_id)
        summary_file.write_text("summary", encoding="utf-8")
        summary.result.markdown_url = f"/api/summary-files/{summary_file.name}"
        video_tasks._persist_task(summary, "summary", force=True)
        accounts.register_resource("summary_file", summary_file.name, uid=self.a_id)
        response = self.a.post("/api/download", json={"url": self.url, "format_id": parsed["formats"][0]["format_id"], "parse_record_id": record_id})
        response.raise_for_status()
        task = video_tasks.tasks[response.json()["task_id"]]
        video_file = storage.path("file", f"{task.task_id}.mp4", uid=self.a_id)
        video_file.write_bytes(b"video")
        video_tasks._set_task(task, status="finished", progress=100, filename=video_file.name)
        self.a.delete(f"/api/videos/records/{record_id}?cascade=true").raise_for_status()
        self.assertFalse(video_file.exists())
        self.assertFalse(summary_file.exists())
        self.assertEqual(self.a.get(f"/api/files/{video_file.name}").status_code, 404)

    def test_admin_created_tasks_belong_to_record_owner(self):
        record_id = self.parse()["record_id"]
        response = self.admin.post("/api/summaries", json={"url": self.url, "parse_record_id": record_id})
        response.raise_for_status()
        task = video_tasks.summary_tasks[response.json()["task_id"]]
        self.assertEqual(task.user_id, self.a_id)
        result = video_schemas.SummaryResult(title="总结", webpage_url=self.url, language="zh", source="字幕", summary_markdown="摘要", transcript=[])
        video_tasks._set_summary_task(task, status="finished", progress=100, result=result)
        response = self.admin.post(f"/api/summaries/{task.task_id}/mind-map")
        response.raise_for_status()
        map_task = video_tasks.mind_map_tasks[task.task_id]
        self.assertEqual(map_task.user_id, self.a_id)
        self.assertEqual(self.a.get(f"/api/summaries/{task.task_id}").status_code, 200)
        self.assertEqual(self.b.get(f"/api/summaries/{task.task_id}").status_code, 404)

    def test_deleted_record_cannot_receive_new_map(self):
        record_id = self.parse()["record_id"]
        summary = self.summary(record_id)
        self.a.delete(f"/api/videos/records/{record_id}").raise_for_status()
        self.assertEqual(self.a.post(f"/api/summaries/{summary.task_id}/mind-map").status_code, 404)

    def test_deletion_revokes_associated_preview(self):
        from backend.modules.video import preview_assets as assets
        parsed = self.parse()
        with patch.object(assets, '_start'), patch.object(assets, 'jobs', {}):
            response = self.a.post("/api/previews", json={"url": self.url, "format_id": parsed["formats"][0]["format_id"], "parse_record_id": parsed["record_id"]})
            response.raise_for_status()
            job = assets.jobs[response.json()['preview_id']]
            self.a.delete(f"/api/videos/records/{parsed['record_id']}").raise_for_status()
            self.assertTrue(job.cancelled.is_set())
            self.assertEqual(self.a.get(response.json()["status_url"]).status_code, 404)

    def test_concurrent_map_creation_and_cascade_leave_no_orphan(self):
        record_id = self.parse()["record_id"]
        summary = self.summary(record_id)
        barrier = Barrier(2)
        def create():
            barrier.wait(timeout=5)
            return self.a.post(f"/api/summaries/{summary.task_id}/mind-map")
        def delete():
            barrier.wait(timeout=5)
            return self.a.delete(f"/api/videos/records/{record_id}?cascade=true")
        with ThreadPoolExecutor(max_workers=2) as pool:
            created, deleted = pool.submit(create), pool.submit(delete)
            self.assertIn(created.result(timeout=10).status_code, {200, 404})
            self.assertEqual(deleted.result(timeout=10).status_code, 200)
        with library.database() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM video_mind_maps WHERE deleted=FALSE AND summary_task_id=%s", (summary.task_id,))
            self.assertEqual(cursor.fetchone()["n"], 0)

    def test_business_tables_hold_content_and_catalogue_has_only_metadata(self):
        record_id = self.parse()["record_id"]
        summary = self.summary(record_id)
        map_id = self.a.post(f"/api/summaries/{summary.task_id}/mind-map").json()["task_id"]
        self.finish_map(map_id)
        self.a.put(f"/api/videos/records/{record_id}/view", json={"tab": "mind-map", "playback_time": 31}).raise_for_status()
        with library.database() as cursor:
            for table in ('video_recent_links', *(item[0] for item in video_store.TABLES.values())):
                cursor.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=%s", (self.a_id,))
                self.assertEqual(cursor.fetchone()["n"], 1, table)
            cursor.execute("SELECT payload FROM resources WHERE kind IN ('video_parse','summary','mind_map','video_view','video_search')")
            for row in cursor.fetchall():
                data = json.loads(row["payload"])
                self.assertEqual(data["storage"], video_store.MIGRATION)
                self.assertNotIn("result", data)
                self.assertNotIn("transcript", data)
        workspace = self.a.get('/api/workspace/resources?kind=summary').json()['items'][0]['payload']
        self.assertEqual(workspace['result']['summary_markdown'], '测试摘要')
        admin = self.admin.get(f'/api/admin/data?kind=mind_map&user_id={self.a_id}').json()['items'][0]['payload']
        self.assertEqual(admin['result']['nodes'][0]['id'], 'node-1')

    def test_deleted_map_versions_are_not_reused(self):
        summary = self.summary(self.parse()["record_id"])
        path = f"/api/summaries/{summary.task_id}/mind-map"
        first = self.a.post(path).json()["task_id"]
        self.finish_map(first)
        self.a.delete('/api/workspace/resources/mind_map/' + first).raise_for_status()
        second = self.a.post(path + '?regenerate=true').json()['task_id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.a.get(path + '?map_id=' + second).json()['version'], 2)
        with library.database() as cursor:
            self.assertTrue(video_store.get(cursor, 'mind_map', first, include_deleted=True)['deleted'])

    def test_legacy_table_migration_preserves_data_files_tombstones_and_is_idempotent(self):
        record_id = self.parse()['record_id']
        summary = self.summary(record_id)
        path = f'/api/summaries/{summary.task_id}/mind-map'
        first = self.a.post(path).json()['task_id']
        self.finish_map(first)
        second = self.a.post(path + '?regenerate=true').json()['task_id']
        self.finish_map(second)
        self.a.delete('/api/workspace/resources/mind_map/' + second).raise_for_status()
        filename = summary.task_id + '.summary.md'
        (storage.path("summary_file", filename, uid=self.a_id)).write_text('完整的原始总结', encoding='utf-8')
        summary.result.markdown_url = '/api/summary-files/' + filename
        video_tasks._persist_task(summary, 'summary', force=True)
        self.a.put(f'/api/videos/records/{record_id}/view', json={'tab': 'mind-map', 'playback_time': 42}).raise_for_status()
        with library.database() as cursor:
            cursor.execute('SELECT * FROM resources')
            originals = [video_store.hydrate(cursor, row) for row in cursor.fetchall() if row['kind'] in video_store.KINDS]
            for table in ('video_recent_links', *(item[0] for item in video_store.TABLES.values())):
                cursor.execute(f'DELETE FROM {table}')
            for row in originals:
                value = json.loads(row['payload'])
                if row['kind'] == 'mind_map':
                    value.pop('version')
                cursor.execute('UPDATE resources SET payload=%s WHERE kind=%s AND resource_key=%s',
                               (json.dumps(value, ensure_ascii=False), row['kind'], row['resource_key']))
            cursor.execute('DELETE FROM account_migrations WHERE id=%s', (video_store.MIGRATION,))
        folder = self.root / 'table-backups'
        with patch.object(video_store, 'BACKUP_DIR', folder):
            with library.database() as cursor:
                video_store.migrate(cursor)
            with library.database() as cursor:
                for row in originals:
                    restored = video_store.get(cursor, row['kind'], row['resource_key'], include_deleted=True)
                    self.assertEqual(restored['user_id'], row['user_id'])
                    self.assertEqual(restored['created_at'], row['created_at'])
                    self.assertEqual(restored['updated_at'], row['updated_at'])
                    self.assertEqual(restored['deleted'], row['deleted'])
                    self.assertEqual(json.loads(restored['payload']), json.loads(row['payload']))
                video_store.migrate(cursor)
        self.assertEqual(len(list(folder.glob('*.json'))), 1)
        backup = json.loads(next(folder.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(backup), len(originals))
        with zipfile.ZipFile(next(folder.glob('*.files.zip'))) as archive:
            self.assertEqual(archive.read(filename).decode(), '完整的原始总结')
        self.assertEqual(self.a.get(f'/api/videos/records/{record_id}').json()['view']['playback_time'], 42)

    def test_migration_failure_rolls_back_content_and_keeps_original_catalogue(self):
        legacy = {'task_id': 'legacy-summary', 'status': 'finished', 'result': {'summary_markdown': '保留正文'}}
        with library.database() as cursor:
            cursor.execute('DELETE FROM account_migrations WHERE id=%s', (video_store.MIGRATION,))
            cursor.execute("INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at,deleted) VALUES ('summary','legacy-summary',%s,%s,1,2,FALSE)",
                           (self.a_id, json.dumps(legacy, ensure_ascii=False)))
        folder = self.root / 'failed-table-backup'
        save = video_store.save
        def fail_after_write(*args, **kwargs):
            save(*args, **kwargs)
            raise RuntimeError('模拟迁移中断')
        with patch.object(video_store, 'BACKUP_DIR', folder), patch.object(video_store, 'save', side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                with library.database() as cursor:
                    video_store.migrate(cursor)
        with library.database() as cursor:
            cursor.execute("SELECT payload FROM resources WHERE kind='summary' AND resource_key='legacy-summary'")
            self.assertEqual(json.loads(cursor.fetchone()['payload']), legacy)
            cursor.execute('SELECT COUNT(*) AS n FROM video_summaries')
            self.assertEqual(cursor.fetchone()['n'], 0)
        with patch.object(video_store, 'BACKUP_DIR', folder):
            with library.database() as cursor:
                video_store.migrate(cursor)
        self.assertEqual(self.a.get('/api/workspace/resources?kind=summary').json()['total'], 1)

    def test_concurrent_recent_links_keep_five_distinct_submissions(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda n: self.a.post('/api/videos/recent-links/migrate',
                json={'links': [f'https://example.com/{n}']}), range(8)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        items = self.a.get('/api/videos/recent-links').json()['items']
        self.assertEqual(len({item['url'] for item in items}), 5)
