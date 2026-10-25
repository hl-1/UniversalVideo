"""Transactional 63-bit Snowflake user IDs, serialized by the database."""
from __future__ import annotations

from fastapi import HTTPException

EPOCH_MS = 1577836800000  # 2020-01-01 UTC
WORKER_BITS = 10
SEQUENCE_BITS = 12
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1
MIGRATION_WORKER = (1 << WORKER_BITS) - 1


def compose(timestamp_ms: int, worker: int, sequence: int) -> int:
    elapsed = timestamp_ms - EPOCH_MS
    if not 0 <= elapsed < 1 << 41 or not 0 <= worker <= MIGRATION_WORKER or not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError("Snowflake component is outside its supported range")
    return elapsed << (WORKER_BITS + SEQUENCE_BITS) | worker << SEQUENCE_BITS | sequence


def next_id(cursor, *, worker: int | None = None) -> int:
    if worker is None:
        from backend.modules.accounts.service import environment
        try:
            worker = int(environment("VIDEODREAM_USER_ID_WORKER", "0"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(503, "用户 ID 节点配置无效。") from exc
        if not 0 <= worker < MIGRATION_WORKER:
            raise HTTPException(503, "用户 ID 节点必须在 0–1022 之间。")
    cursor.execute("INSERT INTO user_id_generator_state (worker_id,last_timestamp_ms,sequence) VALUES (%s,0,0) ON DUPLICATE KEY UPDATE worker_id=VALUES(worker_id)", (worker,))
    cursor.execute("SELECT last_timestamp_ms,sequence FROM user_id_generator_state WHERE worker_id=%s FOR UPDATE", (worker,))
    state = cursor.fetchone()
    # Read the clock after acquiring the lock. All app instances use the same clock.
    cursor.execute("SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3))*1000 AS UNSIGNED) AS now_ms")
    now = int(cursor.fetchone()["now_ms"])
    last = int(state["last_timestamp_ms"])
    if now < last:
        raise HTTPException(503, "用户 ID 时钟回退，请稍后重试。")
    sequence = int(state["sequence"]) + 1 if now == last else 0
    if sequence > MAX_SEQUENCE:
        raise HTTPException(503, "用户 ID 分配繁忙，请稍后重试。")
    result = compose(now, worker, sequence)
    cursor.execute("UPDATE user_id_generator_state SET last_timestamp_ms=%s,sequence=%s WHERE worker_id=%s", (now, sequence, worker))
    return result
