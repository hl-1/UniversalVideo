from __future__ import annotations

import time
from functools import partial

from fastapi import HTTPException

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import sources as music
from backend.infrastructure import network
from backend.modules.music.agent import auth, budget, features, platforms, store, validation
from backend.modules.music.agent.models import RunResult


def current_run(run_id):
    with library.database() as cursor:
        return store.get(cursor, run_id, accounts.current().id)


def check_active(run_id):
    row = current_run(run_id)
    try:
        auth.check_session(row["user_id"], row["context"]["session_id"])
        auth.require_enabled()
    except HTTPException as exc:
        exc.agent_policy_error = True
        raise
    if row["status"] != "running" or not row["lease_until"] or row["lease_until"] <= time.time():
        error = HTTPException(409,"任务已停止")
        error.agent_policy_error = True
        raise error
    total = row["context"].get("base_seconds",0) + time.time()-row["context"].get("active_started_at",time.time())
    if total > row["context"]["limits"]["seconds"]:
        raise budget.BudgetExceeded("已达到执行时长预算上限")
    return row


def network_allowed(row):
    return bool(row["request"]["parsed"]["network_allowed"] and auth.require_enabled()["music_agent_network_enabled"])


def online_guard(row, provider):
    parsed = row["request"]["parsed"]
    config = auth.require_enabled()
    if not network_allowed(row) or parsed["source_mode"] == "personal_only":
        raise HTTPException(403, "当前任务禁止在线候选检索")
    if provider not in config["music_agent_sources"] or provider not in row["context"]["limits"]["sources"]:
        raise HTTPException(403, "此音乐来源未获授权")
    if parsed["source_mode"] == "personal_first":
        if not row["context"].get("personal_searched"):
            raise HTTPException(409, "请先检索个人音乐库")
        if satisfied(row)[0]:
            raise HTTPException(409, "个人库已满足目标，不需要在线补充")
        personal = [t for t in row["context"].get("candidates",[]) if t["source"]["kind"]=="personal"]
        if any(not t.get("analysis") or not t.get("validation") for t in personal):
            raise HTTPException(409, "请先完成个人库候选的分析和播放校验")
        coverage = row["context"].get("coverage",{})
        if (coverage.get("personal_checked_until",0)<coverage.get("personal_total",0)
                and len(row["context"].get("candidates",[]))<row["context"]["limits"]["candidates"]):
            raise HTTPException(409, "请继续分页检索个人库后再联网补充")


def add_candidates(run_id, rows, source):
    with library.database() as cursor:
        row = store.get(cursor, run_id, accounts.current().id, lock=True)
        if row["status"] != "running":
            raise HTTPException(409, "任务已停止")
        candidates = list(row["context"].get("candidates", []))
        existing = {t["key"]: t for t in candidates}
        returned = []
        for track in rows:
            payload = library.TrackInput.model_validate(track)
            key = library.track_id(payload)
            key = library.resolve_track_id(cursor, key)
            if key in existing:
                returned.append(existing[key])
                continue
            if len(candidates) >= row["context"]["limits"]["candidates"]:
                break
            value = payload.model_dump() | {"key": key, "user_id": row["user_id"], "source": source,
                **{k:track[k] for k in ("filename", "local_url", "id", "playlist_hints") if track.get(k)}}
            candidates.append(value)
            existing[key] = value
            returned.append(value)
        store.update(cursor, row, context=row["context"] | {"candidates": candidates})
        return [{k: v for k, v in track.items() if k != "user_id"} for track in returned]


def selected_candidates(row, keys):
    if not 1 <= len(keys) <= 20 or len(set(keys)) != len(keys):
        raise ValueError("每批需为 1 至 20 个不同的候选标识")
    items = {t["key"]: t for t in row["context"].get("candidates", [])}
    if any(key not in items for key in keys):
        raise HTTPException(422, "只能分析或校验已经检索的候选歌曲")
    return [items[key] for key in keys]


def replace_candidate(run_id, key, **changes):
    with library.database() as cursor:
        row = store.get(cursor, run_id, accounts.current().id, lock=True)
        if row["status"] != "running":
            raise HTTPException(409, "任务已停止")
        candidates = [t | changes if t["key"] == key else t for t in row["context"]["candidates"]]
        store.update(cursor, row, context=row["context"] | {"candidates": candidates})


def search_personal(run_id, query="", offset=0, limit=20):
    if len(query)>100 or not 0<=offset<=1000 or not 1<=limit<=20:
        raise ValueError("个人库分页参数无效")
    uid = accounts.current().id
    where = """WHERE t.user_id=%s AND (f.track_id IS NOT NULL OR d.id IS NOT NULL OR EXISTS
        (SELECT 1 FROM music_playlist_tracks pt JOIN music_playlists p ON p.id=pt.playlist_id
         WHERE pt.track_id=t.id AND p.user_id=%s))"""
    args = (uid, uid)
    if query:
        where += """ AND (t.title LIKE %s OR t.artist LIKE %s OR EXISTS
            (SELECT 1 FROM music_playlist_tracks pt JOIN music_playlists p ON p.id=pt.playlist_id
             WHERE pt.track_id=t.id AND p.user_id=%s AND (p.name LIKE %s OR p.description LIKE %s)))"""
        term = "%"+query+"%"
        args += (term, term, uid, term, term)
    with library.database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM ("+library.TRACK_SELECT+where+") AS selected", args)
        total = cursor.fetchone()["n"]
        cursor.execute(library.TRACK_SELECT+where+" ORDER BY CASE WHEN EXISTS (SELECT 1 FROM music_playlist_tracks pt WHERE pt.track_id=t.id) THEN 0 WHEN f.track_id IS NOT NULL THEN 1 ELSE 2 END,t.id LIMIT %s OFFSET %s", args+(limit, offset))
        rows = [library.serialize_track(t) for t in cursor.fetchall()]
        for t in rows:
            cursor.execute("SELECT p.name,p.description FROM music_playlists p JOIN music_playlist_tracks pt ON pt.playlist_id=p.id WHERE pt.track_id=%s AND p.user_id=%s LIMIT 5", (t["id"], uid))
            t["playlist_hints"] = cursor.fetchall()
    candidates = add_candidates(run_id, rows, {"kind": "personal", "label": "个人歌单、收藏和下载"})
    with library.database() as cursor:
        row = store.get(cursor, run_id, uid, lock=True)
        # Targeted artist pages must not masquerade as a scan of the whole library.
        targeted_artist = any(features.artist_identity(query) == features.artist_identity(a)
                              for a in row["request"]["parsed"].get("preferred_artists", []))
        if not targeted_artist:
            store.update(cursor, row, context=row["context"] | {"personal_searched": True,
                "coverage": (row["context"].get("coverage") or {}) | {"personal_total": total, "personal_checked_until": offset+len(rows)}})
    return {"tracks": candidates, "offset": offset, "next_offset": offset+len(rows), "total": total,
            "has_more": offset+len(rows)<total and len(candidates)>0, "coverage": "已分页检索当前账号个人库"}


def qualified(row):
    parsed = row["request"].get("parsed")
    if not parsed:
        return []
    return [t | {"match": features.match(t, parsed)} for t in row["context"].get("candidates", [])
            if features.match(t, parsed)["qualified"]]


def deduplicated(tracks):
    result = []
    for track in sorted(tracks, key=lambda t:t["match"]["score"], reverse=True):
        same = any(validation.normalize(t["title"]) == validation.normalize(track["title"])
                   and validation.normalize(t["artist"]) == validation.normalize(track["artist"])
                   and t.get("duration") and track.get("duration")
                   and abs(t["duration"]-track["duration"])<=3 for t in result)
        if not same:
            result.append(track)
    return result


def artist_coverage(row):
    parsed, ctx = row["request"].get("parsed", {}), row["context"]
    coverage = []
    for artist in parsed.get("preferred_artists", []):
        tracks = [t for t in ctx.get("candidates", []) if features.matched_artists(t, [artist])][:5]
        evaluated = [t for t in tracks if t.get("analysis") and t.get("validation")]
        qualified_count = sum(features.match(t, parsed)["qualified"] for t in evaluated)
        searches = [s for s in ctx.get("artist_searches", [])
                    if features.artist_identity(s["artist"]) == features.artist_identity(artist)]
        online = any(s["source"] != "personal" and s["success"] for s in searches)
        personal = any(s["source"] == "personal" and s["success"] for s in searches)
        can_network = parsed.get("network_allowed", True) and ctx.get("limits", {}).get("network_enabled", True)
        local_only = parsed.get("source_mode") == "personal_only" or not can_network
        searched = online or (personal and (local_only or qualified_count > 0))
        complete = searched and len(evaluated) == len(tracks)
        status = ("qualified" if qualified_count else "no_qualified" if tracks else "no_candidates") if complete else "incomplete"
        coverage.append({"artist": artist, "search_attempted": bool(searches), "evaluated_count": len(evaluated),
                         "candidate_count": len(tracks), "qualified_count": qualified_count, "status": status,
                         "complete": complete,
                         "reasons": list(dict.fromkeys(reason for t in evaluated
                             for reason in features.match(t, parsed)["reasons"]))[:6]})
    return coverage


def artists_complete(row):
    return all(item["complete"] for item in artist_coverage(row))


def batch_size(row):
    parsed = row['request']['parsed']
    if parsed.get('goal') != 'count':
        return 5
    gap = max(1, parsed.get('target_count', 20) - len(deduplicated(qualified(row))))
    return 1 if gap == 1 else 2 if gap < 5 else 5


def satisfied(row):
    tracks = deduplicated(qualified(row))
    parsed = row["request"].get("parsed", {})
    seconds = sum(t.get("validation", {}).get("duration") or t.get("duration") or 0 for t in tracks[:50])
    success = seconds>=parsed.get("target_duration_minutes",60)*60 if parsed.get("goal")=="duration" else len(tracks)>=parsed.get("target_count",20)
    return success and artists_complete(row), tracks


def result(row):
    parsed = row["request"]["parsed"]
    remaining = deduplicated(qualified(row))
    ordered = []
    duration = 0
    artist_counts = {}
    while remaining and len(ordered)<50:
        if parsed["goal"]=="count" and len(ordered)>=parsed["target_count"]:
            break
        if parsed["goal"]=="duration" and duration>=parsed["target_duration_minutes"]*60:
            break
        previous = ((ordered[-1].get("analysis") or {}).get("features") or {}).get("energy") if ordered else None
        def rank(t):
            energy = ((t.get("analysis") or {}).get("features") or {}).get("energy")
            delta = abs(previous-energy)*.25 if previous is not None and energy is not None else 0
            return (t["match"]["score"], -artist_counts.get(t["artist"],0), -delta)
        chosen = max(remaining, key=rank)
        remaining.remove(chosen)
        duration += chosen["validation"].get("duration") or chosen.get("duration") or 0
        artist_counts[chosen["artist"]] = artist_counts.get(chosen["artist"],0)+1
        chosen = dict(chosen)
        chosen.pop("user_id", None)
        chosen["reason"] = f"{parsed['scene']}场景 · {chosen['source']['label']} · 匹配分 {chosen['match']['score']}"
        if chosen["match"].get("matched_artists"):
            chosen["reason"] += " · 歌手偏好 +20：" + "、".join(chosen["match"]["matched_artists"])
        chosen["reason"] = chosen["reason"][:600]
        chosen["unknown_features"] = [k for k,v in chosen.get("analysis",{}).get("features",{}).items() if v is None]
        ordered.append(chosen)
    gap_count = max(0, parsed["target_count"]-len(ordered)) if parsed["goal"]=="count" else 0
    gap_seconds = max(0, parsed["target_duration_minutes"]*60-duration)
    rejected = [{"key":t["key"], "title":t["title"], "artist":t["artist"], "reasons":features.match(t, parsed)["reasons"]}
                for t in row["context"].get("candidates",[]) if not features.match(t,parsed)["qualified"]]
    return RunResult.model_validate({"title":parsed["title"], "tracks":ordered, "duration_seconds":round(duration),
            "gap": {"count":gap_count,"seconds":round(gap_seconds), "primary":parsed["goal"]},
            "coverage":row["context"].get("coverage",{}) | ({"artists": artist_coverage(row)} if parsed.get("preferred_artists") else {}), "rejected":rejected,
            "notice":"结果来自已检查候选范围；播放时重新解析音源", "budget":row["context"].get("budget",{})}).model_dump()


def save(run_id, result_version, name=None):
    with library.database() as cursor:
        row = store.get(cursor, run_id, accounts.current().id, lock=True)
        if row["status"] not in {"finished","partial"} or row["result_version"]!=result_version:
            raise HTTPException(409, "结果版本已改变或当前不能保存")
        if row["saved_at"]:
            if row["saved_playlist_id"]:
                return {"playlist_id":row["saved_playlist_id"], "already_saved":True}
            raise HTTPException(410, "已保存的歌单已删除，重新创建需生成新任务并确认")
        tracks = row["result"].get("tracks",[])
        if not tracks:
            raise HTTPException(422, "没有合格曲目可保存")
        import secrets
        key, now = secrets.token_hex(12), time.time()
        playlist = library.PlaylistRequest(name=name or row["result"]["title"], description=row["request"]["need"][:300])
        cursor.execute("INSERT INTO music_playlists (id,name,description,created_at,user_id) VALUES (%s,%s,%s,%s,%s)",
                       (key,playlist.name,playlist.description,now,row["user_id"]))
        saved = set()
        for track in tracks:
            track_key = library.save_track(cursor, library.TrackInput.model_validate(track))
            if track_key in saved:
                continue
            cursor.execute("INSERT INTO music_playlist_tracks (playlist_id,track_id,added_at,position) VALUES (%s,%s,%s,%s)",
                           (key,track_key,now,len(saved)))
            saved.add(track_key)
        store.update(cursor, row, saved_playlist_id=key, saved_at=now)
        return {"playlist_id":key,"already_saved":False}


def execute(token, name, arguments):
    with auth.tool_scope(token,name) as (row,_):
        def guard():
            current = check_active(row["run_id"])
            if not network_allowed(current):
                error = HTTPException(403,"当前任务已禁止新增网络请求")
                error.agent_policy_error = True
                raise error
            if name != "validate_tracks" and budget.validation_only(current):
                raise budget.BudgetExceeded("剩余网络额度已预留给播放校验", "network_reserve")
            budget.charge(row["run_id"],network_requests=1)
        marker = network.request_guard.set(guard)
        try:
            return execute_scoped(token,name,arguments)
        finally:
            network.request_guard.reset(marker)


def execute_scoped(token, name, arguments):
    started = time.monotonic()
    with auth.tool_scope(token, name) as (row, claims):
        run_id = row["run_id"]
        if name == "save_playlist":
            if arguments.get("result_version") != claims["result_version"]:
                raise HTTPException(403,"保存授权版本不匹配")
            data = save(run_id, claims["result_version"], arguments.get("name"))
            return {"data":data, "source":"MySQL", "coverage":{"result_version":claims["result_version"]},
                    "error":None, "elapsed":round(time.monotonic()-started,3), "cost":{}}
        charge = partial(budget.charge, run_id)
        before = current_run(run_id)["context"].get("budget",{})
        error = None
        data = {}
        try:
            charge(tools=1)
            row = check_active(run_id)
            if name == "search_my_music":
                data = search_personal(run_id, **arguments)
            elif name == "list_music_categories":
                data = platforms.categories()
            elif name in {"search_online_playlists", "get_playlist_tracks", "search_online_tracks"}:
                provider = arguments["provider"]
                online_guard(row, provider)
                if name == "search_online_playlists":
                    data = platforms.search_playlists(provider,arguments["query"],arguments.get("page",1),charge)
                    with library.database() as cursor:
                        current = store.get(cursor,run_id,lock=True)
                        known = current["context"].get("online_playlists",[])
                        store.update(cursor,current,context=current["context"] | {"online_playlists":(known+data["playlists"])[-50:]})
                elif name == "get_playlist_tracks":
                    if not any(p["provider"]==provider and p["playlist_id"]==arguments["playlist_id"] for p in row["context"].get("online_playlists",[])):
                        raise HTTPException(422,"只能读取已经检索到的歌单")
                    data = platforms.playlist_tracks(provider,arguments["playlist_id"],arguments.get("offset",0),arguments.get("limit",20),charge)
                    data["tracks"] = add_candidates(run_id,data["tracks"],{"kind":"online_playlist","label":data["playlist"]["name"],"provider":provider,"playlist_id":arguments["playlist_id"]})
                else:
                    charge(search_rounds=1)
                    rows = [t.model_dump() for t in music._search(provider,arguments["query"],arguments.get("page",1))]
                    data = {"tracks":add_candidates(run_id,rows,{"kind":"online_search","label":"在线单曲检索","provider":provider}), "has_more":len(rows)==music.SEARCH_PAGE_SIZE}
                with library.database() as cursor:
                    current = store.get(cursor,run_id,lock=True)
                    coverage = dict(current["context"].get("coverage",{}))
                    if name == "get_playlist_tracks":
                        online = dict(coverage.get("online_playlists",{}))
                        online[provider+":"+arguments["playlist_id"]] = data.get("coverage",{}) | {
                            "name":data["playlist"]["name"], "checked_until":data["next_offset"], "has_more":data.get("has_more",False)}
                        coverage["online_playlists"] = online
                    else:
                        coverage["searches"] = (coverage.get("searches",[])+[{
                            "provider":provider, "query":arguments["query"], "tool":name,
                            "page":arguments.get("page",1), "count":len(data.get("tracks",data.get("playlists",[])))}])[-5:]
                    store.update(cursor,current,context=current["context"] | {"coverage":coverage})
            elif name in {"get_track_features","validate_tracks"}:
                tracks = selected_candidates(row,arguments["keys"])[:batch_size(row)]
                parsed = row["request"]["parsed"]
                data = {"tracks":[]}
                for track in tracks:
                    current = check_active(run_id)
                    if satisfied(current)[0]:
                        break
                    if name == "get_track_features":
                        value = features.analyze(run_id,track,charge,network_allowed(row),
                            need_audio=parsed["scene"] in {"睡眠","游戏","运动","工作","专注","放松"},check=lambda:check_active(run_id))
                        replace_candidate(run_id,track["key"],analysis=value)
                        data["tracks"].append({"key":track["key"], **value})
                    else:
                        value = validation.validate(track,charge,network_allowed(row),check=lambda:check_active(run_id))
                        replace_candidate(run_id,track["key"],validation=value)
                        data["tracks"].append({"key":track["key"],**value})
            else:
                raise ValueError("Unknown tool")
        except budget.BudgetExceeded as exc:
            error = {"code":"BUDGET_EXHAUSTED","message":str(exc),"budget":getattr(exc,"resource",None),"retryable":False}
        except HTTPException as exc:
            if exc.status_code in {401,503} or exc.detail=="任务已停止":
                raise
            error = {"code":str(exc.status_code),"message":str(exc.detail),"retryable":False}
        except Exception:
            error = {"code":"SOURCE_UNAVAILABLE","message":"来源未返回有效数据，请尝试其他来源或候选","retryable":True}
        if name in {"search_my_music", "search_online_tracks"}:
            query = arguments.get("query", "")
            artists = row["request"]["parsed"].get("preferred_artists", [])
            artist = next((a for a in artists if features.artist_identity(a) == features.artist_identity(query)), None)
            if artist:
                with library.database() as cursor:
                    current = store.get(cursor, run_id, lock=True)
                    searches = current["context"].get("artist_searches", [])
                    record = {"artist": artist, "source": arguments.get("provider", "personal"), "success": error is None}
                    store.update(cursor, current, context=current["context"] | {"artist_searches": (searches + [record])[-40:]})
        budget.mark_step(run_id,name,data,error,time.monotonic()-started)
        after = current_run(run_id)["context"].get("budget",{})
        return {"data":data,"source":name,"coverage":data.get("coverage"),"error":error,
                "elapsed":round(time.monotonic()-started,3),"cost":{k:v-before.get(k,0) for k,v in after.items()}}
