import json
import secrets
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi import HTTPException
from backend.tests.music import test_music_agent as agent_tests
from backend.modules.music.discovery import identity, store, models, profile, policy, ranking, recall, service
from backend.modules.music import library
from backend.modules.music.agent import store as runs, runtime
from backend.modules.accounts import service as accounts

class AlgorithmTests(unittest.TestCase):
    def candidate(self,index,artist,score=.8):
        return dict(key=str(index),title=f'Song {index}',duration=180,artist=artist,
            recording_key=str(index),work_key=None,selection={'match_score':score})

    def test_each_individual_artist_counts_in_collaborations_and_decorated_names(self):
        names=['周杰伦','周杰伦 / Lara梁心颐','蔡依林 / 周杰伦','周杰伦♚','周杰伦',
            '张韶涵','张韶涵','IU','IU','BIGBANG','BIGBANG']
        selected=ranking.select([self.candidate(i,name) for i,name in enumerate(names)],familiar_artists=['周杰伦'])
        self.assertEqual(len(selected),6)
        counts={}
        for track in selected:
            for name in identity.artist_members(track['artist']): counts[name]=counts.get(name,0)+1
        self.assertTrue(all(count<=2 for count in counts.values()),counts)
        self.assertLessEqual(counts.get('周杰伦',0),2)
        self.assertNotEqual(identity.artists('周杰伦'),identity.artists('周杰伦♚'))

    def test_collaboration_with_familiar_artist_is_not_exploration(self):
        tracks=[self.candidate(0,'Lara梁心颐 / 周杰伦',.95),self.candidate(1,'周杰伦♚',.94),
            self.candidate(2,'陌生歌手',.6)]
        selected=ranking.select(tracks,target=3,exploration=.3,familiar_artists={'周杰伦':40})
        self.assertEqual(selected[0]['artist'],'陌生歌手')
        self.assertTrue(selected[0]['selection']['exploration'])
        self.assertTrue(all(not t['selection']['exploration'] for t in selected[1:]))

    def test_retained_collaborations_and_exposure_use_individual_artists(self):
        retained=[self.candidate(0,'周杰伦 / Lara'),self.candidate(1,'周杰伦'),self.candidate(2,'蔡依林 / 周杰伦')]
        selected=ranking.select([self.candidate(3,'IU'),self.candidate(4,'EXO')],retained=retained,
            familiar_artists=['周杰伦'],exploration=0)
        self.assertEqual([t['key'] for t in selected[:2]],['0','1'])
        self.assertNotIn('2',[t['key'] for t in selected])
        self.assertFalse(selected[0]['selection']['exploration'])
        selected=ranking.select([self.candidate(5,'Jay / Guest',.9),self.candidate(6,'Other',.85)],
            target=1,exploration=0,exposure={'Jay':5})
        self.assertEqual(selected[0]['artist'],'Other')

    def test_unknown_language_match_cannot_become_full_seed_similarity(self):
        p={'settings':models.Settings().model_dump(),'long_term':{'features':{'artist':{'周杰伦':40},'language':{'中文':40}}},
            'short_term':{},'seeds':[{'artist':'周杰伦','features':{'language':'中文'}}]}
        track={'key':'x','artist':'陌生歌手','analysis':{'features':{'language':'中文'},
            'evidence':{'fields':{'language':{'confidence':.8}}}}}
        self.assertIsNone(ranking.score(track,p,{}))
        familiar=ranking.score(track|{'artist':'周杰伦'},p,{})
        self.assertIsNotNone(familiar)
        self.assertLess(familiar['match_score'],.8)
        self.assertLess(familiar['components']['seed_similarity'],1)
        self.assertIn('证据有限',familiar['reason'])
        explicit=ranking.score(track,p,{'languages':['中文']})
        self.assertIsNotNone(explicit)
        self.assertIsNone(explicit['components']['seed_similarity'])
        self.assertIn('明确条件',explicit['reason'])

    def test_weaker_favorite_and_verified_music_can_still_qualify(self):
        p={'settings':models.Settings().model_dump(),
            'long_term':{'features':{'artist':{'周杰伦':40,'IU':5},'language':{'中文':40},'moods':{'平静':10}}},
            'short_term':{},'seeds':[{'artist':'周杰伦','features':{'language':'中文','moods':['平静']}}]}
        track={'key':'x','artist':'IU','analysis':{'features':{'language':'中文'},
            'evidence':{'fields':{'language':{'confidence':.8}}}}}
        self.assertIsNotNone(ranking.score(track,p,{}))
        track['artist']='陌生歌手'
        track['analysis']['features']['moods']=['平静']
        track['analysis']['evidence']['fields']['moods']={'confidence':.8}
        self.assertIsNotNone(ranking.score(track,p,{}))
        self.assertLessEqual(ranking.similarity({'language':'中文'},{'language':{'中文':10}}),.3)

    def test_excluded_artists_cannot_return_in_collaboration(self):
        p={'settings':models.Settings(excluded_artists=['周杰伦']).model_dump(),'long_term':{},'short_term':{},'seeds':[]}
        self.assertIsNone(ranking.score({'key':'x','artist':'Lara / 周杰伦♚'},p,{'languages':['中文']}))

    def test_recall_uses_all_favorite_artists_instead_of_first_four_tracks(self):
        p={'settings':models.Settings().model_dump(),'long_term':{'features':{'artist':{
            '周杰伦':40,'BIGBANG':25,'IU':20,'薛之谦':15,'EXO':10,'周深':5}}},
            'seeds':[{'artist':'周杰伦'}]*8+[{'artist':'张韶涵'}]}
        plan=recall.baseline({},p)
        self.assertEqual(len(plan['routes']),5)
        self.assertEqual(len({r['query'] for r in plan['routes']}),5)
        self.assertIn('iu',[r['query'] for r in plan['routes']])
        self.assertIn('exo',[r['query'] for r in plan['routes']])
        self.assertIn('张韶涵',[r['query'] for r in recall.artist_routes(p)])

    def test_credit_order_deduplication_preserves_versions_and_unknowns(self):
        track=dict(title='刀马旦',artist='周杰伦 / 李玟',duration=192.075)
        self.assertTrue(identity.same_recording(track,track|{'artist':'李玟 / 周杰伦','duration':192}))
        for changes in ({'title':'刀马旦 (Live)'},{'artist':'其他歌手'},{'duration':None},{'duration':195}):
            with self.subTest(changes=changes):
                self.assertFalse(identity.same_recording(track,track|changes))
        tracks=[track|dict(key=str(i),recording_key=str(i),work_key=None,selection={'match_score':.9},
            artist=artist) for i,artist in enumerate(['周杰伦 / 李玟','李玟 / 周杰伦'])]
        self.assertEqual(len(ranking.select(tracks)),1)

    def test_legacy_aliases_do_not_chain_duration_tolerances(self):
        rows=[dict(recording_key=str(i),canonical={'title':'Song','artist':'Artist','duration':duration})
              for i,duration in enumerate([180,180.8,181.6])]
        catalog=identity.Catalog(rows)
        self.assertEqual(catalog.aliases('0'),('0','1'))
        self.assertEqual(catalog.aliases('2'),('2',))

    def test_completion_requires_actual_time_and_coverage(self):
        event=dict(duration_ms=180000,covered_ms=179000,listened_ms=1000,terminal_reason='ended')
        self.assertFalse(profile.completed(event))
        self.assertTrue(profile.completed(event|{'listened_ms':150000,'covered_ms':150000}))
        self.assertFalse(profile.completed(event|{'duration_ms':None}))
        self.assertFalse(profile.completed(event|{'listened_ms':180000,'terminal_reason':'error'}))

    def test_unknown_features_never_become_match_evidence(self):
        p={'settings':models.Settings().model_dump(),'long_term':{},'short_term':{},'seeds':[]}
        self.assertIsNone(ranking.score({'key':'x','artist':'歌手'},p,{}))

    def test_versions_and_artist_diversity(self):
        tracks=[dict(key=str(i),artist='a' if i<4 else 'b',recording_key=str(i//2),work_key=None,
            selection={'match_score':.8}) for i in range(6)]
        selected=ranking.select(tracks)
        self.assertEqual(len(selected),3)
        self.assertEqual(len({t['recording_key'] for t in selected}),3)

    def test_selectable_inventory_counts_batches_and_deduplicates_across_them(self):
        def track(index,artist):
            return dict(key=str(index),title=f'Song {index}',duration=180,artist=artist,
                recording_key=str(index),work_key=None,selection={'match_score':.8})
        tracks=[track(i,'A') for i in range(11)]+[track(20,'B'),track(21,'B'),track(30,'C')]
        self.assertEqual(ranking.selectable_count(tracks),5)
        self.assertEqual(ranking.selectable_count(tracks+[track(31,'C')]),8)
        duplicate=tracks[-1]|{'key':'alias','recording_key':'alias'}
        self.assertEqual(ranking.selectable_count(tracks+[duplicate]),5)
        balanced=[track(i,artist) for i,artist in enumerate(['A','B','C']*4)]
        self.assertEqual(ranking.selectable_count(balanced),12)
        self.assertEqual(ranking.selectable_count(balanced+[balanced[0]|{'key':'duplicate','recording_key':'duplicate'}]),12)

# Reuse the isolated Flyway/session fixture, not the inherited scene test cases.
class DiscoveryIntegrationTests(unittest.TestCase):
    setUpClass=classmethod(agent_tests.AgentIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(agent_tests.AgentIntegrationTests.tearDownClass.__func__)
    tearDown=agent_tests.AgentIntegrationTests.tearDown
    def setUp(self):
        agent_tests.AgentIntegrationTests.setUp(self)
        from backend.modules.music.discovery import recall
        self.stack.enter_context(patch.object(recall,'advise',return_value={}))
        with library.database() as cursor:
            for table in ('music_discovery_events','music_discovery_feedback','music_discovery_identities','music_discovery_profiles'):
                cursor.execute('DELETE FROM '+table)
            config=accounts.DEFAULT_SETTINGS|{'music_daily_enabled':True,'music_agent_enabled':True}
            cursor.execute('UPDATE system_settings SET config=%s WHERE id=1',(json.dumps(config),))
            store.profile(cursor,self.user_id)
            cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(json.dumps(models.Settings(languages=['中文']).model_dump()),self.user_id))

    def create(self,key=None,need='中文'):
        return self.client.post('/api/music/discovery/batches',json={'need':need},headers={'Idempotency-Key':key or secrets.token_hex(12)})

    def stop(self,batch):
        self.client.post('/api/music/discovery/batches/'+batch['batch_id']+'/cancel')

    def expire_refill(self):
        with library.database() as cursor:
            cursor.execute('UPDATE music_discovery_daily_usage SET next_refill_at=0 WHERE user_id=%s',(self.user_id,))

    def track(self):
        return dict(title='Test',artist='Artist',provider='qq',identifier='004test',webpage_url='https://y.qq.com/n/ryqq/songDetail/004test',duration=180)

    def test_quota_idempotency_and_scene_bypass(self):
        first=self.create('fixed-key-123');self.assertEqual(first.status_code,201,first.text)
        batch=first.json();again=self.create('fixed-key-123')
        self.assertEqual(batch['batch_id'],again.json()['batch_id'])
        self.assertEqual(self.create('fixed-key-123',need='英文').status_code,409)
        self.stop(batch)
        for suffix in ('retry','continue','fill'):
            data={'result_version':1} if suffix=='fill' else {}
            response=self.client.post('/api/music/recommendations/'+batch['run_id']+'/'+suffix,json=data,headers={'Idempotency-Key':secrets.token_hex(12)})
            self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.create().status_code,429)
        for _ in range(4):
            self.expire_refill()
            response=self.create();self.assertEqual(response.status_code,201,response.text)
            self.stop(response.json())

    def test_events_reordering_feedback_and_no_history_fabrication(self):
        playback=str(uuid.uuid4())
        def event(seq,**kwargs):
            return dict(event_id=str(uuid.uuid4()),track=self.track(),playback_id=playback,seq=seq,event_type='playback_progress',
                origin='web',occurred_at=time.time(),listened_ms=1000*seq,covered_ms=1000*seq,duration_ms=180000,**kwargs)
        start=event(0);start.update(event_type='playback_start',occurred_at=time.time()-10)
        newest=event(2)
        response=self.client.post('/api/music/discovery/events',json={'events':[start,newest,newest,event(1)]})
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['accepted'],2)
        with library.database() as cursor:
            cursor.execute('SELECT track_key FROM music_discovery_identities WHERE user_id=%s',(self.user_id,));key=cursor.fetchone()['track_key']
        response=self.client.put('/api/music/discovery/feedback',json={'track_key':key,'scope':'work','reason':'fatigue'})
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['scope'],'recording')
        with library.database() as cursor:
            cursor.execute('SELECT * FROM music_discovery_feedback');f=cursor.fetchone()
            self.assertAlmostEqual(f['expires_at']-time.time(),90*86400,delta=10)
            self.assertTrue(store.blocked(cursor,self.user_id,f['target_key'],None))
        response=self.client.request('DELETE','/api/music/discovery/feedback',json={'track_key':key,'scope':'work','reason':'fatigue'})
        self.assertEqual(response.status_code,200,response.text)

    def test_event_size_and_foreign_batch_rejected(self):
        response=self.client.post('/api/music/discovery/events',content=b' '*65537)
        self.assertEqual(response.status_code,413)
        response=self.client.post('/api/music/discovery/events',json={'events':[dict(event_id=str(uuid.uuid4()),event_type='exposure',
            origin='web',track=self.track(),batch_id='0'*32,position=1,occurred_at=time.time())]})
        self.assertEqual(response.status_code,422,response.text)

    def test_concurrent_same_key_creates_one_batch(self):
        principal=accounts.current()
        def create(_):
            token=accounts.principal.set(principal)
            try: return store.create(models.CreateBatch(need='中文'),'concurrent-key','test')[0]['batch_id']
            finally: accounts.principal.reset(token)
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids=list(executor.map(create,range(4)))
        self.assertEqual(len(set(ids)),1)

    def test_partial_publish_filters_feedback_and_saves(self):
        batch=self.create().json()
        from backend.modules.music.discovery import identity
        with library.database() as cursor:
            row=runs.get(cursor,batch['run_id'],self.user_id,lock=True)
            ident=identity.ensure(cursor,self.user_id,self.track())
            key=ident['track_key']
            p=store.profile(cursor,self.user_id)
            p['settings']['languages']=['中文']
            cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(json.dumps(p['settings']),self.user_id))
            track=self.track()|dict(key=key,recording_key=ident['recording_key'],work_key=None,
                analysis={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}},
                selection={},validation={'full':True,'playable':True,'identity':True,'checked_at':time.time()})
            runs.update(cursor,row,status='running',worker_id='test',lease_until=time.time()+60,
                context=row['context']|{'candidates':[track],'active_started_at':time.time()})
        service.finish(batch['run_id'])
        result=self.client.get('/api/music/discovery/batches/'+batch['batch_id']).json()
        self.assertEqual(result['status'],'partial',result);self.assertEqual(len(result['tracks']),1)
        self.client.put('/api/music/discovery/feedback',json={'track_key':key,'reason':'dislike'})
        result=self.client.get('/api/music/discovery/batches/'+batch['batch_id']).json()
        self.assertEqual(len(result['tracks']),0)
        saved=self.client.post('/api/music/discovery/batches/'+batch['batch_id']+'/save',json={'result_version':1})
        self.assertEqual(saved.status_code,422,saved.text)

    def test_runtime_model_failure_preserves_algorithm_result(self):
        from backend.modules.music.discovery import recall
        from backend.modules.music.agent import features, validation
        batch=self.create().json()
        with library.database() as cursor:
            store.profile(cursor,self.user_id)
            settings=models.Settings(languages=['中文']).model_dump()
            cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(json.dumps(settings),self.user_id))
        track=library.TrackInput.model_validate(self.track())
        with patch.object(features,'prefetch_lyrics'), patch.object(recall,'plan',return_value={'routes':[{'query':'中文','type':'exploration'}],'languages':['中文'],'excluded_artists':[]}), \
             patch.object(service.sources,'_search',return_value=[track]), \
             patch.object(features,'analyze',return_value={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}}), \
             patch.object(validation,'validate',return_value={'identity':True,'full':True,'playable':True,'checked_at':time.time()}):
            runtime.run(batch['run_id'])
        result=self.client.get('/api/music/discovery/batches/'+batch['batch_id']).json()
        self.assertEqual(result['status'],'partial',result)
        self.assertEqual(len(result['tracks']),1,result)
        self.assertIn('exploration',result['tracks'][0]['discovery'])
        self.stop(batch)
        self.expire_refill()
        second=self.create().json()
        from backend.modules.music.agent.faults import ModelFault
        other=library.TrackInput.model_validate(self.track()|{'identifier':'004other','title':'Other'})
        config=accounts.settings()|{'ai_api_key':'test-only'}
        with patch.object(accounts,'settings',return_value=config), patch.object(features,'prefetch_lyrics'), \
             patch.object(recall,'plan',side_effect=ModelFault('用量未知')), \
             patch.object(service.sources,'_search',return_value=[other]) as search, \
             patch.object(features,'analyze',return_value={'features':{'language':'中文'},'evidence':{'fields':{'language':{'confidence':.9}}}}), \
             patch.object(validation,'validate',return_value={'identity':True,'full':True,'playable':True,'checked_at':time.time()}), \
             patch.object(recall,'advise') as advise:
            runtime.run(second['run_id'])
            self.assertTrue(search.called)
            advise.assert_not_called()
        result=self.client.get('/api/music/discovery/batches/'+second['batch_id']).json()
        self.assertEqual(result['status'],'partial',result)
        self.assertEqual(len(result['tracks']),1)

    def test_automatic_today_reuses_latest_even_after_failure_or_manual_change(self):
        first=self.client.post('/api/music/discovery/today',json={})
        self.assertEqual(first.status_code,200,first.text)
        self.stop(first.json())
        again=self.client.post('/api/music/discovery/today',json={})
        self.assertEqual(again.json()['batch_id'],first.json()['batch_id'])
        self.expire_refill()
        manual=self.create().json()
        self.assertEqual(self.client.post('/api/music/discovery/today',json={}).json()['batch_id'],manual['batch_id'])
        self.assertIsNone(self.client.get('/api/music/discovery/today').json()['remaining_batches'])

    def test_concurrent_automatic_entry_creates_one_task(self):
        principal=accounts.current()
        def enter(_):
            token=accounts.principal.set(principal)
            try: return store.create(models.CreateBatch(),'automatic-test-key','test',automatic=True)[0]['batch_id']
            finally: accounts.principal.reset(token)
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids=list(executor.map(enter,range(4)))
        self.assertEqual(len(set(ids)),1)

    def test_interrupted_usage_exposed_without_retrying_or_resetting_quota(self):
        first=self.create().json()
        with library.database() as cursor:
            row=runs.get(cursor,first['run_id'],self.user_id,lock=True)
            runs.update(cursor,row,status='interrupted',worker_id=None,lease_until=None,
                context=row['context']|{'message':'服务执行中断','model_requests':[{'state':'unconfirmed','reserved':3000}]})
        response=self.client.get('/api/music/discovery/batches/'+first['batch_id']).json()
        self.assertEqual(response['status'],'interrupted')
        self.assertTrue(response['usage_pending'])
        self.assertIsNone(response['remaining_batches'])
        again=self.client.post('/api/music/discovery/today',json={}).json()
        self.assertEqual(again['batch_id'],first['batch_id'])
        self.assertTrue(again['usage_pending'])
        self.expire_refill()
        retry=self.create().json()
        self.assertIsNone(retry['remaining_batches'])
        self.assertNotEqual(retry['batch_id'],first['batch_id'])

    def test_cold_start_does_not_spend_quota(self):
        with library.database() as cursor:
            cursor.execute('UPDATE music_discovery_profiles SET settings=%s WHERE user_id=%s',(json.dumps(models.Settings().model_dump()),self.user_id))
        response=self.client.post('/api/music/discovery/today',json={})
        self.assertEqual(response.status_code,422,response.text)
        self.assertIsNone(self.client.get('/api/music/discovery/today').json()['remaining_batches'])

    def test_playback_events_reach_profile_and_change_ranking(self):
        refs=[]
        for i in range(3):
            event=dict(event_id=str(uuid.uuid4()),track=self.track()|{'identifier':f'event{i}','title':f'Song {i}'},playback_id=str(uuid.uuid4()),seq=1,
                event_type='playback_end',origin='web',occurred_at=time.time(),listened_ms=150000,covered_ms=150000,
                duration_ms=180000,terminal_reason='ended')
            start=event|{'event_id':str(uuid.uuid4()),'event_type':'playback_start','seq':0,
                'listened_ms':0,'covered_ms':0,'terminal_reason':'','occurred_at':time.time()-150}
            response=self.client.post('/api/music/discovery/events',json={'events':[start,event]})
            self.assertEqual(response.status_code,200,response.text)
            refs.append(event['event_id'])
        result=self.client.get('/api/music/discovery/profile').json()
        self.assertEqual(result['long_term']['support']['artist'],3)
        self.assertTrue({'event:'+ref for ref in refs}.issubset(result['long_term']['evidence_refs']))
        self.assertGreater(result['short_term']['features']['artist']['Artist'],0)
        result['settings']['languages']=[]
        track={'key':'new','artist':'Artist'}
        self.assertIsNotNone(ranking.score(track,result,{}))
        self.assertIsNone(ranking.score(track|{'artist':'Different'},result,{}))

    def test_reservation_overrun_settles_without_stopping_under_total_limit(self):
        from backend.modules.music.agent import budget
        batch=self.create().json()
        with library.database() as cursor:
            row=runs.get(cursor,batch['run_id'],lock=True)
            runs.update(cursor,row,status='running',lease_until=time.time()+60)
        reservation=budget.reserve(batch['run_id'],1000,'semantic','test',time.time()+30)
        budget.settle(batch['run_id'],reservation['id'],actual=1139,usage_details={'input_tokens':139,'output_tokens':1000})
        budget.settle(batch['run_id'],reservation['id'],actual=1139)
        with library.database() as cursor: row=runs.get(cursor,batch['run_id'])
        self.assertEqual(row['context']['actual_tokens'],1139)
        self.assertFalse(row['context'].get('model_stop'))
        self.assertEqual(row['context']['model_requests'][0]['usage']['output_tokens'],1000)
        later=budget.reserve(batch['run_id'],1000,'semantic','next',time.time()+30)
        budget.settle(batch['run_id'],later['id'],actual=None)
        with self.assertRaisesRegex(Exception,'用量待确认'):
            budget.reserve(batch['run_id'],1000,'semantic','blocked',time.time()+30)

    def test_old_favorite_is_long_term_but_not_recent_interest(self):
        with library.database() as cursor:
            key=library.save_track(cursor,library.TrackInput.model_validate(self.track()))
            cursor.execute('INSERT INTO music_favorites (user_id,track_id,created_at) VALUES (%s,%s,%s)',(self.user_id,key,time.time()-30*86400))
        result=self.client.get('/api/music/discovery/profile').json()
        self.assertIn('Artist',result['long_term']['features']['artist'])
        self.assertNotIn('artist',result['short_term']['features'])
