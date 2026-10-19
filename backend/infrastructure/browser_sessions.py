from __future__ import annotations

import queue
import shutil
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from playwright.sync_api import Error as BrowserError, sync_playwright

from backend.core.paths import ROOT_DIR as PROJECT_ROOT
from backend.infrastructure.network import public_url
from backend.infrastructure.runtime import browser_executable


def needs_verification(page: Any) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=1500)
        return any(value in text for value in (
            "请完成下列验证", "验证后继续", "拖动滑块", "滑块验证", "安全验证",
        ))
    except Exception:
        return False


def verification_required(url: str, detected: bool = True) -> HTTPException:
    message = "抖音要求完成安全验证，请打开验证窗口后重试。" if detected else "未获取到抖音视频资源，请打开浏览器检查验证或登录状态后重试。"
    return HTTPException(409, {"code": "browser_verification_required", "message": message, "url": url})


@dataclass
class Session:
    context: Any
    page: Any
    visible: bool
    touched: float
    expires: float
    closed: bool = False
    target: str = ""


class BrowserSessions:
    """All Playwright calls run on one thread, including idle window events."""

    def __init__(self, root: Path, *, factory=sync_playwright, clock=time.monotonic):
        self.root = root
        self.factory = factory
        self.clock = clock
        self.commands: queue.Queue = queue.Queue(maxsize=16)
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stopping = False
        self.sessions: dict[int, Session] = {}
        self.states: dict[int, str] = {}
        self.playwright = None

    def profile(self, uid: int) -> Path:
        if not isinstance(uid, int) or uid <= 0:
            raise HTTPException(401, "请先登录。")
        return self.root / str(uid)

    def _submit(self, operation: Callable):
        future: Future = Future()
        with self.lock:
            if self.stopping:
                raise HTTPException(503, "浏览器服务正在关闭。")
            if self.thread is None:
                self.thread = threading.Thread(target=self._worker, name="douyin-browser", daemon=True)
                self.thread.start()
            try:
                self.commands.put_nowait((copy_context(), operation, future))
            except queue.Full as exc:
                raise HTTPException(429, "浏览器正忙，请稍后再试。") from exc
        try:
            return future.result(timeout=90)
        except TimeoutError as exc:
            future.cancel()
            raise HTTPException(503, "浏览器响应超时，请稍后重试。") from exc

    def _worker(self):
        try:
            while True:
                self._expire()
                try:
                    command = self.commands.get(timeout=0.1)
                except queue.Empty:
                    self._pump()
                    continue
                if command is None:
                    break
                context, operation, future = command
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(context.run(operation))
                except Exception as exc:
                    future.set_exception(exc)
                self._pump()
        finally:
            for uid in list(self.sessions):
                self._close(uid)
            if self.playwright is not None:
                self.playwright.stop()

    def _pump(self):
        for session in list(self.sessions.values()):
            if not session.closed and not session.page.is_closed():
                try:
                    session.page.wait_for_timeout(50)
                    break
                except Exception:
                    session.closed = True

    def _expire(self):
        for uid, session in list(self.sessions.items()):
            if session.closed or session.page.is_closed():
                self._close(uid)
            elif self.clock() >= session.expires:
                self._close(uid, state="expired")

    def _close(self, uid: int, state="closed", reset=False):
        session = self.sessions.pop(uid, None)
        if session:
            try:
                session.context.close()
            except Exception:
                pass
        self.states[uid] = state
        if reset:
            profile = self.profile(uid)
            if profile.exists():
                try:
                    shutil.rmtree(profile)
                except OSError as exc:
                    self.states[uid] = "error"
                    raise HTTPException(503, "浏览器数据清除失败，请关闭验证窗口后重试。") from exc

    def _ensure(self, uid: int, visible: bool | None = None) -> Session:
        profile = self.profile(uid)
        session = self.sessions.get(uid)
        if session and not session.closed and not session.page.is_closed():
            if visible is None or session.visible == visible:
                session.touched = self.clock()
                if not session.visible:
                    session.expires = self.clock() + 600
                return session
        if session:
            self._close(uid)
        if len(self.sessions) >= 4:
            idle = [(value.touched, key) for key, value in self.sessions.items() if not value.visible]
            if not idle:
                raise HTTPException(429, "验证窗口数量已达上限，请先关闭其他窗口。")
            self._close(min(idle)[1])
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        options = {
            "headless": not bool(visible), "locale": "zh-CN",
            "viewport": {"width": 1365, "height": 768}, "service_workers": "block",
            "args": ["--autoplay-policy=no-user-gesture-required", "--disable-extensions", "--disable-notifications", "--mute-audio"],
        }
        executable = browser_executable()
        if executable:
            options["executable_path"] = str(executable)
        try:
            if self.playwright is None:
                self.playwright = self.factory().start()
            context = self.playwright.chromium.launch_persistent_context(str(profile), **options)
        except Exception as exc:
            self.states[uid] = "error"
            raise HTTPException(503, "无法启动本地浏览器，请检查 Chrome/Edge 安装以及桌面显示环境。") from exc
        # Context routing also protects popups and requests made while the user verifies.
        def route_request(route):
            try:
                public_url(route.request.url)
            except HTTPException:
                route.abort()
                return
            route.continue_()
        context.route("**/*", route_request)
        page = next(iter(context.pages), None) or context.new_page()
        now = self.clock()
        session = Session(context, page, bool(visible), now, now + (900 if visible else 600))
        context.on("close", lambda *_: setattr(session, "closed", True))
        self.sessions[uid] = session
        self.states[uid] = "open"
        return session

    def run(self, uid: int, operation: Callable, *, url: str = ""):
        def execute():
            from backend.modules.accounts import service as accounts
            accounts.active_worker(uid)
            session = self._ensure(uid)
            session.target = url
            try:
                result = operation(session.page)
            except BrowserError as exc:
                self._close(uid, state="error")
                raise verification_required(url, detected=False) from exc
            accounts.active_worker(uid)
            return result
        return self._submit(execute)

    def open(self, uid: int, url: str):
        def execute():
            from backend.modules.accounts import service as accounts
            accounts.active_worker(uid)
            public_url(url)
            session = self._ensure(uid, visible=True)
            same_target = session.target == url and session.page.url != "about:blank"
            session.target = url
            try:
                if not same_target and session.page.url != url:
                    session.page.goto(url, wait_until="commit", timeout=12000)
                session.page.bring_to_front()
                session.page.wait_for_timeout(1000)
            except Exception as exc:
                self._close(uid, state="error")
                raise HTTPException(502, "验证页面加载失败，请检查网络后重新打开窗口。") from exc
            accounts.active_worker(uid)
            return self._status(uid)
        return self._submit(execute)

    def _status(self, uid: int):
        self._expire()
        session = self.sessions.get(uid)
        if session is None:
            return {"status": self.states.get(uid, "closed"), "visible": False, "expires_in": 0}
        return {"status": "waiting" if needs_verification(session.page) else "open",
                "visible": session.visible, "expires_in": max(0, int(session.expires - self.clock())), "url": session.target}

    def status(self, uid: int):
        self.profile(uid)
        return self._submit(lambda: self._status(uid))

    def close(self, uid: int, *, reset=False):
        self.profile(uid)
        return self._submit(lambda: (self._close(uid, reset=reset), self._status(uid))[1])

    def cancel_user(self, uid: int, key: str | None = None):
        if key is None:
            self.close(uid, reset=True)

    def shutdown(self):
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
            thread = self.thread
            while True:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                if command is not None and not command[2].cancelled():
                    command[2].set_exception(HTTPException(503, "浏览器服务正在关闭。"))
        if thread is not None:
            self.commands.put(None)
            thread.join(timeout=30)
sessions = BrowserSessions(PROJECT_ROOT / ".run" / "browser-profiles")
