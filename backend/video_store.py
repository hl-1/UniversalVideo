from __future__ import annotations

import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote

TABLES = {"video_parse": ("video_parse_records", "record_id", "snapshot"),
          "summary": ("video_summaries", "task_id", "content"),
          "mind_map": ("video_mind_maps", "task_id", "content"),
          "video_view": ("video_view_states", "view_key", "view_state")}
KINDS = {*TABLES, "video_search"}
BACKUP_DIR = Path(__file__).resolve().parent.parent / ".run" / "video-table-backups"
MIGRATION = "video-tables-v1"
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS video_recent_links (
        user_id BIGINT NOT NULL, url_hash CHAR(64) NOT NULL, link_id VARCHAR(64) NOT NULL,
        url TEXT NOT NULL, submitted_at DOUBLE NOT NULL,
        PRIMARY KEY(user_id,url_hash), INDEX video_recent_time(user_id,submitted_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS video_parse_records (
        record_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL,
        url TEXT NOT NULL, webpage_url TEXT, title TEXT, platform VARCHAR(100) NOT NULL,
        status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), snapshot JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_parse_owner_time(user_id,deleted,created_at,record_id),
        INDEX video_parse_platform(user_id,deleted,platform,created_at),
        INDEX video_parse_status(user_id,deleted,status,created_at), INDEX video_parse_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS video_summaries (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        webpage_url TEXT, status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), content JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_summary_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_summary_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS video_mind_maps (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        summary_task_id VARCHAR(64), version INT UNSIGNED NOT NULL, status VARCHAR(24) NOT NULL,
        worker_id VARCHAR(255), content JSON NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        deleted BOOLEAN NOT NULL DEFAULT FALSE, UNIQUE KEY video_map_version(summary_task_id,version),
        INDEX video_map_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_map_summary(user_id,summary_task_id,deleted,status,created_at),
        INDEX video_map_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS video_view_states (
        user_id BIGINT NOT NULL, record_id VARCHAR(64) NOT NULL, view_key VARCHAR(255) NOT NULL,
        selected_tab VARCHAR(24) NOT NULL, playback_time DOUBLE NOT NULL, view_state JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(user_id,record_id), UNIQUE KEY video_view_key(view_key),
        INDEX video_view_recent(user_id,deleted,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def adapt(kind: str, row: dict) -> dict:
    _, key, content = TABLES[kind]
    value = json.loads(row[content])
    for field in ("status", "worker_id", "platform", "parse_record_id", "summary_task_id", "version"):
        if field in row:
            value[field] = row[field]
    return {"kind": kind, "resource_key": row[key], "user_id": row["user_id"], "payload": dumps(value),
            "created_at": row["created_at"], "updated_at": row["updated_at"], "deleted": row["deleted"]}


def get(cursor, kind: str, key: str, *, lock=False, include_deleted=False) -> dict | None:
    if kind == "video_search":
        cursor.execute("SELECT * FROM resources WHERE kind=%s AND resource_key=%s" +
                       ("" if include_deleted else " AND deleted=FALSE") + (" FOR UPDATE" if lock else ""), (kind, key))
        row = cursor.fetchone()
        if row:
            row["payload"] = dumps({"items": recent(cursor, row["user_id"])})
        return row
    table, column, _ = TABLES[kind]
    cursor.execute(f"SELECT * FROM {table} WHERE {column}=%s" +
                   ("" if include_deleted else " AND deleted=FALSE") + (" FOR UPDATE" if lock else ""), (key,))
    row = cursor.fetchone()
    return adapt(kind, row) if row else None


def hydrate(cursor, row: dict) -> dict:
    if row["kind"] in KINDS:
        full = get(cursor, row["kind"], row["resource_key"], include_deleted=True)
        if full:
            return row | full
    return row


def catalogue(cursor, kind: str, key: str, uid: int, value: dict, created: float, updated: float, deleted=False):
    metadata = {field: value[field] for field in ("status", "parse_record_id", "summary_task_id", "version", "record_id") if field in value}
    metadata["storage"] = "video-tables-v1"
    cursor.execute("""INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at,deleted)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE payload=VALUES(payload),
        updated_at=VALUES(updated_at),deleted=VALUES(deleted)""", (kind, key, uid, dumps(metadata), created, updated, deleted))


def save(cursor, kind: str, key: str, uid: int, value: dict, *, created=None, updated=None, deleted=False):
    created = time.time() if created is None else created
    updated = created if updated is None else updated
    if kind == "video_search":
        cursor.execute("DELETE FROM video_recent_links WHERE user_id=%s", (uid,))
        if not deleted:
            seen = set()
            for item in value.get("items", [])[:5]:
                digest = hashlib.sha256(item["url"].encode()).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                cursor.execute("INSERT INTO video_recent_links VALUES (%s,%s,%s,%s,%s)",
                               (uid, digest, item.get("id", digest[:24]), item["url"], item.get("submitted_at", updated)))
    else:
        table, column, content = TABLES[kind]
        fields = {column: key, "user_id": uid, content: dumps(value), "created_at": created, "updated_at": updated, "deleted": deleted}
        if kind == "video_view":
            fields.update(record_id=value["record_id"], selected_tab=value.get("tab", "summary"), playback_time=value.get("playback_time", 0))
        else:
            fields.update(status=value.get("status", "pending"), worker_id=value.get("worker_id"))
            if kind == "video_parse":
                result = value.get("result") or {}
                fields.update(url=value["url"], webpage_url=result.get("webpage_url"), title=result.get("title"), platform=value.get("platform", ""))
            else:
                fields["parse_record_id"] = value.get("parse_record_id")
                if kind == "summary":
                    fields["webpage_url"] = (value.get("result") or {}).get("webpage_url") or value.get("url")
                else:
                    fields.update(summary_task_id=value.get("summary_task_id"), version=value.get("version", 1))
        columns = ",".join(fields)
        cursor.execute(f"SELECT user_id FROM {table} WHERE {column}=%s FOR UPDATE", (key,))
        existing = cursor.fetchone()
        if existing:
            if existing["user_id"] != uid:
                raise ValueError("Video resource owner cannot change")
            changed = {name: item for name, item in fields.items() if name not in {column, "user_id", "created_at"}}
            cursor.execute(f"UPDATE {table} SET {','.join(name + '=%s' for name in changed)} WHERE {column}=%s",
                           (*changed.values(), key))
        else:
            cursor.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join(['%s'] * len(fields))})", tuple(fields.values()))
    catalogue(cursor, kind, key, uid, value, created, updated, deleted)


def recent(cursor, uid: int, *, lock=False) -> list[dict]:
    cursor.execute("SELECT link_id AS id,url,submitted_at FROM video_recent_links WHERE user_id=%s ORDER BY submitted_at DESC,url_hash DESC LIMIT 5" +
                   (" FOR UPDATE" if lock else ""), (uid,))
    return list(cursor.fetchall())


def tombstone(cursor, kind: str, key: str):
    now = time.time()
    if kind in TABLES:
        table, column, _ = TABLES[kind]
        cursor.execute(f"UPDATE {table} SET deleted=TRUE,updated_at=%s WHERE {column}=%s", (now, key))
    elif kind == "video_search":
        cursor.execute("SELECT user_id FROM resources WHERE kind=%s AND resource_key=%s FOR UPDATE", (kind, key))
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM video_recent_links WHERE user_id=%s", (row["user_id"],))
    cursor.execute("UPDATE resources SET deleted=TRUE,updated_at=%s WHERE kind=%s AND resource_key=%s", (now, kind, key))


def migrate(cursor):
    for statement in SCHEMA:
        cursor.execute(statement)
    cursor.execute("SELECT DATABASE() AS db")
    lock = "video-migrate:" + hashlib.sha256(cursor.fetchone()["db"].encode()).hexdigest()[:40]
    cursor.execute("SELECT GET_LOCK(%s,30) AS acquired", (lock,))
    if cursor.fetchone()["acquired"] != 1:
        raise RuntimeError("Could not acquire video migration lock")
    try:
        cursor.execute("SELECT id FROM account_migrations WHERE id=%s", (MIGRATION,))
        if cursor.fetchone():
            return
        cursor.execute("SELECT * FROM resources WHERE kind IN ('video_search','video_parse','summary','mind_map','video_view') ORDER BY created_at,resource_key FOR UPDATE")
        rows = cursor.fetchall()
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{int(time.time())}-{uuid.uuid4().hex[:12]}.json"
        backup.write_text(dumps(rows), encoding="utf-8")
        from backend.library import DOWNLOAD_DIR
        with zipfile.ZipFile(backup.with_suffix(".files.zip"), "w", compression=zipfile.ZIP_STORED) as archive:
            names = set()
            for row in rows:
                result = json.loads(row["payload"]).get("result") or {}
                for field in ("markdown_url", "json_url"):
                    name = unquote(Path(result.get(field) or "").name)
                    path = DOWNLOAD_DIR / "_summaries" / name
                    if name not in names and path.is_file() and not path.is_symlink() and path.resolve().parent == (DOWNLOAD_DIR / "_summaries").resolve():
                        archive.write(path, arcname=name)
                        names.add(name)
        versions: dict[str, set[int]] = {}
        for row in rows:
            value = json.loads(row["payload"])
            if row["kind"] == "mind_map":
                used = versions.setdefault(value.get("summary_task_id") or row["resource_key"], set())
                version = value.get("version", 1)
                if not isinstance(version, int) or version < 1 or version in used:
                    version = max(used, default=0) + 1
                used.add(version)
                value["version"] = version
            save(cursor, row["kind"], row["resource_key"], row["user_id"], value,
                 created=row["created_at"], updated=row["updated_at"], deleted=row["deleted"])
            migrated = get(cursor, row["kind"], row["resource_key"], include_deleted=True)
            if not migrated or (row["kind"] != "video_search" and any(json.loads(migrated["payload"]).get(k) != v for k, v in value.items())):
                raise RuntimeError(f"Video migration verification failed: {row['kind']}:{row['resource_key']}")
        cursor.execute("INSERT INTO account_migrations VALUES (%s,%s,%s)", (MIGRATION, time.time(), str(backup)))
        cursor.connection.commit()
    except Exception:
        cursor.connection.rollback()
        raise
    finally:
        cursor.execute("SELECT RELEASE_LOCK(%s)", (lock,))
