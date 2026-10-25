"""MySQL is the authority for both discovery ledgers, including old requests."""
import time
from collections import defaultdict

from backend.modules.accounts import service as accounts
from backend.modules.music.agent import store as runs
from . import policy


def lock(cursor, uid):
    # All admission paths use quota -> user -> accounting -> task.
    cursor.execute('SELECT id FROM system_settings WHERE id=1 FOR UPDATE')
    cursor.execute('SELECT id FROM users WHERE id=%s FOR UPDATE', (uid,))


def lock_run(cursor, run_id):
    cursor.execute('SELECT user_id FROM music_recommendation_runs WHERE run_id=%s', (run_id,))
    owner = cursor.fetchone()
    if owner:
        lock(cursor, owner['user_id'])


def candidate_limits(config):
    return (min(60000, config.get('music_daily_tokens', 60000), config['music_agent_tokens']),
            min(16, config.get('music_daily_model_calls', 16), config['music_agent_model_calls']),
            min(5000, config.get('music_daily_network_requests', 5000)))


def sync_candidate(cursor, uid, config=None):
    """Idempotently import/aggregate daily discovery only; never mix scene spend.

    Recomputing under the user lock also accounts for pre-V4 requests, deletion,
    interrupted workers and cross-midnight settlement without a second ledger.
    """
    config = config or accounts.settings()
    totals = defaultdict(lambda: dict(actual_tokens=0, reserved_tokens=0,
                                     unconfirmed_tokens=0, model_calls=0, network_requests=0))
    totals[policy.today()]
    cursor.execute("SELECT created_at,context FROM music_recommendation_runs WHERE user_id=%s "
                   "AND JSON_UNQUOTE(JSON_EXTRACT(request,'$.kind'))='daily_discovery'", (uid,))
    for row in cursor.fetchall():
        context = runs.decode({'context': row['context'], 'request': {}, 'steps': [], 'result': {}})['context']
        ledger = context.get('model_requests', [])
        for request in ledger:
            from datetime import date
            day = date.fromisoformat(request['accounting_date']) if request.get('accounting_date') else policy.today(request.get('started_at', row['created_at']))
            entry = totals[day]
            entry['model_calls'] += 1
            state = request.get('state')
            if state == 'settled':
                entry['actual_tokens'] += request.get('actual') or 0
            elif state == 'in_flight':
                entry['reserved_tokens'] += request['reserved']
            elif state == 'unconfirmed':
                entry['unconfirmed_tokens'] += request['reserved']
        if not ledger:
            totals[policy.today(row['created_at'])]['actual_tokens'] += context.get('actual_tokens', context.get('budget', {}).get('tokens', 0))
            totals[policy.today(row['created_at'])]['model_calls'] += context.get('budget', {}).get('model_calls', 0)
        network = context.get('network_by_date')
        if network:
            from datetime import date
            for day, count in network.items():
                totals[date.fromisoformat(day)]['network_requests'] += count
        else:
            totals[policy.today(row['created_at'])]['network_requests'] += context.get('budget', {}).get('network_requests', 0)
    now = time.time()
    limits = candidate_limits(config)
    for day, entry in totals.items():
        cursor.execute('''INSERT INTO music_discovery_daily_usage
            (user_id,usage_date,token_limit,model_call_limit,network_request_limit,actual_tokens,reserved_tokens,
             unconfirmed_tokens,model_calls,network_requests,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
            token_limit=VALUES(token_limit),model_call_limit=VALUES(model_call_limit),network_request_limit=VALUES(network_request_limit),
            actual_tokens=VALUES(actual_tokens),reserved_tokens=VALUES(reserved_tokens),unconfirmed_tokens=VALUES(unconfirmed_tokens),
            model_calls=VALUES(model_calls),network_requests=VALUES(network_requests),updated_at=VALUES(updated_at)''',
            (uid, day, *limits, entry['actual_tokens'], entry['reserved_tokens'], entry['unconfirmed_tokens'],
             entry['model_calls'], entry['network_requests'], now, now))
    return totals


def current(cursor, uid):
    cursor.execute('SELECT * FROM music_discovery_daily_usage WHERE user_id=%s AND usage_date=%s FOR UPDATE', (uid, policy.today()))
    return cursor.fetchone()


def has_unknown(cursor, uid):
    cursor.execute('SELECT 1 FROM music_discovery_daily_usage WHERE user_id=%s AND unconfirmed_tokens>0 LIMIT 1', (uid,))
    if cursor.fetchone():
        return True
    cursor.execute('SELECT context FROM music_discovery_profile_runs WHERE user_id=%s', (uid,))
    import json
    return any(any(r.get('state') == 'unconfirmed' for r in (json.loads(row['context']) if isinstance(row['context'], str) else row['context']).get('model_requests', []))
               for row in cursor.fetchall())


def candidate_admission(cursor, row, amount=0, network=0):
    from backend.modules.music.agent.budget import BudgetExceeded
    sync_candidate(cursor, row['user_id'])
    day = current(cursor, row['user_id'])
    if amount:
        if has_unknown(cursor, row['user_id']):
            raise BudgetExceeded('模型用量待核对，暂停两类新增付费调用', 'tokens')
        if day['actual_tokens'] + day['reserved_tokens'] + day['unconfirmed_tokens'] + amount > day['token_limit']:
            raise BudgetExceeded('今日候选增强 token 额度已用完', 'tokens')
        if day['model_calls'] >= day['model_call_limit']:
            raise BudgetExceeded('今日候选增强模型请求额度已用完', 'model_calls')
    if day['network_requests'] + network > day['network_request_limit']:
        raise BudgetExceeded('今日候选补充网络额度已用完', 'network_requests')
    return day
