import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend.modules.music import playlists, imports, sources as music, library
from backend.modules.accounts import service as accounts
from backend.tests.music import test_music_imports as import_tests


def netease_song(i):
    return {"id": i, "name": f"歌曲{i}", "ar": [{"name": "歌手"}], "al": {"name": "专辑"}, "dt": 180000}


class PlaylistReaderTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()

    def test_supported_links_and_song_links(self):
        for url, expected in [
            ('https://music.163.com/#/playlist?id=123', ('netease', '123')),
            ('https://music.163.com/m/playlist?id=123', ('netease', '123')),
            ('https://y.qq.com/n/ryqq/playlist/456', ('qq', '456')),
            ('https://y.qq.com/n2/m/share/details/taoge.html?id=456', ('qq', '456')),
            ('https://www.kugou.com/yy/special/single/789.html', ('kugou', '789')),
            ('https://m.kugou.com/plist/list/789', ('kugou', '789')),
            ('https://music.163.com/song?id=123', None),
            ('https://evil.test/playlist?id=123', None),
        ]:
            self.assertEqual(playlists.identify(url), expected, url)

    def test_shared_text_and_short_redirect(self):
        with patch.object(playlists, 'validate_url'), patch.object(playlists, 'request', return_value=('https://music.163.com/playlist?id=123', '')):
            self.assertEqual(playlists.resolve('分享「通勤」https://163cn.tv/aBc。', self.stop), ('netease', '123'))
            with self.assertRaises(playlists.PlaylistError):
                playlists.resolve('https://music.163.com/playlist?id=1 https://music.163.com/playlist?id=2', self.stop)

    def test_reject_foreign_hosts_and_private_dns(self):
        for url in ('https://y.qq.com.evil.test/playlist/1', 'http://127.0.0.1/playlist/1', 'file:///tmp/a'):
            with self.assertRaises(playlists.PlaylistError):
                playlists.validate_url(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(HTTPException):
                playlists.validate_url('https://y.qq.com/playlist/1')

    def test_redirect_revalidated_before_request(self):
        response = Mock(status_code=302, headers={'Location': 'http://127.0.0.1/admin'})
        session = Mock()
        session.get.return_value = response
        with patch.object(playlists, 'public_url', side_effect=lambda u: u), patch.object(playlists.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(playlists.PlaylistError):
                playlists.request('https://163cn.tv/test', self.stop)
            self.assertEqual(session.get.call_count, 1)
            response.close.assert_called_once()

    def test_netease_details_fill_205_rows_in_order(self):
        ids = list(range(1, 206))
        def request(url, stop, *, params):
            if 'playlist/detail' in url:
                return {'playlist': {'name': '公开歌单', 'trackCount': 205, 'trackIds': [{'id': i} for i in ids], 'tracks': [netease_song(1)]}}
            # Provider detail order must not change playlist order.
            return {'songs': [netease_song(int(i)) for i in reversed(json.loads(params['ids']))]}
        with patch.object(playlists, 'request', side_effect=request) as fetch:
            result = playlists.read('netease', '123', self.stop)
        self.assertEqual([t['identifier'] for t in result['tracks']], list(map(str, ids)))
        self.assertTrue(result['playlist']['complete'])
        self.assertEqual(fetch.call_count, 4)

    def test_range_required_and_explicit_slice(self):
        data = {'playlist': {'name': '大歌单', 'trackCount': 600, 'tracks': [netease_song(i) for i in range(1, 601)]}}
        with patch.object(playlists, 'request', return_value=data):
            with self.assertRaises(playlists.RangeRequired) as failure:
                playlists.read('netease', '123', self.stop)
            self.assertEqual(failure.exception.metadata['total'], 600)
            result = playlists.read('netease', '123', self.stop, start=501, limit=100, explicit_range=True)
        self.assertEqual(len(result['tracks']), 100)
        self.assertEqual(result['tracks'][0]['position'], 501)
        self.assertTrue(result['playlist']['range_complete'])
        self.assertFalse(result['playlist']['complete'])

    def test_missing_ids_and_unknown_total_never_claim_complete(self):
        for total in (10, None):
            with patch.object(playlists, 'request', return_value={'playlist': {'name': '部分', 'trackCount': total, 'tracks': [netease_song(1)]}}):
                result = playlists.read('netease', '123', self.stop)
            self.assertFalse(result['playlist']['complete'])
            self.assertTrue(result['warnings'])

    def test_kugou_pagination_filename_and_album(self):
        def request(url, stop, *, params):
            if url.endswith('/info'):
                return {'data': {'specialname': '酷狗歌单'}}
            page = params['page']
            return {'status': 1, 'data': {'total': 105, 'info': [
                {'hash': f'{i:032x}', 'filename': f'歌手 - 歌曲{i}', 'duration': 180, 'album_name': '专辑'}
                for i in range((page - 1) * 100 + 1, min(page * 100, 105) + 1)]}}
        with patch.object(playlists, 'request', side_effect=request):
            result = playlists.read('kugou', '123', self.stop)
        self.assertEqual(len(result['tracks']), 105)
        self.assertEqual(result['tracks'][0]['artist'], '歌手')
        self.assertEqual(result['tracks'][0]['album'], '专辑')
        self.assertTrue(result['playlist']['complete'])

    def test_later_page_failure_preserves_partial_tracks(self):
        first = {'playlist': {'name': '部分', 'trackCount': 101, 'trackIds': [{'id': i} for i in range(1, 102)]}}
        with patch.object(playlists, 'request', side_effect=[first, {'songs': [netease_song(i) for i in range(1, 101)]}, playlists.PlaylistError('限流')]):
            result = playlists.read('netease', '123', self.stop)
        self.assertEqual(len(result['tracks']), 100)
        self.assertFalse(result['playlist']['complete'])
        self.assertTrue(result['warnings'])

    def test_cancel_before_network(self):
        self.stop.set()
        with patch.object(playlists.requests, 'Session') as session:
            with self.assertRaises(playlists.PlaylistError):
                playlists.request('https://music.163.com/api/playlist/detail', self.stop)
            session.assert_not_called()

    def test_bulk_save_does_not_emit_preference_signal(self):
        cursor = Mock(rowcount=1)
        cursor.fetchone.return_value = {'last_position': 0}
        @contextmanager
        def database():
            yield cursor
        from backend.modules.music.discovery import store
        with patch.object(library, 'database', database), patch.object(library, 'require_playlist'), patch.object(library, 'save_track', return_value='track'), patch.object(store, 'lock_user'), patch.object(store, 'library_signal') as signal, patch.object(accounts, 'owner_id', return_value=1):
            library.add_to_playlist('list', library.TrackInput(title='歌', provider='qq', webpage_url='https://y.qq.com/song/1'), bulk_import=True)
            signal.assert_not_called()


class PlaylistImportTests(unittest.TestCase):
    setUp = import_tests.MusicImportTests.setUp
    track = import_tests.MusicImportTests.track
    result = import_tests.MusicImportTests.result

    def create(self):
        response = self.client.post('/api/music/imports/playlist', json={'share_text': 'https://music.163.com/playlist?id=123'})
        self.assertEqual(response.status_code, 200, response.text)
        return imports.jobs[response.json()['import_id']]

    def test_auto_search_keeps_identity_and_deduplicates_only_ids(self):
        job = self.create()
        data = {'playlist': {'name': '测试', 'total': 3, 'read_count': 3, 'complete': True}, 'warnings': [],
                'tracks': [self.track(identifier=i).model_dump() | {'position': n} for n, i in enumerate(['1', '1', '2'], 1)]}
        def read(*args, **kwargs):
            kwargs['progress'](data['playlist'], data['tracks'], [])
            return data
        with patch.object(playlists, 'resolve', return_value=('netease', '123')), patch.object(playlists, 'read', side_effect=read), patch.object(music, 'search', return_value=self.result([])):
            imports.run_playlist(job, imports.PlaylistImport(share_text='link'), job['stop'])
        self.assertEqual((job['status'], len(job['rows']), job['duplicates']), ('finished', 2, 1))
        self.assertEqual([r['selected_id'] for r in job['rows']], ['netease:1', 'netease:2'])
        old = job['rows'][0]
        response = self.client.post(f"/api/music/imports/{job['import_id']}/search", json={'songs': [{'row_id': old['row_id'], 'title': '修改歌名', 'artist': '歌手'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(job['rows'][0]['origin'], old['origin'])

    def test_retry_only_failed_preserves_matched_rows(self):
        job = self.create()
        job['status'] = 'finished'
        job['rows'] = imports.make_rows([imports.SongInput(title='已匹配'), imports.SongInput(title='失败'), imports.SongInput(title='待搜索')])
        job['rows'][0].update(status='matched', selected_id='qq:1')
        job['rows'][1]['status'] = 'error'
        first = dict(job['rows'][0])
        response = self.client.post(f"/api/music/imports/{job['import_id']}/search", json={'retry_failed': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(job['rows'][0], first)
        self.assertEqual(job['search_total'], 2)

    def test_cancelled_reader_cannot_overwrite_new_job(self):
        job = self.create()
        old_stop = job['stop']
        old_stop.set()
        job.update(stop=threading.Event(), status='draft')
        with patch.object(playlists, 'resolve') as resolve:
            imports.run_playlist(job, imports.PlaylistImport(share_text='link'), old_stop)
            resolve.assert_not_called()
        self.assertEqual(job['status'], 'draft')

    def test_album_duration_and_edited_identity_require_confirmation(self):
        original = self.track().model_dump() | {'album': '原专辑'}
        song = {'title': '晴天', 'artist': '周杰伦', 'uncertain': False, 'origin': {'track': original}}
        candidate = self.track(identifier='2').model_copy(update={'provider': 'qq', 'album': '其他专辑'})
        with patch.object(music, 'search', return_value=self.result([candidate])):
            self.assertIsNone(imports.match_song(song, 'qq')['selected_id'])
        candidate = candidate.model_copy(update={'album': '原专辑', 'duration': 250})
        with patch.object(music, 'search', return_value=self.result([candidate])):
            self.assertIsNone(imports.match_song(song, 'qq')['selected_id'])
        song['title'] = '另一首歌'
        with patch.object(music, 'search', return_value=self.result([])):
            self.assertEqual(imports.match_song(song, 'all')['candidates'], [])

    def test_creation_route_is_not_mistaken_for_task_id(self):
        with patch.object(accounts, 'resource') as resource:
            accounts.check_path('/api/music/imports/playlist')
            resource.assert_not_called()


if __name__ == '__main__':
    unittest.main()
