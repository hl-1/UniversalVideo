import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from backend.infrastructure import database as database_store
from backend.core import paths
from backend.modules.video import summary as video_summary
from backend.modules.accounts import service as accounts
from backend.modules.admin import api as administration
from backend.infrastructure import ai
from backend.modules.music import library


class AiConfigTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.local = Path(directory) / ".env.universal-video.local"
        self.legacy = Path(directory) / "ai.json"
        self.stack.enter_context(patch.object(accounts, "ROOT", Path(directory)))
        self.stack.enter_context(patch.object(accounts, "LOCAL_CONFIG_PATH", self.local))
        from backend.core import configuration
        self.stack.enter_context(patch.object(configuration, "ROOT_DIR", Path(directory)))
        self.stack.enter_context(patch.object(configuration, "LOCAL_CONFIG_PATH", self.local))
        self.stack.enter_context(patch.object(paths, 'AI_CONFIG_PATH', self.legacy))
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
        config = video_summary._load_ai_config()
        self.assertEqual(config, {"base_url": "https://local.example/v1", "api_key": "literal-${SECRET}#key", "model": "local-model"})

    def test_process_environment_overrides_local_file(self):
        self.write_local()
        with patch.dict(accounts.os.environ, {"LLM_API_KEY": "process-key", "LLM_MODEL": "process-model"}):
            config = video_summary._load_ai_config()
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

    def test_local_speech_model_overrides_admin_model(self):
        self.write_local()
        self.cursor.fetchone.return_value = {"config": json.dumps({"ai_asr_model": "admin-asr"})}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        with patch.dict(accounts.os.environ, {"LLM_ASR_MODEL": "local-asr"}):
            self.assertEqual(ai.load_config("speech")["model"], "local-asr")

    def test_authenticated_summary_uses_local_settings_without_changing_feature_flags(self):
        self.write_local()
        self.cursor.fetchone.return_value = {"config": json.dumps({"music_enabled": False})}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        config = video_summary._load_ai_config()
        self.assertEqual(config["model"], "local-model")
        self.assertEqual(config["api_key"], "literal-${SECRET}#key")
        self.assertFalse(accounts.settings()["music_enabled"])

    def test_local_configuration_overrides_saved_admin_connection(self):
        self.write_local()
        configured = {"ai_base_url": "https://admin.example/v1", "ai_api_key": "admin-key", "ai_model": "admin-model"}
        self.cursor.fetchone.return_value = {"config": json.dumps(configured)}
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        self.assertEqual(video_summary._load_ai_config(), {"base_url": "https://local.example/v1", "api_key": "literal-${SECRET}#key", "model": "local-model"})

    def write_independent_local(self):
        self.local.write_text("\n".join(
            f"{variable}_{field.upper()}={modality}-{field}" if field != "base_url"
            else f"{variable}_BASE_URL=https://{modality}.example/v1"
            for modality, (_, variable, _) in accounts.AI_GROUPS.items()
            for field in ("base_url", "model", "api_key")), encoding="utf-8")

    def test_each_modality_uses_its_own_environment_connection(self):
        self.write_independent_local()
        for authenticated in (False, True):
            accounts.principal.set(accounts.Principal(1, "admin", "super_admin") if authenticated else None)
            for modality in accounts.AI_GROUPS:
                with self.subTest(authenticated=authenticated, modality=modality):
                    self.assertEqual(ai.load_config(modality), {"base_url": f"https://{modality}.example/v1",
                                     "model": f"{modality}-model", "api_key": f"{modality}-api_key"})

    def test_vision_and_speech_work_without_a_text_api_key(self):
        self.write_independent_local()
        self.local.write_text(self.local.read_text().replace("LLM_API_KEY=text-api_key", "LLM_API_KEY="))
        self.assertEqual(ai.load_config("vision")["api_key"], "vision-api_key")
        self.assertEqual(ai.load_config("speech")["api_key"], "speech-api_key")
        with self.assertRaisesRegex(RuntimeError, "LLM_API_KEY"):
            ai.load_config("text")

    def test_dedicated_address_without_key_never_borrows_text_or_legacy_key(self):
        self.write_local()
        self.legacy.write_text(json.dumps({"api_key": "stale-key", "model": "stale-model"}))
        with patch.dict(accounts.os.environ, {"LLM_ASR_BASE_URL": "https://speech.example/v1", "LLM_ASR_MODEL": "asr"}):
            for authenticated in (False, True):
                accounts.principal.set(accounts.Principal(1, "admin", "super_admin") if authenticated else None)
                with self.subTest(authenticated=authenticated), self.assertRaisesRegex(RuntimeError, "LLM_ASR_API_KEY"):
                    ai.load_config("speech")

    def test_process_environment_overrides_dedicated_file_settings(self):
        self.write_independent_local()
        with patch.dict(accounts.os.environ, {"LLM_VISION_API_KEY": "process-vision", "LLM_ASR_MODEL": "process-asr"}):
            self.assertEqual(ai.load_config("vision")["api_key"], "process-vision")
            self.assertEqual(ai.load_config("speech")["model"], "process-asr")
            self.assertEqual(ai.load_config("text")["model"], "text-model")

    def test_environment_file_edit_is_used_by_new_config_reads(self):
        self.write_independent_local()
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        self.assertEqual(ai.load_config("text")["model"], "text-model")
        self.local.write_text(self.local.read_text().replace("LLM_MODEL=text-model", "LLM_MODEL=updated-model"))
        self.assertEqual(ai.load_config("text")["model"], "updated-model")
        self.assertEqual(ai.load_config("speech")["model"], "speech-model")

    def test_admin_masks_all_three_environment_keys(self):
        self.write_independent_local()
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        response = administration.get_settings()
        for modality, (prefix, _, _) in accounts.AI_GROUPS.items():
            self.assertNotIn(f"{prefix}_api_key", response)
            self.assertTrue(response[f"{prefix}_api_key_configured"])
            self.assertNotIn(f"{modality}-api_key", json.dumps(response))
            self.assertEqual(response["ai_sources"][modality], "environment")

    def test_admin_rejects_changes_to_environment_managed_group(self):
        self.write_independent_local()
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        payload = administration.SettingsUpdate(**(administration.get_settings() | {"ai_asr_model": "ignored-edit"}))
        with self.assertRaises(HTTPException) as caught:
            administration.update_settings(payload)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(any(call.args[0].startswith("UPDATE") for call in self.cursor.execute.call_args_list))

    def test_saving_business_settings_does_not_copy_environment_secrets_to_database(self):
        self.write_independent_local()
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        saved = {"ai_api_key": "saved-key", "ai_model": "saved-model", "ai_base_url": "https://saved.example/v1"}
        self.cursor.fetchone.return_value = {"config": json.dumps(saved)}
        payload = administration.SettingsUpdate(**(administration.get_settings() | {"registration_enabled": True}))
        with patch.object(accounts, "audit"):
            administration.update_settings(payload)
        stored = self.updated_config()
        self.assertTrue(stored["registration_enabled"])
        self.assertEqual(stored["ai_api_key"], "saved-key")
        self.assertEqual(stored["ai_asr_api_key"], "saved-key")
        self.assertNotIn("speech-api_key", json.dumps(stored))
        self.assertNotIn("ai_sources", stored)

    def updated_config(self):
        writes = [call for call in self.cursor.execute.call_args_list if call.args[0].startswith("UPDATE system_settings")]
        return json.loads(writes[-1].args[1][0])

    def test_first_admin_save_copies_legacy_connections_before_text_changes(self):
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        saved = {"ai_base_url": "https://old.example/v1", "ai_api_key": "old-key", "ai_model": "old-text",
                 "ai_asr_model": "asr", "ai_vision_model": "vision"}
        self.cursor.fetchone.return_value = {"config": json.dumps(saved)}
        payload = administration.SettingsUpdate(**(administration.get_settings() | {
            "ai_base_url": "https://new.example/v1", "ai_model": "new-text", "ai_api_key": "new-key"}))
        with patch.object(accounts, "audit"), patch("backend.infrastructure.network.public_url"):
            administration.update_settings(payload)
        stored = self.updated_config()
        self.cursor.fetchone.return_value = {"config": json.dumps(stored)}
        self.assertEqual(ai.load_config("text")["api_key"], "new-key")
        for modality in ("vision", "speech"):
            self.assertEqual(ai.load_config(modality)["api_key"], "old-key")
            self.assertEqual(ai.load_config(modality)["base_url"], "https://old.example/v1")

    def test_key_omission_preserves_other_groups_and_explicit_empty_clears_only_one(self):
        accounts.principal.set(accounts.Principal(1, "admin", "super_admin"))
        saved = accounts.resolve_ai_settings({"ai_api_key": "old-key", "ai_asr_model": "asr"}, use_environment=False)
        self.cursor.fetchone.return_value = {"config": json.dumps(saved)}
        payload = administration.SettingsUpdate(**(administration.get_settings() | {"ai_asr_api_key": ""}))
        with patch.object(accounts, "audit"), patch("backend.infrastructure.network.public_url"):
            administration.update_settings(payload)
        stored = self.updated_config()
        self.assertEqual(stored["ai_asr_api_key"], "")
        self.assertEqual(stored["ai_api_key"], "old-key")
        self.assertEqual(stored["ai_vision_api_key"], "old-key")

    def test_legacy_json_supports_independent_connections(self):
        legacy = {"base_url": "https://text.example/v1", "api_key": "text-key", "model": "text-model",
                  "vision_base_url": "https://vision.example/v1", "vision_api_key": "vision-key", "vision_model": "vision-model",
                  "asr_base_url": "https://speech.example/v1", "asr_api_key": "speech-key", "asr_model": "speech-model"}
        self.legacy.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(ai.load_config("vision", legacy_path=self.legacy)["api_key"], "vision-key")
        self.assertEqual(ai.load_config("speech", legacy_path=self.legacy)["base_url"], "https://speech.example/v1")

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
        self.assertEqual(video_summary._load_ai_config(), legacy)

    def test_no_local_key_preserves_unconfigured_state(self):
        self.assertEqual(accounts.settings()["ai_api_key"], "")
        with self.assertRaisesRegex(RuntimeError, "缺少 AI 配置"):
            video_summary._load_ai_config()

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
        with patch.object(database_store, "CONFIG_PATH", self.local.parent / "database.json"):
            config = database_store.database_config()
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
