import copy
import json
import secrets
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import budget, model, store as runs
from backend.modules.music.discovery import feeds, identity, models, policy, pool, profile_agent, profile_budget, profile_runtime, profile_store, store, usage
from backend.tests.music import test_daily_discovery as discovery_tests


def evidence_snapshot():
    evidence=[{'ref':f'event:{i}','track_key':str(i),'recording_key':str(i),'at':time.time()-(i%3)*86400,
        'weight':.5,'context':'focus','explicit':False,'values':{'language':'中文','energy':.2},'quality':{'language':.9,'energy':.85}}
        for i in range(6)]
    return {'evidence':evidence,'settings':{},'base_state':{},'coverage':{'complete':True}}


class ProfileValidationTests(unittest.TestCase):
    def proposal(self,snapshot,**changes):
        item=dict(dimension='language',value='中文',scope='long_term',context='general',preference=.9,evidence_refs=[e['ref'] for e in snapshot['evidence']])|changes
        return {'summary':'中文偏好','items':[item]}

    def test_confidence_is_computed_and_change_is_bounded(self):
        snapshot=evidence_snapshot()
        state=profile_store.validate(self.proposal(snapshot),snapshot)
        self.assertEqual(len(state['items']),1)
        self.assertGreaterEqual(state['items'][0]['confidence'],.6)
        self.assertEqual(state['items'][0]['preference'],.25)
        with self.assertRaises(ValueError):
            profile_store.validate(self.proposal(snapshot)|{'confidence':1},snapshot)

    def test_sparse_and_single_day_long_term_evidence_stays_observation(self):
        snapshot=evidence_snapshot()
        for e in snapshot['evidence']: e['at']=time.time()
        state=profile_store.validate(self.proposal(snapshot),snapshot)
        self.assertFalse(state['items']);self.assertEqual(len(state['hypotheses']),1)

    def test_fabricated_wrong_dimension_context_and_numeric_evidence_rejected(self):
        snapshot=evidence_snapshot()
        for changes in ({'evidence_refs':['event:foreign']},{'dimension':'style','value':'jazz'},
                        {'scope':'context','context':'sleep'},{'dimension':'energy','value':'.99'},
                        {'preference':-.9}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                profile_store.validate(self.proposal(snapshot,**changes),snapshot)

    def test_agent_tools_only_read_frozen_track_evidence(self):
        snapshot=evidence_snapshot()
        with self.assertRaises(ValueError): profile_agent.execute_tool('inspect_track_evidence',{'track_key':'foreign'},snapshot)
        with self.assertRaises(ValueError): profile_agent.execute_tool('fetch_lyrics',{},snapshot)
        with self.assertRaises(ValueError): profile_agent.execute_tool('summarize_evidence',{'offset':0,'limit':500},snapshot)
        result,finished=profile_agent.execute_tool('summarize_evidence',{'offset':1,'limit':2},snapshot)
        self.assertEqual(len(result['items']),2);self.assertFalse(finished)


class PoolProfileIntegrationTests(unittest.TestCase):
    setUpClass=classmethod(discovery_tests.DiscoveryIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(discovery_tests.DiscoveryIntegrationTests.tearDownClass.__func__)
    setUp=discovery_tests.DiscoveryIntegrationTests.setUp
    tearDown=discovery_tests.DiscoveryIntegrationTests.tearDown
    track=discovery_tests.DiscoveryIntegrationTests.track

    def next(self,key=None,**payload):
        return self.client.post('/api/music/discovery/feed/next',json=payload,headers={'Idempotency-Key':key or secrets.token_hex(16)})

    def fill(self,n=80):
        with library.database() as cursor:
            store.lock_user(cursor,self.user_id)
            for i in range(n):
                track=self.track()|{'identifier':f'pool{i}','title':f'Song {i}','artist':f'Artist {i}',
                    'analysis':{'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}},
                    'validation':{'identity':True,'playable':True,'full':True,'checked_at':time.time()}}
                pool.put(cursor,self.user_id,track)

    def expire_cooldown(self):
        with library.database() as cursor: cursor.execute('UPDATE music_discovery_feeds SET created_at=created_at-4 WHERE user_id=%s',(self.user_id,))

    def legacy_short(self, first, count=2):
        with library.database() as cursor:
            row=feeds.get(cursor,self.user_id,first['feed_id'])
            row=row|{'feed_id':secrets.token_hex(16),'request_key':secrets.token_hex(32),
                'actual_count':count,'created_at':time.time()-4}
            cursor.execute('INSERT INTO music_discovery_feeds ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
            cursor.execute('''INSERT INTO music_discovery_feed_items
                (feed_id,position,track_key,recording_key,work_key,track_snapshot,selection,validated_at)
                SELECT %s,position,track_key,recording_key,work_key,track_snapshot,selection,validated_at
                FROM music_discovery_feed_items WHERE feed_id=%s AND position<=%s''',(row['feed_id'],first['feed_id'],count))
            return row

    def job(self):
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            config=accounts.DEFAULT_SETTINGS|{'music_profile_agent_enabled':True,'music_agent_enabled':True}
            cursor.execute('UPDATE system_settings SET config=%s WHERE id=1',(json.dumps(config),))
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            for i in range(6):
                track=library.TrackInput.model_validate(self.track()|{'identifier':f'favorite{i}','title':f'Fav {i}'})
                key=library.save_track(cursor,track)
                identity.ensure(cursor,self.user_id,track.model_dump())
                cursor.execute('INSERT INTO music_favorites (user_id,track_id,created_at) VALUES (%s,%s,%s)',(self.user_id,key,time.time()-(i%3)*86400))
            row=profile_store.schedule(cursor,self.user_id,'manual',force=True)
            row=profile_store.update_run(cursor,row,status='running',worker_id='test-worker',lease_until=time.time()+60,started_at=time.time())
            return row

    def test_more_than_three_changes_use_no_model_or_external_search(self):
        self.fill()
        seen=set()
        with patch.object(model,'complete',side_effect=AssertionError('model called')),patch.object(pool,'schedule'):
            for _ in range(6):
                result=self.next();self.assertEqual(result.status_code,200,result.text)
                keys={t['key'] for t in result.json()['tracks']}
                self.assertEqual(len(keys),6);self.assertFalse(keys&seen);seen|=keys
                self.expire_cooldown()
        self.assertEqual(len(seen),36)

    def test_idempotency_cooldown_and_foreign_feed(self):
        self.fill(20)
        first=self.next('same-request');self.assertEqual(first.status_code,200,first.text)
        again=self.next('same-request');self.assertEqual(first.json()['feed_id'],again.json()['feed_id'])
        self.assertEqual(self.next('same-request',need='英文').status_code,409)
        self.assertEqual(self.next().status_code,429)
        self.assertEqual(self.client.get('/api/music/discovery/feeds/'+'0'*32).status_code,404)

    def test_concurrent_feed_request_is_one_atomic_list(self):
        self.fill(30);principal=accounts.current()
        def change(_):
            marker=accounts.principal.set(principal)
            try: return feeds.next_feed(models.NextFeed(),'parallel-request')['feed_id']
            finally: accounts.principal.reset(marker)
        with ThreadPoolExecutor(max_workers=4) as executor: ids=list(executor.map(change,range(4)))
        self.assertEqual(len(set(ids)),1)

    def test_candidate_exhaustion_preserves_list_and_replay_keeps_blocks(self):
        self.fill(10);first=self.next().json();self.expire_cooldown()
        exhausted=self.next().json()
        self.assertEqual(exhausted['feed_id'],first['feed_id'])
        key=first['tracks'][0]['key']
        self.client.put('/api/music/discovery/feedback',json={'track_key':key,'reason':'dislike'})
        self.expire_cooldown()
        replay=self.next(replay=True).json()
        self.assertNotIn(key,[t['key'] for t in replay['tracks']])
        self.assertEqual(len(replay['tracks']),6)

    def test_incomplete_candidates_do_not_publish_a_short_batch(self):
        self.fill(4)
        with patch.object(pool,'schedule'):
            short=self.next().json()
            self.assertIsNone(short['feed_id'])
            self.assertEqual(short['tracks'],[])
            self.assertEqual(short['reason'],'insufficient_candidates')
            self.fill(10)
            first=self.next().json()
            self.assertEqual(len(first['tracks']),6)
            self.assertEqual(first['target_count'],6)
            self.expire_cooldown()
            short=self.next().json()
            self.assertEqual(short['feed_id'],first['feed_id'])
            self.assertEqual(short['reason'],'insufficient_candidates')
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_discovery_feeds WHERE user_id=%s',(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],1)

    def test_background_refills_resume_after_cooldown_without_page_requests(self):
        self.fill(4)
        with patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        with library.database() as cursor:
            cursor.execute('SELECT run_id,context FROM music_recommendation_runs WHERE user_id=%s',(self.user_id,))
            first=cursor.fetchone()
            self.assertTrue(store.decoded(first['context'])['background'])
            cursor.execute("UPDATE music_recommendation_runs SET status='finished' WHERE run_id=%s",(first['run_id'],))
        with patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_recommendation_runs WHERE user_id=%s',(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],1)
            cursor.execute('UPDATE music_discovery_daily_usage SET next_refill_at=0 WHERE user_id=%s',(self.user_id,))
        with patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        with library.database() as cursor:
            cursor.execute("SELECT COUNT(*) n FROM music_recommendation_runs WHERE user_id=%s AND status='pending'",(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],1)

    def test_full_raw_pool_still_refills_when_diversity_cannot_fill_six(self):
        self.fill(40)
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            cursor.execute("UPDATE music_discovery_candidates SET track_snapshot=JSON_SET(track_snapshot,'$.artist','Only artist') WHERE user_id=%s",(self.user_id,))
            self.assertIsNotNone(pool.schedule(cursor,self.user_id))

    def test_refill_prioritizes_missing_artists_and_uses_routes_after_the_first_three(self):
        from backend.modules.music.discovery import recall,service
        from backend.modules.music.agent import features,runtime,validation
        self.fill(6)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.expire_cooldown()
        def candidate(index,artist):
            return self.track()|{'identifier':f'diversity{index}','title':f'Diversity {index}','artist':artist}
        analysis={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}}
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            for i,artist in enumerate(['A']*11+['B']*2+['C']):
                pool.put(cursor,self.user_id,candidate(i,artist)|{'analysis':analysis,
                    'validation':{'identity':True,'playable':True,'full':True,'checked_at':time.time()}})
            for i in range(2):
                pool.put(cursor,self.user_id,candidate(100+i,'A')|{'analysis':analysis,
                    'validation':{'identity':True,'playable':True,'full':True,'checked_at':time.time()-7200}})
            run_id=pool.schedule(cursor,self.user_id)
        self.assertIsNotNone(run_id)
        plan=models.Plan(routes=[{'query':artist,'type':'seed_features'} for artist in ['A','B','C','D']],
            languages=['中文']).model_dump()
        def search(provider,query,page):
            values=[candidate(200+i,'A') for i in range(20)] if query=='A' else [candidate(300,'D')] if query=='D' else []
            return [library.TrackInput.model_validate(value) for value in values]
        with patch.object(recall,'baseline',return_value=plan),patch.object(recall,'plan',return_value=plan),patch.object(features,'prefetch_lyrics'), \
             patch.object(features,'analyze',return_value=analysis),patch.object(service.sources,'_search',side_effect=search) as searches, \
             patch.object(validation,'validate',return_value={'identity':True,'playable':True,'full':True,'checked_at':time.time()}) as validations:
            runtime.run(run_id)
        self.assertEqual(searches.call_args_list[0].args[1],'D')
        self.assertEqual({call.args[1] for call in searches.call_args_list},{'A','B','C','D'})
        self.assertEqual([call.args[0]['artist'] for call in validations.call_args_list],['D'])
        with library.database() as cursor:
            row=runs.get(cursor,run_id,self.user_id)
            self.assertEqual(row['status'],'finished',row['context'].get('message'))
        next_feed=self.next().json()
        self.assertEqual(len(next_feed['tracks']),6)
        self.assertNotEqual(next_feed['feed_id'],first['feed_id'])
        self.assertFalse({t['key'] for t in next_feed['tracks']} & {t['key'] for t in first['tracks']})

    def test_refill_covers_the_portfolio_and_rotates_playback_checks_between_artists(self):
        from backend.modules.music.discovery import recall,service
        from backend.modules.music.agent import features,runtime,validation
        self.fill(6)
        with patch.object(pool,'schedule'): self.next()
        analysis={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}}
        def candidate(index,artist):
            return self.track()|{'identifier':f'portfolio{index}','title':f'Portfolio {index}','artist':artist}
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            for i,artist in enumerate(['A','B','C','D','E','F']):
                track=library.TrackInput.model_validate(candidate(i,artist))
                key=library.save_track(cursor,track)
                cursor.execute('INSERT INTO music_favorites (user_id,track_id,created_at) VALUES (%s,%s,%s)',
                    (self.user_id,key,time.time()))
            for i in range(2):
                pool.put(cursor,self.user_id,candidate(100+i,'A')|{'analysis':analysis,
                    'validation':{'identity':True,'playable':True,'full':True,'checked_at':time.time()}})
            run_id=pool.schedule(cursor,self.user_id)
        plan=models.Plan(routes=[{'query':name} for name in ['A','B','C','D','E']],languages=['中文']).model_dump()
        def search(provider,query,page):
            return [library.TrackInput.model_validate(candidate(200+ord(query.lower())*10+i,query)) for i in range(2)]
        with patch.object(recall,'baseline',return_value=plan),patch.object(features,'prefetch_lyrics',
                side_effect=HTTPException(503,'在线歌词服务暂时繁忙')), \
             patch.object(features,'analyze',return_value=analysis),patch.object(service.sources,'_search',side_effect=search) as searches, \
             patch.object(validation,'validate',return_value={'identity':True,'playable':True,'full':True,'checked_at':time.time()}) as validations:
            runtime.run(run_id)
        self.assertEqual({call.args[1].lower() for call in searches.call_args_list},{'b','c','d','e','f'})
        checked=[call.args[0]['artist'].lower() for call in validations.call_args_list]
        self.assertEqual(len(set(checked[:5])),5,checked)
        self.assertEqual(len(checked),10)
        with library.database() as cursor:
            self.assertEqual(runs.get(cursor,run_id,self.user_id)['status'],'finished')

    def test_legacy_artist_concentration_repairs_only_when_a_full_list_is_ready(self):
        self.fill(6)
        with patch.object(pool,'schedule'): first=self.next().json()
        names=['周杰伦 / Lara','周杰伦','张韶涵','周杰伦','蔡依林 / 周杰伦','周杰伦♚']
        with library.database() as cursor:
            for position,name in enumerate(names,1):
                cursor.execute('SELECT track_snapshot FROM music_discovery_feed_items WHERE feed_id=%s AND position=%s',
                    (first['feed_id'],position))
                track=store.decoded(cursor.fetchone()['track_snapshot'])|{'artist':name}
                cursor.execute('UPDATE music_discovery_feed_items SET track_snapshot=%s WHERE feed_id=%s AND position=%s',
                    (runs.encode(track),first['feed_id'],position))
        with patch.object(pool,'schedule'):
            waiting=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(waiting['feed_id'],first['feed_id'])
        self.assertEqual(len(waiting['tracks']),6)
        self.fill(20)
        with patch.object(pool,'schedule'):
            repaired=self.client.get('/api/music/discovery/feed').json()
        self.assertNotEqual(repaired['feed_id'],first['feed_id'])
        self.assertEqual(len(repaired['tracks']),6)
        self.assertEqual(sum('周杰伦' in identity.artist_members(t['artist']) for t in repaired['tracks']),2)
        self.assertTrue({first['tracks'][i]['key'] for i in (0,1,2)}<={t['key'] for t in repaired['tracks']})
        with library.database() as cursor:
            cursor.execute('SELECT track_snapshot FROM music_discovery_feed_items WHERE feed_id=%s ORDER BY position',(first['feed_id'],))
            self.assertEqual([store.decoded(row['track_snapshot'])['artist'] for row in cursor.fetchall()],names)

    def test_pending_recall_resumes_without_repeating_search_or_resetting_budget(self):
        from backend.modules.music.discovery import recall,service
        from backend.modules.music.agent import features,runtime,validation
        self.fill(6)
        with patch.object(pool,'schedule'): self.next()
        with library.database() as cursor: run_id=pool.schedule(cursor,self.user_id)
        analysis={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}}
        plan=models.Plan(routes=[{'query':name} for name in ['A','B','C']],languages=['中文']).model_dump()
        def search(provider,query,page):
            budget.charge(run_id,network_requests=1)
            return [library.TrackInput.model_validate(self.track()|{'identifier':f'resume{query}{i}',
                'title':f'Resume {query} {i}','artist':query}) for i in range(2)]
        def validate(*args,**kwargs):
            budget.charge(run_id,network_requests=1)
            return {'identity':True,'playable':True,'full':True,'checked_at':time.time()}
        denied=HTTPException(401,'认证已失效')
        denied.agent_policy_error=True
        with patch.object(recall,'baseline',return_value=plan),patch.object(recall,'plan',return_value=plan), \
             patch.object(features,'analyze',return_value=analysis),patch.object(service.sources,'_search',side_effect=search) as searches, \
             patch.object(validation,'validate',side_effect=validate):
            with patch.object(features,'prefetch_lyrics',side_effect=denied): runtime.run(run_id)
            with library.database() as cursor:
                row=runs.get(cursor,run_id,self.user_id,lock=True)
                self.assertEqual(row['status'],'waiting_auth')
                self.assertEqual(len(row['context']['recall_pending']),6)
                self.assertEqual(row['context']['budget']['search_rounds'],3)
                self.assertEqual(row['context']['budget']['network_requests'],3)
                runs.update(cursor,row,status='pending',lease_until=time.time()+60)
            with patch.object(features,'prefetch_lyrics',side_effect=HTTPException(503,'歌词暂不可用')):
                runtime.run(run_id)
            self.assertEqual(searches.call_count,3)
        with library.database() as cursor:
            row=runs.get(cursor,run_id,self.user_id)
            self.assertEqual(row['status'],'finished',row['context'])
            self.assertEqual(row['context']['budget']['network_requests'],9)
            self.assertEqual(row['context']['budget']['search_rounds'],3)
            self.assertEqual(row['context']['recall_pending'],[])

    def test_legacy_short_list_recovers_complete_history_without_expired_allocation(self):
        self.fill(20)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.expire_cooldown()
        short=self.legacy_short(first)
        with library.database() as cursor:
            cursor.execute('UPDATE music_discovery_candidates SET validation_expires_at=0 WHERE user_id=%s',(self.user_id,))
            cursor.execute('UPDATE music_discovery_feed_items SET validated_at=%s WHERE feed_id IN (%s,%s)',
                (time.time()-7200,first['feed_id'],short['feed_id']))
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(result['feed_id'],first['feed_id'])
        self.assertEqual(len(result['tracks']),6)
        self.assertTrue(result['validation_stale'])
        key=first['tracks'][0]['key']
        self.client.put('/api/music/discovery/feedback',json={'track_key':key,'reason':'dislike'})
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertNotIn(key,[t['key'] for t in result['tracks']])
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_discovery_feeds WHERE user_id=%s',(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],2)

    def test_background_replaces_a_legacy_short_list_once_six_are_ready(self):
        self.fill(30)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.expire_cooldown()
        short=self.legacy_short(first)
        with patch.object(pool,'schedule'),patch.object(model,'complete',side_effect=AssertionError('model called')), \
             patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertNotIn(result['feed_id'],[first['feed_id'],short['feed_id']])
        self.assertEqual(len(result['tracks']),6)
        self.assertEqual({t['key'] for t in first['tracks'][:2]},
                         {t['key'] for t in result['tracks']}&{t['key'] for t in first['tracks']})

    def test_feedback_fills_one_gap_immediately_and_keeps_old_event_positions(self):
        self.fill(7)
        with patch.object(pool,'schedule'),patch.object(model,'complete',side_effect=AssertionError('model called')):
            first=self.next().json()
            key=first['tracks'][0]['key']
            payload={'track_key':key,'reason':'today','scope':'recording'}
            response=self.client.put('/api/music/discovery/feedback',json=payload)
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()['feed']
            self.assertEqual(len(result['tracks']),6)
            self.assertEqual([t['key'] for t in result['tracks'][:5]],[t['key'] for t in first['tracks'][1:]])
            self.assertNotIn(key,[t['key'] for t in result['tracks']])
            repeated=self.client.put('/api/music/discovery/feedback',json=payload).json()['feed']
            self.assertEqual(result['feed_id'],repeated['feed_id'])
            self.assertEqual(len(repeated['tracks']),6)
        # Existing players can still send events against the original list/position.
        import uuid
        track=first['tracks'][1]
        response=self.client.post('/api/music/discovery/events',json={'events':[{
            'event_id':str(uuid.uuid4()),'event_type':'exposure','origin':'web','track_key':track['key'],
            'feed_id':first['feed_id'],'position':track['discovery']['position'],'occurred_at':time.time()}]})
        self.assertEqual(response.status_code,200,response.text)

    def test_feedback_retains_published_songs_after_validation_expiry(self):
        self.fill(7)
        with patch.object(pool,'schedule'):
            first=self.next().json()
            with library.database() as cursor:
                cursor.execute('UPDATE music_discovery_feed_items SET validated_at=%s WHERE feed_id=%s',
                               (time.time()-7200,first['feed_id']))
            result=self.client.put('/api/music/discovery/feedback',json={
                'track_key':first['tracks'][0]['key'],'reason':'today'}).json()['feed']
        self.assertEqual(len(result['tracks']),6)
        self.assertEqual(sum(t['discovery']['validation_stale'] for t in result['tracks']),5)

    def test_empty_pool_automatically_repairs_feedback_gap_when_one_candidate_arrives(self):
        self.fill(6)
        with patch.object(pool,'schedule') as schedule:
            first=self.next().json()
            result=self.client.put('/api/music/discovery/feedback',json={
                'track_key':first['tracks'][0]['key'],'reason':'today'}).json()['feed']
            self.assertEqual(len(result['tracks']),5)
            schedule.assert_called()
        self.fill(7)
        with patch.object(pool,'schedule'),patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(len(result['tracks']),6)
        self.assertEqual({t['key'] for t in first['tracks'][1:]},{t['key'] for t in result['tracks'][:5]})

    def test_concurrent_feedback_and_reads_keep_six_unique_songs(self):
        from backend.modules.music.discovery import api
        self.fill(8)
        with patch.object(pool,'schedule'):
            first=self.next().json()
            principal=accounts.current()
            def reduce(index):
                marker=accounts.principal.set(principal)
                try:
                    return api.feedback(models.Feedback(track_key=first['tracks'][index]['key'],reason='today'))['feed']
                finally: accounts.principal.reset(marker)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(reduce,range(2)))
            self.assertTrue(all(len(result['tracks'])==6 for result in results))
            result=self.client.get('/api/music/discovery/feed').json()
        keys={t['key'] for t in result['tracks']}
        self.assertEqual(len(keys),6)
        self.assertTrue({t['key'] for t in first['tracks'][2:]}<=keys)
        self.assertFalse({t['key'] for t in first['tracks'][:2]}&keys)

    def test_refill_completion_needs_only_one_candidate_to_repair_feedback_gap(self):
        self.fill(6)
        first=self.next().json()
        response=self.client.put('/api/music/discovery/feedback',json={
            'track_key':first['tracks'][0]['key'],'reason':'today'})
        self.assertEqual(len(response.json()['feed']['tracks']),5)
        with library.database() as cursor:
            cursor.execute("SELECT run_id FROM music_recommendation_runs WHERE user_id=%s AND status='pending'",(self.user_id,))
            row=runs.get(cursor,cursor.fetchone()['run_id'],lock=True)
            candidate=self.track()|{'identifier':'one-new-song','title':'A new song','artist':'New artist',
                'analysis':{'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}},
                'validation':{'identity':True,'playable':True,'full':True,'checked_at':time.time()}}
            runs.update(cursor,row,status='running',lease_until=time.time()+60,context=row['context']|{'candidates':[candidate]})
        from backend.modules.music.discovery import service
        service.finish(row['run_id'])
        with library.database() as cursor:
            result=feeds.public(cursor,self.user_id,feeds.latest(cursor,self.user_id))
        self.assertEqual(len(result['tracks']),6)
        self.assertEqual([t['key'] for t in result['tracks'][:5]],[t['key'] for t in first['tracks'][1:]])
        self.assertEqual(result['tracks'][-1]['title'],'A new song')

    def legacy_duplicates(self, first):
        with library.database() as cursor:
            for i,track in enumerate(first['tracks'][:2]):
                snapshot=track|{'title':'刀马旦','artist':'周杰伦 / 李玟' if i==0 else '李玟 / 周杰伦','duration':192.075}
                # Simulate historical metadata with two independent recording keys.
                cursor.execute('UPDATE music_discovery_identities SET canonical=%s WHERE user_id=%s AND track_key=%s',
                               (json.dumps(snapshot),self.user_id,track['key']))
                cursor.execute('UPDATE music_discovery_feed_items SET track_snapshot=%s WHERE feed_id=%s AND track_key=%s',
                               (json.dumps(snapshot),first['feed_id'],track['key']))

    def test_existing_duplicate_list_is_deduplicated_and_filled_on_read(self):
        self.fill(7)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.legacy_duplicates(first)
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(len(result['tracks']),6)
        self.assertEqual(sum(t['title']=='刀马旦' for t in result['tracks']),1)
        self.assertEqual({t['key'] for t in first['tracks'][2:]},
                         {t['key'] for t in result['tracks']}&{t['key'] for t in first['tracks'][2:]})
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_discovery_feed_items WHERE feed_id=%s',(first['feed_id'],))
            self.assertEqual(cursor.fetchone()['n'],6)

    def test_legacy_feedback_and_recent_allocations_apply_to_all_recording_aliases(self):
        self.fill(8)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.legacy_duplicates(first)
        with library.database() as cursor:
            cursor.execute('SELECT * FROM music_discovery_identities WHERE user_id=%s AND track_key=%s',
                           (self.user_id,first['tracks'][1]['key']))
            other=cursor.fetchone()
            self.assertTrue(store.excluded(cursor,self.user_id,other))
            store.feedback(cursor,self.user_id,models.Feedback(track_key=first['tracks'][0]['key'],reason='dislike'))
            self.assertTrue(store.blocked(cursor,self.user_id,other['recording_key'],None))
            self.assertTrue(store.excluded(cursor,self.user_id,other,replay=True))
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(len(result['tracks']),6)
        self.assertNotIn('刀马旦',[t['title'] for t in result['tracks']])

    def test_new_same_provider_ids_share_identity_despite_credit_order(self):
        with library.database() as cursor:
            a=identity.ensure(cursor,self.user_id,self.track()|{'title':'刀马旦','artist':'周杰伦 / 李玟','identifier':'one'})
            b=identity.ensure(cursor,self.user_id,self.track()|{'title':'刀马旦','artist':'李玟 / 周杰伦','identifier':'two'})
        self.assertNotEqual(a['track_key'],b['track_key'])
        self.assertEqual(a['recording_key'],b['recording_key'])

    def test_favorites_exclude_historical_recording_aliases_even_on_replay(self):
        self.fill(6)
        with patch.object(pool,'schedule'):
            first=self.next().json()
        self.legacy_duplicates(first)
        with library.database() as cursor:
            track=first['tracks'][0]
            key=library.save_track(cursor,library.TrackInput.model_validate(track))
            cursor.execute('INSERT INTO music_favorites (user_id,track_id,created_at) VALUES (%s,%s,%s)',
                           (self.user_id,key,time.time()))
            cursor.execute('SELECT * FROM music_discovery_identities WHERE user_id=%s AND track_key=%s',
                           (self.user_id,first['tracks'][1]['key']))
            self.assertTrue(store.excluded(cursor,self.user_id,cursor.fetchone(),replay=True))

    def test_refill_completion_repairs_a_legacy_short_list(self):
        self.fill(30)
        first=self.next().json()
        self.expire_cooldown()
        short=self.legacy_short(first)
        with library.database() as cursor:
            cursor.execute('SELECT run_id FROM music_recommendation_runs WHERE user_id=%s AND status=\'pending\'',(self.user_id,))
            run_id=cursor.fetchone()['run_id']
            row=runs.get(cursor,run_id,lock=True)
            runs.update(cursor,row,status='running',lease_until=time.time()+60,context=row['context']|{'candidates':pool.tracks(cursor,self.user_id)[:6]})
        from backend.modules.music.discovery import service
        service.finish(run_id)
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertNotIn(result['feed_id'],[first['feed_id'],short['feed_id']])
        self.assertEqual(len(result['tracks']),6)

    def test_exhausted_network_budget_reports_midnight_and_resumes_next_day(self):
        self.fill(20);self.next()
        with library.database() as cursor:
            cursor.execute('SELECT run_id FROM music_recommendation_runs WHERE user_id=%s',(self.user_id,))
            row=runs.get(cursor,cursor.fetchone()['run_id'],lock=True)
            runs.update(cursor,row,status='finished',context=row['context']|{
                'budget':{'network_requests':5000},'network_by_date':{str(policy.today()):5000}})
            usage.sync_candidate(cursor,self.user_id)
            cursor.execute('UPDATE music_discovery_daily_usage SET next_refill_at=0 WHERE user_id=%s',(self.user_id,))
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertFalse(result['refreshing'])
        self.assertEqual(result['refill']['state'],'budget_exhausted')
        self.assertEqual(result['refill']['retry_at'],policy.day_end(time.time()))
        with patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_recommendation_runs WHERE user_id=%s',(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],1)
        from datetime import timedelta
        tomorrow=policy.today()+timedelta(days=1)
        with patch.object(policy,'today',return_value=tomorrow),patch.object(profile_runtime,'_maintenance_after',0):
            profile_runtime.maintain_candidates()
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM music_recommendation_runs WHERE user_id=%s AND status=\'pending\'',(self.user_id,))
            self.assertEqual(cursor.fetchone()['n'],1)

    def test_feed_idempotency_returns_original_list_after_another_change(self):
        self.fill(30)
        first=self.next('original-request').json()
        self.expire_cooldown()
        second=self.next('another-request').json()
        self.assertNotEqual(first['feed_id'],second['feed_id'])
        repeated=self.next('original-request').json()
        self.assertEqual(repeated['feed_id'],first['feed_id'])

    def test_expired_validation_is_never_published_and_save_is_idempotent(self):
        self.fill(20)
        first=self.next().json()
        saved=self.client.post('/api/music/discovery/feeds/'+first['feed_id']+'/save',json={})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.client.post('/api/music/discovery/feeds/'+first['feed_id']+'/save',json={}).json()['playlist_id'],saved.json()['playlist_id'])
        self.expire_cooldown()
        with library.database() as cursor: cursor.execute('UPDATE music_discovery_candidates SET validation_expires_at=0 WHERE user_id=%s',(self.user_id,))
        result=self.next().json();self.assertEqual(result['feed_id'],first['feed_id'])
        with library.database() as cursor: cursor.execute('UPDATE music_discovery_feed_items SET validated_at=%s WHERE feed_id=%s',(time.time()-7200,first['feed_id']))
        result=self.client.get('/api/music/discovery/feed').json()
        self.assertEqual(len(result['tracks']),6)
        self.assertTrue(result['validation_stale'])
        self.assertTrue(all(t['discovery']['validation_stale'] for t in result['tracks']))
        with library.database() as cursor: cursor.execute('UPDATE music_discovery_feed_items SET validated_at=0 WHERE feed_id=%s',(first['feed_id'],))
        self.assertFalse(self.client.get('/api/music/discovery/feed').json()['tracks'])

    def test_partial_settings_keep_learning_and_old_client_fields(self):
        response=self.client.patch('/api/music/discovery/profile',json={'profile_learning_enabled':False})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['settings']['languages'],['中文'])
        response=self.client.patch('/api/music/discovery/profile',json={'long_term_need':'轻快'})
        self.assertFalse(response.json()['learning_enabled'])
        self.assertEqual(response.json()['settings']['languages'],['中文'])

    def test_profile_correction_rejects_old_proposal_and_restore_keeps_confirmed(self):
        row=self.job()
        result=self.client.post('/api/music/discovery/profile/corrections',json={'expected_agent_revision':0,
            'item':{'dimension':'language','value':'中文','preference':.8}})
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['agent_revision'],1)
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            profile_store.apply(cursor,self.user_id,row['run_id'],'test-worker')
            self.assertEqual(profile_store.get_run(cursor,self.user_id,row['run_id'])['status'],'stale')
        self.assertEqual(self.client.post('/api/music/discovery/profile/reset',json={'expected_agent_revision':0}).status_code,409)
        reset=self.client.post('/api/music/discovery/profile/reset',json={'expected_agent_revision':1})
        self.assertEqual(reset.status_code,200,reset.text)
        restored=self.client.post('/api/music/discovery/profile/restore',json={'expected_agent_revision':2,'version':1})
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual(restored.json()['agent_state']['items'][0]['source'],'user')
        self.assertEqual(restored.json()['agent_revision'],3)

    def test_profile_publication_and_lease_fencing(self):
        row=self.job();snapshot=row['input_snapshot']
        proposal={'summary':'喜欢 Artist','items':[{'dimension':'artist','value':'Artist','preference':.8,
            'evidence_refs':[e['ref'] for e in snapshot['evidence']]}]}
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            current=profile_store.get_run(cursor,self.user_id,row['run_id'],True)
            profile_store.update_run(cursor,current,proposal=proposal)
        with self.assertRaises(HTTPException),library.database() as cursor:
            usage.lock(cursor,self.user_id);profile_store.apply(cursor,self.user_id,row['run_id'],'old-worker')
        with library.database() as cursor:
            usage.lock(cursor,self.user_id);profile_store.apply(cursor,self.user_id,row['run_id'],'test-worker')
            p=store.profile(cursor,self.user_id)
            self.assertEqual(p['agent_revision'],1);self.assertEqual(len(p['agent_state']['items']),1)
            self.assertEqual(profile_store.get_run(cursor,self.user_id,row['run_id'])['status'],'succeeded')

    def test_unknown_profile_usage_blocks_candidates_across_dates(self):
        row=self.job()
        request=profile_budget.reserve(self.user_id,row['run_id'],1000,'input',time.time()+30)
        profile_budget.dispatched(self.user_id,row['run_id'],request['id'])
        profile_budget.settle(self.user_id,row['run_id'],request['id'])
        from backend.modules.music.discovery import recall
        batch,_=store.create(models.CreateBatch(need='中文'),'legacy-budget-request',recall.skill()['version'])
        with library.database() as cursor:
            current=runs.get(cursor,batch['run_id'],lock=True)
            runs.update(cursor,current,status='running',lease_until=time.time()+60)
        with self.assertRaisesRegex(Exception,'两类新增付费'):
            budget.reserve(batch['run_id'],1000,'semantic','candidate',time.time()+30)
        profile_budget.settle(self.user_id,row['run_id'],request['id'],actual=1100)
        reservation=budget.reserve(batch['run_id'],1000,'semantic','candidate',time.time()+30)
        budget.settle(batch['run_id'],reservation['id'],actual=800)
        with library.database() as cursor:
            usage.lock(cursor,self.user_id);usage.sync_candidate(cursor,self.user_id)
            self.assertEqual(usage.current(cursor,self.user_id)['actual_tokens'],800)

    def test_candidate_unknown_blocks_profile_and_fixed_day_does_not_reset(self):
        from backend.modules.music.discovery import recall
        batch,_=store.create(models.CreateBatch(need='中文'),'candidate-first-request',recall.skill()['version'])
        with library.database() as cursor:
            current=runs.get(cursor,batch['run_id'],lock=True);runs.update(cursor,current,status='running',lease_until=time.time()+60)
        request=budget.reserve(batch['run_id'],1000,'semantic','input',time.time()+30)
        budget.settle(batch['run_id'],request['id'])
        row=self.job()
        with self.assertRaisesRegex(Exception,'两类新增付费'):
            profile_budget.reserve(self.user_id,row['run_id'],1000,'profile-input',time.time()+30)
        budget.settle(batch['run_id'],request['id'],actual=900)
        request=profile_budget.reserve(self.user_id,row['run_id'],1000,'profile-input',time.time()+30)
        profile_budget.settle(self.user_id,row['run_id'],request['id'],actual=1200)
        from datetime import timedelta
        tomorrow=policy.today()+timedelta(days=1)
        with patch.object(policy,'today',return_value=tomorrow):
            later=profile_budget.reserve(self.user_id,row['run_id'],1000,'different-input',time.time()+30)
            profile_budget.settle(self.user_id,row['run_id'],later['id'],actual=500)
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'])
            self.assertEqual(current['accounting_date'],policy.today())
            self.assertEqual(profile_budget.totals(current['context'])['actual'],1700)

    def test_revision_alone_does_not_trigger_and_cancel_prevents_publication(self):
        with library.database() as cursor:
            usage.lock(cursor,self.user_id)
            cursor.execute('UPDATE system_settings SET config=%s WHERE id=1',(json.dumps(accounts.DEFAULT_SETTINGS|{'music_profile_agent_enabled':True}),))
            cursor.execute('UPDATE music_discovery_profiles SET evidence_revision=999 WHERE user_id=%s',(self.user_id,))
            self.assertIsNone(profile_store.schedule(cursor,self.user_id))
        row=self.job()
        profile_runtime.cancel_user(self.user_id)
        with library.database() as cursor:
            self.assertEqual(profile_store.get_run(cursor,self.user_id,row['run_id'])['status'],'cancelled')
            self.assertEqual(store.profile(cursor,self.user_id)['agent_revision'],0)

    def test_mock_model_tool_proposal_is_accounted_and_published_once(self):
        row=self.job()
        proposal={'summary':'喜欢 Artist','items':[{'dimension':'artist','value':'Artist','preference':.8,
            'evidence_refs':[e['ref'] for e in row['input_snapshot']['evidence']]}]}
        async def response(*args):
            await model._mark_dispatched(None)
            return {'role':'assistant','content':'','finish_reason':'tool_calls','tool_calls':[{
                'id':'profile-call','type':'function','function':{'name':'propose_profile_changes','arguments':json.dumps(proposal)}}]},700
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'],True)
            profile_store.update_run(cursor,current,status='pending',worker_id=None,lease_until=None,
                context=current['context']|{'not_before':0})
        with patch.object(profile_agent.ai,'load_config',return_value={'model':'test','base_url':'https://example.invalid','api_key':'test'}),patch.object(model,'_request',side_effect=response) as request:
            profile_runtime.run_profile(self.user_id,row['run_id'])
            profile_runtime.run_profile(self.user_id,row['run_id'])
            self.assertEqual(request.call_count,1)
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'])
            self.assertEqual(current['status'],'succeeded',current)
            self.assertTrue(current['context']['paid'])
            self.assertEqual(profile_budget.totals(current['context'])['actual'],700)

    def test_restart_recovers_saved_proposal_without_repeating_model(self):
        row=self.job()
        proposal={'summary':'喜欢 Artist','items':[{'dimension':'artist','value':'Artist','preference':.8,
            'evidence_refs':[e['ref'] for e in row['input_snapshot']['evidence']]}]}
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'],True)
            profile_store.update_run(cursor,current,proposal=proposal,lease_until=time.time()-1,
                context=current['context']|{'proposal_ready':True,'not_before':0,'model_requests':[{
                    'id':'paid','signature':'old','state':'settled','actual':400,'reserved':1000}]})
        with library.database() as cursor: profile_runtime.recover_profiles(cursor)
        with patch.object(profile_agent,'run',side_effect=AssertionError('model repeated')):
            profile_runtime.run_profile(self.user_id,row['run_id'])
        with library.database() as cursor:
            self.assertEqual(profile_store.get_run(cursor,self.user_id,row['run_id'])['status'],'succeeded')

    def test_expired_lease_retains_unknown_usage_and_old_worker_cannot_reserve(self):
        row=self.job();request=profile_budget.reserve(self.user_id,row['run_id'],1000,'request',time.time()+30)
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'],True)
            profile_store.update_run(cursor,current,lease_until=time.time()-1)
        with library.database() as cursor: profile_runtime.recover_profiles(cursor)
        with library.database() as cursor:
            current=profile_store.get_run(cursor,self.user_id,row['run_id'])
            self.assertEqual(current['status'],'usage_pending')
            self.assertEqual(profile_budget.totals(current['context'])['unconfirmed'],1000)
        with self.assertRaises(Exception): profile_budget.reserve(self.user_id,row['run_id'],1000,'new',time.time()+30)
        profile_budget.settle(self.user_id,row['run_id'],request['id'],actual=900)
        with library.database() as cursor:
            self.assertEqual(profile_store.get_run(cursor,self.user_id,row['run_id'])['status'],'interrupted')
