"""Bounded candidate storage and durable refill admission; no I/O in admission."""
import hashlib
import secrets
import time

from backend.modules.accounts import service as accounts
from backend.modules.music.agent import store as runs
from backend.modules.music.agent.models import POLICY_VERSION
from . import identity, models, policy, store, usage


def snapshot(track):
    # Resolved/signed playback URLs are transient and must be resolved on play.
    return {k: v for k, v in track.items() if k in {
        'key','title','artist','album','provider','identifier','webpage_url','thumbnail','duration','source',
        'filename','file_id','provider_label','access'}}


def put(cursor, uid, track, run_id=None):
    validation = track.get('validation', {})
    at = validation.get('checked_at')
    if not at or not all(validation.get(k) for k in ('identity','playable','full')):
        return False
    ident = identity.ensure(cursor, uid, track)
    now = time.time()
    cursor.execute('''INSERT INTO music_discovery_candidates
        (user_id,track_key,recording_key,work_key,track_snapshot,analysis,validation,source_run_id,policy_version,
         validated_at,validation_expires_at,expires_at,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
        track_snapshot=VALUES(track_snapshot),analysis=VALUES(analysis),validation=VALUES(validation),
        source_run_id=VALUES(source_run_id),policy_version=VALUES(policy_version),validated_at=VALUES(validated_at),
        validation_expires_at=VALUES(validation_expires_at),expires_at=VALUES(expires_at),updated_at=VALUES(updated_at)''',
        (uid,ident['track_key'],ident['recording_key'],ident['work_key'],runs.encode(snapshot(track)),
         runs.encode(track.get('analysis', {})),runs.encode(validation),run_id,policy.VERSION,at,at+policy.VALIDATION_TTL,
         now+7*86400,now,now))
    return True


def tracks(cursor, uid, replay=False, expired=False, identities=None):
    now = time.time()
    identities = identities or identity.catalog(cursor,uid)
    cursor.execute('''SELECT * FROM music_discovery_candidates WHERE user_id=%s AND expires_at>%s
        AND validation_expires_at ''' + ('<=' if expired else '>') + ' %s ORDER BY updated_at DESC LIMIT 300', (uid,now,now))
    result = []
    for row in cursor.fetchall():
        if store.excluded(cursor,uid,row,replay=replay,identities=identities):
            continue
        track = store.decoded(row['track_snapshot']) | {
            'key':row['track_key'],'recording_key':row['recording_key'],'work_key':row['work_key'],
            '_recording_group':identities.recording(row['recording_key']),
            'user_id':uid,'analysis':store.decoded(row['analysis']),'validation':store.decoded(row['validation'])}
        if all(track['validation'].get(k) for k in ('identity','playable','full')):
            result.append(track)
    return result


def trim(cursor, uid):
    cursor.execute('DELETE FROM music_discovery_candidates WHERE user_id=%s AND expires_at<=%s LIMIT 500', (uid,time.time()))
    cursor.execute('SELECT track_key FROM music_discovery_candidates WHERE user_id=%s ORDER BY updated_at DESC LIMIT 300,300', (uid,))
    for row in cursor.fetchall():
        cursor.execute('DELETE FROM music_discovery_candidates WHERE user_id=%s AND track_key=%s', (uid,row['track_key']))


def refill_status(cursor, uid):
    config = accounts.settings()
    if not config['music_enabled'] or not config['music_daily_enabled'] or not config['music_agent_network_enabled']:
        return {'state':'disabled','retry_at':None}
    cursor.execute("SELECT 1 FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE "
        "AND status IN ('pending','running') AND JSON_UNQUOTE(JSON_EXTRACT(request,'$.kind'))='daily_discovery' LIMIT 1",(uid,))
    if cursor.fetchone():
        return {'state':'running','retry_at':None}
    cursor.execute('SELECT network_requests FROM music_discovery_daily_usage WHERE user_id=%s AND usage_date=%s',(uid,policy.today()))
    day = cursor.fetchone()
    if day and day['network_requests'] >= usage.candidate_limits(config)[2]:
        return {'state':'budget_exhausted','retry_at':policy.day_end(time.time())}
    cursor.execute('SELECT MAX(next_refill_at) AS next_at FROM music_discovery_daily_usage WHERE user_id=%s',(uid,))
    next_at = cursor.fetchone()['next_at'] or 0
    return {'state':'cooldown','retry_at':next_at} if next_at>time.time() else {'state':'idle','retry_at':None}


def schedule(cursor, uid, request=None):
    config = accounts.settings()
    if not config['music_enabled'] or not config['music_daily_enabled'] or not config['music_agent_network_enabled']:
        return None
    cursor.execute("SELECT run_id FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running') LIMIT 1", (uid,))
    active = cursor.fetchone()
    if active:
        return active['run_id']
    usage.sync_candidate(cursor,uid,config)
    cursor.execute('SELECT MAX(next_refill_at) AS next_at FROM music_discovery_daily_usage WHERE user_id=%s', (uid,))
    if (cursor.fetchone()['next_at'] or 0)>time.time():
        return None
    day = usage.current(cursor,uid)
    if day['network_requests'] >= day['network_request_limit']:
        return None
    from .profile import build
    p = build(cursor,uid)
    if not (p['long_term'].get('features') or p['settings']['languages'] or p['settings']['long_term_need'] or p['settings']['seed_track_keys']):
        if not request or not (request.need or request.seed_track_keys):
            return None
    value = (request or models.NextFeed(exploration=p['settings']['exploration'])).model_dump()
    from . import ranking, recall
    intent=recall.baseline(value,p)|{'context':value['context']}
    eligible=[]
    for track in tracks(cursor,uid):
        selection=ranking.score(track,p,intent)
        if selection:
            eligible.append(track|{'selection':selection})
    if len(eligible)>=config.get('music_daily_low_water',policy.LOW_WATER) and len(ranking.select(eligible))>=policy.TARGET:
        return None
    for key in value['seed_track_keys']:
        cursor.execute('SELECT id FROM music_tracks WHERE user_id=%s AND id=%s',(uid,key))
        if not cursor.fetchone():
            from fastapi import HTTPException
            raise HTTPException(422,'种子歌曲不属于当前用户')
    from .recall import skill
    now = time.time()
    limits = {k.removeprefix('music_agent_'):v for k,v in config.items() if k.startswith('music_agent_')}
    limits.update({k:min(v,limits.get(k,v)) for k,v in policy.LIMITS.items()})
    value.update(kind=policy.KIND,need=value['need'] or p['settings']['long_term_need'] or '根据个人偏好发现陌生歌曲',
                 parsed={'network_allowed':True,'source_mode':'discover','preferred_artists':[]})
    run_id = secrets.token_hex(16)
    row = dict(run_id=run_id,user_id=uid,parent_run_id=None,status='pending',request=value,
        context={'background':True,'refill':True,'budget':{},'limits':limits,'message':'候选补充已排队','candidates':[]},
        steps=[],result={},policy_version=POLICY_VERSION,skill_version=skill()['version'],
        idempotency_key=hashlib.sha256(('refill:'+run_id).encode()).hexdigest(),revision=0,result_version=0,
        saved_playlist_id=None,saved_at=None,worker_id=None,lease_until=now+60,created_at=now,updated_at=now,deleted=False)
    runs.insert(cursor,row)
    cursor.execute('UPDATE music_discovery_daily_usage SET next_refill_at=%s WHERE user_id=%s AND usage_date=%s',
                   (now+config.get('music_daily_refill_interval',policy.REFILL_INTERVAL),uid,policy.today()))
    return run_id


def bootstrap(cursor, uid):
    cursor.execute('''SELECT run_id,result FROM music_recommendation_runs WHERE user_id=%s
        AND JSON_UNQUOTE(JSON_EXTRACT(request,'$.kind'))='daily_discovery' AND created_at>%s''', (uid,time.time()-7*86400))
    for row in cursor.fetchall():
        for track in store.decoded(row['result']).get('tracks',[]):
            if (track.get('validation',{}).get('checked_at') or 0)>time.time()-policy.VALIDATION_TTL:
                cursor.execute('SELECT 1 FROM music_discovery_candidates WHERE user_id=%s AND track_key=%s',(uid,track['key']))
                if not cursor.fetchone():
                    put(cursor,uid,track,row['run_id'])
    trim(cursor,uid)
