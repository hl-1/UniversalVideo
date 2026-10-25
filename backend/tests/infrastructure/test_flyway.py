"""Exercise real Flyway migrations against isolated, disposable MySQL databases."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import pymysql

from backend.infrastructure.database import database_config
from backend.migrations import SCHEMA_VERSION, initialize_schema
from backend.migrations import runner


class FlywayIntegrationTests(unittest.TestCase):
    @contextmanager
    def isolated_database(self):
        config = database_config()
        name = "videodream_flyway_test_" + secrets.token_hex(6)
        with pymysql.connect(**{k: v for k, v in config.items() if k != "database"}) as administrator:
            with administrator.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            try:
                yield config | {"database": name}
            finally:
                with administrator.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")

    def flyway(self, config, action="migrate", **kwargs):
        return runner.run_flyway(config, action, capture=True, **kwargs)

    def execute_file(self, cursor, name):
        source = (runner.PROD_SQL / name).read_text(encoding="utf-8")
        # These version-one snapshots have no procedures or semicolons in strings.
        source = re.sub(r"^--[^\n]*", "", source, flags=re.M)
        for statement in source.split(";"):
            if statement.strip():
                cursor.execute(statement)

    def assert_current_schema(self, cursor):
        schema = runner.schema_snapshot(cursor)
        discovery = {name: value for name, value in schema.items() if name.startswith('music_discovery_')}
        self.assertEqual(set(discovery), {'music_discovery_'+name for name in ('profiles','identities','batches','items','events','feedback',
            'candidates','feeds','feed_items','daily_usage','profile_runs','profile_versions')})
        social = {name: value for name, value in schema.items() if name.startswith('social_')}
        self.assertEqual(len(social), 27)
        self.assertEqual(social['social_messages']['indexes']['social_message_client']['columns'], [['conversation_id', None], ['client_msg_id', None]])
        self.assertTrue(any(f['table'] == 'social_rule_versions' for f in social['social_rules']['foreign_keys']))
        expected = json.loads(runner.CONTRACT.read_text(encoding='utf-8'))
        expected['users']['columns']['id']['extra'] = ''
        for name in ('music_profile','music_theme'):
            expected[name]['columns']['id']['type'] = 'bigint'
        for name in ('music_profile','music_theme','music_tracks','music_playlists','music_favorites','music_history','music_downloads','music_lyrics'):
            expected[name]['columns']['user_id']['default'] = None
        self.assertEqual(set(name for name in schema if name.startswith('user_id_')), {'user_id_generator_state','user_id_migration_map'})
        self.assertFalse(runner.schema_differences({name:value for name,value in schema.items() if name in expected}, expected))
        self.assertEqual(discovery['music_discovery_events']['indexes']['discovery_event_sequence']['columns'],
                         [['user_id',None],['playback_id',None],['seq',None]])
        self.assertTrue(any(f['table']=='music_discovery_items' and f['delete']=='SET NULL'
                            for f in discovery['music_discovery_events']['foreign_keys']))

    def test_benchmark_logs_are_idempotent_and_preserve_business_errors(self):
        from backend.modules.music.benchmark_logs import save_snapshot
        document = {'scope': 'test', 'runs': [{'label': 'round1', 'concurrency': 10, 'elapsed_s': 1.5,
            'requests': [{'provider': 'netease', 'query': 'a'*16, 'queue_s': .1, 'network_s': .2,
                          'service_s': .3, 'network_calls': 1, 'business_error': '405'}]}]}
        with self.isolated_database() as config:
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                self.assertEqual(save_snapshot(cursor, document, 'b'*32), 2)
                self.assertEqual(save_snapshot(cursor, document, 'b'*32), 0)
                cursor.execute("SELECT * FROM music_search_benchmark_logs WHERE record_type='request'")
                row = cursor.fetchone()
                self.assertEqual((row['status'], row['network_ms'], row['queue_ms']), ('failed', 200, 100))
                self.assertEqual(row['query_hash'], 'a'*16)
                self.assertEqual(json.loads(row['payload'])['business_error'], '405')

    def test_empty_database_migration_is_repeatable_and_connection_is_read_only(self):
        with self.isolated_database() as config:
            self.flyway(config)
            self.flyway(config)
            self.flyway(config, "validate")
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                self.assert_current_schema(cursor)
                initialize_schema(cursor)
                cursor.execute("SELECT version, success FROM flyway_schema_history WHERE version IS NOT NULL")
                self.assertEqual(cursor.fetchall(), [{"version":str(i),'success':1} for i in range(1,int(SCHEMA_VERSION)+1)])
                cursor.execute("SELECT password_hash FROM users WHERE username='admin'")
                self.assertEqual(cursor.fetchone()["password_hash"], "!uninitialized")
                statements = []
                execute = cursor.execute
                with patch.object(cursor, "execute", side_effect=lambda sql, *args: (statements.append(sql), execute(sql, *args))[1]):
                    initialize_schema(cursor)
                self.assertTrue(all(sql.startswith("SELECT ") for sql in statements))

    def test_runtime_gate_rejects_unmanaged_and_failed_schema(self):
        with self.isolated_database() as config:
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                with self.assertRaises(HTTPException):
                    initialize_schema(cursor)
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE flyway_schema_history SET success=FALSE WHERE version='1'")
                with self.assertRaises(HTTPException):
                    initialize_schema(cursor)

    def test_v4_generated_column_survives_backup_restore(self):
        with self.isolated_database() as config, tempfile.TemporaryDirectory() as work:
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT id FROM users WHERE username='admin'")
                owner = cursor.fetchone()['id']
                cursor.execute('''INSERT INTO music_discovery_profile_runs
                    (run_id,user_id,trigger_type,source_revision,base_control_revision,
                     base_agent_revision,input_hash,input_snapshot,status,context,steps,policy_version,
                     skill_version,created_at,updated_at)
                    VALUES (%s,%s,'manual',0,0,0,%s,'{}','pending','{}','[]','test','test',1,1)''',
                    ('b'*32,owner,'c'*64))
                connection.commit()
                with patch.object(runner,'BACKUP_DIR',Path(work)):
                    backup=runner.backup_database(cursor)
            with self.isolated_database() as restored:
                with pymysql.connect(**restored,client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS) as connection, connection.cursor() as cursor:
                    cursor.execute(backup.read_text(encoding='utf-8'))
                    while cursor.nextset():
                        pass
                    connection.commit()
                    initialize_schema(cursor)
                    cursor.execute('SELECT active_user_id FROM music_discovery_profile_runs WHERE run_id=%s',('b'*32,))
                    self.assertEqual(cursor.fetchone()['active_user_id'],owner)

    def test_application_account_needs_no_ddl_privileges(self):
        with self.isolated_database() as config:
            self.flyway(config)
            username = "flyway_test_" + secrets.token_hex(5)
            password = secrets.token_urlsafe(24)
            with pymysql.connect(**config) as administrator, administrator.cursor() as cursor:
                cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (username, password))
                try:
                    cursor.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON `" + config["database"] + "`.* TO %s@'%%'", (username,))
                    with pymysql.connect(**(config | {"user": username, "password": password})) as connection, connection.cursor() as app:
                        initialize_schema(app)
                        app.execute("UPDATE music_profile SET nickname='DML works' WHERE id=(SELECT id FROM users WHERE username='admin')")
                        with self.assertRaises(pymysql.MySQLError):
                            app.execute("CREATE TABLE ddl_must_be_rejected (id INT)")
                finally:
                    cursor.execute("DROP USER %s@'%%'", (username,))

    def test_existing_database_baseline_backs_up_and_keeps_user_data(self):
        with self.isolated_database() as config, tempfile.TemporaryDirectory() as work:
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                for name in ("001_ddl_schema.sql", "002_dml_seed_data.sql", "003_dml_seed_settings.sql"):
                    self.execute_file(cursor, name)
                cursor.execute("UPDATE music_profile SET nickname='保留资料' WHERE id=1")
                payload = json.dumps({"text": "引号';反斜线\\换行\n保留"}, ensure_ascii=False)
                cursor.execute("INSERT INTO resources(kind,resource_key,user_id,payload,created_at,updated_at) "
                               "VALUES('backup_test','fixture',1,%s,1,1)", (payload,))
                connection.commit()
            # A nonempty unmanaged database must not be silently baselined by migrate.
            with self.assertRaises(RuntimeError):
                self.flyway(config)
            with patch.object(runner, "BACKUP_DIR", Path(work)):
                backup = runner.baseline(config)
            self.assertTrue(backup.is_file())
            self.assertIn("保留资料", backup.read_text(encoding="utf-8"))
            if os.name != 'nt':
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            with self.isolated_database() as restored:
                with pymysql.connect(**restored, client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS) as connection, connection.cursor() as cursor:
                    cursor.execute(backup.read_text(encoding="utf-8"))
                    while cursor.nextset():
                        pass
                    connection.commit()
                    runner.preflight(cursor)
                    cursor.execute("SELECT payload FROM resources WHERE kind='backup_test'")
                    self.assertEqual(cursor.fetchone()["payload"], payload)
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                initialize_schema(cursor)
                cursor.execute("SELECT nickname FROM music_profile WHERE id=(SELECT id FROM users WHERE username='admin')")
                self.assertEqual(cursor.fetchone()["nickname"], "保留资料")
                cursor.execute("SELECT type FROM flyway_schema_history WHERE version='1'")
                self.assertEqual(cursor.fetchone()["type"], "BASELINE")

    def test_preflight_rejects_missing_index_and_changed_default(self):
        with self.isolated_database() as config:
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                cursor.execute("DROP INDEX file_hash_index ON music_downloads")
                cursor.execute("ALTER TABLE music_tracks ALTER COLUMN cover_updated_at SET DEFAULT 7")
                with self.assertRaisesRegex(RuntimeError, "music_downloads: index definitions mismatch"):
                    runner.preflight(cursor)
                differences = runner.schema_differences(runner.schema_snapshot(cursor))
                self.assertTrue(any("music_tracks.cover_updated_at" in item for item in differences))

    def test_editing_applied_migration_fails_validation(self):
        with self.isolated_database() as config, tempfile.TemporaryDirectory() as work:
            migration = Path(work) / "V1__baseline.sql"
            original = (runner.MIGRATIONS / migration.name).read_text(encoding="utf-8")
            migration.write_text(original, encoding="utf-8")
            self.flyway(config, locations=Path(work))
            migration.write_text(original.replace("cover_updated_at DOUBLE NOT NULL DEFAULT 0", "cover_updated_at DOUBLE NOT NULL DEFAULT 9"), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.flyway(config, "validate", locations=Path(work))

    def test_failure_after_ddl_blocks_the_runtime_gate(self):
        with self.isolated_database() as config, tempfile.TemporaryDirectory() as work:
            self.flyway(config)
            (Path(work) / "V1__baseline.sql").write_bytes((runner.MIGRATIONS / "V1__baseline.sql").read_bytes())
            (Path(work) / "V2__daily_discovery.sql").write_bytes((runner.MIGRATIONS / "V2__daily_discovery.sql").read_bytes())
            (Path(work) / "V3__music_search_benchmark_logs.sql").write_bytes((runner.MIGRATIONS / "V3__music_search_benchmark_logs.sql").read_bytes())
            (Path(work) / "V4__discovery_pool_and_profile.sql").write_bytes((runner.MIGRATIONS / "V4__discovery_pool_and_profile.sql").read_bytes())
            for migration in runner.MIGRATIONS.glob('V*.sql'):
                (Path(work) / migration.name).write_bytes(migration.read_bytes())
            (Path(work) / f"V{int(SCHEMA_VERSION) + 1}__deliberate_failure.sql").write_text(
                "CREATE TABLE flyway_failure_probe (id INT PRIMARY KEY);\nINVALID SQL;\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.flyway(config, locations=Path(work))
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                with self.assertRaises(HTTPException):
                    initialize_schema(cursor)

    def test_concurrent_migrators_apply_the_baseline_once(self):
        with self.isolated_database() as config:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: self.flyway(config), range(2)))
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS n FROM flyway_schema_history WHERE version='1'")
                self.assertEqual(cursor.fetchone()["n"], 1)
                self.assert_current_schema(cursor)

    def test_manual_forward_scripts_have_matching_reverse_scripts(self):
        with self.isolated_database() as config:
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                for name in ("001_ddl_schema.sql", "002_dml_seed_data.sql", "003_dml_seed_settings.sql"):
                    self.execute_file(cursor, name)
                runner.preflight(cursor)
                for name in ("997_fallback_seed_settings.sql", "998_fallback_seed_data.sql"):
                    self.execute_file(cursor, name)
                for table in ("users", "music_profile", "system_settings"):
                    cursor.execute("SELECT COUNT(*) AS n FROM " + table)
                    self.assertEqual(cursor.fetchone()["n"], 0)
                self.execute_file(cursor, "999_fallback_drop_all.sql")
                cursor.execute("SHOW TABLES")
                self.assertEqual(cursor.fetchall(), ())

    def test_seed_rollback_preserves_initialized_accounts_and_modified_settings(self):
        with self.isolated_database() as config:
            self.flyway(config)
            with pymysql.connect(**config) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE users SET password_hash='initialized-hash' WHERE username='admin'")
                cursor.execute("UPDATE system_settings SET config=%s WHERE id=1", (json.dumps({"registration_enabled": True}),))
                self.execute_file(cursor, "997_fallback_seed_settings.sql")
                self.execute_file(cursor, "998_fallback_seed_data.sql")
                for table in ("users", "music_profile", "system_settings"):
                    cursor.execute("SELECT COUNT(*) AS n FROM " + table)
                    self.assertEqual(cursor.fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
