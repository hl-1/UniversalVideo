export type DiscoveryState = {
  status: string
  tracks: unknown[]
  gap_reason?: string
  usage_pending?: boolean
}

// Terminal failures persisted by the old per-request token guard. The guard is fixed,
// so the record stays for accounting but must not read like a current generation error.
const LEGACY_BUDGET_FAILURE = '模型实际用量超过预留或任务上限'

export function discoveryStatus(batch: DiscoveryState | null, _remaining?: number | null) {
  if (!batch) return null
  const status = batch.status
  if (status === 'pending' || status === 'running') return null
  const pending = batch.usage_pending ? '上次模型调用的用量尚未确认，已保留记录，不会自动重发。' : ''
  const retry = (title: string, detail: string) => ({ title, detail: [detail, pending].filter(Boolean).join(' '), retry: true })
  if (status === 'interrupted') return retry('推荐生成被中断', '任务执行中断，不能据此判断是否有适合你的歌曲。')
  if (status === 'cancelled') return retry('已取消生成', '本次生成已停止。')
  if (status === 'waiting_auth') return retry('需要重新登录', '请重新登录并确认服务可用后，再重新生成推荐。')
  if (status === 'waiting_input') return retry('需要补充听歌偏好', '请在“调整推荐”中补充需求后重新生成。')
  if (status === 'failed') {
    if ((batch.gap_reason || '').includes(LEGACY_BUDGET_FAILURE)) {
      return retry('今日推荐尚未生成', '上次生成被旧版预算判断中止，代码已修复；点击“重新生成”会使用当前流程重新获取。')
    }
    return retry('本次未能生成推荐', batch.gap_reason || '生成失败，请稍后重试。')
  }
  if (status === 'finished' || status === 'partial') {
    if (!batch.tracks.length) return { title: '当前没有可播放的推荐', detail: '歌曲可能已被反馈过滤，可以调整偏好或换一批。', retry: false }
    if (status === 'partial') {
      const networkReserve = (batch.gap_reason || '').includes('网络额度已预留给播放校验')
      return { title: '已为你找到部分歌曲', detail: networkReserve
        ? '为避免推荐无法播放，本次优先保留了播放校验额度；已找到的歌曲可以直接播放。'
        : batch.gap_reason || '其余候选暂未通过筛选，已找到的歌曲可以直接播放。', retry: false }
    }
    return null
  }
  return retry('推荐暂不可用', '任务状态暂时无法识别，请刷新后重试。')
}
