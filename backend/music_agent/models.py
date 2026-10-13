from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.library import TrackInput


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vocals: Literal["allowed", "instrumental"] = "allowed"
    languages: list[str] = Field(default_factory=list, max_length=8)
    exclusions: list[str] = Field(default_factory=list, max_length=20)
    target_count: int = Field(default=20, ge=1, le=50)
    target_duration_minutes: int = Field(default=60, ge=1, le=300)
    source_mode: Literal["personal_first", "personal_only", "discover"] = "personal_first"
    network_allowed: bool = True
    goal: Literal["count", "duration"] = "count"
    full_playback_required: bool = True

    @field_validator("languages", "exclusions")
    @classmethod
    def clean_terms(cls, values):
        if any(not isinstance(v, str) or len(v) > 100 for v in values):
            raise ValueError("偏好项不能超过 100 字")
        return list(dict.fromkeys(v.strip() for v in values if v.strip()))


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    need: str = Field(min_length=1, max_length=1000)
    preferences: dict = Field(default_factory=dict)

    @field_validator("need")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请输入听歌需求")
        return value.strip()

    @field_validator("preferences")
    @classmethod
    def validate_preferences(cls, value):
        Preferences.model_validate(value)
        return value


def normalize_scene(value):
    aliases = {"游戏": ("游戏", "gaming", "game"), "睡眠": ("睡眠", "睡觉", "助眠"),
               "运动": ("运动", "跑步", "健身"), "专注": ("专注", "学习", "阅读"),
               "工作": ("工作", "办公"), "放松": ("放松", "休闲"), "情歌": ("情歌",), "励志": ("励志",)}
    value = str(value).strip()
    for scene, terms in aliases.items():
        if any(term in value.casefold() for term in terms):
            return scene
    return value


class ParsedNeed(Preferences):
    scene: str = Field(default="放松", max_length=80)

    @field_validator("scene")
    @classmethod
    def canonical_scene(cls, value):
        return normalize_scene(value)

    mood: str = Field(default="", max_length=80)
    effect: str = Field(default="", max_length=80)
    themes: list[str] = Field(default_factory=list, max_length=10)
    excluded_themes: list[str] = Field(default_factory=list, max_length=10)
    query: str = Field(default="", max_length=100)
    title: str = Field(default="我的场景歌单", min_length=1, max_length=80)
    clarification: str = Field(default="", max_length=300)


class ContinueRun(BaseModel):
    need: str = Field(default="", max_length=1000)
    preferences: dict = Field(default_factory=dict)

    @field_validator("preferences")
    @classmethod
    def validate_preferences(cls, value):
        Preferences.model_validate(value)
        return value


class SaveRun(BaseModel):
    result_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)


class ResultSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["personal", "online_playlist", "online_search"]
    label: str = Field(max_length=300)
    provider: str | None = Field(default=None, max_length=12)
    playlist_id: str | None = Field(default=None, max_length=24)


class ResultAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: dict = Field(default_factory=dict, max_length=16)
    evidence: dict = Field(default_factory=dict, max_length=16)
    checked_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cached: bool = False

    @field_validator("features", "evidence")
    @classmethod
    def bounded_evidence(cls, value):
        import json
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 16384:
            raise ValueError("Feature evidence exceeds 16 KiB")
        return value


class ResultValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_at: float = Field(ge=0, allow_inf_nan=False)
    playable: bool
    full: bool
    identity: bool
    reason: str = Field(default="", max_length=300)
    duration: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    source: str | None = Field(default=None, max_length=300)


class ResultMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qualified: bool
    reasons: list[str] = Field(default_factory=list, max_length=20)
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    scoring_version: str = Field(default="legacy", max_length=40)
    breakdown: dict[str, float] = Field(default_factory=dict, max_length=4)


class ResultTrack(TrackInput):
    key: str = Field(min_length=1, max_length=64)
    id: str | None = Field(default=None, max_length=64)
    filename: str | None = Field(default=None, max_length=255)
    local_url: str | None = Field(default=None, max_length=2048)
    source: ResultSource
    analysis: ResultAnalysis = Field(default_factory=ResultAnalysis)
    validation: ResultValidation
    match: ResultMatch
    reason: str = Field(max_length=600)
    unknown_features: list[str] = Field(default_factory=list, max_length=16)


class ResultGap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=0, le=50)
    seconds: int = Field(ge=0, le=18000)
    primary: Literal["count", "duration"]


class RejectedTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=64)
    title: str = Field(max_length=255)
    artist: str = Field(max_length=255)
    reasons: list[str] = Field(max_length=20)


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    tracks: list[ResultTrack] = Field(max_length=50)
    duration_seconds: int = Field(ge=0, le=4320000)
    gap: ResultGap
    coverage: dict = Field(default_factory=dict, max_length=20)
    rejected: list[RejectedTrack] = Field(default_factory=list, max_length=100)
    notice: str = Field(max_length=300)
    budget: dict[str, float] = Field(default_factory=dict, max_length=16)


TERMINAL = {"finished", "partial", "failed", "cancelled"}
RESUMABLE = {"waiting_input", "waiting_auth", "interrupted"}
ACTIVE = {"pending", "running"}
MAX_SNAPSHOT_BYTES = 512 * 1024
POLICY_VERSION = "scene-playlist-v1"
