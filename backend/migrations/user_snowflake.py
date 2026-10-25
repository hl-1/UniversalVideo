"""Offline, resumable rekeying of users and their durable ownership."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pymysql

from backend.core import paths
from backend.modules.accounts.user_ids import MIGRATION_WORKER, next_id

MARKER = "users-snowflake-v1"
USER_FIELDS = {"uid", "user_id", "owner_id", "actor_id", "sender_id", "target_user_id", "requester_id", "partner_id", "creator_id", "inviter_id", "invitee_id", "claimed_by", "author_id", "reporter_id", "handler_id", "contributor_id", "from", "by"}
USER_LISTS = {"recipients", "singers", "controllers", "user_ids", "members"}


def quote(name):
    return "`" + name.replace("`", "``") + "`"


def rewrite(value, mapping, field=""):
    if isinstance(value, dict):
        return {key: rewrite(item, mapping, key) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item, mapping, "user_id" if field in USER_LISTS else field) for item in value]
    if not isinstance(value, bool) and isinstance(value, (int, str)) and field in USER_FIELDS:
        replacement = mapping.get(str(value))
        if replacement is not None:
            return int(replacement) if isinstance(value, int) else replacement
    if isinstance(value, str):
        for old, new in mapping.items():
            for separator in ("/", "\\"):
                value = value.replace(f"users{separator}{old}{separator}", f"users{separator}{new}{separator}")
                value = value.replace(f"_tmp{separator}{old}{separator}", f"_tmp{separator}{new}{separator}")
    return value


def owned_columns(cursor):
    cursor.execute("""SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME='user_id'
        UNION SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME='users' AND REFERENCED_COLUMN_NAME='id'
        UNION SELECT 'audit_logs','actor_id' UNION SELECT 'music_profile','id' UNION SELECT 'music_theme','id'
        UNION SELECT 'users','id'""")
    return [(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in cursor.fetchall()]


def rewrite_documents(cursor, mapping):
    cursor.execute("""SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND DATA_TYPE IN ('json','text','mediumtext','longtext','varchar')
        AND TABLE_NAME NOT IN ('flyway_schema_history','account_migrations','users')""")
    columns = {}
    for row in cursor.fetchall():
        columns.setdefault(row["TABLE_NAME"], []).append(row["COLUMN_NAME"])
    for table, fields in columns.items():
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION", (table,))
        primary = [r["COLUMN_NAME"] for r in cursor.fetchall()]
        if not primary:
            continue
        selected = list(dict.fromkeys(primary + fields))
        cursor.execute("SELECT " + ",".join(map(quote, selected)) + " FROM " + quote(table))
        for row in cursor.fetchall():
            changes = {}
            for field in fields:
                raw = row[field]
                if not isinstance(raw, str):
                    continue
                try:
                    document = json.loads(raw)
                except (ValueError, TypeError):
                    document = None
                converted = rewrite(document if isinstance(document, (dict, list)) else raw, mapping)
                if isinstance(document, (dict, list)):
                    if converted != document:
                        changes[field] = json.dumps(converted, ensure_ascii=False)
                elif converted != raw:
                    changes[field] = converted
            if changes:
                cursor.execute("UPDATE " + quote(table) + " SET " + ",".join(quote(f) + "=%s" for f in changes) + " WHERE " + " AND ".join(quote(f) + "=%s" for f in primary), tuple(changes.values()) + tuple(row[f] for f in primary))
    # These keys are derived from user identity rather than random resource IDs.
    for old, new in mapping.items():
        cursor.execute("UPDATE resources SET resource_key=%s WHERE kind IN ('video_search','video_view') AND resource_key=%s", (new, old))
        cursor.execute("UPDATE video_view_states SET view_key=%s WHERE view_key=%s", (new, old))
        cursor.execute("UPDATE video_view_states SET view_key=CONCAT(%s,SUBSTRING(view_key,%s)) WHERE view_key LIKE %s", (new, len(old) + 1, old + ":%"))
        cursor.execute("UPDATE resources SET resource_key=CONCAT(%s,SUBSTRING(resource_key,%s)) WHERE kind='video_view' AND resource_key LIKE %s", (new, len(old) + 1, old + ":%"))
        cursor.execute("UPDATE audit_logs SET target=%s WHERE target=%s AND action IN ('register','login','user_create','user_delete','password_reset','password_change')", (new, old))
    cursor.execute("SELECT id FROM social_conversations WHERE kind='direct'")
    for row in cursor.fetchall():
        cursor.execute("SELECT user_id FROM social_conversation_members WHERE conversation_id=%s ORDER BY user_id", (row["id"],))
        members = [str(member["user_id"]) for member in cursor.fetchall()]
        if len(members) == 2:
            key = hashlib.sha256(":".join(members).encode()).hexdigest()
            cursor.execute("UPDATE social_conversations SET direct_key=%s WHERE id=%s", (key, row["id"]))


def fingerprint(directory):
    digest = hashlib.sha256()
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise RuntimeError("User directory contains a symbolic link; resolve it before migration")
        if item.is_file():
            stat = item.stat()
            digest.update(f"{item.relative_to(directory)}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def file_plan(mapping, manifest):
    roots = [paths.DOWNLOAD_DIR / "users", paths.DOWNLOAD_DIR / "_tmp", paths.ROOT_DIR / ".run" / "browser-profiles"]
    if manifest.exists():
        plan = json.loads(manifest.read_text(encoding="utf-8"))
        if plan["mapping"] != mapping:
            raise RuntimeError("Migration manifest has a different ID mapping")
    else:
        plan = {"mapping": mapping, "moves": []}
        for root in roots:
            for old, new in mapping.items():
                source, target = root / old, root / new
                if source.exists():
                    if source.is_symlink() or target.exists():
                        raise RuntimeError("User folder is a link or its migration destination already exists")
                    plan["moves"].append({"source": str(source.resolve()), "target": str(target.resolve()), "fingerprint": fingerprint(source)})
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    allowed = {root.resolve() for root in roots}
    for move in plan["moves"]:
        source, target = Path(move["source"]), Path(move["target"])
        if source.parent.resolve() not in allowed or target.parent.resolve() != source.parent.resolve() or source.name not in mapping or target.name != mapping[source.name]:
            raise RuntimeError("Migration folder is outside the expected user storage roots")
        if source.is_symlink() or target.is_symlink():
            raise RuntimeError("Migration folders must not be symbolic links")
    return plan


def move_folders(plan, *, reverse=False):
    for move in reversed(plan["moves"]) if reverse else plan["moves"]:
        source, target = Path(move["source"]), Path(move["target"])
        if reverse:
            source, target = target, source
        if source.exists():
            if target.exists() or fingerprint(source) != move["fingerprint"]:
                raise RuntimeError("User folder changed or destination exists; migration stopped")
            source.rename(target)
        elif not target.exists() or fingerprint(target) != move["fingerprint"]:
            raise RuntimeError("User folder cannot be verified; migration stopped")


def invalidate_caches():
    from backend.modules.accounts import service as accounts
    client = accounts.redis_client()
    # Only this project's transient identity/session namespaces. Keep rate limits.
    for suffix in ("session:*", "user-sessions:*", "social:*", "video:*", "download:*"):
        batch = []
        for key in client.scan_iter(match=accounts.redis_prefix() + suffix, count=200):
            batch.append(key)
            if len(batch) == 200:
                client.delete(*batch)
                batch = []
        if batch:
            client.delete(*batch)


def migrate(config, *, move_files=False):
    from backend.migrations.runner import migration_config
    identity = hashlib.sha256(f"{config['host']}:{config['port']}/{config['database']}".encode()).hexdigest()[:16]
    manifest = paths.RUN_DIR / "user-id-migrations" / (identity + ".json")
    with pymysql.connect(**migration_config(config)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s,30) AS acquired", ("user-snowflake:" + identity,))
        if cursor.fetchone()["acquired"] != 1:
            raise RuntimeError("Another user ID migration is running")
        try:
            cursor.execute("SELECT 1 FROM account_migrations WHERE id=%s", (MARKER,))
            if cursor.fetchone():
                return
            cursor.execute("SELECT id FROM users WHERE id NOT IN (SELECT new_id FROM user_id_migration_map) ORDER BY id FOR UPDATE")
            for row in cursor.fetchall():
                cursor.execute("SELECT new_id FROM user_id_migration_map WHERE old_id=%s", (row["id"],))
                if not cursor.fetchone():
                    cursor.execute("INSERT INTO user_id_migration_map (old_id,new_id,created_at) VALUES (%s,%s,%s)", (row["id"], next_id(cursor, worker=MIGRATION_WORKER), time.time()))
            connection.commit()
            cursor.execute("SELECT old_id,new_id FROM user_id_migration_map ORDER BY old_id")
            mapping = {str(row["old_id"]): str(row["new_id"]) for row in cursor.fetchall()}
            if set(mapping) & set(mapping.values()):
                raise RuntimeError("User ID mapping overlaps existing IDs")
            plan = file_plan(mapping, manifest) if move_files else {"moves": []}
            columns = owned_columns(cursor)
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                connection.begin()
                for table, column in columns:
                    cursor.execute(f"UPDATE {quote(table)} t JOIN user_id_migration_map m ON t.{quote(column)}=m.old_id SET t.{quote(column)}=m.new_id")
                rewrite_documents(cursor, mapping)
                for table, column in columns:
                    cursor.execute(f"SELECT COUNT(*) AS n FROM {quote(table)} t JOIN user_id_migration_map m ON t.{quote(column)}=m.old_id")
                    if cursor.fetchone()["n"]:
                        raise RuntimeError(f"Old user IDs remain in {table}.{column}")
                cursor.execute("SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME='users'")
                for reference in cursor.fetchall():
                    table, column = reference["TABLE_NAME"], reference["COLUMN_NAME"]
                    cursor.execute(f"SELECT COUNT(*) AS n FROM {quote(table)} t LEFT JOIN users u ON t.{quote(column)}=u.id WHERE t.{quote(column)} IS NOT NULL AND u.id IS NULL")
                    if cursor.fetchone()["n"]:
                        raise RuntimeError(f"Orphaned user reference in {table}.{column}")
                move_folders(plan)
                if move_files:
                    invalidate_caches()
                cursor.execute("INSERT INTO account_migrations (id,completed_at,backup_path) VALUES (%s,%s,%s)", (MARKER, time.time(), str(manifest) if move_files else "isolated-schema"))
                connection.commit()
            except BaseException:
                connection.rollback()
                move_folders(plan, reverse=True)
                raise
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        finally:
            cursor.execute("SELECT RELEASE_LOCK(%s)", ("user-snowflake:" + identity,))
