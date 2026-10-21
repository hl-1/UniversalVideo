import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.core import paths, storage
from backend.infrastructure import storage_migration as migration
from backend.modules.accounts import service as accounts


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "downloads"
        self.root.mkdir()
        for mocked in (patch.object(paths, "DOWNLOAD_DIR", self.root),
                       patch.object(paths, "RUN_DIR", Path(self.folder.name) / "run")):
            mocked.start()
            self.addCleanup(mocked.stop)


class UserStorageTests(StorageTestCase):
    def test_same_name_has_independent_content_and_admin_reads_original_owner(self):
        first = storage.path("file", "same.mp3", uid=2)
        second = storage.path("file", "same.mp3", uid=3)
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        with patch.object(accounts, "resource", return_value={"user_id": 2}):
            self.assertEqual(storage.resource_path("file", "same.mp3").read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")

    def test_rejects_path_traversal_and_symlinks(self):
        for name in ("../secret", "..\\secret", "file:stream", ".", "..", "\x00"):
            with self.subTest(name=name), self.assertRaises(HTTPException):
                storage.path("file", name, uid=2)
        inside = storage.path("file", "inside.mp3", uid=2)
        inside.symlink_to(Path(self.folder.name) / "secret")
        with self.assertRaises(HTTPException):
            storage.owned_path("file", inside.name, uid=2)

    def test_legacy_root_is_not_readable_without_matching_migration_entry(self):
        (self.root / "old.mp3").write_bytes(b"legacy")
        self.assertFalse(storage.owned_path("file", "old.mp3", uid=2).exists())
        data = migration.plan(self.root, [{"kind": "file", "resource_key": "old.mp3", "user_id": 2}], [])
        data["state"] = "applying"
        migration.save(paths.RUN_DIR / "storage-migrations" / "active.json", data)
        self.assertEqual(storage.owned_path("file", "old.mp3", uid=2).read_bytes(), b"legacy")
        self.assertFalse(storage.owned_path("file", "old.mp3", uid=3).exists())
        (self.root / "old.mp3").write_bytes(b"changed")
        self.assertFalse(storage.owned_path("file", "old.mp3", uid=2).exists())


class StorageMigrationTests(StorageTestCase):
    def prepare(self):
        (self.root / "first.mp3").write_bytes(b"first")
        (self.root / "second.mp3").write_bytes(b"second")
        (self.root / "unknown.mp3").write_bytes(b"unknown")
        rows = [{"kind": "file", "resource_key": name, "user_id": uid}
                for name, uid in (("first.mp3", 2), ("second.mp3", 3))]
        return migration.plan(self.root, rows, []), Path(self.folder.name) / "manifest.json"

    def test_move_is_idempotent_preserves_hashes_and_leaves_unowned_files(self):
        data, manifest = self.prepare()
        self.assertEqual(data["unmapped"], ["unknown.mp3"])
        migration.move_files(data, manifest)
        migration.move_files(json.loads(manifest.read_text()), manifest)
        self.assertEqual(storage.owned_path("file", "first.mp3", uid=2).read_bytes(), b"first")
        self.assertEqual(storage.owned_path("file", "second.mp3", uid=3).read_bytes(), b"second")
        self.assertTrue((self.root / "unknown.mp3").is_file())
        self.assertFalse((self.root / "first.mp3").exists())

    def test_partial_link_interruption_can_resume_without_replacing_content(self):
        import os
        data, manifest = self.prepare()
        target = storage.path("file", "first.mp3", uid=2)
        os.link(self.root / "first.mp3", target)
        migration.move_files(data, manifest)
        self.assertEqual(target.read_bytes(), b"first")
        self.assertFalse((self.root / "first.mp3").exists())

    def test_conflicting_target_or_changed_source_blocks_all_moves(self):
        for scenario in ("target", "source"):
            with self.subTest(scenario=scenario):
                data, manifest = self.prepare()
                if scenario == "target":
                    target = storage.path("file", "second.mp3", uid=3)
                    target.write_bytes(b"different")
                else:
                    (self.root / "second.mp3").write_bytes(b"modified")
                with self.assertRaises(ValueError):
                    migration.move_files(data, manifest)
                self.assertTrue((self.root / "first.mp3").exists())
                storage.path("file", "second.mp3", uid=3).unlink(missing_ok=True)

    def test_ownership_conflict_blocks_migration(self):
        data = migration.plan(self.root, [{"kind": "file", "resource_key": "song.mp3", "user_id": 2}],
                              [{"filename": "song.mp3", "user_id": 3}])
        self.assertTrue(data["conflicts"])
        with self.assertRaises(ValueError):
            migration.move_files(data, Path(self.folder.name) / "manifest.json")

    def test_rollback_includes_new_files_and_refuses_global_name_collision(self):
        data, manifest = self.prepare()
        migration.move_files(data, manifest)
        storage.path("file", "new.mp3", uid=3).write_bytes(b"new after migration")
        reverse = migration.rollback_plan(self.root)
        migration.move_files(reverse, manifest.with_name("rollback.json"))
        self.assertEqual((self.root / "new.mp3").read_bytes(), b"new after migration")
        storage.path("file", "collision.mp3", uid=2).write_bytes(b"a")
        storage.path("file", "collision.mp3", uid=3).write_bytes(b"b")
        with self.assertRaises(ValueError):
            migration.rollback_plan(self.root)

    def test_migration_rejects_symbolic_links(self):
        (self.root / "escape.mp3").symlink_to(Path(self.folder.name) / "outside")
        data = migration.plan(self.root, [{"kind": "file", "resource_key": "escape.mp3", "user_id": 2}], [])
        self.assertTrue(data["conflicts"])
