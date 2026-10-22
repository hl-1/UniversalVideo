import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.music import streaming, sources


class StreamingPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        marker = accounts.principal.set(accounts.Principal(1, 'test', 'user', 'session-a', device_id='a'))
        self.addCleanup(accounts.principal.reset, marker)
        self.redis = Mock()
        self.saved = {}
        self.redis.set.side_effect = lambda key, raw, ex: self.saved.update({key: raw})
        self.redis.get.side_effect = lambda key: self.saved.get(key)
        self.redis.exists.return_value = True
        self.stack.enter_context(patch.object(accounts, 'redis_client', return_value=self.redis))
        self.stack.enter_context(patch.object(accounts, 'register_resource'))
        self.stack.enter_context(patch.object(accounts, 'active_worker'))
        self.stack.enter_context(patch.object(streaming.platform_access, 'require'))
        self.track = sources.Track(title='song', artist='artist', duration=200, provider='qq', identifier='one', webpage_url='https://y.qq.com/song/one')

    def prepare(self):
        with patch.object(sources, 'resolve', return_value=('https://cdn.example.com/song.mp3', {'http_headers': {'Cookie': 'private'}}, 'platform')):
            result = streaming.prepare(self.track)
        return result['playback_url'].split('/')[-2], result

    def test_source_credentials_remain_server_only_and_token_is_session_bound(self):
        token, result = self.prepare()
        self.assertFalse(result['full'])
        self.assertNotIn('Cookie', json.dumps(result))
        self.assertEqual(json.loads(next(iter(self.saved.values())))['sid'], 'session-a')
        ttl = self.redis.set.call_args.kwargs['ex']
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 600)
        marker = accounts.principal.set(accounts.Principal(1, 'test', 'user', 'session-b'))
        try:
            with self.assertRaises(HTTPException) as error: streaming.content(token, SimpleNamespace(headers={}, method='GET'))
            self.assertEqual(error.exception.status_code, 404)
        finally: accounts.principal.reset(marker)

    def test_range_bytes_stream_before_whole_source_and_response_is_closed(self):
        token, _ = self.prepare()
        consumed = []
        def chunks(size):
            consumed.append(1); yield b'first'
            consumed.append(2); yield b'second'
        upstream = SimpleNamespace(status_code=206, headers={'Content-Length':'11','Content-Type':'audio/mpeg','Content-Range':'bytes 0-10/100'}, iter_content=chunks, close=Mock())
        with patch.object(streaming.video_network, 'get', return_value=upstream) as get:
            response = streaming.content(token, SimpleNamespace(headers={'range':'bytes=0-10'}, method='GET'))
            self.assertEqual(get.call_args.kwargs['headers']['Range'], 'bytes=0-10')
            async def read():
                self.assertEqual(await anext(response.body_iterator), b'first')
                self.assertEqual(consumed, [1])
                self.assertEqual(await anext(response.body_iterator), b'second')
                with self.assertRaises(StopAsyncIteration): await anext(response.body_iterator)
            asyncio.run(read())
        upstream.close.assert_called_once()

    def test_expired_token_has_no_upstream_access_and_revoked_session_stops_bytes(self):
        self.assertIsNone(streaming.content('expired', SimpleNamespace(headers={}, method='GET')))
        token, _ = self.prepare()
        self.redis.exists.return_value = False
        upstream = SimpleNamespace(status_code=200, headers={'Content-Type':'audio/mpeg'}, iter_content=lambda size: iter([b'private']), close=Mock())
        with patch.object(streaming.video_network, 'get', return_value=upstream):
            response = streaming.content(token, SimpleNamespace(headers={}, method='GET'))
            async def read():
                with self.assertRaises(RuntimeError): await anext(response.body_iterator)
            asyncio.run(read())
        upstream.close.assert_called_once()
