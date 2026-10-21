from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from backend.modules.video.constants import TaskStatus


class ParseRequest(BaseModel):
    url: HttpUrl
    force: bool = False


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=4096)
    media_type: Literal["video", "audio"] = "video"
    audio_format: Literal["original", "m4a", "mp3"] = "m4a"
    parse_record_id: str | None = Field(default=None, max_length=64)
    destination: Literal["device", "library"] = "library"


class SummaryRequest(BaseModel):
    url: HttpUrl
    parse_record_id: str | None = Field(default=None, max_length=64)
    generate_mind_map: bool = False
    bilibili_subtitle_policy: Literal["prompt", "speech"] = "prompt"


class PreviewRequest(BaseModel):
    url: HttpUrl
    format_id: str = Field(min_length=1, max_length=4096)
    parse_record_id: str | None = Field(default=None, max_length=64)
    force_prepare: bool = False


class PreviewResponse(BaseModel):
    preview_url: str | None = None
    expires_in: int
    preview_id: str | None = None
    status: Literal["preparing", "ready", "failed"] = "ready"
    status_url: str | None = None
    has_audio: bool | None = None
    message: str = ""
    error_code: str | None = None
    alternate: bool = False
    delivery: Literal["file", "proxy", "fragmented"] = "file"
    complete: bool = True
    reused: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)


class PreviewReuseRequest(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=4096)
    parse_record_id: str = Field(min_length=1, max_length=64)


class FormatInfo(BaseModel):
    format_id: str
    label: str
    ext: str | None = None
    resolution: str | None = None
    filesize: int | None = None
    has_video: bool | None = None


class ParseResponse(BaseModel):
    title: str
    thumbnail: str | None = None
    duration: int | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    formats: list[FormatInfo]
    record_id: str | None = None


class DownloadResponse(BaseModel):
    task_id: str


class BatchTrackRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    webpage_url: HttpUrl


class MusicBatchRequest(BaseModel):
    playlist_id: str | None = Field(default=None, min_length=1, max_length=24)
    playlist_name: str | None = Field(default=None, min_length=1, max_length=80)
    tracks: list[BatchTrackRequest] = Field(min_length=1, max_length=100)
    audio_format: Literal["original", "m4a", "mp3"] = "m4a"


class SubtitleSegment(BaseModel):
    start: float
    end: float | None = None
    timestamp: str
    text: str


class SummaryResult(BaseModel):
    title: str
    webpage_url: str
    language: str
    source: str
    summary_markdown: str
    transcript: list[SubtitleSegment]
    markdown_url: str | None = None
    json_url: str | None = None


class SummaryResponse(BaseModel):
    task_id: str
    mind_map_task_id: str | None = None


class SummaryTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    error: str | None = None
    result: SummaryResult | None = None
    timing: dict | None = None
    model_info: dict | None = None
    mind_map_task_id: str | None = None


class MindMapNode(BaseModel):
    id: str
    title: str
    summary: str = ""
    timestamp: float
    segment_ids: list[int]
    children: list[MindMapNode] = Field(default_factory=list)


class MindMapResult(BaseModel):
    title: str
    nodes: list[MindMapNode]
    generated_at: str


class MindMapResponse(BaseModel):
    task_id: str


class MindMapTaskResponse(BaseModel):
    task_id: str
    summary_task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    error: str | None = None
    result: MindMapResult | None = None
    version: int = 1
    timing: dict | None = None
    model_info: dict | None = None


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    filename: str | None = None
    download_url: str | None = None
    error: str | None = None
    media_type: Literal["video", "audio"] = "video"
    destination: Literal["device", "library"] = "library"
