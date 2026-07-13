# VideoDream 视频下载助手

当前版本：`v1.0.0`

VideoDream 是一个本地运行的视频下载工作台，前端使用 Vue 3 + Vite，后端使用 FastAPI。它封装 `yt-dlp`、浏览器页面解析和任务下载能力，支持先解析视频信息，再选择格式和保存位置。

## 已支持的网站

### 抖音

- 支持抖音公开视频链接，例如 `https://www.douyin.com/video/...` 和带 `modal_id` 的精选页链接。
- 使用本机浏览器解析真实视频流。
- 支持展示真实标题、作者和封面。
- 封面优先使用页面信息，失败时自动生成本地截图封面。

### YouTube

- 支持公开 YouTube 视频链接。
- 优先使用浏览器解析可播放资源。
- 支持音视频分离场景下的自动合并。
- 遇到机器人校验、登录态或年龄确认时会给出明确提示；当前版本不读取 Cookie。

### PornHub

- 支持原始视频页面链接，例如 `https://cn.pornhub.com/view_video.php?viewkey=...`。
- 通过页面播放器数据获取真实 `mediaDefinitions`。
- 在浏览器页面上下文中请求 `get_media`，获取 signed MP4 直链。
- 默认优先展示可直接下载的 MP4 格式，HLS 作为备用格式。
- 已过滤缩略图、广告素材、空数组响应等无效资源，避免生成打不开的 MP4。

### B站

- 支持 B站公开公开视频链接。
- 自动补充常规浏览器请求头。
- 遇到 412 或登录态校验时会给出明确提示；当前版本仅支持无需 Cookie 的公开资源。

### yt-dlp 通用站点

- 其他 `yt-dlp` 支持的公开视频站点会走通用解析能力。
- 实际可用性取决于目标站点是否公开、是否需要登录态，以及 `yt-dlp` 当前版本支持情况。

## 核心功能

- 输入视频链接并解析标题、封面、作者、时长和可用格式。
- 选择下载格式。
- 创建后台下载任务并查看进度。
- 支持浏览器系统保存对话框，选择本地保存位置。
- 支持 MP4 直链下载、HLS 转 MP4、音视频合并。
- 支持基于平台字幕/自动字幕生成 AI 视频总结，并展示带时间戳的字幕文本。
- 支持把 AI 总结结果保存为 Markdown 和 JSON。
- 下载结果会校验，避免图片、空响应或无效小文件被当作视频保存。

## AI 视频总结配置

AI 总结使用 OpenAI 兼容接口。复制 `config/ai.example.json` 为 `config/ai.json`，填写：

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "replace-with-your-api-key",
  "model": "gpt-4.1-mini"
}
```

B 站总结优先调用平台字幕 API，直接获取创作者上传或平台生成的字幕；平台接口没有字幕时，再使用 yt-dlp 提取内嵌字幕或自动字幕。弹幕不会作为字幕参与总结。

`config/ai.json` 已被 `.gitignore` 忽略，请不要提交真实密钥。

## 运行环境

- Python 3.11+
- Node.js 20+
- ffmpeg

项目当前默认使用：

```text
C:\softWare\environment\ffmpeg
```

## 启动后端

```powershell
cd C:\softWare\project\vD
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

## 启动前端

```powershell
cd C:\softWare\project\vD\frontend
npm.cmd install
npm.cmd run dev -- --host 127.0.0.1
```

访问：

```text
http://127.0.0.1:5174
```

## 项目结构

```text
backend/      FastAPI 后端、解析与下载任务
frontend/     Vue 3 前端工作台
downloads/    本地临时下载目录，已加入 .gitignore
docs/         需求与方案文档
scripts/      本地启动脚本
```

## 发布记录

详见 [CHANGELOG.md](CHANGELOG.md)。
