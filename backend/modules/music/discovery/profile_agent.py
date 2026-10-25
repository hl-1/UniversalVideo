"""A bounded tool agent over one immutable user evidence snapshot."""
import asyncio
import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
from backend.infrastructure import ai
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import model
from backend.modules.music.agent.faults import ModelFault
from backend.modules.music.agent.store import encode
from . import models, profile_budget, profile_store, store

SKILL=Path(__file__).resolve().parents[3]/'skills'/'music-profile'/'SKILL.md'


def skill():
    text=SKILL.read_text(encoding='utf-8')
    return {'text':text,'version':hashlib.sha256((text+encode(models.Proposal.model_json_schema())).encode()).hexdigest()}


def check_active(run_id):
    uid=accounts.current().id
    with library.database() as cursor:
        row=profile_store.get_run(cursor,uid,run_id)
        profile_budget.check_row(row)
        p=store.profile(cursor,uid)
    accounts.active_worker(uid)
    config=accounts.settings()
    if not config.get('music_profile_agent_enabled') or not config['music_enabled'] or not config['music_agent_network_enabled'] or not p['settings']['profile_learning_enabled']:
        raise ModelFault('画像学习已暂停')
    if p['control_revision']!=row['base_control_revision'] or p['agent_revision']!=row['base_agent_revision']:
        raise ModelFault('画像控制版本已变化，旧任务停止')
    if time.time()-(row['started_at'] or time.time())>min(120,config.get('music_profile_seconds',120)):
        raise ModelFault('画像评估已达到时长限制')
    return row


def complete(run_id,messages,tools):
    row=check_active(run_id);uid=row['user_id'];config=ai.load_config()
    estimated=model.input_tokens(messages,tools)
    max_output=1500
    if estimated>9000: raise ModelFault('画像任务上下文超出单次限制')
    deadline=min(time.time()+60,row['started_at']+min(120,accounts.settings().get('music_profile_seconds',120)))
    signature=hashlib.sha256(encode([messages,tools]).encode()).hexdigest()
    request=profile_budget.reserve(uid,run_id,estimated+max_output+400,signature,deadline)
    mode=accounts.settings().get('music_agent_api','auto')
    if mode=='auto': mode='responses' if config['model'].startswith(('gpt-6','gpt-5.6')) else 'chat'
    attempt={'dispatched':False,'on_dispatch':lambda:profile_budget.dispatched(uid,run_id,request['id'])}
    marker=model._attempt.set(attempt)
    try:
        result,actual=asyncio.run(model._drive(run_id,deadline,config,mode,messages,tools,max_output,False,check=check_active))
    except Exception:
        profile_budget.settle(uid,run_id,request['id'],None if attempt['dispatched'] else 0,error='画像模型请求中断')
        raise ModelFault('画像模型请求失败，保留当前画像') from None
    finally:
        model._attempt.reset(marker)
    profile_budget.settle(uid,run_id,request['id'],actual,attempt.get('usage'))
    if actual is None: raise ModelFault('画像模型未返回用量，等待核对')
    check_active(run_id)
    return result


def tools():
    specifications={
        'read_profile':('Read explicit settings and the current profile',{'type':'object','properties':{},'additionalProperties':False}),
        'summarize_evidence':('Read a bounded page of frozen evidence',{'type':'object','properties':{'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':30}},'additionalProperties':False}),
        'inspect_track_evidence':('Inspect evidence for one frozen track',{'type':'object','properties':{'track_key':{'type':'string'}},'required':['track_key'],'additionalProperties':False}),
        'inspect_conflicts':('Read positive and negative evidence counts',{'type':'object','properties':{},'additionalProperties':False}),
        'propose_profile_changes':('Submit a proposal for server validation',models.Proposal.model_json_schema()),
        'finish_without_changes':('Finish without changing the profile',{'type':'object','properties':{},'additionalProperties':False})}
    return [{'type':'function','function':{'name':name,'description':description,'parameters':schema}} for name,(description,schema) in specifications.items()]


def execute_tool(name,arguments,snapshot):
    if name=='read_profile':
        if arguments: raise ValueError('不接受参数')
        state=snapshot['base_state']
        return {'settings':snapshot['settings'],'profile':state|{'items':state.get('items',[])[:30],'hypotheses':state.get('hypotheses',[])[:10]},'coverage':snapshot['coverage']},False
    if name=='summarize_evidence':
        if set(arguments)-{'offset','limit'}: raise ValueError('未知参数')
        offset=arguments.get('offset',0);limit=arguments.get('limit',20)
        if type(offset)!=int or type(limit)!=int or offset<0 or not 1<=limit<=30: raise ValueError('分页参数无效')
        return {'items':snapshot['evidence'][offset:offset+limit],'total':len(snapshot['evidence']),'coverage':snapshot['coverage']},False
    if name=='inspect_track_evidence':
        if set(arguments)!={'track_key'}: raise ValueError('需要歌曲标识')
        evidence=[e for e in snapshot['evidence'] if e['track_key']==arguments['track_key']]
        if not evidence: raise ValueError('歌曲不在冻结快照')
        return {'items':evidence[:30],'total':len(evidence)},False
    if name=='inspect_conflicts':
        if arguments: raise ValueError('不接受参数')
        positive=sum(e['weight']>0 for e in snapshot['evidence']);negative=sum(e['weight']<0 for e in snapshot['evidence'])
        return {'positive':positive,'negative':negative,'user_items':snapshot['settings'].get('confirmed_profile_items',[])},False
    if name=='propose_profile_changes':
        profile_store.validate(arguments,snapshot)
        return models.Proposal.model_validate(arguments).model_dump(),True
    if name=='finish_without_changes':
        if arguments: raise ValueError('不接受参数')
        return None,True
    raise ValueError('不允许的画像工具')


def run(row):
    data=skill()
    if row['skill_version']!=data['version']: raise ModelFault('画像 Skill 已变化，停止旧任务')
    snapshot=row['input_snapshot'];run_id=row['run_id'];uid=row['user_id']
    messages=[{'role':'system','content':data['text']},{'role':'user','content':encode({
        'coverage':snapshot['coverage'],'instruction':'检查证据并维护偏好；依据不足时结束。',
        'sample':snapshot['evidence'][:6],'current_profile':{'summary':snapshot['base_state'].get('summary',''),
            'items':snapshot['base_state'].get('items',[])[:20]}})}]
    for _ in range(4):
        response=complete(run_id,messages,tools())
        calls=response.get('tool_calls',[])
        if not calls or len(calls)>6: raise ModelFault('画像模型未返回合法工具调用')
        messages.append({k:v for k,v in response.items() if k!='finish_reason'})
        for call in calls:
            check_active(run_id)
            name=call['function']['name']
            try:
                arguments=model.json_value(call['function']['arguments'])
                result,finished=execute_tool(name,arguments,snapshot)
            except (ValueError,TypeError,KeyError) as exc:
                result={'error':'工具参数或证据校验失败','type':type(exc).__name__};finished=False
            with library.database() as cursor:
                store.lock_user(cursor,uid)
                current=profile_store.get_run(cursor,uid,run_id,True)
                profile_budget.check_row(current)
                changes={'steps':(current['steps']+[{'tool':name,'at':time.time(),'valid':'error' not in (result or {})}])[-40:]}
                if finished:
                    changes.update(proposal=result,context=current['context']|{'proposal_ready':True})
                profile_store.update_run(cursor,current,**changes)
            if finished: return
            messages.append({'role':'tool','tool_call_id':call['id'],'content':encode(result)})
    raise ModelFault('画像评估请求额度已用完，保留原画像')
