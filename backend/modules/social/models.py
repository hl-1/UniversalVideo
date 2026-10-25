from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RelationInput(Input):
    action: Literal["request", "accept", "reject", "remove", "block", "unblock", "remark"]
    message: str = Field(default="", max_length=200)
    remark: str = Field(default="", max_length=40)


class ConversationInput(Input):
    kind: Literal["direct", "group"] = "direct"
    title: str = Field(default="", max_length=80)
    members: list[int] = Field(min_length=1, max_length=99)


class ConversationUpdate(Input):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    announcement: str | None = Field(default=None, max_length=2000)
    pinned: bool | None = None
    muted: bool | None = None
    clear: bool = False


class MemberInput(Input):
    action: Literal["invite", "accept", "reject", "leave", "remove", "admin", "member", "transfer"]
    user_id: int | None = Field(default=None, gt=0)


class MessageInput(Input):
    client_msg_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    msg_type: Literal["text", "image", "voice", "file", "share"] = "text"
    text: str = Field(default="", max_length=8000)
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    share_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    reply_to_seq: int | None = Field(default=None, gt=0)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value):
        return value.strip()


class CursorInput(Input):
    seq: int = Field(ge=0)


class UserSettings(Input):
    discoverable: bool = True
    online_visibility: Literal["friends", "nobody"] = "friends"
    listening_visibility: Literal["friends", "nobody"] = "friends"
    dm_policy: Literal["friends"] = "friends"
    invite_policy: Literal["friends", "nobody"] = "friends"
