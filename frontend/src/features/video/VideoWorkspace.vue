<script setup lang="ts">
import MindMapCanvas from './components/MindMapCanvas.vue'
import ExecutionTiming from './components/ExecutionTiming.vue'
import VideoHistory from './components/VideoHistory.vue'
import WatermarkEditor from '../watermark/WatermarkEditor.vue'
import { ArrowRight, Download, Eraser, ExternalLink, History, LoaderCircle, Music2, Play, RotateCcw, ShieldCheck, Trash2, Video, X } from 'lucide-vue-next'
import { formatDuration, plainMarkdown } from './format'
import { useVideoWorkspace } from './useVideoWorkspace'

const emit = defineEmits<{ back: []; activate: [] }>()
const returnMusicCenter = () => emit('back')
const {
  activeRecordId,
  browserBusy,
  browserSession,
  canDownload,
  canPreview,
  canSummarize,
  changeMapView,
  changeMode,
  closeVerificationBrowser,
  coverFailed,
  currentMapView,
  errorMessage,
  handlePreviewError,
  handlePreviewReady,
  hasRecentLinks,
  hideRecentLinksSoon,
  historyPanel,
  loadingDownload,
  loadingMindMap,
  loadingParse,
  loadingSummary,
  generateMindMap,
  statusFetchMs,
  mapRenderMs,
  mindMapTask,
  mindMapTone,
  mindMapVersions,
  mode,
  open,
  openVerificationBrowser,
  openVideoRecord,
  parseResult,
  parseVideo,
  previewError,
  previewState,
  previewUrl,
  previewVideoRef,
  recentLinks,
  recordMessage,
  removeRecentLink,
  removedVideoRecord,
  renderedMindMap,
  reparseHistory,
  resetPreview,
  retryVerifiedVideo,
  saveFinishedFile,
  saveMessage,
  savePlaybackProgress,
  saveVideoView,
  savingFile,
  scheduleVideoView,
  seekToTimestamp,
  selectMindMap,
  selectRecentLink,
  selectSummary,
  selectedFormat,
  showRecentLinks,
  sourceHost,
  startDownload,
  startMindMap,
  startPreview,
  startSummary,
  summaryTab,
  summaryTask,
  summaryTone,
  summaryVersions,
  supported,
  task,
  taskTone,
  url,
  verificationMessage,
  verificationTarget,
  videoWorkspace,
  viewSaveStatus,
} = useVideoWorkspace(returnMusicCenter, () => emit('activate'))
defineExpose({ open })
</script>

<template>
  <div class="tools-page">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent="returnMusicCenter">
        <span class="brand-mark">VD</span>
        <span>VideoDream</span>
      </a>
      <nav class="nav-links" aria-label="主导航">
        <a href="#" @click.prevent="returnMusicCenter">音乐中心</a>
        <a href="#workspace">下载工作台</a>
        <a href="#watermark">视频去水印</a>
        <a href="#platforms">支持平台</a>
        <a href="#notice">授权提醒</a>
      </nav>
      <a class="login-pill" href="#workspace"><Download :size="16" />下载工作台</a>
    </header>

    <main>
      <section id="workspace" class="hero">
        <div class="hero-copy">
          <span class="eyebrow">视频与音乐</span>
          <h1>VideoDream</h1>
        </div>
        <section class="download-panel" aria-label="下载工作台">
          <div class="download-modes" role="tablist" aria-label="下载类型">
            <button id="video-tab" role="tab" :aria-selected="mode === 'video'" :tabindex="mode === 'video' ? 0 : -1" aria-controls="video-panel" :class="{ active: mode === 'video' }" @click="changeMode('video')" @keydown.right.prevent="changeMode('music', true)" @keydown.left.prevent="changeMode('music', true)"><Video :size="21" /><span>视频下载</span></button>
            <button type="button" @click="changeMode('music')"><Music2 :size="21" /><span>音乐中心</span></button>
          </div>
          <div v-show="mode === 'video'" id="video-panel" role="tabpanel" aria-labelledby="video-tab">
          <nav class="video-workspace-tabs" aria-label="视频工作台视图"><button :class="{ active: videoWorkspace === 'current' }" @click="videoWorkspace = 'current'"><Video :size="17" />当前视频</button><button :class="{ active: videoWorkspace === 'history' }" @click="saveVideoView(); videoWorkspace = 'history'"><History :size="17" />解析历史</button></nav>
          <VideoHistory v-if="videoWorkspace === 'history'" ref="historyPanel" @open="openVideoRecord" @reparse="reparseHistory" @removed="removedVideoRecord" />
          <div v-show="videoWorkspace === 'current'">
          <div class="search-area">
            <form class="link-composer" @submit.prevent="parseVideo()">
              <textarea
                v-model="url"
                aria-label="视频链接或分享文字"
                placeholder="粘贴视频链接或分享文字…"
                autocomplete="off"
                @focus="showRecentLinks = true"
                @blur="hideRecentLinksSoon"
                @keydown.ctrl.enter.prevent="parseVideo()"
              />
              <div class="composer-footer"><span class="composer-source">抖音 · B站 · YouTube</span><button class="composer-submit" type="submit" :disabled="loadingParse || !url.trim()"><LoaderCircle v-if="loadingParse" :size="18" class="spin" /><ArrowRight v-else :size="18" />{{ loadingParse ? '解析中' : '解析视频' }}</button></div>
            </form>
            <div v-if="showRecentLinks && hasRecentLinks" class="recent-links" aria-label="最近输入链接">
              <div class="video-recent-head"><span>最近提交</span><button class="video-icon" title="清空最近链接" aria-label="清空最近链接" @mousedown.prevent="removeRecentLink()"><Trash2 :size="15" /></button></div>
              <div v-for="item in recentLinks" :key="item.id" class="video-recent-row"><button type="button" @mousedown.prevent="selectRecentLink(item.url)"><span>最近</span><strong>{{ item.url }}</strong></button><button class="video-icon" title="删除最近链接" aria-label="删除最近链接" @mousedown.prevent="removeRecentLink(item.id)"><X :size="15" /></button></div>
            </div>
          </div>

          <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
          <p v-if="recordMessage" class="video-record-message">{{ recordMessage }}</p>
          <p v-if="activeRecordId && viewSaveStatus" class="video-save-status" :class="{ failed: !['已保存', '保存中'].includes(viewSaveStatus) }" role="status">{{ viewSaveStatus }}</p>
          <div v-if="verificationTarget || browserSession?.visible" class="browser-verification" role="status" aria-live="polite">
            <div class="verification-status"><ShieldCheck :size="18" /><span>{{ verificationMessage }}</span></div>
            <div class="verification-actions">
              <button v-if="verificationTarget" type="button" class="secondary-action compact" :disabled="browserBusy || loadingParse" @click="openVerificationBrowser"><ExternalLink :size="16" />{{ browserSession?.visible ? '显示验证窗口' : '打开验证窗口' }}</button>
              <button v-if="verificationTarget" type="button" class="composer-submit" :disabled="browserBusy || loadingParse" @click="retryVerifiedVideo"><LoaderCircle v-if="loadingParse" :size="16" class="spin" /><RotateCcw v-else :size="16" />验证完成，重新解析</button>
              <button v-if="browserSession?.visible" type="button" class="verification-icon" aria-label="关闭验证窗口" title="关闭验证窗口，保留会话" :disabled="browserBusy || loadingParse" @click="closeVerificationBrowser()"><X :size="18" /></button>
              <button type="button" class="verification-icon" aria-label="清除抖音会话" title="清除抖音会话" :disabled="browserBusy || loadingParse" @click="closeVerificationBrowser(true)"><Eraser :size="18" /></button>
            </div>
          </div>
          <p v-if="saveMessage" class="success-text">{{ saveMessage }}</p>

          <div v-if="parseResult" class="result-card">
            <div class="cover-wrap">
              <video
                v-if="previewUrl"
                ref="previewVideoRef"
                :src="previewUrl"
                :poster="parseResult.thumbnail || undefined"
                controls
                playsinline
                preload="metadata"
                @timeupdate="savePlaybackProgress"
                @pause="scheduleVideoView"
                @loadedmetadata="handlePreviewReady"
                @error="handlePreviewError"
              />
              <template v-else>
                <img
                  v-if="parseResult.thumbnail && !coverFailed"
                  :src="parseResult.thumbnail"
                  :alt="parseResult.title"
                  @error="coverFailed = true"
                />
                <div v-else class="cover-empty">VD</div>
                <button
                  v-if="canPreview"
                  type="button"
                  class="preview-play-button"
                  :aria-label="previewState === 'error' ? '重新连接视频' : '播放视频'"
                  :title="previewState === 'error' ? '重新连接视频' : '播放视频'"
                  @click="startPreview"
                >
                  <RotateCcw v-if="previewState === 'error'" :size="21" /><Play v-else :size="21" />
                </button>
              </template>
              <div v-if="previewState === 'loading'" class="preview-loading" role="status">
                <span class="preview-spinner" aria-hidden="true"></span>
                <span>正在连接视频</span>
              </div>
              <p v-if="previewState === 'error' && previewError" class="preview-error">
                {{ previewError }}
              </p>
            </div>
            <div class="video-meta">
              <span class="status-chip">解析成功</span>
              <h2>{{ parseResult.title }}</h2>
              <div class="meta-row">
                <span v-if="parseResult.uploader">{{ parseResult.uploader }}</span>
                <span v-if="parseResult.duration">{{ formatDuration(parseResult.duration) }}</span>
                <a
                  v-if="parseResult.webpage_url"
                  :href="parseResult.webpage_url"
                  target="_blank"
                  rel="noreferrer"
                >
                  {{ sourceHost || '打开原网页' }}
                </a>
              </div>
              <label class="format-select">
                <span>下载格式</span>
                <select v-model="selectedFormat" @change="resetPreview">
                  <option value="">自动选择最佳格式</option>
                  <option v-for="item in parseResult.formats" :key="item.format_id" :value="item.format_id">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <button class="primary-action" :disabled="!canDownload" @click="startDownload">
                {{ loadingDownload ? '准备文件中' : '选择位置并下载' }}
              </button>
              <button class="secondary-action full-width" :disabled="!canSummarize" @click="startSummary">
                {{ loadingSummary ? '启动生成中' : generateMindMap ? '生成总结与思维导图' : '生成 AI 总结' }}
              </button>
              <label class="generation-option"><input v-model="generateMindMap" type="checkbox" :disabled="!canSummarize" />同时生成思维导图</label>
            </div>
          </div>

          <ExecutionTiming v-if="summaryTask" :summary="summaryTask" :map="mindMapTask" :render-ms="mapRenderMs" :fetch-ms="statusFetchMs" />
          <div v-if="summaryTask" class="summary-card" :class="`tone-${summaryTone}`">
            <div class="task-head">
              <span>{{ summaryTask.status === 'finished' ? '总结完成' : summaryTask.status === 'failed' ? '总结失败' : '正在总结' }}</span>
              <strong>{{ Math.round(summaryTask.progress) }}%</strong>
            </div>
            <div class="progress-track">
              <span :style="{ width: `${Math.max(4, summaryTask.progress)}%` }"></span>
            </div>
            <p class="summary-message">{{ summaryTask.error || summaryTask.message }}</p>
            <div v-if="summaryVersions.length > 1" class="video-version-controls"><label>总结版本<select :value="summaryTask.task_id" aria-label="总结版本" @change="selectSummary"><option v-for="item in summaryVersions" :key="item.task_id" :value="item.task_id">{{ new Date(item.created_at * 1000).toLocaleString() }} · {{ item.status === 'finished' ? '完成' : item.status === 'failed' ? '失败' : item.status === 'cancelled' ? '已取消' : '处理中' }}</option></select></label></div>

            <div v-if="summaryTask.result || mindMapTask" class="summary-result">
              <div v-if="summaryTask.result" class="summary-meta">
                <span>{{ summaryTask.result.source }}</span>
                <span>{{ summaryTask.result.language }}</span>
                <span>{{ summaryTask.result.transcript.length }} 段文本</span>
              </div>
              <div v-if="summaryTask.result" class="summary-actions">
                <a v-if="summaryTask.result.markdown_url" :href="summaryTask.result.markdown_url" download>下载 Markdown</a>
                <a v-if="summaryTask.result.json_url" :href="summaryTask.result.json_url" download>下载 JSON</a>
              </div>
              <div class="summary-tabs" role="tablist" aria-label="总结视图">
                <button type="button" role="tab" :aria-selected="summaryTab === 'summary'" @click="summaryTab = 'summary'">总结</button>
                <button type="button" role="tab" :aria-selected="summaryTab === 'transcript'" @click="summaryTab = 'transcript'">字幕与转写</button>
                <button type="button" role="tab" :aria-selected="summaryTab === 'mind-map'" @click="summaryTab = 'mind-map'">思维导图</button>
              </div>
              <section v-if="summaryTab === 'summary'" class="summary-text" aria-label="AI 总结">
                <pre v-if="summaryTask.result">{{ plainMarkdown(summaryTask.result.summary_markdown) }}</pre>
                <p v-else>{{ summaryTask.error || summaryTask.message }}</p>
              </section>
              <section v-else-if="summaryTab === 'transcript'" class="transcript-list" aria-label="字幕文本">
                <div v-for="item in summaryTask.result?.transcript.slice(0, 80) || []" :key="`${item.timestamp}-${item.text}`">
                  <time>{{ item.timestamp }}</time>
                  <span>{{ item.text }}</span>
                </div>
              </section>
              <section v-else class="mind-map-panel" aria-label="思维导图">
                <div v-if="mindMapVersions.length" class="video-version-controls"><label>导图版本<select :value="mindMapTask?.task_id || renderedMindMap?.task_id" aria-label="导图版本" @change="selectMindMap"><option v-for="item in mindMapVersions" :key="item.task_id" :value="item.task_id">v{{ item.version }} · {{ item.status === 'finished' ? '完成' : item.status === 'failed' ? '失败' : item.status === 'cancelled' ? '已取消' : '生成中' }}</option></select></label><button class="video-icon" title="重新生成思维导图" aria-label="重新生成思维导图" :disabled="loadingMindMap || ['pending', 'running'].includes(mindMapTask?.status || '')" @click="startMindMap(true)"><RotateCcw :size="17" /></button></div>
                <div v-if="!mindMapTask" class="mind-map-empty">
                  <button type="button" class="secondary-action" :disabled="loadingMindMap" @click="startMindMap(false)">
                    {{ loadingMindMap ? '正在启动' : '生成思维导图' }}
                  </button>
                </div>
                <template v-else>
                  <div class="task-head mind-map-task-head">
                    <span>{{ mindMapTask.status === 'finished' ? '思维导图完成' : mindMapTask.status === 'failed' ? '生成失败' : '正在生成思维导图' }}</span>
                    <strong>{{ Math.round(mindMapTask.progress) }}%</strong>
                  </div>
                  <div class="progress-track" :class="`tone-${mindMapTone}`">
                    <span :style="{ width: `${Math.max(4, mindMapTask.progress)}%` }"></span>
                  </div>
                  <p v-if="mindMapTask.error || mindMapTask.status !== 'finished'" class="summary-message">
                    {{ mindMapTask.error || mindMapTask.message }}
                  </p>
                  <div v-if="mindMapTask.status === 'failed'" class="mind-map-retry">
                    <button type="button" class="secondary-action" :disabled="loadingMindMap" @click="startMindMap(true)">重新生成</button>
                  </div>
                  <div v-if="renderedMindMap?.result" class="mind-map-tree-wrap">
                    <MindMapCanvas
                      :key="renderedMindMap.task_id"
                      :result="renderedMindMap.result"
                      :saved-view="currentMapView"
                      :seek-enabled="Boolean(previewVideoRef || canPreview)"
                      @seek="seekToTimestamp"
                      @view-change="changeMapView"
                      @rendered="mapRenderMs = $event"
                    />
                  </div>
                </template>
              </section>
            </div>
          </div>

          <div v-if="task" class="task-card" :class="`tone-${taskTone}`">
            <div class="task-head">
              <span>{{ task.status === 'finished' ? '下载完成' : task.status === 'failed' ? '处理失败' : '任务进行中' }}</span>
              <strong>{{ Math.round(task.progress) }}%</strong>
            </div>
            <div class="progress-track">
              <span :style="{ width: `${Math.max(4, task.progress)}%` }"></span>
            </div>
            <p>{{ task.error || task.message }}</p>
            <button v-if="task.download_url" class="download-link" :disabled="savingFile" @click="saveFinishedFile()">
              {{ savingFile ? '保存中' : '另存为' }}
            </button>
            <a v-if="task.status === 'finished' && task.filename" class="secondary-action compact watermark-entry" href="#watermark"><Eraser :size="16" aria-hidden="true" />去水印</a>
          </div>
          </div>
          </div>
        </section>
      </section>

      <WatermarkEditor :preferred-filename="task?.status === 'finished' ? task.filename : null" />

      <section id="platforms" class="platform-band">
        <div class="section-title">
          <span>通用支持</span>
          <h2>来源平台</h2>
        </div>
        <div class="tag-grid">
          <a v-for="item in supported" :key="item.href" :href="item.href" target="_blank" rel="noreferrer">
            {{ item.label }}
          </a>
        </div>
      </section>

      <section id="notice" class="notice">
        <strong>授权提醒</strong>
        <span>请仅下载你拥有权利、获得授权或平台允许保存的内容。</span>
      </section>
    </main>
  </div>
</template>

<style scoped>
.generation-option{display:flex;align-items:center;gap:8px;font-size:13px;color:#52647c;padding-top:10px}.generation-option input{width:16px;height:16px}
</style>
