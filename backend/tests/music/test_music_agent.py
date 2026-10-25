from __future__ import annotations

import io
import asyncio
import json
import secrets
import sys
import tempfile
import threading
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pymysql
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import application
from backend.infrastructure import database as database_store
from backend.core import paths
from backend.modules.accounts import service as accounts
from backend.modules.admin import api as administration
from backend.modules.music import library
from backend.modules.music.agent import auth, budget, features, mcp, model, runtime, service, skill, store, validation, work


class SettingsTests(unittest.TestCase):
    def test_token_budget_range_and_default(self):
        payload = dict(accounts.DEFAULT_SETTINGS)
        payload.pop("music_agent_tokens")
        self.assertEqual(administration.SettingsUpdate.model_validate(payload).music_agent_tokens,150000)
        for tokens in (2000,80000,150000):
            with self.subTest(tokens=tokens):
                settings = administration.SettingsUpdate.model_validate(payload | {"music_agent_tokens":tokens})
                self.assertEqual(settings.music_agent_tokens,tokens)
        for tokens in (1999,150001):
            with self.subTest(tokens=tokens), self.assertRaises(ValidationError):
                administration.SettingsUpdate.model_validate(payload | {"music_agent_tokens":tokens})

    def test_network_request_settings_range(self):
        payload = dict(accounts.DEFAULT_SETTINGS)
        payload.pop('music_agent_network_requests')
        self.assertEqual(administration.SettingsUpdate.model_validate(payload).music_agent_network_requests, 300)
        for count in (160, 300, 600):
            self.assertEqual(administration.SettingsUpdate.model_validate(payload | {'music_agent_network_requests': count}).music_agent_network_requests, count)
        for count in (159, 601):
            with self.assertRaises(ValidationError):
                administration.SettingsUpdate.model_validate(payload | {'music_agent_network_requests': count})
        daily = dict(accounts.DEFAULT_SETTINGS)
        daily.pop('music_daily_network_requests')
        self.assertEqual(administration.SettingsUpdate.model_validate(daily).music_daily_network_requests, 5000)
        for count in (1, 480, 5000):
            self.assertEqual(administration.SettingsUpdate.model_validate(daily | {'music_daily_network_requests': count}).music_daily_network_requests, count)
        for count in (0, 5001):
            with self.assertRaises(ValidationError):
                administration.SettingsUpdate.model_validate(daily | {'music_daily_network_requests': count})

    def test_search_round_budget_default_and_maximum(self):
        payload = dict(accounts.DEFAULT_SETTINGS)
        payload.pop('music_agent_search_rounds')
        self.assertEqual(administration.SettingsUpdate.model_validate(payload).music_agent_search_rounds,5)
        for count in (0,3,5):
            self.assertEqual(administration.SettingsUpdate.model_validate(payload | {'music_agent_search_rounds':count}).music_agent_search_rounds,count)
        with self.assertRaises(ValidationError):
            administration.SettingsUpdate.model_validate(payload | {'music_agent_search_rounds':6})


class NeedParsingTests(unittest.TestCase):
    def test_romance_alias_negation_and_explicit_scene(self):
        self.assertEqual(model.offline_parse('谈恋爱', {})['scene'], '情歌')
        self.assertEqual(model.offline_parse('工作，不要爱情主题', {})['scene'], '工作')
        self.assertTrue(model.offline_parse('谈恋爱', {'scene':'工作'})['clarification'])
        with self.assertRaises(ValidationError):
            from backend.modules.music.agent.models import ParsedNeed
            ParsedNeed(scene='未实现的场景')

    def parse(self, replies):
        request = {'need': '游戏，EXO和BIGBANG优先', 'preferences': {'target_count': 13, 'source_mode': 'discover'}}
        with patch.object(model.library, 'database'), patch.object(model.store, 'get', return_value={'context': {'limits': {'network_enabled': True}}}), patch.object(model, 'complete', side_effect=replies) as call:
            result = model.parse('test', request, {'text': 'rules', 'schema': {}})
        return result, call

    def test_empty_truncated_and_invalid_outputs_retry_once(self):
        valid = {'content': json.dumps(model.offline_parse('游戏', {})), 'finish_reason': 'stop'}
        for bad in ({'content': ''}, {'content': 'explanation'}, {'content': '{}', 'finish_reason': 'length'}, {'content': '[]'}, {'content': '{"target_count":999}'}):
            with self.subTest(bad=bad):
                result, call = self.parse([bad, valid])
                self.assertEqual(call.call_count, 2)
                self.assertEqual(call.call_args.kwargs['max_output'], 4000)
                self.assertTrue(call.call_args.kwargs['json_output'])
                self.assertEqual(result['target_count'], 13)
                self.assertEqual(result['source_mode'], 'discover')

    def test_second_failure_is_actionable_and_budget_is_not_swallowed(self):
        with self.assertRaisesRegex(model.ModelOutputError, '模型需求解析失败.*已重试一次'):
            self.parse([{'content': ''}, {'content': ''}])
        with self.assertRaises(budget.BudgetExceeded):
            self.parse([{'content': ''}, budget.BudgetExceeded('limit')])

    def test_valid_fenced_json_does_not_retry(self):
        result, call = self.parse([{'content': '```json\n'+json.dumps(model.offline_parse('游戏', {}))+'\n```'}])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['scene'], '游戏')


class MatchTests(unittest.TestCase):
    def candidate(self, vocals=False, language=None):
        return {"key":"k", "title":"Track", "artist":"Artist", "duration":180, "source":{"kind":"personal"},
                "analysis":{"features":{"vocals":vocals,"language":language,"themes":[],"scene_scores":{"游戏":.8,"放松":.8}},
                            "evidence":{"fields":{"vocals":{"confidence":.9},"language":{"confidence":.9},"scene_scores":{"confidence":.9}}}},
                "validation":{"identity":True,"playable":True,"full":True}}

    def test_instrumental_ignores_language_but_unknown_vocals_fails(self):
        parsed = model.offline_parse("纯音乐",{})
        self.assertTrue(features.match(self.candidate(),parsed)["qualified"])
        self.assertFalse(features.match(self.candidate(None),parsed)["qualified"])
        self.assertFalse(features.match(self.candidate(True,"英文"),model.offline_parse("工作，中文歌",{}))["qualified"])

    def test_default_language_is_unrestricted(self):
        parsed = model.offline_parse("打游戏的音乐",{})
        self.assertEqual(parsed["languages"],[])
        self.assertTrue(features.match(self.candidate(True,"英文"),parsed)["qualified"])
        self.assertTrue(features.match(self.candidate(True),parsed)["qualified"])

    def test_partial_audio_does_not_prove_sleep_and_theme_exclusion_needs_lyrics(self):
        track = self.candidate()
        self.assertFalse(features.match(track,model.offline_parse("睡觉",{}))["qualified"])
        track = self.candidate(True,"中文")
        self.assertFalse(features.match(track,model.offline_parse("不要失恋主题",{}))["qualified"])
        self.assertFalse(features.match(track,model.offline_parse("励志",{}))["qualified"])

    def test_explicit_preferences_and_duration_goal(self):
        parsed = model.offline_parse("打游戏，英文歌，30首，凑够一小时",{"languages":["中文"],"target_count":5})
        self.assertEqual((parsed["languages"],parsed["target_count"],parsed["goal"]),(["中文"],5,"duration"))
        self.assertEqual(model.offline_parse("工作，英文歌",{})["languages"],["英文"])
        self.assertEqual(model.offline_parse("工作，中文歌",{"languages":[]})["languages"],[])
        self.assertFalse(model.offline_parse("只用我的歌单，不要联网",{})["network_allowed"])

    def test_versions_are_not_merged_by_song_name(self):
        first = self.candidate() | {"match":{"score":1}}
        second = first | {"title":"Track (Live)"}
        third = first | {"duration":230}
        self.assertEqual(len(service.deduplicated([first,dict(first),second,third])),3)

    def test_audio_analysis_uses_full_waveform_and_does_not_infer_vocals(self):
        import math
        import struct
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"tone.wav"
            with wave.open(str(path),"wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b"".join(struct.pack("<h",int(math.sin(i*2*math.pi*220/16000)*1000)) for i in range(32000)))
            value = features.audio_features(path)
        self.assertAlmostEqual(value["duration"],2,places=1)
        self.assertGreaterEqual(value["energy"],0)
        self.assertNotIn("vocals",value)

    def test_cancel_stops_and_reaps_decoder_process(self):
        started = time.monotonic()
        def check():
            if time.monotonic()-started > .15:
                raise work.Stopped('cancel decoder')
        processes = []
        original = work.subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(work.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(work.Stopped):
                work.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], check=check,
                                 capture_output=True)
        self.assertLess(time.monotonic()-started, 1)
        self.assertIsNotNone(processes[0].poll())

    def test_task_media_resources_are_isolated_and_cleaned_on_failure(self):
        row = {'user_id':1, 'run_id':'test-resource-run', 'worker_id':'first-worker'}
        track = {'user_id':1, 'key':'same-song', 'provider':'netease', 'identifier':'1'}
        folder = tempfile.TemporaryDirectory()
        path = Path(folder.name)/'audio.media'
        path.write_bytes(b'audio')
        with self.assertRaisesRegex(RuntimeError, 'test abort'):
            with work.task(row), work.operation(row, lambda:None) as first:
                self.assertTrue(first.resources.retain_audio(track, folder, path, 'test'))
                changed_lease = row | {'worker_id':'replacement-worker'}
                with work.operation(changed_lease, lambda:None) as second:
                    self.assertIsNone(second.resources.cached_audio(track))
                self.assertIsNone(first.resources.cached_audio(track | {'user_id':2}))
                raise RuntimeError('test abort')
        self.assertFalse(path.exists())


class AgentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.original_ready = database_store.schema_ready
        config = database_store.database_config()
        cls.name = "videodream_agent_test_"+secrets.token_hex(6)
        cls.admin_connection = pymysql.connect(**{k:v for k,v in config.items() if k!="database"})
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.name}` CHARACTER SET utf8mb4")
        cls.test_cleanup_done=False
        cls.addClassCleanup(lambda: AgentIntegrationTests.tearDownClass.__func__(cls))
        cls.stack.enter_context(patch.object(database_store,"database_config",return_value=config | {"database":cls.name}))
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cls.stack.enter_context(patch.object(library,"DOWNLOAD_DIR",cls.root))
        from backend.core import paths
        cls.stack.enter_context(patch.object(paths,"DOWNLOAD_DIR",cls.root))
        cls.stack.enter_context(patch.object(accounts,"redis_prefix",return_value="videodream:agent-test:"+secrets.token_hex(8)+":"))
        cls.stack.enter_context(patch.object(runtime,"submit"))
        from backend.modules.music.discovery import profile_runtime
        cls.stack.enter_context(patch.object(profile_runtime,'start'))
        from backend.tests.support import migrate_test_database
        migrate_test_database(config | {"database": cls.name})
        database_store.schema_ready = False
        with library.database() as cursor:
            cursor.execute("UPDATE users SET password_hash=%s,must_change_password=FALSE WHERE id=1",(accounts.passwords.hash("Test-admin-123!"),))
        cls.user_id = accounts.create_user(accounts.Credentials(username="agent_user",password="Test-user-123!"))
        cls.client = cls.stack.enter_context(TestClient(application.app,base_url="http://127.0.0.1:8001",headers={"Origin":"http://127.0.0.1:5174","X-VideoDream-CSRF":"1"}))
        with patch.object(accounts.captcha, 'make_code', return_value='A2B3'):
            challenge = cls.client.get('/api/auth/captcha').json()
        response = cls.client.post("/api/auth/login",json={"username":"agent_user","password":"Test-user-123!",
            "captcha_id": challenge['captcha_id'], "captcha_code": "A2B3"})
        response.raise_for_status()
        cls.client.headers["Authorization"] = "Bearer "+response.json()["access_token"]
        cls.sid = cls.client.cookies.get(accounts.COOKIE).split(".")[0]

    @classmethod
    def tearDownClass(cls):
        if cls.test_cleanup_done: return
        cls.test_cleanup_done=True
        keys = list(accounts.redis_client().scan_iter(match=accounts.redis_prefix()+"*"))
        if keys:
            accounts.redis_client().delete(*keys)
        cls.stack.close()
        database_store.schema_ready = cls.original_ready
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.name}`")
        cls.admin_connection.close()

    def setUp(self):
        self.stack = ExitStack()
        self.marker = accounts.principal.set(accounts.Principal(self.user_id,"agent_user","user",self.sid))
        with library.database() as cursor:
            for table in ('music_discovery_feeds','music_discovery_daily_usage','music_discovery_profile_runs','music_discovery_profile_versions'):
                cursor.execute('DELETE FROM '+table)
            for table in ("music_recommendation_runs","music_track_features","music_playlists","music_favorites","music_history","music_downloads","music_tracks","resources"):
                cursor.execute(f"DELETE FROM {table}")
            config = accounts.DEFAULT_SETTINGS | {"music_agent_enabled":True}
            cursor.execute("UPDATE system_settings SET config=%s WHERE id=1",(json.dumps(config),))

    def tearDown(self):
        self.stack.close()
        accounts.principal.reset(self.marker)

    def create(self,preferences=None):
        key = secrets.token_hex(16)
        request = {"need":"放松", "preferences":preferences or {}}
        response = self.client.post("/api/music/recommendations",json=request,headers={"Idempotency-Key":key})
        response.raise_for_status()
        return response.json(),key,request

    def running(self,preferences=None):
        public,_,_ = self.create(preferences)
        row = runtime.claim(public["run_id"])
        with library.database() as cursor:
            row = store.get(cursor,row["run_id"],lock=True)
            row = store.update(cursor,row,request=row["request"] | {"parsed":model.offline_parse("放松",preferences or {})})
        return row

    @contextmanager
    def real_mcp_transport(self):
        """Use the real MCP client, HTTP protocol, routing and tools in TestClient."""
        import httpx
        original = mcp.streamablehttp_client
        test_client = self.client
        class ApplicationTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                response = await asyncio.to_thread(test_client.request, request.method, str(request.url),
                    headers=dict(request.headers), content=await request.aread())
                return httpx.Response(response.status_code, headers=response.headers,
                                      content=response.content, request=request)
        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=ApplicationTransport(), headers=headers, timeout=timeout, auth=auth)
        with patch.object(mcp, 'streamablehttp_client',
                          side_effect=lambda *args, **kwargs: original(*args, **kwargs, httpx_client_factory=factory)):
            yield

    def test_fill_keeps_parent_and_refreshes_expired_validation(self):
        row = self.running()
        seed = [{'key': 'old', 'analysis': {'checked_at': None}, 'validation': {'checked_at': time.time()-7200}},
                {'key': 'fresh', 'validation': {'checked_at': time.time()}}]
        with library.database() as cursor:
            row = store.get(cursor, row['run_id'], lock=True)
            store.update(cursor, row, status='partial', result={'tracks': seed}, result_version=1)
        url = f"/api/music/recommendations/{row['run_id']}/fill"
        headers = {'Idempotency-Key': secrets.token_hex(16)}
        self.assertEqual(self.client.post(url, json={'result_version': 2}, headers=headers).status_code, 409)
        result = self.client.post(url, json={'result_version': 1}, headers=headers)
        self.assertEqual(result.status_code, 201, result.text)
        again = self.client.post(url, json={'result_version': 1}, headers=headers)
        self.assertEqual(again.json()['run_id'], result.json()['run_id'])
        with library.database() as cursor:
            child = store.get(cursor, result.json()['run_id'])
            parent = store.get(cursor, row['run_id'])
        self.assertEqual(child['parent_run_id'], row['run_id'])
        self.assertEqual(parent['status'], 'partial')
        self.assertNotIn('validation', child['context']['candidates'][0])
        self.assertIn('validation', child['context']['candidates'][1])
        self.assertEqual(child['context']['candidates'][0]['user_id'], self.user_id)
        self.assertEqual(child['context']['budget'], {})
        self.assertEqual(child['context']['limits']['network_requests'], 300)

    def test_reserve_blocks_analysis_requests_but_allows_validation(self):
        from backend.infrastructure import network
        row = self.running()
        token = auth.issue(row)
        budget.charge(row['run_id'], network_requests=225)
        def request(*args):
            network.request_guard.get()()
            return {'ok': True}
        with patch.object(service, 'execute_scoped', side_effect=request):
            with self.assertRaises(budget.BudgetExceeded) as raised:
                service.execute(token, 'get_track_features', {'keys': []})
            self.assertEqual(raised.exception.resource, 'network_reserve')
            self.assertEqual(service.execute(token, 'validate_tracks', {'keys': []}), {'ok': True})
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['network_requests'], 226)

    def test_network_limit_snapshot_and_legacy_default(self):
        row = self.running()
        budget.charge(row['run_id'], network_requests=300)
        with self.assertRaises(budget.BudgetExceeded):
            budget.charge(row['run_id'], network_requests=1)
        with library.database() as cursor:
            current = store.get(cursor, row['run_id'], lock=True)
            limits = dict(current['context']['limits']); limits.pop('network_requests')
            store.update(cursor, current, context=current['context'] | {'limits': limits, 'budget': {}})
        budget.charge(row['run_id'], network_requests=160)
        with self.assertRaises(budget.BudgetExceeded):
            budget.charge(row['run_id'], network_requests=1)

    def test_idempotency_concurrency_isolation_and_no_session_leak(self):
        row,key,request = self.create()
        response = self.client.post("/api/music/recommendations",json=request,headers={"Idempotency-Key":key})
        self.assertEqual(response.json()["run_id"],row["run_id"])
        self.assertEqual(self.client.post("/api/music/recommendations",json=request,headers={"Idempotency-Key":secrets.token_hex(16)}).status_code,409)
        self.assertNotIn(self.sid,json.dumps(row))
        with library.database() as cursor:
            with self.assertRaises(HTTPException):
                store.get(cursor,row["run_id"],1)

    def test_mcp_real_protocol_scope_and_cancel(self):
        row = self.running()
        token = auth.issue(row)
        headers = {"Authorization":"Bearer "+token,"Accept":"application/json, text/event-stream"}
        response = self.client.post("/api/music/mcp",headers=headers,json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}})
        self.assertEqual(response.status_code,200,response.text)
        response = self.client.post('/api/music/mcp', headers=headers,
            json={'jsonrpc':'2.0', 'method':'notifications/initialized'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.content, b'')
        response = self.client.post("/api/music/mcp",headers=headers,json={"jsonrpc":"2.0","id":2,"method":"tools/list"})
        self.assertEqual(len(response.json()["result"]["tools"]),8)
        response = self.client.post("/api/music/mcp",headers=headers,json={"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_music_categories","arguments":{}}})
        self.assertFalse(response.json()["result"].get("isError"),response.text)
        with self.assertRaises(HTTPException):
            service.execute(token,"save_playlist",{"result_version":1})
        self.client.post(f"/api/music/recommendations/{row['run_id']}/cancel",json={}).raise_for_status()
        self.assertEqual(self.client.post("/api/music/mcp",headers=headers,json={"jsonrpc":"2.0","id":4,"method":"tools/list"}).status_code,409)

    def test_offline_personal_search_validation_and_atomic_save(self):
        buffer = io.BytesIO()
        with wave.open(buffer,"wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000); output.writeframes(b"\0\0"*16000)
        from backend.core import storage
        path = storage.path("file", "track.wav", uid=self.user_id)
        path.write_bytes(buffer.getvalue())
        accounts.register_resource("file",path.name)
        track = library.TrackInput(title="本地歌曲",artist="作者",provider="local",identifier="local-1",webpage_url="/api/files/track.wav",duration=2)
        with library.database() as cursor:
            key = library.save_track(cursor,track)
            cursor.execute("INSERT INTO music_downloads (id,track_id,filename,created_at,imported,user_id) VALUES (%s,%s,%s,%s,TRUE,%s)",(secrets.token_hex(12),key,path.name,time.time(),self.user_id))
            library.write_lyrics(cursor,key,{"text":"[00:00.00]这是中文歌词\n[00:01.00]今天一起听音乐","source":"本地 LRC","signature":"","checked_at":time.time(),"updated_at":time.time(),"manual":True})
        row = self.running({"network_allowed":False,"target_count":1})
        token = auth.issue(row)
        with patch.object(model,"complete",side_effect=AssertionError("offline model call")), patch.object(platforms_for_test(),"track_lyrics",side_effect=AssertionError("offline lyrics call")):
            response = service.execute(token,"search_my_music",{})
            self.assertEqual(len(response["data"]["tracks"]),1)
            self.assertNotIn("user_id", response["data"]["tracks"][0])
            for tool in ("get_track_features","validate_tracks"):
                response = service.execute(token,tool,{"keys":[key]})
                self.assertIsNone(response["error"],response)
        runtime.finish(row["run_id"],None,row["worker_id"])
        with library.database() as cursor:
            finished = store.get(cursor,row["run_id"])
        self.assertEqual(finished["status"],"finished")
        save_token = auth.issue(finished,save_version=1)
        first = service.execute(save_token,"save_playlist",{"result_version":1})["data"]
        second = service.execute(save_token,"save_playlist",{"result_version":1})["data"]
        self.assertEqual(first["playlist_id"],second["playlist_id"])
        with library.database() as cursor:
            cursor.execute("SELECT position FROM music_playlist_tracks WHERE playlist_id=%s",(first["playlist_id"],))
            self.assertEqual(cursor.fetchone()["position"],0)
            cursor.execute("DELETE FROM music_playlists WHERE id=%s",(first["playlist_id"],))
        with self.assertRaises(HTTPException) as error:
            service.execute(save_token,"save_playlist",{"result_version":1})
        self.assertEqual(error.exception.status_code,410)

    def test_budget_recovery_old_worker_and_versions(self):
        row = self.running()
        token = auth.issue(row)
        with self.assertRaises(budget.BudgetExceeded):
            budget.charge(row["run_id"],tokens=200001)
        with library.database() as cursor:
            row = store.get(cursor,row["run_id"],lock=True)
            store.update(cursor,row,lease_until=time.time()-1)
        recovered = self.client.get(f"/api/music/recommendations/{row['run_id']}").json()
        self.assertEqual(recovered["status"],"interrupted")
        self.client.post(f"/api/music/recommendations/{row['run_id']}/continue",json={}).raise_for_status()
        new = runtime.claim(row["run_id"])
        with self.assertRaises(HTTPException):
            auth.claims(token)
        runtime.finish(row["run_id"],None,row["worker_id"])
        with library.database() as cursor:
            self.assertEqual(store.get(cursor,row["run_id"])["status"],"running")
        with self.assertRaises(HTTPException):
            service.save(row["run_id"],99)

    def test_source_policy_unknown_candidates_and_deleted_resources(self):
        row = self.running({"source_mode":"personal_only"})
        token = auth.issue(row)
        response = service.execute(token,"search_online_playlists",{"provider":"netease","query":"工作"})
        self.assertEqual(response["error"]["code"],"403")
        response = service.execute(token,"validate_tracks",{"keys":["unknown"]})
        self.assertEqual(response["error"]["code"],"422")
        self.client.delete(f"/api/workspace/resources/music_recommendation/{row['run_id']}").raise_for_status()
        with self.assertRaises(HTTPException):
            auth.claims(token)
        with library.database() as cursor:
            cursor.execute("SELECT deleted FROM resources WHERE kind='music_recommendation' AND resource_key=%s",(row["run_id"],))
            self.assertTrue(cursor.fetchone()["deleted"])

    def test_json_transport_reports_truncation_and_uses_json_mode(self):
        row = self.running()
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(tool_calls=[], content=None))], usage=SimpleNamespace(total_tokens=20))
        config = {'api_key': 'test-key', 'base_url': 'https://api.siliconflow.cn/v1', 'model': 'test-model'}
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, 'AsyncOpenAI') as constructor:
            constructor.return_value.__aenter__.return_value.chat.completions.create = AsyncMock(return_value=response)
            result = model.complete(row['run_id'], [{'role':'user','content':'Return JSON'}], json_output=True)
            self.assertEqual(result['content'], '')
            self.assertEqual(result['finish_reason'], 'length')
            self.assertEqual(constructor.return_value.__aenter__.return_value.chat.completions.create.call_args.kwargs['response_format'], {'type':'json_object'})
        with library.database() as cursor:
            current = store.get(cursor, row['run_id'], lock=True)
            store.update(cursor, current, context=current['context'] | {'limits': current['context']['limits'] | {'api':'responses'}})
        with patch.object(model.ai, 'load_config', return_value=config | {'base_url':'https://api.openai.com/v1'}), patch.object(model, 'AsyncOpenAI') as constructor:
            constructor.return_value.__aenter__.return_value.responses.create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='', status='incomplete', usage=SimpleNamespace(total_tokens=20)))
            result = model.complete(row['run_id'], [{'role':'user','content':'Return JSON using Responses'}], json_output=True)
            self.assertEqual(result['finish_reason'], 'incomplete')
            self.assertEqual(constructor.return_value.__aenter__.return_value.responses.create.call_args.kwargs['text'], {'format':{'type':'json_object'}})

    def test_model_request_reserves_budget_and_uses_existing_connection(self):
        row = self.running()
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[],content='{}'))],
                                   usage=SimpleNamespace(total_tokens=15,prompt_tokens=5,completion_tokens=10,completion_tokens_details=SimpleNamespace(reasoning_tokens=7)))
        config = {"api_key":"test-key", "base_url":"https://model.test/v1", "model":"test-model"}
        with patch.object(model.ai,"load_config",return_value=config), patch.object(model,"AsyncOpenAI") as constructor:
            constructor.return_value.__aenter__.return_value.chat.completions.create = AsyncMock(return_value=response)
            answer = model.complete(row["run_id"],[{"role":"user","content":"test"}],max_output=80,decision=True)
            self.assertEqual(answer["content"],'{}')
            self.assertNotIn("model",constructor.call_args.kwargs)
            constructor.return_value.__aenter__.return_value.chat.completions.create.assert_awaited_once()
        with library.database() as cursor:
            changed = store.get(cursor,row["run_id"])
        self.assertEqual(changed["context"]["budget"]["decisions"],1)
        self.assertEqual(changed["context"]["budget"]["tokens"],15)
        self.assertEqual(changed['context']['model_requests'][0]['usage'],{'input_tokens':5,'output_tokens':10,'reasoning_tokens':7})
        self.assertEqual(budget.usage(changed["context"]), {"actual":15,"reserved":0,"unconfirmed":0})

    def test_atomic_reservation_actual_refund_and_idempotent_late_settlement(self):
        row = self.running()
        rid = row['run_id']
        request = budget.reserve(rid, 12000, 'semantic', 'song-a', time.time()+90)
        with self.assertRaises(model.ModelFault):
            budget.reserve(rid, 1000, 'parse', 'second', time.time()+60)
        self.assertEqual(budget.usage(service.current_run(rid)['context'])['reserved'], 12000)
        budget.settle(rid, request['id'], actual=3100)
        budget.settle(rid, request['id'], actual=3100)
        self.assertEqual(budget.usage(service.current_run(rid)['context']), {'actual':3100, 'reserved':0, 'unconfirmed':0})
        second = budget.reserve(rid, 12000, 'semantic', 'song-b', time.time()+90)
        self.client.post(f'/api/music/recommendations/{rid}/cancel').raise_for_status()
        with store.worker_scope(rid, 'expired-worker'):
            budget.settle(rid, second['id'], error='cancelled')
            budget.settle(rid, second['id'], actual=70)
            budget.settle(rid, second['id'], actual=70)
        current = service.current_run(rid)
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(budget.usage(current['context']), {'actual':3170, 'reserved':0, 'unconfirmed':0})
        self.assertFalse(current['result'])

    def test_total_budget_calls_and_corrections_are_shared(self):
        row = self.running()
        rid = row['run_id']
        with self.assertRaises(budget.BudgetExceeded):
            budget.reserve(rid, 145001, 'semantic', 'too-large', time.time()+90)
        for n in range(40):
            request = budget.reserve(rid, 1000, 'semantic', str(n), time.time()+90)
            budget.settle(rid, request['id'], actual=10)
        with self.assertRaises(budget.BudgetExceeded):
            budget.reserve(rid, 1000, 'parse', 'new-phase', time.time()+60)
        self.assertEqual(service.current_run(rid)['context']['budget']['model_calls'], 40)
        self.assertEqual(service.current_run(rid)['context']['budget']['tokens'], 400)

    def test_unknown_usage_stops_future_paid_dispatch(self):
        row = self.running()
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request',
                new=AsyncMock(return_value=({'content':'{}', 'finish_reason':'stop'}, None))) as request:
            with self.assertRaisesRegex(model.ModelFault, '未返回用量'):
                model.complete(row['run_id'], [{'role':'user','content':'test'}])
            with self.assertRaises(model.ModelFault):
                model.complete(row['run_id'], [{'role':'user','content':'test again'}])
            self.assertEqual(request.await_count, 1)
        totals = budget.usage(service.current_run(row['run_id'])['context'])
        self.assertEqual(totals['actual'], 0)
        self.assertEqual(totals['reserved'], 0)
        self.assertGreater(totals['unconfirmed'], 4000)

    def test_event_loop_misuse_is_rejected_before_reservation(self):
        row = self.running()
        async def invoke():
            with self.assertRaisesRegex(model.ModelFault, '请求尚未发送'):
                model.complete(row['run_id'], [{'role':'user', 'content':'test'}])
        with patch.object(budget, 'reserve', wraps=budget.reserve) as reserve, \
             patch.object(model, '_request') as request:
            asyncio.run(invoke())
        reserve.assert_not_called()
        request.assert_not_called()
        current = service.current_run(row['run_id'])
        self.assertEqual(budget.usage(current['context']), {'actual':0, 'reserved':0, 'unconfirmed':0})
        self.assertEqual(current['context']['budget'].get('model_calls', 0), 0)

    def test_sdk_failure_before_dispatch_releases_reservation_and_redacts_logs(self):
        row = self.running()
        config = {'api_key':'test-secret', 'base_url':'https://model.test/v1', 'model':'test'}
        with patch.object(model.ai, 'load_config', return_value=config), \
             patch.object(model, 'AsyncOpenAI', side_effect=ValueError('test-secret')), \
             self.assertLogs(model.logger, level='WARNING') as logs:
            with self.assertRaisesRegex(model.ModelFault, '发送前失败'):
                model.complete(row['run_id'], [{'role':'user', 'content':'test'}])
        current = service.current_run(row['run_id'])
        self.assertEqual(budget.usage(current['context']), {'actual':0, 'reserved':0, 'unconfirmed':0})
        request = current['context']['model_requests'][0]
        self.assertEqual((request['state'], request['actual'], request['dispatched']), ('settled', 0, False))
        self.assertTrue(current['context']['model_stop'])
        self.assertIn('ValueError', '\n'.join(logs.output))
        self.assertNotIn('test-secret', '\n'.join(logs.output))

    def test_transport_failure_after_dispatch_retains_unknown_usage(self):
        import httpx
        row = self.running()
        requests = []
        async def fail(request):
            requests.append(request)
            raise httpx.RemoteProtocolError('Peer closed without response')
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(fail), **kwargs)
        config = {'api_key':'fake', 'base_url':'https://model.test/v1', 'model':'test'}
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, 'AsyncClient', side_effect=factory):
            with self.assertRaisesRegex(model.ModelFault, '用量待确认'):
                model.complete(row['run_id'], [{'role':'user', 'content':'test'}])
            with self.assertRaises(model.ModelFault):
                model.complete(row['run_id'], [{'role':'user', 'content':'do not retry'}])
        self.assertEqual(len(requests), 1)
        current = service.current_run(row['run_id'])
        self.assertTrue(current['context']['model_requests'][0]['dispatched'])
        self.assertGreater(budget.usage(current['context'])['unconfirmed'], 0)

    def test_provider_excess_is_recorded_truthfully_and_blocks_new_requests(self):
        row = self.running()
        request = budget.reserve(row['run_id'], 1000, 'semantic', 'unexpected-usage', time.time()+90)
        budget.settle(row['run_id'], request['id'], actual=150001)
        current = service.current_run(row['run_id'])
        self.assertEqual(current['context']['budget']['tokens'], 150001)
        self.assertTrue(current['context']['model_stop'])
        with self.assertRaises(model.ModelFault):
            budget.reserve(row['run_id'], 1000, 'parse', 'new', time.time()+60)

    def test_inflight_async_request_is_cancelled_and_accounting_retained(self):
        row = self.running()
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        closed = []
        async def pending(*args):
            await model._mark_dispatched(None)
            await asyncio.sleep(.03)
            model.cancel(row['run_id'])
            try:
                await asyncio.sleep(30)
            finally:
                closed.append(True)
        started = time.monotonic()
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request', side_effect=pending):
            with self.assertRaisesRegex(model.ModelFault, '已取消'):
                model.complete(row['run_id'], [{'role':'user','content':'test'}])
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(closed, [True])
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['model_calls'], 1)
        self.assertGreater(budget.usage(service.current_run(row['run_id'])['context'])['unconfirmed'], 0)

    def test_response_after_cancellation_only_settles_usage(self):
        row = self.running()
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        async def late(*args):
            with library.database() as cursor:
                current = store.get(cursor, row['run_id'], lock=True)
                store.update(cursor, current, status='cancelled', worker_id=None, lease_until=None)
            return {'content':'{}', 'finish_reason':'stop'}, 71
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request', side_effect=late):
            with self.assertRaises(HTTPException):
                model.complete(row['run_id'], [{'role':'user','content':'test'}])
        current = service.current_run(row['run_id'])
        self.assertEqual(current['context']['budget']['tokens'], 71)
        self.assertEqual(current['status'], 'cancelled')
        self.assertFalse(current['result'])

    def test_no_gain_diagnoses_once_then_stops_after_next_ten(self):
        row = self.running()
        def update(count):
            with library.database() as cursor:
                current = store.get(cursor, row['run_id'], lock=True)
                return store.update(cursor, current, context=current['context'] | {'candidates':[
                    {'key':str(i), 'title':'Song', 'artist':'Artist', 'source':{'kind':'personal'},
                     'analysis':{'features':{}}, 'validation':{'identity':False}} for i in range(count)]})
        self.assertEqual(runtime.no_gain(update(10)), 'adjust')
        self.assertIsNone(runtime.no_gain(service.current_run(row['run_id'])))
        with self.assertRaisesRegex(budget.BudgetExceeded, '又有10首'):
            runtime.no_gain(update(20))

    def test_shared_correction_cap_and_one_correction_per_input(self):
        row = self.running()
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        valid = ({'content':'{"scene":"放松"}', 'finish_reason':'stop'}, 10)
        invalid = ({'content':'not-json', 'finish_reason':'stop'}, 10)
        from backend.modules.music.agent.models import ParsedNeed
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request',
                new=AsyncMock(side_effect=[invalid, valid, invalid, valid, invalid])) as request:
            for i in range(2):
                model.structured(row['run_id'], [{'role':'user','content':f'need-{i}'}],
                                 ParsedNeed.model_validate, phase='parse')
            with self.assertRaises(model.ModelOutputError):
                model.structured(row['run_id'], [{'role':'user','content':'need-2'}],
                                 ParsedNeed.model_validate, phase='parse')
            self.assertEqual(request.await_count, 5)
        used = service.current_run(row['run_id'])['context']['budget']
        self.assertEqual((used['model_calls'], used['corrections'], used['tokens']), (5, 2, 50))

    def test_three_lyric_candidates_share_one_paid_request(self):
        row = self.running()
        tracks = []
        for i in range(3):
            payload = library.TrackInput(title=f'歌曲{i}', artist='歌手', provider='netease', identifier=str(200+i),
                webpage_url=f'https://music.163.com/song?id={200+i}', duration=180)
            with library.database() as cursor:
                key = library.save_track(cursor, payload)
                library.write_lyrics(cursor, key, {'text':'[00:01]今天一起听音乐', 'source':'本地 LRC',
                    'signature':'', 'checked_at':time.time(), 'updated_at':time.time(), 'manual':True})
            tracks.append(payload.model_dump() | {'key':key, 'user_id':self.user_id})
        result = {t['key']:{'vocal_role':'lead', 'language':'中文', 'themes':['甜蜜'], 'moods':['放松'],
                           'scene_scores':{'放松':.9, '情歌':.8}, 'confidence':.9} for t in tracks}
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request',
                new=AsyncMock(return_value=({'content':json.dumps(result), 'finish_reason':'stop'}, 1700))) as request:
            values = features.analyze_many(row['run_id'], tracks, lambda **_:None, True)
            self.assertEqual(len(values), 3)
            self.assertEqual(request.await_count, 1)
            cached = features.analyze_many(row['run_id'], tracks, lambda **_:None, True)
            self.assertTrue(all(v['cached'] for _, v in cached))
            self.assertEqual(request.await_count, 1)
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['tokens'], 1700)

    def test_database_cancellation_closes_request_without_local_signal(self):
        row = self.running()
        closed = []
        async def pending(*args):
            with library.database() as cursor:
                current = store.get(cursor, row['run_id'], lock=True)
                store.update(cursor, current, status='cancelled', worker_id=None, lease_until=None)
            try:
                await asyncio.sleep(30)
            finally:
                closed.append(True)
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        started = time.monotonic()
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, '_request', side_effect=pending):
            with self.assertRaises(HTTPException):
                model.complete(row['run_id'], [{'role':'user','content':'test'}])
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(closed, [True])

    def test_absolute_timeout_cancels_underlying_coroutine(self):
        row = self.running()
        closed = []
        async def pending(*args):
            try:
                await asyncio.sleep(30)
            finally:
                closed.append(True)
        with patch.object(model, '_request', side_effect=pending):
            with self.assertRaisesRegex(model.ModelFault, '超时'):
                asyncio.run(model._drive(row['run_id'], time.time()+.05))
        self.assertEqual(closed, [True])

    def test_model_fault_without_qualified_songs_is_failed(self):
        public, _, _ = self.create()
        with library.database() as cursor:
            row = store.get(cursor, public['run_id'], lock=True)
            store.update(cursor, row, request=row['request'] | {'parsed':model.offline_parse('放松', {})},
                context=row['context'] | {'personal_searched':True, 'candidates':[{
                    'key':'candidate', 'title':'Song', 'artist':'Artist', 'source':{'kind':'personal'}, 'duration':180}]})
        class InProcessClient:
            def __init__(self, token): self.token = token
            def tools(self): return []
            def call(self, name, args): return service.execute(self.token, name, args)
        with patch.object(mcp, 'LocalClient', InProcessClient), \
             patch.object(features, 'analyze_many', side_effect=model.ModelOutputError('模型格式错误，已停止调用')), \
             patch.object(validation, 'validate', side_effect=AssertionError('Must stop downloads')):
            runtime.run(row['run_id'])
        current = service.current_run(row['run_id'])
        self.assertEqual(current['status'], 'failed')
        self.assertIn('模型格式错误', current['result']['notice'])
        self.assertFalse(current['result']['tracks'])

    def test_model_fault_preserves_qualified_songs_as_partial(self):
        row = self.running()
        track = library.TrackInput(title='Qualified song', artist='Artist', provider='netease', identifier='990',
            webpage_url='https://music.163.com/song?id=990', duration=180).model_dump() | {
            'key':'qualified', 'source':{'kind':'personal', 'label':'个人库'},
            'analysis':{'features':{'vocal_role':'lead', 'language':'中文', 'scene_scores':{'放松':.9}},
                'evidence':{'fields':{field:{'confidence':.9} for field in ('vocal_role','language','scene_scores')}}},
            'validation':{'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}}
        with library.database() as cursor:
            row = store.get(cursor, row['run_id'], lock=True)
            store.update(cursor, row, context=row['context'] | {'candidates':[track],
                'model_stop':'模型已停止调用', 'stop_reason':'模型已停止调用'})
        runtime.finish(row['run_id'], None, row['worker_id'])
        current = service.current_run(row['run_id'])
        self.assertEqual(current['status'], 'partial')
        self.assertEqual(len(current['result']['tracks']), 1)
        self.assertIn('模型已停止调用', current['result']['notice'])

    def test_concurrent_reservations_admit_only_one_request(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        row = self.running()
        barrier = Barrier(2)
        def admit(index):
            barrier.wait(timeout=2)
            try:
                return budget.reserve(row['run_id'], 10000, 'semantic', str(index), time.time()+90)
            except model.ModelFault:
                return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(admit, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['model_calls'], 1)
        self.assertEqual(budget.usage(service.current_run(row['run_id'])['context'])['reserved'], 10000)

    def test_real_async_sdk_closes_transport_on_cancel(self):
        import httpx
        row = self.running()
        state = {'closed':False, 'requests':0, 'interrupted':False}
        class HoldingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                state['requests'] += 1
                asyncio.get_running_loop().call_later(.03, model.cancel, row['run_id'])
                try:
                    await asyncio.sleep(30)
                finally:
                    state['interrupted'] = True
            async def aclose(self):
                state['closed'] = True
        def factory(**kwargs):
            return httpx.AsyncClient(transport=HoldingTransport(), **kwargs)
        config = {'api_key':'test', 'base_url':'https://model.test/v1', 'model':'test'}
        with patch.object(model.ai, 'load_config', return_value=config), patch.object(model, 'AsyncClient', side_effect=factory):
            with self.assertRaisesRegex(model.ModelFault, '取消'):
                model.complete(row['run_id'], [{'role':'user','content':'test'}])
        self.assertEqual(state, {'closed':True, 'requests':1, 'interrupted':True})
        self.assertGreater(budget.usage(service.current_run(row['run_id'])['context'])['unconfirmed'], 0)

    def test_real_mcp_can_cancel_while_sdk_transport_is_waiting(self):
        import httpx
        row = self.running()
        payload = library.TrackInput(title='Song', artist='Artist', provider='netease', identifier='991',
            webpage_url='https://music.163.com/song?id=991', duration=180)
        with library.database() as cursor:
            key = library.save_track(cursor, payload)
            library.write_lyrics(cursor, key, {'text':'[00:01]与你相爱的每一天', 'source':'本地 LRC',
                'signature':'', 'checked_at':time.time(), 'updated_at':time.time(), 'manual':True})
        service.add_candidates(row['run_id'], [payload.model_dump()], {'kind':'personal', 'label':'测试'})
        started = threading.Event()
        closed = threading.Event()
        class HoldingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                started.set()
                await asyncio.sleep(30)
            async def aclose(self):
                closed.set()
        def factory(**kwargs):
            return httpx.AsyncClient(transport=HoldingTransport(), **kwargs)
        config = {'api_key':'fake', 'base_url':'https://model.test/v1', 'model':'test'}
        with self.real_mcp_transport(), patch.object(model.ai, 'load_config', return_value=config), \
             patch.object(model, 'AsyncClient', side_effect=factory), ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(mcp.LocalClient(auth.issue(row)).call, 'get_track_features', {'keys':[key]})
            ready = started.wait(3)
            before = time.monotonic()
            self.client.get('/api/health').raise_for_status()
            self.client.post(f"/api/music/recommendations/{row['run_id']}/cancel").raise_for_status()
            self.assertTrue(ready, 'Real MCP tool must reach the SDK HTTP transport')
            self.assertTrue(closed.wait(1), 'Cancel must close the SDK transport without blocking the server loop')
            self.assertLess(time.monotonic()-before, 1)
            try:
                response = future.result(timeout=3)
            except Exception as exc:
                # The SDK can wrap cancellation/409 from MCP cleanup in a task group.
                def expected(error):
                    if isinstance(error, BaseExceptionGroup):
                        return all(expected(item) for item in error.exceptions)
                    return isinstance(error, model.ModelFault) or (
                        isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 409)
                self.assertTrue(expected(exc), repr(exc))
            else:
                self.assertEqual(response['error']['code'], 'MODEL_STOP')
        current = service.current_run(row['run_id'])
        self.assertEqual(current['status'], 'cancelled')
        self.assertFalse(current['result'])
        self.assertGreater(budget.usage(current['context'])['unconfirmed'], 0)

    def test_fabricated_batch_key_stops_without_correction(self):
        jobs = [{'key':str(i), 'messages':[{'role':'system','content':'rules'},
            {'role':'user','content':'{"lyrics":"歌词"}'}]} for i in range(3)]
        with patch.object(model, 'complete', return_value={'content':'{"fabricated":{}}'}) as request:
            with self.assertRaisesRegex(model.ModelFault, '编造'):
                features._semantic('unused', jobs)
        self.assertEqual(request.call_count, 1)

    def test_duplicate_tool_arguments_ignore_json_key_order(self):
        row = self.running()
        attempted = set()
        runtime.record_attempt(row['run_id'], 'search_online_tracks', {'provider':'qq','query':'情歌'}, attempted)
        with self.assertRaises(model.ModelFault):
            runtime.record_attempt(row['run_id'], 'search_online_tracks', {'query':'情歌','provider':'qq'}, attempted)

    def test_complete_romance_run_uses_shared_ledger_and_stops_at_goal(self):
        import httpx
        tracks = []
        for i in range(3):
            payload = library.TrackInput(title=f'甜蜜歌曲{i}', artist=f'歌手{i}', provider='netease', identifier=str(500+i),
                webpage_url=f'https://music.163.com/song?id={500+i}', duration=180)
            with library.database() as cursor:
                key = library.save_track(cursor, payload)
                library.write_lyrics(cursor, key, {'text':'[00:01]与你相爱的每一天', 'source':'本地 LRC',
                    'signature':'', 'checked_at':time.time(), 'updated_at':time.time(), 'manual':True})
            tracks.append(payload.model_dump())
        response = self.client.post('/api/music/recommendations', json={'need':'谈恋爱',
            'preferences':{'target_count':3, 'source_mode':'discover'}}, headers={'Idempotency-Key':secrets.token_hex(16)})
        response.raise_for_status()
        rid = response.json()['run_id']
        semantic = {'vocal_role':'lead', 'language':'中文', 'themes':['甜蜜'], 'moods':['浪漫'],
                    'scene_scores':{'情歌':.9}, 'confidence':.9}
        requests = []
        async def provider(request):
            requests.append(request)
            body = json.loads(request.content)
            if body.get('tools'):
                message = {'role':'assistant','content':None,'tool_calls':[{
                    'id':'recall', 'type':'function', 'function':{'name':'search_online_tracks',
                    'arguments':'{"provider":"netease","query":"情歌","page":1}'}}]}
                tokens = 100
            else:
                evidence = model.json_value(body['messages'][1]['content'])
                value = semantic if 'lyrics' in evidence else {key:semantic for key in evidence}
                message, tokens = {'role':'assistant','content':json.dumps(value)}, 500
            return httpx.Response(200, json={'id':'mock', 'object':'chat.completion', 'created':0, 'model':'fake',
                'choices':[{'index':0, 'message':message, 'finish_reason':'tool_calls' if body.get('tools') else 'stop'}],
                'usage':{'prompt_tokens':tokens-10, 'completion_tokens':10, 'total_tokens':tokens}})
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(provider), **kwargs)
        config = {'api_key':'fake','base_url':'https://model.test/v1','model':'fake'}
        with self.real_mcp_transport(), patch.object(model.ai, 'load_config', return_value=config), \
             patch.object(model, 'AsyncClient', side_effect=factory), \
             patch.object(service.music, '_search', return_value=[SimpleNamespace(model_dump=lambda t=t:t) for t in tracks]), \
             patch.object(validation, 'validate', return_value={'checked_at':time.time(), 'identity':True,
                 'playable':True, 'full':True, 'duration':180, 'reason':'mock full playback'}):
            runtime.run(rid)
        row = service.current_run(rid)
        self.assertEqual(row['status'], 'finished')
        self.assertEqual(row['request']['parsed']['scene'], '情歌')
        self.assertEqual(len(row['result']['tracks']), 3)
        self.assertEqual(row['result']['gap']['count'], 0)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(not json.loads(r.content).get('tools') for r in requests))
        self.assertEqual(row['context']['budget']['model_calls'], 2)
        self.assertEqual(row['context']['budget']['tokens'], 1000)
        self.assertEqual(row['context']['budget']['search_rounds'], 1)
        self.assertEqual(budget.usage(row['context']), {'actual':1000, 'reserved':0, 'unconfirmed':0})
        self.assertTrue(all(r['dispatched'] for r in row['context']['model_requests']))

    def performance_candidates(self, row, count, analyzed=0):
        tracks = [library.TrackInput(provider='netease', identifier=str(8000+i), title=f'并发歌曲{i}',
            artist='歌手', duration=180, webpage_url=f'https://music.163.com/song?id={8000+i}').model_dump()
            for i in range(count)]
        service.add_candidates(row['run_id'], tracks, {'kind':'online_search', 'label':'test', 'provider':'netease'})
        analysis = {'features':{'vocal_role':'lead', 'vocals':True, 'language':'中文', 'themes':['甜蜜'],
                                'scene_scores':{'放松':.9, '情歌':.9}},
                    'evidence':{'fields':{k:{'confidence':.9} for k in ('vocals','vocal_role','language','scene_scores')}}}
        current = service.current_run(row['run_id'])
        tracks = current['context']['candidates']
        for track in tracks[:analyzed]:
            service.replace_candidate(row['run_id'], track['key'], analysis=analysis)
        return service.current_run(row['run_id'])['context']['candidates']

    def test_lyrics_workers_keep_identity_lease_and_shared_network_budget(self):
        row = self.running({'scene':'情歌'})
        tracks = self.performance_candidates(row, 3)
        barrier = threading.Barrier(3)
        observed = []
        lock = threading.Lock()
        def lyrics(track, charge, check=None):
            check()
            service.network.charge_request()
            with lock:
                observed.append((accounts.current().id, accounts.owner_id(), store.execution_lease.get()))
            barrier.wait(timeout=2)
            return {'text':'[00:01]与你相爱的每一天', 'source':'test', 'document_complete':True, 'instrumental':None}
        def semantic(run_id, jobs):
            return {j['key']:{'vocal_role':'lead', 'language':'中文', 'themes':['甜蜜'], 'moods':['浪漫'],
                'scene_scores':{'情歌':.9}, 'confidence':.9} for j in jobs}
        with patch.object(features.platforms, 'track_lyrics', side_effect=lyrics), \
             patch.object(features, '_semantic', side_effect=semantic) as semantic_call:
            response = service.execute(auth.issue(row), 'get_track_features', {'keys':[t['key'] for t in tracks]})
        self.assertIsNone(response['error'], response)
        self.assertEqual(len(response['data']['tracks']), 3)
        self.assertEqual(observed, [(self.user_id, self.user_id, (row['run_id'],row['worker_id']))]*3)
        semantic_call.assert_called_once()
        context = service.current_run(row['run_id'])['context']
        self.assertEqual(context['budget']['network_requests'], 3)
        self.assertEqual(context['budget']['analysis'], 3)
        self.assertEqual(context['performance']['stages']['lyrics']['count'], 3)
        with patch.object(features.platforms, 'track_lyrics', side_effect=AssertionError('Cached features must skip lyrics')), \
             patch.object(features, '_semantic', side_effect=AssertionError('Cached features must skip model')):
            cached = service.execute(auth.issue(row), 'get_track_features', {'keys':[t['key'] for t in tracks]})
        self.assertTrue(all(t['cached'] for t in cached['data']['tracks']))

    def test_explicit_metadata_exclusion_skips_analysis_download_and_model(self):
        row = self.running({'exclusions':['歌手']})
        track = self.performance_candidates(row, 1)[0]
        with patch.object(features, 'analyze_many', side_effect=AssertionError('Excluded tracks must skip analysis')), \
             patch.object(validation, 'validate', side_effect=AssertionError('Excluded tracks must skip media')):
            response = service.execute(auth.issue(row), 'get_track_features', {'keys':[track['key']]})
        self.assertIsNone(response['error'], response)
        current = service.current_run(row['run_id'])
        self.assertFalse(service.qualified(current))
        self.assertNotIn('analysis', current['context']['budget'])
        self.assertNotIn('model_calls', current['context']['budget'])
        self.assertIn('排除项', current['context']['candidates'][0]['validation']['reason'])

    def test_cached_candidates_are_prioritized_without_changing_stored_order(self):
        row = self.running()
        tracks = self.performance_candidates(row, 2)
        with library.database() as cursor:
            cursor.execute('INSERT INTO music_track_features (user_id,track_key,track_snapshot,features,evidence,'
                'content_signature,analyzer_version,checked_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (self.user_id, tracks[1]['key'], '{}', '{}', '{}', 'a'*64, features.ANALYZER_VERSION,
                 time.time(), time.time()+600))
        self.assertEqual([t['key'] for t in features.prioritize_cached(tracks)], [tracks[1]['key'], tracks[0]['key']])
        self.assertEqual([t['key'] for t in service.current_run(row['run_id'])['context']['candidates']], [t['key'] for t in tracks])

    def test_completed_validation_is_visible_while_other_source_is_waiting(self):
        row = self.running()
        tracks = self.performance_candidates(row, 3, analyzed=3)
        slow_started, release, fast_returned = threading.Event(), threading.Event(), threading.Event()
        active = maximum = 0
        lock = threading.Lock()
        def verify(track, charge, allow_network, check=None):
            nonlocal active, maximum
            check()
            service.network.charge_request()
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if track['key'] == tracks[1]['key']:
                    slow_started.set()
                    self.assertTrue(release.wait(3))
                elif track['key'] == tracks[0]['key']:
                    self.assertTrue(slow_started.wait(2))
                    fast_returned.set()
                return {'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}
            finally:
                with lock:
                    active -= 1
        with patch.object(validation, 'validate', side_effect=verify), ThreadPoolExecutor(max_workers=1) as pool:
            future = work.submit(pool, service.execute, auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks]})
            try:
                self.assertTrue(fast_returned.wait(2))
                deadline = time.monotonic()+2
                while time.monotonic() < deadline:
                    public = store.public(service.current_run(row['run_id']))
                    if any(t['key'] == tracks[0]['key'] for t in public['progress']['preview']):
                        break
                    release.wait(.01)
                self.assertTrue(any(t['key'] == tracks[0]['key'] for t in public['progress']['preview']))
                self.assertFalse(future.done())
                self.assertIn('first_qualified_seconds', service.current_run(row['run_id'])['context']['performance'])
            finally:
                release.set()
            response = future.result(timeout=3)
        self.assertIsNone(response['error'], response)
        self.assertEqual(maximum, 2)
        self.assertEqual(len(response['data']['tracks']), 3)
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['network_requests'], 3)

    def test_next_lyrics_prefetch_overlaps_validation_without_model_calls(self):
        row = self.running({'target_count':8})
        tracks = self.performance_candidates(row, 6, analyzed=3)
        entered, prefetched = threading.Event(), threading.Event()
        warmed = []
        lock = threading.Lock()
        def prefetch(track, charge, check):
            self.assertTrue(entered.wait(2))
            check()
            self.assertTrue(work.prefetching.get())
            service.network.charge_request()
            with lock:
                warmed.append(track['key'])
                if len(warmed) == 3:
                    prefetched.set()
        def verify(track, charge, allow_network, check=None):
            entered.set()
            self.assertTrue(prefetched.wait(2))
            check()
            return {'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}
        with patch.object(features, 'prefetch_lyrics', side_effect=prefetch), \
             patch.object(validation, 'validate', side_effect=verify), \
             patch.object(model, 'complete', side_effect=AssertionError('Prefetch must never call model')):
            response = service.execute(auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks[:3]]})
        self.assertIsNone(response['error'], response)
        self.assertEqual(set(warmed), {t['key'] for t in tracks[3:]})
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['network_requests'], 3)
        self.assertTrue(all(not t.get('analysis') for t in service.current_run(row['run_id'])['context']['candidates'][3:]))

    def test_prefetch_does_not_consume_playback_network_reserve(self):
        row = self.running({'target_count':8})
        tracks = self.performance_candidates(row, 6, analyzed=3)
        with library.database() as cursor:
            row = store.get(cursor, row['run_id'], lock=True)
            used = row['context']['limits']['network_requests']*3//4
            row = store.update(cursor, row, context=row['context'] | {'budget':{'network_requests':used}})
        def verify(track, charge, allow_network, check=None):
            service.network.charge_request()
            return {'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}
        with patch.object(features, 'prefetch_lyrics', side_effect=AssertionError('Reserve forbids prefetch')), \
             patch.object(validation, 'validate', side_effect=verify):
            response = service.execute(auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks[:3]]})
        self.assertIsNone(response['error'], response)
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['network_requests'], used+3)

    def test_duration_goal_stops_other_validation_and_does_not_publish_it(self):
        row = self.running({'goal':'duration', 'target_duration_minutes':1})
        tracks = self.performance_candidates(row, 2, analyzed=2)
        entered, stopped = threading.Event(), threading.Event()
        def verify(track, charge, allow_network, check=None):
            if track['key'] == tracks[0]['key']:
                self.assertTrue(entered.wait(2))
                return {'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}
            entered.set()
            try:
                while not stopped.wait(.02):
                    check()
            finally:
                stopped.set()
        with patch.object(validation, 'validate', side_effect=verify):
            response = service.execute(auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks]})
        self.assertIsNone(response['error'], response)
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(response['data']['tracks']), 1)
        current = service.current_run(row['run_id'])
        self.assertEqual(sum(bool(t.get('validation')) for t in current['context']['candidates']), 1)

    def test_concurrent_prefetch_admission_cannot_cross_reserve_boundary(self):
        row = self.running({'target_count':8})
        tracks = self.performance_candidates(row, 6, analyzed=3)
        with library.database() as cursor:
            row = store.get(cursor, row['run_id'], lock=True)
            boundary = row['context']['limits']['network_requests']*3//4
            row = store.update(cursor, row, context=row['context'] | {'budget':{'network_requests':boundary-1}})
        barrier = threading.Barrier(3)
        finished = threading.Event()
        admitted, completed = [], []
        lock = threading.Lock()
        def prefetch(track, charge, check):
            barrier.wait(timeout=2)
            try:
                service.network.charge_request()
                with lock:
                    admitted.append(track['key'])
            finally:
                with lock:
                    completed.append(track['key'])
                    if len(completed) == 3:
                        finished.set()
        def verify(track, charge, allow_network, check=None):
            self.assertTrue(finished.wait(2))
            service.network.charge_request()
            return {'checked_at':time.time(), 'identity':True, 'playable':True, 'full':True, 'duration':180}
        with patch.object(features, 'prefetch_lyrics', side_effect=prefetch), \
             patch.object(validation, 'validate', side_effect=verify):
            response = service.execute(auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks[:3]]})
        self.assertIsNone(response['error'], response)
        self.assertEqual(len(admitted), 1)
        self.assertEqual(service.current_run(row['run_id'])['context']['budget']['network_requests'], boundary+3)

    def test_audio_is_reused_between_tools_and_cleaned_after_validation(self):
        row = self.running()
        track = self.performance_candidates(row, 1)[0]
        seen = []
        class AudioResponse:
            headers = {'Content-Length':'5'}
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
            def raise_for_status(self): pass
            def close(self): pass
            def iter_content(self, size): yield b'audio'
        def audio(path, check=None):
            check()
            seen.append(path)
            return {'duration':180, 'signature':'full', 'tempo':80, 'energy':.1, 'variation':.1, 'rhythm_stability':.9}
        with work.task(row), \
             patch.object(features.platforms, 'track_lyrics', return_value={'text':'纯音乐', 'instrumental':True,
                 'source':'test', 'document_complete':True}), patch.object(features, 'audio_features', side_effect=audio), \
             patch.object(validation.music, 'resolve', return_value=('https://audio.test/full', {}, 'test')), \
             patch.object(validation.network, 'public_get', return_value=AudioResponse()) as download, \
             patch.object(validation.music, 'parse_url', return_value=SimpleNamespace(model_dump=lambda:track)), \
             patch.object(work, 'probe', return_value={'duration':180}), \
             patch.object(work, 'run_process', return_value=SimpleNamespace(returncode=0)):
            analyzed = service.execute(auth.issue(row), 'get_track_features', {'keys':[track['key']]})
            self.assertIsNone(analyzed['error'], analyzed)
            self.assertTrue(seen[0].is_file())
            validated = service.execute(auth.issue(row), 'validate_tracks', {'keys':[track['key']]})
            self.assertIsNone(validated['error'], validated)
            self.assertTrue(validated['data']['tracks'][0]['full'])
            self.assertEqual(download.call_count, 1)
            self.assertFalse(seen[0].exists())
        context = service.current_run(row['run_id'])['context']
        self.assertEqual(context['performance']['stages']['audio_download']['cache_hits'], 1)

    def test_cancelled_prefetch_cannot_write_lyrics_or_publish_validation(self):
        from backend.modules.music import lyric_lookup
        row = self.running({'target_count':8})
        tracks = self.performance_candidates(row, 6, analyzed=3)
        entered, release = threading.Event(), threading.Event()
        def remote(track):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'text':'未经取消允许不能写入', 'source':'test', 'document_complete':True, 'instrumental':None}
        def verify(track, charge, allow_network, check=None):
            entered.wait(2)
            while not release.wait(.02):
                check()
            check()
            raise AssertionError('Cancelled validation must not return a result')
        with patch.object(lyric_lookup, 'remote', side_effect=remote), \
             patch.object(validation, 'validate', side_effect=verify), ThreadPoolExecutor(max_workers=1) as pool:
            future = work.submit(pool, service.execute, auth.issue(row), 'validate_tracks', {'keys':[t['key'] for t in tracks[:3]]})
            try:
                self.assertTrue(entered.wait(2))
                with library.database() as cursor:
                    current = store.get(cursor, row['run_id'], lock=True)
                    store.update(cursor, current, status='cancelled', worker_id=None, lease_until=None)
            finally:
                release.set()
            with self.assertRaises(HTTPException):
                future.result(timeout=3)
        current = service.current_run(row['run_id'])
        self.assertTrue(all(not t.get('validation') for t in current['context']['candidates']))
        with library.database() as cursor:
            cursor.execute('SELECT COUNT(*) AS n FROM music_lyrics WHERE user_id=%s', (self.user_id,))
            self.assertEqual(cursor.fetchone()['n'], 0)

    def test_feature_cache_invalidates_on_network_model_and_lyrics_changes(self):
        row = self.running()
        payload = library.TrackInput(title="歌曲", artist="歌手", provider="netease", identifier="123",
                                     webpage_url="https://music.163.com/song?id=123", duration=180)
        with library.database() as cursor:
            key = library.save_track(cursor, payload)
            library.write_lyrics(cursor,key,{"text":"[00:01]今天一起听音乐", "source":"本地 LRC",
                "signature":"", "checked_at":time.time(), "updated_at":time.time(), "manual":True})
        track = payload.model_dump() | {"key":key, "user_id":self.user_id}
        response = {"content":json.dumps({"language":"中文", "themes":["治愈"], "moods":["放松"],
                                          "scene_scores":{"放松":.9}, "confidence":.9})}
        with patch.object(model,"complete",return_value=response) as semantic:
            first = features.analyze(row["run_id"],track,lambda **_:None,False)
            self.assertFalse(first["cached"])
            self.assertTrue(features.analyze(row["run_id"],track,lambda **_:None,False)["cached"])
            online = features.analyze(row["run_id"],track,lambda **_:None,True)
            self.assertFalse(online["cached"])
            self.assertTrue(online["evidence"]["network_analysis"])
            self.assertTrue(features.analyze(row["run_id"],track,lambda **_:None,True)["cached"])
            with patch.dict(accounts.os.environ, {"LLM_MODEL": "changed-semantic-model"}):
                self.assertFalse(features.analyze(row["run_id"],track,lambda **_:None,True)["cached"])
                with library.database() as cursor:
                    cursor.execute("UPDATE music_lyrics SET text=%s WHERE track_id=%s",("[00:01]改变歌词后的新内容",key))
                self.assertFalse(features.analyze(row["run_id"],track,lambda **_:None,True)["cached"])
            self.assertEqual(semantic.call_count,3)

    def test_tool_budget_returns_structured_error(self):
        row = self.running()
        with library.database() as cursor:
            row = store.update(cursor,row,context=row["context"] | {"budget":{"tools":60}})
        response = service.execute(auth.issue(row),"list_music_categories",{})
        self.assertEqual(response["error"]["code"],"BUDGET_EXHAUSTED")
        self.assertEqual(response["cost"].get("tools"),0)

    def test_personal_first_requires_full_candidate_checks_before_online_search(self):
        row = self.running({"target_count":2})
        track = {"title":"Track", "artist":"Artist", "provider":"local", "identifier":"test",
                 "webpage_url":"/api/files/test.wav", "key":"key", "source":{"kind":"personal","label":"我的歌单"}}
        with library.database() as cursor:
            row = store.update(cursor,row,context=row["context"] | {"personal_searched":True,
                "coverage":{"personal_total":2,"personal_checked_until":1},"candidates":[track]})
        token = auth.issue(row)
        with patch.object(service.platforms,"search_playlists",return_value={"playlists":[]}) as online:
            for context in [row["context"], row["context"] | {"candidates":[track | {
                "analysis":{"features":{}},"validation":{"identity":False}}]}]:
                with library.database() as cursor:
                    current = store.get(cursor,row["run_id"],lock=True)
                    store.update(cursor,current,context=context)
                response = service.execute(token,"search_online_playlists",{"provider":"netease","query":"放松"})
                self.assertEqual(response["error"]["code"],"409")
            online.assert_not_called()
            with library.database() as cursor:
                current = store.get(cursor,row["run_id"],lock=True)
                store.update(cursor,current,context=current["context"] | {
                    "coverage":{"personal_total":2,"personal_checked_until":2}})
            self.assertIsNone(service.execute(token,"search_online_playlists",{
                "provider":"netease","query":"放松"})["error"])
            online.assert_called_once()

    def test_full_decode_required_for_local_audio_with_unknown_duration(self):
        track = {"provider":"local", "duration":None}
        from contextlib import contextmanager
        @contextmanager
        def audio(*args, **kwargs):
            yield self.root/"test.wav", "本地完整文件"
        with patch.object(validation,"local_path",return_value=self.root/"test.wav"), \
             patch.object(validation,"audio_file",audio), \
             patch.object(validation,"probe_audio",return_value={"duration":60}), \
             patch.object(validation.subprocess,"run",return_value=SimpleNamespace(returncode=1)) as decoder:
            value = validation.validate(track,lambda **_:None,False)
        decoder.assert_called_once()
        self.assertFalse(value["full"])
        self.assertIn("解码",value["reason"])

    def test_worker_replacement_during_parsing_cannot_update_request(self):
        row,_,_ = self.create()
        def replace_worker(run_id, request, loaded):
            marker = store.execution_lease.set(None)
            try:
                with library.database() as cursor:
                    current = store.get(cursor,run_id,lock=True)
                    store.update(cursor,current,worker_id="replacement")
            finally:
                store.execution_lease.reset(marker)
            return model.offline_parse(request["need"],{})
        with patch.object(model,"parse",side_effect=replace_worker), patch.object(mcp,"LocalClient") as client:
            runtime.run(row["run_id"])
        client.assert_not_called()
        with library.database() as cursor:
            current = store.get(cursor,row["run_id"])
        self.assertEqual(current["status"],"running")
        self.assertEqual(current["worker_id"],"replacement")
        self.assertNotIn("parsed",current["request"])
        self.assertIsNone(store.execution_lease.get())

    def test_waiting_state_charges_elapsed_time_and_snapshot_limit_is_atomic(self):
        row = self.running()
        with library.database() as cursor:
            row = store.update(cursor,row,context=row["context"] | {"active_started_at":time.time()-23})
        runtime.stop_state(row["run_id"],"waiting_auth","login",row["worker_id"])
        with library.database() as cursor:
            paused = store.get(cursor,row["run_id"])
        self.assertGreaterEqual(paused["context"]["budget"]["seconds"],23)
        self.assertLess(paused["context"]["budget"]["seconds"],26)
        self.client.post(f"/api/music/recommendations/{row['run_id']}/continue",json={}).raise_for_status()
        resumed = runtime.claim(row["run_id"])
        self.assertEqual(resumed["context"]["base_seconds"],paused["context"]["budget"]["seconds"])
        with self.assertRaises(HTTPException) as error:
            with library.database() as cursor:
                current = store.get(cursor,row["run_id"],lock=True)
                store.update(cursor,current,context=current["context"] | {"oversize":"x"*524288})
        self.assertEqual(error.exception.status_code,413)
        with library.database() as cursor:
            self.assertNotIn("oversize",store.get(cursor,row["run_id"])["context"])

    def test_network_fallbacks_cannot_swallow_budget_and_old_worker_cannot_write(self):
        from backend.infrastructure import network
        from backend.modules.music import sources as music
        marker = network.request_guard.set(lambda: (_ for _ in ()).throw(budget.BudgetExceeded("limit")))
        try:
            with self.assertRaises(budget.BudgetExceeded):
                music.resolve(music.Track(title="x",artist="a",provider="netease",identifier="1",webpage_url="https://music.163.com/song?id=1"),{},public_only=True)
        finally:
            network.request_guard.reset(marker)
        row = self.running()
        with library.database() as cursor:
            store.update(cursor,row,worker_id="replacement")
        marker = store.execution_lease.set((row["run_id"],row["worker_id"]))
        try:
            with self.assertRaises(HTTPException):
                service.replace_candidate(row["run_id"],"any",validation={})
        finally:
            store.execution_lease.reset(marker)

    def test_session_revocation(self):
        row = self.running()
        token = auth.issue(row)
        with library.database() as cursor:
            cursor.execute("UPDATE users SET auth_version=auth_version+1 WHERE id=%s",(self.user_id,))
        try:
            with self.assertRaises(HTTPException):
                auth.claims(token)
        finally:
            with library.database() as cursor:
                cursor.execute("UPDATE users SET auth_version=auth_version-1 WHERE id=%s",(self.user_id,))

    def test_runtime_model_decides_search_analyze_validate_then_finishes(self):
        row,_,_ = self.create({"target_count":1})
        track = {"provider":"netease","identifier":"123","title":"歌曲","artist":"歌手","duration":180,
                 "webpage_url":"https://music.163.com/song?id=123"}
        with library.database() as cursor:
            key = library.save_track(cursor,library.TrackInput.model_validate(track))
            cursor.execute("INSERT INTO music_favorites (track_id,created_at,user_id) VALUES (%s,%s,%s)",(key,time.time(),self.user_id))
        parent = self
        class InProcessClient:
            def __init__(self,token): self.token=token
            def tools(self): return []
            def call(self,name,args): return service.execute(self.token,name,args)
        def decision(run_id,messages,tools=None,**kwargs):
            current = service.current_run(run_id)
            candidates = current["context"].get("candidates",[])
            if not candidates: name,args="search_my_music",{}
            elif not candidates[0].get("analysis"): name,args="get_track_features",{"keys":[key]}
            else: name,args="validate_tracks",{"keys":[key]}
            return {"role":"assistant","content":"","tool_calls":[{"id":secrets.token_hex(6),"type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}
        analysis = {"features":{"vocals":True,"language":"中文","themes":["治愈"],"scene_scores":{"放松":.9}},
                    "evidence":{"fields":{"vocals":{"confidence":.9},"language":{"confidence":.9},"scene_scores":{"confidence":.9}}}}
        with patch.object(model,"parse",return_value=model.offline_parse("放松",{"target_count":1})),\
             patch.object(model,"complete",side_effect=decision),patch.object(mcp,"LocalClient",InProcessClient),\
             patch.object(features,"analyze_many",side_effect=lambda rid, tracks, *a, **kw: [(t, analysis) for t in tracks]),\
             patch.object(validation,"validate",return_value={"checked_at":time.time(),"identity":True,"playable":True,"full":True,"duration":180}):
            runtime.run(row["run_id"])
        with library.database() as cursor:
            finished = store.get(cursor,row["run_id"])
        self.assertEqual(finished["status"],"finished")
        self.assertEqual([s["tool"] for s in finished["steps"]],["search_my_music","get_track_features","validate_tracks"])

    def test_artist_runtime_searches_both_artists_and_preserves_scene_rejection(self):
        prefs = {"target_count": 1, "source_mode": "discover", "preferred_artists": ["EXO", "BIGBANG"]}
        public, _, _ = self.create(prefs)
        class InProcessClient:
            def __init__(self, token): self.token = token
            def tools(self): return []
            def call(self, name, args): return service.execute(self.token, name, args)
        queries, evaluated = [], []
        def search(provider, query, page):
            queries.append(query)
            return [library.TrackInput(provider=provider, identifier=str(index + (100 if query == 'EXO' else 200)),
                title=query + str(index), artist=query, duration=180,
                webpage_url='https://music.163.com/song?id=' + str(index + (100 if query == 'EXO' else 200))) for index in range(2)]
        def analyze(run_id, track, *args, **kwargs):
            evaluated.append(track['artist'])
            score = .1 if track['title'] == 'EXO0' else .9 if track['artist'] == 'EXO' else .7
            return {'features': {'vocal_role': 'lead', 'vocals': True, 'language': '韩文', 'scene_scores': {'游戏': score}},
                    'evidence': {'fields': {k: {'confidence': .9} for k in ('vocal_role', 'vocals', 'language', 'scene_scores')}}}
        parsed = model.offline_parse('游戏', prefs)
        with patch.object(model, 'parse', return_value=parsed), patch.object(mcp, 'LocalClient', InProcessClient), \
             patch.object(service.music, '_search', side_effect=search), patch.object(features, 'analyze_many', side_effect=lambda rid, tracks, *a, **kw: [(t, analyze(rid, t, *a, **kw)) for t in tracks]), \
             patch.object(validation, 'validate', return_value={'checked_at': time.time(), 'identity': True,
                 'playable': True, 'full': True, 'duration': 180}), \
             patch.object(model, 'complete', side_effect=AssertionError('Artist work should be scheduled before generic discovery')):
            runtime.run(public['run_id'])
        with library.database() as cursor:
            finished = store.get(cursor, public['run_id'])
        self.assertEqual(finished['status'], 'finished', finished['context'].get('message'))
        self.assertEqual(queries, ['EXO', 'BIGBANG'])
        self.assertEqual(evaluated, ['EXO', 'BIGBANG', 'EXO', 'BIGBANG'])
        self.assertEqual(finished['result']['tracks'][0]['title'], 'EXO1')
        match = finished['result']['tracks'][0]['match']
        self.assertEqual(match['score'], 87.2)
        self.assertEqual(match['breakdown']['artist'], 20)
        self.assertEqual(match['matched_artists'], ['EXO'])
        self.assertEqual([a['evaluated_count'] for a in finished['result']['coverage']['artists']], [2, 2])
        rejected = next(t for t in finished['result']['rejected'] if t['title'] == 'EXO0')
        self.assertIn('曲目级证据与场景不匹配', rejected['reasons'])

    def test_targeted_personal_search_does_not_claim_full_library_coverage(self):
        row = self.running({'preferred_artists': ['EXO']})
        response = service.execute(auth.issue(row), 'search_my_music', {'query': 'EXO'})
        self.assertIsNone(response['error'])
        with library.database() as cursor:
            current = store.get(cursor, row['run_id'])
        self.assertFalse(current['context'].get('personal_searched'))
        self.assertNotIn('personal_total', current['context'].get('coverage', {}))
        self.assertEqual(current['context']['artist_searches'], [{'artist': 'EXO', 'source': 'personal', 'success': True}])

    def test_fifth_search_allowed_sixth_blocked_but_discovered_playlist_drains(self):
        row = self.running({"target_count":1})
        track = {"provider":"netease","identifier":"456","title":"歌曲","artist":"歌手","duration":180,
                 "webpage_url":"https://music.163.com/song?id=456"}
        with library.database() as cursor:
            row = store.update(cursor,row,context=row["context"] | {"personal_searched":True,"budget":{"search_rounds":4},
                "online_playlists":[{"provider":"netease","playlist_id":"789"}]})
        budget.charge(row['run_id'],search_rounds=1)
        with self.assertRaises(budget.BudgetExceeded) as exc:
            budget.charge(row['run_id'],search_rounds=1)
        self.assertEqual(exc.exception.resource,'search_rounds')
        class InProcessClient:
            def __init__(self, token): self.token=token
            def tools(self): return []
            def call(self,name,args): return service.execute(self.token,name,args)
        analysis = {"features":{"vocals":True,"vocal_role":"lead","language":"中文","scene_scores":{"放松":.9}},
                    "evidence":{"fields":{k:{"confidence":.9} for k in ('vocals','vocal_role','language','scene_scores')}}}
        playlist = {"playlist":{"name":"new"},"tracks":[track],"next_offset":1,"has_more":False,"coverage":{}}
        with patch.object(mcp,'LocalClient',InProcessClient), patch.object(model,'complete',side_effect=AssertionError('search budget exhausted')), \
             patch.object(platforms_for_test(),'playlist_tracks',return_value=playlist), patch.object(features,'analyze_many',side_effect=lambda rid, tracks, *a, **kw: [(t, analysis) for t in tracks]), \
             patch.object(validation,'validate',return_value={"checked_at":time.time(),"identity":True,"playable":True,"full":True,"duration":180}):
            runtime.execute_run(row,time.monotonic())
        result=service.current_run(row['run_id'])
        self.assertEqual(result['status'],'finished')
        self.assertEqual([s['tool'] for s in result['steps']],['get_playlist_tracks','get_track_features','validate_tracks'])
        self.assertEqual(result['context']['budget']['search_rounds'],5)

    def test_result_lyrics_version_owner_and_manual_cache(self):
        from backend.modules.music import lyric_lookup
        row=self.running()
        track={"provider":"netease","identifier":"999","title":"歌曲","artist":"歌手","duration":180,
               "webpage_url":"https://music.163.com/song?id=999"}
        with library.database() as cursor:
            key=library.save_track(cursor,library.TrackInput.model_validate(track))
            library.write_lyrics(cursor,key,{"text":"[00:01]手动歌词","source":"本地 LRC"})
            store.update(cursor,row,status='partial',result_version=1,result={"tracks":[track | {"key":key}]})
        url=f"/api/music/recommendations/{row['run_id']}/tracks/{key}/lyrics"
        with patch.object(lyric_lookup,'remote',side_effect=AssertionError('manual cache must win')):
            result=self.client.post(url,json={'result_version':1})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['lines'][0]['text'],'手动歌词')
        self.assertEqual(self.client.post(url,json={'result_version':2}).status_code,409)
        self.assertEqual(self.client.post(url.replace(key,'unknown'),json={'result_version':1}).status_code,404)
        with library.database() as cursor:
            cursor.execute('UPDATE music_recommendation_runs SET user_id=1 WHERE run_id=%s',(row['run_id'],))
        self.assertEqual(self.client.post(url,json={'result_version':1}).status_code,404)

    def test_credit_only_analysis_does_not_call_model_or_infer_vocals(self):
        row=self.running()
        track={"provider":"netease","identifier":"1963232440","title":"The Runner","artist":"Yubik","duration":396,
               "webpage_url":"https://music.163.com/song?id=1963232440"}
        with library.database() as cursor:
            key=library.save_track(cursor,library.TrackInput.model_validate(track))
            library.write_lyrics(cursor,key,{"text":"[00:00]作词：Maximilian Hacker\n[00:00]作曲：Maximilian Hacker","source":"本地 LRC"})
        with patch.object(model,'complete',side_effect=AssertionError('credits cannot be lyrics')):
            result=features.analyze(row['run_id'],track | {'key':key,'user_id':self.user_id},lambda **_:None,True)
        self.assertIsNone(result['features']['vocals'])
        self.assertIsNone(result['features']['language'])
        self.assertEqual(result['features']['vocal_role'],'unknown')


    def test_shared_lyrics_cache_preserves_platform_declaration_and_invalidates_features(self):
        from backend.modules.music import lyric_lookup
        row=self.running()
        track={"provider":"netease","identifier":"111222","title":"器乐","artist":"作者","duration":180,
               "webpage_url":"https://music.163.com/song?id=111222"}
        with patch.object(lyric_lookup,'remote',return_value={'text':'','source':'平台','instrumental':True,'document_complete':True}) as remote:
            first=lyric_lookup.lookup(track)
            second=lyric_lookup.lookup(track)
        remote.assert_called_once()
        self.assertTrue(first['instrumental'])
        self.assertTrue(second['instrumental'])
        with library.database() as cursor:
            key=library.track_id(library.TrackInput.model_validate(track))
        item=track | {'key':key,'user_id':self.user_id}
        analysis=features.analyze(row['run_id'],item,lambda **_:None,True)
        self.assertEqual(analysis['features']['vocal_role'],'instrumental')
        with library.database() as cursor:
            library.write_lyrics(cursor,key,{'text':'[00:01]人工歌词','source':'本地 LRC'})
            cursor.execute('SELECT expires_at FROM music_track_features WHERE user_id=%s AND track_key=%s',(self.user_id,key))
            self.assertEqual(cursor.fetchone()['expires_at'],0)
        with patch.object(lyric_lookup,'remote',side_effect=AssertionError('manual data must win')):
            self.assertIn('人工歌词',lyric_lookup.lookup(track)['text'])


def platforms_for_test():
    from backend.modules.music.agent import platforms
    return platforms


if __name__=="__main__":
    unittest.main()
