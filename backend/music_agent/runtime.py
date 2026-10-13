from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import redis
from openai import APIStatusError
from fastapi import HTTPException

from backend import accounts, library
from backend.music_agent import auth, budget, features, mcp, model, service, skill, store


logger = logging.getLogger("videodream.music-agent")
pool = ThreadPoolExecutor(max_workers=8,thread_name_prefix="scene-playlist")
worker_prefix = secrets.token_hex(12)


def claim(run_id):
    config = auth.require_enabled()
    with library.database() as cursor:
        cursor.execute("SELECT config FROM system_settings WHERE id=1 FOR UPDATE")
        store.recover(cursor)
        row = store.get(cursor,run_id,accounts.current().id,lock=True)
        if row["status"]!="pending":
            return None
        cursor.execute("SELECT COUNT(*) AS n FROM music_recommendation_runs WHERE deleted=FALSE AND status='running'")
        if cursor.fetchone()["n"]>=config["music_agent_concurrency"]:
            return None
        return store.update(cursor,row,status="running",worker_id=worker_prefix+":"+secrets.token_hex(8),lease_until=time.time()+60,
                            context=row["context"] | {"active_started_at":time.time(),"base_seconds":row["context"].get("budget",{}).get("seconds",0)})


def renew(run_id,worker,stop):
    while not stop.wait(15):
        try:
            with library.database() as cursor:
                row = store.get(cursor,run_id,lock=True)
                if row["status"]!="running" or row["worker_id"]!=worker or row["lease_until"]<=time.time():
                    return
                used = dict(row["context"].get("budget",{}))
                used["seconds"] = min(row["context"]["limits"]["seconds"],row["context"].get("base_seconds",0)+time.time()-row["context"].get("active_started_at",time.time()))
                store.update(cursor,row,lease_until=time.time()+60,context=row["context"] | {"budget":used})
        except Exception:
            logger.warning("Music agent lease renewal failed",exc_info=True)
            return


def stop_state(run_id,status,message,worker=None):
    with library.database() as cursor:
        row = store.get(cursor,run_id,lock=True,include_deleted=True)
        if row["deleted"] or row["status"] not in {"pending","running"} or (worker and row["worker_id"]!=worker):
            return
        store.update(cursor,row,status=status,worker_id=None,lease_until=None,
                     context=row["context"] | {"message":message,"waiting_reason":message})


def elapsed_charge(run_id,last):
    now = time.monotonic()
    with library.database() as cursor:
        row = store.get(cursor,run_id,lock=True)
        seconds = row["context"].get("base_seconds",0)+time.time()-row["context"].get("active_started_at",time.time())
        if seconds>row["context"]["limits"]["seconds"]:
            raise budget.BudgetExceeded("已达到执行时长预算上限")
        used = row["context"].get("budget",{}) | {"seconds":seconds}
        store.update(cursor,row,context=row["context"] | {"budget":used})
    return now


def summarize_candidates(row):
    return [{"key":t["key"],"title":t["title"],"artist":t["artist"],"source":t["source"],
             "analyzed":bool(t.get("analysis")),"validated":bool(t.get("validation")),
             "match":features.match(t,row["request"]["parsed"])}
            for t in row["context"].get("candidates",[])]


def run(run_id):
    row = None
    worker = None
    stop = threading.Event()
    heartbeat = None
    last = time.monotonic()
    try:
        # Queue while the global workers are occupied without consuming execution time.
        while row is None:
            current = service.current_run(run_id)
            if current["status"]!="pending":
                return
            auth.check_session(current["user_id"],current["context"]["session_id"])
            with library.database() as cursor:
                current = store.get(cursor,run_id,lock=True)
                if current["status"]=="pending":
                    store.update(cursor,current,lease_until=time.time()+60)
            row = claim(run_id)
            if row is None:
                stop.wait(2)
        last = time.monotonic()
        heartbeat = threading.Thread(target=renew,args=(run_id,row["worker_id"],stop),daemon=True)
        heartbeat.start()
        worker = row["worker_id"]
        with store.worker_scope(run_id, worker):
            execute_run(row, last)
    except budget.BudgetExceeded as exc:
        try:
            with library.database() as cursor:
                current = store.get(cursor,run_id,lock=True)
                if current["status"]=="running" and current["worker_id"]==worker and current["request"].get("parsed"):
                    store.update(cursor,current,context=current["context"] | {"message":str(exc),"stop_reason":str(exc)})
                    finish_after = True
                else:
                    finish_after = False
            if finish_after:
                finish(run_id,None,worker)
            else:
                stop_state(run_id,"failed",str(exc),worker)
        except Exception:
            logger.exception("Cannot persist exhausted task")
    except redis.RedisError:
        stop_state(run_id,"waiting_auth","认证服务不可用，恢复后继续",worker)
    except HTTPException as exc:
        stop_state(run_id,"waiting_auth" if exc.status_code in {401,503} else "failed",str(exc.detail),worker)
    except APIStatusError as exc:
        messages = {401:"AI 服务密钥无效，请更新配置后重试",402:"AI 服务余额不足，请充值或更换配置后重试",
                    429:"AI 服务请求受限，请稍后重试"}
        stop_state(run_id,"failed",messages.get(exc.status_code,"AI 服务调用失败，请检查配置后重试"),worker)
    except Exception as exc:
        logger.exception("Music agent failed")
        # Do not persist endpoint URLs, API errors containing headers, or model reasoning.
        message = str(exc) if isinstance(exc,RuntimeError) and ("模型" in str(exc) or "Skill" in str(exc)) else "执行失败，请检查 AI 配置、平台来源或服务日志后重试"
        try:
            current = service.current_run(run_id)
            auth.check_session(current["user_id"],current["context"]["session_id"])
            stop_state(run_id,"failed",message[:300],worker)
        except (HTTPException,redis.RedisError):
            stop_state(run_id,"waiting_auth","请重新认证后继续任务",worker)
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=2)


def pending_tool(row, attempted):
    """Drain discovered work without spending another model/search round."""
    ctx = row["context"]
    candidates = ctx.get("candidates", [])
    def available(name, arguments):
        return (name, arguments) if store.encode([name, arguments]) not in attempted else None
    if not ctx.get("personal_searched") and row["request"]["parsed"]["source_mode"] != "discover":
        call = available("search_my_music", {"offset": 0, "limit": 20})
        if call:
            return call
    for name, field in (("get_track_features", "analysis"), ("validate_tracks", "validation")):
        keys = [t["key"] for t in candidates if not t.get(field)]
        for start in range(0, len(keys), 20):
            call = available(name, {"keys": keys[start:start+20]})
            if call:
                return call
    if len(candidates) >= ctx["limits"]["candidates"]:
        return None
    coverage = ctx.get("coverage", {})
    offset = coverage.get("personal_checked_until", 0)
    if offset < coverage.get("personal_total", 0):
        call = available("search_my_music", {"offset": offset, "limit": 20})
        if call:
            return call
    if not service.network_allowed(row) or row["request"]["parsed"]["source_mode"] == "personal_only":
        return None
    for playlist in ctx.get("online_playlists", []):
        provider, pid = playlist["provider"], playlist["playlist_id"]
        checked = coverage.get("online_playlists", {}).get(provider+":"+pid)
        if checked and not checked.get("has_more", False):
            continue
        call = available("get_playlist_tracks", {"provider": provider, "playlist_id": pid,
                                                 "offset": (checked or {}).get("checked_until", 0), "limit": 20})
        if call:
            return call
    return None


def execute_run(row, last):
    run_id = row["run_id"]
    loaded = skill.load()
    if loaded["version"]!=row["skill_version"]:
        raise RuntimeError("任务 Skill 版本已变化，请创建重试任务")
    if "parsed" not in row["request"]:
        parsed = model.parse(run_id,row["request"],loaded)
        with library.database() as cursor:
            row = store.get(cursor,run_id,lock=True)
            row = store.update(cursor,row,request=row["request"] | {"parsed":parsed})
    parsed = row["request"]["parsed"]
    if parsed["clarification"]:
        stop_state(run_id,"waiting_input",parsed["clarification"],row["worker_id"])
        return
    client = mcp.LocalClient(auth.issue(row))
    tools = client.tools()
    messages = [{"role":"system","content":loaded["text"]},
                {"role":"user","content":store.encode({"request":parsed,"need":row["request"]["need"]})}]
    called = False
    drain = False
    attempted = set()
    while True:
        last = elapsed_charge(run_id,last)
        row = service.check_active(run_id)
        client.token = auth.issue(row)
        if service.satisfied(row)[0]:
            break
        used, limits = row["context"].get("budget", {}), row["context"]["limits"]
        exhausted_search = used.get("search_rounds", 0) >= limits["search_rounds"]
        offline = not service.network_allowed(row)
        if drain or offline or exhausted_search or used.get("decisions", 0) >= limits["decisions"]:
            action = pending_tool(row, attempted)
            if action is None:
                reason = "已检查可用候选，目标尚有缺口"
                if exhausted_search:
                    reason = f"已达到搜索{limits['search_rounds']}次上限，并已检查可用候选"
                with library.database() as cursor:
                    current = store.get(cursor,run_id,lock=True)
                    store.update(cursor,current,context=current["context"] | {"stop_reason":reason})
                break
            name, arguments = action
            attempted.add(store.encode([name, arguments]))
            data = client.call(name, arguments)
            if (data.get("error") or {}).get("code") == "BUDGET_EXHAUSTED":
                raise budget.BudgetExceeded(data["error"]["message"], data["error"].get("budget"))
            called = True
            continue
        messages.append({"role":"user","content":store.encode({"candidate_status":summarize_candidates(row),
            "budget":used,"limits":limits,
            "discovered_playlists":row["context"].get("online_playlists",[]),
            "coverage":row["context"].get("coverage",{}),
            "instruction":"Choose the next useful tool. Read discovered playlists before searching again. Stop only when goal met or usable work exhausted."})})
        try:
            assistant = model.complete(run_id,messages,tools,max_output=1600,decision=True)
        except budget.BudgetExceeded as exc:
            if exc.resource == "decisions":
                drain = True
                continue
            raise
        messages.append(assistant)
        if not assistant.get("tool_calls"):
            drain = True
            continue
        for call in assistant["tool_calls"]:
            row = service.check_active(run_id)
            if service.satisfied(row)[0]:
                break
            name = call["function"]["name"]
            if name not in auth.READ_TOOLS:
                raise RuntimeError("模型请求未授权的工具")
            arguments = model.json_value(call["function"]["arguments"])
            attempted.add(store.encode([name, arguments]))
            data = client.call(name,arguments)
            error = data.get("error") or {}
            if error.get("code") == "BUDGET_EXHAUSTED":
                if error.get("budget") == "search_rounds":
                    drain = True
                    break
                raise budget.BudgetExceeded(error["message"], error.get("budget"))
            called = True
            messages.append({"role":"tool","tool_call_id":call["id"],"content":store.encode(data)})
        if len(store.encode(messages))>35000:
            messages = messages[:2]
    finish(run_id,last,row["worker_id"])


def finish(run_id,last,worker=None):
    with library.database() as cursor:
        row = store.get(cursor,run_id,lock=True)
        if row["status"]!="running" or (worker and row["worker_id"]!=worker) or row["lease_until"]<=time.time():
            return
        if last is not None:
            seconds = row["context"].get("base_seconds",0)+time.time()-row["context"].get("active_started_at",time.time())
            if seconds>row["context"]["limits"]["seconds"]:
                raise budget.BudgetExceeded("已达到执行时长预算上限")
        result = service.result(row)
        if row["context"].get("stop_reason"):
            result["notice"] = row["context"]["stop_reason"]+"；"+result["notice"]
        success = result["gap"]["seconds"]==0 if result["gap"]["primary"]=="duration" else result["gap"]["count"]==0
        store.update(cursor,row,status="finished" if success else "partial",result=result,result_version=row["result_version"]+1,
                     worker_id=None,lease_until=None,context=row["context"] | {"candidates":[],
                         "message":"歌单已生成" if success else "已生成部分结果，目标尚有缺口"})


def submit(run_id):
    return accounts.submit(pool,run,run_id)


def cancel_user(uid,key=None):
    with library.database() as cursor:
        cursor.execute("SELECT * FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running','waiting_auth','waiting_input','interrupted')"+(" AND run_id=%s" if key else "")+" FOR UPDATE", (uid,key) if key else (uid,))
        for value in cursor.fetchall():
            row = store.decode(value)
            store.update(cursor,row,status="cancelled",worker_id=None,lease_until=None,context=row["context"] | {"message":"账号或资源操作已停止任务"})
