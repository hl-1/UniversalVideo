import type { ComputedRef, Ref } from 'vue'
import { extractHttpUrl } from '../../shared/utils/downloadInput'
import { readApiError } from '../../shared/api/client'
import { safeFilename } from './format'
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
  taskTone: ComputedRef<string>
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
    taskTone,
    pollingTimer,
    localVideoUrl,
    prepareFormats,
    suggestedExtension,
    refreshTaskReferences,
  } = context

  async function chooseSaveHandle() {
    if (!window.showSaveFilePicker || !parseResult.value) return null
    const ext = suggestedExtension()
    return window.showSaveFilePicker({
      suggestedName: safeFilename(parseResult.value.title, ext),
      types: [
        {
          description: ext === 'm4a' ? 'Audio file' : 'Video file',
          accept:
            ext === 'm4a'
              ? { 'audio/mp4': ['.m4a'], 'audio/mpeg': ['.mp3'] }
              : { 'video/mp4': ['.mp4'], 'video/webm': ['.webm'], 'video/x-matroska': ['.mkv'] },
        },
      ],
    })
  }

  async function startDownload() {
    const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
    if (!target || loadingDownload.value) return
    const revision = revisions.record
    const recordId = activeRecordId.value
    errorMessage.value = ''
    saveMessage.value = ''
    loadingDownload.value = true
    task.value = null

    try {
      if (!await prepareFormats() || revision !== revisions.record) return
      if (window.showSaveFilePicker) {
        try {
          const handle = await chooseSaveHandle()
          if (revision !== revisions.record) return
          saveHandle.value = handle
        } catch (error) {
          if (error instanceof DOMException && error.name === 'AbortError') return
          throw error
        }
      }

      const response = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: target,
          format_id: selectedFormat.value || undefined,
          parse_record_id: recordId || undefined,
        }),
      })
      if (!response.ok) throw new Error(await readApiError(response))
      const data: { task_id: string } = await response.json()
      if (revision !== revisions.record) return
      await pollTask(data.task_id)
      if (revision !== revisions.record) return
      if (['pending', 'running'].includes(taskTone.value)) pollingTimer.value = window.setInterval(() => pollTask(data.task_id), 1200)
      await refreshTaskReferences()
    } catch (error) {
      if (revision === revisions.record) errorMessage.value = error instanceof Error ? error.message : '下载任务创建失败。'
    } finally {
      if (revision === revisions.record) loadingDownload.value = false
    }
  }

  async function pollTask(taskId: string) {
    const revision = revisions.record
    try {
      const response = await fetch(`/api/tasks/${taskId}`)
      if (!response.ok) throw new Error(await readApiError(response))
      const data = await response.json()
      if (revision !== revisions.record || (task.value && task.value.task_id !== taskId)) return
      task.value = data
      if (['finished', 'failed', 'cancelled'].includes(task.value?.status || '')) {
        stopPolling()
        await refreshTaskReferences()
        if (revision !== revisions.record || task.value?.task_id !== taskId) return
        if (task.value?.status === 'finished' && task.value.download_url && /\.(mp4|webm)$/i.test(task.value.filename || '')) localVideoUrl.value = task.value.download_url
        if (task.value?.status === 'finished' && task.value.download_url && saveHandle.value) {
          await saveFinishedFile(saveHandle.value)
        }
      }
    } catch (error) {
      if (revision !== revisions.record || (task.value && task.value.task_id !== taskId)) return
      stopPolling()
      errorMessage.value = error instanceof Error ? error.message : '任务状态查询失败。'
    }
  }

  async function saveFinishedFile(handle?: FilePickerHandle | null) {
    if (!task.value?.download_url) return
    savingFile.value = true
    errorMessage.value = ''
    saveMessage.value = ''

    try {
      const response = await fetch(task.value.download_url)
      if (!response.ok) throw new Error(await readApiError(response))
      const blob = await response.blob()

      if (handle || window.showSaveFilePicker) {
        const targetHandle = handle || (await chooseSaveHandle())
        if (!targetHandle) return
        const writable = await targetHandle.createWritable()
        await writable.write(blob)
        await writable.close()
        saveMessage.value = '已保存到你选择的位置。'
        saveHandle.value = null
        return
      }

      const objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = task.value.filename || safeFilename(parseResult.value?.title || 'VideoDream', suggestedExtension())
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(objectUrl)
      saveMessage.value = '浏览器已开始下载文件。'
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '保存文件失败。'
    } finally {
      savingFile.value = false
    }
  }

  function stopPolling() {
    if (pollingTimer.value) {
      window.clearInterval(pollingTimer.value)
      pollingTimer.value = undefined
    }
  }

  return { chooseSaveHandle, startDownload, pollTask, saveFinishedFile, stopPolling }
}
