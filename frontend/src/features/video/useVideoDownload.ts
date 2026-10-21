import { computed, reactive, ref, type Ref } from 'vue'
import { extractHttpUrl } from '../../shared/utils/downloadInput.ts'
import { readApiError } from '../../shared/api/client.ts'
import { safeFilename } from './format.ts'
import { saveResponseToFile, waitForFileSave } from './fileSave.ts'
import { downloadFilename, downloadMedia, nativeDownload, progressText, saveMessage as deliveryMessage } from '../../shared/downloads.ts'
import { downloadedPreviewUrl } from './videoPreview.ts'
import type { ParseResult, TaskResult, FilePickerHandle } from './types'

type DownloadContext = {
  url: Ref<string>
  parseResult: Ref<ParseResult | null>
  activeRecordId: Ref<string>
  revisions: { record: number }
  errorMessage: Ref<string>
  saveMessage: Ref<string>
  loadingDownload: Ref<boolean>
  task: Ref<TaskResult | null>
  selectedFormat: Ref<string>
  saveHandle: Ref<FilePickerHandle | null>
  savingFile: Ref<boolean>
  pollingTimer: Ref<number | undefined>
  localVideoUrl: Ref<string>
  prepareFormats: () => Promise<boolean>
  suggestedExtension: () => string
  refreshTaskReferences: () => Promise<void>
}

export function useVideoDownload(context: DownloadContext) {
  const {
    url,
    parseResult,
    activeRecordId,
    revisions,
    errorMessage,
    saveMessage,
    loadingDownload,
    task,
    selectedFormat,
    saveHandle,
    savingFile,
    pollingTimer,
    localVideoUrl,
    prepareFormats,
    suggestedExtension,
    refreshTaskReferences,
  } = context

  let pollingRevision = 0
  let activePoll: { revision: number; taskId: string } | null = null
  let handledTaskId = ''
  let activeSave: AbortController | null = null
  const savedTasks = reactive(new Set<string>())
  const fileSaved = computed(() => {
    const id = task.value?.task_id
    if (!id) return false
    if (savedTasks.has(id)) return true
    try { return window.localStorage?.getItem(`videodream.video.saved.${id}`) === '1' } catch { return false }
  })
  const saveProgress = ref('')
  const downloadDestination = ref<'device' | 'library'>('device')

  async function chooseSaveHandle(file?: TaskResult) {
    if (!window.showSaveFilePicker) return null
    const savedExtension = file?.filename?.split('.').pop()?.toLowerCase()
    const ext = savedExtension && ['mp4', 'webm', 'mkv', 'm4a', 'mp3'].includes(savedExtension) ? savedExtension : suggestedExtension()
    return window.showSaveFilePicker({
      suggestedName: downloadFilename(file?.filename || safeFilename(parseResult.value?.title || 'VideoDream', ext)),
      types: [
        {
          description: ['m4a', 'mp3'].includes(ext) ? 'Audio file' : 'Video file',
          accept:
            ['m4a', 'mp3'].includes(ext)
              ? { 'audio/mp4': ['.m4a'], 'audio/mpeg': ['.mp3'] }
              : { 'video/mp4': ['.mp4'], 'video/webm': ['.webm'], 'video/x-matroska': ['.mkv'] },
        },
      ],
    })
  }

  async function startDownload() {
    const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
    if (!target || loadingDownload.value || savingFile.value || ['pending', 'running'].includes(task.value?.status || '')) return
    const revision = revisions.record
    const recordId = activeRecordId.value
    errorMessage.value = ''
    saveMessage.value = ''
    loadingDownload.value = true
    stopPolling()
    saveHandle.value = null
    handledTaskId = ''
    task.value = null

    const controller = new AbortController()
    activeSave = controller
    try {
      // The picker must run during the click, before format refresh or parsing.
      const handle = downloadDestination.value === 'device' ? await chooseSaveHandle() : null
      if (!await prepareFormats() || revision !== revisions.record) return
      savingFile.value = downloadDestination.value === 'device'
      task.value = { task_id: `local-${Date.now()}`, status: 'running', progress: 0, message: '正在准备下载', destination: downloadDestination.value }
      const result = await downloadMedia({ url: target, title: parseResult.value?.title, format_id: selectedFormat.value || undefined,
        parse_record_id: recordId || undefined, destination: downloadDestination.value }, {
        handle, controller,
        onTask(data) { if (revision === revisions.record) task.value = data },
        onProgress(progress) {
          if (revision !== revisions.record) return
          saveProgress.value = progressText(progress)
          if (task.value) task.value.progress = progress.total ? Math.min(99, progress.bytes / progress.total * 100) : 0
        },
      })
      if (revision !== revisions.record) return
      task.value = { task_id: result.plan.task_id || result.plan.download_id, status: 'finished', progress: 100,
        message: deliveryMessage(result), filename: result.plan.filename, destination: downloadDestination.value,
        download_url: result.status === 'library' ? result.plan.url : undefined }
      if (result.status === 'saved') savedTasks.add(task.value.task_id)
      saveMessage.value = deliveryMessage(result)
      void refreshTaskReferences().catch(() => {})
    } catch (error) {
      if (revision === revisions.record && !(error instanceof DOMException && error.name === 'AbortError')) {
        errorMessage.value = error instanceof Error ? error.message : '下载失败。'
        if (task.value) { task.value.status = 'failed'; task.value.error = errorMessage.value }
      } else if (task.value && revision === revisions.record) { task.value.status = 'cancelled'; task.value.message = '下载已取消' }
    } finally {
      if (activeSave === controller) activeSave = null
      if (revision === revisions.record) { loadingDownload.value = false; savingFile.value = false; saveProgress.value = '' }
    }
  }

  async function startPolling(taskId: string) {
    stopPolling()
    const polling = pollingRevision
    const tick = async () => {
      await pollTask(taskId, polling)
      if (polling === pollingRevision && task.value?.task_id === taskId && ['pending', 'running'].includes(task.value.status)) {
        pollingTimer.value = window.setTimeout(() => void tick(), 1200)
      }
    }
    await tick()
  }

  async function pollTask(taskId: string, polling = pollingRevision) {
    if (polling !== pollingRevision || activePoll?.revision === polling) return
    const request = { revision: polling, taskId }
    activePoll = request
    const revision = revisions.record
    try {
      const response = await fetch(`/api/tasks/${taskId}`)
      if (!response.ok) throw new Error(await readApiError(response))
      const data = await response.json()
      if (polling !== pollingRevision || revision !== revisions.record || (task.value && task.value.task_id !== taskId)) return
      task.value = data
      if (['finished', 'failed', 'cancelled'].includes(task.value?.status || '')) {
        stopPolling()
        if (handledTaskId === taskId) return
        handledTaskId = taskId
        const handle = saveHandle.value
        saveHandle.value = null
        const playable = downloadedPreviewUrl(task.value)
        if (playable) localVideoUrl.value = playable
        // History refresh must not delay or repeat the claimed automatic save.
        void refreshTaskReferences().catch(() => {})
        if (task.value?.status === 'finished' && task.value.download_url && handle) await saveFinishedFile(handle)
      }
    } catch (error) {
      if (polling !== pollingRevision || revision !== revisions.record || (task.value && task.value.task_id !== taskId)) return
      stopPolling()
      errorMessage.value = error instanceof Error ? error.message : '任务状态查询失败。'
    } finally {
      if (activePoll === request) activePoll = null
    }
  }

  async function saveFinishedFile(handle?: FilePickerHandle | null) {
    if (!task.value?.download_url || activeSave || savingFile.value) return
    const snapshot = { ...task.value }
    const revision = revisions.record
    const filename = snapshot.filename || safeFilename(parseResult.value?.title || 'VideoDream', suggestedExtension())
    const controller = new AbortController()
    activeSave = controller
    const current = () => activeSave === controller && revision === revisions.record && task.value?.task_id === snapshot.task_id
    savingFile.value = true
    saveProgress.value = '正在选择保存位置…'
    errorMessage.value = ''
    saveMessage.value = ''

    try {
      const usePicker = Boolean(handle || window.showSaveFilePicker)
      const targetHandle = usePicker ? handle || (await chooseSaveHandle(snapshot)) : null
      if (!current() || controller.signal.aborted) return
      if (usePicker && !targetHandle) return
      if (!targetHandle) {
        nativeDownload(snapshot.download_url!, filename)
        saveMessage.value = '已交给浏览器下载，请在浏览器下载列表中查看保存结果。'
        return
      }
      saveProgress.value = '正在读取视频文件…'
      const response = await waitForFileSave(() => fetch(snapshot.download_url!, { signal: controller.signal }), controller)
      if (!response.ok) throw new Error(await waitForFileSave(() => readApiError(response), controller))
      if (targetHandle) {
        await saveResponseToFile(response, targetHandle, controller, (written, total) => {
          if (!current()) return
          const size = (written / 1024 / 1024).toFixed(1)
          saveProgress.value = total
            ? `正在保存到本机：${size} / ${(total / 1024 / 1024).toFixed(1)} MB（${Math.min(99, Math.floor(written / total * 100))}%）`
            : `正在保存到本机：${size} MB`
        }, () => { if (current()) saveProgress.value = '正在完成文件写入…' })
        if (current() && !controller.signal.aborted) {
          savedTasks.add(snapshot.task_id)
          try { window.localStorage?.setItem(`videodream.video.saved.${snapshot.task_id}`, '1') } catch { /* Saving works even when browser storage is unavailable. */ }
          saveMessage.value = '已保存到你选择的位置。'
        }
        return
      }


    } catch (error) {
      if (current() && !(error instanceof DOMException && error.name === 'AbortError')) {
        errorMessage.value = error instanceof Error ? error.message : '保存文件失败，请重试另存为。'
      }
      controller.abort(error)
    } finally {
      if (activeSave === controller) {
        activeSave = null
        savingFile.value = false
        saveProgress.value = ''
      }
    }
  }

  function cancelSave() {
    if (!activeSave) return
    activeSave.abort(new DOMException('保存已取消', 'AbortError'))
    saveMessage.value = '已取消本次保存，可直接重试另存为。'
  }

  function stopPolling() {
    pollingRevision++
    if (pollingTimer.value !== undefined) {
      window.clearTimeout(pollingTimer.value)
      pollingTimer.value = undefined
    }
  }

  function resetDownload() {
    stopPolling()
    activeSave?.abort(new DOMException('已切换视频', 'AbortError'))
    activeSave = null
    savingFile.value = false
    saveProgress.value = ''
    saveHandle.value = null
  }

  return { chooseSaveHandle, startDownload, startPolling, pollTask, saveFinishedFile, stopPolling, resetDownload, cancelSave, fileSaved, saveProgress, downloadDestination }
}
