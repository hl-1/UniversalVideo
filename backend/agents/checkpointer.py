"""MySQL LangGraph saver including atomic, replayable pending writes."""
from __future__ import annotations

import asyncio
import base64

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP, get_checkpoint_metadata

from backend.agents import store
from backend.infrastructure.database import database


class MySQLSaver(BaseCheckpointSaver):
    def get_tuple(self, config):
        c = config["configurable"]
        task, namespace, checkpoint = c["thread_id"], c.get("checkpoint_ns", ""), c.get("checkpoint_id")
        with database() as cursor:
            cursor.execute("SELECT * FROM agent_checkpoints WHERE task_id=%s AND namespace=%s AND payload_format<>'pending'" +
                           (" AND checkpoint_id=%s" if checkpoint else " ORDER BY checkpoint_id DESC LIMIT 1"),
                           (task, namespace, checkpoint) if checkpoint else (task, namespace))
            row = cursor.fetchone()
        return self._tuple(row) if row else None

    def _tuple(self, row):
        import json
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        writes = json.loads(row["pending_writes"]) if isinstance(row["pending_writes"], str) else row["pending_writes"]
        configuration = {"configurable": {"thread_id": row["task_id"], "checkpoint_ns": row["namespace"], "checkpoint_id": row["checkpoint_id"]}}
        parent = {"configurable": configuration["configurable"] | {"checkpoint_id": row["parent_checkpoint_id"]}} if row["parent_checkpoint_id"] else None
        return CheckpointTuple(configuration, self.serde.loads_typed((row["payload_format"], bytes(row["payload"]))), metadata,
                               parent, [(w["task_id"], w["channel"], self.serde.loads_typed((w["type"], base64.b64decode(w["value"])))) for w in writes])

    def list(self, config, *, filter=None, before=None, limit=None):
        sql = "SELECT * FROM agent_checkpoints WHERE payload_format<>'pending'"
        args = []
        if config:
            sql += " AND task_id=%s AND namespace=%s"
            args += [config["configurable"]["thread_id"], config["configurable"].get("checkpoint_ns", "")]
        else:
            # Callers must scope checkpoint reads to one task.
            raise ValueError("A task configuration is required")
        if before:
            sql += " AND checkpoint_id<%s"
            args.append(before["configurable"]["checkpoint_id"])
        sql += " ORDER BY checkpoint_id DESC"
        with database() as cursor:
            cursor.execute(sql, args)
            rows = cursor.fetchall()
        count = 0
        for row in rows:
            item = self._tuple(row)
            if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield item

    def put(self, config, checkpoint, metadata, new_versions):
        c = config["configurable"]
        task, namespace = c["thread_id"], c.get("checkpoint_ns", "")
        kind, payload = self.serde.dumps_typed(checkpoint)
        stamp = store.now()
        with database() as cursor:
            store.get(cursor, task)
            parent = c.get("checkpoint_id")
            if parent:
                cursor.execute("SELECT 1 FROM agent_checkpoints WHERE task_id=%s AND namespace=%s AND checkpoint_id=%s", (task, namespace, parent))
                if not cursor.fetchone():
                    raise ValueError("Parent checkpoint does not belong to this task namespace")
            cursor.execute("INSERT INTO agent_checkpoints (task_id,namespace,checkpoint_id,parent_checkpoint_id,payload_format,payload,metadata,pending_writes,created_at,updated_at) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]',%s,%s) ON DUPLICATE KEY UPDATE parent_checkpoint_id=VALUES(parent_checkpoint_id),payload_format=VALUES(payload_format),payload=VALUES(payload),metadata=VALUES(metadata),revision=revision+1,updated_at=VALUES(updated_at)",
                           (task, namespace, checkpoint["id"], parent, kind, payload,
                            store.encode(get_checkpoint_metadata(config, metadata)), stamp, stamp))
        return {"configurable": c | {"checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        c = config["configurable"]
        identity = (c["thread_id"], c.get("checkpoint_ns", ""), c["checkpoint_id"])
        with database() as cursor:
            # LangGraph may submit pending writes before its asynchronous
            # checkpoint save commits. Keep them in a placeholder row which
            # cannot be loaded as a checkpoint until put() fills its payload.
            stamp = store.now()
            cursor.execute("INSERT INTO agent_checkpoints (task_id,namespace,checkpoint_id,payload_format,payload,metadata,pending_writes,created_at,updated_at) VALUES (%s,%s,%s,'pending',%s,'{}','[]',%s,%s) ON DUPLICATE KEY UPDATE revision=revision", (*identity, b"", stamp, stamp))
            cursor.execute("SELECT pending_writes FROM agent_checkpoints WHERE task_id=%s AND namespace=%s AND checkpoint_id=%s FOR UPDATE", identity)
            row = cursor.fetchone()
            if row is None:
                raise ValueError("Checkpoint missing for pending writes")
            import json
            pending = json.loads(row["pending_writes"]) if isinstance(row["pending_writes"], str) else row["pending_writes"]
            indexed = {(w["task_id"], w["index"]): w for w in pending}
            for index, (channel, value) in enumerate(writes):
                write_index = WRITES_IDX_MAP.get(channel, index)
                key = (task_id, write_index)
                if key in indexed and channel not in WRITES_IDX_MAP:
                    continue
                kind, encoded = self.serde.dumps_typed(value)
                indexed[key] = {"task_id": task_id, "channel": channel, "index": write_index,
                                "type": kind, "value": base64.b64encode(encoded).decode(), "task_path": task_path}
            cursor.execute("UPDATE agent_checkpoints SET pending_writes=%s,revision=revision+1,updated_at=%s WHERE task_id=%s AND namespace=%s AND checkpoint_id=%s",
                           (store.encode(list(indexed.values())), store.now(), *identity))

    def delete_thread(self, thread_id):
        with database() as cursor:
            cursor.execute("DELETE FROM agent_checkpoints WHERE task_id=%s", (thread_id,))

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for row in rows:
            yield row

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)
