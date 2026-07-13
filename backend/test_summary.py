import unittest
import json
from http.cookiejar import Cookie, CookieJar
from unittest.mock import patch

from yt_dlp.utils import DownloadError

from backend import main


class FailingYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=False):
        raise DownloadError("[Douyin] 123456789: Fresh cookies (not necessarily logged in) are needed")


class NoSubtitleYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=False):
        return {"title": "硬字幕视频", "webpage_url": url, "subtitles": {"danmaku": [{"ext": "xml", "url": "https://example.com/danmaku.xml"}]}}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)
        self.encoding = "utf-8"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class SummarySourceTests(unittest.TestCase):
    def test_bilibili_ai_subtitle_is_classified_as_automatic(self):
        self.assertEqual(main._bilibili_subtitle_source({"lan": "ai-zh", "type": 1, "ai_type": 0}), "automatic")
        self.assertEqual(main._bilibili_subtitle_source({"lan": "zh-CN", "type": 0, "ai_type": 0}), "manual")
        self.assertEqual(main._language_rank("ai-zh")[0], main._language_rank("zh")[0])

    def test_bilibili_cookie_header_only_uses_bilibili_login_cookies(self):
        jar = CookieJar()
        for name, value, domain in [
            ("SESSDATA", "session-value", ".bilibili.com"),
            ("bili_jct", "csrf-value", ".bilibili.com"),
            ("secret", "other-site", ".example.com"),
        ]:
            jar.set_cookie(Cookie(0, name, value, None, False, domain, True, domain.startswith("."), "/", True, False, None, False, None, None, {}, False))

        header = main._bilibili_cookie_header(jar)

        self.assertIn("SESSDATA=session-value", header)
        self.assertIn("bili_jct=csrf-value", header)
        self.assertNotIn("other-site", header)

    def test_bilibili_cookie_header_requires_login_cookie(self):
        jar = CookieJar()
        jar.set_cookie(Cookie(0, "buvid3", "visitor", None, False, ".bilibili.com", True, True, "/", True, False, None, False, None, None, {}, False))
        self.assertEqual(main._bilibili_cookie_header(jar), "")

    def test_bilibili_api_extracts_creator_subtitle(self):
        responses = [
            FakeResponse({"code": 0, "data": {"title": "测试视频", "owner": {"name": "作者"}, "pages": [{"cid": 123}]}}),
            FakeResponse({"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//example.com/subtitle.json", "ai_type": 0}]}}}),
            FakeResponse({"body": [{"from": 1.25, "to": 3.5, "content": "平台字幕"}]}),
        ]
        with patch.object(main.requests, "get", side_effect=responses):
            result = main._extract_bilibili_api_summary_source("https://www.bilibili.com/video/BV1eKMn6MEHo/")

        self.assertIsNotNone(result)
        info, language, source, segments = result
        self.assertEqual(info["title"], "测试视频")
        self.assertEqual(language, "zh-CN")
        self.assertEqual(source, "manual")
        self.assertEqual(segments[0].text, "平台字幕")

    def test_summary_source_uses_platform_api_before_ytdlp(self):
        expected = ({"title": "API 字幕"}, "zh-CN", "manual", [main.SubtitleSegment(start=1, end=2, timestamp="0:01", text="字幕")])
        with patch.object(main, "_extract_bilibili_api_summary_source", return_value=expected), patch.object(
            main, "YoutubeDL"
        ) as youtube_dl:
            result = main._extract_summary_source("https://www.bilibili.com/video/BV1eKMn6MEHo/")
        self.assertEqual(result, expected)
        youtube_dl.assert_not_called()

    def test_summary_source_reports_when_api_and_ytdlp_have_no_subtitle(self):
        with patch.object(main, "_extract_bilibili_api_summary_source", return_value=None), patch.object(main, "YoutubeDL", NoSubtitleYoutubeDL):
            with self.assertRaisesRegex(RuntimeError, "平台字幕 API 和 yt-dlp 均未找到可用字幕"):
                main._extract_summary_source("https://www.bilibili.com/video/BV1eKMn6MEHo/")

    def test_select_subtitle_ignores_bilibili_danmaku(self):
        info = {
            "subtitles": {
                "danmaku": [
                    {
                        "ext": "xml",
                        "url": "https://comment.bilibili.com/39797786570.xml",
                    }
                ]
            }
        }

        self.assertIsNone(main._select_subtitle(info))

    def test_parse_bilibili_json_subtitle(self):
        content = json.dumps(
            {
                "body": [
                    {"from": 1.25, "to": 3.5, "content": "第一句字幕"},
                    {"from": 3.5, "to": 6.0, "content": "第二句字幕"},
                ]
            },
            ensure_ascii=False,
        )

        segments = main._parse_subtitle_content(content, "json")

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].start, 1.25)
        self.assertEqual(segments[0].end, 3.5)
        self.assertEqual(segments[0].timestamp, "0:01")
        self.assertEqual(segments[0].text, "第一句字幕")

    def test_douyin_summary_falls_back_to_page_text_when_ytdlp_needs_cookies(self):
        parsed = main.ParseResponse(
            title="猫咪今天学会开门",
            thumbnail=None,
            duration=12,
            uploader="小王",
            webpage_url="https://www.douyin.com/video/123456789",
            formats=[],
        )

        with patch.object(main, "YoutubeDL", FailingYoutubeDL), patch.object(main, "parse_video", return_value=parsed):
            info, language, source, segments = main._extract_summary_source("https://www.douyin.com/video/123456789")

        self.assertEqual(info["title"], "猫咪今天学会开门")
        self.assertEqual(info["webpage_url"], "https://www.douyin.com/video/123456789")
        self.assertEqual(language, "zh-CN")
        self.assertEqual(source, "page_text")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].timestamp, "0:00")
        self.assertIn("视频标题：猫咪今天学会开门", segments[0].text)
        self.assertIn("作者：小王", segments[0].text)

    def test_summary_source_label_names_page_text(self):
        self.assertEqual(main._summary_source_label("page_text"), "页面文案")


if __name__ == "__main__":
    unittest.main()
