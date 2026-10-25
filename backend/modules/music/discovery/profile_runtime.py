"""Application-owned durable queue scanner with leases and bounded workers."""
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import model, runtime, store as runs
from . import feeds, pool, profile_agent, profile_budget, profile_store, store, usage

logger=logging.getLogger(__name__)
_stop=threading.Event();_wake=threading.Event();_thread=None
_pool=None;_inflight=set();_lock=threading.Lock()
_worker=secrets.token_hex(12)
_maintenance_after=0
_maintenance_at=0


def wake():
    _wake.set()


def principal(uid):
    user=accounts.get_user(uid)
    return accounts.Principal(uid,user['username'],user['role'])


def claim(uid,run_id):
    config=accounts.settings()
    with library.database() as cursor:
        usage.lock(cursor,uid)
        row=profile_store.get_run(cursor,uid,run_id,True)
        if row['status']!='pending' or row['context'].get('not_before',0)>time.time(): return None
        p=store.profile(cursor,uid)
        if not config.get('music_profile_agent_enabled') or not p['settings']['profile_learning_enabled']:
            profile_store.update_run(cursor,row,status='cancelled',finished_at=time.time())
            return None
        cursor.execute("SELECT COUNT(*) AS n FROM music_discovery_profile_runs WHERE status='running'")
        profiles=cursor.fetchone()['n']
        cursor.execute("SELECT COUNT(*) AS n FROM music_recommendation_runs WHERE status='running' AND deleted=FALSE")
        if profiles>=config.get('music_profile_concurrency',1) or profiles+cursor.fetchone()['n']>=config['music_agent_concurrency']:
            return None
        return profile_store.update_run(cursor,row,status='running',worker_id=_worker+':'+secrets.token_hex(8),
            lease_until=time.time()+45,started_at=time.time())


def renew(uid,run_id,worker,stop):
    while not stop.wait(10):
        try:
            with library.database() as cursor:
                row=profile_store.get_run(cursor,uid,run_id,True)
                if row['status']!='running' or row['worker_id']!=worker or row['lease_until']<=time.time(): return
                profile_store.update_run(cursor,row,lease_until=time.time()+45)
        except Exception:
            logger.warning('Profile lease renewal failed',exc_info=True)
            return


def run_profile(uid,run_id):
    marker=None;lease_marker=None;heartbeat_stop=threading.Event();heartbeat=None;worker=None
    try:
        marker=accounts.principal.set(principal(uid))
        row=claim(uid,run_id)
        if not row: return
        worker=row['worker_id']
        lease_marker=profile_store.execution_lease.set((run_id,worker))
        heartbeat=threading.Thread(target=renew,args=(uid,run_id,worker,heartbeat_stop),daemon=True)
        heartbeat.start()
        if not row['context'].get('proposal_ready'):
            # A previously issued request is never automatically repeated after restart.
            if row['context'].get('model_requests'):
                raise RuntimeError('画像请求已发出，禁止自动重发')
            profile_agent.run(row)
        with library.database() as cursor:
            usage.lock(cursor,uid)
            profile_store.apply(cursor,uid,run_id,worker)
    except Exception as exc:
        logger.warning('Profile evaluation stopped: run=%s type=%s',run_id,type(exc).__name__)
        try:
            with library.database() as cursor:
                usage.lock(cursor,uid)
                row=profile_store.get_run(cursor,uid,run_id,True)
                if row['status']=='running' and row['worker_id']==worker:
                    unknown=profile_budget.totals(row['context'])['unconfirmed']
                    p=store.profile(cursor,uid)
                    stale=p['control_revision']!=row['base_control_revision'] or p['agent_revision']!=row['base_agent_revision']
                    cancelled=not p['settings']['profile_learning_enabled'] or not accounts.settings().get('music_profile_agent_enabled')
                    profile_store.update_run(cursor,row,status='usage_pending' if unknown else 'cancelled' if cancelled else 'stale' if stale else 'failed',
                        error_code=type(exc).__name__[:64],error_message='画像评估未完成，继续使用已有画像',
                        finished_at=None if unknown else time.time(),worker_id=None,lease_until=None)
        except Exception:
            logger.exception('Cannot persist profile failure')
    finally:
        heartbeat_stop.set()
        if heartbeat: heartbeat.join(timeout=2)
        if marker is not None: accounts.principal.reset(marker)
        if lease_marker is not None: profile_store.execution_lease.reset(lease_marker)
        with _lock: _inflight.discard(('profile',run_id))


def run_refill(uid,run_id):
    marker=None
    try:
        marker=accounts.principal.set(principal(uid))
        runtime.run(run_id)
    except Exception:
        logger.warning('Candidate refill stopped: run=%s',run_id,exc_info=True)
    finally:
        if marker is not None: accounts.principal.reset(marker)
        with _lock: _inflight.discard(('refill',run_id))


def recover_profiles(cursor):
    cursor.execute("SELECT user_id,run_id FROM music_discovery_profile_runs WHERE status='running' AND lease_until<%s",(time.time(),))
    rows=cursor.fetchall()
    for item in rows:
        usage.lock(cursor,item['user_id'])
        row=profile_store.get_run(cursor,item['user_id'],item['run_id'],True)
        if row['status']!='running' or row['lease_until']>=time.time(): continue
        requests=[r|{'state':'unconfirmed'} if r['state']=='in_flight' else r for r in row['context'].get('model_requests',[])]
        unknown=any(r['state']=='unconfirmed' for r in requests)
        ready=row['context'].get('proposal_ready') and not unknown
        profile_store.update_run(cursor,row,status='usage_pending' if unknown else 'pending' if ready or not requests else 'interrupted',
            worker_id=None,lease_until=None,context=row['context']|{'model_requests':requests},
            finished_at=None if unknown or ready else time.time())


def maintain_candidates():
    """Revisit low pools after cooldown even when no page makes another request."""
    global _maintenance_after
    config=accounts.settings()
    if not config['music_enabled'] or not config['music_daily_enabled'] or not config['music_agent_network_enabled']:
        return
    with library.database() as cursor:
        cursor.execute('''SELECT p.user_id,u.username,u.role FROM music_discovery_profiles p
            JOIN users u ON u.id=p.user_id WHERE u.status='active' AND p.user_id>%s
            AND (p.updated_at>%s OR EXISTS (SELECT 1 FROM music_discovery_feeds f
                WHERE f.user_id=p.user_id AND f.created_at>%s)) ORDER BY p.user_id LIMIT 20''',
            (_maintenance_after,time.time()-7*86400,time.time()-7*86400))
        users=cursor.fetchall()
    _maintenance_after=users[-1]['user_id'] if users else 0
    for user in users:
        marker=accounts.principal.set(accounts.Principal(user['user_id'],user['username'],user['role']))
        try:
            with library.database() as cursor:
                usage.lock(cursor,user['user_id'])
                pool.trim(cursor,user['user_id'])
                feeds.ensure_current(cursor,user['user_id'])
                pool.schedule(cursor,user['user_id'])
        except Exception:
            logger.warning('Candidate maintenance deferred: user=%s',user['user_id'],exc_info=True)
        finally:
            accounts.principal.reset(marker)


def scan():
    global _maintenance_at
    if time.time()>=_maintenance_at:
        maintain_candidates()
        _maintenance_at=time.time()+30
    with library.database() as cursor:
        # Recovery runs before scheduling to preserve unknown occupancy across dates.
        usage.lock(cursor,0)
        runs.recover(cursor)
        recover_profiles(cursor)
        cursor.execute("SELECT run_id,user_id FROM music_recommendation_runs WHERE status='pending' AND deleted=FALSE "
            "AND JSON_EXTRACT(context,'$.background')=TRUE ORDER BY created_at LIMIT 20")
        refills=cursor.fetchall()
        cursor.execute("SELECT run_id,user_id FROM music_discovery_profile_runs WHERE status='pending' ORDER BY created_at LIMIT 20")
        profiles=cursor.fetchall()
        cursor.execute('SELECT user_id FROM music_discovery_profiles WHERE next_review_at<=%s ORDER BY next_review_at LIMIT 20',(time.time(),))
        reviews=cursor.fetchall()
    for item in reviews:
        with library.database() as cursor:
            usage.lock(cursor,item['user_id'])
            profile_store.schedule(cursor,item['user_id'])
            profile_store.cleanup(cursor,item['user_id'])
    for kind,items,operation in [('refill',refills,run_refill),('profile',profiles,run_profile)]:
        for item in items:
            with _lock:
                identity=(kind,item['run_id'])
                if identity in _inflight or len(_inflight)>=4: continue
                _inflight.add(identity)
            _pool.submit(operation,item['user_id'],item['run_id'])


def loop():
    while not _stop.is_set():
        try: scan()
        except Exception: logger.warning('Discovery queue scan deferred',exc_info=True)
        _wake.wait(5);_wake.clear()


def start():
    global _thread,_pool
    if _thread and _thread.is_alive(): return
    _stop.clear();_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='discovery')
    _thread=threading.Thread(target=loop,name='discovery-queue',daemon=True)
    _thread.start()


def stop():
    global _thread
    _stop.set();_wake.set()
    if _thread: _thread.join(timeout=5)
    with _lock:
        active=list(_inflight)
    for _,run_id in active:
        model.cancel(run_id)
    if _pool: _pool.shutdown(wait=False,cancel_futures=True)
    _thread=None


def cancel_user(uid,key=None):
    with library.database() as cursor:
        usage.lock(cursor,uid)
        cursor.execute("SELECT * FROM music_discovery_profile_runs WHERE user_id=%s AND status IN ('pending','running')"+
            (' AND run_id=%s' if key else '')+' FOR UPDATE',(uid,key) if key else (uid,))
        for raw in cursor.fetchall():
            row=profile_store.decode_run(raw)
            requests=[r|{'state':'unconfirmed'} if r['state']=='in_flight' else r for r in row['context'].get('model_requests',[])]
            unknown=any(r['state']=='unconfirmed' for r in requests)
            profile_store.update_run(cursor,row,status='usage_pending' if unknown else 'cancelled',
                worker_id=None,lease_until=None,finished_at=None if unknown else time.time(),
                context=row['context']|{'model_requests':requests})
            model.cancel(row['run_id'])
