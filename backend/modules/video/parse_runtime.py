"""Request budgets, timings and account scoped coalescing for metadata extraction."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field

from fastapi import HTTPException

from backend.infrastructure import network
from backend.modules.accounts import service as accounts

MAX_SECONDS = 25
logger = logging.getLogger("videodream.video")


@dataclass
class Budget:
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    timings: dict[str, float] = field(default_factory=dict)
    cancelled: threading.Event = field(default_factory=threading.Event)
    source_url: str | None = None
    role_checked: float = 0

    def check(self):
        from backend.infrastructure import platform_access
        if self.source_url and platform_access.platform(self.source_url) and accounts.principal.get() and time.monotonic() - self.role_checked >= 1:
            accounts.active_worker(accounts.current().id, source={"url": self.source_url})
            self.role_checked = time.monotonic()
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise HTTPException(504, "解析等待超时，请检查网络后重试。")

    def seconds(self, maximum):
        self.check()
        return max(0.05, min(maximum, self.deadline - time.monotonic()))


current: ContextVar[Budget | None] = ContextVar("video_parse_budget", default=None)
flights: dict[tuple, tuple[Future, Budget]] = {}
epochs: dict[tuple, int] = {}
lock = threading.Lock()


def check():
    if budget := current.get():
        budget.check()


def seconds(maximum):
    budget = current.get()
    return budget.seconds(maximum) if budget else maximum


@contextmanager
def stage(name):
    check()
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if budget := current.get():
            budget.timings[name] = budget.timings.get(name, 0) + elapsed
        logger.info("Video parse stage=%s duration_ms=%.1f", name, elapsed)


@contextmanager
def scope(budget=None):
    budget = budget or Budget()
    marker = current.set(budget)
    previous = network.request_guard.get()

    def guard():
        budget.check()
        if previous is not None:
            previous()

    guard_marker = network.request_guard.set(guard)
    try:
        yield budget
    finally:
        network.request_guard.reset(guard_marker)
        current.reset(marker)


def resolve(url, factory, *, force=False):
    """Only extraction is shared. Each HTTP request keeps its own history record."""
    from backend.modules.video import cache, parse_cache
    from backend.infrastructure import platform_access, video_proxy
    platform_access.require(url)
    route = video_proxy.selected(url)
    route_revision = (route or {}).get("revision", "")

    uid = accounts.owner_id() if accounts.principal.get() else 0
    identity = (uid, (url, route_revision), force)
    cache_identity = identity[:2]
    with lock:
        flight = flights.get(identity)
        if flight is None:
            if len(flights) >= 4:
                raise HTTPException(429, "解析服务正忙，请稍后重试。")
            future = Future()
            generation = epochs.get(cache_identity, 0) + int(force)
            epochs[cache_identity] = generation
            extraction_budget = Budget(source_url=url)
            flights[identity] = (future, extraction_budget)
            context = copy_context()

            def work():
                try:
                    with scope(extraction_budget) as budget:
                        if force:
                            cache.remove(cache.parse_key(url))
                        with stage("cache"):
                            result = parse_cache._get_sniff_cache(url)
                        if result is None:
                            result = factory(url)
                            check()
                            with lock:
                                if epochs.get(cache_identity) == generation:
                                    check()
                                    parse_cache._set_sniff_cache(url, result)
                                    if result.webpage_url and result.webpage_url != url:
                                        parse_cache._set_sniff_cache(result.webpage_url, result)
                        check()
                        future.set_result((result, budget.timings.copy()))
                except BaseException as exc:
                    future.set_exception(exc)
                finally:
                    with lock:
                        flights.pop(identity, None)
                        if not any(key[:2] == cache_identity for key in flights):
                            epochs.pop(cache_identity, None)

            threading.Thread(target=context.run, args=(work,), daemon=True, name="video-parse").start()
        else:
            future, extraction_budget = flight
    started = time.monotonic()
    try:
        result, timings = future.result(timeout=max(0.001, extraction_budget.deadline - time.monotonic()))
        if budget := current.get():
            budget.timings.update(timings)
            budget.timings["resolve"] = round((time.monotonic() - started) * 1000, 1)
        return result.model_copy(deep=True)
    except TimeoutError as exc:
        extraction_budget.cancelled.set()
        # The worker retains its slot until its current bounded IO has exited.
        raise HTTPException(504, "解析等待超时，请检查网络后重试。") from exc
