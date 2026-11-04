"""LangGraph music orchestration with the existing business gates and API projection."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from backend.agents import actions, config, gateway, runtime as agent_runtime, store
from backend.agents.checkpointer import MySQLSaver
from backend.infrastructure.database import database


def attach(cursor, row, profile=False):
    if config.engine() != "langgraph":
        return
    # Daily candidate generation retains its existing special quota engine.
    if not profile and row["request"].get("kind") == "daily_discovery":
        return
    kind = "music_profile" if profile else "music_scene"
    row["context"].update(engine="langgraph", graph_version=config.GRAPH_VERSION)
    settings = row["context"].get("limits", {})
    limits = config.limits(kind)
    if profile:
        from backend.modules.accounts import service as accounts
        limits["tokens"] = min(12000, accounts.settings().get("music_profile_task_tokens", 12000))
    else:
        limits.update(tokens=min(150000, settings.get("tokens", 150000)), seconds=settings.get("seconds", 300))
    store.create(cursor, row["user_id"], kind, {"business_run_id": row["run_id"]}, kind + ":" + row["run_id"],
                 task_id=row["run_id"], business_ref=row["run_id"], skill_version=row["skill_version"], task_limits=limits)


def sync(cursor, row, profile=False):
    if row["context"].get("engine") != "langgraph":
        return
    task = store.get(cursor, row["run_id"], lock=True)
    status = {"pending": "queued", "finished": "succeeded", "no_change": "succeeded", "usage_pending": "waiting_usage", "stale": "failed"}.get(row["status"], row["status"])
    if row.get("deleted"):
        status = "cancelled"
    if task["status"] == "waiting_usage" and status not in {"cancelled", "waiting_usage"}:
        status = "waiting_usage"
    lease = datetime.fromtimestamp(row["lease_until"], timezone.utc).replace(tzinfo=None) if row.get("lease_until") else None
    context = task["context"] | {"message": row["context"].get("message", ""), "node": row["context"].get("stage", "music")}
    changes = {"status": status, "worker_id": row.get("worker_id"), "lease_until": lease, "context": context,
               "usage": task["usage"] | {"seconds": row["context"].get("budget", {}).get("seconds", 0)}}
    if status == "running" and not task["started_at"]:
        changes["started_at"] = store.now()
    if status in config.TERMINAL:
        changes.update(finished_at=store.now(), result=row.get("proposal") if profile else row.get("result"))
    store.update(cursor, task, **changes)


def complete(run_id, messages, tools=None, max_output=4000, phase="semantic", json_output=False,
             signature=None, correction=False, profile=False):
    from backend.modules.music.agent import budget, model
    from backend.modules.music.discovery import profile_agent, profile_budget
    from backend.infrastructure.ai import load_config
    from backend.modules.accounts import service as accounts
    check = profile_agent.check_active if profile else __import__("backend.modules.music.agent.service", fromlist=["check_active"]).check_active
    row = check(run_id)
    cfg = load_config()
    request = {"messages": messages, "tools": tools, "max_output": max_output, "json": json_output}
    fingerprint = store.digest({"phase": phase, "model": cfg["model"], "provider": cfg["base_url"], "request": request})
    with database() as cursor:
        cursor.execute("SELECT state,response FROM agent_model_calls WHERE task_id=%s AND request_hash=%s ORDER BY attempt DESC LIMIT 1", (run_id, fingerprint))
        cached = store.decode(cursor.fetchone())
    if cached and cached["state"] == "settled" and cached["response"] is not None:
        return cached["response"]
    estimated = model.input_tokens(messages, tools) + max_output
    deadline = time.time() + 60
    if profile:
        legacy = profile_budget.reserve(row["user_id"], run_id, estimated + 400, signature or store.digest(request), deadline)
    else:
        legacy = budget.reserve(run_id, estimated, phase, signature or store.digest(request), deadline, correction)
    try:
        response = gateway.chat(run_id, messages, tools, phase=phase, max_output=max_output, json_output=json_output, model=cfg)
    except Exception:
        with database() as cursor:
            cursor.execute("SELECT dispatched,state FROM agent_model_calls WHERE task_id=%s AND request_hash=%s ORDER BY attempt DESC LIMIT 1", (run_id, fingerprint))
            call = cursor.fetchone()
        unknown = call and call["state"] != "settled"
        if profile:
            profile_budget.settle(row["user_id"], run_id, legacy["id"], None if unknown else 0, error="公共网关停止调用")
        else:
            budget.settle(run_id, legacy["id"], actual=None if unknown else 0, error="公共网关停止调用", dispatched=bool(call and call["dispatched"]))
        raise
    with database() as cursor:
        cursor.execute("SELECT * FROM agent_model_calls WHERE task_id=%s AND request_hash=%s ORDER BY attempt DESC LIMIT 1", (run_id, fingerprint))
        call = store.decode(cursor.fetchone())
    if profile:
        profile_budget.settle(row["user_id"], run_id, legacy["id"], call["actual_tokens"], call["usage"])
    else:
        budget.settle(run_id, legacy["id"], actual=call["actual_tokens"], dispatched=True, usage_details=call["usage"])
    check(run_id)
    return response


class State(TypedDict, total=False):
    task_id: str
    tool: str
    arguments: dict
    step: int
    done: bool
    drain: bool
    result: dict


def scene_graph():
    from backend.modules.music.agent import auth, budget, mcp, model, runtime, service, skill, store as runs
    def current(state):
        row = service.check_active(state["task_id"])
        if row["context"].get("graph_version") != config.GRAPH_VERSION or skill.load()["version"] != row["skill_version"]:
            raise HTTPException(409, "任务图或 Skill 版本已变化")
        return row
    def parse(state):
        row = current(state)
        if "parsed" not in row["request"]:
            parsed = model.parse(row["run_id"], row["request"], skill.load())
            with database() as cursor:
                row = runs.get(cursor, row["run_id"], lock=True)
                row = runs.update(cursor, row, request=row["request"] | {"parsed": parsed})
        if row["request"]["parsed"].get("clarification"):
            runtime.stop_state(row["run_id"], "waiting_input", row["request"]["parsed"]["clarification"], row["worker_id"])
            interrupt({"kind": "clarification"})
            return parse(state)
        return {"done": False, "step": state.get("step", 0)}
    def select(state):
        row = current(state)
        runtime.elapsed_charge(row["run_id"], time.monotonic())
        if service.satisfied(row)[0]:
            return {"done": True}
        attempted = set(row["context"].get("attempted_tools", []))
        ready = runtime.pending_tool(row, attempted)
        used, limits = row["context"].get("budget", {}), row["context"]["limits"]
        adjust = runtime.no_gain(row) == "adjust"
        exhausted = used.get("search_rounds", 0) >= limits["search_rounds"]
        offline = not service.network_allowed(row)
        if adjust and (offline or exhausted or used.get("decisions", 0) >= limits["decisions"]):
            raise budget.BudgetExceeded("连续候选无增益，剩余额度无法调整检索", "no_progress")
        if not adjust and (ready or budget.validation_only(row) or state.get("drain") or offline or exhausted or used.get("decisions", 0) >= limits["decisions"]):
            if not ready:
                return {"done": True}
            return {"tool": ready[0], "arguments": ready[1], "done": False}
        client = mcp.LocalClient(auth.issue(row))
        messages = [{"role": "system", "content": skill.load()["text"]}, {"role": "user", "content": runs.encode({
            "request": row["request"]["parsed"], "need": row["request"]["need"],
            "candidate_status": runtime.summarize_candidates(row)[-12:], "budget": used, "limits": limits,
            "coverage": row["context"].get("coverage", {}), "discovered_playlists": row["context"].get("online_playlists", [])[-10:],
            "instruction": "只选择一个工具。连续候选无增益时更换检索词或来源，不放宽业务约束。"})}]
        response = model.complete(row["run_id"], messages, client.tools(), max_output=2000, decision=True)
        calls = response.get("tool_calls", [])
        if not calls:
            return {"drain": True, "tool": "", "arguments": {}}
        call = calls[0]["function"]
        if call["name"] not in auth.READ_TOOLS or (adjust and call["name"] not in {"search_online_tracks", "search_online_playlists"}):
            raise model.ModelFault("模型请求了未授权或无效的工具")
        return {"tool": call["name"], "arguments": model.json_value(call["arguments"]), "done": False}
    def execute(state):
        row = current(state)
        if not state.get("tool"):
            return {"step": state.get("step", 0) + 1}
        name, arguments = state["tool"], state["arguments"]
        prepared = actions.prepare(row["run_id"], name, arguments, step=["music", state.get("step", 0), name, arguments])
        client = mcp.LocalClient(auth.issue(row))
        data = actions.execute(prepared, lambda _: client.call(name, arguments))
        error = data.get("error") or {}
        if error.get("code") in {"MODEL_STOP", "403", "422"}:
            raise model.ModelFault(error["message"])
        if error.get("code") == "BUDGET_EXHAUSTED" and error.get("budget") not in {"search_rounds", "network_reserve"}:
            raise budget.BudgetExceeded(error["message"], error.get("budget"))
        runtime.record_attempt(row["run_id"], name, arguments, set(row["context"].get("attempted_tools", [])))
        return {"step": state.get("step", 0) + 1, "drain": bool(error.get("code") == "BUDGET_EXHAUSTED") or state.get("drain", False)}
    def finish(state):
        runtime.finish(state["task_id"], None)
        return {}
    graph = StateGraph(State)
    graph.add_node("parse", parse)
    graph.add_node("select", select)
    graph.add_node("execute", execute)
    graph.add_node("finish", finish)
    graph.add_edge(START, "parse")
    graph.add_conditional_edges("parse", lambda s: END if s.get("done") else "select")
    graph.add_conditional_edges("select", lambda s: "finish" if s.get("done") else "execute")
    graph.add_edge("execute", "select")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=MySQLSaver())


def execute_scene(row, last):
    from backend.modules.music.agent import work
    with work.task(row):
        graph = scene_graph()
        cfg = {"configurable": {"thread_id": row["run_id"]}}
        snapshot = graph.get_state(cfg)
        if snapshot.values and not snapshot.next:
            # A continued partial task starts planning again; completed actions
            # retain their ledger and the root budget is never reset.
            graph.update_state(cfg, {"done": False, "drain": False}, as_node="execute")
        return agent_runtime.invoke_graph(graph, row["run_id"])


def cancel_business(task):
    from backend.modules.music.agent import model, store as runs
    from backend.modules.music.discovery import profile_store
    with database() as cursor:
        if task["kind"] == "music_scene":
            row = runs.get(cursor, task["business_ref"], task["user_id"], lock=True)
            runs.update(cursor, row, status="cancelled", worker_id=None, lease_until=None)
        else:
            row = profile_store.get_run(cursor, task["user_id"], task["business_ref"], True)
            profile_store.update_run(cursor, row, status="cancelled", worker_id=None, lease_until=None)
    model.cancel(task["business_ref"])


def execute_profile(row):
    from backend.modules.music.discovery import profile_agent, profile_budget, profile_store, store as profiles
    from backend.modules.music.agent import model
    from backend.modules.music.agent.faults import ModelFault
    class ProfileState(TypedDict, total=False):
        task_id: str
        messages: list
        calls: list
        round: int
        done: bool
    def prepare(state):
        current = profile_agent.check_active(state["task_id"])
        data = profile_agent.skill()
        if current["skill_version"] != data["version"]:
            raise ModelFault("画像 Skill 版本已变化")
        snapshot = current["input_snapshot"]
        return {"round": 0, "done": False, "messages": [{"role": "system", "content": data["text"]}, {"role": "user", "content": store.encode({
            "coverage": snapshot["coverage"], "sample": snapshot["evidence"][:6], "current_profile": snapshot["base_state"],
            "instruction": "检查证据并维护偏好；依据不足时结束。"})}]}
    def think(state):
        if state["round"] >= 4:
            raise ModelFault("画像评估请求额度已用完")
        response = profile_agent.complete(state["task_id"], state["messages"], profile_agent.tools())
        calls = response.get("tool_calls", [])
        if not calls or len(calls) > 6:
            raise ModelFault("画像模型未返回合法工具调用")
        return {"calls": calls, "messages": state["messages"] + [{k: v for k, v in response.items() if k != "finish_reason"}], "round": state["round"] + 1}
    def execute(state):
        current = profile_agent.check_active(state["task_id"])
        messages = list(state["messages"])
        for i, call in enumerate(state["calls"]):
            name = call["function"]["name"]
            arguments = model.json_value(call["function"]["arguments"])
            action = actions.prepare(state["task_id"], "profile_" + name, arguments, step=[state["round"], i, name, arguments])
            def tool(_):
                result, finished = profile_agent.execute_tool(name, arguments, current["input_snapshot"])
                return {"data": result, "finished": finished}
            result = actions.execute(action, tool)
            with database() as cursor:
                profiles.lock_user(cursor, current["user_id"])
                live = profile_store.get_run(cursor, current["user_id"], current["run_id"], True)
                profile_budget.check_row(live)
                changes = {"steps": (live["steps"] + [{"tool": name, "at": time.time(), "valid": True}])[-40:]}
                if result["finished"]:
                    changes.update(proposal=result["data"], context=live["context"] | {"proposal_ready": True})
                profile_store.update_run(cursor, live, **changes)
            if result["finished"]:
                return {"done": True}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": store.encode(result["data"])})
        return {"messages": messages}
    builder = StateGraph(ProfileState)
    builder.add_node("prepare", prepare)
    builder.add_node("think", think)
    builder.add_node("tools", execute)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "think")
    builder.add_edge("think", "tools")
    builder.add_conditional_edges("tools", lambda s: END if s.get("done") else "think")
    return agent_runtime.invoke_graph(builder.compile(checkpointer=MySQLSaver()), row["run_id"])
