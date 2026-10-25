import assert from 'node:assert/strict'
import test from 'node:test'
import { discoveryStatus } from '../src/features/music/discoveryStatus.ts'

test('interrupted generation preserves unknown usage without a batch quota', () => {
  const result = discoveryStatus({ status: 'interrupted', tracks: [], usage_pending: true }, 2)!
  assert.equal(result.title, '推荐生成被中断')
  assert.equal(result.retry, true)
  assert.match(result.detail, /用量尚未确认/)
  assert.doesNotMatch(result.detail, /今日次数|还剩.*次/)
  assert.doesNotMatch(result.detail, /调整偏好/)
})
test('legacy budget failure is presented as a retryable unfinished state', () => {
  const result = discoveryStatus({ status: 'failed', tracks: [], gap_reason: '模型实际用量超过预留或任务上限，已如实记账并停止调用' }, 1)!
  assert.equal(result.title, '今日推荐尚未生成')
  assert.equal(result.retry, true)
  assert.doesNotMatch(result.detail, /模型实际用量超过预留或任务上限/)
  assert.match(result.detail, /旧版预算判断/)
  assert.doesNotMatch(result.detail, /还剩.*次/)
})
test('partial results explain the playback-validation reserve without internal terms', () => {
  const result = discoveryStatus({ status: 'partial', tracks: [{}], gap_reason: '剩余网络额度已预留给播放校验' }, 0)!
  assert.equal(result.title, '已为你找到部分歌曲')
  assert.match(result.detail, /保留.*播放校验额度/)
  assert.doesNotMatch(result.detail, /网络额度已预留给播放校验/)
})
test('legacy remaining count never disables algorithmic changes', () => {
  const result = discoveryStatus({ status: 'interrupted', tracks: [] }, 0)!
  assert.doesNotMatch(result.detail, /今日次数已用完/)
  assert.doesNotMatch(result.detail, /还剩/)
})
test('running, failure, cancellation and filtered results have distinct states', () => {
  assert.equal(discoveryStatus({ status: 'running', tracks: [] }, 2), null)
  assert.equal(discoveryStatus({ status: 'pending', tracks: [] }, 2), null)
  for (const [status, title] of [['failed', '本次未能生成推荐'], ['cancelled', '已取消生成'], ['waiting_auth', '需要重新登录'], ['waiting_input', '需要补充听歌偏好']]) {
    assert.equal(discoveryStatus({ status: status!, tracks: [] }, 2)!.title, title)
  }
  assert.equal(discoveryStatus({ status: 'finished', tracks: [] }, 2)!.retry, false)
  assert.equal(discoveryStatus({ status: 'partial', tracks: [{}] }, 2)!.title, '已为你找到部分歌曲')
  assert.equal(discoveryStatus({ status: 'finished', tracks: [{}] }, 2), null)
})
