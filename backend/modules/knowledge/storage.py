from __future__ import annotations

import io
from contextlib import contextmanager
from urllib.parse import urlparse

from elasticsearch import Elasticsearch
from minio import Minio
import pymysql
from fastapi import HTTPException

from backend.modules.accounts.service import environment


@contextmanager
def document_lock(document_id):
    from backend.infrastructure.database import database_config
    key = "videodream:knowledge:" + document_id
    connection = pymysql.connect(**(database_config() | {"autocommit": True}))
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (key,))
            acquired = bool(cursor.fetchone()["acquired"])
        if not acquired:
            raise HTTPException(409, "文档正在处理，请稍后重试")
        yield
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (key,))
        connection.close()


def index_name():
    name = environment("VIDEODREAM_RAG_INDEX", "videodream_rag_chunks_v1")
    if not name.startswith("videodream_"):
        raise ValueError("Knowledge indexes must use the videodream_ prefix")
    return name


def search_client():
    url = environment("VIDEODREAM_ES_URL")
    if not url:
        raise HTTPException(503, "请配置 VIDEODREAM_ES_URL")
    username, password = environment("VIDEODREAM_ES_USER"), environment("VIDEODREAM_ES_PASSWORD")
    return Elasticsearch(url, request_timeout=20, max_retries=0,
                         **({"basic_auth": (username, password)} if username else {}))


def ensure_index(dimensions):
    index = index_name()
    with search_client() as es:
        if es.indices.exists(index=index):
            mapping = es.indices.get_mapping(index=index)[index]["mappings"]["properties"]
            if mapping["embedding"]["dims"] != dimensions:
                raise HTTPException(409, "Embedding 维度与 ES 索引不一致，请新建项目索引并重建版本")
            return index
        es.indices.create(index=index, mappings={"dynamic": "strict", "properties": {
            **{k: {"type": "keyword"} for k in ("chunk_id", "kb_id", "document_id", "content_hash", "pipeline_hash")},
            "version": {"type": "integer"}, "chunk_no": {"type": "integer"},
            "content": {"type": "text", "analyzer": environment("VIDEODREAM_RAG_ANALYZER", "ik_max_word")},
            "embedding": {"type": "dense_vector", "dims": dimensions, "index": True, "similarity": "cosine"},
            "locator": {"properties": {**{k: {"type": "integer"} for k in ("page", "offset_start", "offset_end")},
                                       **{k: {"type": "long"} for k in ("start_ms", "end_ms")}, "section": {"type": "keyword"}}}}})
    return index


def object_client():
    endpoint = environment("VIDEODREAM_MINIO_ENDPOINT")
    access, secret = environment("VIDEODREAM_MINIO_ACCESS_KEY"), environment("VIDEODREAM_MINIO_SECRET_KEY")
    if not endpoint or not access or not secret:
        raise HTTPException(503, "请配置项目受限的 MinIO 连接和凭据")
    parsed = urlparse(endpoint if "://" in endpoint else "http://" + endpoint)
    return Minio(parsed.netloc, access_key=access, secret_key=secret, secure=parsed.scheme == "https")


def bucket():
    name = environment("VIDEODREAM_MINIO_BUCKET", "videodream")
    if not (name == "videodream" or name.startswith("videodream-")):
        raise ValueError("Knowledge bucket must belong to VideoDream")
    return name


def put(key, data, content_type="application/octet-stream"):
    object_client().put_object(bucket(), key, io.BytesIO(data), len(data), content_type=content_type)


def read(key):
    response = object_client().get_object(bucket(), key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def remove_document(kb_id, document_id):
    with document_lock(document_id):
        _remove_document(kb_id, document_id)


def _remove_document(kb_id, document_id):
    with search_client() as es:
        es.delete_by_query(index="videodream_rag_*", query={"bool": {"filter": [
            {"term": {"kb_id": kb_id}}, {"term": {"document_id": document_id}}]}}, refresh=True, conflicts="proceed", ignore_unavailable=True)
    client = object_client()
    for item in client.list_objects(bucket(), prefix=f"knowledge/{kb_id}/{document_id}/", recursive=True):
        client.remove_object(bucket(), item.object_name)
