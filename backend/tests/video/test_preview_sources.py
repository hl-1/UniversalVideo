import io
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
from urllib3.response import HTTPResponse

from backend.modules.video import formats, preview_sources as sources


def upstream(url, body=b'test', status=200, headers=None):
    response = requests.Response()
    response.url, response.status_code, response.reason = url, status, 'OK'
    response.headers.update({'Content-Length': str(len(body)), **(headers or {})})
    response.raw = HTTPResponse(body=io.BytesIO(body), headers=response.headers, status=status, preload_content=False)
    return response


class PreviewNetworkTests(unittest.TestCase):
    def test_transient_tls_failure_retries_and_revalidates_the_destination(self):
        response = upstream('https://example.com/video')
        with sources.PreviewYoutubeDL({'quiet': True}, lambda: None, lambda size: None) as ydl, \
             patch.object(sources.video_network, 'validate', side_effect=lambda url: url) as validate, \
             patch.object(ydl.session, 'request', side_effect=[requests.exceptions.SSLError('UNEXPECTED_EOF'), response]) as request:
            with ydl.urlopen('https://example.com/video') as stream:
                self.assertEqual(stream.read(), b'test')
            self.assertEqual(request.call_count, 2)
            self.assertEqual(validate.call_count, 2)

    def test_retry_limit_preserves_safe_error_details_and_allows_cancellation(self):
        operation, wait = Mock(side_effect=requests.exceptions.SSLError('UNEXPECTED_EOF https://example.com/?token=secret')), Mock()
        with self.assertRaises(sources.PreparationError) as caught:
            sources.retry_network(operation, lambda: None, wait=wait)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(caught.exception.code, 'network_tls')
        self.assertNotIn('secret', caught.exception.message)
        self.assertEqual(wait.call_count, 2)
        def cancel(delay):
            raise sources.PreparationError('cancelled', 'cancelled')
        operation.reset_mock()
        with self.assertRaises(sources.PreparationError) as caught:
            sources.retry_network(operation, lambda: None, wait=cancel)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(operation.call_count, 1)

    def test_certificate_failures_are_not_retried_or_bypassed(self):
        operation = Mock(side_effect=requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'))
        with self.assertRaises(sources.PreparationError) as caught:
            sources.retry_network(operation, lambda: None, wait=Mock())
        self.assertEqual(caught.exception.code, 'network_tls')
        self.assertEqual(operation.call_count, 1)

    def test_post_requests_are_not_automatically_repeated(self):
        with sources.PreviewYoutubeDL({'quiet': True}, lambda: None, lambda size: None) as ydl, \
             patch.object(sources.video_network, 'validate', side_effect=lambda url: url), \
             patch.object(ydl.session, 'request', side_effect=requests.exceptions.Timeout()) as request:
            with self.assertRaises(sources.PreparationError) as caught:
                ydl.urlopen(sources.Request('https://example.com/api', data=b'payload'))
            self.assertEqual(caught.exception.code, 'network_timeout')
            self.assertEqual(request.call_count, 1)

    def test_wrapped_transport_errors_keep_their_network_cause(self):
        error = sources.TransportError(cause=requests.exceptions.ProxyError('proxy credentials'))
        self.assertEqual(sources.network_failure(error).code, 'network_proxy')

    def test_reader_respects_the_platform_proxy_option(self):
        for options, expected in [({'quiet': True, 'proxy': ''}, False), ({'quiet': True}, True)]:
            with sources.PreviewYoutubeDL(options, lambda: None, lambda size: None) as ydl:
                self.assertEqual(ydl.session.trust_env, expected)

    def test_private_redirect_is_rejected_before_another_request(self):
        redirect = upstream('https://example.com/video', status=302, headers={'Location': 'http://127.0.0.1/internal'})
        def validate(url):
            if '127.0.0.1' in url:
                raise HTTPException(422, 'private address')
        with sources.PreviewYoutubeDL({'quiet': True}, lambda: None, lambda size: None) as ydl, \
             patch.object(sources.network, 'public_url', side_effect=validate) as check, \
             patch.object(ydl.session, 'request', return_value=redirect) as request:
            with self.assertRaises(HTTPException):
                ydl.urlopen('https://example.com/video')
            self.assertEqual(request.call_count, 1)
            self.assertEqual(check.call_count, 2)
            self.assertTrue(redirect.raw.closed)

    def test_cross_host_redirect_drops_credentials_and_converts_post(self):
        redirect = upstream('https://example.com/api', status=302, headers={'Location': 'https://cdn.example.net/video'})
        final = upstream('https://cdn.example.net/video')
        with sources.PreviewYoutubeDL({'quiet': True}, lambda: None, lambda size: None) as ydl, \
             patch.object(sources.network, 'public_url', side_effect=lambda url: url), \
             patch.object(ydl.session, 'request', side_effect=[redirect, final]) as request:
            req = sources.Request('https://example.com/api', data=b'payload', headers={'Cookie': 'secret', 'Authorization': 'secret'})
            with ydl.urlopen(req) as response:
                self.assertEqual(response.read(), b'test')
            self.assertEqual(request.call_args.args[0], 'GET')
            self.assertIsNone(request.call_args.kwargs['data'])
            self.assertFalse(any(key.lower() in {'cookie', 'authorization'} for key in request.call_args.kwargs['headers']))

    def test_native_reader_enforces_byte_limit_and_closes_response(self):
        received = 0
        def receive(size):
            nonlocal received
            received += size
            if received > 64:
                raise sources.PreparationError('size', 'limit')
        response = upstream('https://example.com/video', b'x' * 128)
        with sources.PreviewYoutubeDL({'quiet': True}, lambda: None, receive) as ydl, \
             patch.object(sources.network, 'public_url', side_effect=lambda url: url), \
             patch.object(ydl.session, 'request', return_value=response):
            with self.assertRaises(sources.PreparationError) as caught:
                with ydl.urlopen('https://example.com/video') as stream:
                    stream.read(128)
            self.assertEqual(caught.exception.code, 'size')
        self.assertTrue(response.raw.closed)

    def test_cancelled_request_does_not_start_network_access(self):
        def check():
            raise sources.PreparationError('cancelled', 'cancelled')
        with sources.PreviewYoutubeDL({'quiet': True}, check, lambda size: None) as ydl, \
             patch.object(ydl.session, 'request') as request:
            with self.assertRaises(sources.PreparationError):
                ydl.urlopen('https://example.com/video')
            request.assert_not_called()

    def test_live_and_drm_hls_never_fall_back_to_remote_ffmpeg(self):
        for manifest, code in [('#EXTM3U\n#EXTINF:1,\nvideo.ts\n', 'live'),
                               ('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"\n#EXT-X-ENDLIST\n', 'drm')]:
            with self.subTest(code=code), \
                 sources.PreviewYoutubeDL({'quiet': True}, lambda: None, lambda size: None) as ydl, \
                 patch.object(sources.network, 'public_url', side_effect=lambda url: url), \
                 patch.object(ydl.session, 'request', return_value=upstream('https://example.com/video.m3u8', manifest.encode())):
                fd = sources.NativeHlsFD(ydl, {})
                with self.assertRaises(sources.PreparationError) as caught:
                    fd.real_download('unused', {'url': 'https://example.com/video.m3u8'})
                self.assertEqual(caught.exception.code, code)


class PreviewFormatMetadataTests(unittest.TestCase):
    def test_single_file_results_produce_a_preview_candidate(self):
        candidates = formats._extract_formats({'url': 'https://example.com/video.mp4', 'ext': 'mp4'})
        self.assertEqual(candidates[0].format_id, 'best')
        self.assertIsNone(candidates[0].has_video)

    def test_video_and_audio_webm_formats_can_be_distinguished(self):
        candidates = formats._extract_formats({'formats': [
            {'format_id': 'video', 'vcodec': 'vp9', 'acodec': 'none', 'ext': 'webm'},
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'opus', 'ext': 'webm'}]})
        self.assertEqual({item.format_id: item.has_video for item in candidates},
                         {'video+bestaudio/best': True, 'audio': False})
