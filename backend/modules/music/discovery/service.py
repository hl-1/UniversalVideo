import time
from functools import partial
from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music import library, sources
from backend.modules.music.agent import budget, features, validation, platforms, work, model, store as runs, service as agent
from backend.infrastructure import network
from . import store, profile, ranking, recall, identity, policy, pool


def require_enabled():
    config=accounts.settings()
    if not config['music_enabled'] or not config['music_daily_enabled']:
        raise HTTPException(403,'今日推荐已由管理员关闭')
    return config


def optional_model(run_id, operation, fallback):
    from backend.modules.music.agent.faults import ModelFault
    current=agent.check_active(run_id)
    if current['context'].get('model_stop') or not accounts.settings().get('ai_api_key'):
        return fallback
    try:
        return operation()
    except (ModelFault, budget.BudgetExceeded) as exc:
        if isinstance(exc,budget.BudgetExceeded) and exc.resource not in {'tokens','model_calls','decisions'}:
            raise
        with library.database() as cursor:
            current=runs.get(cursor,run_id,lock=True)
            runs.update(cursor,current,context=current['context']|{'model_stop':current['context'].get('model_stop') or str(exc),
                'enhancement_status':'unavailable'})
        agent.check_active(run_id)
        return fallback


def execute(row):
    run_id,uid=row['run_id'],row['user_id']
    if row['skill_version']!=recall.skill()['version']:
        raise RuntimeError('今日推荐 Skill 版本变化，请创建新批次')
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        p=profile.build(cursor,uid,row['request'].get('seed_track_keys',[]))
        cursor.execute('SELECT * FROM music_discovery_batches WHERE run_id=%s',(run_id,))
        batch=cursor.fetchone() or {'batch_id':None,'batch_no':1+int(row['created_at'])%3}
        cursor.execute('SELECT t.* FROM music_tracks t WHERE t.user_id=%s AND (EXISTS(SELECT 1 FROM music_favorites f WHERE f.track_id=t.id) OR EXISTS(SELECT 1 FROM music_history h WHERE h.track_id=t.id)) LIMIT 2000',(uid,))
        for known in cursor.fetchall(): identity.ensure(cursor,uid,known)
        # Current batch seed choices do not become long-term settings.
        for key in row['request']['seed_track_keys']:
            cursor.execute('SELECT * FROM music_tracks WHERE user_id=%s AND id=%s',(uid,key))
            seed=cursor.fetchone()
            if seed and not any(s['id']==key for s in p['seeds']):
                p['seeds'].append(seed|{'features':{}})
        row=runs.update(cursor,row,context=row['context']|{'profile':{k:v for k,v in p.items() if k!='evidence'},'batch_id':batch['batch_id']})
    # Seed analysis is bounded; do not initialize the whole library by downloading it.
    charge=partial(budget.charge,run_id)
    def seed_guard():
        agent.check_active(run_id)
        if not require_enabled()['music_agent_network_enabled']:
            raise HTTPException(403,'已关闭网络检索')
        budget.charge(run_id,network_requests=1,reserve_playback=True)
    seed_token=network.request_guard.set(seed_guard)
    try:
        with work.operation(row,lambda:agent.check_active(run_id)):
            for seed in p['seeds'][:3]:
                if not seed.get('features'):
                    seed['key']=seed['id'];seed['user_id']=uid
                    analyzed=features.analyze(run_id,seed,charge,False,
                        need_audio=False,check=lambda:agent.check_active(run_id))
                    seed['features']=analyzed['features']
    finally:
        network.request_guard.reset(seed_token)
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        updated=profile.build(cursor,uid,row['request'].get('seed_track_keys',[]))
        p=p|{'long_term':updated['long_term'],'short_term':updated['short_term']}
    with library.database() as cursor:
        cursor.execute("""SELECT context FROM music_recommendation_runs WHERE user_id=%s AND run_id<>%s
            AND deleted=FALSE AND JSON_UNQUOTE(JSON_EXTRACT(request,'$.kind'))='daily_discovery'
            ORDER BY created_at DESC LIMIT 5""",(uid,run_id))
        recent_routes=[route for previous in cursor.fetchall() for route in store.decoded(previous['context']).get('routes',[])]
    plan=recall.baseline(row['request'],p)
    baseline_plan=dict(plan)
    low_yield={r['query'] for r in recent_routes if r.get('new_candidates',0)==0}
    plan['routes'].sort(key=lambda r:r['query'] in low_yield)
    charge=partial(budget.charge,run_id)
    providers=[s for s in row['context']['limits']['sources'] if s in require_enabled()['music_agent_sources']]
    if not providers:
        raise HTTPException(422,'没有启用的音乐来源')
    reserve=True
    def check():
        return agent.check_active(run_id)
    def guard():
        current=check()
        config=require_enabled()
        if not config['music_agent_network_enabled']:
            raise HTTPException(403,'已关闭网络检索')
        budget.charge(run_id,network_requests=1,reserve_playback=reserve)
    token=network.request_guard.set(guard)
    try:
        with work.operation(row,check):
            routes=list(plan['routes'])
            queries={identity.text(route['query']) for route in routes}
            for route in recall.artist_routes(p):
                if identity.text(route['query']) not in queries:
                    routes.append(route)
                    queries.add(identity.text(route['query']))
            saved_pending=row['context'].get('recall_pending')
            searched=list(row['context'].get('routes',[])) if saved_pending is not None else []
            routes=[route for route in routes if identity.text(route['query']) not in
                {identity.text(searched_route['query']) for searched_route in searched}]
            qualified=list(row['context'].get('candidates',[])) if saved_pending is not None else []
            pending=[(item['track'],item['route']) for item in saved_pending or []]
            processed={track['key'] for track,_ in pending}|{track['key'] for track in qualified}
            adjusted=False
            refilling=bool(row['context'].get('refill'))
            target=policy.REFILL_TARGET if refilling else policy.TARGET
            available=[]
            with library.database() as cursor:
                if refilling:
                    for track in pool.tracks(cursor,uid):
                        selection=ranking.score(track,p,plan)
                        if selection: available.append(track|{'selection':selection})
                expired=pool.tracks(cursor,uid,expired=True)
            capacity=ranking.selectable_count(available)
            def needed(track):
                # Do not spend network budget on an artist surplus that cannot
                # improve the reserves under the same rules used by feed allocation.
                proposed=track|{'selection':track.get('selection') or {'match_score':policy.MIN_SCORE}}
                return ranking.selectable_count(available+[proposed])>capacity
            def remember(track):
                nonlocal capacity
                available.append(track)
                capacity=ranking.selectable_count(available)
            def artist_count(name):
                members=identity.artist_members(name)
                return max((sum(member in identity.artist_members(track['artist']) for track in available)
                    for member in members),default=0)
            def route_priority(route):
                query=identity.text(route['query'])
                recent=[r for r in recent_routes if identity.text(r['query'])==query]
                return (artist_count(route['query']),len(recent),route['query'] in low_yield)
            for track in expired[:30]:
                if track['key'] in processed: continue
                selection=ranking.score(track,p,plan)
                if selection:
                    pending.append((track|{'_cached_selection':selection},
                        {'query':track.get('source',{}).get('label') or track['artist'],'type':'artist_expansion'}))
            enhanced=0
            while routes and len(searched)<row['context']['limits']['search_rounds']:
                check()
                routes.sort(key=route_priority)
                route=routes.pop(0)
                provider=providers[len(searched)%len(providers)]
                page=1+(batch['batch_no']-1)%3
                reserve=True
                agent.progress(run_id,'search','正在检索陌生歌曲')
                if route.get('type')=='related_playlist' and provider in {'qq','netease'}:
                    lists=platforms.search_playlists(provider,route['query'],page,charge)['playlists'][:2]
                    found=[]
                    for playlist in lists:
                        found.extend(platforms.playlist_tracks(provider,playlist['playlist_id'],(batch['batch_no']-1)*20,20,charge)['tracks'])
                else:
                    charge(search_rounds=1)
                    found=[t.model_dump() for t in sources._search(provider,route['query'],page)]
                added=[]
                with library.database() as cursor:
                    store.lock_user(cursor,uid)
                    # Reserve candidate slots for the other routes as well.
                    route_limit=max(1,row['context']['limits']['candidates']//max(1,row['context']['limits']['search_rounds']))
                    for candidate in found[:route_limit]:
                        ident=identity.ensure(cursor,uid,candidate)
                        if ident['track_key'] in processed or len(processed)>=row['context']['limits']['candidates']:
                            continue
                        processed.add(ident['track_key'])
                        if store.excluded(cursor,uid,ident,batch['batch_id']):
                            continue
                        added.append(candidate|{'key':ident['track_key'],'user_id':uid,
                            'recording_key':ident['recording_key'],'work_key':ident['work_key'],
                            'source':{'kind':'online_search','label':route['query'],'provider':provider}})
                    current=runs.get(cursor,run_id,lock=True)
                    searched.append(route|{'provider':provider,'page':page,'new_candidates':len(added)})
                    pending.extend((track,route) for track in added)
                    runs.update(cursor,current,context=current['context']|{'routes':searched,
                        'recall_pending':[{'track':track,'route':source} for track,source in pending]})
                if not routes and not adjusted and len(searched)<row['context']['limits']['search_rounds']:
                    adjusted=True
                    plan=optional_model(run_id,lambda:recall.plan(run_id,row['request'],p,searched),plan)
                    if baseline_plan['languages']: plan['languages']=baseline_plan['languages']
                    if baseline_plan['vocals']=='instrumental': plan['vocals']='instrumental'
                    plan['excluded_artists']=list(dict.fromkeys(plan.get('excluded_artists',[])+baseline_plan['excluded_artists']))
                    routes=[r for r in plan['routes'] if r['query'] not in {s['query'] for s in searched}]
            # Search the bounded route portfolio before spending most of the
            # network budget on lyrics/playback. Rotate analysis between artists.
            attempts={}
            while pending and len(qualified)<target:
                def candidate_priority(item):
                    members=identity.artist_members(item[0]['artist'])
                    return (artist_count(item[0]['artist']),max((attempts.get(name,0) for name in members),default=0))
                pending.sort(key=candidate_priority)
                track,route=pending.pop(0)
                check()
                if not needed(track): continue
                with library.database() as cursor:
                    current=runs.get(cursor,run_id,lock=True)
                    runs.update(cursor,current,context=current['context']|{
                        'recall_pending':[{'track':candidate,'route':source} for candidate,source in [(track,route)]+pending]})
                for name in identity.artist_members(track['artist']): attempts[name]=attempts.get(name,0)+1
                agent.progress(run_id,'analysis','正在分析歌曲特征',track['title'])
                cached=track.pop('_cached_selection',None)
                if not cached:
                    try:
                        features.prefetch_lyrics(track,charge,check)
                    except HTTPException as exc:
                        # Lyrics are optional evidence. A busy lyric cache/service
                        # must not strand all the other artists in waiting_auth.
                        if getattr(exc,'agent_policy_error',False) or exc.status_code not in {408,422,429,500,502,503,504}:
                            raise
                    track['analysis']=features.analyze(run_id,track,charge,False,need_audio=False,check=check)
                if not cached and adjusted and enhanced<5:
                    enhanced+=1
                    track['analysis']=optional_model(run_id,lambda:features.analyze(run_id,track,charge,True,
                        need_audio=plan.get('desired_energy') is not None,check=check),track['analysis'])
                selection=cached or ranking.score(track,p,plan)
                if not selection: continue
                reserve=False
                agent.progress(run_id,'validation','正在校验完整音频',track['title'])
                track['validation']=validation.validate(track,charge,True,check=check)
                reserve=True
                if not all(track['validation'].get(k) for k in ('identity','playable','full')): continue
                track['selection']=selection|{'route':route,'policy_version':policy.VERSION}
                qualified.append(track)
                remember(track)
                with library.database() as cursor:
                    store.lock_user(cursor,uid)
                    current=runs.get(cursor,run_id,lock=True)
                    pool.put(cursor,uid,track,run_id)
                    pool.trim(cursor,uid)
                    runs.update(cursor,current,context=current['context']|{'candidates':qualified,'intent':plan})
    finally:
        network.request_guard.reset(token)
    # The baseline is already stored. Spend tokens on verified lyrics, not inventing songs.
    token=network.request_guard.set(guard)
    try:
        with work.operation(row,check):
            for seed in p['seeds'][:3]:
                seed.update(key=seed['id'],user_id=uid)
                optional_model(run_id,lambda:features.analyze(run_id,seed,charge,True,check=check),None)
            for track in qualified[:5]:
                track['analysis']=optional_model(run_id,lambda:features.analyze(run_id,track,charge,True,check=check),track['analysis'])
    finally:
        network.request_guard.reset(token)
    with library.database() as cursor:
        current=runs.get(cursor,run_id,lock=True)
        runs.update(cursor,current,context=current['context']|{'candidates':qualified,'intent':plan,'recall_pending':[]})
    advice=optional_model(run_id,lambda:recall.advise(run_id,qualified),{})
    if advice:
        with library.database() as cursor:
            current=runs.get(cursor,run_id,lock=True)
            candidates=[t|{'selection':t['selection']|{'agent_order_bonus':advice.get(t['key'],0)}} for t in current['context'].get('candidates',[])]
            runs.update(cursor,current,context=current['context']|{'candidates':candidates})
    finish(run_id)


def finish(run_id):
    uid=accounts.current().id
    with library.database() as cursor:
        store.lock_user(cursor,uid)
        row=runs.get(cursor,run_id,uid,lock=True)
        if row['status']!='running' or not row['lease_until'] or row['lease_until']<=time.time():
            return
        cursor.execute('SELECT * FROM music_discovery_batches WHERE run_id=%s',(run_id,))
        batch=cursor.fetchone()
        for track in row['context'].get('candidates',[]):
            pool.put(cursor,uid,track,run_id)
        pool.trim(cursor,uid)
        if not batch:
            from . import feeds
            from .models import NextFeed
            payload=NextFeed.model_validate({k:v for k,v in row['request'].items() if k in NextFeed.model_fields})
            feeds.ensure_current(cursor,uid,payload)
            count=len(row['context'].get('candidates',[]))
            runs.update(cursor,row,status='finished' if count else 'partial',result={'title':'候选补充','tracks':[],'actual_count':count},
                result_version=row['result_version']+1,worker_id=None,lease_until=None,
                context=row['context']|{'candidates':[],'message':f'已补充 {count} 首候选'})
            return
        # Rebuild under the same user lock as feedback, then re-filter before publishing.
        p=profile.build(cursor,uid,row['request'].get('seed_track_keys',[]))
        candidates=[]
        for t in row['context'].get('candidates',[]):
            ident={'track_key':t['key'],'recording_key':t['recording_key'],'work_key':t['work_key']}
            if store.excluded(cursor,uid,ident,batch['batch_id']): continue
            selection=ranking.score(t,p,row['context'].get('intent',{}))
            if selection: candidates.append(t|{'selection':t['selection']|selection})
        cursor.execute("""SELECT JSON_UNQUOTE(JSON_EXTRACT(i.track_snapshot,'$.artist')) AS artist,SUM(i.exposure_count) AS n
            FROM music_discovery_items i JOIN music_discovery_batches b ON b.batch_id=i.batch_id
            WHERE b.user_id=%s AND i.last_exposed_at>%s GROUP BY artist""",(uid,time.time()-14*86400))
        exposure={r['artist']:int(r['n']) for r in cursor.fetchall()}
        selected=ranking.select(candidates,exploration={'low':.1,'balanced':.2,'high':.3}[row['request']['exploration']],
            familiar_artists=p['long_term'].get('features',{}).get('artist',{}),exposure=exposure)
        tracks=[];now=time.time()
        for position,t in enumerate(selected,1):
            discovery={k:t['selection'][k] for k in ('match_score','reason','evidence_refs')}
            discovery.update(batch_id=batch['batch_id'],position=position,recording_key=t['recording_key'],work_key=t['work_key'],
                             exploration=t['selection'].get('exploration',False))
            public={k:v for k,v in t.items() if k not in {'user_id','selection','recording_key','work_key'}}|{'discovery':discovery}
            cursor.execute('''INSERT INTO music_discovery_items
                (batch_id,position,track_key,recording_key,work_key,track_snapshot,selection,validated_at,published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(batch['batch_id'],position,t['key'],t['recording_key'],t['work_key'],
                runs.encode(public),runs.encode(t['selection']),t['validation']['checked_at'],now))
            tracks.append(public)
        cursor.execute('UPDATE music_discovery_batches SET profile_revision=%s WHERE batch_id=%s',(p['revision'],batch['batch_id']))
        gap='' if len(tracks)==policy.TARGET else row['context'].get('stop_reason') or '其余候选重复、匹配证据不足或未通过完整播放校验'
        result={'title':'今日推荐','tracks':tracks,'gap_reason':gap,'budget':row['context'].get('budget',{})}
        runs.update(cursor,row,status='finished' if len(tracks)==policy.TARGET else 'partial' if tracks else 'failed',
            result=result,result_version=row['result_version']+1,worker_id=None,lease_until=None,
            context=row['context']|{'candidates':[],'message':gap or '今日推荐已生成'})
