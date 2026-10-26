from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import ExitStack, suppress
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, WebSocketDisconnect

from backend.modules.social import realtime


class SocialHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    interval = .1

    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(realtime, "HEARTBEAT_INTERVAL_SECONDS", self.interval))
        self.stack.enter_context(patch.dict(realtime.connections, clear=True))
        self.stack.enter_context(patch.dict(realtime.connection_loops, clear=True))
        self.client = MagicMock()
        self.data = {"uid": "903456789012345679", "sid": "test-session", "origin": "http://127.0.0.1:5174",
                     "auth_version": 1, "expires_at": time.time() + 30}
        self.client.eval.return_value = json.dumps(self.data)
        self.stack.enter_context(patch.object(realtime.accounts, "redis_client", return_value=self.client))
        self.check_session = self.stack.enter_context(patch.object(realtime, "check_session"))
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.ws = MagicMock()
        self.ws.query_params = {"ticket": "x" * 32}
        self.ws.headers = {"origin": self.data["origin"]}
        self.ws.accept = AsyncMock()
        self.ws.close = AsyncMock()

        async def receive():
            message = await self.incoming.get()
            if isinstance(message, Exception):
                raise message
            return message

        async def send(message):
            self.outgoing.put_nowait((asyncio.get_running_loop().time(), message))

        self.ws.receive_text = receive
        self.ws.send_json = send
        self.task = asyncio.create_task(realtime.socket(self.ws))
        self.addAsyncCleanup(self.stop_socket)

    async def stop_socket(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    async def heartbeat(self):
        timestamp, message = await asyncio.wait_for(self.outgoing.get(), timeout=2)
        self.assertEqual(message["topic"], "heartbeat")
        self.assertIsInstance(message["server_time"], float)
        return timestamp

    async def test_immediate_replies_do_not_trigger_heartbeats(self):
        previous = await self.heartbeat()
        for _ in range(2):
            self.incoming.put_nowait("{}")
            current = await self.heartbeat()
            self.assertGreaterEqual(current - previous, self.interval * .95)
            previous = current
        self.assertEqual(self.check_session.call_count, 4)  # Handshake plus three scheduled heartbeats.
        self.assertEqual(self.client.pipeline.return_value.__enter__.return_value.execute.call_count, 3)

    async def test_continuous_replies_do_not_advance_or_postpone_heartbeat(self):
        first = await self.heartbeat()

        async def reply():
            while True:
                self.incoming.put_nowait("{}")
                await asyncio.sleep(self.interval / 5)

        replies = asyncio.create_task(reply())
        try:
            second = await self.heartbeat()
            self.assertGreaterEqual(second - first, self.interval * .95)
            self.assertEqual(self.check_session.call_count, 3)
            self.assertEqual(self.client.pipeline.return_value.__enter__.return_value.execute.call_count, 2)
        finally:
            replies.cancel()
            with suppress(asyncio.CancelledError):
                await replies

    async def test_missing_reply_does_not_stop_scheduled_heartbeats(self):
        previous = await self.heartbeat()
        for _ in range(2):
            current = await self.heartbeat()
            self.assertGreaterEqual(current - previous, self.interval * .95)
            previous = current

    async def test_disconnect_during_interval_removes_presence(self):
        await self.heartbeat()
        connection_id, = realtime.connections
        self.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(self.task, timeout=1)
        self.assertNotIn(connection_id, realtime.connections)
        self.assertNotIn(connection_id, realtime.connection_loops)
        self.client.zrem.assert_called_once_with(realtime.s.PREFIX + "user-presence:" + self.data["uid"], connection_id)
        self.ws.close.assert_not_awaited()
        self.assertTrue(self.outgoing.empty())

    async def test_oversized_message_closes_during_interval(self):
        await self.heartbeat()
        self.incoming.put_nowait("x" * 2049)
        await asyncio.wait_for(self.task, timeout=1)
        self.ws.close.assert_awaited_once_with(code=1009)
        self.assertFalse(realtime.connections)
        self.client.zrem.assert_called_once()
        self.assertTrue(self.outgoing.empty())

    async def test_session_is_rechecked_on_next_heartbeat(self):
        await self.heartbeat()
        self.check_session.side_effect = HTTPException(401, "会话已撤销。")
        self.incoming.put_nowait("{}")
        await asyncio.wait_for(self.task, timeout=2)
        self.ws.close.assert_awaited_once_with(code=4401)
        self.assertFalse(realtime.connections)
        self.client.zrem.assert_called_once()
        self.assertTrue(self.outgoing.empty())
