"""Profile spend lives on profile tasks, independent from candidate daily totals."""
import secrets
import time

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent.budget import BudgetExceeded
from . import policy, profile_store, usage


def totals(context):
    requests=context.get('model_requests',[])
    return {'actual':sum(r.get('actual') or 0 for r in requests if r['state']=='settled'),
        'reserved':sum(r['reserved'] for r in requests if r['state']=='in_flight'),
        'unconfirmed':sum(r['reserved'] for r in requests if r['state']=='unconfirmed')}


def reserve(uid,run_id,amount,signature,deadline):
    config=accounts.settings()
    with library.database() as cursor:
        usage.lock(cursor,uid)
        usage.sync_candidate(cursor,uid,config)
        if usage.has_unknown(cursor,uid): raise BudgetExceeded('模型用量待核对，暂停两类新增付费调用','tokens')
        row=profile_store.get_run(cursor,uid,run_id,True)
        check_row(row)
        context=row['context'];requests=context.get('model_requests',[])
        if any(r['state']=='in_flight' for r in requests): raise BudgetExceeded('已有画像请求执行中','model_calls')
        if context.get('model_stop'): raise BudgetExceeded(context['model_stop'],'tokens')
        if any(r['signature']==signature for r in requests): raise BudgetExceeded('相同画像输入已发送，禁止自动重发','model_calls')
        if len(requests)>=min(4,config.get('music_profile_model_calls',4)):
            raise BudgetExceeded('画像任务模型请求次数已用完','model_calls')
        used=totals(context)
        task_limit=min(12000,config.get('music_profile_task_tokens',12000),config['music_agent_tokens'])
        if sum(used.values())+amount>task_limit: raise BudgetExceeded('画像任务 token 额度不足','tokens')
        day=row['accounting_date'] or policy.today()
        cursor.execute('SELECT run_id,context FROM music_discovery_profile_runs WHERE user_id=%s AND accounting_date=%s',(uid,day))
        rows=[profile_store.decode_run({'context':r['context'],'input_snapshot':{},'proposal':None,'steps':[]})|{'run_id':r['run_id']} for r in cursor.fetchall()]
        daily=sum(sum(totals(r['context']).values()) for r in rows)
        paid=sum(bool(r['context'].get('paid')) for r in rows)
        if not context.get('paid') and paid>=min(3,config.get('music_profile_daily_tasks',3)):
            raise BudgetExceeded('今日画像付费任务次数已用完','model_calls')
        if daily+amount>min(24000,config.get('music_profile_daily_tokens',24000)):
            raise BudgetExceeded('今日画像 token 额度已用完','tokens')
        request={'id':secrets.token_hex(16),'signature':signature,'reserved':amount,'state':'in_flight',
            'actual':None,'started_at':time.time(),'deadline':deadline,'dispatched':False}
        profile_store.update_run(cursor,row,accounting_date=day,context=context|{'model_requests':requests+[request]})
        return request


def check_row(row):
    execution=profile_store.execution_lease.get()
    if row['status']!='running' or not row['lease_until'] or row['lease_until']<=time.time() or (
            execution and execution[0]==row['run_id'] and execution[1]!=row['worker_id']):
        raise BudgetExceeded('画像任务已停止','model_calls')


def dispatched(uid,run_id,request_id):
    with library.database() as cursor:
        usage.lock(cursor,uid)
        row=profile_store.get_run(cursor,uid,run_id,True)
        check_row(row)
        requests=[dict(r) for r in row['context'].get('model_requests',[])]
        request=next(r for r in requests if r['id']==request_id)
        request['dispatched']=True
        profile_store.update_run(cursor,row,context=row['context']|{'model_requests':requests,'paid':True})


def settle(uid,run_id,request_id,actual=None,details=None,error=None):
    with library.database() as cursor:
        usage.lock(cursor,uid)
        row=profile_store.get_run(cursor,uid,run_id,True)
        requests=[dict(r) for r in row['context'].get('model_requests',[])]
        request=next((r for r in requests if r['id']==request_id),None)
        if not request or request['state']=='settled': return
        if actual is None and request['state']=='unconfirmed': return
        request.update(state='settled' if actual is not None else 'unconfirmed',actual=actual,
            usage=details or {},settled_at=time.time(),error=error)
        context=row['context']|{'model_requests':requests}
        if actual is None: context['model_stop']='画像用量待确认，暂停新增付费调用'
        if totals(context)['actual']>min(12000,accounts.settings().get('music_profile_task_tokens',12000)):
            context['model_stop']='画像实际用量超限，停止后续调用'
        changes={'context':context}
        if row['status']=='usage_pending' and not totals(context)['unconfirmed']:
            changes.update(status='interrupted',finished_at=time.time(),worker_id=None,lease_until=None)
        profile_store.update_run(cursor,row,**changes)
