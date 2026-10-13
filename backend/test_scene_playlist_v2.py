import copy
import unittest
from unittest.mock import Mock, patch

from backend import lyric_lookup
from backend.music_agent import features, model, runtime
from backend.music_agent.models import ParsedNeed, ResultMatch


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
        attempted = {runtime.store.encode(list(action))}
        self.assertIsNone(runtime.pending_tool(row, attempted))

    @patch.object(runtime.service, 'network_allowed', return_value=True)
    def test_analysis_and_validation_precede_new_playlists(self, _):
        row = self.row()
        row['context']['candidates'] = [{'key': 'song'}]
        self.assertEqual(runtime.pending_tool(row, set())[0], 'get_track_features')
        row['context']['candidates'][0]['analysis'] = {'features': {}}
        self.assertEqual(runtime.pending_tool(row, set())[0], 'validate_tracks')

    @patch.object(runtime.service, 'network_allowed', return_value=False)
    def test_offline_does_not_read_online_playlists(self, _):
        self.assertIsNone(runtime.pending_tool(self.row(), set()))


if __name__ == '__main__':
    unittest.main()
