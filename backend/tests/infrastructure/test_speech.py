import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core import paths
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import schemas as video_schemas
from backend.modules.video import subtitles as video_subtitles
from backend.modules.video import summary as video_summary
from backend.modules.video import tasks as video_tasks
from backend.infrastructure import ai
from backend.infrastructure import speech
from backend.infrastructure.runtime import executable_name


CONFIG = {"base_url": "https://speech.example/v1", "api_key": "test-key", "model": "asr-model"}


def write_audio(path: Path, seconds=2):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * int(16000 * seconds))


class SpeechResponseTests(unittest.TestCase):
    def test_text_only_response_uses_chunk_offsets_and_removes_model_tags(self):
        segments = speech.response_segments({"text": "<|zh|><|Speech|> 安装 PyCharm。"}, 120.5, 30)
        self.assertEqual(segments, [speech.SpeechSegment(120.5, 150.5, "安装 PyCharm。")])

    def test_provider_sentence_timestamps_are_offset_and_invalid_segments_rejected(self):
        payload = {"text": "full text", "segments": [
            {"start": 1, "end": 3, "text": "first"}, {"start": 5, "end": 4, "text": "backward"},
            {"start": float("nan"), "end": 8, "text": "invalid"}, {"start": 61, "end": 70, "text": "outside"},
        ]}
        self.assertEqual(speech.response_segments(payload, 60, 60), [speech.SpeechSegment(61, 63, "first")])

    def test_empty_silence_and_bad_responses(self):
        self.assertEqual(speech.response_segments({"text": "<|nospeech|>"}, 0, 30), [])
        with self.assertRaisesRegex(RuntimeError, "无效转写"):
            speech.response_segments({"text": None}, 0, 30)

    def test_retry_reopens_audio_and_sends_multipart_without_redirects(self):
        seen = []
        with tempfile.TemporaryDirectory() as working:
            path = Path(working) / "chunk.wav"
            write_audio(path)
            def respond(*args, **kwargs):
                seen.append(kwargs["files"]["file"][1].read())
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(kwargs["data"], {"model": "asr-model"})
                return Mock(status_code=503 if len(seen) == 1 else 200, json=lambda: {"text": "转写"})
            with patch.object(speech.requests, "post", side_effect=respond), patch.object(speech, "_wait_retry"):
                self.assertEqual(speech.transcribe_chunk(path, CONFIG, checkpoint=lambda: None), {"text": "转写"})
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], seen[1])

    def test_service_errors_hide_upstream_details_and_do_not_retry_authentication(self):
        with tempfile.TemporaryDirectory() as working:
            path = Path(working) / "chunk.wav"
            write_audio(path)
            response = Mock(status_code=401, text="secret upstream token")
            with patch.object(speech.requests, "post", return_value=response) as post:
                with self.assertRaisesRegex(RuntimeError, "密钥无效"):
                    speech.transcribe_chunk(path, CONFIG, checkpoint=lambda: None)
            self.assertEqual(post.call_count, 1)
            response.close.assert_called_once()

    def test_config_is_checked_before_audio_is_downloaded(self):
        with patch.object(ai, "load_config", side_effect=RuntimeError("missing ASR")), patch.object(speech, "download_audio") as download:
            with self.assertRaisesRegex(RuntimeError, "missing ASR"):
                speech.transcribe_video(url="https://example.com/video", title="video", task_id="test", work_root=Path("unused"),
                                        ffmpeg_dir=paths.FFMPEG_DIR, options={}, progress=Mock(), checkpoint=Mock())
            download.assert_not_called()


class SpeechAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (paths.FFMPEG_DIR / executable_name("ffmpeg")).is_file():
            raise unittest.SkipTest("ffmpeg is not installed")

    def test_real_ffmpeg_chunks_cover_entire_audio_with_absolute_offsets(self):
        with tempfile.TemporaryDirectory() as working:
            directory = Path(working)
            source = directory / "source.wav"
            write_audio(source, 2.4)
            with patch.object(speech, "CHUNK_SECONDS", 1):
                chunks = speech.split_audio(source, directory, paths.FFMPEG_DIR, checkpoint=lambda: None)
            self.assertEqual(len(chunks), 3)
            self.assertAlmostEqual(sum(item[2] for item in chunks), 2.4, places=2)
            self.assertEqual(chunks[0][1], 0)
            self.assertAlmostEqual(chunks[2][1], chunks[0][2] + chunks[1][2])
            for path, _, _ in chunks:
                with wave.open(str(path)) as audio:
                    self.assertEqual(audio.getframerate(), 16000)
                    self.assertEqual(audio.getnchannels(), 1)

    def test_transcription_cleans_temporary_audio_on_success_failure_and_cancel(self):
        for mode in ("success", "failure", "cancel", "silence"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as working:
                root = Path(working)
                cancelled = threading.Event()
                def download(**kwargs):
                    path = kwargs["download_dir"] / "source.wav"
                    write_audio(path)
                    return path
                def progress(value, message):
                    if mode == "cancel" and value >= 28:
                        cancelled.set()
                def checkpoint():
                    if cancelled.is_set():
                        raise RuntimeError("cancelled")
                def transcribe(path, config, **kwargs):
                    kwargs["checkpoint"]()
                    if mode == "failure":
                        raise RuntimeError("service failed")
                    return {"text": "" if mode == "silence" else "安装工具"}
                with patch.object(ai, "load_config", return_value=CONFIG), patch.object(speech, "download_audio", side_effect=download), \
                        patch.object(speech, "transcribe_chunk", side_effect=transcribe):
                    arguments = dict(url="https://example.com/video", title="video", task_id="test", work_root=root,
                                     ffmpeg_dir=paths.FFMPEG_DIR, options={}, progress=progress, checkpoint=checkpoint)
                    if mode == "success":
                        segments = speech.transcribe_video(**arguments)
                        self.assertEqual(segments[0].text, "安装工具")
                        self.assertAlmostEqual(segments[0].end, 2)
                    else:
                        with self.assertRaises(RuntimeError):
                            speech.transcribe_video(**arguments)
                self.assertEqual(list(root.iterdir()), [])


class SpeechSummaryTests(unittest.TestCase):
    def test_existing_subtitles_skip_speech_fallback(self):
        expected = ({"title": "subtitles"}, "zh", "manual", [video_schemas.SubtitleSegment(start=1, timestamp="0:01", text="字幕")])
        fallback = Mock()
        with patch.object(video_subtitles, '_extract_bilibili_api_summary_source', return_value=expected):
            self.assertEqual(video_subtitles._extract_summary_source("https://www.bilibili.com/video/BVtest", speech_fallback=fallback), expected)
        fallback.assert_not_called()

    def test_no_subtitle_invokes_transcription_with_correct_episode_information(self):
        from backend.tests.video.test_summary import NoSubtitleYoutubeDL
        expected = ({"title": "transcribed"}, "自动识别", "speech", [])
        fallback = Mock(return_value=expected)
        url = "https://www.bilibili.com/video/BV1qW4y1a7fU/?p=11"
        with patch.object(video_subtitles, '_extract_bilibili_api_summary_source', return_value=None), patch.object(video_subtitles, 'YoutubeDL', NoSubtitleYoutubeDL):
            self.assertEqual(video_subtitles._extract_summary_source(url, speech_fallback=fallback), expected)
        self.assertEqual(fallback.call_args.args[0]["webpage_url"], url)

    def test_summary_task_connects_transcription_to_ai_and_labels_source(self):
        task = video_tasks.SummaryTask("speech-test", "https://example.com/video")
        rows = [speech.SpeechSegment(60, 120, "安装 PyCharm")]
        info = {"title": "第11集", "duration": 120, "webpage_url": task.url}
        def extract(url, **kwargs):
            return kwargs["speech_fallback"](info)
        with tempfile.TemporaryDirectory() as working, patch.object(video_summary.storage, "directory", return_value=Path(working)), \
                patch.object(video_subtitles, '_extract_summary_source', side_effect=extract), patch.object(video_parse_cache, '_get_sniff_cache', return_value=None), \
                patch.object(speech, "transcribe_video", return_value=rows) as transcribe, \
                patch.object(video_summary, '_generate_ai_summary', return_value="## 概览\n工具安装") as summary, \
                patch.object(video_summary, '_save_summary_files', side_effect=lambda task, result: result):
            video_summary._run_summary(task)
        self.assertEqual(task.status, "finished")
        self.assertEqual(task.result.source, "语音转写（分段时间戳）")
        self.assertEqual(task.result.transcript[0].timestamp, "1:00")
        self.assertEqual(summary.call_args.kwargs["source"], "speech")
        self.assertEqual(transcribe.call_args.kwargs["expected_duration"], 120)

    def test_failed_transcription_does_not_call_summary_model(self):
        task = video_tasks.SummaryTask("speech-failed", "https://example.com/video")
        with patch.object(video_subtitles, '_extract_summary_source', side_effect=RuntimeError("speech failed")), \
                patch.object(video_summary, '_generate_ai_summary') as summary:
            video_summary._run_summary(task)
        self.assertEqual(task.status, "failed")
        summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
