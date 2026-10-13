from __future__ import annotations

import io
import json
import runpy
import secrets
import tempfile
import time
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pymysql
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import accounts, library, main
from backend.music_agent import auth, budget, features, mcp, model, runtime, service, skill, store, validation


class MatchTests(unittest.TestCase):
    def candidate(self, vocals=False, language=None):
        return {"key":"k", "title":"Track", "artist":"Artist", "duration":180, "source":{"kind":"personal"},
                "analysis":{"features":{"vocals":vocals,"language":language,"themes":[],"scene_scores":{}},
                            "evidence":{"fields":{"vocals":{"confidence":.9},"language":{"confidence":.9}}}},
                "validation":{"identity":True,"playable":True,"full":True}}

    def test_instrumental_ignores_language_but_unknown_vocals_fails(self):
        parsed = model.offline_parse("纯音乐",{})
        self.assertTrue(features.match(self.candidate(),parsed)["qualified"])
        self.assertFalse(features.match(self.candidate(None),parsed)["qualified"])
        self.assertFalse(features.match(self.candidate(True,"英文"),model.offline_parse("工作",{}))["qualified"])

    def test_partial_audio_does_not_prove_sleep_and_theme_exclusion_needs_lyrics(self):
        track = self.candidate()
        self.assertFalse(features.match(track,model.offline_parse("睡觉",{}))["qualified"])
        track = self.candidate(True,"中文")
        self.assertFalse(features.match(track,model.offline_parse("不要失恋主题",{}))["qualified"])
        self.assertFalse(features.match(track,model.offline_parse("励志",{}))["qualified"])

    def test_explicit_preferences_and_duration_goal(self):
        parsed = model.offline_parse("打游戏，英文歌，30首，凑够一小时",{"languages":["中文"],"target_count":5})
        self.assertEqual((parsed["languages"],parsed["target_count"],parsed["goal"]),(["中文"],5,"duration"))
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


class AgentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.original_ready = library.schema_ready
        config = library.database_config()
        cls.name = "videodream_agent_test_"+secrets.token_hex(6)
        cls.admin_connection = pymysql.connect(**{k:v for k,v in config.items() if k!="database"})
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.name}` CHARACTER SET utf8mb4")
        cls.stack.enter_context(patch.object(library,"database_config",return_value=config | {"database":cls.name}))
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cls.stack.enter_context(patch.object(library,"DOWNLOAD_DIR",cls.root))
        cls.stack.enter_context(patch.object(accounts,"redis_prefix",return_value="videodream:agent-test:"+secrets.token_hex(8)+":"))
        cls.stack.enter_context(patch.object(runtime,"submit"))
        library.schema_ready = False
        with library.database() as cursor:
            cursor.execute("UPDATE users SET password_hash=%s,must_change_password=FALSE WHERE id=1",(accounts.passwords.hash("Test-admin-123!"),))
        cls.user_id = accounts.create_user(accounts.Credentials(username="agent_user",password="Test-user-123!"))
        cls.client = cls.stack.enter_context(TestClient(main.app,base_url="http://127.0.0.1:8001",headers={"Origin":"http://127.0.0.1:5174","X-VideoDream-CSRF":"1"}))
        response = cls.client.post("/api/auth/login",json={"username":"agent_user","password":"Test-user-123!"})
        response.raise_for_status()
        cls.client.headers["Authorization"] = "Bearer "+response.json()["access_token"]
        cls.sid = cls.client.cookies.get(accounts.COOKIE).split(".")[0]

    @classmethod
    def tearDownClass(cls):
        keys = list(accounts.redis_client().scan_iter(match=accounts.redis_prefix()+"*"))
        if keys:
            accounts.redis_client().delete(*keys)
        cls.stack.close()
        library.schema_ready = cls.original_ready
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.name}`")
        cls.admin_connection.close()

    def setUp(self):
        self.stack = ExitStack()
        self.marker = accounts.principal.set(accounts.Principal(self.user_id,"agent_user","user",self.sid))
        with library.database() as cursor:
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
        path = self.root/"track.wav"
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
            budget.charge(row["run_id"],tokens=80001)
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

    def test_model_request_reserves_budget_and_uses_existing_connection(self):
        row = self.running()
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[],content='{}'))],
                                   usage=SimpleNamespace(total_tokens=15))
        config = {"api_key":"test-key", "base_url":"https://model.test/v1", "model":"test-model"}
        with patch.object(model.ai,"load_config",return_value=config), patch.object(model,"OpenAI") as constructor:
            constructor.return_value.chat.completions.create.return_value = response
            answer = model.complete(row["run_id"],[{"role":"user","content":"test"}],max_output=80,decision=True)
            self.assertEqual(answer["content"],'{}')
            self.assertNotIn("model",constructor.call_args.kwargs)
            constructor.return_value.chat.completions.create.assert_called_once()
        with library.database() as cursor:
            changed = store.get(cursor,row["run_id"])
        self.assertEqual(changed["context"]["budget"]["decisions"],1)
        self.assertGreater(changed["context"]["budget"]["tokens"],80)

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
            with library.database() as cursor:
                config = accounts.settings() | {"ai_model":"changed-semantic-model"}
                cursor.execute("UPDATE system_settings SET config=%s WHERE id=1",(json.dumps(config),))
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

    def test_migration_rollback_preserves_playlists_and_can_be_reapplied(self):
        row = self.running()
        playlist = secrets.token_hex(12)
        with library.database() as cursor:
            cursor.execute("INSERT INTO music_playlists (id,name,description,created_at,user_id) VALUES (%s,'保留歌单','',%s,%s)",
                           (playlist,time.time(),self.user_id))
            store.migrate(cursor)
        script = Path(__file__).resolve().parents[1]/"scripts"/"music-agent-migration.py"
        try:
            with patch("sys.argv",[str(script),"rollback","--apply"]), patch.object(library,"ROOT_DIR",self.root):
                runpy.run_path(str(script),run_name="__main__")
            with library.database() as cursor:
                cursor.execute("SHOW TABLES LIKE 'music_recommendation_runs'")
                self.assertIsNone(cursor.fetchone())
                cursor.execute("SHOW COLUMNS FROM music_playlist_tracks LIKE 'position'")
                self.assertIsNone(cursor.fetchone())
                cursor.execute("SELECT id FROM music_playlists WHERE id=%s",(playlist,))
                self.assertIsNotNone(cursor.fetchone())
                cursor.execute("SELECT deleted FROM resources WHERE kind='music_recommendation' AND resource_key=%s",(row["run_id"],))
                self.assertTrue(cursor.fetchone()["deleted"])
            backups = list((self.root/".run"/"library-backups").glob("*.json"))
            self.assertTrue(backups)
            self.assertIn("music_recommendation_runs",json.loads(backups[0].read_text(encoding="utf-8")))
        finally:
            with library.database() as cursor, patch.object(library,"ROOT_DIR",self.root):
                store.migrate(cursor)

    def test_network_fallbacks_cannot_swallow_budget_and_old_worker_cannot_write(self):
        from backend import network,music
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
                    "evidence":{"fields":{"vocals":{"confidence":.9},"language":{"confidence":.9}}}}
        with patch.object(model,"parse",return_value=model.offline_parse("放松",{"target_count":1})),\
             patch.object(model,"complete",side_effect=decision),patch.object(mcp,"LocalClient",InProcessClient),\
             patch.object(features,"analyze",return_value=analysis),\
             patch.object(validation,"validate",return_value={"checked_at":time.time(),"identity":True,"playable":True,"full":True,"duration":180}):
            runtime.run(row["run_id"])
        with library.database() as cursor:
            finished = store.get(cursor,row["run_id"])
        self.assertEqual(finished["status"],"finished")
        self.assertEqual([s["tool"] for s in finished["steps"]],["search_my_music","get_track_features","validate_tracks"])


def platforms_for_test():
    from backend.music_agent import platforms
    return platforms


if __name__=="__main__":
    unittest.main()
