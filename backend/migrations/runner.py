"""Explicit Flyway operations and read-only schema comparison for adoption."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
from urllib.parse import quote

import pymysql

from backend.core.paths import ROOT_DIR

FLYWAY_VERSION = "13.11.1"
MIGRATIONS = Path(__file__).resolve().parent / "sql"
CONTRACT = Path(__file__).resolve().parent / "schema_v1.json"
PROD_SQL = ROOT_DIR / "deploy" / "sql" / "prod" / "v1"
HISTORY = "flyway_schema_history"
BACKUP_DIR = ROOT_DIR / ".run" / "database-backups"


def schema_snapshot(cursor) -> dict:
    cursor.execute("SELECT DATABASE() AS db")
    schema = cursor.fetchone()["db"]
    cursor.execute("SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME", (schema,))
    tables = {row["TABLE_NAME"]: {"engine": row["ENGINE"], "collation": row["TABLE_COLLATION"],
                               "columns": {}, "indexes": {}, "foreign_keys": []}
              for row in cursor.fetchall() if row["TABLE_NAME"] != HISTORY}
    cursor.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLLATION_NAME "
                   "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s ORDER BY ORDINAL_POSITION", (schema,))
    for row in cursor.fetchall():
        if row["TABLE_NAME"] not in tables:
            continue
        column_type = re.sub(r"\b(tinyint|smallint|mediumint|int|bigint)\(\d+\)", r"\1", row["COLUMN_TYPE"].lower())
        tables[row["TABLE_NAME"]]["columns"][row["COLUMN_NAME"]] = {
            "type": column_type, "nullable": row["IS_NULLABLE"] == "YES",
            "default": row["COLUMN_DEFAULT"], "extra": row["EXTRA"].lower(), "collation": row["COLLATION_NAME"]}
    cursor.execute("SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SUB_PART FROM information_schema.STATISTICS "
                   "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX", (schema,))
    for row in cursor.fetchall():
        if row["TABLE_NAME"] in tables:
            index = tables[row["TABLE_NAME"]]["indexes"].setdefault(
                row["INDEX_NAME"], {"unique": not row["NON_UNIQUE"], "columns": []})
            index["columns"].append([row["COLUMN_NAME"], row["SUB_PART"]])
    cursor.execute("SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, "
                   "k.REFERENCED_COLUMN_NAME, r.DELETE_RULE, r.UPDATE_RULE FROM information_schema.KEY_COLUMN_USAGE k "
                   "JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA "
                   "AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
                   "WHERE k.TABLE_SCHEMA=%s ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION", (schema,))
    relations = {}
    for row in cursor.fetchall():
        key = (row["TABLE_NAME"], row["CONSTRAINT_NAME"])
        relation = relations.setdefault(key, {"columns": [], "table": row["REFERENCED_TABLE_NAME"],
            "references": [], "delete": row["DELETE_RULE"], "update": row["UPDATE_RULE"]})
        relation["columns"].append(row["COLUMN_NAME"])
        relation["references"].append(row["REFERENCED_COLUMN_NAME"])
    for (table, _), relation in relations.items():
        tables[table]["foreign_keys"].append(relation)
    return tables


def schema_differences(actual: dict, expected: dict | None = None) -> list[str]:
    expected = expected if expected is not None else json.loads(CONTRACT.read_text(encoding="utf-8"))
    differences = []
    for table in sorted(set(expected) - set(actual)):
        differences.append(f"Missing table: {table}")
    for table in sorted(set(actual) - set(expected)):
        differences.append(f"Unexpected table: {table}")
    allowed_collations = {"utf8mb4_unicode_ci", "utf8mb4_0900_ai_ci"}
    for table in sorted(set(expected) & set(actual)):
        current, target = actual[table], expected[table]
        if current["engine"] != target["engine"]:
            differences.append(f"{table}: storage engine mismatch")
        if current["collation"] not in allowed_collations:
            differences.append(f"{table}: unsupported collation {current['collation']}")
        for column in sorted(set(target["columns"]) | set(current["columns"])):
            left, right = current["columns"].get(column), target["columns"].get(column)
            if left is None or right is None:
                differences.append(f"{table}.{column}: missing or unexpected column")
                continue
            left, right = dict(left), dict(right)
            lc, rc = left.pop("collation"), right.pop("collation")
            if left != right or (rc is not None and lc not in allowed_collations):
                differences.append(f"{table}.{column}: type/default/nullability/extra/collation mismatch")
        # MySQL may name implicit FK indexes differently. Compare their definitions.
        signatures = lambda indexes: {json.dumps(item, sort_keys=True) for item in indexes.values()}
        if signatures(current["indexes"]) != signatures(target["indexes"]):
            differences.append(f"{table}: index definitions mismatch")
        relations = lambda values: {json.dumps(item, sort_keys=True) for item in values}
        if relations(current["foreign_keys"]) != relations(target["foreign_keys"]):
            differences.append(f"{table}: foreign key definitions mismatch")
        for relation in current["foreign_keys"]:
            for column, referenced in zip(relation["columns"], relation["references"]):
                if relation["table"] not in actual:
                    continue
                if current["columns"][column]["collation"] != actual[relation["table"]]["columns"][referenced]["collation"]:
                    differences.append(f"{table}.{column}: foreign key collation mismatch")
    return differences


def preflight(cursor) -> None:
    differences = schema_differences(schema_snapshot(cursor))
    if differences:
        raise RuntimeError("Schema is not ready for V1 baseline:\n" + "\n".join(differences))


def migration_environment(config: dict) -> dict:
    from backend.core.configuration import local_environment
    local = local_environment()
    def environment(name):
        return os.getenv(name, local.get(name))
    result = os.environ.copy()
    # Connection credentials stay in child environment variables, never argv.
    host = config["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    user = environment("VIDEODREAM_MIGRATION_USER")
    password = environment("VIDEODREAM_MIGRATION_PASSWORD") if user else config["password"]
    if user and password is None:
        raise RuntimeError("VIDEODREAM_MIGRATION_PASSWORD must be configured for the migration account")
    result.update(FLYWAY_URL=f"jdbc:mysql://{host}:{config['port']}/{quote(config['database'], safe='')}",
                  FLYWAY_USER=user or config["user"], FLYWAY_PASSWORD=password,
                  VIDEODREAM_MIGRATION_USER=user or config["user"], VIDEODREAM_MIGRATION_PASSWORD=password)
    for key in ("host", "port", "database", "user", "password"):
        result["MYSQL_" + key.upper()] = str(config[key])
    return result


def migration_config(config: dict) -> dict:
    environment = migration_environment(config)
    return config | {"user": environment["FLYWAY_USER"], "password": environment["FLYWAY_PASSWORD"]}


def run_flyway(config: dict, action: str, *, engine="local", locations: Path | None = None,
               capture=False, user_id_files=False) -> subprocess.CompletedProcess:
    if action not in {"info", "validate", "migrate", "baseline"}:
        raise ValueError("Unsupported Flyway operation")
    if action == "migrate" and locations is None and user_id_files:
        with pymysql.connect(**migration_config(config)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_id_migration_map'")
            if not cursor.fetchone()["n"]:
                print("Pre-snowflake database backup: " + str(backup_database(cursor)), flush=True)
    environment = migration_environment(config)
    if engine == "docker":
        if locations is not None:
            raise ValueError("Custom migration locations are supported only by the local test runner")
        # The caller already performs baseline preflight/backup under its lock.
        # Invoke Flyway directly, so the container cannot re-enter that Python flow.
        command = ["docker", "compose", "-f", str(ROOT_DIR / "deploy" / "compose.yml"), "run", "--rm", "--no-deps",
                   "--entrypoint", "/flyway/flyway",
                   *[item for name in ("FLYWAY_URL", "FLYWAY_USER", "FLYWAY_PASSWORD") for item in ("-e", name)],
                   "migrate", "-configFiles=/app/config/flyway.conf",
                   "-locations=filesystem:/app/backend/migrations/sql", action]
    else:
        executable = os.getenv("VIDEODREAM_FLYWAY_EXE")
        if not executable:
            executable = str(ROOT_DIR / ".run" / "tools" / f"flyway-{FLYWAY_VERSION}" /
                             ("flyway.cmd" if os.name == "nt" else "flyway"))
            if not Path(executable).is_file():
                executable = shutil.which("flyway")
        if not executable:
            raise RuntimeError("Flyway is not installed. Run scripts/setup-flyway.py (Java 21+) or use --engine docker.")
        command = [executable, f"-configFiles={ROOT_DIR / 'config' / 'flyway.conf'}",
                   f"-locations=filesystem:{locations or MIGRATIONS}", action]
    result = subprocess.run(command, env=environment, cwd=ROOT_DIR, text=True, capture_output=capture)
    if result.returncode:
        if capture:
            # Do not include arbitrary subprocess output in errors (it may contain credentials).
            raise RuntimeError(f"Flyway {action} failed (exit {result.returncode})")
        raise RuntimeError(f"Flyway {action} failed; application must remain stopped")
    if action == 'migrate' and locations is None:
        from backend.migrations.user_snowflake import migrate as migrate_user_ids
        migrate_user_ids(config, move_files=user_id_files)
        backfill_discovery(config)
    return result


def backfill_discovery(config):
    """Run after successful V4 DDL, before starting either paid executor."""
    from backend.modules.accounts import service as accounts
    from backend.modules.music.discovery import pool, usage
    with pymysql.connect(**migration_config(config)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT config FROM system_settings WHERE id=1")
        row=cursor.fetchone()
        settings=accounts.DEFAULT_SETTINGS | (json.loads(row['config']) if isinstance(row['config'],str) else row['config'])
        cursor.execute('SELECT id,username,role FROM users ORDER BY id')
        for user in cursor.fetchall():
            usage.lock(cursor,user['id'])
            usage.sync_candidate(cursor,user['id'],settings)
            marker=accounts.principal.set(accounts.Principal(user['id'],user['username'],user['role']))
            try:
                pool.bootstrap(cursor,user['id'])
            finally:
                accounts.principal.reset(marker)
        connection.commit()


def backup_database(cursor) -> Path:
    directory = BACKUP_DIR / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, mode=0o700)
    destination = directory / "database.sql"
    cursor.execute("SHOW TABLES")
    tables = [next(iter(row.values())) for row in cursor.fetchall()]
    cursor.execute("SELECT @@SESSION.sql_mode AS mode")
    sql_mode = cursor.fetchone()["mode"]
    with destination.open("x", encoding="utf-8") as output:
        destination.chmod(0o600)
        output.write("-- Restore only into the intended VideoDream database with the application stopped.\n"
                     "SET NAMES utf8mb4;\nSET @VIDEODREAM_OLD_SQL_MODE=@@SESSION.SQL_MODE;\n")
        output.write(cursor.mogrify("SET SESSION SQL_MODE=%s;\n", (sql_mode,)))
        output.write("SET FOREIGN_KEY_CHECKS=0;\n")
        for table in tables:
            quoted = "`" + table.replace("`", "``") + "`"
            cursor.execute("SHOW CREATE TABLE " + quoted)
            output.write("DROP TABLE IF EXISTS " + quoted + ";\n" + cursor.fetchone()["Create Table"] + ";\n")
            cursor.execute('SHOW COLUMNS FROM '+quoted)
            generated={column['Field'] for column in cursor.fetchall() if 'GENERATED' in column['Extra']}
            cursor.execute("SELECT * FROM " + quoted)
            while rows := cursor.fetchmany(500):
                for row in rows:
                    row={key:value for key,value in row.items() if key not in generated}
                    columns = ",".join("`" + name.replace("`", "``") + "`" for name in row)
                    query = "INSERT INTO " + quoted + " (" + columns + ") VALUES (" + ",".join(["%s"] * len(row)) + ");\n"
                    output.write(cursor.mogrify(query, tuple(row.values())))
        output.write("SET FOREIGN_KEY_CHECKS=1;\nSET SESSION SQL_MODE=@VIDEODREAM_OLD_SQL_MODE;\n")
        output.flush()
        os.fsync(output.fileno())
    return destination


def baseline(config: dict, *, engine="local") -> Path:
    with pymysql.connect(**migration_config(config)) as connection, connection.cursor() as cursor:
        lock = "videodream-schema:" + hashlib.sha256(config["database"].encode()).hexdigest()[:40]
        cursor.execute("SELECT GET_LOCK(%s,30) AS acquired", (lock,))
        if cursor.fetchone()["acquired"] != 1:
            raise RuntimeError("Another schema operation is active")
        try:
            cursor.execute("SHOW TABLES LIKE 'flyway_schema_history'")
            if cursor.fetchone():
                raise RuntimeError("Flyway history already exists; use info/validate/migrate")
            preflight(cursor)
            connection.commit()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            backup = backup_database(cursor)
            connection.commit()
            run_flyway(config, "baseline", engine=engine)
            return backup
        finally:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock,))
