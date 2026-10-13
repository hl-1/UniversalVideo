from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from backend import accounts, library
from backend.music_agent import auth, mcp, runtime, skill, store
from backend.music_agent.models import ContinueRun, CreateRun, RESUMABLE, SaveRun


router = APIRouter(prefix="/api/music/recommendations",tags=["scene-playlist"])


@router.get("/capabilities")
def capabilities():
    config = accounts.settings()
    return {"enabled":config["music_agent_enabled"],"network_enabled":config["music_agent_network_enabled"],
            "sources":config["music_agent_sources"],"skill_version":skill.load()["version"]}


@router.post("",status_code=201)
def create_run(payload:CreateRun,idempotency_key:str=Header(min_length=8,max_length=200)):
    auth.require_enabled()
    auth.check_session(accounts.current().id,accounts.current().session_id)
    row,created = store.create(payload.model_dump(),idempotency_key,skill.load()["version"])
    if created:
        runtime.submit(row["run_id"])
    return store.public(row)


@router.get("")
def list_runs(page:int=Query(default=1,ge=1)):
    uid = accounts.current().id
    with library.database() as cursor:
        store.recover(cursor)
        cursor.execute("SELECT COUNT(*) AS n FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE",(uid,))
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT * FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE ORDER BY created_at DESC LIMIT 20 OFFSET %s",(uid,(page-1)*20))
        rows = [store.public(store.decode(r)) for r in cursor.fetchall()]
    return {"items":rows,"total":total}


@router.get("/{run_id}")
def get_run(run_id:str):
    with library.database() as cursor:
        store.recover(cursor)
        return store.public(store.get(cursor,run_id,accounts.current().id))


@router.post("/{run_id}/continue")
def continue_run(run_id:str,payload:ContinueRun):
    auth.require_enabled()
    uid = accounts.current().id
    auth.check_session(uid,accounts.current().session_id)
    with library.database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE",(uid,))
        store.recover(cursor)
        row = store.get(cursor,run_id,uid,lock=True)
        if row["status"] not in RESUMABLE:
            raise HTTPException(409,"当前任务不可继续，请创建重试任务")
        cursor.execute("SELECT COUNT(*) AS n FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running')",(uid,))
        if cursor.fetchone()["n"]:
            raise HTTPException(409,"已有任务正在执行")
        request = dict(row["request"])
        if payload.need or payload.preferences:
            request["need"] = (request["need"]+"；"+payload.need)[:1000] if payload.need else request["need"]
            request["preferences"] = request.get("preferences",{}) | payload.preferences
            request.pop("parsed",None)
        elif row["status"]=="waiting_input":
            raise HTTPException(422,"请补充听歌需求")
        context = row["context"] | {"session_id":accounts.current().session_id,"message":"等待继续","waiting_reason":None}
        row = store.update(cursor,row,status="pending",request=request,context=context,worker_id=None,lease_until=time.time()+60)
    runtime.submit(run_id)
    return store.public(row)


@router.post("/{run_id}/cancel")
def cancel_run(run_id:str):
    with library.database() as cursor:
        row = store.get(cursor,run_id,accounts.current().id,lock=True)
        if row["status"] in {"pending","running",*RESUMABLE}:
            row = store.update(cursor,row,status="cancelled",worker_id=None,lease_until=None,context=row["context"] | {"message":"已取消"})
    return store.public(row)


@router.post("/{run_id}/retry",status_code=201)
def retry_run(run_id:str,payload:ContinueRun,idempotency_key:str=Header(min_length=8,max_length=200)):
    auth.require_enabled()
    with library.database() as cursor:
        old = store.get(cursor,run_id,accounts.current().id)
    request = {"need":payload.need or old["request"]["need"],"preferences":old["request"].get("preferences",{}) | payload.preferences}
    CreateRun.model_validate(request)
    row,created = store.create(request,idempotency_key,skill.load()["version"],run_id)
    if created:
        runtime.submit(row["run_id"])
    return store.public(row)


@router.post("/{run_id}/save")
def save_run(run_id:str,payload:SaveRun):
    auth.require_enabled()
    auth.check_session(accounts.current().id,accounts.current().session_id)
    with library.database() as cursor:
        row = store.get(cursor,run_id,accounts.current().id,lock=True)
        if row["result_version"]!=payload.result_version or row["status"] not in {"finished","partial"}:
            raise HTTPException(409,"当前不能保存此结果版本")
        row = store.update(cursor,row,context=row["context"] | {"session_id":accounts.current().session_id})
    credential = auth.issue(row,save_version=payload.result_version)
    response = mcp.LocalClient(credential).call("save_playlist",payload.model_dump())
    return response["data"]


@router.delete("/{run_id}")
def remove_run(run_id:str):
    return delete_run(run_id)


def delete_run(run_id:str,admin=False):
    if admin:
        accounts.admin()
    with library.database() as cursor:
        row = store.get(cursor,run_id,None if admin else accounts.current().id,lock=True)
        store.update(cursor,row,deleted=True,status="cancelled" if row["status"] in {"pending","running",*RESUMABLE} else row["status"],
                     worker_id=None,lease_until=None)
    return {"ok":True}


class LyricsResultRequest(BaseModel):
    result_version: int = Field(ge=1)


@router.post('/{run_id}/tracks/{key}/lyrics')
def result_lyrics(run_id: str, key: str, payload: LyricsResultRequest):
    from backend import music_lyrics
    uid = accounts.current().id
    def checked_track():
        with library.database() as cursor:
            row = store.get(cursor, run_id, uid)
        if row['result_version'] != payload.result_version or row['status'] not in {'finished', 'partial'}:
            raise HTTPException(409, '结果版本已变化，请刷新后重试')
        track = next((t for t in row['result'].get('tracks', []) if t['key'] == key), None)
        if track is None:
            raise HTTPException(404, '歌曲不在当前结果中')
        return row, track
    row, track = checked_track()
    config = accounts.settings()
    allowed = bool(row['request']['parsed']['network_allowed'] and config['music_agent_network_enabled']
                   and track['provider'] in config['music_agent_sources'])
    return music_lyrics.track_lyrics_response(track, allowed, check=checked_track)
