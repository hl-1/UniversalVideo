import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.infrastructure import audio
from backend.core import paths, storage
from backend.modules.video import downloads as video_downloads
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import parsing as video_parsing
from backend.modules.video import platforms as video_platforms
from backend.modules.video import resources as video_resources
from backend.modules.video import schemas as video_schemas
from backend.modules.video import tasks as video_tasks
from backend.modules.music import sources as music
from backend.infrastructure.runtime import executable_name


class AudioExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (paths.FFMPEG_DIR / executable_name("ffmpeg")).is_file():
            raise unittest.SkipTest("ffmpeg is not installed")
        cls.work = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.work.name)
        cls.source = cls.directory / "source.mp4"
        subprocess.run([
            str(paths.FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=size=64x64:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(cls.source),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_exports_audio_only_in_both_formats(self):
        for extension in ("m4a", "mp3"):
            with self.subTest(extension=extension):
                target = self.directory / f"result.{extension}"
                audio.export_audio(self.source, target, paths.FFMPEG_DIR, 2)
                info = audio.probe_audio(target, paths.FFMPEG_DIR)
                self.assertEqual([stream["codec_type"] for stream in info["streams"]], ["audio"])
                self.assertAlmostEqual(info["duration"], 2, delta=0.15)
                self.assertEqual(info["codec"], "aac" if extension == "m4a" else "mp3")
                if extension == "m4a":
                    content = target.read_bytes()
                    self.assertLess(content.index(b'moov'), content.index(b'mdat'))

    def test_rejects_short_trial_instead_of_marking_complete(self):
        target = self.directory / "trial.m4a"
        with self.assertRaisesRegex(RuntimeError, "试听"):
            audio.export_audio(self.source, target, paths.FFMPEG_DIR, 180)
        self.assertFalse(target.exists())

    def test_rejects_video_without_audio(self):
        silent = self.directory / "silent.mp4"
        subprocess.run([str(paths.FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-y", "-i", str(self.source), "-an", "-c:v", "copy", str(silent)], check=True, capture_output=True)
        with self.assertRaisesRegex(RuntimeError, "音轨"):
            audio.export_audio(silent, self.directory / "silent.m4a", paths.FFMPEG_DIR)


class AudioRoutingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for mocked in (patch.object(paths, "DOWNLOAD_DIR", Path(folder.name)),
                       patch.object(storage, "owner", side_effect=lambda uid=None: uid or 1)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_douyin_detail_metadata_is_bound_to_target_video(self):
        response = Mock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/", status=200)
        response.json.return_value = {"aweme_detail": {"aweme_id": "123", "desc": "target", "video": {"duration": 64800, "cover": {"url_list": ["https://example.com/cover.jpg"]}}, "author": {"nickname": "creator"}}}
        meta = video_platforms._extract_douyin_response_meta(response, "123")
        self.assertEqual(meta["duration"], 64)
        self.assertEqual(meta["title"], "target")
        self.assertEqual(meta["thumbnail"], "https://example.com/cover.jpg")
        self.assertEqual(video_platforms._extract_douyin_response_meta(response, "other-video"), {})

    def test_unavailable_douyin_detail_preserves_page_metadata_fallback(self):
        response = Mock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/", status=200)
        response.json.side_effect = ValueError("empty response")
        self.assertEqual(video_platforms._extract_douyin_response_meta(response, "123"), {})

    def test_douyin_audio_is_identified_by_dash_path_even_with_video_mime(self):
        self.assertTrue(video_resources._is_douyin_audio_stream("https://v11-weba.douyinvod.com/example/media-audio-und-mp4a/?mime_type=video_mp4"))
        self.assertFalse(video_resources._is_douyin_audio_stream("https://v11-weba.douyinvod.com/example/media-video-avc1/?mime_type=video_mp4"))
        self.assertFalse(video_resources._is_douyin_audio_stream("https://evil.example/media-audio-und-mp4a/"))

    def test_legacy_request_defaults_to_video(self):
        request = video_schemas.DownloadRequest(url="https://example.com/video")
        self.assertEqual(request.media_type, "video")

    def test_audio_uses_cached_douyin_source_with_matching_duration(self):
        url = "https://www.douyin.com/video/123456789"
        media_url = "https://cdn.example.com/source.mp4"
        format_id = "sniff:https%3A%2F%2Fcdn.example.com%2Fsource.mp4"
        result = video_schemas.ParseResponse(title="track", duration=15, webpage_url=url, formats=[video_schemas.FormatInfo(format_id=format_id, label="MP4", ext="mp4")])
        task = video_tasks.DownloadTask("audio-test", url, format_id, media_type="audio")
        with patch.object(video_parse_cache, '_get_sniff_cache', return_value=result), patch.object(video_parsing, 'parse_video') as parse, patch.object(audio, 'download_audio', return_value=Path("track.m4a")) as download:
            video_downloads._run_download(task)
        parse.assert_not_called()
        self.assertEqual(download.call_args.kwargs["url"], media_url)
        self.assertTrue(download.call_args.kwargs["direct"])
        self.assertEqual(download.call_args.kwargs["expected_duration"], 15)
        self.assertEqual(task.status, "finished")
        self.assertEqual(task.to_response().media_type, "audio")

    def test_split_video_downloads_only_the_audio_source(self):
        url = "https://www.youtube.com/watch?v=example"
        format_id = "sniffpair:https%3A%2F%2Fcdn.example.com%2Fvideo.mp4|https%3A%2F%2Fcdn.example.com%2Faudio.m4a"
        result = video_schemas.ParseResponse(title="track", formats=[video_schemas.FormatInfo(format_id=format_id, label="merged")])
        task = video_tasks.DownloadTask("split-audio-test", url, format_id, media_type="audio", audio_format="mp3")
        with patch.object(video_parse_cache, '_get_sniff_cache', return_value=result), patch.object(audio, 'download_audio', return_value=Path("track.mp3")) as download:
            video_downloads._run_download(task)
        self.assertEqual(download.call_args.kwargs["url"], "https://cdn.example.com/audio.m4a")
        self.assertEqual(download.call_args.kwargs["output_format"], "mp3")

    def test_audio_export_failure_marks_task_failed(self):
        task = video_tasks.DownloadTask("failed-audio", "https://example.com/video", None, media_type="audio")
        with patch.object(video_parse_cache, '_get_sniff_cache', return_value=None), patch.object(video_parsing, 'parse_video', side_effect=RuntimeError("unavailable")):
            video_downloads._run_download(task)
        self.assertEqual(task.status, "failed")
        self.assertIsNone(task.filename)

    def test_trial_source_retries_public_parser_and_still_validates_duration(self):
        for provider, source in (("netease", "原平台"), ("kugou", "酷狗音乐")):
            with self.subTest(provider=provider):
                track = music._register(music.Track(title="song", duration=180, webpage_url="https://example.com/song", provider=provider, identifier="123"))
                task = video_tasks.DownloadTask("fallback-audio", track.webpage_url, f"music:{track.token}", media_type="audio")
                with patch.object(music, "resolve", side_effect=[("https://cdn.example.com/trial.mp3", {}, source), ("https://cdn.example.com/full.mp3", {}, "parser")]) as resolve, patch.object(audio, 'download_audio', side_effect=[audio.IncompleteAudioError("trial"), Path("song.m4a")]) as download:
                    video_downloads._run_download(task)
                self.assertEqual(task.status, "finished")
                self.assertTrue(resolve.call_args.kwargs["public_only"])
                self.assertEqual(download.call_args.kwargs["expected_duration"], 180)

    def test_truncated_public_parser_result_is_not_retried_or_saved(self):
        track = music._register(music.Track(title="song", duration=180, webpage_url="https://music.163.com/song?id=123", provider="netease", identifier="123"))
        task = video_tasks.DownloadTask("failed-trial", track.webpage_url, f"music:{track.token}", media_type="audio")
        with patch.object(music, "resolve", return_value=("https://cdn.example.com/trial.mp3", {}, "parser")) as resolve, patch.object(audio, 'download_audio', side_effect=audio.IncompleteAudioError("trial")):
            video_downloads._run_download(task)
        self.assertEqual(task.status, "failed")
        self.assertIsNone(task.filename)
        resolve.assert_called_once()


class MusicIdentityTests(unittest.TestCase):
    def test_kugou_paid_link_uses_mobile_metadata_and_full_duration(self):
        url = "https://www.kugou.com/song/#hash=" + "A" * 32
        with patch.object(music, "_json", side_effect=[{"data": [], "status": 0}, {"songName": "song", "singerName": "artist", "timeLength": 0, "extra": {"128timelength": 269000}, "128privilege": 10}]):
            track = music.parse_url(url)
        self.assertEqual(track.title, "song")
        self.assertEqual(track.artist, "artist")
        self.assertEqual(track.duration, 269)
        self.assertEqual(track.access, "会员或付费")
        self.assertEqual(music.get_track(track.token, url).identifier, "A" * 32)

    def test_kugou_retries_public_source_when_native_source_is_unavailable(self):
        track = music.Track(title="song", webpage_url="https://www.kugou.com/song/#hash=" + "A" * 32, provider="kugou", identifier="A" * 32)
        with patch.object(music, "_json", side_effect=[{"data": {}}, {"url": ""}, {"code": 1, "data": "https://cdn.example.com/full.mp3"}]):
            url, _, source = music.resolve(track, {})
        self.assertEqual(url, "https://cdn.example.com/full.mp3")
        self.assertIn("music.lzmhhh.com", source)

    def test_kugou_public_only_skips_native_and_rejects_invalid_source(self):
        track = music.Track(title="song", webpage_url="https://www.kugou.com/song/#hash=" + "A" * 32, provider="kugou", identifier="A" * 32)
        for data in ({"code": 1, "data": "javascript:alert(1)"}, {"code": 0, "data": "https://cdn.example.com/full.mp3"}, {"code": 1, "data": {"url": "https://cdn.example.com/full.mp3"}}):
            with self.subTest(data=data), patch.object(music, "_json", return_value=data) as get:
                with self.assertRaisesRegex(RuntimeError, "酷狗"):
                    music.resolve(track, {}, public_only=True)
                get.assert_called_once()

    def test_kugou_uses_mobile_source_when_web_source_has_no_url(self):
        track = music.Track(title="song", webpage_url="https://www.kugou.com/song/#hash=" + "A" * 32, provider="kugou", identifier="A" * 32)
        with patch.object(music, "_json", side_effect=[{"data": {}}, {"url": "https://cdn.example.com/song.mp3"}]) as get:
            url, _, source = music.resolve(track, {})
        self.assertEqual(url, "https://cdn.example.com/song.mp3")
        self.assertEqual(source, "酷狗音乐")
        self.assertEqual(get.call_count, 2)

    def test_recognizes_platform_song_links_and_rejects_lookalike_hosts(self):
        self.assertEqual(music.identify("https://music.163.com/#/song?id=123"), ("netease", "123"))
        self.assertEqual(music.identify("https://y.qq.com/n/ryqq/songDetail/004Ti8rT003TaZ"), ("qq", "004Ti8rT003TaZ"))
        self.assertEqual(music.identify("https://www.kugou.com/song/#hash=" + "a" * 32), ("kugou", "A" * 32))
        self.assertIsNone(music.identify("https://music.163.com.evil.example/song?id=123"))
        self.assertIsNone(music.identify("https://music.163.com/playlist?id=123"))

    def test_download_token_is_bound_to_the_original_song(self):
        track = music._register(music.Track(title="song", webpage_url="https://music.163.com/song?id=123", provider="netease", identifier="123"))
        self.assertEqual(music.get_track(track.token, track.webpage_url).title, "song")
        with self.assertRaises(HTTPException):
            music.get_track(track.token, "https://music.163.com/song?id=456")


if __name__ == "__main__":
    unittest.main()
