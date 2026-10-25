"""Administrator review of user-submitted evidence and content withdrawal."""
import time
from typing import Literal
from fastapi import APIRouter
from pydantic import Field
from backend.modules.accounts import service as accounts
from backend.modules.social import store as s, rtc
from backend.modules.social.models import Input

router = APIRouter(prefix="/api/admin/social", tags=["administration"])


class Resolution(Input):
    action: Literal["dismiss", "hide"]
    reason: str = Field(min_length=1, max_length=500)


@router.get("/reports")
def reports():
    accounts.admin()
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_reports ORDER BY (status='pending') DESC,created_at DESC LIMIT 100")
        return {"items": [s.decode(x, "evidence") for x in cursor.fetchall()]}


@router.post("/reports/{key}")
def resolve(key: str, payload: Resolution):
    accounts.admin()
    now = time.time()
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_reports WHERE id=%s FOR UPDATE", (key,))
        row = s.decode(cursor.fetchone())
        if payload.action == "hide":
            kind, target = row["target_kind"], row["target_key"]
            if kind == "message":
                conversation, seq = target.split(":", 1)
                cursor.execute("UPDATE social_messages SET status='recalled',body='{}',recalled_at=%s WHERE conversation_id=%s AND seq=%s", (now, conversation, int(seq)))
                s.event(cursor, "recall", conversation, int(seq), s.recipients(cursor, conversation))
            elif kind == "share":
                cursor.execute("UPDATE social_shares SET revoked_at=%s WHERE id=%s", (now, target))
            elif kind == "rule":
                cursor.execute("UPDATE social_rules SET status='withdrawn' WHERE id=%s", (target,))
            elif kind == "performance":
                cursor.execute("UPDATE social_performances SET status='withdrawn',visibility='private',revision=revision+1 WHERE id=%s", (target,))
                cursor.execute("UPDATE social_shares SET revoked_at=%s WHERE target_kind='performance' AND source_key=%s", (now, target))
            elif kind == "room":
                cursor.execute("SELECT conversation_id,kind FROM social_rooms WHERE id=%s FOR UPDATE", (target,))
                room = s.decode(cursor.fetchone())
                if room["kind"] != "listen" and rtc.available():
                    rtc.delete_room(target)
                cursor.execute("UPDATE social_rooms SET status='closed',revision=revision+1,closed_at=%s WHERE id=%s", (now, target))
                cursor.execute("UPDATE social_conversations SET status='closed' WHERE id=%s", (room["conversation_id"],))
                s.event(cursor, "room", target, 0, s.recipients(cursor, room["conversation_id"]))
        cursor.execute("UPDATE social_reports SET status='resolved',handler_id=%s,resolution=%s,resolved_at=%s WHERE id=%s", (s.uid(), (payload.action + ": " + payload.reason)[:500], now, key))
    accounts.audit("social_report_" + payload.action, key)
    return {"ok": True}
