"""Bounded I/O fan-out with copied account context and shared provider capacity."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import threading

model_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="video-model")
speech_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="video-speech")
provider_slots = threading.BoundedSemaphore(3)
text_config = ContextVar("video_text_config", default=None)
request_metrics = ContextVar("video_request_metrics", default=None)
request_checkpoint = ContextVar("video_request_checkpoint", default=lambda: None)
chunk_progress = ContextVar("video_chunk_progress", default=lambda done, total: None)


@contextmanager
def provider_slot(checkpoint=lambda: None):
    while not provider_slots.acquire(timeout=0.2):
        checkpoint()
    try:
        checkpoint()
        yield
    finally:
        provider_slots.release()


def bounded_map(function, items, *, executor, workers, checkpoint=lambda: None, progress=lambda done, total: None):
    """Keep at most workers dispatched; return source order even on out-of-order completion."""
    items = list(items)
    results = [None] * len(items)
    pending = {}
    next_index = 0
    completed = 0
    try:
        while pending or next_index < len(items):
            checkpoint()
            while next_index < len(items) and len(pending) < workers:
                checkpoint()
                context = copy_context()
                future = executor.submit(context.run, function, items[next_index])
                pending[future] = next_index
                next_index += 1
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                results[index] = future.result()
                completed += 1
                progress(completed, len(items))
        return results
    finally:
        # Wait for in-flight requests before callers remove Windows temporary audio files.
        for future in pending:
            future.cancel()
        if pending:
            wait(pending)
