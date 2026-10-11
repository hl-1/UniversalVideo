import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend import accounts, administration, ai, library, main


class AiConfigTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.local = Path(directory) / ".env.universal-video.local"
        self.legacy = Path(directory) / "ai.json"
        self.stack.enter_context(patch.object(accounts, "ROOT", Path(directory)))
        self.stack.enter_context(patch.object(accounts, "LOCAL_CONFIG_PATH", self.local))
        self.stack.enter_context(patch.object(main, "AI_CONFIG_PATH", self.legacy))
        self.stack.enter_context(patch.dict(accounts.os.environ, {}, clear=True))
        database = self.stack.enter_context(patch.object(accounts, "database"))
        self.cursor = database.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = {"config": "{}"}
        token = accounts.principal.set(None)
        self.addCleanup(accounts.principal.reset, token)

    def write_local(self):
        self.local.write_text(
            "LLM_BASE_URL=https://local.example/v1\n"
            "LLM_API_KEY='literal-${SECRET}#key'\n"
            "LLM_MODEL=local-model\n",
            encoding="utf-8",
        )

    def test_local_key_is_literal_and_unauthenticated_summary_uses_it(self):
        self.write_local()
        config = main._load_ai_config()
        self.assertEqual(config, {"base_url": "https://local.example/v1", "api_key": "literal-${SECRET}#key", "model": "local-model"})

    def test_process_environment_overrides_local_file(self):
        self.write_local()
        with patch.dict(accounts.os.environ, {"LLM_API_KEY": "process-key", "LLM_MODEL": "process-model"}):
            config = main._load_ai_config()
        self.assertEqual(config["api_key"], "process-key")
        self.assertEqual(config["model"], "process-model")
        self.assertEqual(config["base_url"], "https://local.example/v1")

    def test_speech_uses_dedicated_model_and_existing_connection(self):
        self.write_local()
        with patch.dict(accounts.os.environ, {"LLM_ASR_MODEL": "speech-model"}):
            config = ai.load_config("speech")
        self.assertEqual(config["model"], "speech-model")
        self.assertEqual(config["base_url"], "https://local.example/v1")
        self.assertEqual(config["api_key"], "literal-${SECRET}#key")

    def test_speech_requires_its_own_model(self):
        self.write_local()
        with self.assertRaisesRegex(RuntimeError, "LLM_ASR_MODEL"):
            ai.load_config("speech")

    def test_admin_speech_model_overrides_local_model(self):
        self.write_local()
        self.cursor.fetchone.return_value = {"config": json.dumps({"ai_asr_model": "admin-asr"})}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        with patch.dict(accounts.os.environ, {"LLM_ASR_MODEL": "local-asr"}):
            self.assertEqual(ai.load_config("speech")["model"], "admin-asr")

    def test_authenticated_summary_uses_local_settings_without_changing_feature_flags(self):
        self.write_local()
        self.cursor.fetchone.return_value = {"config": json.dumps({"music_enabled": False})}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        config = main._load_ai_config()
        self.assertEqual(config["model"], "local-model")
        self.assertEqual(config["api_key"], "literal-${SECRET}#key")
        self.assertFalse(accounts.settings()["music_enabled"])

    def test_admin_configuration_takes_precedence_as_a_complete_connection(self):
        self.write_local()
        configured = {"ai_base_url": "https://admin.example/v1", "ai_api_key": "admin-key", "ai_model": "admin-model"}
        self.cursor.fetchone.return_value = {"config": json.dumps(configured)}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        self.assertEqual(main._load_ai_config(), {"base_url": "https://admin.example/v1", "api_key": "admin-key", "model": "admin-model"})

    def test_admin_response_masks_local_api_key(self):
        self.write_local()
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        response = administration.get_settings()
        self.assertTrue(response["ai_api_key_configured"])
        self.assertEqual(response["ai_api_key_masked"], "********")
        self.assertNotIn("ai_api_key", response)
        self.assertNotIn("literal-${SECRET}#key", json.dumps(response))

    def test_legacy_json_configuration_still_works_without_local_key(self):
        legacy = {"base_url": "https://legacy.example/v1", "api_key": "legacy-key", "model": "legacy-model"}
        self.legacy.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(main._load_ai_config(), legacy)

    def test_no_local_key_preserves_unconfigured_state(self):
        self.assertEqual(accounts.settings()["ai_api_key"], "")
        with self.assertRaisesRegex(RuntimeError, "缺少 AI 配置"):
            main._load_ai_config()

    def test_named_file_overrides_legacy_and_process_environment_wins(self):
        legacy = self.local.parent / ".env"
        legacy.write_text("MYSQL_PORT=3307\nREDIS_PORT=6378\n", encoding="utf-8")
        self.local.write_text("MYSQL_PORT=3306\n", encoding="utf-8")
        self.assertEqual(accounts.environment("MYSQL_PORT"), "3306")
        self.assertEqual(accounts.environment("REDIS_PORT"), "6378")
        with patch.dict(accounts.os.environ, {"MYSQL_PORT": "3310"}):
            self.assertEqual(accounts.environment("MYSQL_PORT"), "3310")

    def test_database_reads_named_file_without_database_json(self):
        self.local.write_text(
            "MYSQL_HOST=127.0.0.1\nMYSQL_PORT=3306\nMYSQL_DATABASE=videodream\n"
            "MYSQL_USER=local-user\nMYSQL_PASSWORD='literal-${SECRET}#password'\n",
            encoding="utf-8",
        )
        with patch.object(library, "CONFIG_PATH", self.local.parent / "database.json"):
            config = library.database_config()
        self.assertEqual(config["port"], 3306)
        self.assertEqual(config["database"], "videodream")
        self.assertEqual(config["user"], "local-user")
        self.assertEqual(config["password"], "literal-${SECRET}#password")

    def test_redis_and_jwt_read_named_file(self):
        key = "test-key-" * 8
        self.local.write_text(
            f"VIDEODREAM_JWT_SECRET='{key}'\nREDIS_HOST=127.0.0.1\nREDIS_PORT=6379\n"
            "REDIS_DATABASE=2\nREDIS_PASSWORD='test-redis-password'\n",
            encoding="utf-8",
        )
        self.assertEqual(accounts.signing_key(), key)
        connection = accounts.redis_client().connection_pool.connection_kwargs
        self.assertEqual(connection["host"], "127.0.0.1")
        self.assertEqual(connection["port"], 6379)
        self.assertEqual(connection["db"], 2)
        self.assertEqual(connection["password"], "test-redis-password")


if __name__ == "__main__":
    unittest.main()
