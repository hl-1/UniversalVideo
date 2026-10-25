import assert from 'node:assert/strict'
import test from 'node:test'
import { changeNotice, feedNotice, type DailyFeed } from '../src/features/music/dailyFeed.ts'
const feed: DailyFeed = { feed_id: 'one', tracks: [], profile_revision: 1, latest_profile_revision: 1, agent_revision: 0, refreshing: false, can_change: true, retry_after_seconds: 0, reason: 'ready' }
const tracks = Array.from({ length: 6 }, (_, i) => ({ title: `歌曲 ${i}`, webpage_url: `https://example.com/song/${i}` }))
test('empty and partial lists explain automatic preparation without operational details', () => {
  assert.match(feedNotice({ ...feed, reason: 'cold_start' }), /收藏/)
  for (const reason of ['ready', 'refreshing', 'exhausted', 'insufficient_candidates'] as const) {
    assert.match(feedNotice({ ...feed, reason }), /准备好后会自动显示/)
    assert.match(feedNotice({ ...feed, reason, tracks: tracks.slice(0, 5) }), /自动补齐/)
  }
  assert.match(feedNotice({ ...feed, reason: 'error' }), /服务暂时不可用/)
})
test('a full list stays quiet during maintenance, stale validation and unavailable refills', () => {
  for (const state of ['running', 'cooldown', 'budget_exhausted', 'disabled', 'idle'] as const) {
    for (const reason of ['ready', 'refreshing', 'exhausted', 'insufficient_candidates'] as const) {
      assert.equal(feedNotice({ ...feed, tracks, refreshing: state === 'running', reason,
        validation_stale: true, refill: { state, retry_at: Date.now() / 1000 + 60 } }), '')
    }
  }
})
test('ordinary listening pages do not expose quotas, schedules or administrator settings', () => {
  for (const state of ['running', 'cooldown', 'budget_exhausted', 'disabled', 'idle'] as const) {
    assert.doesNotMatch(feedNotice({ ...feed, refill: { state, retry_at: Date.now() / 1000 + 60 } }), /额度|后台|管理员|间隔|音源|月.*日/)
  }
})
test('unsuccessful deliberate list changes receive a short action response', () => {
  const previous = { ...feed, tracks }
  assert.equal(changeNotice(previous, { ...previous, reason: 'insufficient_candidates' }), '新推荐准备中，稍后再试。')
  assert.equal(changeNotice(previous, { ...previous, feed_id: 'two' }), '')
  assert.match(changeNotice(previous, { ...previous, feed_id: 'two', tracks: tracks.slice(0, 5) }), /稍后再试/)
})
test('explicit profile updates explain when preferences will take effect', () => {
  assert.match(feedNotice({ ...feed, tracks, latest_profile_revision: 2 }), /偏好已更新，下批生效/)
})
