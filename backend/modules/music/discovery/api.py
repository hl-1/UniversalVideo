import json
from fastapi import APIRouter, Header, HTTPException, Request, Query
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import auth, runtime, store as runs, model
from backend.modules.music.agent.models import SaveRun
from . import models, store, policy, recall, service, feeds, profile_store, profile_runtime, usage

router=APIRouter(prefix='/api/music/discovery',tags=['daily-discovery'])

@router.get('/capabilities')
def capabilities():
    config=accounts.settings()
    return {'enabled':config['music_enabled'] and config['music_daily_enabled'],'target_count':policy.TARGET,
            'daily_limit':policy.DAILY_LIMIT,'limits':{k:min(v,config.get('music_agent_'+k,v)) for k,v in policy.LIMITS.items()}}

@router.get('/today')
def today():
    uid=accounts.current().id
    with library.database() as cursor:
        runs.recover(cursor)
        cursor.execute('SELECT * FROM music_discovery_batches WHERE user_id=%s AND recommendation_date=%s ORDER BY batch_no DESC',(uid,policy.today()))
        batches=cursor.fetchall()
        return {'batches':[store.public(cursor,b) for b in batches], 'remaining_batches':None}


@router.post('/prepare')
def prepare():
    service.require_enabled()
    return feeds.prepare()


@router.get('/feed')
def feed():
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid)
        return feeds.public(cursor,uid,feeds.ensure_current(cursor,uid))


@router.post('/feed/next')
def next_feed(payload:models.NextFeed,idempotency_key:str=Header(min_length=8,max_length=200)):
    service.require_enabled()
    return feeds.next_feed(payload,idempotency_key)


@router.get('/feeds/{feed_id}')
def get_feed(feed_id:str):
    uid=accounts.current().id
    with library.database() as cursor: return feeds.public(cursor,uid,feeds.get(cursor,uid,feed_id))


@router.post('/feeds/{feed_id}/save')
def save_feed(feed_id:str,payload:models.SaveFeed):
    uid=accounts.current().id
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        return feeds.save(cursor,uid,feed_id,payload.name)

@router.post('/today')
def ensure_today():
    service.require_enabled()
    auth.check_session(accounts.current().id,accounts.current().session_id)
    batch,created=store.create(models.CreateBatch(),'automatic-today:'+str(policy.today()),recall.skill()['version'],automatic=True)
    if created: runtime.submit(batch['run_id'])
    with library.database() as cursor: return store.public(cursor,batch)

@router.post('/batches',status_code=201)
def create(payload:models.CreateBatch,idempotency_key:str=Header(min_length=8,max_length=200)):
    service.require_enabled()
    auth.check_session(accounts.current().id,accounts.current().session_id)
    batch,created=store.create(payload,idempotency_key,recall.skill()['version'])
    if created: runtime.submit(batch['run_id'])
    with library.database() as cursor: return store.public(cursor,batch)

@router.get('/batches/{batch_id}')
def get(batch_id:str):
    with library.database() as cursor:
        runs.recover(cursor)
        return store.public(cursor,store.get_batch(cursor,batch_id,accounts.current().id))

@router.post('/batches/{batch_id}/cancel')
def cancel(batch_id:str):
    with library.database() as cursor:
        batch=store.get_batch(cursor,batch_id,accounts.current().id)
    runtime.cancel_user(accounts.current().id,batch['run_id'])
    return get(batch_id)

@router.post('/batches/{batch_id}/save')
def save(batch_id:str,payload:SaveRun):
    service.require_enabled()
    auth.check_session(accounts.current().id,accounts.current().session_id)
    uid=accounts.current().id
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        batch=store.get_batch(cursor,batch_id,uid)
        row=runs.get(cursor,batch['run_id'],uid,lock=True)
        if row['result_version']!=payload.result_version or row['status'] not in {'finished','partial'}:
            raise HTTPException(409,'结果版本不可保存')
        if row['saved_playlist_id']:
            return {'playlist_id':row['saved_playlist_id'],'already_saved':True}
        tracks=store.public(cursor,batch)['tracks']
        if not tracks: raise HTTPException(422,'当前没有可保存歌曲')
        import secrets,time
        key=secrets.token_hex(12);now=time.time()
        cursor.execute('INSERT INTO music_playlists (id,user_id,name,description,created_at) VALUES (%s,%s,%s,%s,%s)',
                       (key,uid,payload.name or '今日推荐',row['request']['need'][:300],now))
        for i,t in enumerate(tracks):
            track_key=library.save_track(cursor,library.TrackInput.model_validate(t))
            cursor.execute('INSERT INTO music_playlist_tracks (playlist_id,track_id,added_at,position) VALUES (%s,%s,%s,%s)',(key,track_key,now,i))
        runs.update(cursor,row,saved_playlist_id=key,saved_at=now)
        return {'playlist_id':key,'already_saved':False}

@router.post('/events')
async def events(request:Request):
    # Check the actual streamed body, not only a client-provided Content-Length.
    chunks=bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks)>65536: raise HTTPException(413,'事件请求不得超过 64 KiB')
    try: payload=models.Events.model_validate_json(chunks)
    except ValueError as exc: raise HTTPException(422,'事件格式无效') from exc
    from starlette.concurrency import run_in_threadpool
    def persist():
        with library.database() as cursor: result=store.ingest(cursor,accounts.current().id,payload.events)
        profile_runtime.wake()
        return result
    return await run_in_threadpool(persist)

@router.get('/profile')
def profile():
    from .profile import build
    uid=accounts.current().id
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        result=build(cursor,uid)
        evidence=result.pop('evidence',[])
        wanted={ref for item in result['agent_state'].get('items',[])+result['agent_state'].get('hypotheses',[]) for ref in item.get('evidence_refs',[])}
        result['evidence_details']={e['ref']:{k:e[k] for k in ('track','at','context','weight')} for e in evidence if e['ref'] in wanted}
        missing=wanted-set(result['evidence_details'])
        if missing:
            cursor.execute('SELECT evidence_summary FROM music_discovery_profile_versions WHERE user_id=%s ORDER BY version DESC LIMIT 50',(uid,))
            for version in cursor.fetchall():
                for item in store.decoded(version['evidence_summary']).get('items',[]):
                    if item['ref'] in missing:
                        result['evidence_details'][item['ref']]={k:item[k] for k in ('track','at','context','weight') if k in item}
                        missing.discard(item['ref'])
        result['learning_enabled']=result['settings']['profile_learning_enabled']
        result['agent_enabled']=accounts.settings().get('music_profile_agent_enabled',True)
        cursor.execute('SELECT run_id,status,error_message,created_at FROM music_discovery_profile_runs WHERE user_id=%s ORDER BY created_at DESC LIMIT 1',(uid,))
        result['latest_evaluation']=cursor.fetchone()
        return result

@router.patch('/profile')
def update_profile(payload:models.Settings):
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid);saved=store.profile(cursor,uid)
        changes=payload.model_dump(exclude_unset=True)
        for key in changes.get('seed_track_keys',[]):
            cursor.execute('SELECT id FROM music_tracks WHERE user_id=%s AND id=%s',(uid,key))
            if not cursor.fetchone(): raise HTTPException(422,'种子歌曲不属于当前用户')
        if changes and any(saved['settings'].get(k)!=v for k,v in changes.items()):
            cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(runs.encode(saved['settings']|changes),uid))
            store.invalidate(cursor,uid,evidence=False,control=True)
            profile_store.schedule(cursor,uid,'settings',force=True)
    profile_runtime.wake()
    return profile()


@router.post('/profile/evaluations')
def evaluate_profile(idempotency_key:str=Header(min_length=8,max_length=200)):
    uid=accounts.current().id
    import hashlib
    request_key=hashlib.sha256(idempotency_key.encode()).hexdigest()
    with library.database() as cursor:
        usage.lock(cursor,uid)
        cursor.execute("SELECT * FROM music_discovery_profile_runs WHERE user_id=%s AND JSON_SEARCH(JSON_EXTRACT(context,'$.request_keys'),'one',%s) IS NOT NULL LIMIT 1",(uid,request_key))
        previous=profile_store.decode_run(cursor.fetchone())
        if previous:
            return {k:previous[k] for k in ('run_id','status','created_at')}
        row=profile_store.schedule(cursor,uid,'manual',force=True)
        if not row:
            return {'run_id':None,'status':'disabled' if not accounts.settings().get('music_profile_agent_enabled') else 'insufficient_evidence'}
        result={k:row[k] for k in ('run_id','status','created_at')}
        keys=row['context'].get('request_keys',[])
        if request_key not in keys and len(keys)<100:
            profile_store.update_run(cursor,row,context=row['context']|{'request_keys':keys+[request_key]})
    profile_runtime.wake()
    return result


@router.get('/profile/evaluations/{run_id}')
def evaluation(run_id:str):
    with library.database() as cursor:
        row=profile_store.get_run(cursor,accounts.current().id,run_id)
        return {k:row[k] for k in ('run_id','status','trigger_type','applied_version','error_code','error_message','created_at','started_at','finished_at')}


@router.get('/profile/versions')
def versions(before:int=Query(default=2**63-1,ge=1),limit:int=Query(default=20,ge=1,le=50)):
    with library.database() as cursor:
        cursor.execute('''SELECT version,base_version,source,changes,evidence_summary,summary,created_at
            FROM music_discovery_profile_versions WHERE user_id=%s AND version<%s ORDER BY version DESC LIMIT %s''',
            (accounts.current().id,before,limit))
        rows=[r|{k:store.decoded(r[k]) for k in ('changes','evidence_summary')} for r in cursor.fetchall()]
        return {'items':rows,'next_before':rows[-1]['version'] if len(rows)==limit else None}


def profile_control(payload,action):
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid)
        profile_store.control(cursor,uid,payload,action)
    profile_runtime.wake()
    return profile()


@router.post('/profile/corrections')
def correct_profile(payload:models.Correction):
    return profile_control(payload,'correction')


@router.post('/profile/reset')
def reset_profile(payload:models.ProfileControl):
    return profile_control(payload,'reset')


@router.post('/profile/restore')
def restore_profile(payload:models.RestoreProfile):
    return profile_control(payload,'restore')

@router.get('/feedback')
def list_feedback():
    uid=accounts.current().id
    with library.database() as cursor:
        cursor.execute('''SELECT f.*,i.track_key,i.canonical FROM music_discovery_feedback f JOIN music_discovery_identities i
            ON i.user_id=f.user_id AND ((f.scope='recording' AND i.recording_key=f.target_key) OR (f.scope='work' AND i.work_key=f.target_key))
            WHERE f.user_id=%s AND f.revoked_at IS NULL ORDER BY f.updated_at DESC LIMIT 500''',(uid,))
        return {'items':[r|{'canonical':store.decoded(r['canonical'])} for r in cursor.fetchall()]}

@router.put('/feedback')
def feedback(payload:models.Feedback):
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid)
        result=store.feedback(cursor,uid,payload)
        row=feeds.ensure_current(cursor,uid)
        pool_request=models.NextFeed(exploration=store.profile(cursor,uid)['settings']['exploration'])
        from . import pool
        pool.schedule(cursor,uid,pool_request)
        result['feed']=feeds.public(cursor,uid,row)
    profile_runtime.wake()
    return result

@router.delete('/feedback')
def revoke(payload:models.Feedback):
    with library.database() as cursor: return store.feedback(cursor,accounts.current().id,payload,True)
