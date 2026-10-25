from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pymysql
from fastapi import HTTPException

from backend.core import paths
from backend.infrastructure import database as db
from backend.infrastructure.json_response import SafeJSONResponse
from backend.migrations import runner, user_snowflake
from backend.modules.accounts import service as accounts, user_ids


class SnowflakeUnitTests(unittest.TestCase):
    def test_layout_and_full_precision_json(self):
        value = user_ids.compose(user_ids.EPOCH_MS + 234567890123, 1022, 4095)
        self.assertEqual(value >> 22, 234567890123)
        self.assertEqual((value >> 12) & 1023, 1022)
        self.assertEqual(value & 4095, 4095)
        response = SafeJSONResponse({"id": value, "members": [value, True], "count": 3})
        self.assertEqual(json.loads(response.body), {"id": str(value), "members": [str(value), True], "count": 3})

    def test_json_ownership_rewrite_preserves_business_numbers(self):
        source = {"user_id": 1, "position_ms": 1, "singers": [1], "track": {"id": 1}, "file": "users/1/files/song.mp3"}
        self.assertEqual(user_snowflake.rewrite(source, {"1": "999999999999999999"}), {
            "user_id": 999999999999999999, "position_ms": 1, "singers": [999999999999999999],
            "track": {"id": 1}, "file": "users/999999999999999999/files/song.mp3"})

    def test_folder_move_and_reverse_are_resumable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "downloads" / "users" / "1"
            source.mkdir(parents=True)
            (source / "fixture.txt").write_text("keep")
            with patch.object(paths, "DOWNLOAD_DIR", root / "downloads"), patch.object(paths, "ROOT_DIR", root):
                plan = user_snowflake.file_plan({"1": "999999999999999999"}, root / "manifest.json")
                user_snowflake.move_folders(plan)
                user_snowflake.move_folders(plan)
                self.assertFalse(source.exists())
                user_snowflake.move_folders(plan, reverse=True)
                self.assertEqual((source / "fixture.txt").read_text(), "keep")


class SnowflakeDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        base = db.database_config()
        name = "videodream_snowflake_test_" + secrets.token_hex(5)
        cls.config = base | {"database": name}
        cls.admin = pymysql.connect(**{key: value for key, value in base.items() if key != "database"})
        cls.addClassCleanup(cls.admin.close)
        with cls.admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
        cls.addClassCleanup(lambda: cls.drop_database(name))
        cls.stack.enter_context(patch.object(db, "database_config", return_value=cls.config))
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        for attribute, value in (("DOWNLOAD_DIR", cls.root / "downloads"), ("ROOT_DIR", cls.root), ("RUN_DIR", cls.root / "run")):
            cls.stack.enter_context(patch.object(paths, attribute, value))
        cls.stack.enter_context(patch.object(user_snowflake, "invalidate_caches"))
        migrations = cls.root / "v5"
        migrations.mkdir()
        for migration in runner.MIGRATIONS.glob("V[1-5]__*.sql"):
            (migrations / migration.name).write_bytes(migration.read_bytes())
        runner.run_flyway(cls.config, "migrate", locations=migrations, capture=True)
        with pymysql.connect(**cls.config) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO users (id,username,password_hash,created_at) VALUES (99,'legacy_friend','!test',1)")
            cursor.execute("INSERT INTO music_profile (id,nickname,user_id) VALUES (99,'friend',99)")
            cursor.execute("INSERT INTO music_theme (id,config,user_id) VALUES (1,'{}',1)")
            cursor.execute("INSERT INTO social_relations (user_id,target_user_id,kind,status,created_at,updated_at) VALUES (1,99,'friend','accepted',1,1)")
            key = "a" * 32
            cursor.execute("INSERT INTO social_conversations (id,kind,direct_key,title,settings,created_at,updated_at) VALUES (%s,'direct',%s,'','{}',1,1)", (key, hashlib.sha256(b"1:99").hexdigest()))
            for uid in (1, 99):
                cursor.execute("INSERT INTO social_conversation_members (conversation_id,user_id,role,status,created_at,updated_at) VALUES (%s,%s,'member','active',1,1)", (key, uid))
            cursor.execute("INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at) VALUES ('file','fixture',1,%s,1,1)", (json.dumps({"user_id": 1, "singers": [1, 99], "position_ms": 1}),))
            connection.commit()
        source = cls.root / "downloads" / "users" / "1"
        source.mkdir(parents=True)
        (source / "fixture.txt").write_text("original")
        runner.run_flyway(cls.config, "migrate", locations=runner.MIGRATIONS, capture=True)
        user_snowflake.migrate(cls.config, move_files=True)
        db.schema_ready = False
        with accounts.database() as cursor:
            cursor.execute("SELECT old_id,new_id FROM user_id_migration_map")
            cls.mapping = {row["old_id"]: row["new_id"] for row in cursor.fetchall()}

    @classmethod
    def drop_database(cls, name):
        with cls.admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{name}`")

    def test_existing_user_references_documents_and_files_migrate(self):
        owner, friend = self.mapping[1], self.mapping[99]
        self.assertGreater(owner, 2 ** 53)
        with accounts.database() as cursor:
            cursor.execute("SELECT user_id,target_user_id FROM social_relations")
            self.assertEqual(cursor.fetchone(), {"user_id": owner, "target_user_id": friend})
            cursor.execute("SELECT id,user_id FROM music_profile WHERE id=%s", (owner,))
            self.assertEqual(cursor.fetchone(), {"id": owner, "user_id": owner})
            cursor.execute("SELECT id,user_id FROM music_theme")
            self.assertEqual(cursor.fetchone(), {"id": owner, "user_id": owner})
            cursor.execute("SELECT payload FROM resources WHERE resource_key='fixture'")
            self.assertEqual(json.loads(cursor.fetchone()["payload"]), {"user_id": owner, "singers": [owner, friend], "position_ms": 1})
            cursor.execute("SELECT direct_key FROM social_conversations")
            self.assertEqual(cursor.fetchone()["direct_key"], hashlib.sha256(f"{owner}:{friend}".encode()).hexdigest())
        self.assertEqual((self.root / "downloads" / "users" / str(owner) / "fixture.txt").read_text(), "original")
        user_snowflake.migrate(self.config, move_files=True)
        self.assertEqual(accounts.public_user(accounts.get_user(owner))["legacy_id"], "1")

    def test_concurrent_creation_and_generator_restart_do_not_duplicate(self):
        def create(index):
            return accounts.create_user(accounts.Credentials(username=f"snowflake_{index}", password="Test-user-123!"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            generated = list(pool.map(create, range(20)))
        self.assertEqual(len(generated), len(set(generated)))
        self.assertTrue(all(value > 2 ** 53 and (value >> 12) & 1023 == 0 for value in generated))
        with accounts.database() as cursor:
            later = user_ids.next_id(cursor)
        self.assertGreater(later, max(generated))

    def test_clock_rollback_fails_without_reusing_sequence(self):
        with accounts.database() as cursor:
            cursor.execute("INSERT INTO user_id_generator_state VALUES (1010,%s,4095)", (9999999999999,))
        with self.assertRaises(HTTPException) as failure:
            with accounts.database() as cursor:
                user_ids.next_id(cursor, worker=1010)
        self.assertIn("时钟回退", failure.exception.detail)
        with accounts.database() as cursor:
            cursor.execute("SELECT sequence FROM user_id_generator_state WHERE worker_id=1010")
            self.assertEqual(cursor.fetchone()["sequence"], 4095)
