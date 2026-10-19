import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from playwright.sync_api import Error as BrowserError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.infrastructure import browser_sessions as browser_session_store
from backend.infrastructure import network
from backend.core import paths
from backend.modules.video import api as video_api
from backend.modules.video import browser as video_browser
from backend.modules.video import douyin as video_douyin
from backend.modules.video import parsing as video_parsing
from backend.modules.video import schemas as video_schemas
from backend.modules.accounts import service as accounts
from backend.infrastructure.browser_sessions import BrowserSessions, needs_verification, verification_required


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False
        self.text = ""
        self.navigations = []

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, _):
        pass

    def goto(self, url, **_):
        self.url = url
        self.navigations.append(url)

    def bring_to_front(self):
        pass

    def locator(self, _):
        return SimpleNamespace(inner_text=lambda **_: self.text)


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.closed = False
        self.handlers = {}

    def route(self, _, handler):
        self.route_handler = handler

    def on(self, event, handler):
        self.handlers[event] = handler

    def close(self):
        self.closed = True
        self.pages[0].closed = True
        self.handlers["close"]()


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.now = 100.0
        self.launches = []
        self.contexts = []
        self.playwright = Mock()
        self.playwright.chromium.launch_persistent_context.side_effect = self.launch
        self.service = BrowserSessions(Path(self.directory.name), factory=lambda: SimpleNamespace(start=lambda: self.playwright), clock=lambda: self.now)
        self.account_patch = patch.object(accounts, "active_worker")
        self.account_patch.start()
        self.network_patch = patch("backend.infrastructure.browser_sessions.public_url", side_effect=lambda url: url)
        self.network_patch.start()

    def tearDown(self):
        self.service.shutdown()
        self.network_patch.stop()
        self.account_patch.stop()
        self.directory.cleanup()

    def launch(self, profile, **options):
        self.launches.append((profile, options))
        context = FakeContext()
        self.contexts.append(context)
        return context

    def test_account_profiles_and_contexts_are_isolated(self):
        first = self.service.run(1, id)
        self.assertEqual(first, self.service.run(1, id))
        self.assertNotEqual(first, self.service.run(2, id))
        self.assertEqual([Path(item[0]).name for item in self.launches], ["1", "2"])
        if os.name != "nt":
            self.assertEqual(self.service.profile(1).stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.launches[0][1]["headless"])
        self.assertEqual(self.launches[0][1]["service_workers"], "block")

    def test_showing_window_reuses_profile_and_does_not_reload_open_challenge(self):
        self.service.run(1, id)
        url = "https://www.douyin.com/video/123456789"
        status = self.service.open(1, url)
        self.assertTrue(self.contexts[0].closed)
        self.assertEqual(self.launches[0][0], self.launches[1][0])
        self.assertFalse(self.launches[1][1]["headless"])
        self.assertTrue(status["visible"])
        self.assertEqual(status["url"], url)
        self.service.open(1, url)
        self.service.run(1, id)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.contexts[1].pages[0].navigations, [url])

    def test_redirected_share_links_keep_the_current_verification_page(self):
        url = "https://v.douyin.com/share/"
        self.service.open(1, url)
        self.contexts[0].pages[0].url = "https://www.douyin.com/video/123456789"
        self.service.open(1, url)
        self.assertEqual(self.contexts[0].pages[0].navigations, [url])

    def test_interrupted_browser_exposes_a_recoverable_verification_error(self):
        def interrupted(_):
            raise BrowserError("Target closed")
        with self.assertRaises(HTTPException) as raised:
            self.service.run(1, interrupted, url="https://www.douyin.com/video/123456789")
        self.assertEqual(raised.exception.detail["code"], "browser_verification_required")
        self.assertEqual(self.service.status(1)["status"], "error")
        self.assertTrue(self.contexts[0].closed)

    def test_all_calls_are_serialized_on_worker_and_keep_request_context(self):
        marker = ContextVar("test_marker", default=None)
        def request(uid):
            marker.set(uid)
            return self.service.run(uid, lambda _: (threading.get_ident(), marker.get()))
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(request, [1, 2, 3]))
        self.assertEqual(len({item[0] for item in results}), 1)
        self.assertNotEqual(results[0][0], threading.get_ident())
        self.assertEqual([item[1] for item in results], [1, 2, 3])

    def test_verification_state_expiry_and_manual_close(self):
        self.service.open(1, "https://www.douyin.com/video/123456789")
        self.contexts[0].pages[0].text = "请完成下列验证后继续"
        self.assertEqual(self.service.status(1)["status"], "waiting")
        self.now += 901
        self.assertEqual(self.service.status(1)["status"], "expired")
        self.assertTrue(self.service.profile(1).exists())
        self.service.open(1, "https://www.douyin.com/video/123456789")
        self.contexts[1].pages[0].closed = True
        self.assertEqual(self.service.status(1)["status"], "closed")

    def test_close_preserves_profile_and_reset_clears_only_owner(self):
        self.service.run(1, id)
        self.service.run(2, id)
        self.service.close(1)
        self.assertTrue(self.service.profile(1).exists())
        self.service.cancel_user(2, "some-resource")
        self.assertFalse(self.contexts[1].closed)
        self.service.cancel_user(1)
        self.assertFalse(self.service.profile(1).exists())
        self.assertTrue(self.service.profile(2).exists())

    def test_idle_requests_and_popups_reject_private_urls(self):
        self.service.run(1, id)
        route = Mock(request=SimpleNamespace(url="http://127.0.0.1/private"))
        with patch("backend.infrastructure.browser_sessions.public_url", side_effect=HTTPException(422, "private")):
            self.service._submit(lambda: self.contexts[0].route_handler(route))
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_visible_session_limit_does_not_close_other_verification_windows(self):
        for uid in range(1, 5):
            self.service.open(uid, "https://www.douyin.com/video/123456789")
        with self.assertRaises(HTTPException) as raised:
            self.service.open(5, "https://www.douyin.com/video/123456789")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertTrue(all(not context.closed for context in self.contexts))

    def test_inactive_accounts_cannot_start_browser(self):
        with patch.object(accounts, "active_worker", side_effect=HTTPException(401, "disabled")):
            with self.assertRaises(HTTPException):
                self.service.run(1, id)
        self.assertEqual(self.launches, [])

    def test_shutdown_closes_contexts_and_stops_worker(self):
        self.service.run(1, id)
        self.service.shutdown()
        self.assertTrue(self.contexts[0].closed)
        self.assertFalse(self.service.thread.is_alive())
        self.playwright.stop.assert_called_once()


class VerificationIntegrationTests(unittest.TestCase):
    def test_browser_endpoints_require_login_and_obey_video_feature_gate(self):
        app = FastAPI()
        app.add_middleware(accounts.AuthenticationMiddleware)
        app.get("/api/browser-session")(lambda: {"ok": True})
        with TestClient(app) as client, patch.object(accounts, "authenticate", return_value=accounts.Principal(42, "tester", "user")), \
                patch.object(accounts, "settings", return_value=accounts.DEFAULT_SETTINGS | {"video_enabled": False}):
            self.assertEqual(client.get("/api/browser-session").status_code, 401)
            response = client.get("/api/browser-session", headers={"Authorization": "Bearer test"})
            self.assertEqual(response.status_code, 403)
            self.assertIn("关闭", response.json()["detail"])

    def test_verification_error_is_preserved_without_downloader_fallback(self):
        url = "https://www.douyin.com/video/123456789"
        with patch.object(video_browser, 'parse_browser_sniffed_page', side_effect=verification_required(url)), \
                patch.object(video_douyin, 'parse_douyin_public') as public, patch.object(video_parsing, 'YoutubeDL') as ydl:
            with self.assertRaises(HTTPException) as raised:
                video_parsing.parse_video(url)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "browser_verification_required")
        public.assert_not_called()
        ydl.assert_not_called()

    def test_open_endpoint_checks_site_and_uses_authenticated_owner(self):
        user = accounts.Principal(42, "tester", "user")
        marker = accounts.principal.set(user)
        try:
            with patch.object(network, 'public_url'), patch.object(browser_session_store.sessions, "open") as opened:
                video_api.open_browser_session(video_schemas.ParseRequest(url="https://www.douyin.com/search/ai?modal_id=7491234567890123456"))
                opened.assert_called_once_with(42, "https://www.douyin.com/video/7491234567890123456")
                for url in ("https://example.com/video", "https://www.douyin.com/search/ai", "https://douyin.com.example.com/video/123456789"):
                    with self.assertRaises(HTTPException):
                        video_api.open_browser_session(video_schemas.ParseRequest(url=url))
        finally:
            accounts.principal.reset(marker)

    def test_challenge_detection_and_regular_pages(self):
        page = FakePage()
        self.assertFalse(needs_verification(page))
        page.text = "请完成下列验证后继续"
        self.assertTrue(needs_verification(page))


if __name__ == "__main__":
    unittest.main()
