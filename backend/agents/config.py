from __future__ import annotations

from backend.modules.accounts.service import environment

GRAPH_VERSION = "videodream-agent-v1"
TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})
WAITING = frozenset({"waiting_input", "waiting_auth", "waiting_approval", "waiting_children", "waiting_usage", "interrupted"})


def integer(name, default, minimum=1, maximum=10_000_000):
    return max(minimum, min(maximum, int(environment(name, default))))


def engine():
    value = environment("VIDEODREAM_AGENT_ENGINE", "langgraph")
    if value not in {"legacy", "langgraph"}:
        raise ValueError("VIDEODREAM_AGENT_ENGINE must be legacy or langgraph")
    return value


def available(cursor):
    cursor.execute("SELECT version FROM flyway_schema_history WHERE success=TRUE AND version IS NOT NULL ORDER BY installed_rank DESC LIMIT 1")
    row = cursor.fetchone()
    return bool(row and row["version"] == "8")


def enabled():
    return str(environment("VIDEODREAM_AGENT_ENABLED", "true")).lower() == "true"


def require_enabled():
    from fastapi import HTTPException
    from backend.infrastructure.database import database
    if not enabled():
        raise HTTPException(503, "Agent 新任务入口已关闭")
    with database() as cursor:
        if not available(cursor):
            raise HTTPException(503, "请先执行 Flyway V7/V8 迁移")


def limits(kind):
    tokens, seconds = {"knowledge_ingest": (500000, 300), "knowledge_query": (20000, 90),
                       "browser_playlist": (30000, 180), "music_scene": (150000, 300),
                       "music_profile": (150000, 120)}[kind]
    return {"tokens": tokens, "seconds": seconds, "actions": 30, "children": 8,
            "daily_tokens": integer("VIDEODREAM_AGENT_DAILY_TOKENS", 1000000)}


def model_config(phase):
    from fastapi import HTTPException
    prefix = "LLM_EMBEDDING" if phase == "embedding" else "LLM_RERANK"
    values = {k: environment(f"{prefix}_{k.upper()}", "") for k in ("base_url", "api_key", "model")}
    if not all(values.values()):
        raise HTTPException(503, f"请配置独立的 {prefix}_BASE_URL、API_KEY 和 MODEL")
    if phase == "embedding":
        values["dimensions"] = integer("LLM_EMBEDDING_DIMENSIONS", 1024, maximum=4096)
    return values
