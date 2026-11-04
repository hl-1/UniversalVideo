"""Transactional admission and replay for every new-workflow model request."""
from __future__ import annotations

from contextvars import ContextVar
import threading
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
import tiktoken
from fastapi import HTTPException
from langchain_core.embeddings import Embeddings
from langchain_core.messages import convert_to_messages
from langchain_openai import ChatOpenAI

from backend.agents import config, store
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts

current_task = ContextVar("agent_current_task", default=None)
_clients, _clients_lock = {}, threading.Lock()


def cancel(task_id=None):
    with _clients_lock:
        clients = [client for key, values in _clients.items() if task_id is None or key == task_id for client in values]
    for client in clients:
        client.close()


class UsagePending(RuntimeError):
    pass


def tokens(value):
    return len(tiktoken.get_encoding("cl100k_base").encode(store.encode(value), disallowed_special=()))


def pricing(model, usage=None):
    """Optional operator-supplied price snapshot; absent prices remain unknown."""
    try:
        value = json.loads(accounts.environment("VIDEODREAM_AGENT_PRICING", "{}")).get(model)
        if not value:
            return None
        input_rate, output_rate = Decimal(str(value["input_per_million"])), Decimal(str(value["output_per_million"]))
        if not input_rate.is_finite() or not output_rate.is_finite() or min(input_rate, output_rate) < 0:
            raise ValueError("invalid price")
        result = {"currency": str(value["currency"]), "version": str(value["version"]), "input_per_million": str(input_rate), "output_per_million": str(output_rate), "estimated": True}
        if usage is not None:
            units = usage.get("billed_units", usage)
            inputs = units.get("input_tokens", units.get("prompt_tokens"))
            outputs = units.get("output_tokens", units.get("completion_tokens"))
            if isinstance(inputs, int) and isinstance(outputs, int):
                result["amount"] = str((input_rate * inputs + output_rate * outputs) / 1_000_000)
        return result
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise HTTPException(503, "模型价格配置无效，请检查 VIDEODREAM_AGENT_PRICING") from None


def reserve(task_id, phase, model, request, amount):
    with database() as cursor:
        # Always lock user -> root -> task, including sibling model requests.
        task = store.get(cursor, task_id)
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (task["user_id"],))
        root = store.get(cursor, task["root_id"] or task_id, lock=True)
        task = store.get(cursor, task_id, lock=True)
        store.assert_running(task)
        if task["status"] != "running" or root["status"] in config.TERMINAL or root["status"] == "waiting_usage":
            raise HTTPException(409, "任务已停止")
        fingerprint = store.digest({"phase": phase, "model": model["model"], "provider": model["base_url"], "request": request})
        cursor.execute("SELECT * FROM agent_model_calls WHERE task_id=%s AND request_hash=%s ORDER BY attempt DESC LIMIT 1 FOR UPDATE", (task_id, fingerprint))
        existing = store.decode(cursor.fetchone())
        if existing:
            if existing["state"] == "settled" and existing["response"] is not None:
                return existing, True
            if existing["state"] != "settled":
                raise UsagePending("先前请求尚未核对，禁止重复付费调用")
        day = store.now().date()
        cursor.execute("SELECT root_task_id,accounting_day,state,actual_tokens,reserved_tokens FROM agent_model_calls WHERE root_task_id=%s OR (user_id=%s AND accounting_day=%s) FOR UPDATE",
                       (root["id"], task["user_id"], day))
        rows = cursor.fetchall()
        if any(r["state"] == "unknown" for r in rows):
            raise UsagePending("有模型用量待核对，后续付费调用已暂停")
        # Locking reads are essential under MySQL REPEATABLE READ: admission
        # must see reservations committed while this transaction waited on user.
        charge = lambda r: r["actual_tokens"] if r["state"] == "settled" else r["reserved_tokens"]
        root_used = sum(charge(r) or 0 for r in rows if r["root_task_id"] == root["id"])
        daily_used = sum(charge(r) or 0 for r in rows if r["accounting_day"] == day)
        if root_used + amount > root["limits"]["tokens"] or daily_used + amount > root["limits"]["daily_tokens"]:
            raise HTTPException(409, "根任务或用户当日模型预算不足")
        stamp = store.now()
        row = {"id": __import__("secrets").token_hex(16), "task_id": task_id, "root_task_id": root["id"],
               "user_id": task["user_id"], "accounting_day": day, "phase": phase,
               "provider": (urlparse(model["base_url"]).hostname or "configured")[:80], "model": model["model"],
               "state": "reserved", "request_hash": fingerprint, "attempt": existing["attempt"] + 1 if existing else 0, "reserved_tokens": amount,
               "usage": {}, "pricing": pricing(model["model"]), "created_at": stamp, "updated_at": stamp}
        cursor.execute("INSERT INTO agent_model_calls (" + ",".join(f"`{k}`" for k in row) + ") VALUES (" + ",".join(["%s"] * len(row)) + ")",
                       tuple(store.encode(v) if k in {"usage", "pricing"} and v is not None else v for k, v in row.items()))
        return row, False


def dispatch(call):
    with database() as cursor:
        row = store.get(cursor, call["task_id"], lock=True)
        store.assert_running(row)
        if row["status"] != "running":
            raise HTTPException(409, "任务已取消")
        cursor.execute("UPDATE agent_model_calls SET dispatched=TRUE,state='dispatched',started_at=%s,updated_at=%s WHERE id=%s AND state='reserved'", (store.now(), store.now(), call["id"]))
        if cursor.rowcount != 1:
            raise UsagePending("请求已发送，禁止再次发送")


def settle(call, response, usage, *, dispatched, error=None):
    actual = usage.get("total_tokens")
    if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
        actual = None if dispatched else 0
    with database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (call["user_id"],))
        root = store.get(cursor, call["root_task_id"], lock=True)
        row = store.get(cursor, call["task_id"], lock=True)
        cursor.execute("SELECT state FROM agent_model_calls WHERE id=%s FOR UPDATE", (call["id"],))
        if cursor.fetchone()["state"] == "settled":
            return
        stamp = store.now()
        price = call.get("pricing")
        if price and actual is not None:
            units = usage.get("billed_units", usage)
            inputs, outputs = units.get("input_tokens", units.get("prompt_tokens")), units.get("output_tokens", units.get("completion_tokens"))
            if isinstance(inputs, int) and isinstance(outputs, int):
                price = price | {"amount": str((Decimal(price["input_per_million"]) * inputs + Decimal(price["output_per_million"]) * outputs) / 1_000_000)}
        cursor.execute("UPDATE agent_model_calls SET state=%s,actual_tokens=%s,`usage`=%s,response=%s,pricing=%s,provider_request_id=%s,error_code=%s,settled_at=%s,updated_at=%s WHERE id=%s",
                       ("settled" if actual is not None else "unknown", actual, store.encode(usage),
                        store.encode(response) if response is not None else None, store.encode(price) if price else None,
                        str(usage.get("provider_request_id") or "")[:128] or None, error, stamp, stamp, call["id"]))
        cursor.execute("SELECT COALESCE(SUM(actual_tokens),0) AS actual,COALESCE(SUM(CASE WHEN state<>'settled' THEN reserved_tokens ELSE 0 END),0) AS reserved FROM agent_model_calls WHERE root_task_id=%s", (call["root_task_id"],))
        totals = {k: int(v) for k, v in cursor.fetchone().items()}
        cursor.execute("SELECT 1 FROM agent_model_calls WHERE root_task_id=%s AND state='unknown' LIMIT 1", (root["id"],))
        unknown = bool(cursor.fetchone())
        changes = {"usage": row["usage"] | totals | {"unknown": unknown}}
        if actual is None and row["status"] not in config.TERMINAL:
            changes |= {"status": "waiting_usage", "context": row["context"] | {"message": "模型用量待核对，已暂停后续付费调用"}}
        store.update(cursor, row, **changes)
        if root["id"] != row["id"]:
            store.update(cursor, root, usage=root["usage"] | totals | {"unknown": unknown},
                         **({"status": "waiting_usage"} if unknown and root["status"] not in config.TERMINAL else {}))
    if actual is None:
        raise UsagePending("模型未返回可核对用量，已暂停")


def invoke(task_id, phase, model, request, amount, callback):
    store.check(task_id)
    accounts.active_worker(store.check(task_id)["user_id"])
    call, replay = reserve(task_id, phase, model, request, amount)
    if replay:
        return call["response"]
    sent = False
    def on_request(_):
        nonlocal sent
        dispatch(call)
        sent = True
    try:
        # No implicit retries, fallback providers or transactions across the network.
        with httpx.Client(timeout=httpx.Timeout(60, connect=5), event_hooks={"request": [on_request]}) as transport:
            with _clients_lock:
                _clients.setdefault(task_id, set()).add(transport)
            try:
                store.check(task_id)
                result, usage = callback(transport)
            finally:
                with _clients_lock:
                    _clients.get(task_id, set()).discard(transport)
                    if not _clients.get(task_id):
                        _clients.pop(task_id, None)
    except Exception as exc:
        settle(call, None, {}, dispatched=sent, error=type(exc).__name__[:64])
        raise RuntimeError("模型请求未完成，请检查服务配置") from None
    settle(call, result, usage, dispatched=sent)
    store.check(task_id)  # Late responses settle but cannot publish after cancellation.
    return result


def reconcile(call_id, revision, actual, reference, admin_id):
    """Settle only explicitly verified supplier usage; retain an audit record."""
    with database() as cursor:
        cursor.execute("SELECT * FROM agent_model_calls WHERE id=%s", (call_id,))
        call = store.decode(cursor.fetchone())
        if not call:
            raise HTTPException(404, "模型调用不存在")
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (call["user_id"],))
        root = store.get(cursor, call["root_task_id"], lock=True)
        task = store.get(cursor, call["task_id"], lock=True)
        cursor.execute("SELECT * FROM agent_model_calls WHERE id=%s FOR UPDATE", (call_id,))
        call = store.decode(cursor.fetchone())
        if task["revision"] != revision or call["state"] != "unknown":
            raise HTTPException(409, "任务版本或调用状态已变化")
        usage = call["usage"] | {"total_tokens": actual, "reconciliation": {"admin_id": str(admin_id), "provider_reference": reference, "at": store.now().isoformat() + "Z"}}
        cursor.execute("UPDATE agent_model_calls SET state='settled',actual_tokens=%s,`usage`=%s,settled_at=%s,updated_at=%s WHERE id=%s", (actual, store.encode(usage), store.now(), store.now(), call_id))
        cursor.execute("SELECT state,actual_tokens,reserved_tokens FROM agent_model_calls WHERE root_task_id=%s FOR UPDATE", (root["id"],))
        calls = cursor.fetchall()
        unknown = any(c["state"] == "unknown" for c in calls)
        totals = {"actual": sum(c["actual_tokens"] or 0 for c in calls), "reserved": sum(c["reserved_tokens"] for c in calls if c["state"] != "settled"), "unknown": unknown}
        for row in ([root, task] if root["id"] != task["id"] else [task]):
            updated = store.update(cursor, row, usage=row["usage"] | totals,
                                   **({"status": "interrupted", "context": row["context"] | {"message": "用量已核对，可恢复任务"}} if not unknown and row["status"] == "waiting_usage" else {}))
        store.event(cursor, updated, "usage_reconciled", {"call_id": call_id, "actual_tokens": actual, "admin_id": str(admin_id)})
    return {"call_id": call_id, "state": "settled", "actual_tokens": actual}


def chat(task_id, messages, tools=None, *, phase="text", max_output=4000, json_output=False, model=None):
    from backend.infrastructure.ai import load_config
    model = model or load_config("vision" if phase == "vision" else "text")
    request = {"messages": messages, "tools": tools, "max_output": max_output, "json": json_output}
    def send(transport):
        llm = ChatOpenAI(model=model["model"], base_url=model["base_url"], api_key=model["api_key"],
                         max_retries=0, timeout=60, max_completion_tokens=max_output, http_client=transport,
                         use_responses_api=model["model"].startswith(("gpt-6", "gpt-5.6")))
        bound = llm.bind_tools(tools) if tools else llm
        if json_output:
            bound = bound.bind(response_format={"type": "json_object"})
        # Conversion normalizes OpenAI tool-call messages without exposing private reasoning.
        response = bound.invoke(convert_to_messages(messages), config={"callbacks": []})
        usage = response.usage_metadata or {}
        usage = usage | {"provider_request_id": response.response_metadata.get("id")}
        result = {"role": "assistant", "content": response.text, "finish_reason": response.response_metadata.get("finish_reason", "stop")}
        if response.tool_calls:
            result["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": store.encode(c["args"])}} for c in response.tool_calls]
        return result, usage
    return invoke(task_id, phase, model, request, tokens(request) + max_output, send)


class GovernedEmbeddings(Embeddings):
    def __init__(self, task_id):
        self.task_id = task_id
        self.model = config.model_config("embedding")

    def embed_documents(self, texts):
        request = {"model": self.model["model"], "input": texts}
        def send(transport):
            response = transport.post(self.model["base_url"].rstrip("/") + "/embeddings", json=request,
                                      headers={"Authorization": "Bearer " + self.model["api_key"]})
            response.raise_for_status()
            body = response.json()
            data = sorted(body["data"], key=lambda x: x["index"])
            vectors = [d["embedding"] for d in data]
            if len(vectors) != len(texts) or any(len(v) != self.model["dimensions"] for v in vectors):
                # Actual usage must still settle even when the returned dimension is wrong.
                return {"vectors": [], "invalid_dimensions": True}, body.get("usage", {})
            return {"vectors": vectors}, body.get("usage", {}) | {"provider_request_id": body.get("id") or response.headers.get("x-request-id")}
        result = invoke(self.task_id, "embedding", self.model, request, tokens(texts) + 100, send)
        if result.get("invalid_dimensions"):
            raise HTTPException(409, "Embedding 输出维度与配置不一致")
        return result["vectors"]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def rerank(task_id, query, candidates):
    model = config.model_config("rerank")
    request = {"model": model["model"], "query": query, "documents": [c["content"] for c in candidates[:30]], "top_n": min(6, len(candidates))}
    def send(transport):
        response = transport.post(model["base_url"].rstrip("/") + "/rerank", json=request, headers={"Authorization": "Bearer " + model["api_key"]})
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        units = body.get("meta", {}).get("billed_units", {})
        if "total_tokens" not in usage and all(isinstance(units.get(k), int) and not isinstance(units[k], bool) and units[k] >= 0 for k in ("input_tokens", "output_tokens")):
            usage = {"total_tokens": units["input_tokens"] + units["output_tokens"], "billed_units": units}
        return {"results": body["results"]}, usage | {"provider_request_id": body.get("id") or response.headers.get("x-request-id")}
    return invoke(task_id, "rerank", model, request, tokens(request) + 100, send)["results"]
