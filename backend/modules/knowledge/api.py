from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.agents import config, runtime, store
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts
from backend.modules.knowledge import service, storage

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=[Depends(config.require_enabled)])


class BaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class BaseUpdate(BaseInput):
    revision: int = Field(ge=1)
    owner_id: str | None = Field(None, pattern=r"^[0-9]{1,19}$")


class MemberInput(BaseModel):
    revision: int = Field(ge=1)
    user_id: str = Field(pattern=r"^[0-9]{1,19}$")
    role: Literal["editor", "viewer"] = "viewer"


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_ids: list[str] = Field(default_factory=list, max_length=20)
    rerank: bool = False


class VideoImport(BaseModel):
    kb_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    summary_task_id: str = Field(min_length=1, max_length=64)
    kind: Literal["video_subtitle", "video_summary"]


@router.get("/bases")
def bases():
    uid = accounts.current().id
    with database() as cursor:
        ids = service.allowed(cursor, uid)
        items = []
        for kb_id in ids:
            kb = service.require(cursor, kb_id, uid)
            cursor.execute("SELECT COUNT(*) AS n FROM knowledge_documents WHERE kb_id=%s AND status<>'deleted'", (kb_id,))
            items.append(kb | {"owner_id": str(kb["owner_id"]), "document_count": cursor.fetchone()["n"]})
        return {"items": items}


@router.post("/bases", status_code=201)
def create_base(payload: BaseInput):
    if not payload.name.strip():
        raise HTTPException(422, "知识库名称不能为空")
    key, stamp = secrets.token_hex(16), store.now()
    with database() as cursor:
        cursor.execute("INSERT INTO knowledge_bases (id,owner_id,name,status,config,created_at,updated_at) VALUES (%s,%s,%s,'active',%s,%s,%s)",
                       (key, accounts.current().id, payload.name.strip(), store.encode({"chunk_size": 600, "overlap": 80, "top_k": 20, "evidence": 6, "index": storage.index_name()}), stamp, stamp))
    return {"id": key, "revision": 1}


def require_revision(kb, revision):
    if kb["revision"] != revision:
        raise HTTPException(409, "知识库版本已变化，请刷新")


@router.patch("/bases/{kb_id}")
def update_base(kb_id: str, payload: BaseUpdate):
    with database() as cursor:
        kb = service.require(cursor, kb_id, accounts.current().id, "owner", True)
        require_revision(kb, payload.revision)
        owner = int(payload.owner_id) if payload.owner_id else kb["owner_id"]
        if owner != kb["owner_id"]:
            cursor.execute("SELECT id FROM users WHERE id=%s AND status='active'", (owner,))
            if not cursor.fetchone():
                raise HTTPException(422, "新所有者账号不可用")
            cursor.execute("INSERT INTO knowledge_members (kb_id,user_id,role,created_at,updated_at) VALUES (%s,%s,'editor',%s,%s) ON DUPLICATE KEY UPDATE role='editor',updated_at=VALUES(updated_at)", (kb_id, kb["owner_id"], store.now(), store.now()))
            cursor.execute("DELETE FROM knowledge_members WHERE kb_id=%s AND user_id=%s", (kb_id, owner))
        if not payload.name.strip():
            raise HTTPException(422, "名称不能为空")
        cursor.execute("UPDATE knowledge_bases SET name=%s,owner_id=%s,revision=revision+1,updated_at=%s WHERE id=%s", (payload.name.strip(), owner, store.now(), kb_id))
    return {"id": kb_id, "revision": payload.revision + 1}


@router.delete("/bases/{kb_id}")
def delete_base(kb_id: str, revision: int = Query(ge=1)):
    with database() as cursor:
        kb = service.require(cursor, kb_id, accounts.current().id, "owner", True)
        require_revision(kb, revision)
        cursor.execute("UPDATE knowledge_bases SET status='deleted',revision=revision+1,updated_at=%s WHERE id=%s", (store.now(), kb_id))
        cursor.execute("UPDATE knowledge_documents SET status='deleted',revision=revision+1,updated_at=%s WHERE kb_id=%s", (store.now(), kb_id))
    runtime.wake()
    return {"ok": True}


@router.get("/bases/{kb_id}/members")
def members(kb_id: str):
    with database() as cursor:
        kb = service.require(cursor, kb_id, accounts.current().id)
        cursor.execute("SELECT m.user_id,u.username,m.role FROM knowledge_members m JOIN users u ON u.id=m.user_id WHERE m.kb_id=%s", (kb_id,))
        return {"revision": kb["revision"], "owner_id": str(kb["owner_id"]), "items": [r | {"user_id": str(r["user_id"])} for r in cursor.fetchall()]}


@router.put("/bases/{kb_id}/members")
def set_member(kb_id: str, payload: MemberInput):
    with database() as cursor:
        kb = service.require(cursor, kb_id, accounts.current().id, "owner", True)
        require_revision(kb, payload.revision)
        uid = int(payload.user_id)
        if uid == kb["owner_id"]:
            raise HTTPException(409, "请通过所有权转移调整所有者")
        cursor.execute("SELECT id FROM users WHERE id=%s AND status='active'", (uid,))
        if not cursor.fetchone():
            raise HTTPException(422, "成员账号不存在或已停用")
        cursor.execute("INSERT INTO knowledge_members (kb_id,user_id,role,created_at,updated_at) VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE role=VALUES(role),updated_at=VALUES(updated_at)", (kb_id, uid, payload.role, store.now(), store.now()))
        cursor.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=%s WHERE id=%s", (store.now(), kb_id))
    return {"revision": payload.revision + 1}


@router.delete("/bases/{kb_id}/members")
def remove_member(kb_id: str, user_id: str, revision: int = Query(ge=1)):
    if not user_id.isdigit():
        raise HTTPException(422, "成员 ID 无效")
    with database() as cursor:
        kb = service.require(cursor, kb_id, accounts.current().id, "owner", True)
        require_revision(kb, revision)
        if str(kb["owner_id"]) == user_id:
            raise HTTPException(409, "不能移除知识库所有者")
        cursor.execute("DELETE FROM knowledge_members WHERE kb_id=%s AND user_id=%s", (kb_id, int(user_id)))
        cursor.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=%s WHERE id=%s", (store.now(), kb_id))
    return {"revision": revision + 1}


@router.get("/bases/{kb_id}/documents")
def documents(kb_id: str):
    with database() as cursor:
        service.require(cursor, kb_id, accounts.current().id)
        cursor.execute("SELECT * FROM knowledge_documents WHERE kb_id=%s AND status<>'deleted' ORDER BY updated_at DESC", (kb_id,))
        rows = cursor.fetchall()
        for row in rows:
            row["uploaded_by"] = str(row["uploaded_by"])
            cursor.execute("SELECT version,source_kind,source_locator,embedding_model,embedding_dimensions,status,error,chunk_count,published_at FROM knowledge_document_versions WHERE document_id=%s ORDER BY version DESC", (row["id"],))
            row["versions"] = cursor.fetchall()
        return {"items": rows}


@router.post("/bases/{kb_id}/documents", status_code=202)
def upload(kb_id: str, file: UploadFile, idempotency_key: str = Header(min_length=8, max_length=200)):
    with database() as cursor:
        service.require(cursor, kb_id, accounts.current().id, "editor")
    data = file.file.read(20 * 1024 * 1024 + 1)
    kind = Path(file.filename or "").suffix.lower().lstrip(".")
    if kind not in {"pdf", "docx", "md", "txt"}:
        raise HTTPException(422, "支持 PDF、DOCX、Markdown 和 TXT")
    row = service.enqueue_document(accounts.current().id, kb_id, Path(file.filename or "文档").name[:255], data, kind, idempotency_key)
    return {"task_id": row["id"], "document_id": row["request"]["document_id"]}


@router.post("/documents/import", status_code=202)
def import_video(payload: VideoImport, idempotency_key: str = Header(min_length=8, max_length=200)):
    resource = accounts.resource("summary", payload.summary_task_id)
    content = json.loads(resource["payload"]) if isinstance(resource["payload"], str) else resource["payload"]
    result = content.get("result") or {}
    if payload.kind == "video_summary":
        segments = [{"text": result.get("summary_markdown", ""), "locator": {"section": "视频摘要"}}]
    else:
        segments = [{"text": s["text"], "locator": {"start_ms": int(s["start"] * 1000), "end_ms": int(s["end"] * 1000)}} for s in content.get("transcript", [])]
    title = str(result.get("title") or "视频内容")[:255]
    row = service.enqueue_document(accounts.current().id, payload.kb_id, title, store.encode({"segments": segments}).encode(), payload.kind,
                                   idempotency_key, source_locator="summary:" + payload.summary_task_id)
    return {"task_id": row["id"], "document_id": row["request"]["document_id"]}


@router.post("/documents/{document_id}/reindex", status_code=202)
def reindex(document_id: str, revision: int = Query(ge=0), idempotency_key: str = Header(min_length=8, max_length=200)):
    model = config.model_config("embedding")
    index = storage.ensure_index(model["dimensions"])
    with database() as cursor:
        uid = accounts.current().id
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        doc = service.document(cursor, document_id, accounts.current().id, "editor", True)
        cursor.execute("SELECT * FROM agent_tasks WHERE user_id=%s AND idempotency_key=%s FOR UPDATE", (uid, store.digest(idempotency_key)))
        existing = store.decode(cursor.fetchone())
        if existing:
            if existing["kind"] != "knowledge_ingest" or existing["request"].get("document_id") != document_id or existing["request"].get("input_revision") != revision:
                raise HTTPException(409, "幂等键已用于其他请求")
            return {"task_id": existing["id"]}
        if doc["revision"] != revision or doc["status"] == "indexing":
            raise HTTPException(409, "文档版本已变化或正在构建")
        cursor.execute("SELECT * FROM knowledge_document_versions WHERE document_id=%s AND version=%s", (document_id, doc["target_version"]))
        old = cursor.fetchone()
        version = doc["target_version"] + 1
        row, created = store.create(cursor, accounts.current().id, "knowledge_ingest", {"document_id": document_id, "version": version, "kb_id": doc["kb_id"], "input_revision": revision}, idempotency_key)
        if created:
            service.insert_version(cursor, document_id, version, old["source_kind"], old["source_locator"], old["source_key"], old["sha256"], model, index)
            cursor.execute("UPDATE knowledge_documents SET target_version=%s,status='indexing',revision=revision+1,updated_at=%s WHERE id=%s", (version, store.now(), document_id))
    runtime.wake()
    return {"task_id": row["id"]}


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, revision: int = Query(ge=0)):
    with database() as cursor:
        doc = service.document(cursor, document_id, accounts.current().id, "editor", True)
        if doc["revision"] != revision:
            raise HTTPException(409, "文档版本已变化")
        cursor.execute("UPDATE knowledge_documents SET status='deleted',revision=revision+1,updated_at=%s WHERE id=%s", (store.now(), document_id))
    runtime.wake()
    return {"ok": True}


@router.get("/documents/{document_id}/versions/{version}/source")
def source(document_id: str, version: int):
    uid = accounts.current().id
    with database() as cursor:
        service.document(cursor, document_id, uid)
        cursor.execute("SELECT source_key,source_kind FROM knowledge_document_versions WHERE document_id=%s AND version=%s AND status='published'", (document_id, version))
        item = cursor.fetchone()
        if not item:
            raise HTTPException(404, "来源版本不可用")
    data = storage.read(item["source_key"])
    with database() as cursor:
        service.document(cursor, document_id, uid)  # Recheck after remote object read.
    return Response(data, media_type={"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                     "md": "text/markdown", "txt": "text/plain"}.get(item["source_kind"], "application/json"),
                    headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="source.{item["source_kind"]}"'})


@router.post("/queries", status_code=202)
def query(payload: QueryRequest, idempotency_key: str = Header(min_length=8, max_length=200)):
    with database() as cursor:
        ids = service.allowed(cursor, accounts.current().id, payload.kb_ids)
        if not ids:
            raise HTTPException(422, "请先创建并选择知识库")
        row, _ = store.create(cursor, accounts.current().id, "knowledge_query", payload.model_dump() | {"kb_ids": ids}, idempotency_key)
    runtime.wake()
    return {"task_id": row["id"]}
