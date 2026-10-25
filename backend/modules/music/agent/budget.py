from __future__ import annotations

import time
import secrets

from fastapi import HTTPException

from backend.modules.music import library
from backend.modules.music.agent import store
from backend.modules.music.agent.faults import ModelFault, ModelOutputError
from backend.modules.music.agent.models import POLICY_VERSION


TOKEN_LIMIT = 150000
MODEL_CALL_LIMIT = 40
SAFETY_TOKENS = 5000


def usage(context):
    ledger = context.get("model_requests", [])
    return {"actual": context.get("actual_tokens", 0),
            "reserved": sum(r["reserved"] for r in ledger if r["state"] == "in_flight"),
            "unconfirmed": sum(r["reserved"] for r in ledger if r["state"] == "unconfirmed")}


def reserve(run_id, amount, phase, signature, deadline, correction=False):
    """Serialize admission with every other request and cancellation for this task."""
    with library.database() as cursor:
        from backend.modules.music.discovery import usage as daily_usage, policy
        daily_usage.lock_run(cursor, run_id)
        row = store.get(cursor, run_id, lock=True)
        if row["status"] != "running" or not row["lease_until"] or row["lease_until"] <= time.time():
            raise HTTPException(409, "任务已停止")
        ctx = row["context"]
        if row["policy_version"] != POLICY_VERSION:
            raise ModelFault("旧版任务不能继续付费调用，请重新生成")
        ledger = list(ctx.get("model_requests", []))
        totals = usage(ctx)
        if ctx.get("model_stop") or totals["unconfirmed"]:
            raise ModelFault("模型用量待确认或调用已停止，请查看任务结果")
        if totals["reserved"]:
            raise ModelFault("当前任务已有模型请求正在执行")
        limits = ctx["limits"]
        used = dict(ctx.get("budget", {}))
        call_limit = min(MODEL_CALL_LIMIT, limits.get("model_calls", MODEL_CALL_LIMIT))
        if used.get("model_calls", 0) >= call_limit:
            raise BudgetExceeded(f"已达到模型调用{call_limit}次上限", "model_calls")
        if phase == "planning" and used.get("decisions", 0) >= min(6, limits.get("decisions", 6)):
            raise BudgetExceeded("已达到规划调整次数上限", "decisions")
        previous = [r for r in ledger if r["signature"] == signature and r["phase"] == phase]
        if previous and not correction:
            raise ModelFault("相同输入已执行过模型调用，已阻止重复请求")
        if correction and (used.get("corrections", 0) >= 2 or any(r.get("correction") for r in previous)):
            raise ModelOutputError("模型格式纠错次数已用完，已停止调用")
        limit = min(TOKEN_LIMIT, limits.get("tokens", TOKEN_LIMIT))
        margin = min(SAFETY_TOKENS, limit // 30)
        if sum(totals.values()) + amount + margin > limit:
            raise BudgetExceeded("剩余 token 不足以安全执行下一次模型请求", "tokens")
        daily = row['request'].get('kind') == policy.KIND
        if daily:
            daily_usage.candidate_admission(cursor, row, amount=amount + margin)
        request = {"id": secrets.token_hex(16), "phase": phase, "signature": signature,
                   "reserved": amount, "started_at": time.time(), "deadline": deadline,
                   "state": "in_flight", "actual": None, "correction": correction}
        if daily:
            request['accounting_date'] = str(policy.today())
        used["model_calls"] = used.get("model_calls", 0) + 1
        if phase == "planning":
            used["decisions"] = used.get("decisions", 0) + 1
        if correction:
            used["corrections"] = used.get("corrections", 0) + 1
        store.update(cursor, row, context=ctx | {"budget": used, "model_requests": ledger + [request],
                     "stage": phase, "message": {"parse": "正在理解听歌需求", "planning": "正在调整检索方案",
                         "semantic": "正在分析歌词与场景适配"}.get(phase, "正在执行模型请求")})
        if daily:
            daily_usage.sync_candidate(cursor, row['user_id'])
        return request


def settle(run_id, request_id, actual=None, error=None, dispatched=None, usage_details=None):
    """Accounting alone may finish after cancellation/lease change, exactly once."""
    marker = store.execution_lease.set(None)
    try:
        with library.database() as cursor:
            from backend.modules.music.discovery import usage as daily_usage, policy
            daily_usage.lock_run(cursor, run_id)
            row = store.get(cursor, run_id, lock=True, include_deleted=True)
            ctx = row["context"]
            ledger = [dict(r) for r in ctx.get("model_requests", [])]
            request = next((r for r in ledger if r["id"] == request_id), None)
            if not request or request["state"] == "settled":
                return
            if actual is None and request["state"] == "unconfirmed":
                return
            request.update(state="settled" if actual is not None else "unconfirmed", actual=actual,
                           settled_at=time.time(), error=error)
            if usage_details is not None:
                request['usage'] = usage_details
            if actual is not None:
                request['reservation_delta'] = actual-request['reserved']
            if dispatched is not None:
                request["dispatched"] = dispatched
            confirmed = ctx.get("actual_tokens", 0) + (actual if actual is not None else 0)
            used = dict(ctx.get("budget", {})) | {"tokens": confirmed}
            updated = ctx | {"model_requests": ledger, "actual_tokens": confirmed, "budget": used}
            if actual is None or error:
                updated["model_stop"] = error or "模型用量未确认，已停止后续调用"
            elif confirmed > min(TOKEN_LIMIT, ctx["limits"].get("tokens", TOKEN_LIMIT)):
                updated["model_stop"] = "模型累计实际用量超过任务上限，已如实记账并停止调用"
            changes = {"context": updated}
            if row["result"]:
                changes["result"] = row["result"] | {"budget": used}
            store.update(cursor, row, **changes)
            if row['request'].get('kind') == policy.KIND:
                daily_usage.sync_candidate(cursor, row['user_id'])
    finally:
        store.execution_lease.reset(marker)


class BudgetExceeded(RuntimeError):
    agent_policy_error = True

    def __init__(self, message, resource=None):
        super().__init__(message)
        self.resource = resource


def charge(run_id, *, reserve_playback=False, **costs):
    with library.database() as cursor:
        from backend.modules.music.discovery import usage as daily_usage, policy
        daily_usage.lock_run(cursor, run_id)
        row = store.get(cursor, run_id, lock=True)
        if row["status"] != "running" or not row["lease_until"] or row["lease_until"] <= time.time():
            raise HTTPException(409, "任务已停止")
        context = row["context"]
        used = dict(context.get("budget", {}))
        limits = {"network_requests": 160, "tools": 60, "analysis": 100} | context["limits"]
        if (reserve_playback and costs.get("network_requests", 0) > 0
                and used.get("network_requests", 0)+costs["network_requests"] > limits["network_requests"]*3//4):
            raise BudgetExceeded("剩余网络额度已预留给播放校验", "network_reserve")
        for key, cost in costs.items():
            if cost < 0:
                raise ValueError("Budget cannot be refunded")
            if used.get(key, 0)+cost > limits.get(key, 0):
                raise BudgetExceeded("网络请求额度已用完" if key == "network_requests" else f"已达到{key}预算上限", key)
        for key, cost in costs.items():
            used[key] = used.get(key, 0)+cost
        changes = {"budget": used}
        if row['request'].get('kind') == policy.KIND and costs.get('network_requests'):
            daily_usage.candidate_admission(cursor, row, network=costs['network_requests'])
            by_date = dict(context.get('network_by_date', {}))
            if not by_date and context.get('budget', {}).get('network_requests'):
                by_date[str(policy.today(row['created_at']))] = context['budget']['network_requests']
            day = str(policy.today())
            by_date[day] = by_date.get(day, 0) + costs['network_requests']
            changes['network_by_date'] = by_date
        store.update(cursor, row, context=context | changes)
        if row['request'].get('kind') == policy.KIND:
            daily_usage.sync_candidate(cursor, row['user_id'])
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
