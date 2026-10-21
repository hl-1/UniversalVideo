import json
import time
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.modules.accounts import service as accounts
from backend.modules.video import api, bilibili_session as bili, schemas, subtitles, summary, tasks
from backend.tests.video.test_summary import FakeResponse


class BilibiliSessionTests(unittest.TestCase):
    """Use an isolated Redis namespace to exercise real atomic Lua transitions."""

    @classmethod
    def setUpClass(cls):
        cls.client = accounts.redis_client()
        cls.client.ping()

    def setUp(self):
        self.prefix = f"videodream:test-bilibili:{uuid.uuid4().hex}:"
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(accounts, "redis_prefix", return_value=self.prefix))
        self.stack.enter_context(patch.object(accounts, "redis_client", return_value=self.client))
        self.stack.enter_context(patch.object(accounts, "signing_key", return_value="test-secret-" * 8))
        self.stack.enter_context(patch.object(accounts, "active_worker"))

    def tearDown(self):
        keys = list(self.client.scan_iter(match=self.prefix + "*"))
        if keys:
            self.client.delete(*keys)
        self.stack.close()

    def create(self, uid=42):
        with patch.object(bili, "_request", return_value=({"code": 0, "data": {
            "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?auth_code=provider-secret",
            "qrcode_key": "provider-secret"}}, requests.cookies.RequestsCookieJar())):
            return bili.create_qr(uid)

    def confirmed(self, action=None):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("SESSDATA", "private-session", domain=".bilibili.com", path="/", expires=int(time.time()) + 7200)
        jar.set("secret", "other-site", domain=".example.com", path="/")
        def response(url, **kwargs):
            if action:
                action()
            return {"code": 0, "data": {"code": 0}}, jar
        return response

    def test_confirmation_encrypts_and_isolates_credentials(self):
        qr = self.create()
        self.assertTrue(qr["qr_image"].startswith("data:image/png;base64,"))
        self.assertNotIn("provider-secret", json.dumps(qr))
        self.assertNotIn("provider-secret", self.client.get(bili.key(42, "qr")))
        with patch.object(bili, "_request", side_effect=self.confirmed()), patch.object(bili, "_verify", return_value={"bilibili_uid": "7", "nickname": "测试用户"}):
            result = bili.poll_qr(42, qr["login_id"])
        self.assertEqual(result["status"], "confirmed")
        self.assertNotIn("private-session", json.dumps(result))
        self.assertNotIn("private-session", self.client.get(bili.key(42, "session")))
        header, _ = bili.cookie_header(42)
        self.assertEqual(header, "SESSDATA=private-session")
        self.assertEqual(bili.cookie_header(43), ("", None))
        self.assertIsNone(self.client.get(bili.key(42, "qr")))
        self.assertLessEqual(self.client.ttl(bili.key(42, "session")), 7200)

    def test_poll_is_owned_and_throttled_and_handles_scanned_state(self):
        qr = self.create()
        with patch.object(bili, "_request", return_value=({"code": 0, "data": {"code": 86090}}, None)) as request:
            self.assertEqual(bili.poll_qr(43, qr["login_id"])["status"], "expired")
            request.assert_not_called()
            self.assertEqual(bili.poll_qr(42, qr["login_id"])["status"], "waiting_confirm")
            self.assertEqual(bili.poll_qr(42, qr["login_id"])["status"], "waiting_confirm")
            self.assertEqual(request.call_count, 1)

    def test_cancel_or_disconnect_wins_against_late_confirmation(self):
        for action_name in ("cancel", "disconnect"):
            with self.subTest(action=action_name):
                qr = self.create()
                action = lambda: bili.cancel_qr(42, qr["login_id"]) if action_name == "cancel" else bili.disconnect(42)
                with patch.object(bili, "_request", side_effect=self.confirmed(action)), patch.object(bili, "_verify", return_value={"bilibili_uid": "7", "nickname": "测试用户"}):
                    self.assertEqual(bili.poll_qr(42, qr["login_id"])["status"], "expired")
                self.assertIsNone(self.client.get(bili.key(42, "session")))

    def test_refresh_expires_old_id_and_old_cancel_preserves_new_qr(self):
        old, new = self.create(), self.create()
        bili.cancel_qr(42, old["login_id"])
        with patch.object(bili, "_request") as request:
            self.assertEqual(bili.poll_qr(42, old["login_id"])["status"], "expired")
            request.assert_not_called()
        self.assertEqual(bili._read(42, "qr")[0]["login_id"], new["login_id"])

    def test_upstream_expiry_removes_qr(self):
        qr = self.create()
        with patch.object(bili, "_request", return_value=({"code": 0, "data": {"code": 86038}}, None)):
            self.assertEqual(bili.poll_qr(42, qr["login_id"])["status"], "expired")
        self.assertIsNone(self.client.get(bili.key(42, "qr")))

    def test_cancel_during_qr_creation_cannot_restore_it(self):
        def upstream(*args, **kwargs):
            bili.disconnect(42)
            return {"code": 0, "data": {"url": "https://account.bilibili.com/scan", "qrcode_key": "secret"}}, None
        with patch.object(bili, "_request", side_effect=upstream), self.assertRaises(HTTPException) as caught:
            bili.create_qr(42)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNone(self.client.get(bili.key(42, "qr")))

    def test_rotating_key_discards_encrypted_sessions(self):
        qr = self.create()
        with patch.object(accounts, "signing_key", return_value="different-secret-" * 8):
            self.assertEqual(bili.poll_qr(42, qr["login_id"])["status"], "expired")
        self.assertIsNone(self.client.get(bili.key(42, "qr")))

    def test_invalidating_old_session_does_not_delete_replacement(self):
        self.client.set(bili.key(42, "session"), "new-session", ex=60)
        bili.invalidate(42, "old-session")
        self.assertEqual(self.client.get(bili.key(42, "session")), "new-session")

    def test_stale_verification_cannot_restore_disconnected_session(self):
        value = {"version": 1, "uid": 42, "expires_at": time.time() + 3600, "cookies": [], "last_verified_at": 0}
        self.client.set(bili.key(42, "session"), bili._seal(value), ex=3600)
        def verify(cookies):
            bili.disconnect(42)
            return {"bilibili_uid": "7", "nickname": "测试用户"}
        with patch.object(bili, "_verify", side_effect=verify):
            self.assertEqual(bili.session(42), (None, None))
        self.assertIsNone(self.client.get(bili.key(42, "session")))

    def test_concurrent_session_verification_uses_the_winning_binding(self):
        for verified_identity in (None, {"bilibili_uid": "old", "nickname": "旧登录"}):
            with self.subTest(valid=bool(verified_identity)):
                old = {"version": 1, "uid": 42, "expires_at": time.time() + 3600, "cookies": [], "last_verified_at": 0}
                new = old | {"bilibili_uid": "new", "nickname": "新登录", "last_verified_at": time.time()}
                self.client.set(bili.key(42, "session"), bili._seal(old), ex=3600)
                def verify(cookies):
                    self.client.set(bili.key(42, "session"), bili._seal(new), ex=3600)
                    return verified_identity
                with patch.object(bili, "_verify", side_effect=verify):
                    value, _ = bili.session(42)
                self.assertEqual(value["nickname"], "新登录")

    def test_network_error_does_not_discard_binding(self):
        value = {"version": 1, "uid": 42, "expires_at": time.time() + 3600, "cookies": [], "last_verified_at": 0}
        encoded = bili._seal(value)
        self.client.set(bili.key(42, "session"), encoded, ex=3600)
        with patch.object(bili, "_verify", side_effect=HTTPException(502, "暂不可用")), self.assertRaises(HTTPException):
            bili.session(42)
        self.assertEqual(self.client.get(bili.key(42, "session")), encoded)

    def test_cancel_retries_if_waiting_status_changes_during_delete(self):
        qr = self.create()
        real_eval = self.client.eval
        changed = False
        def racing_eval(script, *args):
            nonlocal changed
            if script == bili.COMPARE_DELETE and not changed:
                changed = True
                value, _ = bili._read(42, "qr")
                value["status"] = "waiting_confirm"
                self.client.set(bili.key(42, "qr"), bili._seal(value), ex=180)
            return real_eval(script, *args)
        with patch.object(self.client, "eval", side_effect=racing_eval):
            self.assertEqual(bili.cancel_qr(42, qr["login_id"]), {"cancelled": True})
        self.assertIsNone(self.client.get(bili.key(42, "qr")))

    def test_http_endpoints_require_auth_and_use_actor_not_data_owner(self):
        app = FastAPI()
        app.include_router(api.router)
        app.add_middleware(accounts.AuthenticationMiddleware)
        qr = self.create(42)
        owner = accounts.data_owner.set(99)
        try:
            with TestClient(app) as client, patch.object(accounts, "authenticate", side_effect=lambda token: accounts.Principal(int(token), "test", "user")), \
                    patch.object(accounts, "settings", return_value=accounts.DEFAULT_SETTINGS), patch.object(accounts, "audit"):
                self.assertEqual(client.get("/api/videos/bilibili/session").status_code, 401)
                self.assertEqual(client.post("/api/videos/bilibili/qr").status_code, 401)
                other = client.get(f'/api/videos/bilibili/qr/{qr["login_id"]}', headers={"Authorization": "Bearer 43"})
                self.assertEqual(other.json()["status"], "expired")
                client.delete(f'/api/videos/bilibili/qr/{qr["login_id"]}', headers={"Authorization": "Bearer 43"})
                self.assertIsNotNone(bili._read(42, "qr")[0])
                client.delete("/api/videos/bilibili/session", headers={"Authorization": "Bearer 42"})
                self.assertIsNone(bili._read(42, "qr")[0])
        finally:
            accounts.data_owner.reset(owner)


class SubtitleAdmissionTests(unittest.TestCase):
    url = "https://www.bilibili.com/video/BV1tDsgzxECr/?p=9"

    def setUp(self):
        self.stack = ExitStack()
        token = accounts.principal.set(accounts.Principal(42, "actor", "super_admin"))
        self.stack.callback(accounts.principal.reset, token)
        owner = accounts.data_owner.set(99)
        self.stack.callback(accounts.data_owner.reset, owner)
        self.stack.enter_context(patch.object(api.network, "public_url"))
        self.stack.enter_context(patch.object(api.video_history, "validate_relation", return_value={"user_id": 99}))
        self.persist = self.stack.enter_context(patch.object(tasks, "_persist_task"))
        self.submit = self.stack.enter_context(patch.object(accounts, "submit"))
        self.cookie = self.stack.enter_context(patch.object(bili, "cookie_header", return_value=("", None)))
        self.old_ids = set(tasks.summary_tasks)

    def tearDown(self):
        for identity in set(tasks.summary_tasks) - self.old_ids:
            tasks.summary_tasks.pop(identity)
        self.stack.close()

    def test_login_required_does_not_create_persist_or_queue_task(self):
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", side_effect=subtitles.BilibiliLoginRequired("需要登录")), self.assertRaises(HTTPException) as caught:
            api.summary_endpoint(schemas.SummaryRequest(url=self.url, generate_mind_map=True))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "bilibili_login_required")
        self.cookie.assert_called_once_with(42)
        self.persist.assert_not_called()
        self.submit.assert_not_called()
        self.assertEqual(set(tasks.summary_tasks), self.old_ids)

    def test_platform_error_never_silently_starts_speech(self):
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", side_effect=subtitles.BilibiliSubtitleError("网络错误")), self.assertRaises(HTTPException) as caught:
            api.summary_endpoint(schemas.SummaryRequest(url=self.url))
        self.assertEqual(caught.exception.status_code, 502)
        self.submit.assert_not_called()

    def test_explicit_speech_bypasses_cookie_and_platform_probe(self):
        with patch.object(subtitles, "_extract_bilibili_api_summary_source") as probe:
            result = api.summary_endpoint(schemas.SummaryRequest(url=self.url, bilibili_subtitle_policy="speech"))
        probe.assert_not_called()
        self.cookie.assert_not_called()
        task = tasks.summary_tasks[result.task_id]
        self.assertTrue(task.bilibili_prepared)
        self.assertIsNone(task.prepared_source)

    def test_prefetched_subtitles_are_consumed_without_repeat_fetch(self):
        source = ({"title": "测试", "webpage_url": self.url}, "zh", "automatic", [schemas.SubtitleSegment(start=0, timestamp="0:00", text="字幕")])
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", return_value=source):
            result = api.summary_endpoint(schemas.SummaryRequest(url=self.url))
        task = tasks.summary_tasks[result.task_id]
        self.assertEqual(task.user_id, 99)
        self.cookie.assert_called_once_with(42)
        with patch.object(subtitles, "_extract_summary_source") as extract, \
                patch.object(summary, "_load_ai_config", return_value={"model": "test", "api_key": "test"}), \
                patch.object(summary, "_generate_ai_summary", return_value="# 摘要"), \
                patch.object(summary, "_save_summary_files", side_effect=lambda task, result: result), \
                patch.object(tasks, "_checkpoint"):
            summary._run_summary(task)
        extract.assert_not_called()
        self.assertEqual(task.status, "finished")
        self.assertIsNone(task.prepared_source)

    def test_strict_anonymous_probe_requires_login_without_browser_access(self):
        responses = [FakeResponse({"code": 0, "data": {"pages": [{"page": 9, "cid": 123}]}}),
                     FakeResponse({"code": 0, "data": {"need_login_subtitle": True, "subtitle": {"subtitles": []}}})]
        with patch.object(requests, "get", side_effect=responses), patch.object(subtitles, "_extract_bilibili_browser_cookie_header") as browser, self.assertRaises(subtitles.BilibiliLoginRequired):
            subtitles._extract_bilibili_api_summary_source(self.url, strict=True)
        browser.assert_not_called()

    def test_bound_probe_uses_correct_part_and_never_sends_cookie_to_subtitle_cdn(self):
        responses = [FakeResponse({"code": 0, "data": {"pages": [{"page": 1, "cid": 111}, {"page": 9, "cid": 999}]}}),
                     FakeResponse({"code": 0, "data": {"need_login_subtitle": True}}),
                     FakeResponse({"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "https://aisubtitle.hdslb.com/sub.json"}]}}}),
                     FakeResponse({"body": [{"from": 0, "to": 1, "content": "字幕"}]})]
        with patch.object(requests, "get", side_effect=responses) as request:
            result = subtitles._extract_bilibili_api_summary_source(self.url, cookie_header="SESSDATA=private", strict=True)
        self.assertEqual(result[3][0].text, "字幕")
        self.assertEqual(request.call_args_list[1].kwargs["params"]["cid"], 999)
        self.assertIn("Cookie", request.call_args_list[2].kwargs["headers"])
        self.assertNotIn("Cookie", request.call_args_list[3].kwargs["headers"])

    def test_verified_login_without_captions_requires_speech_confirmation(self):
        self.cookie.return_value = ("SESSDATA=private", "encrypted")
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", side_effect=subtitles.BilibiliLoginRequired("需要登录")), \
                patch.object(bili, "still_valid", return_value=True), patch.object(bili, "invalidate") as invalidate, \
                self.assertRaises(HTTPException) as caught:
            api.summary_endpoint(schemas.SummaryRequest(url=self.url))
        invalidate.assert_not_called()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "bilibili_speech_confirmation_required")
        self.persist.assert_not_called()
        self.submit.assert_not_called()

    def test_no_captions_never_creates_tasks_until_speech_is_confirmed(self):
        for cookie in [("", None), ("SESSDATA=private", "encrypted")]:
            with self.subTest(bound=bool(cookie[0])):
                self.cookie.return_value = cookie
                with patch.object(subtitles, "_extract_bilibili_api_summary_source", return_value=None), \
                        patch.object(summary, "create_pipeline_map") as create_map, \
                        self.assertRaises(HTTPException) as caught:
                    api.summary_endpoint(schemas.SummaryRequest(url=self.url, generate_mind_map=True))
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail["code"], "bilibili_speech_confirmation_required")
                self.persist.assert_not_called()
                self.submit.assert_not_called()
                create_map.assert_not_called()
                self.assertEqual(set(tasks.summary_tasks), self.old_ids)

    def test_expired_login_prompts_again_and_invalidates_only_old_session(self):
        self.cookie.return_value = ("SESSDATA=expired", "old-encrypted")
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", side_effect=subtitles.BilibiliLoginRequired("失效")), \
                patch.object(bili, "still_valid", return_value=False), patch.object(bili, "invalidate") as invalidate, self.assertRaises(HTTPException):
            api.summary_endpoint(schemas.SummaryRequest(url=self.url))
        invalidate.assert_called_once_with(42, "old-encrypted")
        self.submit.assert_not_called()

    def test_anonymous_unauthorized_response_retries_with_bound_credential(self):
        responses = [FakeResponse({"code": 0, "data": {"pages": [{"page": 9, "cid": 999}]}}),
                     FakeResponse({"code": -101}),
                     FakeResponse({"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/sub.json"}]}}}),
                     FakeResponse({"body": [{"from": 0, "to": 1, "content": "登录字幕"}]})]
        with patch.object(requests, "get", side_effect=responses) as request:
            result = subtitles._extract_bilibili_api_summary_source(self.url, cookie_header="SESSDATA=private", strict=True)
        self.assertEqual(result[3][0].text, "登录字幕")
        self.assertEqual(request.call_args_list[2].kwargs["headers"]["Cookie"], "SESSDATA=private")

    def test_malformed_player_payload_is_not_treated_as_no_subtitles(self):
        responses = [FakeResponse({"code": 0, "data": {"pages": [{"page": 9, "cid": 999}]}}),
                     FakeResponse({"code": 0, "data": {}})]
        with patch.object(requests, "get", side_effect=responses), self.assertRaises(subtitles.BilibiliSubtitleError):
            subtitles._extract_bilibili_api_summary_source(self.url, strict=True)


if __name__ == "__main__":
    unittest.main()
