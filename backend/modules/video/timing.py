"""Monotonic task timing; wall timestamps are display values, never duration clocks."""
from contextlib import contextmanager
import threading
import time

TERMINAL = {"finished", "failed", "cancelled"}


class ExecutionTimer:
    def __init__(self):
        self.lock = threading.RLock()
        self.origin = time.monotonic()
        self.accepted_at_ms = time.time() * 1000
        self.finished = None
        self.stages = {}
        self.start("queue", "任务排队")

    def now(self):
        return self.accepted_at_ms + (time.monotonic() - self.origin) * 1000

    def start(self, identity, label, parent_id=None):
        with self.lock:
            if self.finished is not None or identity in self.stages:
                return
            self.stages[identity] = dict(id=identity, label=label, parent_id=parent_id, status="running",
                                         started_at_ms=self.now(), finished_at_ms=None, elapsed_ms=0,
                                         attempts=0, retry_wait_ms=0, completed_units=0, total_units=0, reused=False)

    def stop(self, identity, status="finished"):
        with self.lock:
            item = self.stages.get(identity)
            if item and item["status"] == "running":
                item.update(status=status, finished_at_ms=self.now())
                item["elapsed_ms"] = item["finished_at_ms"] - item["started_at_ms"]

    def update(self, identity, **changes):
        with self.lock:
            if identity in self.stages:
                self.stages[identity].update(changes)

    def metric(self, identity, event, value=0):
        with self.lock:
            item = self.stages.get(identity)
            if item:
                if event == "model_request":
                    item.setdefault("model_requests", []).append(dict(value))
                elif event == "attempt":
                    item["attempts"] += 1
                elif event == "retry":
                    item["retry_wait_ms"] += value

    @contextmanager
    def stage(self, identity, label, parent_id=None):
        self.start(identity, label, parent_id)
        try:
            yield
        except BaseException:
            self.stop(identity, "failed")
            raise
        else:
            self.stop(identity)

    def finish(self, status):
        with self.lock:
            if self.finished is None:
                for identity in self.stages:
                    self.stop(identity, status)
                self.finished = self.now()

    def snapshot(self):
        with self.lock:
            now = self.finished if self.finished is not None else self.now()
            stages = []
            for item in self.stages.values():
                value = dict(item)
                if "model_requests" in value:
                    value["model_requests"] = [dict(request) for request in value["model_requests"]]
                if value["status"] == "running":
                    value["elapsed_ms"] = max(0, now - value["started_at_ms"])
                stages.append(value)
            return dict(version=1, accepted_at_ms=self.accepted_at_ms, server_now_ms=now,
                        finished_at_ms=self.finished, elapsed_ms=max(0, now-self.accepted_at_ms), stages=stages)


def snapshot(value):
    return value.snapshot() if isinstance(value, ExecutionTimer) else value


def freeze_saved(value, status="failed"):
    """A dead worker's last recorded elapsed time excludes unknown offline time."""
    if not isinstance(value, dict) or value.get("finished_at_ms") is not None:
        return value
    value = dict(value)
    end = value["accepted_at_ms"] + value["elapsed_ms"]
    value.update(finished_at_ms=end, server_now_ms=end)
    value["stages"] = [dict(s, status=status, finished_at_ms=s["started_at_ms"]+s["elapsed_ms"])
                       if s["status"] == "running" else s for s in value.get("stages", [])]
    return value
