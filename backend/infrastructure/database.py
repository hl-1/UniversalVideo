"""Shared MySQL connections and transaction boundaries."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager

import pymysql
from fastapi import HTTPException

from backend.core import configuration
from backend.core.paths import CONFIG_DIR


CONFIG_PATH = CONFIG_DIR / "database.json"
schema_lock = threading.Lock()
schema_ready = False
schema_database = None

def database_config(*, local_path=None) -> dict:
    config = {}
    if CONFIG_PATH.is_file():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "音乐库数据库配置无法读取。") from exc
    local_environment = configuration.local_environment(local_path=local_path)
    for key in ("host", "port", "database", "user", "password"):
        variable = f"MYSQL_{key.upper()}"
        value = os.getenv(variable, local_environment.get(variable))
        value = os.getenv(f"VIDEODREAM_DB_{key.upper()}", value)
        if value is not None:
            config[key] = value
    if not config.get("database") or not config.get("user"):
        raise HTTPException(503, "音乐库尚未配置 MySQL 连接。")
    try:
        return {"host": config.get("host", "127.0.0.1"), "port": int(config.get("port", 3306)),
                "database": config["database"], "user": config["user"], "password": config.get("password", ""),
                "charset": "utf8mb4", "cursorclass": pymysql.cursors.DictCursor,
                "connect_timeout": 5, "read_timeout": 15, "write_timeout": 15}
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "音乐库数据库端口配置无效。") from exc


@contextmanager
def database():
    global schema_ready, schema_database
    connection = None
    try:
        config = database_config()
        connection = pymysql.connect(**config)
        identity = tuple(config[key] for key in ("host", "port", "database", "user"))
        with schema_lock:
            if not schema_ready or schema_database != identity:
                with connection.cursor() as cursor:
                    from backend.migrations import initialize_schema
                    initialize_schema(cursor)
                schema_ready = True
                schema_database = identity
        with connection.cursor() as cursor:
            yield cursor
        connection.commit()
    except pymysql.MySQLError as exc:
        if connection:
            connection.rollback()
        raise HTTPException(503, "音乐库暂时无法连接 MySQL，请检查数据库服务。") from exc
    except Exception:
        if connection:
            connection.rollback()
        raise
    finally:
        if connection:
            connection.close()
