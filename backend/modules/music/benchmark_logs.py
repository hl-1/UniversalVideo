"""Import bounded offline benchmark snapshots; never invoked by user searches."""
from __future__ import annotations

import json
import math
import re


def milliseconds(value):
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Timing must be finite')
    return max(0, value * 1000)


def save_snapshot(cursor, document, benchmark_id, *, prefix='', provider_concurrency=2):
    if not re.fullmatch(r'[a-f0-9]{32}', benchmark_id):
        raise ValueError('benchmark_id must contain 32 hexadecimal characters')
    if not 1 <= provider_concurrency <= 100:
        raise ValueError('Invalid provider concurrency')
    inserted = 0
    for run in document['runs']:
        label = prefix + run['label']
        if len(label) > 64:
            raise ValueError('Run label exceeds 64 characters')
        concurrency = int(run['concurrency'])
        if not 1 <= concurrency <= 100:
            raise ValueError('Invalid song concurrency')
        per_provider = 1 if concurrency == 1 else provider_concurrency
        records = [('run', 0, None, None, None, None, run['elapsed_s'],
                    'aborted' if run.get('aborted') else 'finished',
                    {k: v for k, v in run.items() if k != 'requests'} | {'scope': document.get('scope'), 'provider_concurrency': per_provider})]
        for index, request in enumerate(run.get('requests', []), 1):
            provider = request['provider']
            query_hash = request.get('query')
            if provider not in {'qq', 'netease', 'kugou'} or query_hash and not re.fullmatch(r'[a-f0-9]{16,64}', query_hash):
                raise ValueError('Invalid provider or query digest')
            status = 'skipped' if request.get('skipped') else 'failed' if request.get('exception') or request.get('business_error') else 'cache_hit' if request.get('network_calls') == 0 else 'ok'
            records.append(('request', index, provider, query_hash, request.get('queue_s'), request.get('network_s'), request.get('service_s'), status, request))
        for kind, sequence, provider, query, queue, network, elapsed, status, payload in records:
            cursor.execute('''INSERT IGNORE INTO music_search_benchmark_logs
                (benchmark_id,run_label,record_type,sequence_no,song_concurrency,provider_concurrency,
                 provider,query_hash,queue_ms,network_ms,elapsed_ms,status,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (benchmark_id,label,kind,sequence,concurrency,per_provider,provider,query,
                 milliseconds(queue),milliseconds(network),milliseconds(elapsed),status,
                 json.dumps(payload,ensure_ascii=False,allow_nan=False)))
            inserted += cursor.rowcount
    return inserted
