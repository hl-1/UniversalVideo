"""Algorithmic feed allocation. This module never searches or calls a model."""
import hashlib
import secrets
import time

from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import store as runs
from . import identity, models, policy, pool, profile, ranking, recall, store, usage


def latest(cursor, uid):
    cursor.execute('SELECT * FROM music_discovery_feeds WHERE user_id=%s ORDER BY created_at DESC,feed_id DESC LIMIT 1',(uid,))
    return cursor.fetchone()


def current(cursor, uid):
    row=latest(cursor,uid)
    # Older builds could publish short lists. Recover a complete allocated list
    # without creating a new batch from expired candidate validation.
    if row and row['actual_count']<policy.TARGET:
        cursor.execute('''SELECT * FROM music_discovery_feeds WHERE user_id=%s AND actual_count=%s
            AND created_at>%s ORDER BY created_at DESC,feed_id DESC LIMIT 10''',(uid,policy.TARGET,time.time()-30*86400))
        for previous in cursor.fetchall():
            if len(public(cursor,uid,previous)['tracks'])==policy.TARGET:
                return previous
    return row


def ensure_current(cursor, uid, payload=None):
    row=latest(cursor,uid)
    identities=identity.catalog(cursor,uid)
    items=visible_items(cursor,uid,row,identities)
    retained=[item['_track'] | {'key':item['track_key'],'recording_key':item['recording_key'],
        'work_key':item['work_key'],'_recording_group':identities.recording(item['recording_key']),
        'selection':item['_selection'],'validation':{'checked_at':item['validated_at']}} for item in items]
    retained=ranking.select([],retained=retained)
    if len(retained)>=policy.TARGET: return row
    p=store.profile(cursor,uid)
    payload=payload or models.NextFeed(exploration=p['settings']['exploration'])
    if not row:
        allocate(cursor,uid,payload,'repair:'+secrets.token_hex(16))
    else:
        # Repair is a new immutable list, retaining songs and their original evidence.
        # Old list positions remain valid for in-flight playback/exposure events.
        p,selected=choose(cursor,uid,payload.model_copy(update={'replay':False}),row,retained)
        if len(selected)==policy.TARGET:
            return publish(cursor,uid,p,payload,'repair:'+secrets.token_hex(16),selected)
    return current(cursor,uid)


def get(cursor, uid, feed_id):
    cursor.execute('SELECT * FROM music_discovery_feeds WHERE user_id=%s AND feed_id=%s',(uid,feed_id))
    row=cursor.fetchone()
    if not row:
        raise HTTPException(404,'推荐列表不存在')
    return row


def visible_items(cursor, uid, row, identities=None, settings=None):
    identities=identities or identity.catalog(cursor,uid)
    settings=settings or store.profile(cursor,uid)['settings']
    items=[];recordings=set();works=set()
    if row:
        cursor.execute('SELECT * FROM music_discovery_feed_items WHERE feed_id=%s ORDER BY position',(row['feed_id'],))
        for item in cursor.fetchall():
            recording=identities.recording(item['recording_key'])
            if recording in recordings or item['work_key'] and item['work_key'] in works:
                continue
            if store.blocked(cursor,uid,item['recording_key'],item['work_key'],identities=identities):
                continue
            track=store.decoded(item['track_snapshot'])
            # Validation expiry controls new allocation, not the lifetime of a
            # published song. Playback resolves a fresh source and checks duration.
            selection=store.decoded(item['selection'])
            values=selection.get('verified_features',{})
            if track.get('artist') in settings['excluded_artists'] or settings['languages'] and values.get('language') not in settings['languages']:
                continue
            if not item['validated_at']:
                continue
            recordings.add(recording);works.add(item['work_key'])
            items.append(item | {'_track':track,'_selection':selection})
    return items


def public(cursor, uid, row=None, reason=None):
    p=store.profile(cursor,uid)
    tracks=[];validation_stale=False
    for item in visible_items(cursor,uid,row,settings=p['settings']):
        selection=item['_selection']
        stale=time.time()-item['validated_at']>policy.VALIDATION_TTL
        validation_stale=validation_stale or stale
        tracks.append(item['_track'] | {'key':item['track_key'],'discovery':{
            'feed_id':row['feed_id'],'position':item['position'],'recording_key':item['recording_key'],
            'work_key':item['work_key'],'match_score':selection['match_score'],
            'reason':selection['reason'],'evidence_refs':selection['evidence_refs'],
            'exploration':selection.get('exploration',False),'validation_stale':stale}})
    refill=pool.refill_status(cursor,uid)
    refreshing=refill['state']=='running'
    cursor.execute('SELECT MAX(created_at) AS last_at FROM music_discovery_feeds WHERE user_id=%s',(uid,))
    last_at=cursor.fetchone()['last_at'] or 0
    retry=max(0,policy.CHANGE_INTERVAL-(time.time()-last_at))
    cold=not (p['long_term'].get('features') or p['settings']['languages'] or p['settings']['long_term_need'] or p['settings']['seed_track_keys'])
    return {'feed_id':row['feed_id'] if row else None,'tracks':tracks,'actual_count':len(tracks),'target_count':policy.TARGET,
        'profile_revision':row['profile_revision'] if row else p['revision'],'latest_profile_revision':p['revision'],
        'agent_revision':p['agent_revision'],'refreshing':refreshing,'can_change':retry<=0,
        'refill':refill,'validation_stale':validation_stale,
        'retry_after_seconds':round(retry,2),'reason':reason or ('cold_start' if cold and not tracks else 'refreshing' if refreshing else 'insufficient_candidates' if 0<len(tracks)<policy.TARGET else 'ready' if tracks else 'exhausted'),
        'saved_playlist_id':row['saved_playlist_id'] if row else None}


def allocate(cursor, uid, payload, key):
    digest=hashlib.sha256(runs.encode(payload.model_dump()).encode()).hexdigest()
    key_hash=hashlib.sha256(key.encode()).hexdigest()
    cursor.execute('SELECT * FROM music_discovery_feeds WHERE user_id=%s AND request_key=%s',(uid,key_hash))
    old=cursor.fetchone()
    if old:
        if old['request_hash']!=digest:
            raise HTTPException(409,'幂等键已用于不同参数')
        return old,False,'ready'
    current=latest(cursor,uid)
    if current and time.time()-current['created_at']<policy.CHANGE_INTERVAL:
        raise HTTPException(429,'请稍等片刻再换歌',headers={'Retry-After':str(policy.CHANGE_INTERVAL)})
    p,selected=choose(cursor,uid,payload,current)
    if not selected:
        return current,False,'exhausted'
    if len(selected)<policy.TARGET:
        return current,False,'insufficient_candidates'
    return publish(cursor,uid,p,payload,key,selected),True,'ready'


def choose(cursor, uid, payload, current, retained=()):
    p=profile.build(cursor,uid,payload.seed_track_keys)
    identities=identity.catalog(cursor,uid)
    intent=recall.baseline(payload.model_dump(),p)
    intent['context']=payload.context
    candidates=[]
    current_recordings=set()
    if current:
        cursor.execute('SELECT recording_key FROM music_discovery_feed_items WHERE feed_id=%s',(current['feed_id'],))
        current_recordings={identities.recording(r['recording_key']) for r in cursor.fetchall()}
    for track in pool.tracks(cursor,uid,replay=payload.replay,identities=identities):
        if track['_recording_group'] in current_recordings and not payload.replay:
            continue
        selection=ranking.score(track,p,intent)
        if selection:
            candidates.append(track|{'selection':selection|{'profile_revision':p['revision'],'agent_revision':p['agent_revision'],
                'verified_features':track.get('analysis',{}).get('features',{})}})
    cursor.execute('''SELECT JSON_UNQUOTE(JSON_EXTRACT(i.track_snapshot,'$.artist')) AS artist,SUM(i.exposure_count) AS n
        FROM music_discovery_feed_items i JOIN music_discovery_feeds f ON f.feed_id=i.feed_id
        WHERE f.user_id=%s AND i.last_exposed_at>%s GROUP BY artist''',(uid,time.time()-7*86400))
    exposure={r['artist']:int(r['n']) for r in cursor.fetchall()}
    exploration=payload.exploration if 'exploration' in payload.model_fields_set else p['settings']['exploration']
    selected=ranking.select(candidates,exploration={'low':.1,'balanced':.2,'high':.3}[exploration],
        familiar_artists=p['long_term'].get('features',{}).get('artist',{}),exposure=exposure,retained=retained)
    return p,selected


def publish(cursor, uid, p, payload, key, selected):
    # The user row is held through the complete deterministic selection and commit.
    cursor.execute('SELECT revision FROM music_discovery_profiles WHERE user_id=%s',(uid,))
    if cursor.fetchone()['revision']!=p['revision']:
        raise HTTPException(409,'偏好正在更新，请重试')
    now=time.time()
    row=dict(feed_id=secrets.token_hex(16),user_id=uid,recommendation_date=policy.today(),request_key=hashlib.sha256(key.encode()).hexdigest(),
        request_hash=hashlib.sha256(runs.encode(payload.model_dump()).encode()).hexdigest(),
        profile_revision=p['revision'],policy_version=policy.VERSION,generation_seed=secrets.token_hex(32),
        target_count=policy.TARGET,actual_count=len(selected),saved_playlist_id=None,created_at=now)
    cursor.execute('INSERT INTO music_discovery_feeds ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
    for position,track in enumerate(selected,1):
        cursor.execute('''INSERT INTO music_discovery_feed_items
            (feed_id,position,track_key,recording_key,work_key,track_snapshot,selection,validated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',(row['feed_id'],position,track['key'],track['recording_key'],track['work_key'],
            runs.encode(pool.snapshot(track)),runs.encode(track['selection']),track['validation']['checked_at']))
    return row


def prepare():
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid)
        store.cleanup(cursor,uid)
        pool.bootstrap(cursor,uid)
        p=store.profile(cursor,uid)
        cursor.execute('UPDATE music_discovery_profiles SET updated_at=%s WHERE user_id=%s',(time.time(),uid))
        payload=models.NextFeed(exploration=p['settings']['exploration'])
        row=ensure_current(cursor,uid,payload)
        pool.schedule(cursor,uid,payload)
        from . import profile_store
        profile_store.cleanup(cursor,uid)
        profile_store.schedule(cursor,uid,'active')
        result=public(cursor,uid,row)
    from . import profile_runtime
    profile_runtime.wake()
    return result


def next_feed(payload, key):
    uid=accounts.current().id
    with library.database() as cursor:
        usage.lock(cursor,uid)
        store.cleanup(cursor,uid)
        pool.trim(cursor,uid)
        row,created,reason=allocate(cursor,uid,payload,key)
        cursor.execute('UPDATE music_discovery_profiles SET updated_at=%s WHERE user_id=%s',(time.time(),uid))
        pool.schedule(cursor,uid,payload)
        result=public(cursor,uid,row if created or reason=='ready' else current(cursor,uid),reason if reason!='ready' else None)
        if reason=='exhausted' and result['refreshing']:
            result['reason']='refreshing'
    from . import profile_runtime
    profile_runtime.wake()
    return result


def save(cursor, uid, feed_id, name=None):
    row=get(cursor,uid,feed_id)
    if row['saved_playlist_id']:
        library.require_playlist(cursor,row['saved_playlist_id'])
        return {'playlist_id':row['saved_playlist_id'],'already_saved':True}
    tracks=public(cursor,uid,row)['tracks']
    if not tracks:
        raise HTTPException(422,'当前没有可保存歌曲')
    key=secrets.token_hex(12);now=time.time()
    cursor.execute('INSERT INTO music_playlists (id,user_id,name,description,created_at) VALUES (%s,%s,%s,%s,%s)',
        (key,uid,name or '今日推荐','根据音乐偏好发现的新声音',now))
    for i,track in enumerate(tracks):
        track_key=library.save_track(cursor,library.TrackInput.model_validate(track))
        cursor.execute('INSERT INTO music_playlist_tracks (playlist_id,track_id,added_at,position) VALUES (%s,%s,%s,%s)',(key,track_key,now,i))
    cursor.execute('UPDATE music_discovery_feeds SET saved_playlist_id=%s WHERE feed_id=%s',(key,feed_id))
    return {'playlist_id':key,'already_saved':False}
