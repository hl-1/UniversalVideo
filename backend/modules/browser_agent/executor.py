from __future__ import annotations

from contextlib import contextmanager
import secrets
import time
from urllib.parse import parse_qs, urlparse, urljoin

import pymysql
from fastapi import HTTPException
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from backend.agents import store
from backend.infrastructure.database import database, database_config


def permitted(url):
    p = urlparse(url)
    return p.scheme == "https" and p.hostname == "music.163.com" and p.port in {None, 443} and not p.username and not p.password


@contextmanager
def browser(task_id, *, headless=True, storage_state=None):
    # A connection lock provides global concurrency across backend processes;
    # autocommit avoids holding a transaction over browser/network operations.
    connection = pymysql.connect(**(database_config() | {"autocommit": True}))
    process = None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('videodream:browser-agent',0) AS acquired")
            if not cursor.fetchone()["acquired"]:
                raise HTTPException(409, "浏览器执行器忙，请稍后重试")
        with sync_playwright() as playwright:
            try:
                process = playwright.chromium.launch(headless=headless)
                context = process.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=False, service_workers="block", storage_state=storage_state)
                def guard(route):
                    p = urlparse(route.request.url)
                    host = p.hostname or ""
                    allowed = p.scheme == "https" and p.port in {None, 443} and (host == "music.163.com" or host.endswith(".music.163.com") or host.endswith(".music.126.net") or host.endswith(".music.127.net"))
                    if allowed:
                        route.continue_()
                    else:
                        route.abort()
                context.route("**/*", guard)
                page = context.new_page()
                page.set_default_timeout(8000)
                page.set_default_navigation_timeout(15000)
                yield page
            finally:
                if process:
                    process.close()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK('videodream:browser-agent')")
        connection.close()


def read_with_retry(callback, task_id=None):
    for attempt in range(3):
        if task_id:
            store.check(task_id)
        try:
            return callback()
        except PlaywrightTimeout:
            if attempt == 2:
                raise


def frame(page):
    return next((f for f in page.frames if f != page.main_frame and f.url.startswith("https://music.163.com/")), page.main_frame)


def ready(page):
    locator = page.locator('iframe[name="contentFrame"]')
    locator.wait_for(state="attached", timeout=8000)
    current = locator.element_handle().content_frame()
    if current is None:
        raise PlaywrightTimeout("站点内容尚未加载")
    route = urlparse(page.url)
    expected = urlparse(urljoin(page.url, route.fragment)) if route.fragment.startswith("/") else route
    expected_id = parse_qs(expected.query).get("id")
    current.wait_for_url(lambda url: urlparse(url).path.rstrip("/") == expected.path.rstrip("/")
                         and (not expected_id or parse_qs(urlparse(url).query).get("id") == expected_id),
                         wait_until="domcontentloaded", timeout=8000)
    current.wait_for_load_state("domcontentloaded")
    current.locator("body").wait_for(state="visible")


def observe(page, task_id):
    if not permitted(page.url):
        raise HTTPException(403, "页面离开已适配站点")
    with database() as cursor:
        task = store.get(cursor, task_id, lock=True)
        revision = int(task["context"].get("observation_revision", 0)) + 1
        nonce = secrets.token_hex(6)
    read_with_retry(lambda: ready(page), task_id)
    current = frame(page)
    observation = current.evaluate(r"""({revision, nonce}) => {
      const elements = [...document.querySelectorAll('a[href],button,input:not([type=password]):not([type=hidden]),[role=button],textarea')]
        .filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height)
        .filter(e => (e.innerText||e.getAttribute('title')||e.querySelector('[title]')?.getAttribute('title')||e.getAttribute('placeholder')||'').trim())
        .filter(e => !e.getAttribute('href')?.startsWith('javascript:')).slice(0,60)
        .map((e,i) => { const id = nonce+'-'+i; e.setAttribute('data-vd-agent',id);
          return {id,tag:e.tagName.toLowerCase(),label:(e.getAttribute('title')||e.querySelector('[title]')?.getAttribute('title')||e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,100),type:e.getAttribute('type')||'',href:e.getAttribute('href')||''}; });
      const clone=document.body.cloneNode(true); clone.querySelectorAll('.soil,script,style,input,textarea').forEach(e=>e.remove());
      return {revision, url:location.href, title:document.title.slice(0,150), text:(clone.textContent||'').replace(/\s+/g,' ').slice(0,2500),elements};
    }""", {"revision": revision, "nonce": nonce})
    observation["page_type"] = "playlist_detail" if urlparse(current.url).path.rstrip("/") == "/playlist" else "catalogue"
    observation["hash"] = store.digest(observation)
    with database() as cursor:
        task = store.get(cursor, task_id, lock=True)
        if task["status"] != "running":
            raise HTTPException(409, "任务已停止")
        store.update(cursor, task, context=task["context"] | {"observation_revision": revision, "observation": observation,
                     "message": "已观察页面", "node": "observe"})
    return observation


def execute(page, observation, action):
    if action.revision != observation["revision"]:
        raise HTTPException(409, "模型使用了过期观察")
    current = frame(page)
    if current.url != observation["url"]:
        raise HTTPException(409, "页面已变化，请重新观察")
    if action.kind == "navigate":
        if not permitted(action.url):
            raise HTTPException(403, "导航地址不在已适配站点范围内")
        page.goto(action.url, wait_until="domcontentloaded")
    elif action.kind in {"click", "fill"}:
        element = next((e for e in observation["elements"] if e["id"] == action.element_id), None)
        if element is None:
            raise HTTPException(409, "元素不属于当前观察")
        locator = current.locator(f'[data-vd-agent="{element["id"]}"]')
        if action.kind == "fill":
            if element["type"] in {"password", "file"} or element["tag"] not in {"input", "textarea"}:
                raise HTTPException(403, "此输入须由用户接管")
            locator.fill(action.text)
        else:
            if "/search" in current.url and element["label"] in {"歌单", "搜索"} and element.get("href") == "javascript:void(0)":
                locator.click()
                return {"ok": True}
            target = urljoin(current.url, element.get("href", ""))
            path = urlparse(target).path
            if element["tag"] != "a" or not permitted(target) or not path.startswith(("/playlist", "/song", "/artist", "/search", "/discover")):
                raise HTTPException(403, "此页面操作需要人工接管")
            # Follow the observed read URL directly; arbitrary onclick handlers
            # are never used by automatic actions.
            page.goto(target, wait_until="domcontentloaded")
    elif action.kind == "scroll":
        current.locator("body").evaluate("e => window.scrollBy(0,600)")
    elif action.kind == "extract":
        tracks = current.evaluate("""() => [...document.querySelectorAll('a[href*="/song?id="]')].slice(0,500).map(a => {
          const row=a.closest('tr') || a.parentElement; const artist=row.querySelector('.text[title],a[href*="/artist?id="]');
          return {title:(a.getAttribute('title')||a.querySelector('[title]')?.getAttribute('title')||a.innerText||'').trim(),artist:artist?(artist.getAttribute('title')||artist.innerText||'').trim():'',href:a.getAttribute('href')};
        })""")
        import re
        result, seen = [], set()
        for track in tracks:
            match = re.search(r"[?&]id=(\d{1,24})", track["href"] or "")
            if match and track["title"] and match[1] not in seen:
                seen.add(match[1])
                result.append({"title": track["title"][:255], "artist": track["artist"][:255], "provider": "netease",
                               "identifier": match[1], "webpage_url": f"https://music.163.com/song?id={match[1]}"})
        return {"tracks": result}
    return {"ok": True}
