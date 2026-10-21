import copy
import unittest
from unittest.mock import Mock, patch

from backend.core import paths
from backend.modules.music import lyric_lookup
from backend.modules.music.agent import budget, features, model, runtime, service
from backend.modules.music.agent.models import ParsedNeed, ResultMatch


class SceneScoreTests(unittest.TestCase):
    def candidate(self, language='英文', role='lead'):
        return {'title': 'Song', 'artist': 'Artist', 'source': {'kind': 'online_playlist'},
                'analysis': {'features': {'vocal_role': role, 'vocals': role != 'instrumental', 'language': language,
                    'scene_scores': {'游戏': .8, '放松': .8, '工作': .8}},
                    'evidence': {'fields': {k: {'confidence': .9} for k in ('vocal_role', 'vocals', 'language', 'scene_scores')}}},
                'validation': {'identity': True, 'playable': True, 'full': True}}

    def test_language_priority_only_in_games(self):
        tracks = [self.candidate(language) for language in ('英文', '韩文', '日文', '中文')]
        games = [features.match(t, ParsedNeed(scene='打游戏').model_dump()) for t in tracks]
        self.assertEqual([s['score'] for s in games], [83, 78, 75, 73])
        for scene in ('放松', '工作'):
            scores = [features.match(t, ParsedNeed(scene=scene).model_dump()) for t in tracks]
            self.assertEqual([s['breakdown']['language'] for s in scores], [0]*4)
            self.assertEqual([s['score'] for s in scores], [68]*4)

    def test_samples_unknown_and_instrumental_do_not_get_vocal_or_language_bonus(self):
        for role in ('background', 'unknown', 'instrumental'):
            result = features.match(self.candidate(role=role), ParsedNeed(scene='游戏').model_dump())
            self.assertEqual(result['score'], 48)
            self.assertEqual(result['breakdown']['language'], 0)
        lead = self.candidate()
        lead['analysis']['evidence']['fields']['vocal_role']['confidence'] = .6
        self.assertEqual(features.match(lead, ParsedNeed(scene='游戏').model_dump())['score'], 48)

    def test_missing_and_low_scene_score_cannot_be_rescued_by_language(self):
        track = self.candidate()
        track['analysis']['features']['scene_scores'] = {}
        result = features.match(track, ParsedNeed(scene='打游戏').model_dump())
        self.assertFalse(result['qualified'])
        self.assertIsNone(result['score'])
        track['analysis']['features']['scene_scores'] = {'游戏': .1}
        self.assertFalse(features.match(track, ParsedNeed(scene='打游戏').model_dump())['qualified'])

    def test_explicit_instrumental_and_language_constraints_win(self):
        self.assertFalse(features.match(self.candidate(), ParsedNeed(scene='游戏', vocals='instrumental').model_dump())['qualified'])
        self.assertFalse(features.match(self.candidate(), ParsedNeed(scene='游戏', languages=['中文']).model_dump())['qualified'])
        result = features.match(self.candidate(role='instrumental'), ParsedNeed(scene='游戏', vocals='instrumental', languages=['中文']).model_dump())
        self.assertTrue(result['qualified'])
        self.assertEqual(result['breakdown']['vocals'], 0)

    def test_audio_scores_canonical_scene_and_effect(self):
        track = self.candidate(role='instrumental')
        f = track['analysis']['features']
        f.update(scene_scores={}, tempo=130, energy=.8, variation=.2, rhythm_stability=.9)
        for field in ('tempo', 'energy', 'variation', 'rhythm_stability'):
            track['analysis']['evidence']['fields'][field] = {'confidence': .9, 'coverage': 'full'}
        score = features.match(track, ParsedNeed(scene='打游戏', effect='刺激').model_dump())
        self.assertTrue(score['qualified'])
        self.assertGreater(score['score'], 40)
        track['analysis']['evidence']['fields']['tempo']['coverage'] = 'partial'
        self.assertIsNone(features.match(track, ParsedNeed(scene='游戏').model_dump())['score'])

    def test_legacy_score_schema_and_scene_normalization(self):
        self.assertEqual(ResultMatch(qualified=True, reasons=[], score=.3).scoring_version, 'legacy')
        self.assertEqual(ParsedNeed(scene='打游戏').scene, '游戏')


class ArtistPreferenceTests(unittest.TestCase):
    candidate = SceneScoreTests.candidate

    def test_optional_artist_bonus_and_non_stacking(self):
        track = self.candidate('韩文')
        track['artist'] = 'EXO / BIGBANG'
        base = features.match(track, ParsedNeed(scene='游戏').model_dump())
        preferred = features.match(track, ParsedNeed(scene='游戏', preferred_artists=['EXO', 'BIGBANG']).model_dump())
        self.assertEqual(base['score'], 78)
        self.assertNotIn('artist', base['breakdown'])
        self.assertEqual(preferred['score'], 82.4)
        self.assertEqual(preferred['base_score'], 78)
        self.assertEqual(preferred['breakdown']['artist'], 20)
        self.assertEqual(preferred['matched_artists'], ['EXO', 'BIGBANG'])
        ResultMatch.model_validate(preferred)
        track['artist'] = 'Other'
        self.assertEqual(features.match(track, ParsedNeed(scene='游戏', preferred_artists=['EXO']).model_dump())['score'], 62.4)

    def test_credit_identity_not_title_substring_or_group_membership(self):
        for credit in (' exo ', 'ＥＸＯ', '엑소', 'EXO / IU', 'IU feat. EXO'):
            self.assertEqual(features.matched_artists({'artist': credit}, ['EXO']), ['EXO'])
        for credit in ('EXO-M', 'EXO-CBX', 'D.O.', 'SomeEXO', 'Other'):
            self.assertEqual(features.matched_artists({'artist': credit, 'title': 'EXO'}, ['EXO']), [])
        self.assertEqual(features.matched_artists({'artist': 'Jay Chou'}, ['周杰伦']), ['周杰伦'])
        self.assertEqual(features.matched_artists({'artist': 'AC/DC'}, ['AC']), [])

    def test_artist_bonus_never_changes_any_scene_qualification(self):
        for scene in features.SCENES:
            for relevance in (None, .1, .149, .15, .8):
                for failure in ('none', 'identity', 'playable', 'full', 'instrumental', 'language', 'exclusion', 'theme', 'weak_evidence'):
                    track = self.candidate('韩文')
                    track['artist'] = 'EXO'
                    track['analysis']['features']['scene_scores'] = {} if relevance is None else {scene: relevance}
                    parsed = ParsedNeed(scene=scene).model_dump()
                    if failure in ('identity', 'playable', 'full'):
                        track['validation'][failure] = False
                    elif failure == 'instrumental':
                        parsed['vocals'] = 'instrumental'
                    elif failure == 'language':
                        parsed['languages'] = ['中文']
                    elif failure == 'exclusion':
                        parsed['exclusions'] = ['EXO']
                    elif failure == 'theme':
                        parsed['excluded_themes'] = ['失恋']
                    elif failure == 'weak_evidence':
                        track['analysis']['evidence']['fields']['scene_scores']['confidence'] = .6
                    before = features.match(track, parsed)
                    after = features.match(track, parsed | {'preferred_artists': ['EXO']})
                    with self.subTest(scene=scene, relevance=relevance, failure=failure):
                        self.assertEqual(after['qualified'], before['qualified'])
                        self.assertEqual(after['reasons'], before['reasons'])
                        self.assertEqual(after['base_score'], before['score'])
                        if before['score'] is None:
                            self.assertIsNone(after['score'])

    def test_sleep_full_audio_thresholds_unchanged(self):
        for energy in (.3, .4, .41):
            for variation in (.7, .8, .81):
                for coverage in ('full', 'partial'):
                    track = self.candidate()
                    track['artist'] = 'EXO'
                    track['analysis']['features'].update(energy=energy, variation=variation)
                    for field in ('energy', 'variation'):
                        track['analysis']['evidence']['fields'][field] = {'confidence': .9, 'coverage': coverage}
                    parsed = ParsedNeed(scene='睡眠').model_dump()
                    before, after = [features.match(track, p) for p in (parsed, parsed | {'preferred_artists': ['EXO']})]
                    self.assertEqual(before['qualified'], after['qualified'])
                    self.assertEqual(before['reasons'], after['reasons'])
                    self.assertEqual(after['qualified'], coverage == 'full' and energy <= .4 and variation <= .8)

    def row(self, artists=None):
        return {'request': {'parsed': ParsedNeed(scene='游戏', source_mode='discover', target_count=1,
                    preferred_artists=artists or ['EXO', 'BIGBANG']).model_dump()},
                'context': {'candidates': [], 'limits': {'candidates': 100, 'sources': ['netease', 'qq'],
                    'search_rounds': 5, 'network_requests': 300}, 'budget': {}}}

    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_each_artist_searched_before_round_robin_evaluation(self, _):
        row = self.row()
        self.assertEqual(runtime.pending_tool(row, set()), ('search_online_tracks', {'provider': 'netease', 'query': 'EXO', 'page': 1}))
        row['context']['artist_searches'] = [{'artist': 'EXO', 'source': 'netease', 'success': True}]
        row['context']['candidates'] = [{'key': 'exo', 'title': 'Song', 'artist': 'EXO', 'source': {'kind': 'online_search'}}]
        self.assertEqual(runtime.pending_tool(row, set())[1]['query'], 'BIGBANG')
        row['context']['artist_searches'].append({'artist': 'BIGBANG', 'source': 'netease', 'success': True})
        row['context']['candidates'].append({'key': 'bigbang', 'title': 'Song2', 'artist': 'BIGBANG', 'source': {'kind': 'online_search'}})
        self.assertEqual(runtime.pending_tool(row, set()), ('get_track_features', {'keys': ['exo']}))
        row['context']['candidates'][0]['analysis'] = {'features': {}}
        self.assertEqual(runtime.pending_tool(row, set()), ('validate_tracks', {'keys': ['exo']}))
        row['context']['candidates'][0]['validation'] = {'identity': False}
        self.assertEqual(runtime.pending_tool(row, set()), ('get_track_features', {'keys': ['bigbang']}))

    def test_count_cannot_stop_before_artist_evaluation(self):
        row = self.row(['EXO'])
        other = self.candidate()
        other.update(key='other', duration=180)
        row['context']['candidates'] = [other]
        self.assertFalse(service.satisfied(row)[0])
        row['context']['artist_searches'] = [{'artist': 'EXO', 'source': 'netease', 'success': True}]
        self.assertTrue(service.satisfied(row)[0])
        self.assertEqual(service.artist_coverage(row)[0]['status'], 'no_candidates')
        exo = self.candidate()
        exo.update(key='exo', artist='EXO')
        exo.pop('validation')
        row['context']['candidates'].append(exo)
        self.assertFalse(service.satisfied(row)[0])
        exo['validation'] = {'identity': False, 'reason': '身份不符'}
        self.assertTrue(service.satisfied(row)[0])
        self.assertEqual(service.artist_coverage(row)[0]['status'], 'no_qualified')
        self.assertIn('身份不符', service.artist_coverage(row)[0]['reasons'])

    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_budget_reserve_and_search_failure_report_incomplete(self, _):
        row = self.row(['EXO'])
        row['context']['artist_searches'] = [{'artist': 'EXO', 'source': 'netease', 'success': False}]
        self.assertEqual(runtime.pending_tool(row, set())[1]['provider'], 'qq')
        row['context']['budget']['search_rounds'] = 5
        self.assertIsNone(runtime.pending_tool(row, set()))
        self.assertEqual(service.artist_coverage(row)[0]['status'], 'incomplete')
        row['context']['budget'] = {'network_requests': 225}
        self.assertIsNone(runtime.pending_tool(row, set()))

    @patch.object(runtime.service, 'network_allowed', return_value=False)
    def test_offline_artist_search_stays_local(self, _):
        row = self.row(['EXO'])
        row['request']['parsed']['network_allowed'] = False
        self.assertEqual(runtime.pending_tool(row, set())[0], 'search_my_music')
        row['context']['artist_searches'] = [{'artist': 'EXO', 'source': 'personal', 'success': True}]
        self.assertTrue(service.artists_complete(row))

    def test_offline_explicit_artist_parsing(self):
        parsed = model.offline_parse('打游戏的音乐,exo和bigbang合适的歌优先', {})
        self.assertEqual(parsed['preferred_artists'], ['exo', 'bigbang'])
        self.assertEqual(model.offline_parse('想听周杰伦的歌', {})['preferred_artists'], ['周杰伦'])
        self.assertEqual(model.offline_parse('打游戏的音乐', {})['preferred_artists'], [])
        self.assertEqual(model.offline_parse('不要EXO', {})['preferred_artists'], [])
        self.assertEqual(model.offline_parse('英文优先', {})['preferred_artists'], [])
        self.assertEqual(model.offline_parse('喜欢EXO，不要BIGBANG', {})['preferred_artists'], ['EXO'])


class LyricClassificationTests(unittest.TestCase):
    def test_actual_credit_only_responses_have_no_lyric_body(self):
        for text in ('[00:00.00-1] 作曲 : T3CHN0/Oliver Stock',
                     '[00:00.00-1] 作词 : Maximilian Hacker\n[00:00.00-1] 作曲 : Maximilian Hacker',
                     '[00:00]Lyrics by: Person\n[00:01]作詞：作者\n[00:02]작곡: Author'):
            classified = lyric_lookup.classify_lines(text)
            self.assertFalse(classified['body'])
            self.assertTrue(classified['credits'])

    def test_preserves_actual_lyrics_and_marks_declarations(self):
        text = '[00:01]作曲：作者\n[00:12]I keep running through the night\n[00:30]纯音乐，请欣赏'
        result = lyric_lookup.classify_lines(text)
        self.assertEqual(result['body'], ['I keep running through the night'])
        self.assertEqual(len(result['declarations']), 1)

    def test_wrong_song_and_source_failure_are_not_empty_lyrics(self):
        lyric_lookup._next_remote_at = 0
        response = Mock(status_code=200)
        response.json.return_value = {'trackName': 'Other', 'artistName': 'Artist', 'duration': 120, 'plainLyrics': 'hello'}
        with patch.object(lyric_lookup.requests, 'get', return_value=response), self.assertRaises(ValueError):
            lyric_lookup.lrclib('Song', 'Artist', 120)


class PendingWorkTests(unittest.TestCase):
    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_changed_source_candidates_are_evaluated_before_old_pool(self, _):
        row = self.row()
        row['context']['candidates'] = [
            {'key':'old', 'title':'Old', 'artist':'Artist'},
            {'key':'new', 'title':'New', 'artist':'Artist'}]
        row['context']['adjustment_candidates'] = ['new']
        self.assertEqual(runtime.pending_tool(row, set()), ('get_track_features', {'keys':['new']}))
        row['context']['candidates'][1]['analysis'] = {'features':{}}
        self.assertEqual(runtime.pending_tool(row, set()), ('validate_tracks', {'keys':['new']}))

    def row(self):
        return {'request': {'parsed': ParsedNeed().model_dump()}, 'context': {
            'personal_searched': True, 'candidates': [], 'limits': {'candidates': 100},
            'budget': {'search_rounds': 5}, 'online_playlists': [{'provider': 'netease', 'playlist_id': '123'}]}}

    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_search_exhaustion_keeps_discovered_playlist_available(self, _):
        row = self.row()
        action = runtime.pending_tool(row, set())
        self.assertEqual(action[0], 'get_playlist_tracks')
        self.assertEqual(action[1]['playlist_id'], '123')
        attempted = {runtime.store.tool_signature(*action)}
        self.assertIsNone(runtime.pending_tool(row, attempted))

    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_analysis_and_validation_precede_new_playlists(self, _):
        row = self.row()
        row['context']['candidates'] = [{'key': 'song', 'title': 'Song', 'artist': 'Artist'}]
        self.assertEqual(runtime.pending_tool(row, set())[0], 'get_track_features')
        row['context']['candidates'][0]['analysis'] = {'features': {}}
        self.assertEqual(runtime.pending_tool(row, set())[0], 'validate_tracks')

    def test_gap_batch_and_validation_reserve(self):
        row = self.row()
        row['request']['parsed']['target_count'] = 20
        with patch.object(service, 'qualified', return_value=[]), patch.object(service, 'deduplicated') as unique:
            for count, size in ((0, 3), (16, 2), (19, 1)):
                unique.return_value = [{}] * count
                self.assertEqual(service.batch_size(row), size)
        row['context']['limits']['network_requests'] = 300
        row['context']['budget']['network_requests'] = 224
        self.assertFalse(budget.validation_only(row))
        row['context']['budget']['network_requests'] = 225
        self.assertTrue(budget.validation_only(row))
        row['context']['candidates'] = [{'key': 'raw', 'title': 'Raw', 'artist': 'Artist'}, {'key': 'ready', 'title': 'Ready', 'artist': 'Artist', 'analysis': {'features': {}}}]
        self.assertEqual(runtime.pending_tool(row, set()), ('validate_tracks', {'keys': ['ready']}))
        row['context']['candidates'].pop()
        self.assertIsNone(runtime.pending_tool(row, set()))

    @patch.object(runtime.service, 'network_allowed', return_value=False)
    def test_offline_does_not_read_online_playlists(self, _):
        self.assertIsNone(runtime.pending_tool(self.row(), set()))


if __name__ == '__main__':
    unittest.main()
