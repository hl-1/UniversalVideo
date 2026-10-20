import copy
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.modules.accounts import service as accounts
from backend.modules.video import preview, preview_assets as assets, preview_cache, schemas


class PreviewReuseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(assets, 'ROOT', self.root))
        self.stack.enter_context(patch.object(assets, 'jobs', {}))
        self.stack.enter_context(patch.object(assets, '_start'))
        self.stack.enter_context(patch.object(accounts, 'active_worker'))
        self.stack.enter_context(patch.object(assets.history, 'owned_record', return_value={}))
        self.stack.enter_context(patch.object(preview.video_history, 'validate_relation'))
        self.stack.enter_context(patch.object(assets.platform_access, 'metadata_for', return_value={}))
        self.metadata = {}

        def put(key, value, ttl=600, *, only_existing=False):
            if only_existing and key not in self.metadata:
                return False
            self.metadata[key] = copy.deepcopy(value)
            return True

        self.stack.enter_context(patch.object(assets.cache, 'put', side_effect=put))
        self.stack.enter_context(patch.object(assets.cache, 'get', side_effect=lambda key: copy.deepcopy(self.metadata.get(key))))
        self.stack.enter_context(patch.object(assets.cache, 'remove', side_effect=lambda key: self.metadata.pop(key, None)))
        marker = accounts.principal.set(accounts.Principal(42, 'owner', 'super_admin'))
        self.addCleanup(accounts.principal.reset, marker)
        self.page = 'https://example.com/watch/123'
        app = FastAPI()
        app.include_router(preview.router)
        self.client = TestClient(app)

    def ready(self, *, complete=True, delivery='file'):
        response = assets.create('https://cdn.example.com/video.mp4', None, self.page, 'record', selection='720p')
        job = assets.jobs[response['preview_id']]
        job.directory.mkdir()
        (job.directory / 'preview.mp4').write_bytes(b'prepared local media')
        assets._publish(job, True, 10, delivery, complete=complete)
        return job

    def test_completed_media_is_reused_after_cancellation_without_reextracting(self):
        original = self.ready()
        assets.cancel(original.token)
        self.assertFalse(original.directory.exists())
        with patch.object(preview.sources, 'resolve', side_effect=AssertionError('must not extract')):
            first = self.client.post('/api/previews', json=dict(url=self.page, format_id='720p', parse_record_id='record'))
            second = self.client.post('/api/previews/reuse', json=dict(url=self.page, format_id='720p', parse_record_id='record'))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        a, b = first.json(), second.json()
        self.assertTrue(a['reused'])
        self.assertNotEqual(a['preview_id'], b['preview_id'])
        self.client.delete(a['status_url']).raise_for_status()
        content = self.client.get(b['preview_url'], headers={'Range': 'bytes=0-7'})
        self.assertEqual(content.status_code, 206)
        self.assertEqual(content.content, b'prepared')
        self.assertLessEqual(b['expires_in'], preview_cache.TTL)

    def test_cache_is_scoped_to_owner_record_page_and_format(self):
        self.ready()
        self.assertIsNone(assets.reuse(self.page, '1080p', 'record'))
        self.assertIsNone(assets.reuse(self.page, '720p', 'other'))
        self.assertIsNone(assets.reuse('https://example.com/watch/other', '720p', 'record'))
        marker = accounts.principal.set(accounts.Principal(43, 'other', 'super_admin'))
        try:
            self.assertIsNone(assets.reuse(self.page, '720p', 'record'))
        finally:
            accounts.principal.reset(marker)
        self.assertIsNotNone(assets.reuse(self.page, None, 'record'))

    def test_unfinished_and_remote_proxy_results_are_not_cached(self):
        unfinished = self.ready(complete=False, delivery='fragmented')
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))
        assets.cancel(unfinished.token)
        self.ready(delivery='proxy')
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))

    def test_expired_cache_is_a_miss_and_does_not_extend_existing_session(self):
        self.ready()
        reused = assets.reuse(self.page, '720p', 'record')
        original_expiry = assets.jobs[reused['preview_id']].value['expires_at']
        directory, metadata = preview_cache.find(self.root, 42, 'record', self.page, '720p')
        metadata['expires_at'] = time.time() - 1
        (directory / 'manifest.json').write_text(json.dumps(metadata))
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))
        self.assertFalse(directory.exists())
        self.assertEqual(assets.jobs[reused['preview_id']].value['expires_at'], original_expiry)
        self.assertEqual(self.client.get(reused['preview_url']).content, b'prepared local media')

    def test_missing_cached_file_is_a_miss(self):
        self.ready()
        directory, _ = preview_cache.find(self.root, 42, 'record', self.page, '720p')
        (directory / 'preview.mp4').unlink()
        response = self.client.post('/api/previews/reuse', json=dict(url=self.page, parse_record_id='record'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json())

    def test_record_deletion_and_account_cleanup_purge_cached_files(self):
        self.ready()
        reused = assets.reuse(self.page, '720p', 'record')
        assets.cancel_record(42, 'other')
        self.assertIsNotNone(preview_cache.find(self.root, 42, 'record', self.page, '720p'))
        assets.cancel_record(42, 'record')
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))
        self.assertEqual(self.client.get(reused['status_url']).status_code, 404)
        self.ready()
        assets.cancel_user(42)
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))

    def test_force_prepare_skips_reuse_and_source_is_validated(self):
        self.ready()
        with patch.object(preview.sources, 'resolve', side_effect=HTTPException(404, 'Expired source')) as resolve:
            response = self.client.post('/api/previews', json=dict(url=self.page, format_id='720p', parse_record_id='record', force_prepare=True))
        self.assertEqual(response.status_code, 404)
        resolve.assert_called_once()

    def test_platform_permissions_are_rechecked_for_cached_media(self):
        original = self.ready()
        original.value.update(access_scope='super_admin', source_platform='youtube')
        preview_cache.store(self.root, original.directory / 'preview.mp4', original.value, '720p')
        marker = accounts.principal.set(accounts.Principal(42, 'owner', 'user'))
        try:
            with self.assertRaises(HTTPException) as caught:
                assets.reuse(self.page, '720p', 'record')
            self.assertEqual(caught.exception.status_code, 403)
        finally:
            accounts.principal.reset(marker)

    def test_cache_has_a_fixed_expiry_and_bounded_storage(self):
        original = self.ready()
        assets.cancel(original.token)
        directory, metadata = preview_cache.find(self.root, 42, 'record', self.page, '720p')
        assets.reuse(self.page, '720p', 'record')
        self.assertEqual(json.loads((directory / 'manifest.json').read_text())['expires_at'], metadata['expires_at'])
        with patch.object(preview_cache, 'MAX_BYTES', 1):
            preview_cache.cleanup(self.root)
        self.assertIsNone(assets.reuse(self.page, '720p', 'record'))
