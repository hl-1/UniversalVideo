"""Rule evidence uses immutable observation times and recording/day caps."""
import math
import time
from collections import defaultdict
from . import store, policy
from backend.modules.music.agent.store import encode


def completed(event):
    duration=event.get('duration_ms')
    return bool(duration and event['covered_ms']>=duration*.8 and event['listened_ms']>=min(60000,duration*.8)
                and event['terminal_reason']!='error')


def trusted_features(features, evidence):
    result={}
    for field in ('language','vocal_role','moods','themes','tempo','energy','style','instrument'):
        value=features.get(field)
        field_evidence=evidence.get('fields',{}).get(field,{})
        if value is None or value in ('','unknown') or not field_evidence.get('confidence',0):
            continue
        if field in {'tempo','energy'} and field_evidence.get('coverage')!='full':
            continue
        result[field]=value
    return result


def build(cursor, uid, extra_seeds=()):
    saved=store.profile(cursor,uid)
    now=time.time()
    seeds=list(dict.fromkeys(saved['settings']['seed_track_keys']+list(extra_seeds)))
    seed_clause='t.id IN ('+','.join(['%s']*len(seeds))+')' if seeds else 'FALSE'
    cursor.execute('''SELECT t.*, f.track_id AS favorite, f.created_at AS favorite_at FROM music_tracks t
        LEFT JOIN music_favorites f ON f.track_id=t.id WHERE t.user_id=%s AND (f.track_id IS NOT NULL OR
        EXISTS(SELECT 1 FROM music_discovery_events e WHERE e.user_id=t.user_id AND e.track_key=t.id AND e.received_at>%s)
        OR '''+seed_clause+') ORDER BY (f.track_id IS NOT NULL) DESC,t.id LIMIT 2001',(uid,now-180*86400,*seeds))
    fetched=cursor.fetchall()
    tracks={t['id']:t for t in fetched[:2000]}
    cursor.execute('SELECT track_key,features,evidence,checked_at FROM music_track_features WHERE user_id=%s AND expires_at>%s',(uid,now))
    feature_rows=cursor.fetchall()
    cache={r['track_key']:trusted_features(store.decoded(r['features']),store.decoded(r['evidence'])) for r in feature_rows}
    quality={r['track_key']:store.decoded(r['evidence']) for r in feature_rows}
    cursor.execute('''SELECT * FROM music_discovery_events WHERE user_id=%s AND received_at>%s
        AND event_type IN ('playback_end','playlist_add') ORDER BY received_at DESC,event_id DESC LIMIT 20001''',(uid,now-180*86400))
    fetched_events=cursor.fetchall(); events=list(reversed(fetched_events[:20000]))
    cursor.execute('SELECT track_key,recording_key,canonical FROM music_discovery_identities WHERE user_id=%s',(uid,))
    identities={r['track_key']:r for r in cursor.fetchall()}
    for key,ident in identities.items():
        if key not in tracks:
            tracks[key]=store.decoded(ident['canonical'])|{'id':key,'favorite':None}
    evidence=[]; daily=defaultdict(float); seen_sessions=set()
    for event in events:
        payload=store.decoded(event['payload'])
        if event['event_type']=='playlist_add':
            weight=3
        else:
            if event['origin']!='web' or event['playback_id'] in seen_sessions:
                continue
            seen_sessions.add(event['playback_id'])
            weight=.5 if completed(event) else -.2 if event['terminal_reason']=='user_next' and event['listened_ms']<15000 else 0
            if payload.get('replay') and payload.get('active') and event['terminal_reason']!='error':
                weight+=2
            day=(identities.get(event['track_key'],{}).get('recording_key',event['track_key']),str(policy.today(event['received_at'])))
            direction='positive' if weight>0 else 'negative'; cap=2.5 if weight>0 else .4
            amount=min(abs(weight),max(0,cap-daily[(day,direction)]))
            weight=amount if weight>0 else -amount
            daily[(day,direction)]+=amount
        if weight:
            evidence.append({'track_key':event['track_key'],'weight':weight,'at':event['received_at'],
                'ref':'event:'+event['event_id'],'context':payload.get('context','general'),'explicit':event['event_type']=='playlist_add'})
    for key,track in tracks.items():
        if track.get('favorite') or key in seeds:
            favorite=bool(track.get('favorite'))
            evidence.append({'track_key':key,'weight':5,'at':track['favorite_at'] if favorite else saved['created_at'],
                'ref':('favorite:' if favorite else 'seed:')+key,'context':'general','explicit':True})
    def aggregate(days,half):
        totals=defaultdict(lambda:defaultdict(float)); support=defaultdict(set); refs=[]; explicit=set()
        for item in evidence:
            key,weight,at,ref=item['track_key'],item['weight'],item['at'],item['ref']
            if days==7 and (ref.startswith('seed:') or at<now-7*86400):
                continue
            if key not in tracks or at<now-days*86400 and not ref.startswith(('favorite:','seed:')):
                continue
            w=weight*math.pow(.5,max(0,now-at)/(half*86400))
            if item['explicit']: explicit.add(key)
            values={'artist':tracks[key]['artist']}|cache.get(key,{})
            for field,value in values.items():
                if value is None or value in ('','unknown'):
                    continue
                for value_item in value if isinstance(value,list) else [value]:
                    totals[field][str(value_item)]+=w
                    support[field].add(identities.get(key,{}).get('recording_key',key))
            if weight>0: refs.append(ref)
        return {'features':{f:dict(v) for f,v in totals.items() if len(support[f])>=3 or explicit},
            'support':{f:len(v) for f,v in support.items()},'evidence_refs':refs[-100:],'window_days':days}
    long,short=aggregate(180,60),aggregate(7,3)
    if not extra_seeds:
        cursor.execute('''UPDATE music_discovery_profiles SET long_term=%s,short_term=%s,built_revision=%s
            WHERE user_id=%s AND revision=%s''',(encode(long),encode(short),saved['revision'],uid,saved['revision']))
    evidence_data=[]
    for item in evidence:
        key=item['track_key']
        if key not in tracks: continue
        values={'artist':tracks[key]['artist']}|cache.get(key,{})
        evidence_data.append(item|{'recording_key':identities.get(key,{}).get('recording_key',key),'values':values,
            'track':{'title':tracks[key]['title'],'artist':tracks[key]['artist']},
            'quality':{f:quality.get(key,{}).get('fields',{}).get(f,{}).get('confidence',.9 if f=='artist' else 0) for f in values}})
    evidence_data.sort(key=lambda e:e['at'])
    result=saved|{'long_term':long,'short_term':short,'seeds':[tracks[k]|{'features':cache.get(k,{})} for k in tracks
        if tracks[k].get('favorite') or k in seeds][:50],
        'evidence':evidence_data,'coverage':{'complete':len(fetched)<=2000 and len(fetched_events)<=20000,
            'track_limit':2000,'event_limit':20000,'included_events':len(events)}}
    from backend.modules.accounts import service as accounts
    result['agent_fusion_enabled']=accounts.settings().get('music_profile_fusion_enabled',True)
    return result
