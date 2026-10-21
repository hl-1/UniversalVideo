"""Offline, resumable relocation of existing user files; no schema changes."""
from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

from backend.core import storage


VERSION = "user-storage-v1"
RESOURCE_KINDS = {"file", "thumbnail", "image", "summary_file"}


def safe_path(root: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or any(c in relative for c in ("\\", ":", "\x00")):
        raise ValueError("Invalid migration path")
    target = root / part
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Migration path escapes downloads")
    current = root
    for item in ("", *part.parts):
        current = current / item
        if current.is_symlink():
            raise ValueError("Migration does not follow symbolic links")
    return target


def fingerprint(path: Path) -> dict:
    with path.open("rb") as stream:
        return {"size": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def plan(root: Path, resources: list[dict], downloads: list[dict]) -> dict:
    root = root.resolve()
    result = {"version": VERSION, "root": str(root), "created_at": time.time(), "state": "planned",
              "entries": [], "conflicts": [], "unmapped": []}
    owners = {(row["kind"], row["resource_key"]): row["user_id"] for row in resources if row["kind"] in RESOURCE_KINDS}
    mapped = set()

    def add(kind, name, uid, authority=None):
        source = str(Path(storage.LEGACY_FOLDERS[kind]) / storage.filename(name))
        if source in mapped:
            return
        mapped.add(source)
        destination = str(Path("users") / str(storage.owner(uid)) / storage.FOLDERS[kind] / name)
        entry = {"kind": kind, "name": name, "uid": uid, "source": source, "destination": destination,
                 "authority": authority or [kind, name], "status": "pending"}
        try:
            old, new = safe_path(root, source), safe_path(root, destination)
            present = old if old.is_file() else new if new.is_file() else None
            if present is None:
                entry.update(status="missing", size=0, sha256=None)
            else:
                entry.update(fingerprint(present))
                if new.exists() and (not new.is_file() or fingerprint(new) != {k: entry[k] for k in ("size", "sha256")}):
                    raise ValueError("Target contains different data")
                if not old.exists():
                    entry["status"] = "moved"
            result["entries"].append(entry)
        except (ValueError, OSError) as exc:
            result["conflicts"].append({"source": source, "reason": str(exc)})

    for row in downloads:
        if owners.get(("file", row["filename"])) != row["user_id"]:
            result["conflicts"].append({"source": row["filename"], "reason": "Download and resource ownership differ or resource is absent"})
    for (kind, name), uid in owners.items():
        try:
            add(kind, name, uid)
            if kind == "file":
                for sidecar_kind, sidecar in (
                    ("lyrics", hashlib.sha256(name.encode()).hexdigest() + ".json"),
                    ("file", str(Path(name).with_suffix(".lrc"))),
                    ("thumbnail", "audio-" + hashlib.sha256(name.encode()).hexdigest()[:24] + ".jpg"),
                ):
                    indexed = owners.get((sidecar_kind, sidecar))
                    source = str(Path(storage.LEGACY_FOLDERS[sidecar_kind]) / sidecar)
                    if indexed is not None and indexed != uid:
                        if safe_path(root, source).exists():
                            result["conflicts"].append({"source": source, "reason": "Derived file ownership differs"})
                    elif indexed is None and safe_path(root, source).exists():
                        add(sidecar_kind, sidecar, uid, [kind, name])
        except (ValueError, OSError) as exc:
            result["conflicts"].append({"source": name, "reason": str(exc)})
    mapped.update(item["destination"] for item in result["entries"])
    for candidate in root.rglob("*"):
        relative = str(candidate.relative_to(root))
        if candidate.is_file() and relative not in mapped:
            result["unmapped"].append(relative)
    return result


def validate(data: dict):
    if data.get("version") != VERSION or data.get("conflicts"):
        raise ValueError("Resolve migration conflicts and generate a new plan first")
    root = Path(data["root"])
    seen = set()
    for entry in data["entries"]:
        if entry["destination"] in seen:
            raise ValueError("Duplicate migration destination")
        seen.add(entry["destination"])
        source, target = safe_path(root, entry["source"]), safe_path(root, entry["destination"])
        expected = {k: entry[k] for k in ("size", "sha256")}
        if entry["status"] == "missing":
            if source.exists() or target.exists():
                raise ValueError("A missing file appeared after planning; regenerate the plan")
            continue
        if not source.is_file() and not target.is_file():
            raise ValueError(f"File disappeared: {entry['source']}")
        for path in (source, target):
            if path.exists() and (not path.is_file() or fingerprint(path) != expected):
                raise ValueError(f"File changed or target conflicts: {path.name}")


def move_files(data: dict, manifest: Path, *, active: Path | None = None):
    # Validate the entire plan before moving its first file.
    validate(data)
    data["state"] = "applying"
    save(manifest, data)
    if active:
        save(active, data)
    root = Path(data["root"])
    for entry in data["entries"]:
        if entry["status"] == "missing":
            continue
        source, target = safe_path(root, entry["source"]), safe_path(root, entry["destination"])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.exists():
            # link() refuses to replace an existing file; source and target share a filesystem.
            if not target.exists():
                os.link(source, target)
            if fingerprint(target) != {k: entry[k] for k in ("size", "sha256")}:
                raise ValueError("Target verification failed")
            source.unlink()
        entry["status"] = "moved"
        save(manifest, data)
    validate(data)


def rollback_plan(root: Path) -> dict:
    """Include every current user file, including files created after deployment."""
    data = {"version": VERSION, "root": str(root.resolve()), "state": "planned", "created_at": time.time(),
            "entries": [], "conflicts": [], "unmapped": []}
    user_root = safe_path(root, "users")
    if not user_root.exists():
        return data
    categories = {value: key for key, value in storage.FOLDERS.items()}
    for source in user_root.rglob("*"):
        if source.is_symlink():
            data["conflicts"].append({"source": str(source), "reason": "Symbolic link"})
            continue
        if not source.is_file():
            continue
        parts = source.relative_to(user_root).parts
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) <= 0 or parts[1] not in categories:
            data["conflicts"].append({"source": str(source), "reason": "Unknown user storage layout"})
            continue
        kind, uid = categories[parts[1]], int(parts[0])
        # Nested temporary workspaces are preserved under the legacy temporary directory.
        suffix = Path(*parts[2:])
        if len(parts) > 3 and kind != "temp":
            data["conflicts"].append({"source": str(source), "reason": "Unexpected nested resource"})
            continue
        destination = Path(storage.LEGACY_FOLDERS[kind]) / suffix
        if kind == "temp":
            destination = Path("_tmp") / str(uid) / suffix
        data["entries"].append({"kind": kind, "name": source.name, "uid": uid, "source": str(source.relative_to(root)),
                                "destination": str(destination), "status": "pending", **fingerprint(source)})
    validate(data)
    return data


def backup(root: Path, folder: Path, cursor):
    if folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("Backup must be outside downloads")
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    sql = folder / "database.sql"
    with sql.open("w", encoding="utf-8") as output:
        os.chmod(sql, 0o600)
        output.write("SET FOREIGN_KEY_CHECKS=0;\n")
        cursor.execute("SHOW TABLES")
        tables = [next(iter(row.values())) for row in cursor.fetchall()]
        for table in tables:
            quoted = "`" + table.replace("`", "``") + "`"
            cursor.execute("SHOW CREATE TABLE " + quoted)
            definition = cursor.fetchone()["Create Table"]
            output.write("DROP TABLE IF EXISTS " + quoted + ";\n" + definition + ";\n")
            cursor.execute("SELECT * FROM " + quoted)
            for row in cursor.fetchall():
                columns = ",".join("`" + column.replace("`", "``") + "`" for column in row)
                query = "INSERT INTO " + quoted + " (" + columns + ") VALUES (" + ",".join(["%s"] * len(row)) + ");\n"
                output.write(cursor.mogrify(query, tuple(row.values())))
        output.write("SET FOREIGN_KEY_CHECKS=1;\n")
        output.flush()
        os.fsync(output.fileno())
    archive = folder / "downloads.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        os.chmod(archive, 0o600)
        for path in root.rglob("*"):
            if path.is_file():
                safe_path(root, str(path.relative_to(root)))
                output.write(path, arcname=str(path.relative_to(root)))
    # Read every archived member and verify its CRC before allowing relocation.
    with zipfile.ZipFile(archive) as stored:
        if stored.testzip() is not None:
            raise ValueError("File backup verification failed")
