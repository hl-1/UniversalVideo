"""Read-only application schema gate. DDL is executed exclusively by Flyway."""
from fastapi import HTTPException
import pymysql

SCHEMA_VERSION = "4"


def initialize_schema(cursor):
    """Keep the connection hook compatible while replacing automatic migrations."""
    try:
        cursor.execute("SELECT version, success FROM flyway_schema_history ORDER BY installed_rank")
        rows = cursor.fetchall()
    except pymysql.err.ProgrammingError as exc:
        if exc.args[0] != 1146:
            raise
        raise HTTPException(503, "数据库尚未接入 Flyway，请先运行 scripts/database.py migrate；存量库请先 preflight、baseline。") from exc
    versions = [row["version"] for row in rows if row["version"] is not None]
    if not rows or any(not row["success"] for row in rows) or not versions or versions[-1] != SCHEMA_VERSION:
        raise HTTPException(503, f"数据库结构版本不匹配或迁移失败，当前应用要求 Flyway V{SCHEMA_VERSION}。")


def check_database():
    """Always check at startup, even if a previous connection cached readiness."""
    from backend.infrastructure.database import database_config
    with pymysql.connect(**database_config()) as connection, connection.cursor() as cursor:
        initialize_schema(cursor)
