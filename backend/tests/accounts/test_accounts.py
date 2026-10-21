from __future__ import annotations

import io
import json
import secrets
import tempfile
import time
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import jwt
import pymysql
import redis
from fastapi.testclient import TestClient

from backend import application
from backend.infrastructure import browser_sessions as browser_session_store
from backend.infrastructure import database as database_store
from backend.infrastructure import network
from backend.core import paths, storage
from backend.modules.video import tasks as video_tasks
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import sources as music
from backend.modules.music import lyrics as music_lyrics
from backend.modules.watermark import api as watermark


class AccountIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.original_ready = database_store.schema_ready
        config = database_store.database_config()
        cls.db_name = 'videodream_auth_test_' + secrets.token_hex(5)
        cls.connection = pymysql.connect(**{key: value for key, value in config.items() if key != 'database'})
        with cls.connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE `{cls.db_name}` CHARACTER SET utf8mb4')
        cls.stack.enter_context(patch.object(database_store, 'database_config', return_value=config | {'database': cls.db_name}))
        cls.folder = cls.stack.enter_context(tempfile.TemporaryDirectory())
        cls.root = Path(cls.folder)
        for module in (library, paths, watermark, music_lyrics):
            cls.stack.enter_context(patch.object(module, 'DOWNLOAD_DIR', cls.root))
        for field, subdir in (('SUMMARY_DIR', '_summaries'), ('THUMBNAIL_DIR', '_thumbnails')):
            path = cls.root / subdir
            path.mkdir()
            cls.stack.enter_context(patch.object(paths, field, path))
        cls.stack.enter_context(patch.object(accounts, 'signing_key', return_value=secrets.token_urlsafe(48)))
        database_store.schema_ready = False
        with library.database() as cursor:
            cursor.execute('UPDATE users SET password_hash=%s,must_change_password=FALSE WHERE id=1', (accounts.passwords.hash('Test-admin-123!'),))
        cls.a_id = accounts.create_user(accounts.Credentials(username='user_a', password='Test-user-123!'))
        cls.b_id = accounts.create_user(accounts.Credentials(username='user_b', password='Test-user-123!'))

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        database_store.schema_ready = cls.original_ready
        with cls.connection.cursor() as cursor:
            cursor.execute(f'DROP DATABASE `{cls.db_name}`')
        cls.connection.close()

    def setUp(self):
        self.stack = ExitStack()
        from backend.infrastructure.browser_sessions import BrowserSessions
        original_browser = browser_session_store.sessions
        browser = BrowserSessions(self.root / ('browser-' + secrets.token_hex(4)))
        self.stack.enter_context(patch.object(browser_session_store, 'sessions', browser))
        callbacks = [browser.cancel_user if getattr(callback, '__self__', None) is original_browser else callback for callback in accounts.cancel_callbacks]
        self.stack.enter_context(patch.object(accounts, 'cancel_callbacks', callbacks))
        self.prefix = 'videodream:test:' + secrets.token_hex(8) + ':'
        self.stack.enter_context(patch.object(accounts, 'redis_prefix', return_value=self.prefix))
        self.stack.enter_context(patch.object(network, 'public_url', side_effect=lambda value: value))
        self.stack.enter_context(patch.object(video_tasks.executor, 'submit'))
        self.stack.enter_context(patch.object(video_tasks.ai_executor, 'submit'))
        self.stack.enter_context(patch.object(video_tasks.music_batch_executor, 'submit'))
        with library.database() as cursor:
            from backend.modules.video import store as video_store
            for table in ('video_recent_links', *(item[0] for item in video_store.TABLES.values())):
                cursor.execute(f'DELETE FROM {table}')
            for table in ('music_playlists', 'music_favorites', 'music_history', 'music_downloads', 'music_lyrics', 'music_tracks', 'music_theme', 'resources', 'audit_logs'):
                cursor.execute(f'DELETE FROM {table}')
            cursor.execute("DELETE FROM users WHERE id NOT IN (1,%s,%s)", (self.a_id, self.b_id))
            cursor.execute("UPDATE users SET role=IF(id=1,'super_admin','user'),status='active',must_change_password=FALSE,auth_version=0,password_hash=%s WHERE id<>1", (accounts.passwords.hash('Test-user-123!'),))
            cursor.execute('UPDATE users SET password_hash=%s WHERE id=1', (accounts.passwords.hash('Test-admin-123!'),))
            cursor.execute('UPDATE system_settings SET config=%s WHERE id=1', (json.dumps(accounts.DEFAULT_SETTINGS),))
        video_tasks.tasks.clear()
        video_tasks.summary_tasks.clear()
        video_tasks.mind_map_tasks.clear()
        video_tasks.music_batches.clear()
        watermark.jobs.clear()
        self.admin = self.client('admin', 'Test-admin-123!')
        self.a = self.client('user_a', 'Test-user-123!')
        self.b = self.client('user_b', 'Test-user-123!')
        self.track = {'title': 'Shared public song', 'artist': 'artist', 'provider': 'qq', 'identifier': 'song1', 'webpage_url': 'https://y.qq.com/n/ryqq/songDetail/song1'}

    def tearDown(self):
        client = accounts.redis_client()
        keys = list(client.scan_iter(match=self.prefix + '*'))
        if keys:
            client.delete(*keys)
        self.stack.close()

    def anonymous(self):
        return self.stack.enter_context(TestClient(application.app, headers={'Origin': 'http://127.0.0.1:5174', 'X-VideoDream-CSRF': '1'}))

    def client(self, name, password):
        client = self.anonymous()
        response = client.post('/api/auth/login', json={'username': name, 'password': password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        return client

    def import_audio(self, client):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\0\0' * 8000)
        response = client.post('/api/library/import', files={'file': ('song.wav', buffer.getvalue(), 'audio/wav')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['filename']

    def test_anonymous_and_user_admin_access_are_blocked(self):
        for path in ('/api/library', '/api/music/search?q=song', '/api/watermarks/videos', '/api/files/unknown.wav', '/api/admin/users'):
            self.assertEqual(self.anonymous().get(path).status_code, 401, path)
        for path in ('users', 'settings', 'logs', 'statistics', 'data'):
            self.assertEqual(self.a.get('/api/admin/' + path).status_code, 403, path)

    def test_all_tracks_includes_indexed_songs_and_isolates_accounts(self):
        a_key = self.a.post('/api/library/tracks', json=self.track).json()['id']
        b_key = self.b.post('/api/library/tracks', json=self.track).json()['id']
        self.assertNotEqual(a_key, b_key)
        self.assertEqual(self.a.get('/api/library/tracks?view=history').json()['tracks'], [])
        for client, expected in ((self.a, a_key), (self.b, b_key)):
            response = client.get('/api/library/tracks?view=all')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([track['id'] for track in response.json()['tracks']], [expected])
        self.assertEqual(self.admin.get('/api/library/tracks?view=all').json()['tracks'], [])

    def test_library_isolation_and_guessed_identifiers(self):
        key = self.a.put('/api/library/favorites', json={'track': self.track, 'liked': True}).json()['id']
        self.a.post('/api/library/history', json={'track': self.track}).raise_for_status()
        other_key = self.b.put('/api/library/favorites', json={'track': self.track, 'liked': True}).json()['id']
        self.assertNotEqual(key, other_key)
        playlist = self.a.post('/api/library/playlists', json={'name': 'Private list'}).json()['id']
        self.a.post(f'/api/library/playlists/{playlist}/tracks', json=self.track).raise_for_status()
        for method, path, kwargs in (
            ('get', f'/api/library/tracks?view=playlist&playlist_id={playlist}', {}),
            ('patch', f'/api/library/playlists/{playlist}', {'json': {'name': 'stolen'}}),
            ('delete', f'/api/library/playlists/{playlist}', {}),
            ('delete', f'/api/library/tracks/{key}/cover', {}),
            ('delete', f'/api/library/history/{key}', {})):
            self.assertEqual(getattr(self.b, method)(path, **kwargs).status_code, 404)
        self.b.delete('/api/library/history').raise_for_status()
        self.assertEqual(len(self.a.get('/api/library/tracks?view=history').json()['tracks']), 1)
        self.assertEqual(self.b.get('/api/library').json()['playlists'], [])
        self.assertEqual(self.admin.get('/api/admin/data?kind=favorites').json()['total'], 2)

    def test_files_range_lyrics_and_duplicate_content_remain_private(self):
        filename = self.import_audio(self.a)
        self.import_audio(self.b)
        self.assertEqual(self.a.get('/api/files/' + filename, headers={'Range': 'bytes=0-31'}).status_code, 206)
        self.assertEqual(self.b.get('/api/files/' + filename, headers={'Range': 'bytes=0-31'}).status_code, 404)
        self.assertEqual(self.admin.get('/api/files/' + filename).status_code, 200)
        self.assertEqual(self.b.put('/api/music/lyrics/' + filename, content=b'[00:01]stolen').status_code, 404)
        self.assertEqual(self.b.post('/api/music/lyrics/fetch', json={'filename': filename, 'title': 'song', 'artist': 'artist'}).status_code, 404)
        self.assertEqual(self.b.put('/api/library/favorites', json={'liked': True, 'track': {'title': 'stolen', 'provider': 'local', 'webpage_url': '/api/files/' + filename}}).status_code, 404)
        a_tracks = self.a.get('/api/library/tracks?view=downloads').json()['tracks']
        b_tracks = self.b.get('/api/library/tracks?view=downloads').json()['tracks']
        self.assertNotEqual(a_tracks[0]['id'], b_tracks[0]['id'])

    def test_media_cookie_supports_native_playback_head_and_range_but_not_business(self):
        filename = self.import_audio(self.a)
        self.assertTrue(storage.path('file', filename, uid=self.a_id).is_file())
        self.a.headers.pop('Authorization')
        self.b.headers.pop('Authorization')
        url = '/api/files/' + filename
        response = self.a.get(url, headers={'Range': 'bytes=0-31'})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(len(response.content), 32)
        self.assertIn('inline', response.headers['content-disposition'])
        self.assertEqual(self.a.head(url).status_code, 200)
        self.assertEqual(self.a.head(url).content, b'')
        self.assertEqual(self.b.get(url).status_code, 404)
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        self.assertEqual(self.a.delete('/api/workspace/resources/file/' + filename).status_code, 401)
        self.assertEqual(self.a.get(url, headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertIn('attachment', self.a.get(url + '?download=1').headers['content-disposition'])
        self.assertEqual(self.a.get(url, headers={'Range': 'bytes=999999-'}).status_code, 416)

    def device_download(self):
        response = self.a.post('/api/downloads/plan', json={
            'url': 'https://example.com/song', 'title': 'Here For It All',
            'media_type': 'audio', 'audio_format': 'mp3', 'compatibility': True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()
        task = video_tasks.tasks[plan['task_id']]
        name = task.task_id + '.Here For It All.mp3'
        data = b'complete validated audio fixture'
        (task.directory() / name).write_bytes(data)
        video_tasks._set_task(task, status='finished', progress=100, filename=name)
        return [plan['url'], f"/api/downloads/tasks/{task.task_id}/file"], plan['download_id'], data

    def test_native_download_cookie_delivers_prepared_file_with_head_and_range(self):
        urls, identity, data = self.device_download()
        self.a.headers.pop('Authorization')
        self.b.headers.pop('Authorization')
        anonymous = self.anonymous()
        for url in urls:
            with self.subTest(url=url):
                response = self.a.get(url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, data)
                self.assertIn('attachment', response.headers['content-disposition'])
                from urllib.parse import unquote
                self.assertIn('Here For It All.mp3', unquote(response.headers['content-disposition']))
                self.assertNotRegex(unquote(response.headers['content-disposition']), r"[a-f0-9]{12}\.Here For It All")
                self.assertEqual(response.headers['content-length'], str(len(data)))
                self.assertEqual(self.a.head(url).status_code, 200)
                self.assertEqual(self.a.head(url).content, b'')
                partial = self.a.get(url, headers={'Range': 'bytes=3-8'})
                self.assertEqual(partial.status_code, 206, partial.text)
                self.assertEqual(partial.content, data[3:9])
                self.assertEqual(self.a.get(url, headers={'Authorization': 'Bearer invalid'}).status_code, 401)
                self.assertEqual(anonymous.get(url).status_code, 401)
                self.assertIn(self.b.get(url).status_code, (404, 410))
        self.assertEqual(self.a.get(f'/api/downloads/{identity}/state').status_code, 401)
        self.assertEqual(self.a.post(f'/api/downloads/{identity}/complete', json={'status': 'completed', 'completion_source': 'filesystem'}).status_code, 401)
        self.assertEqual(self.a.post('/api/downloads/plan', json={'url': 'https://example.com/song'}).status_code, 401)
        sid = self.a.cookies.get(accounts.COOKIE).split('.')[0]
        accounts.redis_client().delete(accounts.session_key(sid))
        for url in urls:
            self.assertEqual(self.a.get(url).status_code, 401)

    def test_native_legacy_archive_uses_media_cookie_without_authorizing_other_download_routes(self):
        from backend.modules.delivery import api as delivery_api
        response = self.a.post('/api/downloads/batches', json={'tracks': [self.track]})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()['url']
        self.a.headers.pop('Authorization')
        self.b.headers.pop('Authorization')
        with patch.object(delivery_api, 'zip_chunks', return_value=iter([b'archive fixture'])) as stream:
            response = self.a.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b'archive fixture')
            self.assertEqual(response.headers['content-type'], 'application/zip')
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertEqual(self.a.head(url).status_code, 200)
            stream.assert_called_once()
            self.assertIn(self.b.get(url).status_code, (404, 410))
            self.assertEqual(self.anonymous().get(url).status_code, 401)
        self.assertEqual(self.a.post('/api/downloads/batches', json={'tracks': [self.track]}).status_code, 401)
        for path in ('/api/downloads/anything/state', '/api/downloads/tasks/anything', '/api/downloads/batches/anything'):
            self.assertEqual(self.a.get(path).status_code, 401)

    def test_media_jwt_has_separate_audience_and_refresh_does_not_extend_session(self):
        token = self.a.cookies.get(accounts.MEDIA_COOKIE)
        claims = jwt.decode(token, accounts.signing_key(), algorithms=['HS256'], audience=accounts.MEDIA_AUDIENCE, issuer='videodream')
        self.assertEqual(claims['sub'], str(self.a_id))
        self.assertEqual(self.a.get('/api/library', headers={'Authorization': 'Bearer ' + token}).status_code, 401)
        accounts.redis_client().expire(accounts.session_key(claims['sid']), 90)
        self.a.post('/api/auth/refresh').raise_for_status()
        refreshed = jwt.decode(self.a.cookies.get(accounts.MEDIA_COOKIE), accounts.signing_key(), algorithms=['HS256'], audience=accounts.MEDIA_AUDIENCE, issuer='videodream')
        self.assertLessEqual(refreshed['exp'] - int(time.time()), 90)
        # Rotating the refresh secret does not revoke an already-issued media JWT.
        accounts.authenticate(token, audience=accounts.MEDIA_AUDIENCE)

    def test_cookie_playback_revokes_on_logout_and_fails_closed_on_redis_outage(self):
        filename = self.import_audio(self.a)
        old = self.a.cookies.get(accounts.MEDIA_COOKIE)
        self.a.headers.pop('Authorization')
        with patch.object(accounts, 'redis_client', side_effect=redis.ConnectionError('offline')):
            self.assertEqual(self.a.get('/api/files/' + filename).status_code, 503)
        self.a.post('/api/auth/logout').raise_for_status()
        self.assertIsNone(self.a.cookies.get(accounts.MEDIA_COOKIE))
        replay = self.anonymous()
        replay.cookies.set(accounts.MEDIA_COOKIE, old, path='/api/')
        self.assertEqual(replay.get('/api/files/' + filename).status_code, 401)

    def test_admin_deletion_removes_original_user_file_only(self):
        first, second = self.import_audio(self.a), self.import_audio(self.b)
        self.admin.delete('/api/admin/data/file/' + first).raise_for_status()
        self.assertFalse(storage.path('file', first, uid=self.a_id).exists())
        self.assertTrue(storage.path('file', second, uid=self.b_id).exists())

    def test_storage_migration_cli_and_rollback_preserve_new_files_and_urls(self):
        import runpy
        import sys
        from backend.infrastructure import storage_migration
        original = self.import_audio(self.a)
        storage.path('file', original, uid=self.a_id).replace(self.root / original)
        script = paths.ROOT_DIR / 'scripts' / 'migrate-user-storage.py'
        with tempfile.TemporaryDirectory() as working, patch.object(paths, 'RUN_DIR', Path(working)):
            manifest = Path(working) / 'manifest.json'
            def command(action, name=manifest):
                arguments = [str(script), action, '--manifest', str(name)]
                if action in {'apply', 'rollback'}:
                    arguments.append('--offline-confirmed')
                with patch.object(sys, 'argv', arguments):
                    runpy.run_path(str(script), run_name='__main__')
            command('plan')
            command('apply')
            self.assertEqual(self.a.get('/api/files/' + original).status_code, 200)
            data = json.loads(manifest.read_text())
            backup = Path(data['backup_path'])
            self.assertTrue((backup / 'database.sql').is_file())
            self.assertTrue((backup / 'downloads.zip').is_file())
            newer = self.import_audio(self.b)
            command('rollback')
            self.assertTrue((self.root / original).is_file())
            self.assertTrue((self.root / newer).is_file())
            with accounts.database() as cursor:
                cursor.execute('SELECT id FROM account_migrations WHERE id=%s', (storage_migration.VERSION,))
                self.assertIsNone(cursor.fetchone())
            replacement = Path(working) / 'reapply.json'
            command('plan', replacement)
            command('apply', replacement)
            self.assertEqual(self.a.get('/api/files/' + original).status_code, 200)
            self.assertEqual(self.b.get('/api/files/' + newer).status_code, 200)

    def test_profile_theme_and_images_are_private(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (20, 20), 'red').save(buffer, 'PNG')
        image = self.a.post('/api/library/profile/avatar', files={'file': ('avatar.png', buffer.getvalue(), 'image/png')}).json()['avatar']
        self.assertEqual(self.a.get(image).status_code, 200)
        self.assertEqual(self.b.get(image).status_code, 404)
        self.assertEqual(self.b.patch('/api/library/profile', json={'nickname': 'B', 'avatar': image}).status_code, 404)
        self.a.patch('/api/library/profile', json={'nickname': 'Private A', 'avatar': image}).raise_for_status()
        self.assertNotEqual(self.b.get('/api/library').json()['profile']['nickname'], 'Private A')
        self.a.put('/api/library/theme', json={'mode': 'dark', 'background': '#181b1f', 'text': '#f4f5f6'}).raise_for_status()
        self.assertEqual(self.b.get('/api/library/theme').json()['mode'], 'light')

    def test_tasks_are_owned_and_survive_memory_loss(self):
        response = self.a.post('/api/download', json={'url': 'https://example.com/video'})
        self.assertEqual(response.status_code, 200, response.text)
        task_id = response.json()['task_id']
        self.assertEqual(video_tasks.tasks[task_id].user_id, self.a_id)
        self.assertEqual(self.b.get('/api/tasks/' + task_id).status_code, 404)
        video_tasks.tasks.clear()
        self.assertEqual(self.a.get('/api/tasks/' + task_id).status_code, 200)
        self.assertEqual(self.b.get('/api/workspace/resources?kind=download').json()['total'], 0)
        self.assertEqual(self.b.delete('/api/workspace/resources/download/' + task_id).status_code, 404)
        self.a.delete('/api/workspace/resources/download/' + task_id).raise_for_status()
        self.assertEqual(self.a.get('/api/tasks/' + task_id).status_code, 404)

    def test_administrator_cover_upload_preserves_original_owner(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (20, 20), 'red').save(buffer, 'PNG')
        key = self.a.put('/api/library/favorites', json={'track': self.track, 'liked': True}).json()['id']
        playlist = self.a.post('/api/library/playlists', json={'name': 'Private list'}).json()['id']
        for path, field in ((f'tracks/{key}/cover', 'thumbnail'), (f'playlists/{playlist}/cover', 'thumbnail')):
            response = self.admin.post('/api/library/' + path, files={'file': ('cover.png', buffer.getvalue(), 'image/png')})
            self.assertEqual(response.status_code, 200, response.text)
            image = response.json()[field]
            self.assertEqual(self.a.get(image).status_code, 200)
            self.assertEqual(self.b.get(image).status_code, 404)

    def test_administrator_batch_retry_preserves_task_and_worker_owner(self):
        track = music._register(music.Track(**self.track))
        response = self.a.post('/api/music/batches', json={'tracks': [{'token': track.token, 'webpage_url': track.webpage_url}]})
        self.assertEqual(response.status_code, 200, response.text)
        batch_id = response.json()['batch_id']
        batch = video_tasks.music_batches[batch_id]
        video_tasks._set_task(batch['items'][0]['task'], status='failed', error='temporary failure')
        batch['status'] = 'failed'
        response = self.admin.post(f'/api/music/batches/{batch_id}/retry')
        self.assertEqual(response.status_code, 200, response.text)
        task = batch['items'][0]['task']
        self.assertEqual(task.user_id, self.a_id)
        submitted = video_tasks.music_batch_executor.submit.call_args.args
        self.assertEqual(submitted[0].__self__.run(accounts.owner_id), self.a_id)
        self.assertEqual(self.a.get('/api/tasks/' + task.task_id).status_code, 200)
        self.assertEqual(self.b.get('/api/tasks/' + task.task_id).status_code, 404)
        video_tasks.music_batches.clear()
        self.assertEqual(self.a.get(f'/api/music/batches/{batch_id}').json()['items'][0]['task_id'], task.task_id)

    def test_refresh_rotation_expiry_and_session_loss(self):
        old = self.a.cookies.get(accounts.COOKIE)
        sid = old.split('.')[0]
        accounts.redis_client().expire(accounts.session_key(sid), 120)
        response = self.a.post('/api/auth/refresh')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(old, self.a.cookies.get(accounts.COOKIE))
        self.assertLessEqual(accounts.redis_client().ttl(accounts.session_key(sid)), 120)
        replay = self.anonymous()
        replay.cookies.set(accounts.COOKIE, old, path='/api/auth')
        self.assertEqual(replay.post('/api/auth/refresh').status_code, 401)
        claims = jwt.decode(response.json()['access_token'], accounts.signing_key(), algorithms=['HS256'], audience='videodream-web', issuer='videodream')
        claims['exp'] = int(time.time()) - 1
        self.a.headers['Authorization'] = 'Bearer ' + jwt.encode(claims, accounts.signing_key(), algorithm='HS256')
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        self.assertEqual(self.a.post('/api/auth/refresh').status_code, 200)
        self.a.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        accounts.redis_client().delete(accounts.session_key(sid))
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        self.assertEqual(self.a.post('/api/auth/refresh').status_code, 401)

    def test_logout_revokes_current_session_and_requires_csrf(self):
        second = self.client('user_a', 'Test-user-123!')
        self.assertEqual(self.a.post('/api/auth/logout', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.a.post('/api/auth/logout').raise_for_status()
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        self.assertEqual(second.get('/api/library').status_code, 200)
        self.assertEqual(self.a.post('/api/auth/refresh').status_code, 401)

    def test_disable_revokes_sessions_and_cancels_pending_tasks(self):
        task_id = self.a.post('/api/download', json={'url': 'https://example.com/video'}).json()['task_id']
        second = self.client('user_a', 'Test-user-123!')
        self.admin.patch(f'/api/admin/users/{self.a_id}', json={'status': 'disabled'}).raise_for_status()
        self.assertTrue(video_tasks.tasks[task_id].cancel.is_set())
        self.assertEqual(video_tasks.tasks[task_id].status, 'cancelled')
        for client in (self.a, second):
            self.assertEqual(client.get('/api/library').status_code, 401)
            self.assertEqual(client.post('/api/auth/refresh').status_code, 401)
        self.admin.patch(f'/api/admin/users/{self.a_id}', json={'status': 'active'}).raise_for_status()
        self.assertEqual(self.a.get('/api/library').status_code, 401)

    def test_demotion_is_immediate_and_last_administrator_is_protected(self):
        for method, body in (('patch', {'role': 'user'}), ('patch', {'status': 'disabled'}), ('delete', None)):
            response = getattr(self.admin, method)('/api/admin/users/1', **({'json': body} if body else {}))
            self.assertEqual(response.status_code, 409)
        self.admin.patch(f'/api/admin/users/{self.a_id}', json={'role': 'super_admin'}).raise_for_status()
        self.assertEqual(self.a.get('/api/admin/users').status_code, 200)
        self.admin.patch(f'/api/admin/users/{self.a_id}', json={'role': 'user'}).raise_for_status()
        self.assertEqual(self.a.get('/api/admin/users').status_code, 403)
        self.assertEqual(self.a.get('/api/library').status_code, 200)

    def test_password_reset_forces_change_and_invalidates_all_sessions(self):
        self.admin.post(f'/api/admin/users/{self.a_id}/reset-password', json={'password': 'Temporary-123!'}).raise_for_status()
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        temporary = self.client('user_a', 'Temporary-123!')
        self.assertEqual(temporary.get('/api/library').status_code, 403)
        self.assertEqual(temporary.get('/api/auth/me').status_code, 200)
        temporary.post('/api/auth/password', json={'old_password': 'Temporary-123!', 'new_password': 'Permanent-123!'}).raise_for_status()
        self.assertEqual(temporary.get('/api/library').status_code, 401)
        permanent = self.client('user_a', 'Permanent-123!')
        self.assertEqual(permanent.get('/api/library').status_code, 200)

    def test_registration_settings_feature_switches_and_secret_redaction(self):
        candidate = {'username': 'new_user', 'password': 'New-user-123!', 'role': 'super_admin'}
        self.assertEqual(self.anonymous().post('/api/auth/register', json=candidate).status_code, 403)
        config = accounts.DEFAULT_SETTINGS | {'registration_enabled': True, 'music_enabled': False, 'ai_api_key': 'secret-that-must-not-be-shown'}
        local = {key: value for key, value in accounts.local_environment().items() if not key.startswith('LLM_')}
        with patch('backend.infrastructure.network.public_url', side_effect=lambda value: value), patch.object(accounts, 'local_environment', return_value=local):
            response = self.admin.put('/api/admin/settings', json=config)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(config['ai_api_key'], response.text)
        self.assertEqual(self.a.get('/api/music/search?q=song').status_code, 403)
        self.anonymous().post('/api/auth/register', json=candidate).raise_for_status()
        with library.database() as cursor:
            cursor.execute("SELECT role,password_hash FROM users WHERE username='new_user'")
            row = cursor.fetchone()
        self.assertEqual(row['role'], 'user')
        self.assertTrue(row['password_hash'].startswith('$argon2id$'))
        self.assertNotIn(config['ai_api_key'], self.admin.get('/api/admin/logs').text)
        self.assertNotIn('password_hash', self.admin.get('/api/admin/users').text)

    def test_redis_failure_never_authorizes_and_limits_failed_logins(self):
        with patch.object(accounts, 'redis_client', side_effect=redis.ConnectionError('offline')):
            self.assertEqual(self.a.get('/api/library').status_code, 503)
            self.assertEqual(self.a.post('/api/auth/refresh').status_code, 503)
            self.assertEqual(self.anonymous().post('/api/auth/login', json={'username': 'user_a', 'password': 'Test-user-123!'}).status_code, 503)
        for _ in range(10):
            self.assertEqual(self.anonymous().post('/api/auth/login', json={'username': 'unknown', 'password': 'Wrong-password!'}).status_code, 401)
        self.assertEqual(self.anonymous().post('/api/auth/login', json={'username': 'unknown', 'password': 'Wrong-password!'}).status_code, 429)

    def test_jwt_claims_algorithms_and_cookie_flags(self):
        cookie = self.a.cookies.get(accounts.COOKIE)
        sid = cookie.split('.')[0]
        claims = {'sub': str(self.a_id), 'sid': sid, 'iat': int(time.time()), 'exp': int(time.time()) + 900, 'iss': 'videodream', 'aud': 'wrong'}
        self.a.headers['Authorization'] = 'Bearer ' + jwt.encode(claims, accounts.signing_key(), algorithm='HS256')
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        claims['aud'] = 'videodream-web'
        self.a.headers['Authorization'] = 'Bearer ' + jwt.encode(claims, accounts.signing_key(), algorithm='HS384')
        self.assertEqual(self.a.get('/api/library').status_code, 401)
        response = self.a.post('/api/auth/refresh')
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=strict', response.headers['set-cookie'])
        self.assertIn('Path=/api/auth', response.headers['set-cookie'])
        stored = accounts.redis_client().hgetall(accounts.session_key(sid))
        self.assertNotIn(cookie.split('.')[1], str(stored))


    def test_restricted_platforms_are_guarded_across_all_incoming_features(self):
        from backend.modules.video import parsing, schemas, thumbnails
        for source in ('https://youtu.be/test1234567', 'https://cn.pornhub.com/view_video.php?viewkey=fixture'):
            for path in ('/api/parse', '/api/download', '/api/summaries', '/api/music/parse', '/api/previews', '/api/previews/reuse', '/api/browser-session/open'):
                response = self.a.post(path, json={'url': source, 'format_id': 'best'})
                self.assertEqual(response.status_code, 403, (path, response.text))
            self.assertEqual(self.a.get('/api/image-proxy', params={'url': source}).status_code, 403)
            parsed = schemas.ParseResponse(title='fixture', webpage_url=source, formats=[])
            with patch.object(parsing, 'parse_video', return_value=parsed), patch.object(thumbnails, 'schedule'):
                response = self.admin.post('/api/parse', json={'url': source, 'force': True})
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.a.get('/api/admin/video-network').status_code, 403)
        self.assertEqual(self.a.post('/api/admin/video-network/check').status_code, 403)
        response = self.admin.get('/api/admin/video-network')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('127.0.0.1:7890', response.text)

    def test_legacy_download_and_derived_files_stay_restricted_in_lists_and_direct_links(self):
        parent = 'abcdef123456'
        filename = parent + '.fixture.mp4'
        derived = 'fedcba654321.' + parent + '.fixture.watermark-removed.mp4'
        accounts.register_resource('download', parent, {'url': 'https://www.youtube.com/watch?v=fixture', 'status': 'finished'}, uid=self.a_id)
        # Insert the old payload shape to exercise source recovery without new flags.
        with accounts.database() as cursor:
            for name in (filename, derived):
                storage.path('file', name, uid=self.a_id).write_bytes(b'legacy-video')
                cursor.execute('INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)',
                               ('file', name, self.a_id, json.dumps({'filename': name}), time.time(), time.time()))
        for name in (filename, derived):
            self.assertEqual(self.a.get('/api/files/' + name).status_code, 403)
            self.assertEqual(self.admin.get('/api/files/' + name).status_code, 200)
        listing = self.a.get('/api/workspace/resources?kind=file')
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json(), {'items': [], 'total': 0})
        self.assertEqual(self.a.get('/api/tasks/' + parent).status_code, 403)
        # Reindexing may not clear a restriction already attached to a file.
        accounts.register_resource('file', filename, {'source_url': 'https://youtu.be/fixture'}, uid=self.a_id)
        accounts.register_resource('file', filename, {'filename': filename, 'size': 12}, uid=self.a_id)
        self.assertEqual(self.a.get('/api/files/' + filename).status_code, 403)

    def test_demotion_hides_historical_records_previews_and_music(self):
        from backend.modules.video import store, cache
        record_id = 'source_record'
        token = 'restricted_preview_fixture'
        now = time.time()
        with accounts.database() as cursor:
            store.save(cursor, 'video_parse', record_id, self.a_id,
                       {'record_id': record_id, 'url': 'https://cn.pornhub.com/view_video.php?viewkey=fixture', 'platform': 'Pornhub', 'status': 'finished', 'result': {'title': 'private'}})
            store.save(cursor, 'video_view', str(self.a_id) + ':' + record_id, self.a_id, {'record_id': record_id, 'tab': 'summary'})
            store.save(cursor, 'video_search', str(self.a_id), self.a_id, {'items': [{'id': 'restricted', 'url': 'https://youtu.be/fixture', 'submitted_at': now}, {'id': 'normal', 'url': 'https://weibo.com/123', 'submitted_at': now - 1}]})
            track_id = 'a' * 64
            cursor.execute('INSERT INTO music_tracks (id,provider,identifier,title,artist,webpage_url,access,user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                           (track_id, 'video', 'fixture', 'private', '', 'https://youtu.be/fixture', '', self.a_id))
            cursor.execute('INSERT INTO music_history (track_id,played_at,play_count,preview,user_id) VALUES (%s,%s,1,FALSE,%s)', (track_id, now, self.a_id))
        cache.put(cache.key('preview', token, self.a_id), {'user_id': self.a_id, 'media_url': 'https://shared.example/media', 'referer': 'https://youtu.be/fixture', 'expires_at': now + 100})
        self.assertEqual(self.a.get('/api/videos/records/' + record_id).status_code, 403)
        records = self.a.get('/api/videos/records').json()
        self.assertEqual(records, {'items': [], 'total': 0})
        self.assertIsNone(self.a.get('/api/videos/last-record').json()['record_id'])
        self.assertEqual([item['id'] for item in self.a.get('/api/videos/recent-links').json()['items']], ['normal'])
        self.assertEqual(self.a.get('/api/previews/' + token).status_code, 403)
        self.assertEqual(self.a.get('/api/library/tracks?view=history').json()['tracks'], [])
        self.assertEqual(self.a.get('/api/library').json()['counts']['history'], 0)
        self.assertEqual(self.a.delete('/api/library/history/' + track_id).status_code, 403)

class NetworkSecurityTests(unittest.TestCase):
    def test_private_addresses_and_embedded_credentials_are_rejected(self):
        from backend.infrastructure.network import public_url
        from fastapi import HTTPException
        for url in ('http://127.0.0.1/file', 'http://[::1]/file', 'http://10.0.0.1/file', 'file:///etc/passwd', 'https://user:password@example.com/file', 'http://example.com:8001/file'):
            with self.subTest(url=url), self.assertRaises(HTTPException):
                public_url(url)


if __name__ == '__main__':
    unittest.main()
