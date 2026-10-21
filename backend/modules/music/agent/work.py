"""Bounded I/O workers and transient resources, isolated by task owner and lease."""
from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context


current = ContextVar("scene_playlist_work", default=None)
prefetching = ContextVar("scene_playlist_prefetch", default=False)
_tasks = {}
_lock = threading.Lock()


class Stopped(RuntimeError):
    agent_policy_error = True


class Resources:
    def __init__(self):
        self.lock = threading.Lock()
        self.audio = {}
        self.timings = {}

    def audio_key(self, track):
        from backend.modules.music.agent import store
        identity = [track.get(k) for k in ("user_id", "key", "provider", "identifier", "title",
                                           "artist", "duration", "webpage_url")]
        return hashlib.sha256(store.encode(identity).encode()).hexdigest()

    def cached_audio(self, track):
        with self.lock:
            value = self.audio.get(self.audio_key(track))
            return (value[1], value[2]) if value and value[1].is_file() else None

    def retain_audio(self, track, folder, path, source):
        # Tools process at most five songs. Never retain an unbounded media cache.
        with self.lock:
            if len(self.audio) >= 5:
                return False
            self.audio[self.audio_key(track)] = (folder, path, source)
            return True

    def discard_audio(self, track):
        with self.lock:
            value = self.audio.pop(self.audio_key(track), None)
        if value:
            value[0].cleanup()

    def record(self, stage, seconds=0, cached=False):
        with self.lock:
            value = self.timings.setdefault(stage, {"seconds": 0, "count": 0, "cache_hits": 0})
            value["seconds"] = round(value["seconds"] + seconds, 4)
            value["count"] += 1
            value["cache_hits"] += int(cached)

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.timings.items()}

    def close(self):
        with self.lock:
            values, self.audio = list(self.audio.values()), {}
        for folder, _, _ in values:
            folder.cleanup()


def task_key(row):
    return row["user_id"], row["run_id"], row["worker_id"]


@contextmanager
def task(row):
    resources = Resources()
    key = task_key(row)
    with _lock:
        if key in _tasks:
            raise RuntimeError("任务执行资源已经存在")
        _tasks[key] = resources
    try:
        yield
    finally:
        with _lock:
            _tasks.pop(key, None)
        resources.close()


class Batch:
    def __init__(self, resources, check):
        self.resources = resources
        self.active_check = check
        self.stopped = threading.Event()
        self.failure = None
        self.lock = threading.Lock()
        self.closers = set()

    def check(self):
        if self.failure is not None:
            raise self.failure
        if self.stopped.is_set():
            raise Stopped("本批处理已停止")
        return self.active_check()

    def stop(self, error=None):
        if error is not None:
            with self.lock:
                if self.failure is None:
                    self.failure = error
        self.stopped.set()
        with self.lock:
            closers = list(self.closers)
        for close in closers:
            try:
                close()
            except Exception:
                pass

    @contextmanager
    def closing(self, close):
        with self.lock:
            self.closers.add(close)
        try:
            self.check()
            yield
        finally:
            with self.lock:
                self.closers.discard(close)


@contextmanager
def operation(row, check):
    with _lock:
        resources = _tasks.get(task_key(row))
    owned = resources is None
    resources = resources or Resources()
    batch = Batch(resources, check)
    marker = current.set(batch)
    try:
        yield batch
    finally:
        batch.stop()
        current.reset(marker)
        if owned:
            resources.close()


def submit(pool, fn, *args):
    # Principal, resource owner, lease and the network budget guard must all follow.
    return pool.submit(copy_context().run, fn, *args)


def parallel(items, fn, workers, check=None):
    """Schedule only a bounded window; surface completions without an ordered wait."""
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scene-io")
    pending = {}
    iterator = iter(items)
    batch = current.get()
    try:
        while True:
            if check:
                check()
            while len(pending) < workers:
                item = next(iterator, None)
                if item is None:
                    break
                pending[submit(pool, fn, item)] = item
            if not pending:
                return
            done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                yield item, future.result()
    finally:
        if pending and batch:
            batch.stop()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


@contextmanager
def timing(stage, cached=False):
    started = time.monotonic()
    try:
        yield
    finally:
        batch = current.get()
        if batch:
            batch.resources.record(stage, time.monotonic()-started, cached)


def run_process(command, *, check=None, timeout=30, require_success=False, **kwargs):
    """Interrupt local decoders instead of waiting for subprocess.run's timeout."""
    if check is None:
        return subprocess.run(command, timeout=timeout, check=require_success, **kwargs)
    check()
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic()+timeout
    try:
        while True:
            check()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(.1, remaining))
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                if require_success:
                    result.check_returncode()
                return result
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def probe(path, ffmpeg_dir, check=None):
    # Keep the standard probe's validation while making its subprocess cancellable.
    import json
    import math
    from backend.infrastructure.runtime import executable_name
    result = run_process([str(ffmpeg_dir/executable_name("ffprobe")), "-v", "error", "-show_streams",
                          "-show_format", "-of", "json", str(path)], check=check, capture_output=True)
    if result.returncode:
        raise RuntimeError("无法读取音频，音源可能返回了错误页面或不完整文件。")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (audio or {}).get("duration") or 0)
    if not audio or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("资源中没有有效音轨。")
    return {"codec": audio.get("codec_name"), "duration": duration, "streams": streams}
