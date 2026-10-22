export function deviceAudioFailure(code = 0) {
  if (code === 3) return '本地音频解码失败，文件可能已损坏或含不支持的编码。请更换有效文件后重新导入。'
  if (code === 4) return '当前浏览器无法解码此音频，请使用有效的 AAC 编码 M4A 或 MP3 文件。'
  return '本地文件读取失败，请重新定位或授权文件。'
}

// Metadata alone can describe a damaged M4A. Wait for decoded media data too.
export function inspectDeviceAudio(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    const audio = document.createElement('audio')
    const url = URL.createObjectURL(file)
    let settled = false
    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      const duration = audio.duration
      clearTimeout(timer)
      audio.onloadeddata = null; audio.onerror = null
      audio.pause(); audio.removeAttribute('src'); audio.load()
      URL.revokeObjectURL(url)
      if (error) reject(new Error(`${file.name}：${error.message}`))
      else resolve(duration)
    }
    const timer = setTimeout(() => finish(new Error('浏览器未能读取音频，请检查文件和格式后重试。')), 10000)
    audio.preload = 'auto'
    audio.muted = true
    audio.onloadeddata = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) finish()
      else finish(new Error('无法读取有效音频时长，请检查文件是否完整。'))
    }
    audio.onerror = () => finish(new Error(deviceAudioFailure(audio.error?.code)))
    audio.src = url
    audio.load()
  })
}
