from __future__ import annotations

import io
import json
import secrets
import tempfile
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.infrastructure import database as db
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.social import api, chat, karaoke, media, moderation, playlists, realtime, rooms, rtc, rules, shares, store as s, worker
from backend.modules.social.models import ConversationInput, MemberInput, MessageInput, RelationInput


class RuleEvidenceTests(unittest.TestCase):
    def test_numeric_filters_require_full_confident_evidence_and_tempo(self):
        config = rules.Config(filters=rules.Filters(bpm_min=90)).model_dump()
        track = {"title": "Example", "features": {"tempo": 110}}
        self.assertIsNone(rules.evaluate(track, config)[0])
        track["evidence"] = {"fields": {"tempo": {"confidence": .9, "coverage": "full"}}}
        self.assertIsNotNone(rules.evaluate(track, config)[0])
        track["features"]["tempo"] = 80
        self.assertIsNone(rules.evaluate(track, config)[0])
        config["filters"]["unknown_feature"] = "allow_unscored"
        score, reasons = rules.evaluate({"title": "Unknown"}, config)
        self.assertEqual(score, 0)
        self.assertIn("未知", " ".join(reasons))


class SocialIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.original_ready, cls.original_database = db.schema_ready, db.schema_database
        config = db.database_config()
        cls.name = "videodream_social_test_" + secrets.token_hex(6)
        cls.admin = pymysql.connect(**{k: v for k, v in config.items() if k != "database"})
        with cls.admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.name}` CHARACTER SET utf8mb4")
        cls.addClassCleanup(cls.cleanup)
        cls.stack.enter_context(patch.object(db, "database_config", return_value=config | {"database": cls.name}))
        cls.prefix = "videodream:social-test:" + secrets.token_hex(8) + ":"
        cls.stack.enter_context(patch.object(s, "PREFIX", cls.prefix))
        cls.stack.enter_context(patch.object(accounts, "redis_prefix", return_value=cls.prefix + "auth:"))
        redis_client = accounts.redis_client()
        cls.stack.enter_context(patch.object(accounts, "redis_client", return_value=redis_client))
        cls.stack.callback(redis_client.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cls.stack.enter_context(patch.object(media, "ROOT", cls.root))
        # The media directory is deliberately constrained by the real containment helper.
        from backend.core import storage
        cls.stack.enter_context(patch.object(storage, "contained", side_effect=lambda p: Path(p)))
        from backend.tests.support import migrate_test_database
        migrate_test_database(config | {"database": cls.name})
        cls.users = [accounts.create_user(accounts.Credentials(username="social_" + str(i), password="Test-social-123!")) for i in range(3)]
        cls.stack.enter_context(patch.object(accounts, "settings", return_value=accounts.DEFAULT_SETTINGS | {"social_realtime_duet_enabled": True}))
        app = FastAPI()
        app.include_router(accounts.router)
        app.include_router(api.router)
        app.add_middleware(accounts.AuthenticationMiddleware)
        cls.client = cls.stack.enter_context(TestClient(app, base_url="http://127.0.0.1:8001", headers={"Origin": "http://127.0.0.1:5174", "X-VideoDream-CSRF": "1"}))

    @classmethod
    def cleanup(cls):
        keys = list(accounts.redis_client().scan_iter(match=cls.prefix + "*"))
        if keys:
            accounts.redis_client().delete(*keys)
        cls.stack.close()
        db.schema_ready, db.schema_database = cls.original_ready, cls.original_database
        with cls.admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.name}`")
        cls.admin.close()

    @contextmanager
    def user(self, index, session=""):
        marker = accounts.principal.set(accounts.Principal(self.users[index], "social_" + str(index), "user", session))
        try:
            yield
        finally:
            accounts.principal.reset(marker)

    def setUp(self):
        with s.database() as cursor:
            # Each test starts from the migrated schema and the same three distinct users.
            cursor.execute("DELETE FROM social_rule_run_bindings")
            cursor.execute("DELETE FROM social_reports")
            cursor.execute("DELETE FROM social_rules")
            cursor.execute("DELETE FROM social_performances")
            cursor.execute("DELETE FROM social_rooms")
            cursor.execute("DELETE FROM social_shares")
            cursor.execute("DELETE FROM social_conversations")
            cursor.execute("DELETE FROM social_karaoke_songs")
            cursor.execute("DELETE FROM social_playlists")
            cursor.execute("DELETE FROM social_assets")
            cursor.execute("DELETE FROM social_relations")
            cursor.execute("DELETE FROM social_outbox")
        keys = list(accounts.redis_client().scan_iter(match=self.prefix + "rate:*"))
        if keys:
            accounts.redis_client().delete(*keys)

    def friend(self, first=0, second=1):
        with self.user(first):
            chat.relation(self.users[second], RelationInput(action="request"))
        with self.user(second):
            incoming = chat.relations()["items"]
            self.assertEqual(incoming[0]["username"], "social_" + str(first))
            chat.relation(self.users[first], RelationInput(action="accept"))

    def conversation(self):
        self.friend()
        with self.user(0):
            return chat.create(ConversationInput(members=[self.users[1]]))["id"]

    def test_friend_permissions_block_and_unauthenticated_api(self):
        self.assertEqual(self.client.get("/api/social/conversations").status_code, 401)
        with self.user(0), self.assertRaises(HTTPException):
            chat.create(ConversationInput(members=[self.users[1]]))
        key = self.conversation()
        with self.user(2), self.assertRaises(HTTPException):
            chat.messages(key)
        with self.user(1):
            chat.relation(self.users[0], RelationInput(action="block"))
        with self.user(0), self.assertRaises(HTTPException):
            chat.send(key, MessageInput(client_msg_id=s.identifier(), text="blocked"))

    def test_retry_concurrent_sequence_read_and_recall(self):
        key = self.conversation()
        payload = MessageInput(client_msg_id=s.identifier(), text="retry")
        def send(_):
            with self.user(0):
                return chat.send(key, payload)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(send, range(4)))
        self.assertEqual({x["seq"] for x in results}, {1})
        def unique(index):
            with self.user(index % 2):
                return chat.send(key, MessageInput(client_msg_id=s.identifier(), text=str(index)))["seq"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(set(executor.map(unique, range(8))), set(range(2, 10)))
        with self.user(1):
            chat.cursor_update(key, 9)
            chat.cursor_update(key, 2)
            self.assertEqual(chat.detail(key)["membership"]["last_read_seq"], 9)
            with self.assertRaises(HTTPException):
                chat.recall(key, 1)
        with self.user(0):
            chat.recall(key, 1)
            self.assertEqual(chat.messages(key)["items"][0]["body"], {})

    def test_group_invitation_history_and_role(self):
        self.friend()
        with self.user(0):
            key = chat.create(ConversationInput(kind="group", title="Friends", members=[self.users[1]]))["id"]
            chat.send(key, MessageInput(client_msg_id=s.identifier(), text="before joining"))
        with self.user(1):
            with self.assertRaises(HTTPException):
                chat.messages(key)
            chat.change_member(key, MemberInput(action="accept"))
            self.assertEqual(chat.messages(key)["items"], [])
            with self.assertRaises(HTTPException):
                chat.change_member(key, MemberInput(action="admin", user_id=self.users[0]))

    def test_share_copy_owner_isolation_and_revoke(self):
        key = self.conversation()
        with self.user(0):
            with library.database() as cursor:
                track = library.save_track(cursor, library.TrackInput(title="Shared", artist="Singer", provider="netease", identifier="1234", webpage_url="https://music.163.com/song?id=1234"))
            shared = shares.create(shares.ShareInput(target_kind="track", source_key=track, conversation_id=key))
            chat.send(key, MessageInput(client_msg_id=s.identifier(), msg_type="share", share_id=shared["id"]))
        with self.user(2), self.assertRaises(HTTPException):
            shares.get(shared["id"])
        with self.user(1):
            copy = shares.copy_playlist(shared["id"])
            self.assertEqual(copy["copied"], 1)
            with s.database() as cursor:
                cursor.execute("SELECT t.id,t.user_id FROM music_tracks t JOIN music_playlist_tracks p ON p.track_id=t.id WHERE p.playlist_id=%s", (copy["playlist_id"],))
                row = cursor.fetchone()
                self.assertEqual(row["user_id"], self.users[1])
                self.assertNotEqual(row["id"], track)
        with self.user(0):
            shares.revoke(shared["id"])
        with self.user(1), self.assertRaises(HTTPException):
            shares.get(shared["id"])

    def test_room_capacity_control_and_playlist_conflicts(self):
        self.friend()
        with self.user(0):
            room = rooms.create(rooms.RoomInput(title="Listen", capacity=2))
            invite = rooms.invite(room["id"])
            playlist = playlists.create(playlists.PlaylistInput(title="Together", members=[self.users[1]]))
        with self.user(1):
            room = rooms.join(room["id"], invite["code"])
            with self.assertRaises(HTTPException):
                rooms.control(room["id"], rooms.ControlInput(revision=room["revision"], action="next"))
            playlist = playlists.edit(playlist["id"], playlists.EditInput(revision=0, track=library.TrackInput(title="Track", provider="netease", identifier="1", webpage_url="https://music.163.com/song?id=1")))
            with self.assertRaises(HTTPException):
                playlists.edit(playlist["id"], playlists.EditInput(revision=0, order=[]))
        with self.user(2), self.assertRaises(HTTPException):
            rooms.join(room["id"], invite["code"])
        with self.user(0):
            rooms.control(room["id"], rooms.ControlInput(revision=room["revision"], action="remove", target_user_id=self.users[1]))
        with self.user(1), self.assertRaises(HTTPException):
            rooms.get(room["id"])

    def audio_asset(self, kind, index):
        key = s.identifier()
        with wave.open(str(media.path_for(key)), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(b"\x01\x00" * 32000)
        with self.user(index), s.database() as cursor:
            media.register(cursor, key, s.uid(), kind, "audio/wav", media.path_for(key).stat().st_size, key * 2, media.probe(media.path_for(key)))
        return key

    def test_real_mix_and_joint_consent_invalidation(self):
        backing = self.audio_asset("backing", 0)
        vocals = [self.audio_asset("vocal", i) for i in (0, 1)]
        with self.user(0):
            song = karaoke.create_song(karaoke.SongInput(title="Duet", backing_asset_id=backing, rights_note="Original test recording"))
            work = karaoke.create(karaoke.PerformanceInput(karaoke_song_id=song["id"]))
            invite = karaoke.action(work["id"], karaoke.PerformanceAction(action="invite", revision=work["revision"]))
        with self.user(1):
            work = karaoke.claim(invite["code"])
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="take", revision=work["revision"], asset_id=vocals[1], record_consent=True, offset_ms=-100))
        with self.user(0):
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="take", revision=work["revision"], asset_id=vocals[0], record_consent=True, offset_ms=100))
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="mix", revision=work["revision"]))
        worker.process_one()
        with self.user(0):
            work = karaoke.get(work["id"])
            self.assertEqual(work["status"], "ready")
            self.assertAlmostEqual(media.probe(media.path_for(work["mix_asset_id"])), 2000, delta=100)
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="confirm", revision=work["revision"], visibility="public"))
            self.assertEqual(work["status"], "ready")
        with self.user(1):
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="confirm", revision=work["revision"], visibility="public"))
            self.assertEqual(work["status"], "published")
        with self.user(0):
            shared = shares.create(shares.ShareInput(target_kind="performance", source_key=work["id"], scope="public"))
        with self.user(2), s.database() as cursor:
            media.require(cursor, work["mix_asset_id"])
            with self.assertRaises(HTTPException):
                media.require(cursor, vocals[0])
        with self.user(0):
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="align", revision=work["revision"], offset_ms=200))
            self.assertEqual(work["status"], "draft")
        with self.user(2), s.database() as cursor, self.assertRaises(HTTPException):
            media.require(cursor, work["mix_asset_id"])
        with self.user(2), self.assertRaises(HTTPException):
            shares.get(shared["id"])
        with self.user(0):
            work = karaoke.action(work["id"], karaoke.PerformanceAction(action="mix", revision=work["revision"]))
        worker.process_one()
        for index in (0, 1):
            with self.user(index):
                work = karaoke.get(work["id"])
                work = karaoke.action(work["id"], karaoke.PerformanceAction(action="confirm", revision=work["revision"], visibility="public"))
        with self.user(0):
            shared = shares.create(shares.ShareInput(target_kind="performance", source_key=work["id"], scope="public"))
        with self.user(1):
            room = rooms.create(rooms.RoomInput(title="Will close"))
            invitation = rooms.invite(room["id"])
        with self.user(0):
            rooms.join(room["id"], invitation["code"])
            with s.database() as cursor:
                s.clean_user(cursor, self.users[1])
            with self.assertRaises(HTTPException):
                chat.messages(room["conversation_id"])
        with self.user(2), self.assertRaises(HTTPException):
            shares.get(shared["id"])

    def test_rule_versions_are_immutable(self):
        with self.user(0):
            first = rules.save(rules.RuleInput(title="Calm", config=rules.Config()))
            old = first["versions"][0]
            rules.action(first["id"], rules.RuleAction(action="publish", version_id=old["id"]))
            second = rules.save(rules.RuleInput(title="Calmer", config=rules.Config(filters=rules.Filters(energy_max=.3))), first["id"])
            self.assertEqual(len(second["versions"]), 2)
            self.assertEqual(next(x for x in second["versions"] if x["id"] == old["id"])["config"], old["config"])
        with self.user(1):
            request = rules.prepare_request({"rule_version_id": old["id"], "preferences": {"target_count": 3}})
            self.assertEqual(request["effective_rule"]["output"]["target_count"], 3)

    def test_websocket_ticket_once_and_session_revocation(self):
        with patch.object(accounts.captcha, "make_code", return_value="A2B3"):
            captcha = self.client.get("/api/auth/captcha").json()
        response = self.client.post("/api/auth/login", json={"username": "social_0", "password": "Test-social-123!", "captcha_id": captcha["captcha_id"], "captcha_code": "A2B3"})
        response.raise_for_status()
        self.client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        ticket = self.client.post("/api/social/ws-ticket").json()["ticket"]
        ticket_data = json.loads(accounts.redis_client().get(s.PREFIX + "ws-ticket:" + s.digest(ticket)))
        realtime.check_session(ticket_data)
        path = "/api/social/ws?ticket=" + ticket
        with self.client.websocket_connect(path, headers={"Origin": "http://127.0.0.1:5174"}) as ws:
            self.assertEqual(ws.receive_json()["topic"], "heartbeat")
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(path, headers={"Origin": "http://127.0.0.1:5174"}):
                    pass
            sid = self.client.cookies.get(accounts.COOKIE).split(".")[0]
            accounts.redis_client().delete(accounts.session_key(sid))
            ws.send_text("{}")
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.client.headers.pop("Authorization", None)
        self.client.cookies.clear()

    def test_room_share_invite_ready_and_disabled_owner(self):
        with self.user(0):
            room = rooms.create(rooms.RoomInput(title="Private"))
            shared = shares.create(shares.ShareInput(target_kind="room", source_key=room["id"], scope="link"))
            room = rooms.control(room["id"], rooms.ControlInput(action="enqueue", revision=room["revision"], track=library.TrackInput(title="A", provider="netease", identifier="1", webpage_url="https://music.163.com/song?id=1")))
            room = rooms.control(room["id"], rooms.ControlInput(action="next", revision=room["revision"]))
            self.assertEqual(room["playback"]["state"], "paused")
            with self.assertRaises(HTTPException):
                rooms.control(room["id"], rooms.ControlInput(action="play", revision=room["revision"]))
            room = rooms.control(room["id"], rooms.ControlInput(action="ready", revision=room["revision"]))
            room = rooms.control(room["id"], rooms.ControlInput(action="play", revision=room["revision"]))
            self.assertGreater(room["playback"]["started_at"], time.time())
        with self.user(1):
            card = shares.get(shared["id"], shared["token"])
            joined = rooms.join(room["id"], card["snapshot"]["code"])
            self.assertEqual(len(joined["members"]), 2)
            with self.assertRaises(HTTPException):
                shares.create(shares.ShareInput(target_kind="room", source_key=room["id"], scope="public"))
            with s.database() as cursor:
                cursor.execute("UPDATE users SET status='disabled' WHERE id=%s", (self.users[0],))
            try:
                with self.assertRaises(HTTPException):
                    rooms.get(room["id"])
                rooms.maintain()
                with s.database() as cursor:
                    cursor.execute("SELECT status FROM social_rooms WHERE id=%s", (room["id"],))
                    self.assertEqual(cursor.fetchone()["status"], "closed")
            finally:
                with s.database() as cursor:
                    cursor.execute("UPDATE users SET status='active' WHERE id=%s", (self.users[0],))

    def test_moderation_requires_admin_and_withdraws_message(self):
        key = self.conversation()
        with self.user(0):
            chat.send(key, MessageInput(client_msg_id=s.identifier(), text="Review me"))
        with self.user(1):
            report = api.report(api.ReportInput(target_kind="message", target_key=key + ":1", reason="Test evidence"))
            with self.assertRaises(HTTPException):
                moderation.resolve(report["id"], moderation.Resolution(action="hide", reason="review"))
        marker = accounts.principal.set(accounts.Principal(self.users[2], "reviewer", "super_admin", ""))
        try:
            self.assertEqual(moderation.reports()["items"][0]["evidence"]["body"]["text"], "Review me")
            moderation.resolve(report["id"], moderation.Resolution(action="hide", reason="Verified test"))
        finally:
            accounts.principal.reset(marker)
        with self.user(0):
            self.assertEqual(chat.messages(key)["items"][0]["status"], "recalled")

    def test_rtc_rechecks_session_and_never_grants_audience_publish(self):
        import jwt
        import base64
        import hashlib
        config = {"url": "ws://rtc.test", "key": "test-key", "secret": "unit-test-secret-" * 4}
        with self.user(0):
            room = rooms.create(rooms.RoomInput(title="K", kind="karaoke"))
        reference = s.identifier()
        data = {"uid": self.users[0], "sid": "test", "auth_version": 1, "room_id": room["id"]}
        accounts.redis_client().setex(s.PREFIX + "rtc-session:" + reference, 300, s.encode(data))
        participant = {"identity": str(self.users[0]), "metadata": s.encode({"grant": reference})}
        with patch.object(rtc, "config", return_value=config), patch.object(rtc, "permission") as permission, patch.object(rtc, "remove") as remove:
            encoded = rtc.token(room["id"], self.users[0])
            self.assertFalse(jwt.decode(encoded, config["secret"], algorithms=["HS256"])["video"]["canPublish"])
            with patch.object(realtime, "check_session"):
                rtc.reconcile_participant(room["id"], participant)
                permission.assert_called_with(room["id"], participant["identity"], False)
                body = s.encode({"event": "participant_joined", "room": {"name": "vd-" + room["id"]}, "participant": participant}).encode()
                signed = jwt.encode({"iss": config["key"], "exp": time.time() + 60, "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()}, config["secret"], algorithm="HS256")
                self.assertTrue(rtc.webhook(body, signed)["ok"])
                with self.assertRaises(HTTPException):
                    rtc.webhook(body + b" ", signed)
            with patch.object(realtime, "check_session", side_effect=HTTPException(401)):
                rtc.reconcile_participant(room["id"], participant)
                remove.assert_called_with(room["id"], participant["identity"])


class WorkerLifecycleTests(unittest.TestCase):
    def test_independent_event_loops_start_and_stop_without_cross_loop_tasks(self):
        import asyncio
        import threading
        entered, release = threading.Event(), threading.Event()
        async def idle():
            await asyncio.Event().wait()
        async def run(first):
            worker.start(); realtime.start()
            if first:
                entered.set()
                await asyncio.to_thread(release.wait, 5)
            await worker.stop(); await realtime.stop()
        with patch.object(worker, "loop", idle), patch.object(worker, "maintenance", idle), patch.object(realtime, "publisher", idle), patch.object(realtime, "subscriber", idle):
            with ThreadPoolExecutor(max_workers=2) as executor:
                future = executor.submit(asyncio.run, run(True))
                self.assertTrue(entered.wait(5))
                try:
                    asyncio.run(run(False))
                finally:
                    release.set()
                future.result(timeout=5)
