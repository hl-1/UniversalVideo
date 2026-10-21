from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi import HTTPException
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ExtractorError

from backend.core import paths, storage
from backend.infrastructure import audio
from backend.infrastructure.ytdlp import BaseYoutubeDL, BilibiliPublicApiIE, BilibiliVideoError, YoutubeDL
from backend.modules.video import downloads, parsing, subtitles, tasks


BVID = "BV1qW4y1a7fU"
URL = f"https://www.bilibili.com/video/{BVID}/?spm_id_from=test&p=6"


class BilibiliApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(storage, 'owner', side_effect=lambda uid=None: uid or 1))
        self.metadata = {"code": 0, "data": {
            "bvid": BVID, "aid": 123, "title": "教程", "pic": "https://i0.hdslb.com/cover.jpg",
            "owner": {"mid": 456, "name": "作者"},
            "pages": [{"page": 1, "cid": 111, "duration": 90, "part": "第一集"},
                      {"page": 6, "cid": 666, "duration": 727, "part": "Linux 安装"}],
        }}
        self.play = {"code": 0, "data": {"timelength": 726435, "dash": {
            "video": [{"id": 80, "baseUrl": "https://cdn.example/video-30080.m4s?deadline=4102444800",
                       "mimeType": "video/mp4", "codecs": "avc1.640028", "width": 1920, "height": 1080,
                       "bandwidth": 1000000}],
            "audio": [{"id": 30280, "baseUrl": "https://cdn.example/audio.m4s",
                       "mimeType": "audio/mp4", "codecs": "mp4a.40.2", "bandwidth": 128000}],
        }}}
        self.api = self.stack.enter_context(patch.object(BilibiliPublicApiIE, "_download_json", autospec=True,
                                                       side_effect=self.api_response))
        self.webpage = self.stack.enter_context(patch.object(BilibiliPublicApiIE, "_download_webpage_handle",
                                                            side_effect=AssertionError("网页入口被拦截")))
        self.ydl = self.stack.enter_context(YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False,
                                                      "noplaylist": True}, auto_init=False))
        self.ie = self.ydl.get_info_extractor("BiliBili")
        self.stack.enter_context(patch.object(BilibiliPublicApiIE, "_wbi_key_cache", {}))

    def api_response(self, ie, url, video_id, **kwargs):
        if url.endswith("/view"):
            return deepcopy(self.metadata)
        if url.endswith("/nav"):
            return {"code": -101, "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
            }}}
        if url.endswith("/playurl"):
            return deepcopy(self.play)
        if url.endswith("/wbi/v2"):
            return {"code": 0, "data": {"subtitle": {"subtitles": []}}}
        raise AssertionError(f"Unexpected API: {url}")

    def test_extracts_requested_part_and_signed_formats_without_webpage(self):
        info = self.ydl.extract_info(URL, download=False)
        self.assertEqual(info["id"], BVID + "_p6")
        self.assertEqual(info["webpage_url"], f"https://www.bilibili.com/video/{BVID}/?p=6")
        self.assertIn("p06 Linux 安装", info["title"])
        self.assertEqual(info["uploader"], "作者")
        self.assertAlmostEqual(info["duration"], 726.435)
        self.assertEqual({fmt["format_id"] for fmt in info["formats"]}, {"30080", "30280"})
        play_call = next(call for call in self.api.call_args_list if call.args[1].endswith("/playurl"))
        query = play_call.kwargs["query"]
        self.assertEqual((query["bvid"], query["cid"]), (BVID, "666"))
        self.assertEqual(len(query["w_rid"]), 32)
        self.assertIn("wts", query)
        self.webpage.assert_not_called()

    def test_parse_endpoint_model_keeps_video_audio_format_selection(self):
        result = parsing.parse_video(URL)
        self.assertEqual(result.duration, 726)
        self.assertEqual(result.uploader, "作者")
        self.assertTrue(result.thumbnail.startswith("/api/image-proxy?"))
        self.assertIn("30080+bestaudio/best", [item.format_id for item in result.formats])
        self.webpage.assert_not_called()

    def test_av_url_resolves_to_canonical_bv_and_default_first_part(self):
        result = self.ie._real_extract("https://www.bilibili.com/video/av123/")
        self.assertEqual(result["id"], BVID + "_p1")
        self.assertIn("第一集", result["title"])
        self.assertEqual(self.api.call_args_list[0].kwargs["query"], {"aid": "123"})

    def test_single_video_retains_unsuffixed_identity(self):
        self.metadata["data"]["pages"] = [self.metadata["data"]["pages"][0]]
        result = self.ie._real_extract(f"https://www.bilibili.com/video/{BVID}/")
        self.assertEqual(result["id"], BVID)
        self.assertEqual(result["webpage_url"], f"https://www.bilibili.com/video/{BVID}/")

    def test_invalid_or_missing_part_never_falls_back_to_other_content(self):
        with patch.object(BiliBiliIE, "_real_extract") as fallback:
            for part in ("0", "-1", "abc", "", "2"):
                with self.subTest(part=part), self.assertRaises(BilibiliVideoError):
                    self.ie._real_extract(f"https://www.bilibili.com/video/{BVID}/?p={part}")
            fallback.assert_not_called()

    def test_wrong_identity_deleted_and_exclusive_videos_are_terminal(self):
        for payload in (
            {"code": -404, "message": "不存在"},
            {"code": 0, "data": self.metadata["data"] | {"bvid": "BV1otherVideo"}},
            {"code": 0, "data": self.metadata["data"] | {"is_upower_exclusive": True}},
        ):
            with self.subTest(payload=payload), patch.object(self.ie, "_download_json", return_value=payload), \
                    patch.object(BiliBiliIE, "_real_extract") as fallback:
                with self.assertRaises(BilibiliVideoError):
                    self.ie._real_extract(URL)
                fallback.assert_not_called()

    def test_api_failure_uses_existing_webpage_extractor(self):
        with patch.object(self.ie, "_download_json", side_effect=ExtractorError("API unavailable")), \
                patch.object(BiliBiliIE, "_real_extract", return_value={"id": "fallback"}) as fallback:
            self.assertEqual(self.ie._real_extract(URL), {"id": "fallback"})
            fallback.assert_called_once_with(URL)

    def test_dual_failure_preserves_api_and_webpage_causes(self):
        with patch.object(BilibiliPublicApiIE, "_download_json", side_effect=ExtractorError("API timeout")), \
                patch.object(BiliBiliIE, "_real_extract", side_effect=ExtractorError("HTTP Error 412")), \
                patch.object(parsing.video_sniffing, "parse_sniffed_page") as sniff:
            with self.assertRaises(HTTPException) as raised:
                parsing.parse_video(URL)
            self.assertEqual(raised.exception.status_code, 422)
            self.assertIn("API timeout", raised.exception.detail)
            self.assertIn("HTTP Error 412", raised.exception.detail)
            sniff.assert_not_called()

    def test_malformed_signing_or_playback_response_uses_webpage_fallback(self):
        for endpoint in ("/nav", "/playurl"):
            def malformed(ie, url, video_id, **kwargs):
                if url.endswith(endpoint):
                    return {"code": 0}
                return self.api_response(ie, url, video_id, **kwargs)

            self.api.side_effect = malformed
            with self.subTest(endpoint=endpoint), \
                    patch.object(BiliBiliIE, "_real_extract", return_value={"id": "fallback"}) as fallback:
                self.assertEqual(self.ie._real_extract(URL), {"id": "fallback"})
                fallback.assert_called_once_with(URL)

    def test_invalid_part_does_not_enter_generic_sniffing(self):
        with patch.object(parsing.video_sniffing, "parse_sniffed_page") as sniff, \
                patch.object(parsing.video_browser, "parse_browser_sniffed_page") as browser:
            with self.assertRaises(HTTPException) as raised:
                parsing.parse_video(f"https://www.bilibili.com/video/{BVID}/?p=2")
            self.assertIn("不存在第 2 P", raised.exception.detail)
            sniff.assert_not_called()
            browser.assert_not_called()

    def test_nonstandard_bilibili_urls_keep_the_original_extractor(self):
        url = f"https://www.bilibili.com/festival/example?bvid={BVID}"
        with patch.object(BiliBiliIE, "_real_extract", return_value={"id": "festival"}) as original:
            self.assertEqual(self.ie._real_extract(url), {"id": "festival"})
            original.assert_called_once_with(url)
        self.api.assert_not_called()

    def test_summary_yt_dlp_fallback_also_uses_public_api(self):
        with patch.object(subtitles, "_extract_bilibili_api_summary_source", return_value=None):
            result = subtitles._extract_summary_source(URL, speech_fallback=lambda info: (info["id"], "asr"))
        self.assertEqual(result, (BVID + "_p6", "asr"))
        self.webpage.assert_not_called()

    def write_selected_media(self, ydl, info):
        self.selected.append(deepcopy(info))
        Path(ydl.prepare_filename(info)).write_bytes(b"test-media")

    def test_video_download_reextracts_fresh_formats_using_api(self):
        def transfer(url, target, **kwargs):
            Path(target).write_bytes(b'test-media')
        def merge(command, **kwargs):
            Path(command[-1]).write_bytes(b'merged')
            return Mock(poll=Mock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "DOWNLOAD_DIR", Path(folder)), \
                patch.object(downloads.video_transfer, "download", side_effect=transfer) as download, \
                patch.object(downloads.subprocess, "Popen", side_effect=merge) as merged:
            task = tasks.DownloadTask("bilibili-video", URL, "30080+bestaudio/best")
            downloads._run_download(task)
            self.assertEqual(task.status, "finished", task.error)
            self.assertTrue(storage.owned_path('file', task.filename, uid=1).is_file())
        self.assertEqual(len(download.call_args_list), 2)
        self.assertIn('video-30080.m4s', download.call_args_list[0].args[0])
        self.assertIn('audio.m4s', download.call_args_list[1].args[0])
        command = merged.call_args.args[0]
        self.assertFalse(any(value.startswith('https://') for value in command))
        self.webpage.assert_not_called()

    def test_dash_prefers_official_cdn_backup_without_changing_format_identity(self):
        stream = self.play['data']['dash']['video'][0]
        stream.update(baseUrl='https://peer.mcdn.bilivideo.cn:8082/video-30080.m4s?deadline=4102444800',
                      backupUrl=['https://upos-sz-mirrorcos.bilivideo.com/video-30080.m4s?deadline=4102444800'])
        info = self.ydl.extract_info(URL, download=False)
        video = next(item for item in info['formats'] if item['format_id'] == '30080')
        self.assertEqual(video['url'], stream['backupUrl'][0])

    def test_audio_download_uses_api_and_selects_only_audio(self):
        self.selected = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(BaseYoutubeDL, "process_info", autospec=True, side_effect=self.write_selected_media), \
                patch.object(audio, "export_audio", side_effect=lambda source, target, *args: target.write_bytes(b'test-audio')) as export:
            audio.download_audio(url=URL, title="音轨", task_id="bilibili-audio", output_format="m4a",
                                 download_dir=Path(folder), ffmpeg_dir=paths.FFMPEG_DIR,
                                 options=parsing._ydl_base_options(URL), progress=lambda *args: None)
            self.assertEqual(export.call_args.args[3], 726.435)
        self.assertEqual(self.selected[0]["format_id"], "30280")
        self.assertEqual(self.selected[0]["vcodec"], "none")
        self.webpage.assert_not_called()
