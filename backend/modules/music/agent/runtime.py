from __future__ import annotations

import logging
import hashlib
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import redis
from openai import APIStatusError
from fastapi import HTTPException

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import auth, budget, features, mcp, model, service, skill, store, work
from backend.modules.music.agent.faults import ModelFault
from backend.modules.music.agent.models import POLICY_VERSION
from backend.modules.music.agent.scenes import SCENES, normalize_scene


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
    except (budget.BudgetExceeded, ModelFault) as exc:
        try:
            with library.database() as cursor:
                current = store.get(cursor,run_id,lock=True)
                if current["status"]=="running" and current["worker_id"]==worker and current["request"].get("parsed"):
                    store.update(cursor,current,context=current["context"] | {"message":str(exc),"stop_reason":str(exc),
                        **({"model_stop":str(exc)} if isinstance(exc, ModelFault) else {})})
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


def preferred_artist_tool(row, attempted):
    """Bounded artist recall and round-robin evaluation, with unchanged qualification."""
    parsed, ctx = row["request"]["parsed"], row["context"]
    preferred = parsed.get("preferred_artists", [])
    if not preferred:
        return None
    candidates = ctx.get("candidates", [])
    searches = ctx.get("artist_searches", [])
    reserve = budget.validation_only(row)
    local = parsed["source_mode"] != "discover" or not service.network_allowed(row)
    personal_ready = ctx.get("personal_searched") and not any(
        t["source"]["kind"] == "personal" and (not t.get("analysis") or not t.get("validation")) for t in candidates)
    coverage = ctx.get("coverage", {})
    personal_ready = personal_ready and coverage.get("personal_checked_until", 0) >= coverage.get("personal_total", 0)
    sources = ctx["limits"].get("sources", [])
    def available(name, arguments):
        return (name, arguments) if store.tool_signature(name, arguments) not in attempted else None
    # Give every requested artist a search before evaluating any one artist's batch.
    if not reserve and len(candidates) < ctx["limits"]["candidates"]:
        for item in service.artist_coverage(row):
            artist = item["artist"]
            if item["complete"]:
                continue
            history = [s for s in searches if features.artist_identity(s["artist"]) == features.artist_identity(artist)]
            if local and not any(s["source"] == "personal" for s in history):
                action = available("search_my_music", {"query": artist, "offset": 0, "limit": 20})
                if action:
                    return action
            online = parsed["source_mode"] != "personal_only" and service.network_allowed(row)
            if not online or (parsed["source_mode"] == "personal_first" and not personal_ready):
                continue
            if any(s["source"] != "personal" and s["success"] for s in history):
                continue
            if ctx.get("budget", {}).get("search_rounds", 0) >= ctx["limits"].get("search_rounds", 5):
                continue
            for provider in sources:
                if any(s["source"] == provider for s in history):
                    continue
                action = available("search_online_tracks", {"provider": provider, "query": artist, "page": 1})
                if action:
                    return action
    # One candidate per artist per round; validation precedes further analysis.
    progress = sorted(service.artist_coverage(row), key=lambda item: item["evaluated_count"])
    for name, field in (("validate_tracks", "validation"), ("get_track_features", "analysis")):
        if name == "get_track_features" and reserve:
            continue
        for item in progress:
            tracks = [t for t in candidates if features.matched_artists(t, [item["artist"]])][:5]
            for track in tracks:
                if track.get(field) or (name == "validate_tracks" and not track.get("analysis")):
                    continue
                action = available(name, {"keys": [track["key"]]})
                if action:
                    return action
    return None


def pending_tool(row, attempted):
    """Drain discovered work without spending another model/search round."""
    priority = set(row["context"].get("adjustment_candidates", []))
    if priority:
        size = service.batch_size(row)
        for name, field in (("validate_tracks", "validation"), ("get_track_features", "analysis")):
            keys = [t["key"] for t in row["context"].get("candidates", []) if t["key"] in priority and not t.get(field)
                    and (name != "validate_tracks" or t.get("analysis"))]
            arguments = {"keys":keys[:size]}
            if keys and store.tool_signature(name, arguments) not in attempted:
                if name != "get_track_features" or not budget.validation_only(row):
                    return name, arguments
    preferred = preferred_artist_tool(row, attempted)
    if preferred:
        return preferred
    ctx = row["context"]
    candidates = ctx.get("candidates", [])
    def available(name, arguments):
        return (name, arguments) if store.tool_signature(name, arguments) not in attempted else None
    if not ctx.get("personal_searched") and row["request"]["parsed"]["source_mode"] != "discover":
        call = available("search_my_music", {"offset": 0, "limit": 20})
        if call:
            return call
    size = service.batch_size(row)
    for name, field in (("validate_tracks", "validation"), ("get_track_features", "analysis")):
        if name == "get_track_features" and budget.validation_only(row):
            continue
        pending = [t for t in candidates if not t.get(field)]
        if name == "get_track_features":
            pending = features.prioritize_cached(pending)
        keys = [t["key"] for t in pending
                if name != "validate_tracks" or t.get("analysis")]
        for start in range(0, len(keys), size):
            call = available(name, {"keys": keys[start:start+size]})
            if call:
                return call
    if budget.validation_only(row):
        return None
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
    # A plain scene already has versioned recall keywords; no paid planner is needed.
    parsed = row["request"]["parsed"]
    if (normalize_scene(row["request"].get("need", "")) in SCENES and not parsed.get("preferred_artists")
            and ctx.get("budget", {}).get("search_rounds", 0) < ctx["limits"]["search_rounds"]):
        query = skill.load()["categories"]["scenes"][parsed["scene"]][0]
        for provider in ctx["limits"].get("sources", [])[:2]:
            call = available("search_online_tracks", {"provider":provider, "query":query, "page":1})
            if call:
                return call
    return None


def no_gain(row):
    """Count distinct evaluations since the last new qualified song, across resumes."""
    ctx = row["context"]
    evaluated = {t["key"] for t in ctx.get("candidates", []) if t.get("analysis") and t.get("validation")}
    qualified = {t["key"] for t in service.deduplicated(service.qualified(row))}
    state = dict(ctx.get("no_progress", {"evaluated":[], "qualified":[], "count":0, "adjusted":False}))
    fresh = evaluated - set(state["evaluated"])
    state["count"] = 0 if qualified - set(state["qualified"]) else state["count"] + len(fresh)
    state.update(evaluated=sorted(evaluated), qualified=sorted(qualified))
    action = None
    if state["count"] >= 10:
        if state["adjusted"]:
            raise budget.BudgetExceeded("调整检索后又有10首不同候选未增加合格结果，已停止无效探索", "no_progress")
        state.update(adjusted=True, count=0)
        action = "adjust"
    if state != ctx.get("no_progress"):
        with library.database() as cursor:
            current = store.get(cursor, row["run_id"], lock=True)
            store.update(cursor, current, context=current["context"] | {"no_progress":state})
    return action


def record_attempt(run_id, name, arguments, attempted):
    signature = store.tool_signature(name, arguments)
    if signature in attempted:
        raise ModelFault("模型重复请求相同工具和参数，已停止无效调用")
    attempted.add(signature)
    with library.database() as cursor:
        current = store.get(cursor, run_id, lock=True)
        store.update(cursor, current, context=current["context"] | {"attempted_tools":sorted(attempted)})


def prioritize_adjustment(row, name, response, client, attempted):
    """Evaluate the changed source before returning to the original rejected pool."""
    run_id = row["run_id"]
    previous = {t["key"] for t in row["context"].get("candidates", [])}
    if name == "search_online_playlists":
        playlists = response.get("data", {}).get("playlists", [])
        known = {(p["provider"], p["playlist_id"]) for p in row["context"].get("online_playlists", [])}
        playlist = next((p for p in playlists if (p["provider"], p["playlist_id"]) not in known), None)
        if not playlist:
            raise budget.BudgetExceeded("调整检索未发现新歌单，已结束无效探索", "no_progress")
        arguments = {"provider":playlist["provider"], "playlist_id":playlist["playlist_id"], "offset":0, "limit":20}
        record_attempt(run_id, "get_playlist_tracks", arguments, attempted)
        response = client.call("get_playlist_tracks", arguments)
    error = response.get("error") or {}
    if error.get("code") == "MODEL_STOP":
        raise ModelFault(error["message"])
    if error.get("code") == "BUDGET_EXHAUSTED":
        raise budget.BudgetExceeded(error["message"], error.get("budget"))
    keys = [t["key"] for t in response.get("data", {}).get("tracks", []) if t["key"] not in previous]
    if not keys:
        raise budget.BudgetExceeded("调整检索未发现不同候选，已结束无效探索", "no_progress")
    with library.database() as cursor:
        current = store.get(cursor, run_id, lock=True)
        store.update(cursor, current, context=current["context"] | {"adjustment_candidates":keys})


def execute_run(row, last):
    with work.task(row):
        return _execute_run(row, last)


def _execute_run(row, last):
    run_id = row["run_id"]
    if row["policy_version"] != POLICY_VERSION:
        raise ModelFault("旧版任务不能继续执行，请重新生成")
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
    attempted = set(row["context"].get("attempted_tools", []))
    while True:
        last = elapsed_charge(run_id,last)
        row = service.check_active(run_id)
        client.token = auth.issue(row)
        if service.satisfied(row)[0]:
            break
        adjust = no_gain(row) == "adjust"
        used, limits = row["context"].get("budget", {}), row["context"]["limits"]
        exhausted_search = used.get("search_rounds", 0) >= limits["search_rounds"]
        offline = not service.network_allowed(row)
        ready = pending_tool(row, attempted)
        process_candidates = ready is not None
        if adjust and (offline or exhausted_search or used.get("decisions", 0) >= limits["decisions"]):
            raise budget.BudgetExceeded("连续10首未增加合格结果，剩余额度无法调整检索，已结束探索", "no_progress")
        if not adjust and (process_candidates or budget.validation_only(row) or drain or offline or exhausted_search or used.get("decisions", 0) >= limits["decisions"]):
            action = pending_tool(row, attempted)
            if action is None:
                reason = "已检查可用候选，目标尚有缺口"
                if budget.validation_only(row):
                    reason = f"网络额度进入播放校验预留区（{used.get('network_requests',0)}/{limits.get('network_requests',160)}），可校验候选已处理"
                elif exhausted_search:
                    reason = f"已达到搜索{limits['search_rounds']}次上限，并已检查可用候选"
                with library.database() as cursor:
                    current = store.get(cursor,run_id,lock=True)
                    store.update(cursor,current,context=current["context"] | {"stop_reason":reason})
                break
            name, arguments = action
            record_attempt(run_id, name, arguments, attempted)
            service.progress(run_id, "tools", "正在"+{"search_my_music":"检索个人音乐库", "get_track_features":"分析歌曲特征",
                "validate_tracks":"校验完整播放", "get_playlist_tracks":"读取歌单歌曲"}.get(name, "检索候选"))
            data = client.call(name, arguments)
            if (data.get("error") or {}).get("code") == "MODEL_STOP":
                raise ModelFault(data["error"]["message"])
            if (data.get("error") or {}).get("budget") == "network_reserve":
                continue
            if (data.get("error") or {}).get("code") == "BUDGET_EXHAUSTED":
                raise budget.BudgetExceeded(data["error"]["message"], data["error"].get("budget"))
            called = True
            continue
        snapshot = {"candidate_status":summarize_candidates(row)[-12:],
            "budget":used,"limits":limits,
            "discovered_playlists":row["context"].get("online_playlists",[]),
            "coverage":row["context"].get("coverage",{}),
            "attempted_tools":sorted(attempted)[-12:],
            "instruction":("10 distinct candidates gave no new qualified song. Diagnose reasons and use ONE different search query/source. Do not relax constraints."
                if adjust else "Choose the next useful tool. Read discovered playlists before searching again. Stop when usable work exhausted.")}
        messages = messages[:2] + [{"role":"user", "content":store.encode(snapshot)}]
        while model.input_tokens(messages, tools) > 8000 and snapshot["candidate_status"]:
            snapshot["candidate_status"] = snapshot["candidate_status"][len(snapshot["candidate_status"])//2+1:]
            messages[-1]["content"] = store.encode(snapshot)
        try:
            signature = hashlib.sha256(store.encode([messages, tools]).encode()).hexdigest()
            for attempt in range(2):
                assistant = model.complete(run_id,messages,tools,max_output=4000,decision=True,
                                           signature=signature, correction=bool(attempt))
                if assistant.get("finish_reason") not in {"length", "incomplete"}:
                    break
                if attempt:
                    raise ModelFault("模型规划输出连续截断，已停止调用")
                messages += [{"role":"user", "content":"上次输出截断，请仅输出一个完整的工具调用。"}]
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
                raise ModelFault("模型请求未授权的工具，已停止调用")
            try:
                arguments = model.json_value(call["function"]["arguments"])
            except (ValueError, TypeError):
                raise ModelFault("模型工具参数格式错误，已停止调用") from None
            if adjust and name not in {"search_online_playlists", "search_online_tracks"}:
                raise ModelFault("连续无增益后的检索调整无效，已停止调用")
            record_attempt(run_id, name, arguments, attempted)
            data = client.call(name,arguments)
            error = data.get("error") or {}
            if error.get("code") in {"MODEL_STOP", "422", "403"}:
                raise ModelFault(error["message"])
            if error.get("code") == "BUDGET_EXHAUSTED":
                if error.get("budget") in {"search_rounds", "network_reserve"}:
                    drain = True
                    break
                raise budget.BudgetExceeded(error["message"], error.get("budget"))
            called = True
            messages.append({"role":"tool","tool_call_id":call["id"],"content":store.encode(data)})
            if adjust:
                prioritize_adjustment(row, name, data, client, attempted)
                break
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
        if row['context'].get('stop_reason'):
            used = row['context'].get('budget', {}).get('network_requests', 0)
            limit = row['context']['limits'].get('network_requests', 160)
            pending = sum(not t.get('validation') for t in row['context'].get('candidates', []))
            result['notice'] += f"；网络请求 {used}/{limit}，另有 {pending} 首待校验"
        success = (result["gap"]["seconds"]==0 if result["gap"]["primary"]=="duration" else result["gap"]["count"]==0) and service.artists_complete(row)
        if not service.artists_complete(row):
            result["notice"] = (result["notice"] + "；指定歌手尚未全部完成评估，请查看歌手检索范围")[:300]
        result["notice"] = result["notice"][:300]
        candidates = row["context"].get("candidates", [])
        counts = {"candidates":len(candidates), "analyzed":sum(bool(t.get("analysis")) for t in candidates),
                  "evaluated":sum(bool(t.get("analysis") and t.get("validation")) for t in candidates),
                  "qualified":len(service.deduplicated(service.qualified(row)))}
        status = "finished" if success else "partial" if result["tracks"] else "failed"
        message = {"finished":"歌单已生成", "partial":"已结束生成，请查看部分结果和停止原因",
                   "failed":"未生成合格歌曲，请查看停止原因后重新生成"}[status]
        performance = row["context"].get("performance", {}) | {"completion_seconds":round(
            row["context"].get("base_seconds", 0)+max(0, time.time()-row["context"].get("active_started_at", time.time())), 3)}
        store.update(cursor,row,status=status,result=result,result_version=row["result_version"]+1,
                     worker_id=None,lease_until=None,context=row["context"] | {"candidates":[],
                         "counts":counts, "stage":"done", "current_song":None,
                         "message":message, "performance":performance})


def submit(run_id):
    return accounts.submit(pool,run,run_id)


def cancel_user(uid,key=None):
    with library.database() as cursor:
        cursor.execute("SELECT * FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running','waiting_auth','waiting_input','interrupted')"+(" AND run_id=%s" if key else "")+" FOR UPDATE", (uid,key) if key else (uid,))
        for value in cursor.fetchall():
            row = store.decode(value)
            store.update(cursor,row,status="cancelled",worker_id=None,lease_until=None,context=row["context"] | {"message":"账号或资源操作已停止任务"})
            model.cancel(row["run_id"])
