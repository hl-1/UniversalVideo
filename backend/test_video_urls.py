import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend import main


class DouyinUrlTests(unittest.TestCase):
    def test_search_pages_are_rejected_without_browser_or_downloader(self):
        urls = (
            "https://www.douyin.com/search/ai%E8%A7%86%E9%A2%91?aid=a3231f12-82af-4ada-abe6-5cb0bda3e2c7&type=general",
            "https://www.douyin.com/search?keyword=ai",
            "https://www.douyin.com/search/",
            "https://www.douyin.com/search/1234567890123456789?aid=9876543210123456789",
        )
        for url in urls:
            with self.subTest(url=url), patch.object(main, "parse_browser_sniffed_page") as browser, \
                    patch.object(main, "YoutubeDL") as downloader:
                with self.assertRaises(HTTPException) as raised:
                    main.parse_video(url)
                self.assertEqual(raised.exception.status_code, 422)
                self.assertIn("搜索结果页", raised.exception.detail)
                browser.assert_not_called()
                downloader.assert_not_called()

    def test_search_keywords_and_tracking_ids_are_not_video_ids(self):
        url = "https://www.douyin.com/search/1234567890123456789?aid=9876543210123456789"
        self.assertIsNone(main._extract_douyin_video_id(url))
        self.assertEqual(main.normalize_url(url), url)

    def test_search_results_with_an_opened_video_keep_working(self):
        canonical = "https://www.douyin.com/video/7491234567890123456"
        result = main.ParseResponse(title="test", webpage_url=canonical, formats=[])
        for parameter in ("modal_id", "item_ids", "group_id", "aweme_id"):
            url = f"https://www.douyin.com/search/ai?{parameter}=7491234567890123456"
            with self.subTest(parameter=parameter), patch.object(main, "parse_browser_sniffed_page", return_value=result) as browser:
                self.assertEqual(main.parse_video(url), result)
                browser.assert_called_once_with(canonical)

    def test_video_and_share_links_keep_existing_normalization(self):
        cases = (
            ("https://www.douyin.com/video/7491234567890123456?extra=1", "https://www.douyin.com/video/7491234567890123456"),
            ("https://v.douyin.com/abc123/", "https://v.douyin.com/abc123/"),
        )
        for original, expected in cases:
            with self.subTest(url=original):
                self.assertEqual(main.normalize_url(original), expected)


if __name__ == "__main__":
    unittest.main()
