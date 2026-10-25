from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from backend.modules.music.library import TrackInput

Key = str

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

class CreateBatch(Strict):
    need: str = Field(default='', max_length=1000)
    seed_track_keys: list[str] = Field(default_factory=list, max_length=10)
    exploration: Literal['low', 'balanced', 'high'] = 'balanced'

class Settings(Strict):
    languages: list[str] = Field(default_factory=list, max_length=8)
    excluded_artists: list[str] = Field(default_factory=list, max_length=20)
    seed_track_keys: list[str] = Field(default_factory=list, max_length=10)
    long_term_need: str = Field(default='', max_length=1000)
    exploration: Literal['low', 'balanced', 'high'] = 'balanced'
    profile_learning_enabled: bool = True

    @model_validator(mode='after')
    def bounded(self):
        if any(len(x)>100 for x in self.languages+self.excluded_artists+self.seed_track_keys):
            raise ValueError('偏好项过长')
        return self

class Feedback(Strict):
    track_key: str = Field(pattern=r'^[a-f0-9]{64}$')
    scope: Literal['recording', 'work'] = 'recording'
    reason: Literal['dislike', 'fatigue', 'today']
    note: str = Field(default='', max_length=300)

class Event(Strict):
    event_id: UUID
    track: TrackInput | None = None
    track_key: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    batch_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    feed_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    context: Literal['general', 'focus', 'commute', 'exercise', 'sleep'] = 'general'
    position: int | None = Field(default=None, ge=1, le=10)
    playback_id: UUID | None = None
    seq: int | None = Field(default=None, ge=0, le=1000000)
    event_type: Literal['exposure', 'playback_start', 'playback_progress', 'playback_end']
    origin: Literal['web', 'android']
    listened_ms: int = Field(default=0, ge=0, le=86400000)
    covered_ms: int = Field(default=0, ge=0, le=86400000)
    duration_ms: int | None = Field(default=None, gt=0, le=86400000)
    terminal_reason: Literal['', 'ended', 'user_next', 'user_select', 'stop', 'error', 'automatic'] = ''
    active: bool = False
    occurred_at: float = Field(gt=0)

    @model_validator(mode='after')
    def consistent(self):
        if not self.track_key and not self.track:
            raise ValueError('需要歌曲标识')
        if self.batch_id and self.feed_id or bool(self.batch_id or self.feed_id) != (self.position is not None):
            raise ValueError('列表或批次与位置必须同时提供，且不能混用')
        if self.event_type != 'exposure' and (self.playback_id is None or self.seq is None):
            raise ValueError('播放事件需要会话和序号')
        if self.covered_ms > self.listened_ms or self.duration_ms and self.covered_ms > self.duration_ms:
            raise ValueError('播放覆盖范围无效')
        return self

class Events(Strict):
    events: list[Event] = Field(min_length=1, max_length=50)

class Route(Strict):
    query: str = Field(min_length=1, max_length=100)
    type: Literal['seed_features', 'artist_expansion', 'exploration', 'related_playlist'] = 'seed_features'

class Plan(Strict):
    desired_energy: float | None = Field(default=None, ge=0, le=1)
    vocals: Literal['allowed','instrumental'] = 'allowed'
    moods: list[str] = Field(default_factory=list,max_length=8)
    preferred_artists: list[str] = Field(default_factory=list,max_length=10)
    routes: list[Route] = Field(min_length=1, max_length=5)
    languages: list[str] = Field(default_factory=list, max_length=8)
    excluded_artists: list[str] = Field(default_factory=list, max_length=20)


class NextFeed(CreateBatch):
    replay: bool = False
    context: Literal['general', 'focus', 'commute', 'exercise', 'sleep'] = 'general'


class ProfileItem(Strict):
    dimension: Literal['language', 'artist', 'vocal_role', 'moods', 'themes', 'tempo', 'energy', 'style', 'instrument']
    value: str = Field(min_length=1, max_length=100)
    scope: Literal['long_term', 'recent', 'context'] = 'long_term'
    context: Literal['general', 'focus', 'commute', 'exercise', 'sleep'] = 'general'
    preference: float = Field(ge=-1, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode='after')
    def valid_dimension(self):
        if self.scope == 'context' and self.context == 'general':
            raise ValueError('场景偏好需要明确场景')
        if self.scope != 'context' and self.context != 'general':
            raise ValueError('只有场景偏好可指定场景')
        if self.dimension in {'energy', 'tempo'}:
            value = float(self.value)
            if not 0 <= value <= (1 if self.dimension == 'energy' else 400):
                raise ValueError('音频特征超出范围')
        return self


class Proposal(Strict):
    summary: str = Field(default='', max_length=500)
    items: list[ProfileItem] = Field(default_factory=list, max_length=120)


class ProfileControl(Strict):
    expected_agent_revision: int = Field(ge=0)


class Correction(ProfileControl):
    item: ProfileItem


class RestoreProfile(ProfileControl):
    version: int = Field(ge=1)


class SaveFeed(Strict):
    name: str | None = Field(default=None,min_length=1,max_length=80)
