from __future__ import annotations

import re
import json
import time
from typing import Any
from urllib.parse import quote, unquote
import logging

from openai import OpenAI, APIConnectionError, APIStatusError
from urllib.parse import urlparse

from backend.modules.accounts import service as accounts
from backend.infrastructure import ai
from backend.infrastructure import speech
from backend.infrastructure import generation


logger = logging.getLogger("videodream.summary")
from backend.core import paths
from backend.modules.video import constants as video_constants
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import parsing as video_parsing
from backend.modules.video import schemas as video_schemas
from backend.modules.video import subtitles as video_subtitles
from backend.modules.video import tasks as video_tasks
from backend.modules.video import urls as video_urls
from backend.modules.video import timing


def _safe_filename_stem(value: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|\s]+', " ", value).strip()[:80]
    return clean or "VideoDream"


def _summary_source_label(source: str) -> str:
    if source == "manual":
        return "人工字幕"
    if source == "page_text":
        return "页面文案"
    if source == "speech":
        return "语音转写（分段时间戳）"
    return "自动字幕"


def _load_ai_config() -> dict[str, str]:
    return generation.text_config.get() or ai.load_config("text", legacy_path=paths.AI_CONFIG_PATH)


def _model_completion(client, **kwargs):
    checkpoint = generation.request_checkpoint.get()
    for attempt in range(2):
        checkpoint()
        metric = generation.request_metrics.get()
        if metric:
            metric("attempt", 0)
        try:
            with generation.provider_slot(checkpoint):
                result = client.chat.completions.create(**kwargs)
            checkpoint()
            return result
        except (APIConnectionError, APIStatusError) as exc:
            retryable = isinstance(exc, APIConnectionError) or exc.status_code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 1:
                raise
            delay = 1.0
            if isinstance(exc, APIStatusError):
                try:
                    delay = min(10.0, max(1.0, float(exc.response.headers.get("retry-after", 1))))
                except ValueError:
                    pass
            if metric:
                metric("retry", delay * 1000)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                checkpoint()
                time.sleep(min(0.2, max(0, deadline-time.monotonic())))


def _single_model_completion(client, **kwargs):
    try:
        return _model_completion(client, **kwargs)
    finally:
        if hasattr(client, "close"):
            client.close()


def _transcript_for_prompt(segments: list[video_schemas.SubtitleSegment]) -> str:
    lines: list[str] = []
    current_length = 0
    for segment in segments:
        line = f"[{segment.timestamp}] {segment.text}"
        next_length = current_length + len(line) + 1
        if next_length > video_constants.MAX_SUMMARY_TRANSCRIPT_CHARS:
            lines.append("[内容过长，后续字幕已截断用于首版摘要。]")
            break
        lines.append(line)
        current_length = next_length
    return "\n".join(lines)


def _generate_ai_summary(title: str, webpage_url: str, segments: list[video_schemas.SubtitleSegment], *, source: str = "manual") -> str:
    config = _load_ai_config()
    client_kwargs: dict[str, str] = {"api_key": config["api_key"]}
    if config.get("base_url"):
        client_kwargs["base_url"] = config["base_url"]
    client = OpenAI(**client_kwargs, timeout=180, max_retries=0)
    transcript = _transcript_for_prompt(segments)
    completion = _single_model_completion(client,
        model=config["model"],
        messages=[
            {
                "role": "system",
                "content": (
                    "你是专业的视频内容整理助手。请基于带时间戳的字幕生成中文 Markdown 摘要。"
                    "必须包含四个二级标题：概览、关键要点、时间线章节、适合保存的笔记。"
                    "时间线章节中的每条要尽量保留字幕时间戳。不要编造字幕中没有的信息。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"视频标题：{title}\n"
                    f"视频链接：{webpage_url}\n\n" +
                    ("语音转写的时间戳代表音频分段起点，章节定位仅为近似值；转写可能包含识别错误。\n" if source == "speech" else "") +
                    f"字幕：\n{transcript}"
                ),
            },
        ],
        temperature=0.2,
    )
    content = completion.choices[0].message.content if completion.choices else None
    summary = str(content or "").strip()
    if not summary:
        raise RuntimeError("AI 接口返回了空摘要。")
    return summary


def _sanitize_mind_map_payload(
    payload: Any,
    segments: list[video_schemas.SubtitleSegment],
    default_title: str,
    allowed_segment_ids: set[int] | None = None,
) -> video_schemas.MindMapResult:
    if not isinstance(payload, dict):
        raise RuntimeError("AI 思维导图不是合法 JSON 对象。")
    counter = 0

    def clean_text(value: Any, maximum: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]

    def clean_nodes(value: Any, depth: int) -> list[MindMapNode]:
        nonlocal counter
        if not isinstance(value, list) or depth > video_constants.MAX_MIND_MAP_DEPTH:
            return []
        cleaned: list[video_schemas.MindMapNode] = []
        for candidate in value:
            if len(cleaned) >= video_constants.MAX_MIND_MAP_CHILDREN:
                break
            if not isinstance(candidate, dict):
                continue
            title = clean_text(candidate.get("title"), 80)
            raw_ids = candidate.get("segment_ids")
            if not title or not isinstance(raw_ids, list):
                continue
            segment_ids = sorted(
                {
                    item
                    for item in raw_ids
                    if isinstance(item, int)
                    and not isinstance(item, bool)
                    and 0 <= item < len(segments)
                    and (allowed_segment_ids is None or item in allowed_segment_ids)
                }
            )
            if not segment_ids:
                continue
            counter += 1
            cleaned.append(
                video_schemas.MindMapNode(
                    id=f"node-{counter}",
                    title=title,
                    summary=clean_text(candidate.get("summary"), 300),
                    timestamp=min(segments[index].start for index in segment_ids),
                    segment_ids=segment_ids,
                    children=clean_nodes(candidate.get("children"), depth + 1),
                )
            )
        return cleaned

    nodes = clean_nodes(payload.get("nodes"), 1)
    if not nodes:
        raise RuntimeError("AI 思维导图没有有效节点。")
    title = clean_text(payload.get("title"), 80) or clean_text(default_title, 80) or "视频思维导图"
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return video_schemas.MindMapResult(title=title, nodes=nodes, generated_at=generated_at)


def _mind_map_prompt_chunks(segments: list[video_schemas.SubtitleSegment]) -> list[tuple[str, set[int]]]:
    chunks: list[tuple[str, set[int]]] = []
    lines: list[str] = []
    segment_ids: set[int] = set()
    current_length = 0
    for index, segment in enumerate(segments):
        prefix = f"[{index}] [{segment.timestamp}] "
        text_limit = max(1, video_constants.MAX_SUMMARY_TRANSCRIPT_CHARS - len(prefix) - 1)
        line = prefix + segment.text[:text_limit]
        if current_length + len(line) + 1 > video_constants.MAX_SUMMARY_TRANSCRIPT_CHARS:
            if lines:
                chunks.append(("\n".join(lines), segment_ids))
            lines = []
            segment_ids = set()
            current_length = 0
        lines.append(line)
        segment_ids.add(index)
        current_length += len(line) + 1
    if lines:
        chunks.append(("\n".join(lines), segment_ids))
    return chunks


def _generate_ai_mind_map(title: str, segments: list[video_schemas.SubtitleSegment]) -> video_schemas.MindMapResult:
    config = _load_ai_config()
    client_kwargs: dict[str, str] = {"api_key": config["api_key"]}
    if config.get("base_url"):
        client_kwargs["base_url"] = config["base_url"]
    client = OpenAI(**client_kwargs, timeout=180, max_retries=0)
    def generate(chunk):
        indexed_transcript, allowed_ids = chunk
        completion = _model_completion(client,
            model=config["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是视频学习内容整理助手。只依据提供的字幕生成思维导图 JSON，禁止补充字幕外事实。"
                        "顶层格式为 {title, nodes}。每个节点只包含 title、summary、segment_ids、children。"
                        "segment_ids 必须使用字幕行开头的整数索引。目标是在一个屏幕上清晰预览核心知识结构。"
                        "先按知识或论点关系归类，再组织层级；同一主题的重复讲解、操作演示、举例和结尾回顾合并，避免按时间流水账逐步列节点。"
                        "中心 title 应提炼本段实际主题，约8至16字，去掉课程宣传、系列名称及无关前后缀。"
                        "通常4至6个主主题，每个主题2至3个关键点；内容较少时减少，不为凑数补充。"
                        "包含中心主题在内通常不超过25个节点，结构以中心、主主题、关键点三层为主；重要内容较多时优先合并同类项，保持核心主题覆盖。"
                        "节点 title 用6至12字的简短关键词或结论，summary 用一至两句说明关键事实、例子和操作细节，避免与标题重复。"
                        "多次出现的同一概念只组织一次，可引用分散的多个 segment_ids，保留各主题的字幕依据。"
                        "最多四层，每个节点最多十个子节点；这些是兼容上限，不是需要填满的数量。"
                    ),
                },
                {"role": "user", "content": f"视频标题：{title}\n\n字幕：\n{indexed_transcript}"},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content if completion.choices else None
        if not str(content or "").strip():
            raise RuntimeError("AI 接口返回了空思维导图。")
        try:
            payload = json.loads(str(content))
        except json.JSONDecodeError as exc:
            raise RuntimeError("AI 思维导图不是合法 JSON。") from exc
        return _sanitize_mind_map_payload(payload, segments, title, allowed_ids)
    try:
        chunk_results = generation.bounded_map(generate, _mind_map_prompt_chunks(segments),
            executor=generation.model_executor, workers=2, checkpoint=generation.request_checkpoint.get(),
            progress=generation.chunk_progress.get())
    finally:
        if hasattr(client, "close"):
            client.close()
    if not chunk_results:
        raise RuntimeError("没有可用于思维导图的字幕。")

    queues = [list(result.nodes) for result in chunk_results]
    selected: list[MindMapNode] = []
    while len(selected) < video_constants.MAX_MIND_MAP_CHILDREN and any(queues):
        for queue in queues:
            if queue and len(selected) < video_constants.MAX_MIND_MAP_CHILDREN:
                selected.append(queue.pop(0))

    counter = 0

    def reindex(node: MindMapNode) -> MindMapNode:
        nonlocal counter
        counter += 1
        node_id = f"node-{counter}"
        children = [reindex(child) for child in node.children]
        return node.model_copy(update={"id": node_id, "children": children})

    return video_schemas.MindMapResult(
        title=chunk_results[0].title,
        nodes=[reindex(node) for node in selected],
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def _run_mind_map(task: video_tasks.MindMapTask, summary: video_tasks.SummaryTask) -> None:
    checkpoint = lambda: video_tasks._checkpoint(task, {})
    check_token = generation.request_checkpoint.set(checkpoint)
    metrics_token = generation.request_metrics.set(lambda event, value: task.timing.metric("map_model", event, value))
    def progress(done, total):
        task.timing.update("map_model", completed_units=done, total_units=total)
        video_tasks._set_mind_map_task(task, progress=20 + 70 * done/max(1, total), message=f"正在生成导图 {done}/{total}")
    progress_token = generation.chunk_progress.set(progress)
    try:
        source = getattr(summary, "source_info", None)
        if not source and not summary.result:
            raise RuntimeError("字幕内容不可用。")
        config = _load_ai_config()
        task.model_info = {"model": config["model"], "service": urlparse(config.get("base_url") or "https://api.openai.com").hostname}
        video_tasks._set_mind_map_task(task, status="running", progress=20.0, message="正在整理字幕结构")
        with task.timing.stage("map_model", "思维导图生成"):
            video_tasks._persist_task(task, "mind_map", force=True)
            result = _generate_ai_mind_map(source["title"] if source else summary.result.title,
                                          summary.transcript or summary.result.transcript)
        with task.timing.stage("map_validate", "导图结果校验"):
            checkpoint()
            result = video_schemas.MindMapResult.model_validate(result)
        video_tasks._set_mind_map_task(
            task,
            status="finished",
            progress=100.0,
            message="思维导图已生成",
            result=result,
        )
    except Exception as exc:
        video_tasks._set_mind_map_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="思维导图生成失败",
            error=video_urls._clean_error(exc, summary.url),
        )
    finally:
        generation.request_checkpoint.reset(check_token)
        generation.request_metrics.reset(metrics_token)
        generation.chunk_progress.reset(progress_token)


def _save_summary_files(task: video_tasks.SummaryTask, result: video_schemas.SummaryResult) -> video_schemas.SummaryResult:
    stem = f"{task.task_id}.{_safe_filename_stem(result.title)}"
    markdown_name = f"{stem}.summary.md"
    json_name = f"{stem}.summary.json"
    markdown_path = paths.SUMMARY_DIR / markdown_name
    json_path = paths.SUMMARY_DIR / json_name

    transcript_markdown = "\n".join(f"- [{item.timestamp}] {item.text}" for item in result.transcript)
    markdown_content = (
        f"{result.summary_markdown.strip()}\n\n"
        "---\n\n"
        "## 字幕/转写文本\n\n"
        f"{transcript_markdown}\n"
    )
    markdown_path.write_text(markdown_content, encoding="utf-8")
    json_path.write_text(
        json.dumps(result.model_dump(exclude={"markdown_url", "json_url"}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result.markdown_url = f"/api/summary-files/{quote(markdown_name, safe='')}"
    result.json_url = f"/api/summary-files/{quote(json_name, safe='')}"
    accounts.register_resource("summary_file", markdown_name, {"filename": markdown_name, "size": markdown_path.stat().st_size, "source_url": task.url, "summary_task_id": task.task_id, "parse_record_id": task.parse_record_id}, uid=task.user_id)
    accounts.register_resource("summary_file", json_name, {"filename": json_name, "size": json_path.stat().st_size, "source_url": task.url, "summary_task_id": task.task_id, "parse_record_id": task.parse_record_id}, uid=task.user_id)
    return result


def _extract_speech_summary_source(task: video_tasks.SummaryTask, info: dict | None):
    video_tasks._set_summary_task(task, progress=10, message="未找到可用字幕，正在准备音轨转写")
    cached = video_parse_cache._get_sniff_cache(task.url)
    if info is None:
        cached = cached or video_parsing.parse_video(task.url)
        info = {"title": cached.title, "duration": cached.duration, "webpage_url": cached.webpage_url or task.url}
    source_url, direct = task.url, False
    if cached:
        selected = next((item for item in cached.formats if item.format_id.startswith("sniffpair:")), None)
        selected = selected or next((item for item in cached.formats if item.format_id.startswith("sniff:")), None)
        if selected:
            direct = True
            if selected.format_id.startswith("sniffpair:"):
                source_url = unquote(selected.format_id.removeprefix("sniffpair:").partition("|")[2])
            else:
                source_url = unquote(selected.format_id.removeprefix("sniff:"))
    options = video_parsing._ydl_base_options(task.url)
    options["http_headers"]["Referer"] = task.url
    rows = speech.transcribe_video(
        url=source_url, title=str(info.get("title") or "视频音轨"), task_id=task.task_id,
        work_root=paths.ROOT_DIR / ".run" / "speech", ffmpeg_dir=paths.FFMPEG_DIR, options=options, direct=direct,
        expected_duration=info.get("duration"),
        checkpoint=lambda: video_tasks._checkpoint(task, {}),
        progress=lambda value, message: video_tasks._set_summary_task(task, progress=value, message=message),
        timer=task.timing,
    )
    segments = [video_schemas.SubtitleSegment(start=item.start, end=item.end, timestamp=video_subtitles._format_timestamp(item.start), text=item.text) for item in rows]
    return info, "自动识别", "speech", segments


def _run_summary(task: video_tasks.SummaryTask) -> None:
    config_token = None
    check_token = generation.request_checkpoint.set(lambda: video_tasks._checkpoint(task, {}))
    metrics_token = generation.request_metrics.set(lambda event, value: task.timing.metric("summary_model", event, value))
    try:
        video_tasks._set_summary_task(task, status="running", progress=8.0, message="正在解析视频字幕")
        with task.timing.stage("source", "字幕与转写准备"):
            video_tasks._persist_task(task, "summary", force=True)
            info, language, source, segments = video_subtitles._extract_summary_source(
                task.url, speech_fallback=lambda info: _extract_speech_summary_source(task, info),
            )
        title = str(info.get("title") or "未命名视频")
        webpage_url = str(info.get("webpage_url") or task.url)
        task.source_info = {"title": title, "webpage_url": webpage_url}
        config = _load_ai_config()
        config_token = generation.text_config.set(config)
        task.model_info = {"model": config["model"], "service": urlparse(config.get("base_url") or "https://api.openai.com").hostname}
        video_tasks._set_summary_task(
            task,
            progress=64.0 if source == "speech" else 42.0,
            message="语音转写完成，正在生成 AI 摘要" if source == "speech" else f"已提取 {language} 字幕，正在生成 AI 摘要",
            transcript=segments,
        )
        if task.map_task:
            task.map_task.timing.stop("waiting_source")
            task.map_task.timing.start("map_queue", "导图任务排队")
            task.map_future = accounts.submit(video_tasks.ai_executor, _run_mind_map, task.map_task, task)
        elif task.generate_mind_map:
            create_pipeline_map(task)
            task.map_task.timing.stop("waiting_source")
            task.map_task.timing.start("map_queue", "导图任务排队")
            task.map_future = accounts.submit(video_tasks.ai_executor, _run_mind_map, task.map_task, task)
        with task.timing.stage("summary_model", "AI 总结生成"):
            video_tasks._persist_task(task, "summary", force=True)
            summary_markdown = _generate_ai_summary(title, webpage_url, segments, source=source)
        video_tasks._set_summary_task(task, progress=82.0, message="正在保存总结文件")
        result = video_schemas.SummaryResult(
            title=title,
            webpage_url=webpage_url,
            language=language,
            source=_summary_source_label(source),
            summary_markdown=summary_markdown,
            transcript=segments[:video_constants.MAX_TRANSCRIPT_SEGMENTS_IN_RESPONSE],
        )
        with task.timing.stage("save", "保存总结文件"):
            result = _save_summary_files(task, result)
        video_tasks._set_summary_task(
            task,
            status="finished",
            progress=100.0,
            message="AI 总结已生成",
            result=result,
        )
    except Exception as exc:
        video_tasks._set_summary_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="总结失败",
            error=video_urls._clean_error(exc, task.url),
        )
        if task.map_task and not task.map_future:
            video_tasks._set_mind_map_task(task.map_task, status="failed", message="字幕准备失败，导图未执行", error="未取得可用字幕，请重试生成任务。")
    finally:
        if config_token is not None:
            generation.text_config.reset(config_token)
        generation.request_checkpoint.reset(check_token)
        generation.request_metrics.reset(metrics_token)


def create_pipeline_map(summary):
    """Reserve a durable map version before dispatch, using the summary's owner."""
    import uuid
    task = video_tasks.MindMapTask(uuid.uuid4().hex[:12], summary.task_id)
    task.user_id = summary.user_id
    task.parse_record_id = summary.parse_record_id
    task.timing.stop("queue")
    task.timing.start("waiting_source", "等待字幕就绪")
    summary.map_task = task
    summary.mind_map_task_id = task.task_id
    with video_tasks.mind_map_tasks_lock:
        video_tasks.mind_map_tasks[summary.task_id] = task
    video_tasks._persist_task(task, "mind_map", force=True)
    return task
