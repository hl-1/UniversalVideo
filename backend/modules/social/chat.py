from __future__ import annotations

import time
from fastapi import HTTPException
from backend.modules.social import store as s


def user_settings(cursor, user_id):
    cursor.execute("SELECT * FROM social_user_settings WHERE user_id=%s", (user_id,))
    return cursor.fetchone() or dict(user_id=user_id, discoverable=True, online_visibility="friends",
        listening_visibility="friends", dm_policy="friends", invite_policy="friends")


def settings(payload=None):
    with s.database() as cursor:
        if payload is not None:
            now = time.time()
            data = payload.model_dump()
            cursor.execute("""INSERT INTO social_user_settings
                (user_id,discoverable,online_visibility,listening_visibility,dm_policy,invite_policy,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                discoverable=VALUES(discoverable),online_visibility=VALUES(online_visibility),
                listening_visibility=VALUES(listening_visibility),invite_policy=VALUES(invite_policy),updated_at=VALUES(updated_at)""",
                (s.uid(), *data.values(), now, now))
        return user_settings(cursor, s.uid())


def users(query):
    if len(query.strip()) < 2:
        return {"items": []}
    with s.database() as cursor:
        # Prefix matching is escaped; blocked users are never discoverable.
        term = query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        cursor.execute("""SELECT u.id,u.username FROM users u LEFT JOIN social_user_settings t ON t.user_id=u.id
            WHERE u.status='active' AND u.id<>%s AND COALESCE(t.discoverable,TRUE)=TRUE
            AND u.username LIKE %s ESCAPE '!' AND NOT EXISTS
            (SELECT 1 FROM social_relations r WHERE r.kind='block' AND r.status='active'
             AND ((r.user_id=u.id AND r.target_user_id=%s) OR (r.target_user_id=u.id AND r.user_id=%s)))
            ORDER BY u.username LIMIT 30""", (s.uid(), term, s.uid(), s.uid()))
        return {"items": cursor.fetchall()}


def relations():
    with s.database() as cursor:
        cursor.execute("""SELECT r.*,u.username FROM social_relations r JOIN users u ON u.id=IF(r.user_id=%s,r.target_user_id,r.user_id)
            WHERE r.user_id=%s OR (r.target_user_id=%s AND r.kind='friend' AND r.status='pending')
            ORDER BY r.updated_at DESC LIMIT 500""", (s.uid(), s.uid(), s.uid()))
        return {"items": cursor.fetchall()}


def relation(target, payload):
    s.rate("relation", 30)
    if target == s.uid():
        raise HTTPException(422, "不能操作自己。")
    with s.database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id IN (%s,%s) ORDER BY id FOR UPDATE", (s.uid(), target))
        s.active_user(cursor, target)
        now = time.time()
        action = payload.action
        if action in {"request", "accept"} and s.blocked(cursor, s.uid(), target):
            raise HTTPException(403, "对方暂不可联系。")
        if action == "request":
            if not user_settings(cursor, target)["discoverable"]:
                raise HTTPException(404, "用户不可用。")
            cursor.execute("SELECT status FROM social_relations WHERE user_id=%s AND target_user_id=%s AND kind='friend'", (s.uid(), target))
            old = cursor.fetchone()
            if old and old["status"] in {"pending", "accepted"}:
                return {"ok": True}
            cursor.execute("""INSERT INTO social_relations (user_id,target_user_id,kind,status,request_message,created_at,updated_at)
                VALUES (%s,%s,'friend','pending',%s,%s,%s) ON DUPLICATE KEY UPDATE
                status='pending',request_message=VALUES(request_message),updated_at=VALUES(updated_at)""",
                (s.uid(), target, payload.message, now, now))
            s.notify(cursor, target, "friend_request", str(s.uid()), {"from": s.uid(), "message": payload.message})
        elif action in {"accept", "reject"}:
            cursor.execute("SELECT status FROM social_relations WHERE user_id=%s AND target_user_id=%s AND kind='friend' FOR UPDATE", (target, s.uid()))
            if (cursor.fetchone() or {}).get("status") != "pending":
                raise HTTPException(409, "该好友申请已处理。")
            cursor.execute("UPDATE social_relations SET status=%s,updated_at=%s WHERE user_id=%s AND target_user_id=%s AND kind='friend'",
                ("accepted" if action == "accept" else "rejected", now, target, s.uid()))
            if action == "accept":
                cursor.execute("""INSERT INTO social_relations (user_id,target_user_id,kind,status,created_at,updated_at)
                    VALUES (%s,%s,'friend','accepted',%s,%s) ON DUPLICATE KEY UPDATE status='accepted',updated_at=VALUES(updated_at)""",
                    (s.uid(), target, now, now))
            s.notify(cursor, target, "friend_" + action, str(s.uid()), {"from": s.uid()})
        elif action in {"remove", "block"}:
            cursor.execute("DELETE FROM social_relations WHERE kind='friend' AND ((user_id=%s AND target_user_id=%s) OR (user_id=%s AND target_user_id=%s))", (s.uid(), target, target, s.uid()))
            if action == "block":
                cursor.execute("""INSERT INTO social_relations (user_id,target_user_id,kind,status,created_at,updated_at)
                    VALUES (%s,%s,'block','active',%s,%s) ON DUPLICATE KEY UPDATE status='active',updated_at=VALUES(updated_at)""", (s.uid(), target, now, now))
        elif action == "unblock":
            cursor.execute("DELETE FROM social_relations WHERE user_id=%s AND target_user_id=%s AND kind='block'", (s.uid(), target))
        elif action == "remark":
            cursor.execute("UPDATE social_relations SET remark=%s,updated_at=%s WHERE user_id=%s AND target_user_id=%s AND kind='friend' AND status='accepted'", (payload.remark, now, s.uid(), target))
        s.event(cursor, "relations", str(s.uid()), 0, [s.uid(), target])
    return {"ok": True}


def create_conversation(cursor, kind, title, owner, members, direct_key=None):
    key, now = s.identifier(), time.time()
    cursor.execute("""INSERT INTO social_conversations (id,kind,direct_key,owner_id,title,settings,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,'{}',%s,%s)""", (key, kind, direct_key, owner, title, now, now))
    for user_id, role, status in members:
        cursor.execute("""INSERT INTO social_conversation_members
            (conversation_id,user_id,role,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)""", (key, user_id, role, status, now, now))
    return key


def create(payload):
    s.rate("conversation", 20)
    targets = list(dict.fromkeys(x for x in payload.members if x != s.uid()))
    if not targets or payload.kind == "direct" and len(targets) != 1:
        raise HTTPException(422, "请选择有效成员。")
    with s.database() as cursor:
        # Lock users in a stable order; concurrent direct creation cannot race.
        users_to_lock = sorted([s.uid(), *targets])
        cursor.execute("SELECT id FROM users WHERE id IN (" + ",".join(["%s"] * len(users_to_lock)) + ") ORDER BY id FOR UPDATE", users_to_lock)
        for target in targets:
            s.require_friend(cursor, s.uid(), target)
        direct = s.digest(":".join(map(str, sorted([s.uid(), targets[0]])))) if payload.kind == "direct" else None
        if direct:
            cursor.execute("SELECT id FROM social_conversations WHERE direct_key=%s", (direct,))
            found = cursor.fetchone()
            if found:
                return found
        members = [(s.uid(), "owner" if not direct else "member", "active")]
        for target in targets:
            if not direct and user_settings(cursor, target)["invite_policy"] == "nobody":
                raise HTTPException(403, "好友已关闭邀请。")
            members.append((target, "member", "active" if direct else "invited"))
        key = create_conversation(cursor, payload.kind, payload.title or ("好友群聊" if not direct else ""), s.uid() if not direct else None, members, direct)
        for target in targets:
            s.notify(cursor, target, "conversation" if direct else "group_invite", key, {"conversation_id": key, "from": s.uid(), "title": payload.title})
        return {"id": key}


def conversations():
    with s.database() as cursor:
        cursor.execute("""SELECT c.*,m.role,m.status AS member_status,m.pinned,m.muted_until,m.last_read_seq,m.hidden_before_seq,
            (SELECT COUNT(*) FROM social_messages x WHERE x.conversation_id=c.id AND x.seq>GREATEST(m.last_read_seq,m.hidden_before_seq,m.joined_seq) AND (x.sender_id IS NULL OR x.sender_id<>%s)) AS unread,
            (SELECT u.username FROM social_conversation_members n JOIN users u ON u.id=n.user_id WHERE n.conversation_id=c.id AND n.user_id<>%s LIMIT 1) AS peer_name
            FROM social_conversations c JOIN social_conversation_members m ON m.conversation_id=c.id
            WHERE m.user_id=%s AND m.status IN ('active','invited') AND c.status='active'
            ORDER BY m.pinned DESC,c.updated_at DESC LIMIT 200""", (s.uid(), s.uid(), s.uid()))
        return {"items": [s.decode(r, "settings") for r in cursor.fetchall()]}


def detail(key):
    with s.database() as cursor:
        row, membership = s.member(cursor, key)
        cursor.execute("""SELECT m.user_id,m.role,m.status,u.username FROM social_conversation_members m
            JOIN users u ON u.id=m.user_id WHERE m.conversation_id=%s ORDER BY m.created_at""", (key,))
        return row | {"membership": membership, "members": cursor.fetchall()}


def update(key, payload):
    with s.database() as cursor:
        row, membership = s.member(cursor, key, lock=True)
        now = time.time()
        if payload.title is not None or payload.announcement is not None:
            if row["kind"] != "group" or membership["role"] not in {"owner", "admin"}:
                raise HTTPException(403, "需要群主或管理员权限。")
            config = row["settings"] | ({"announcement": payload.announcement} if payload.announcement is not None else {})
            cursor.execute("UPDATE social_conversations SET title=%s,settings=%s,revision=revision+1,updated_at=%s WHERE id=%s", (payload.title or row["title"], s.encode(config), now, key))
        cursor.execute("""UPDATE social_conversation_members SET pinned=%s,muted_until=%s,
            hidden_before_seq=%s,last_read_seq=GREATEST(last_read_seq,%s),updated_at=%s WHERE conversation_id=%s AND user_id=%s""",
            (membership["pinned"] if payload.pinned is None else payload.pinned,
             membership["muted_until"] if payload.muted is None else (32503680000 if payload.muted else None),
             row["last_seq"] if payload.clear else membership["hidden_before_seq"], row["last_seq"] if payload.clear else 0, now, key, s.uid()))
        s.event(cursor, "conversation", key, row["revision"] + 1, s.recipients(cursor, key))
    return {"ok": True}


def change_member(key, payload):
    target, now = payload.user_id or s.uid(), time.time()
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_conversations WHERE id=%s FOR UPDATE", (key,))
        room = s.decode(cursor.fetchone())
        if room["kind"] != "group" or room["status"] != "active":
            raise HTTPException(403, "此会话不支持成员操作。")
        cursor.execute("SELECT * FROM social_conversation_members WHERE conversation_id=%s AND user_id=%s", (key, s.uid()))
        actor = s.decode(cursor.fetchone())
        action = payload.action
        if action in {"accept", "reject"}:
            if target != s.uid() or actor["status"] != "invited":
                raise HTTPException(409, "邀请已处理。")
            if room["owner_id"] is None or s.blocked(cursor, s.uid(), room["owner_id"]):
                raise HTTPException(403, "邀请已失效。")
            cursor.execute("UPDATE social_conversation_members SET status=%s,joined_seq=%s,last_read_seq=%s,updated_at=%s WHERE conversation_id=%s AND user_id=%s", ("active" if action == "accept" else "rejected", room["last_seq"], room["last_seq"], now, key, target))
        else:
            if actor["status"] != "active":
                raise HTTPException(403, "请先加入群聊。")
            if action == "leave":
                target = s.uid()
            elif actor["role"] not in {"owner", "admin"}:
                raise HTTPException(403, "需要管理权限。")
            cursor.execute("SELECT * FROM social_conversation_members WHERE conversation_id=%s AND user_id=%s", (key, target))
            existing = cursor.fetchone()
            if action == "invite":
                s.require_friend(cursor, s.uid(), target)
                if user_settings(cursor, target)["invite_policy"] == "nobody":
                    raise HTTPException(403, "好友已关闭邀请。")
                cursor.execute("SELECT COUNT(*) AS n FROM social_conversation_members WHERE conversation_id=%s AND status IN ('active','invited')", (key,))
                if cursor.fetchone()["n"] >= 100 or existing and existing["status"] in {"active", "invited"}:
                    raise HTTPException(409, "成员已加入、已获邀请或群人数已满。")
                cursor.execute("""INSERT INTO social_conversation_members (conversation_id,user_id,role,status,created_at,updated_at)
                    VALUES (%s,%s,'member','invited',%s,%s) ON DUPLICATE KEY UPDATE status='invited',role='member',updated_at=VALUES(updated_at)""", (key, target, now, now))
                s.notify(cursor, target, "group_invite", key, {"conversation_id": key, "title": room["title"]})
            elif action in {"leave", "remove"}:
                if not existing or existing["role"] == "owner" or existing["role"] == "admin" and actor["role"] != "owner" and action != "leave":
                    raise HTTPException(409, "群主须先转让群；管理员只能由群主管理。")
                cursor.execute("UPDATE social_conversation_members SET status='left',left_seq=%s,updated_at=%s WHERE conversation_id=%s AND user_id=%s", (room["last_seq"], now, key, target))
            else:
                if actor["role"] != "owner" or not existing or existing["status"] != "active" or target == s.uid():
                    raise HTTPException(403, "仅群主可调整其他有效成员的身份。")
                role = "owner" if action == "transfer" else action
                cursor.execute("UPDATE social_conversation_members SET role=%s WHERE conversation_id=%s AND user_id=%s", (role, key, target))
                if action == "transfer":
                    cursor.execute("UPDATE social_conversation_members SET role='member' WHERE conversation_id=%s AND user_id=%s", (key, s.uid()))
                    cursor.execute("UPDATE social_conversations SET owner_id=%s WHERE id=%s", (target, key))
        cursor.execute("UPDATE social_conversations SET revision=revision+1,updated_at=%s WHERE id=%s", (now, key))
        s.event(cursor, "conversation", key, room["revision"] + 1, [*s.recipients(cursor, key), target])
    return {"ok": True}


def message_public(row):
    row = s.decode(row, "body")
    if row["status"] == "recalled":
        row["body"] = {}
    return row


def messages(key, after=0, before=0, query="", limit=50):
    with s.database() as cursor:
        row, membership = s.member(cursor, key)
        floor = max(membership["hidden_before_seq"], membership["joined_seq"], after)
        sql = """SELECT m.*,u.username AS sender_name FROM social_messages m LEFT JOIN users u ON u.id=m.sender_id
            WHERE conversation_id=%s AND seq>%s"""
        args = [key, floor]
        if before:
            sql += " AND seq<%s"
            args.append(before)
        if query:
            sql += " AND status='sent' AND JSON_UNQUOTE(JSON_EXTRACT(body,'$.text')) LIKE %s ESCAPE '!'"
            args.append("%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
        sql += " ORDER BY seq " + ("ASC" if after else "DESC") + " LIMIT %s"
        args.append(limit)
        cursor.execute(sql, args)
        items = [message_public(r) for r in cursor.fetchall()]
        items.sort(key=lambda x: x["seq"])
        return {"items": items, "last_seq": row["last_seq"], "has_more": len(items) == limit}


def send(key, payload):
    s.rate("message", 120)
    with s.database() as cursor:
        # Use the same account locks as relation changes before establishing a read snapshot.
        cursor.execute("SELECT u.id FROM users u JOIN social_conversation_members m ON m.user_id=u.id WHERE m.conversation_id=%s ORDER BY u.id FOR UPDATE", (key,))
        cursor.fetchall()
        room, membership = s.member(cursor, key, lock=True)
        if room["kind"] == "direct":
            other = next((x for x in s.recipients(cursor, key) if x != s.uid()), None)
            if other is None:
                raise HTTPException(403, "对方暂不可联系。")
            s.require_friend(cursor, s.uid(), other)
        cursor.execute("SELECT * FROM social_messages WHERE conversation_id=%s AND client_msg_id=%s", (key, payload.client_msg_id))
        old = cursor.fetchone()
        if old:
            if old["sender_id"] != s.uid():
                raise HTTPException(409, "消息标识冲突，请重新发送。")
            return message_public(old)
        body = {"text": payload.text}
        if payload.msg_type == "text" and not payload.text:
            raise HTTPException(422, "消息不能为空。")
        if payload.reply_to_seq:
            cursor.execute("SELECT seq FROM social_messages WHERE conversation_id=%s AND seq=%s AND seq>%s AND status='sent'", (key, payload.reply_to_seq, max(membership["joined_seq"], membership["hidden_before_seq"])))
            if not cursor.fetchone():
                raise HTTPException(422, "回复的消息不可用。")
        if payload.msg_type in {"image", "voice", "file"}:
            cursor.execute("SELECT * FROM social_assets WHERE id=%s AND owner_id=%s AND state='ready'", (payload.asset_id, s.uid()))
            asset = s.decode(cursor.fetchone(), "metadata")
            if asset["kind"] != payload.msg_type:
                raise HTTPException(422, "附件类型不匹配。")
            body.update(asset_id=asset["id"], filename=asset["metadata"].get("filename", "附件"), mime=asset["mime"], duration_ms=asset["duration_ms"])
        if payload.msg_type == "share":
            from backend.modules.social import shares
            share = shares.require(cursor, payload.share_id, conversation_id=key)
            shares.check_forward(cursor, share, key)
            body.update(share_id=share["id"], title=share["snapshot"].get("title", "音乐分享"), target_kind=share["target_kind"])
        seq, now = room["last_seq"] + 1, time.time()
        cursor.execute("""INSERT INTO social_messages (conversation_id,seq,sender_id,client_msg_id,msg_type,body,reply_to_seq,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (key, seq, s.uid(), payload.client_msg_id, payload.msg_type, s.encode(body), payload.reply_to_seq, now))
        if payload.msg_type in {"image", "voice", "file"}:
            cursor.execute("INSERT INTO social_message_assets (conversation_id,seq,asset_id) VALUES (%s,%s,%s)", (key, seq, payload.asset_id))
            cursor.execute("UPDATE social_assets SET expires_at=NULL WHERE id=%s", (payload.asset_id,))
        cursor.execute("UPDATE social_conversations SET last_seq=%s,updated_at=%s,revision=revision+1 WHERE id=%s", (seq, now, key))
        s.event(cursor, "message", key, seq, s.recipients(cursor, key))
        cursor.execute("SELECT * FROM social_messages WHERE conversation_id=%s AND seq=%s", (key, seq))
        return message_public(cursor.fetchone())


def recall(key, seq):
    config = s.enabled()
    with s.database() as cursor:
        s.member(cursor, key, lock=True)
        cursor.execute("SELECT * FROM social_messages WHERE conversation_id=%s AND seq=%s FOR UPDATE", (key, seq))
        message = s.decode(cursor.fetchone())
        if message["sender_id"] != s.uid() or time.time() - message["created_at"] > config.get("social_recall_seconds", 120):
            raise HTTPException(403, "只能在撤回时间内撤回自己的消息。")
        cursor.execute("UPDATE social_messages SET status='recalled',recalled_at=%s,body='{}' WHERE conversation_id=%s AND seq=%s", (time.time(), key, seq))
        s.event(cursor, "recall", key, seq, s.recipients(cursor, key))
    return {"ok": True}


def cursor_update(key, seq):
    with s.database() as cursor:
        row, _ = s.member(cursor, key, lock=True)
        seq = min(row["last_seq"], seq)
        cursor.execute("""UPDATE social_conversation_members SET last_read_seq=GREATEST(last_read_seq,%s),
            last_delivered_seq=GREATEST(last_delivered_seq,%s),updated_at=%s WHERE conversation_id=%s AND user_id=%s""", (seq, seq, time.time(), key, s.uid()))
        s.event(cursor, "read", key, seq, s.recipients(cursor, key), {"user_id": s.uid()})
    return {"seq": seq}
