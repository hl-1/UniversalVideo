import asyncio
import unittest
import time
from urllib.parse import quote
from unittest.mock import patch

from fastapi import HTTPException

from backend import main


class FakeUpstreamResponse:
    def __init__(self, status_code=206, content_type="video/mp4", body=b"test"):
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body) * 2}",
            "Accept-Ranges": "bytes",
        }
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        yield self.body

    def close(self):
        self.closed = True


class PreviewSessionTests(unittest.TestCase):
    def setUp(self):
        with main.sniff_cache_lock:
            main.sniff_cache.clear()
        if hasattr(main, "preview_sessions_lock"):
            with main.preview_sessions_lock:
                main.preview_sessions.clear()

    def _cache_format(self, page_url: str, media_url: str, ext: str = "mp4") -> str:
        format_id = f"sniff:{quote(media_url, safe='')}"
        main._set_sniff_cache(
            main.normalize_url(page_url),
            main.ParseResponse(
                title="抖音预览测试",
                webpage_url=page_url,
                formats=[
                    main.FormatInfo(
                        format_id=format_id,
                        label=f"{ext.upper()} · 高清线路",
                        ext=ext,
                        resolution="自动",
                    )
                ],
            ),
        )
        return format_id

    def test_create_preview_accepts_cached_douyin_mp4(self):
        page_url = "https://www.douyin.com/video/123456789"
        media_url = "https://v3-dy-o-abtest.zjcdn.com/video/tos/test.mp4"
        format_id = self._cache_format(page_url, media_url)

        response = main.create_preview(main.PreviewRequest(url=page_url, format_id=format_id))

        self.assertTrue(response.preview_url.startswith("/api/previews/"))
        self.assertTrue(response.preview_url.endswith("/content"))
        self.assertEqual(response.expires_in, 600)

    def test_create_preview_rejects_non_douyin_url(self):
        request = main.PreviewRequest(
            url="https://example.com/video/123",
            format_id="sniff:https%3A%2F%2Fcdn.example.com%2Fvideo.mp4",
        )

        with self.assertRaises(HTTPException) as raised:
            main.create_preview(request)

        self.assertEqual(raised.exception.status_code, 422)

    def test_create_preview_rejects_uncached_sniff_format(self):
        request = main.PreviewRequest(
            url="https://www.douyin.com/video/123456789",
            format_id="sniff:https%3A%2F%2Fv3-dy-o-abtest.zjcdn.com%2Fvideo%2Funknown.mp4",
        )

        with self.assertRaises(HTTPException) as raised:
            main.create_preview(request)

        self.assertEqual(raised.exception.status_code, 404)

    def test_create_preview_rejects_hls_format(self):
        page_url = "https://www.douyin.com/video/123456789"
        media_url = "https://v3-dy-o-abtest.zjcdn.com/video/tos/test.m3u8"
        format_id = self._cache_format(page_url, media_url, ext="m3u8")

        with self.assertRaises(HTTPException) as raised:
            main.create_preview(main.PreviewRequest(url=page_url, format_id=format_id))

        self.assertEqual(raised.exception.status_code, 422)


class PreviewContentTests(unittest.TestCase):
    def setUp(self):
        with main.preview_sessions_lock:
            main.preview_sessions.clear()

    def _store_session(self, token="preview-token", expires_at=None):
        with main.preview_sessions_lock:
            main.preview_sessions[token] = main.PreviewSession(
                media_url="https://v3-dy-o-abtest.zjcdn.com/video/tos/test.mp4",
                referer="https://www.douyin.com/video/123456789",
                expires_at=expires_at if expires_at is not None else time.time() + 60,
            )
        return token

    def test_preview_content_forwards_range_and_streams_partial_video(self):
        token = self._store_session()
        upstream = FakeUpstreamResponse()

        with patch.object(main.requests, "get", return_value=upstream) as get:
            response = main.preview_content(token, range_header="bytes=0-3")

        async def read_body():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-3/8")
        self.assertEqual(asyncio.run(read_body()), b"test")
        self.assertTrue(upstream.closed)
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=0-3")

    def test_preview_content_rejects_unknown_token(self):
        with self.assertRaises(HTTPException) as raised:
            main.preview_content("missing-token")

        self.assertEqual(raised.exception.status_code, 404)

    def test_preview_content_rejects_expired_token(self):
        token = self._store_session(expires_at=time.time() - 1)

        with self.assertRaises(HTTPException) as raised:
            main.preview_content(token)

        self.assertEqual(raised.exception.status_code, 410)

    def test_preview_content_rejects_multiple_ranges(self):
        token = self._store_session()

        with self.assertRaises(HTTPException) as raised:
            main.preview_content(token, range_header="bytes=0-3,8-11")

        self.assertEqual(raised.exception.status_code, 416)

    def test_preview_content_rejects_non_video_upstream(self):
        token = self._store_session()
        upstream = FakeUpstreamResponse(status_code=200, content_type="image/png")

        with patch.object(main.requests, "get", return_value=upstream):
            with self.assertRaises(HTTPException) as raised:
                main.preview_content(token)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertTrue(upstream.closed)

if __name__ == "__main__":
    unittest.main()
