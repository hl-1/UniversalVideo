"""Frozen evidence, deterministic validation and atomic profile publication."""
import hashlib
import time
from contextvars import ContextVar
from datetime import datetime

from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music.agent.store import encode
from . import models, policy, profile, store

ACTIVE=('pending','running','usage_pending')
execution_lease=ContextVar('music_profile_execution_lease',default=None)


def key(item):
    return (item['dimension'],item['value'],item['scope'],item.get('context','general'))


def decode_run(row):
    if row:
        for field in ('input_snapshot','proposal','context','steps'):
            row[field]=store.decoded(row[field])
    return row


def get_run(cursor, uid, run_id, lock=False):
    cursor.execute('SELECT * FROM music_discovery_profile_runs WHERE user_id=%s AND run_id=%s'+(' FOR UPDATE' if lock else ''),(uid,run_id))
    row=decode_run(cursor.fetchone())
    if not row: raise HTTPException(404,'画像任务不存在')
    return row


def update_run(cursor,row,**changes):
    merged=row|changes
    if len(encode({k:merged.get(k) for k in ('input_snapshot','proposal','context','steps')}).encode())>512*1024:
        raise HTTPException(413,'画像任务快照超过 512 KiB')
    fields=list(changes)
    values=[encode(v) if k in ('context','steps','proposal') and v is not None else v for k,v in changes.items()]
    cursor.execute('UPDATE music_discovery_profile_runs SET '+','.join(k+'=%s' for k in fields)+
        ',updated_at=%s,revision=revision+1 WHERE run_id=%s AND revision=%s',(*values,time.time(),row['run_id'],row['revision']))
    if cursor.rowcount!=1: raise HTTPException(409,'画像任务已变化')
    return row|changes|{'revision':row['revision']+1}


def freeze(cursor,uid):
    p=profile.build(cursor,uid)
    evidence=[e for e in p['evidence'] if e['at']>=(p['learning_since'] or 0)]
    snapshot={'schema_version':1,'settings':p['settings'],'base_state':p['agent_state'],
        'evidence':evidence[-1000:],'coverage':p['coverage']|{'total_evidence':len(evidence),'included_evidence':min(1000,len(evidence))},
        'frozen_at':time.time(),'learning_since':p['learning_since']}
    while len(encode(snapshot).encode())>450*1024 and snapshot['evidence']:
        snapshot['evidence']=snapshot['evidence'][max(1,len(snapshot['evidence'])//10):]
    snapshot['coverage']['included_evidence']=len(snapshot['evidence'])
    snapshot['coverage']['complete']=snapshot['coverage']['complete'] and len(evidence)==len(snapshot['evidence'])
    return p,snapshot


def schedule(cursor,uid,trigger='evidence',force=False):
    config=accounts.settings()
    if not config.get('music_profile_agent_enabled',True) or not config['music_enabled']:
        return None
    p,snapshot=freeze(cursor,uid)
    if not p['settings']['profile_learning_enabled']:
        return None
    cursor.execute("SELECT * FROM music_discovery_profile_runs WHERE user_id=%s AND status IN ('pending','running','usage_pending') LIMIT 1",(uid,))
    active=decode_run(cursor.fetchone())
    if active: return active
    cursor.execute('SELECT input_snapshot,created_at FROM music_discovery_profile_runs WHERE user_id=%s ORDER BY created_at DESC LIMIT 1',(uid,))
    previous=cursor.fetchone()
    reviewed=set()
    if previous:
        cursor.execute("SELECT input_snapshot FROM music_discovery_profile_runs WHERE user_id=%s AND status IN ('succeeded','no_change') ORDER BY created_at DESC LIMIT 1",(uid,))
        evaluated=cursor.fetchone()
        if evaluated:
            reviewed={e['ref'] for e in store.decoded(evaluated['input_snapshot']).get('evidence',[])}
    fresh=[e for e in snapshot['evidence'] if e['ref'] not in reviewed]
    recordings={e['recording_key'] for e in fresh}
    cold=not p['agent_revision'] and any(e['explicit'] for e in fresh)
    if not force and not cold and (len(fresh)<10 or len(recordings)<3):
        cursor.execute('UPDATE music_discovery_profiles SET next_review_at=NULL WHERE user_id=%s',(uid,))
        return None
    if not snapshot['evidence']:
        return None
    # Stable input hashes exclude wall clock, so repeated manual requests reuse work.
    from .profile_agent import skill
    skill_data=skill()
    digest=hashlib.sha256(encode([uid,p['evidence_revision'],p['control_revision'],p['agent_revision'],
        policy.PROFILE_VERSION,skill_data['version'],snapshot['evidence'],snapshot['settings']]).encode()).hexdigest()
    cursor.execute('SELECT * FROM music_discovery_profile_runs WHERE user_id=%s AND input_hash=%s',(uid,digest))
    existing=decode_run(cursor.fetchone())
    if existing: return existing
    import secrets
    now=time.time()
    earliest=(previous['created_at']+config.get('music_profile_interval',1800)) if previous else now
    row=dict(run_id=secrets.token_hex(16),user_id=uid,status='pending',trigger_type=trigger,
        source_revision=p['evidence_revision'],base_control_revision=p['control_revision'],base_agent_revision=p['agent_revision'],
        input_hash=digest,policy_version=policy.PROFILE_VERSION,skill_version=skill_data['version'],input_snapshot=snapshot,
        proposal=None,context={'model_requests':[],'not_before':max(now,earliest)},steps=[],created_at=now,updated_at=now)
    cursor.execute('INSERT INTO music_discovery_profile_runs ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',
        tuple(encode(v) if k in ('input_snapshot','context','steps') else v for k,v in row.items()))
    cursor.execute('UPDATE music_discovery_profiles SET next_review_at=%s WHERE user_id=%s',(max(now,earliest),uid))
    return get_run(cursor,uid,row['run_id'])


def validate(proposal,snapshot):
    proposal=models.Proposal.model_validate(proposal).model_dump()
    now=time.time(); available={e['ref']:e for e in snapshot['evidence']}
    previous={key(i):i for i in snapshot.get('base_state',{}).get('items',[])}
    items=[];hypotheses=[];seen=set()
    for item in proposal['items']:
        identity=key(item)
        if identity in seen: raise ValueError('画像条目重复')
        seen.add(identity)
        refs=list(dict.fromkeys(item['evidence_refs']))
        if not refs or any(ref not in available for ref in refs):
            raise ValueError('画像引用了快照之外的证据')
        evidence=[available[ref] for ref in refs]
        field=item['dimension'];value=item['value']
        for e in evidence:
            values=e['values'].get(field)
            if values is None or e['quality'].get(field,0)<=0:
                raise ValueError('维度缺少可信证据')
            supported=[str(v) for v in values] if isinstance(values,list) else [str(values)]
            if field not in {'tempo','energy'} and value not in supported:
                raise ValueError('证据不支持该维度值')
            if item['scope']=='context' and e['context']!=item['context']:
                raise ValueError('场景必须来自用户明确选择')
            if item['scope']=='recent' and e['at']<now-7*86400:
                raise ValueError('近期画像不能引用旧兴趣')
        if field in {'tempo','energy'}:
            numbers=[float(e['values'][field]) for e in evidence]
            if not min(numbers)<=float(value)<=max(numbers):
                raise ValueError('数值推断超出音频证据范围')
        direction=1 if item['preference']>=0 else -1
        positive=sum(abs(e['weight']) for e in evidence if e['weight']*direction>0)
        total=sum(abs(e['weight']) for e in evidence)
        agreement=positive/max(total,.001)
        if agreement<.6: raise ValueError('证据与偏好方向矛盾')
        recordings=len({e['recording_key'] for e in evidence})
        days=len({policy.today(e['at']) for e in evidence})
        quality=sum(e['quality'][field] for e in evidence)/len(evidence)
        confidence=min(.98,.3+.08*min(recordings,6)+.045*min(days,5))*quality*agreement
        old=previous.get(identity,{}).get('preference',0)
        item.update(preference=round(max(old-.25,min(old+.25,item['preference'])),4),confidence=round(confidence,4),
            source='inferred',support={'recordings':recordings,'days':days},evidence_refs=refs,
            observed_at=datetime.fromtimestamp(max(e['at'] for e in evidence),policy.ZONE).isoformat(),
            half_life_days=3 if item['scope']=='recent' else 60,expires_at=now+7*86400 if item['scope']=='recent' else None)
        if confidence<.6 or item['scope']=='long_term' and (recordings<3 or days<2):
            hypotheses.append(item)
        else: items.append(item)
    state={'schema_version':1,'policy_version':policy.PROFILE_VERSION,'summary':proposal['summary'],
        'items':items[:120],'hypotheses':hypotheses[:30]}
    if len(encode(state).encode())>128*1024: raise ValueError('画像超过 128 KiB')
    return state


def publish(cursor,uid,state,source,p=None,run=None,summary=None):
    p=p or store.profile(cursor,uid)
    user_items=p['settings'].get('confirmed_profile_items',[])
    user_keys={key(i) for i in user_items}
    state=state|{'items':[i for i in state.get('items',[]) if key(i) not in user_keys]+user_items}
    if len(state['items'])>120 or len(encode(state).encode())>128*1024:
        raise HTTPException(422,'画像条目或大小超限')
    version=p['agent_revision']+1;now=time.time()
    changes={'previous_items':len(p['agent_state'].get('items',[])),'items':len(state['items']),
        'hypotheses':len(state.get('hypotheses',[]))}
    evidence_summary={'coverage':run['input_snapshot']['coverage'] if run else {},
        'references':list(dict.fromkeys(r for i in state['items']+state.get('hypotheses',[]) for r in i.get('evidence_refs',[])))[:200]}
    evidence_summary['items']=[e for e in run['input_snapshot']['evidence'] if e['ref'] in evidence_summary['references']][:200] if run else []
    if not run and evidence_summary['references']:
        missing=set(evidence_summary['references'])
        cursor.execute('SELECT evidence_summary FROM music_discovery_profile_versions WHERE user_id=%s ORDER BY version DESC LIMIT 50',(uid,))
        for version_row in cursor.fetchall():
            for evidence in store.decoded(version_row['evidence_summary']).get('items',[]):
                if evidence['ref'] in missing:
                    evidence_summary['items'].append(evidence)
                    missing.remove(evidence['ref'])
    cursor.execute('''INSERT INTO music_discovery_profile_versions
        (user_id,version,base_version,source,run_id,evidence_revision,control_revision,policy_version,
        snapshot,changes,evidence_summary,summary,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (uid,version,p['agent_revision'],source,run['run_id'] if run else None,p['evidence_revision'],p['control_revision'],
         policy.PROFILE_VERSION,encode(state),encode(changes),encode(evidence_summary),(summary or state.get('summary',''))[:500],now))
    cursor.execute('''UPDATE music_discovery_profiles SET agent_revision=%s,agent_state=%s,agent_updated_at=%s,
        agent_source_revision=%s,updated_at=%s WHERE user_id=%s''',
        (version,encode(state),now,run['source_revision'] if run else p['agent_source_revision'],now,uid))
    return version


def apply(cursor,uid,run_id,worker):
    p=store.profile(cursor,uid);run=get_run(cursor,uid,run_id,True)
    cursor.execute('SELECT status FROM users WHERE id=%s',(uid,));user=cursor.fetchone()
    if run['status']!='running' or run['worker_id']!=worker or not run['lease_until'] or run['lease_until']<=time.time():
        raise HTTPException(409,'画像任务租约已失效')
    if not user or user['status']!='active' or not p['settings']['profile_learning_enabled'] or not accounts.settings().get('music_profile_agent_enabled'):
        update_run(cursor,run,status='cancelled',finished_at=time.time(),worker_id=None,lease_until=None)
        return
    if p['control_revision']!=run['base_control_revision'] or p['agent_revision']!=run['base_agent_revision'] or run['policy_version']!=policy.PROFILE_VERSION:
        update_run(cursor,run,status='stale',finished_at=time.time(),worker_id=None,lease_until=None)
        return
    if run['proposal'] is None:
        update_run(cursor,run,status='no_change',finished_at=time.time(),worker_id=None,lease_until=None)
    else:
        state=validate(run['proposal'],run['input_snapshot'])
        version=publish(cursor,uid,state,'agent',p,run)
        update_run(cursor,run,status='succeeded',applied_version=version,finished_at=time.time(),worker_id=None,lease_until=None)
    cursor.execute('UPDATE music_discovery_profiles SET agent_source_revision=%s,next_review_at=%s WHERE user_id=%s',
        (run['source_revision'],time.time()+1800 if p['evidence_revision']>run['source_revision'] else None,uid))


def control(cursor,uid,payload,action):
    p=store.profile(cursor,uid)
    if p['agent_revision']!=payload.expected_agent_revision:
        raise HTTPException(409,'画像已更新，请刷新后重试')
    state=p['agent_state']
    if action=='correction':
        item=payload.item.model_dump()|{'confidence':1,'source':'user','support':{'recordings':0,'days':0},
            'observed_at':datetime.now(policy.ZONE).isoformat(),'half_life_days':None,'expires_at':None}
        confirmed=p['settings'].get('confirmed_profile_items',[])
        confirmed=[i for i in confirmed if key(i)!=key(item)]+[item]
        if len(confirmed)>120: raise HTTPException(422,'明确偏好最多 120 项')
        settings=p['settings']|{'confirmed_profile_items':confirmed}
        cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(encode(settings),uid))
        p['settings']=settings
        state=state|{'items':[i for i in state.get('items',[]) if key(i)!=key(item)]+[item]}
    elif action=='reset':
        state={'schema_version':1,'policy_version':policy.PROFILE_VERSION,'summary':'已清除推断，开始新的学习区间','items':[],'hypotheses':[]}
        cursor.execute('UPDATE music_discovery_profiles SET learning_since=%s WHERE user_id=%s',(time.time(),uid))
    elif action=='restore':
        cursor.execute('SELECT snapshot FROM music_discovery_profile_versions WHERE user_id=%s AND version=%s',(uid,payload.version))
        old=cursor.fetchone()
        if not old: raise HTTPException(404,'画像版本不存在')
        state=store.decoded(old['snapshot'])
        state=state|{'items':[i for i in state.get('items',[]) if i.get('source')!='user']}
    store.invalidate(cursor,uid,evidence=False,control=True)
    p=store.profile(cursor,uid)
    publish(cursor,uid,state,{'correction':'user','reset':'reset','restore':'restore'}[action],p)
    return store.profile(cursor,uid)


def cleanup(cursor,uid):
    cursor.execute('''DELETE FROM music_discovery_profile_versions WHERE user_id=%s AND created_at<%s
        AND version < (SELECT n.cutoff FROM (SELECT MAX(version)-49 AS cutoff FROM music_discovery_profile_versions WHERE user_id=%s) n)
        LIMIT 100''',(uid,time.time()-180*86400,uid))
    cursor.execute("DELETE FROM music_discovery_profile_runs WHERE user_id=%s AND status NOT IN ('pending','running','usage_pending') "
                   "AND updated_at<%s AND JSON_SEARCH(context,'one','unconfirmed') IS NULL LIMIT 100",(uid,time.time()-180*86400))
