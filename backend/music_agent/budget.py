from __future__ import annotations

import time

from fastapi import HTTPException

from backend import library
from backend.music_agent import store


class BudgetExceeded(RuntimeError):
    agent_policy_error = True

    def __init__(self, message, resource=None):
        super().__init__(message)
        self.resource = resource


def charge(run_id, **costs):
    with library.database() as cursor:
        row = store.get(cursor, run_id, lock=True)
        if row["status"] != "running" or not row["lease_until"] or row["lease_until"] <= time.time():
            raise HTTPException(409, "任务已停止")
        context = row["context"]
        used = dict(context.get("budget", {}))
        limits = {"network_requests": 160, "tools": 60, "analysis": 100} | context["limits"]
        for key, cost in costs.items():
            if cost < 0:
                raise ValueError("Budget cannot be refunded")
            if used.get(key, 0)+cost > limits.get(key, 0):
                raise BudgetExceeded("网络请求额度已用完" if key == "network_requests" else f"已达到{key}预算上限", key)
        for key, cost in costs.items():
            used[key] = used.get(key, 0)+cost
        store.update(cursor, row, context=context | {"budget": used})
    return used


def mark_step(run_id, tool, data=None, error=None, elapsed=0):
    with library.database() as cursor:
        row = store.get(cursor, run_id, lock=True)
        if row["status"] != "running":
            raise HTTPException(409, "任务已停止")
        step = {"tool": tool, "at": time.time(), "elapsed": round(elapsed, 3), "error": error,
                "count": len((data or {}).get("tracks", (data or {}).get("playlists", [])))}
        store.update(cursor, row, steps=(row["steps"]+[step])[-200:],
                     context=row["context"] | {"message": f"{tool} · {step['count']} 项"})


def validation_only(row):
    limit = row['context']['limits'].get('network_requests', 160)
    return row['context'].get('budget', {}).get('network_requests', 0) >= limit * 3 // 4
