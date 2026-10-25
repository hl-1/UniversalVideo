import hashlib
import json
import secrets
import time
import uuid
from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import store as runs
from backend.modules.music.agent.models import POLICY_VERSION
from . import identity, policy
from .models import Settings


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def lock_user(cursor, uid):
    cursor.execute('SELECT id FROM users WHERE id=%s FOR UPDATE', (uid,))


def profile(cursor, uid):
    now = time.time()
    cursor.execute('''INSERT IGNORE INTO music_discovery_profiles
        (user_id,profile_version,settings,long_term,short_term,created_at,updated_at)
        VALUES (%s,%s,%s,'{}','{}',%s,%s)''', (uid,policy.VERSION,runs.encode(Settings().model_dump()),now,now))
    cursor.execute('SELECT * FROM music_discovery_profiles WHERE user_id=%s', (uid,))
    row = cursor.fetchone()
    row = row | {k:decoded(row[k]) for k in ('settings','long_term','short_term')}
    row['settings'] = Settings().model_dump() | row['settings']
    row['agent_state'] = decoded(row.get('agent_state')) or {'schema_version': 1, 'items': [], 'hypotheses': [], 'summary': ''}
    return row


def invalidate(cursor, uid, evidence=True, control=False):
    profile(cursor, uid)
    cursor.execute('''UPDATE music_discovery_profiles SET revision=revision+1,
        evidence_revision=evidence_revision+%s,control_revision=control_revision+%s,
        next_review_at=COALESCE(next_review_at,%s),updated_at=%s WHERE user_id=%s''',
        (int(evidence),int(control),time.time(),time.time(),uid))


def get_batch(cursor, batch_id, uid):
    cursor.execute('SELECT * FROM music_discovery_batches WHERE batch_id=%s AND user_id=%s', (batch_id,uid))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(404,'今日推荐批次不存在')
    return row


def blocked(cursor, uid, recording, work, now=None, identities=None):
    now = now or time.time()
    recordings = (identities or identity.catalog(cursor,uid)).aliases(recording)
    cursor.execute('''SELECT 1 FROM music_discovery_feedback WHERE user_id=%s AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at>%s)
        AND ((scope='recording' AND target_key IN ('''+','.join(['%s']*len(recordings))+'''))
        OR (scope='work' AND target_key=%s)) LIMIT 1''', (uid,now,*recordings,work))
    return bool(cursor.fetchone())


def excluded(cursor, uid, ident, batch_id=None, replay=False, identities=None):
    key, recording, work = ident['track_key'],ident['recording_key'],ident['work_key']
    now = time.time()
    identities = identities or identity.catalog(cursor,uid)
    recordings = identities.aliases(recording)
    placeholders = ','.join(['%s']*len(recordings))
    if blocked(cursor,uid,recording,work,identities=identities):
        return True
    cursor.execute('''SELECT t.id FROM music_tracks t WHERE t.user_id=%s AND (t.id=%s OR t.id IN (SELECT track_key FROM music_discovery_identities WHERE user_id=%s AND (recording_key IN ('''+placeholders+''') OR (work_key IS NOT NULL AND work_key=%s)))) AND
        (EXISTS(SELECT 1 FROM music_favorites f WHERE f.track_id=t.id) OR
         EXISTS(SELECT 1 FROM music_history h WHERE h.track_id=t.id))''',(uid,key,uid,*recordings,work))
    if cursor.fetchone():
        return True
    if replay:
        return False
    cursor.execute('''SELECT 1 FROM music_discovery_feed_items i JOIN music_discovery_feeds f ON f.feed_id=i.feed_id
        WHERE f.user_id=%s AND f.created_at>%s AND (i.recording_key IN ('''+placeholders+''') OR (i.work_key IS NOT NULL AND i.work_key=%s)) LIMIT 1''',
        (uid,now-7*86400,*recordings,work))
    if cursor.fetchone():
        return True
    now = time.time()
    cursor.execute('''SELECT 1 FROM music_discovery_events e JOIN music_discovery_identities i
        ON i.user_id=e.user_id AND i.track_key=e.track_key WHERE e.user_id=%s AND e.received_at>%s
        AND e.event_type LIKE 'playback_%%' AND (i.recording_key IN ('''+placeholders+''') OR (i.work_key IS NOT NULL AND i.work_key=%s)) LIMIT 1''',
        (uid,now-180*86400,*recordings,work))
    if cursor.fetchone():
        return True
    cursor.execute('''SELECT 1 FROM music_discovery_items i JOIN music_discovery_batches b ON b.batch_id=i.batch_id
        WHERE b.user_id=%s AND (%s IS NULL OR b.batch_id<>%s) AND
        (i.recording_key IN ('''+placeholders+''') OR (i.work_key IS NOT NULL AND i.work_key=%s)) AND
        (i.last_exposed_at>%s OR (i.first_exposed_at IS NULL AND i.published_at>%s)) LIMIT 1''',
        (uid,batch_id,batch_id,*recordings,work,now-7*86400,now-7*86400))
    return bool(cursor.fetchone())


def create(payload, key, skill_version, automatic=False):
    uid = accounts.current().id
    config = accounts.settings()
    request = payload.model_dump() | {'kind':policy.KIND}
    digest = hashlib.sha256(runs.encode(request).encode()).hexdigest()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    with library.database() as cursor:
        from . import usage
        usage.lock(cursor,uid)
        runs.recover(cursor)
        if automatic:
            cursor.execute('SELECT * FROM music_discovery_batches WHERE user_id=%s AND recommendation_date=%s ORDER BY batch_no DESC LIMIT 1',(uid,policy.today()))
            existing=cursor.fetchone()
            if existing:
                return existing,False
        cursor.execute('SELECT * FROM music_discovery_batches WHERE user_id=%s AND request_key=%s',(uid,key_hash))
        old = cursor.fetchone()
        if old:
            if old['request_hash'] != digest:
                raise HTTPException(409,'幂等键已用于不同参数')
            return old,False
        cleanup(cursor,uid)
        p = profile(cursor,uid)
        if automatic:
            from .profile import build
            evidence = build(cursor,uid)
            if not (evidence['long_term']['features'] or p['settings']['seed_track_keys'] or p['settings']['languages'] or p['settings']['long_term_need']):
                raise HTTPException(422,'先收藏喜欢的歌曲或设置听歌偏好，即可自动准备今日推荐')
            request['need'] = p['settings']['long_term_need']
            request['exploration'] = p['settings']['exploration']
        seeds = list(dict.fromkeys(payload.seed_track_keys+p['settings']['seed_track_keys']))
        for seed in seeds:
            cursor.execute('SELECT id FROM music_tracks WHERE id=%s AND user_id=%s',(seed,uid))
            if not cursor.fetchone():
                raise HTTPException(422,'种子歌曲不属于当前用户音乐库')
        cursor.execute('SELECT COUNT(*) AS n FROM music_discovery_batches WHERE user_id=%s AND recommendation_date=%s',(uid,policy.today()))
        count = cursor.fetchone()['n']
        usage.sync_candidate(cursor,uid,config)
        cursor.execute('SELECT MAX(next_refill_at) AS next_at FROM music_discovery_daily_usage WHERE user_id=%s',(uid,))
        next_at=cursor.fetchone()['next_at'] or 0
        if next_at>time.time():
            raise HTTPException(429,'候选补充冷却中，请使用候选池换歌',headers={'Retry-After':str(max(1,int(next_at-time.time())))})
        if count>=255:
            raise HTTPException(429,'旧客户端批次编号已满，请升级使用候选列表接口')
        cursor.execute("SELECT 1 FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running') LIMIT 1",(uid,))
        if cursor.fetchone():
            raise HTTPException(409,'已有推荐任务正在执行')
        now = time.time()
        limits = {k.removeprefix('music_agent_'):v for k,v in config.items() if k.startswith('music_agent_')}
        limits.update({k:min(v,limits.get(k,v)) for k,v in policy.LIMITS.items()})
        request['need'] = request['need'] or '根据个人偏好发现陌生歌曲'
        request['parsed'] = {'network_allowed':True,'source_mode':'discover','preferred_artists':[]}
        row = dict(run_id=secrets.token_hex(16),user_id=uid,parent_run_id=None,status='pending',request=request,
            context={'session_id':accounts.current().session_id,'budget':{},'limits':limits,'message':'等待执行','candidates':[]},
            steps=[],result={},policy_version=POLICY_VERSION,skill_version=skill_version,
            idempotency_key=hashlib.sha256(('daily:'+key).encode()).hexdigest(),revision=0,result_version=0,
            saved_playlist_id=None,saved_at=None,worker_id=None,lease_until=now+60,created_at=now,updated_at=now,deleted=False)
        runs.insert(cursor,row)
        cursor.execute('UPDATE music_discovery_daily_usage SET next_refill_at=%s WHERE user_id=%s AND usage_date=%s',
            (now+policy.REFILL_INTERVAL,uid,policy.today()))
        batch = dict(batch_id=secrets.token_hex(16),user_id=uid,run_id=row['run_id'],recommendation_date=policy.today(),
            batch_no=count+1,target_count=policy.TARGET,request_key=key_hash,request_hash=digest,profile_revision=p['revision'],
            policy_version=policy.VERSION,generation_seed=secrets.token_hex(32),created_at=now)
        columns=list(batch)
        cursor.execute('INSERT INTO music_discovery_batches ('+','.join(columns)+') VALUES ('+','.join(['%s']*len(columns))+')',tuple(batch.values()))
        return batch,True


def public(cursor, batch):
    row=runs.get(cursor,batch['run_id'],batch['user_id'])
    tracks=[]
    for track in row['result'].get('tracks',[]):
        d=track['discovery']
        if not blocked(cursor,batch['user_id'],d['recording_key'],d['work_key']):
            tracks.append(track)
    cursor.execute('SELECT COUNT(*) AS n FROM music_discovery_batches WHERE user_id=%s AND recommendation_date=%s',(batch['user_id'],policy.today()))
    return {k:batch[k] for k in ('batch_id','run_id','recommendation_date','batch_no','target_count','profile_revision')} | {
        'status':row['status'],'result_version':row['result_version'],'tracks':tracks,'actual_count':len(tracks),
        'gap_reason':row['result'].get('gap_reason',row['context'].get('message','')),
        'usage_pending':any(r.get('state')=='unconfirmed' for r in row['context'].get('model_requests',[])),
        'remaining_batches':None, 'budget':row['context'].get('budget',{})}


def feedback(cursor, uid, payload, revoke=False):
    lock_user(cursor,uid)
    cursor.execute('SELECT * FROM music_discovery_identities WHERE user_id=%s AND track_key=%s',(uid,payload.track_key))
    ident=cursor.fetchone()
    if not ident:
        raise HTTPException(404,'歌曲身份不存在')
    scope='work' if payload.scope=='work' and ident['work_key'] else 'recording'
    target=ident[scope+'_key']
    now=time.time()
    expiry=now+90*86400 if payload.reason=='fatigue' else policy.day_end(now) if payload.reason=='today' else None
    event=str(uuid.uuid4())
    cursor.execute('''INSERT INTO music_discovery_feedback
        (user_id,scope,target_key,reason,note,expires_at,revoked_at,source_event_id,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE note=VALUES(note),
        expires_at=VALUES(expires_at),revoked_at=VALUES(revoked_at),source_event_id=VALUES(source_event_id),
        revision=revision+1,updated_at=VALUES(updated_at)''',
        (uid,scope,target,payload.reason,payload.note,expiry,now if revoke else None,event,now,now))
    cursor.execute('''INSERT INTO music_discovery_events
        (user_id,event_id,track_key,event_type,origin,payload,occurred_at,received_at)
        VALUES (%s,%s,%s,%s,'server',%s,%s,%s)''',
        (uid,event,payload.track_key,'feedback_revoke' if revoke else 'feedback_set',runs.encode(payload.model_dump()),now,now))
    invalidate(cursor,uid,control=True)
    return {'scope':scope,'target_key':target,'notice':'作品关系未知，仅作用于已确认录音' if scope!=payload.scope else ''}


def ingest(cursor, uid, events):
    lock_user(cursor,uid)
    accepted=0
    now=time.time()
    for event in events:
        data=event.model_dump(mode='json')
        if not now-86400<=event.occurred_at<=now+300:
            raise HTTPException(422,'事件时间超出可接受范围')
        if event.track:
            # save_track performs site/file ownership validation before registration.
            library.save_track(cursor,event.track)
            ident=identity.ensure(cursor,uid,event.track.model_dump())
            key=ident['track_key']
            if event.track_key and event.track_key!=key:
                raise HTTPException(422,'歌曲标识不匹配')
        else:
            key=event.track_key
            cursor.execute('SELECT 1 FROM music_discovery_identities WHERE user_id=%s AND track_key=%s',(uid,key))
            if not cursor.fetchone():
                raise HTTPException(404,'歌曲不属于当前用户')
        if event.batch_id:
            cursor.execute('''SELECT i.track_key FROM music_discovery_items i JOIN music_discovery_batches b ON b.batch_id=i.batch_id
                WHERE b.user_id=%s AND i.batch_id=%s AND i.position=%s''',(uid,event.batch_id,event.position))
            item=cursor.fetchone()
            if not item or item['track_key']!=key:
                raise HTTPException(422,'批次歌曲位置不匹配')
        if event.feed_id:
            cursor.execute('''SELECT i.track_key FROM music_discovery_feed_items i JOIN music_discovery_feeds f ON f.feed_id=i.feed_id
                WHERE f.user_id=%s AND i.feed_id=%s AND i.position=%s''',(uid,event.feed_id,event.position))
            item=cursor.fetchone()
            if not item or item['track_key']!=key:
                raise HTTPException(422,'列表歌曲位置不匹配')
        if event.playback_id:
            cursor.execute('''SELECT * FROM music_discovery_events WHERE user_id=%s AND playback_id=%s
                ORDER BY seq DESC LIMIT 1''',(uid,str(event.playback_id)))
            previous=cursor.fetchone()
            if not previous and event.event_type!='playback_start':
                continue
            if event.event_type=='playback_start' and (event.seq!=0 or event.listened_ms or event.covered_ms):
                raise HTTPException(422,'播放开始事件必须从零开始')
            if previous:
                if previous['track_key']!=key or previous['origin']!=event.origin:
                    raise HTTPException(422,'播放会话归属不匹配')
                if event.seq<=previous['seq']:
                    continue
                if event.listened_ms-previous['listened_ms'] > max(0,event.occurred_at-previous['occurred_at'])*1500+2000:
                    raise HTTPException(422,'播放时长超过事件时间跨度')
                if event.listened_ms<previous['listened_ms'] or event.covered_ms<previous['covered_ms']:
                    raise HTTPException(422,'累计播放数据不能倒退')
                if previous['event_type']=='playback_end':
                    continue
        replay=False
        if event.event_type=='playback_start' and event.active:
            cursor.execute("SELECT 1 FROM music_discovery_events WHERE user_id=%s AND track_key=%s AND event_type='playback_start' AND received_at>%s AND playback_id<>%s LIMIT 1",(uid,key,now-180*86400,str(event.playback_id)))
            replay=bool(cursor.fetchone())
        elif event.playback_id:
            cursor.execute("SELECT payload FROM music_discovery_events WHERE user_id=%s AND playback_id=%s AND event_type='playback_start' LIMIT 1",(uid,str(event.playback_id)))
            start=cursor.fetchone()
            replay=bool(start and decoded(start['payload']).get('replay'))
        cursor.execute('''INSERT IGNORE INTO music_discovery_events
            (user_id,event_id,track_key,batch_id,position,playback_id,seq,event_type,origin,listened_ms,covered_ms,
             duration_ms,terminal_reason,payload,occurred_at,received_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (uid,str(event.event_id),key,event.batch_id,event.position,str(event.playback_id) if event.playback_id else None,
             event.seq,event.event_type,event.origin,event.listened_ms,event.covered_ms,event.duration_ms,event.terminal_reason,
             runs.encode({'active':event.active,'replay':replay,'context':event.context,
                          'feed_id':event.feed_id,'position':event.position}),event.occurred_at,now))
        if cursor.rowcount!=1:
            continue
        accepted+=1
        if event.feed_id and event.event_type in {'exposure','playback_start'}:
            cursor.execute('''UPDATE music_discovery_feed_items SET first_exposed_at=COALESCE(first_exposed_at,%s),
                last_exposed_at=%s,exposure_count=exposure_count+1 WHERE feed_id=%s AND position=%s''',
                (now,now,event.feed_id,event.position))
        if event.batch_id and event.event_type in {'exposure','playback_start'}:
            cursor.execute('''UPDATE music_discovery_items SET first_exposed_at=COALESCE(first_exposed_at,%s),
                last_exposed_at=%s,exposure_count=exposure_count+1 WHERE batch_id=%s AND position=%s''',
                (now,now,event.batch_id,event.position))
        if event.origin=='web' and event.event_type=='playback_end':
            from .profile import completed
            if completed(data) or replay and event.active or event.terminal_reason=='user_next' and event.listened_ms<15000:
                invalidate(cursor,uid)
    return {'accepted':accepted}


def library_signal(cursor, uid, track, kind, result=None):
    """Called inside the initiating library transaction, never for bulk imports."""
    ident=identity.ensure(cursor,uid,track.model_dump())
    now=time.time()
    cursor.execute('''INSERT INTO music_discovery_events
        (user_id,event_id,track_key,event_type,origin,payload,occurred_at,received_at)
        VALUES (%s,%s,%s,%s,'server',%s,%s,%s)''',
        (uid,str(uuid.uuid4()),ident['track_key'],kind,runs.encode({'result':result}),now,now))
    invalidate(cursor,uid)


def cleanup(cursor, uid):
    """Bounded per-user retention; persistent feedback is deliberately retained."""
    cutoff=time.time()-180*86400
    cursor.execute('DELETE FROM music_discovery_events WHERE user_id=%s AND received_at<%s LIMIT 500',(uid,cutoff))
    cursor.execute('DELETE FROM music_discovery_feeds WHERE user_id=%s AND created_at<%s LIMIT 100',(uid,time.time()-30*86400))
    cursor.execute('''SELECT b.batch_id FROM music_discovery_batches b JOIN music_recommendation_runs r ON r.run_id=b.run_id
        WHERE b.user_id=%s AND b.created_at<%s AND r.status NOT IN ('pending','running') LIMIT 50''',(uid,cutoff))
    for row in cursor.fetchall():
        cursor.execute('DELETE FROM music_discovery_batches WHERE batch_id=%s',(row['batch_id'],))
