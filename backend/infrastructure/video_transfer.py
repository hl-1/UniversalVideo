"""Bounded HTTP range transfers with validated, local resumable parts."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path

import requests

from backend.infrastructure import video_network


THRESHOLD = 16 * 1024 * 1024
CONNECTIONS = 4
RETRIES = 3
TTL = 24 * 60 * 60
CHUNK = 256 * 1024
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class SourceExpired(RuntimeError):
    pass


class SourceChanged(RuntimeError):
    pass


class RangeUnavailable(RuntimeError):
    pass


def cleanup(root: Path, *, owner=None, task_id=None):
    """Remove expired or explicitly deleted task parts, without touching active locks."""
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        with _locks_guard:
            lock = _locks.setdefault(str(directory), threading.Lock())
        if not lock.acquire(blocking=False):
            continue
        try:
            try:
                meta = json.loads((directory / "state.json").read_text("utf-8"))
                expired = time.time() - meta["updated"] > TTL
                selected = owner is not None and meta.get("owner") == owner and (task_id is None or meta.get("task_id") == task_id)
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    expired, selected = time.time() - directory.stat().st_mtime > TTL, False
                except FileNotFoundError:
                    continue
            if expired or selected:
                shutil.rmtree(directory, ignore_errors=True)
        finally:
            lock.release()


def _identity(response):
    etag = response.headers.get("ETag", "")
    # Weak ETags cannot establish identity for If-Range. A Last-Modified date
    # is usable only when at least 60 seconds older than the server's Date.
    if etag and not etag.startswith("W/"):
        return "ETag", etag
    modified = response.headers.get("Last-Modified", "")
    try:
        if modified and (parsedate_to_datetime(response.headers["Date"]) - parsedate_to_datetime(modified)).total_seconds() >= 60:
            return "Last-Modified", modified
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    return "", ""


def _validate(response):
    if response.status_code in {401, 403, 410}:
        raise SourceExpired("视频源已过期或拒绝访问，请重新解析。")
    response.raise_for_status()
    kind = response.headers.get("Content-Type", "").lower()
    if kind.startswith(("text/", "image/", "application/json")):
        raise RuntimeError("视频源返回了网页、图片或错误信息。")
    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
        raise RangeUnavailable("视频源使用压缩传输，无法安全拼接分段。")


def _range(response):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
    return tuple(map(int, match.groups())) if match else None


def download(url: str, target: Path, *, referer: str, headers: dict, root: Path,
             owner: int, key: str, task_id: str = "", progress=lambda data: None,
             check=lambda: None, cancel=None, connections: int = CONNECTIONS) -> Path:
    cleanup(root)
    directory = root / hashlib.sha256(f"{owner}:{key}".encode()).hexdigest()
    with _locks_guard:
        lock = _locks.setdefault(str(directory), threading.Lock())
    while not lock.acquire(timeout=0.25):
        check()
        if cancel is not None and cancel.is_set():
            raise RuntimeError("任务已停止。")
    stop = threading.Event()
    resumable = False

    def checkpoint():
        if stop.is_set() or (cancel is not None and cancel.is_set()):
            raise RuntimeError("任务已停止。")

    request_headers = {k: v for k, v in headers.items() if k.lower() not in {"range", "if-range", "accept-encoding"}}
    request_headers["Accept-Encoding"] = "identity"

    def get(extra):
        checkpoint()
        return video_network.get(url, referer=referer, headers={**request_headers, **extra}, stream=True, timeout=(10, 15))

    started = time.monotonic()
    received, initial = [0], [0]
    mode = {"connections": 1, "reason": "正在探测分段支持"}
    counter_lock = threading.Lock()

    def emit(total):
        check()
        checkpoint()
        with counter_lock:
            count = received[0]
        speed = max(0, count - initial[0]) / max(0.001, time.monotonic() - started)
        progress({"status": "downloading", "downloaded_bytes": count, "total_bytes": total,
                  "speed": speed, "eta": (total - count) / speed if total and speed else None,
                  "connections": mode["connections"], "fallback_reason": mode["reason"]})

    def single():
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"version": 0, "owner": owner, "task_id": task_id, "updated": time.time()}), "utf-8")
        partial = directory / "whole.part"
        for attempt in range(RETRIES + 1):
            try:
                received[0] = 0
                with get({}) as response:
                    _validate(response)
                    total = int(response.headers.get("Content-Length") or 0)
                    if response.status_code != 200:
                        raise RuntimeError("视频源没有返回完整文件。")
                    last = 0.0
                    with partial.open("wb") as output:
                        for chunk in response.iter_content(CHUNK):
                            checkpoint()
                            if chunk:
                                output.write(chunk)
                                received[0] += len(chunk)
                            if time.monotonic() - last >= 0.25:
                                emit(total)
                                last = time.monotonic()
                    if not received[0] or (total and received[0] != total):
                        raise requests.ConnectionError("视频源提前结束，文件不完整。")
                emit(total)
                partial.replace(target)
                return
            except requests.RequestException:
                if attempt == RETRIES:
                    raise

    try:
        check()
        # Probe the real GET path; HEAD / Accept-Ranges alone are insufficient.
        for attempt in range(RETRIES + 1):
            try:
                with get({"Range": "bytes=0-0"}) as response:
                    _validate(response)
                    extent, identity = _range(response), _identity(response)
                    supported = response.status_code == 206 and extent and extent[:2] == (0, 0) and extent[2] > 0 and identity[1]
                    if supported:
                        probe = next(response.iter_content(2), b"")
                        supported = len(probe) == 1
                break
            except requests.RequestException:
                if attempt == RETRIES:
                    raise
        if not supported:
            mode.update(connections=1, reason="源站未提供可校验的 Range 分段")
            single()
        else:
            resumable = True
            total = extent[2]
            count = min(CONNECTIONS, max(1, connections)) if total > THRESHOLD else 1
            mode.update(connections=count, reason="小文件单连接" if count == 1 else "")
            ranges = [[total * i // count, total * (i + 1) // count - 1] for i in range(count)]
            state = {"version": 1, "total": total, "identity": list(identity), "ranges": ranges,
                     "owner": owner, "task_id": task_id, "updated": time.time()}
            state_path = directory / "state.json"
            try:
                old = json.loads(state_path.read_text("utf-8"))
                reusable = all(old.get(k) == state[k] for k in ("version", "total", "identity", "ranges", "owner")) and time.time() - old["updated"] <= TTL
            except (OSError, ValueError, KeyError, TypeError):
                reusable = False
            if not reusable:
                shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state), "utf-8")
            parts = [directory / f"{i}.part" for i in range(count)]
            for part, (start, end) in zip(parts, ranges):
                if part.exists() and part.stat().st_size > end - start + 1:
                    part.unlink()
            received[0] = initial[0] = sum(part.stat().st_size for part in parts if part.exists())

            def transfer(index):
                start, end = ranges[index]
                part = parts[index]
                for attempt in range(RETRIES + 1):
                    checkpoint()
                    offset = part.stat().st_size if part.exists() else 0
                    if offset == end - start + 1:
                        return
                    try:
                        with get({"Range": f"bytes={start + offset}-{end}", "If-Range": identity[1]}) as response:
                            _validate(response)
                            if response.status_code == 200:
                                if _identity(response) != identity:
                                    raise SourceChanged("视频内容已改变，需要重新下载。")
                                raise RangeUnavailable("视频源忽略分段请求。")
                            if _identity(response) != identity:
                                raise SourceChanged("视频源校验值已改变，已停止拼接。")
                            if response.status_code != 206 or _range(response) != (start + offset, end, total):
                                raise RangeUnavailable("视频源返回了不匹配的分段范围。")
                            with part.open("ab") as output:
                                for chunk in response.iter_content(CHUNK):
                                    checkpoint()
                                    if offset + len(chunk) > end - start + 1:
                                        raise RangeUnavailable("视频分段超出声明长度。")
                                    if chunk:
                                        output.write(chunk)
                                        offset += len(chunk)
                                        with counter_lock:
                                            received[0] += len(chunk)
                            if offset != end - start + 1:
                                raise requests.ConnectionError("视频分段提前结束。")
                        return
                    except requests.RequestException:
                        if attempt == RETRIES:
                            raise

            try:
                with ThreadPoolExecutor(max_workers=count, thread_name_prefix="video-range") as pool:
                    futures = [pool.submit(copy_context().run, transfer, i) for i in range(count)]
                    pending = set(futures)
                    try:
                        while pending:
                            done, pending = wait(pending, timeout=0.25, return_when="FIRST_COMPLETED")
                            for future in done:
                                future.result()
                            emit(total)
                    except BaseException:
                        stop.set()
                        raise
                # Assemble locally only after every exact-length segment succeeds.
                combined = directory / "assembled.part"
                with combined.open("wb") as output:
                    for part in parts:
                        checkpoint()
                        with part.open("rb") as source:
                            while chunk := source.read(CHUNK):
                                checkpoint()
                                output.write(chunk)
                check()
                combined.replace(target)
            except RangeUnavailable:
                mode.update(connections=1, reason="源站分段校验不通过，已回退单连接")
                resumable = False
                stop.clear()
                initial[0] = 0
                single()
        shutil.rmtree(directory, ignore_errors=True)
        return target
    except SourceChanged:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        if not resumable or (cancel is not None and cancel.is_set()):
            shutil.rmtree(directory, ignore_errors=True)
        lock.release()
