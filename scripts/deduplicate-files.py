"""Review, apply or roll back retained-file deduplication during a write freeze."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core import blobs, paths, storage
from backend.infrastructure.database import database


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Requires stopped media writers and an independent backup directory")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.apply and args.rollback:
        parser.error("Choose apply or rollback")
    if args.rollback:
        manifest = json.loads(args.manifest.read_text())
        if manifest.get("root") != str(paths.DOWNLOAD_DIR.resolve()) or not manifest.get("applied"):
            raise ValueError("Rollback requires an applied manifest for this downloads directory")
        for entry in manifest["files"]:
            source, backup = storage.contained(paths.DOWNLOAD_DIR / entry["relative"]), Path(entry["backup"])
            if not backup.is_file() or digest(backup) != entry["sha256"]:
                raise RuntimeError("Backup verification failed")
            if source.exists() and digest(source) != entry["sha256"]:
                raise RuntimeError("File changed after migration; preserve it and resolve the conflict before rollback")
            temporary = source.with_name("." + source.name + ".restore-" + secrets.token_hex(4))
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, temporary)
            os.replace(temporary, source)
        print("Independent user files restored; deploy the old writer only after this completes.")
        return
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    if args.apply and args.backup.exists() and (not args.backup.is_dir() or any(args.backup.iterdir())):
        parser.error("--backup must be a new or empty directory")
    if args.manifest.exists():
        parser.error("Use a new manifest path; existing migration evidence is never overwritten")
    with database() as cursor:
        cursor.execute("SELECT resource_key,user_id FROM resources WHERE kind='file' AND deleted=FALSE")
        rows = cursor.fetchall()
    manifest = {"root": str(paths.DOWNLOAD_DIR.resolve()), "files": [], "applied": args.apply}
    for row in rows:
        source = storage.owned_path("file", row["resource_key"], uid=row["user_id"])
        if not source.is_file():
            continue
        entry = {"user_id": row["user_id"], "relative": str(source.relative_to(paths.DOWNLOAD_DIR)), "size": source.stat().st_size, "sha256": digest(source)}
        if args.apply:
            backup = args.backup.resolve() / entry["relative"]
            if backup.is_relative_to(paths.DOWNLOAD_DIR.resolve()):
                raise ValueError("Backups must be outside the downloads tree")
            if backup.exists():
                raise ValueError("Use an empty backup destination; existing backups are never overwritten")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            if digest(backup) != entry["sha256"]:
                raise RuntimeError("Backup verification failed")
            entry["backup"] = str(backup)
        manifest["files"].append(entry)
    # Record all independently copied backups before replacing any user alias.
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.apply:
        for entry in manifest["files"]:
            source = storage.contained(paths.DOWNLOAD_DIR / entry["relative"])
            if digest(source) != entry["sha256"]:
                raise RuntimeError("Media writer changed a file; pause and resolve before continuing")
            blobs.publish(source)
        print("Migration applied and verified; keep the manifest and independent backups.")
    else:
        print(f"Read-only inventory: {len(manifest['files'])} files. No media files changed.")


if __name__ == "__main__":
    main()
