from fastapi import HTTPException

from backend.agents import store


def clean_user(cursor, uid):
    from backend.agents import config
    if not config.available(cursor):
        return
    cursor.execute("SELECT 1 FROM knowledge_bases WHERE owner_id=%s LIMIT 1", (uid,))
    if cursor.fetchone():
        raise HTTPException(409, "请先转移该用户的知识库所有权并完成删除清理")
    cursor.execute("SELECT 1 FROM agent_tasks WHERE user_id=%s AND status NOT IN ('succeeded','partial','failed','cancelled') LIMIT 1", (uid,))
    if cursor.fetchone():
        raise HTTPException(409, "请先取消或完成该用户的 Agent 任务")
    cursor.execute("SELECT 1 FROM agent_model_calls WHERE user_id=%s AND state<>'settled' LIMIT 1", (uid,))
    if cursor.fetchone():
        raise HTTPException(409, "该用户存在未核对模型调用，暂不能删除")
    cursor.execute("DELETE FROM knowledge_members WHERE user_id=%s", (uid,))
    cursor.execute("UPDATE knowledge_documents d JOIN knowledge_bases b ON b.id=d.kb_id SET d.uploaded_by=b.owner_id WHERE d.uploaded_by=%s", (uid,))
    cursor.execute("UPDATE agent_actions SET approved_by=NULL WHERE approved_by=%s", (uid,))
    for table in ("agent_checkpoints", "agent_events", "agent_actions"):
        cursor.execute(f"DELETE x FROM {table} x JOIN agent_tasks t ON t.id=x.task_id WHERE t.user_id=%s", (uid,))
    cursor.execute("DELETE FROM agent_model_calls WHERE user_id=%s", (uid,))
    cursor.execute("UPDATE agent_tasks SET parent_id=NULL,root_id=NULL WHERE user_id=%s", (uid,))
    cursor.execute("DELETE FROM agent_tasks WHERE user_id=%s", (uid,))
