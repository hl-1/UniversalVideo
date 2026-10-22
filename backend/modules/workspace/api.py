from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Query

from backend.modules.accounts import service as accounts
from backend.infrastructure import platform_access
from backend.modules.video import store as video_store
from backend.modules.admin.api import ResourceKind, remove_file


router = APIRouter(prefix="/api/workspace", tags=["personal-workspace"])


@router.get("/resources")
def resources(kind: ResourceKind = "file", page: int = Query(default=1, ge=1), q: str = Query(default="", max_length=100)):
    uid = accounts.current().id
    with accounts.database() as cursor:
        args = (uid, kind, "%" + q + "%")
        where = " WHERE user_id=%s AND kind=%s AND deleted=FALSE AND resource_key LIKE %s"
        if kind in {"delivery", "download"}:
            where += " AND device_id=%s"
            args += (accounts.current().device_id or "",)
        cursor.execute("SELECT COUNT(*) AS n FROM resources" + where, args)
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT * FROM resources" + where + " ORDER BY created_at DESC" + (" LIMIT 20 OFFSET %s" if accounts.current().role == "super_admin" else ""), args + (((page - 1) * 20,) if accounts.current().role == "super_admin" else ()))
        items = [video_store.hydrate(cursor, row) for row in cursor.fetchall()]
    if accounts.current().role != "super_admin":
        items = [row for row in items if platform_access.visible(row)]
        total = len(items)
        items = items[(page - 1) * 20:page * 20]
    for row in items:
        row["payload"] = json.loads(row["payload"])
        prefix = {"file": "/api/files/", "summary_file": "/api/summary-files/", "image": "/api/library/images/", "thumbnail": "/api/thumbnails/"}.get(kind)
        row["download_url"] = prefix + quote(row["resource_key"], safe="") if prefix else row["payload"].get("download_url")
        if kind == "summary":
            row["download_url"] = (row["payload"].get("result") or {}).get("markdown_url")
    return {"items": items, "total": total}


@router.delete("/resources/{kind}/{key}")
def remove(kind: ResourceKind, key: str):
    if kind == "music_recommendation":
        from backend.modules.music.agent.api import remove_run
        return remove_run(key)
    if kind == "video_parse":
        from backend.modules.video.history import delete_record
        return delete_record(key)
    row = accounts.resource(kind, key)
    accounts.cancel_resources(row["user_id"], key)
    with accounts.database() as cursor:
        video_store.tombstone(cursor, kind, key)
        if kind == "file":
            cursor.execute("DELETE FROM music_downloads WHERE filename=%s", (key,))
    remove_file(kind, key, uid=row["user_id"])
    accounts.audit("data_delete", f"{kind}:{key}")
    return {"ok": True}
