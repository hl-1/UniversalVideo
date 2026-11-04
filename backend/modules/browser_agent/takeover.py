"""Short lived authenticated takeover. Cookies stay in executor memory only."""
from __future__ import annotations

import base64
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from backend.agents import runtime, store
from backend.infrastructure.database import database
from backend.modules.browser_agent import executor

_sessions = {}
_lock = threading.Lock()
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-takeover")


class Input(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(ge=0)
    observation_revision: int = Field(ge=1)
    session: str = Field(default="", max_length=64)
    view_revision: str = Field(default="", max_length=64)
    action: Literal["open", "click", "fill", "continue", "close"] = "open"
    element: int = Field(default=0, ge=0, le=99)
    value: str = Field(default="", max_length=200)


def pop_state(task_id, uid):
    with _lock:
        session = _sessions.get(task_id)
        if session and session["uid"] == uid and session["expires"] > time.monotonic():
            return {"state": session.get("state"), "url": session["url"]}
    return None


def handle(task_id, user, payload):
    data = Input.model_validate(payload)
    with database() as cursor:
        task = store.get(cursor, task_id, user.id, True)
        if task["kind"] != "browser_playlist" or task["status"] not in {"waiting_auth", "waiting_input"}:
            raise HTTPException(409, "任务当前不允许接管")
        if task["revision"] != data.revision or task["context"].get("observation_revision") != data.observation_revision:
            raise HTTPException(409, "接管版本已失效，请刷新任务")
    with _lock:
        for key, value in list(_sessions.items()):
            if value["expires"] <= time.monotonic():
                del _sessions[key]
        session = _sessions.get(task_id)
        if data.action == "open" and not session:
            session = {"token": secrets.token_urlsafe(24), "uid": user.id, "sid": user.session_id,
                       "state": None, "fields": {}, "mutex": threading.Lock(), "view_revision": "", "elements": [], "url": task["context"]["observation"]["url"], "expires": time.monotonic() + 600}
            _sessions[task_id] = session
        if not session or session["uid"] != user.id or session["sid"] != user.session_id or (data.action != "open" and session["token"] != data.session):
            raise HTTPException(403, "接管会话不匹配或已过期")
        if data.action == "close":
            _sessions.pop(task_id, None)
            return {"closed": True}
    if data.action != "open" and data.view_revision != session["view_revision"]:
        raise HTTPException(409, "接管观察已失效，请重新打开接管")
    if data.action == "continue":
        with database() as cursor:
            live = store.get(cursor, task_id, user.id, True)
            if live["revision"] != data.revision:
                raise HTTPException(409, "任务版本已变化")
            live = store.update(cursor, live, status="queued", available_at=store.now(), context=live["context"] | {"resume": {"takeover": True}, "message": "人工接管完成，等待重新观察"})
        session["fields"].clear()
        runtime.wake()
        return {"task": store.public(live)}
    # Playwright runs on its own thread. Each interaction opens and closes the
    # browser; human waiting holds neither a browser nor a worker slot.
    if not session["mutex"].acquire(blocking=False):
        raise HTTPException(409, "接管操作正在执行")
    try:
        result = _worker.submit(interact, task_id, session, data).result(timeout=45)
    finally:
        session["mutex"].release()
    result |= {"session": session["token"], "revision": task["revision"], "observation_revision": data.observation_revision}
    return result


def elements(current):
    selector = 'a[href],button,input:not([type=hidden]):not([type=file]),textarea,[role=button]'
    visible = [e for e in current.locator(selector).all() if e.is_visible()][:100]
    labels = [{"id": i, "label": (e.inner_text() or e.get_attribute("aria-label") or e.get_attribute("placeholder") or e.evaluate("e => e.tagName"))[:100],
               "type": e.get_attribute("type") or "", "tag": e.evaluate("e => e.tagName.toLowerCase()"),
               "dom_id": e.get_attribute("id") or "", "name": e.get_attribute("name") or "", "href": e.get_attribute("href") or ""}
              for i, e in enumerate(visible)]
    return visible, labels


def interact(task_id, session, data):
    with executor.browser(task_id, storage_state=session["state"]) as page:
        executor.read_with_retry(lambda: page.goto(session["url"], wait_until="domcontentloaded"))
        current = executor.frame(page)
        visible, labels = elements(current)
        # Rebuilding Chromium invalidates element handles. Only a freshly
        # observed, identical DOM may receive the user's pending interaction.
        if data.action in {"click", "fill"} and labels != session["elements"]:
            session["fields"].clear()
            raise HTTPException(409, "页面元素已变化，请重新打开接管后选择")
        for index, value in session["fields"].items():
            if index >= len(visible) or labels[index] != session["elements"][index]:
                session["fields"].clear()
                raise HTTPException(409, "输入元素已变化，请重新打开接管")
            visible[index].fill(value)
        if data.action in {"click", "fill"}:
            if data.element >= len(visible):
                raise HTTPException(409, "接管元素已变化")
            if data.action == "fill":
                if labels[data.element]["tag"] not in {"input", "textarea"}:
                    raise HTTPException(422, "所选元素不能填写")
                visible[data.element].fill(data.value)
                session["fields"][data.element] = data.value
            else:
                visible[data.element].click()
                session["fields"].clear()
        if not executor.permitted(page.url):
            raise HTTPException(403, "接管页面离开支持站点")
        session["state"] = page.context.storage_state()
        session["url"] = page.url
        session["expires"] = time.monotonic() + 600
        current = executor.frame(page)
        _, labels = elements(current)
        session["elements"] = labels
        session["view_revision"] = secrets.token_urlsafe(24)
        screenshot = base64.b64encode(page.screenshot(mask=[current.locator("input,textarea")])).decode()
        return {"url": page.url, "elements": labels, "view_revision": session["view_revision"], "screenshot": "data:image/png;base64," + screenshot}
