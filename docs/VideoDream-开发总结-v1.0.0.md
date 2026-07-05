# VideoDream v1.0.0 开发总结

日期：2026-07-05

## 1. 项目概览

VideoDream 是一个本地运行的视频下载助手，采用 `FastAPI + Vue 3` 架构。项目目标是把多平台视频解析、格式选择、任务下载、本地另存为整合到一个清爽的网页工作台中。

核心流程：

```text
输入视频链接 -> 解析视频信息 -> 选择格式 -> 创建下载任务 -> 查看进度 -> 保存本地文件
```

当前版本：`v1.0.0`

GitHub 仓库：

```text
https://github.com/hl-1/UniversalVideo
```

## 2. 初始 Prompt 与需求

本次开发基于以下核心要求推进：

- 使用 Python 作为后端技术栈。
- 使用 `yt-dlp` 作为通用下载能力基础。
- 前端模仿参考站点的 UI 风格，采用白底、蓝色强调、胶囊输入框、柔和阴影卡片、付费感文案。
- 首版先跑通核心业务流程，不引入数据库。
- 下载文件本地保存。
- 支持用户选择最终保存位置。
- 重点支持抖音、YouTube、PornHub、B站等公开视频链接。
- 沉淀需求分析、方案设计和开发总结文档，方便后续 AI 继续扩展。

人工方案中的关键决策：

- 不做复杂用户系统。
- 不做数据库。
- 后端封装 `yt-dlp`。
- 下载任务使用内存任务表。
- 下载目录放在项目根目录 `downloads/`。
- 对抖音等特殊平台可单独实现专用解析。

## 3. 环境搭建

### 3.1 后端环境

后端使用 Python 虚拟环境：

```powershell
cd C:\softWare\project\vD
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

后端依赖：

```text
fastapi==0.128.0
uvicorn[standard]==0.35.0
yt-dlp==2026.7.4
pydantic==2.12.5
requests==2.32.5
playwright==1.57.0
douyin-tiktok-scraper==1.2.9
```

### 3.2 前端环境

前端使用 Vite + Vue 3 + TypeScript：

```powershell
cd C:\softWare\project\vD\frontend
npm.cmd install
```

主要依赖：

```text
vue
vite
typescript
vue-tsc
@vitejs/plugin-vue
```

### 3.3 ffmpeg

用于音视频合并、HLS 转 MP4：

```text
C:\softWare\environment\ffmpeg
```

后端会检测：

```text
C:\softWare\environment\ffmpeg\ffmpeg.exe
```

### 3.4 浏览器环境

浏览器嗅探使用 Playwright 调用本机浏览器：

```text
C:\Users\11871\AppData\Local\Google\Chrome\Application\chrome.exe
C:\Program Files (x86)\Microsoft\EdgeCore\126.0.2592.113\msedge.exe
```

## 4. 项目结构

```text
backend/
  main.py              FastAPI 服务、解析、下载、任务管理
  requirements.txt     后端依赖

frontend/
  src/App.vue          主页面
  src/styles.css       页面样式
  package.json         前端依赖和脚本

downloads/
  .gitkeep             临时下载目录占位
  _thumbnails/         本地封面截图缓存，已忽略

docs/
  VideoDream-需求分析.md
  VideoDream-方案设计.md
  VideoDream-开发总结-v1.0.0.md

scripts/
  start-backend.ps1
  start-frontend.ps1

AGENTS.md              Codex 协作规则
CHANGELOG.md           发布记录
README.md              项目说明
```

## 5. 后端接口

### 5.1 健康检查

```http
GET /api/health
```

返回：

```json
{ "status": "ok" }
```

### 5.2 解析视频

```http
POST /api/parse
```

请求：

```json
{
  "url": "https://...",
  "use_browser_cookies": false,
  "browser": "auto"
}
```

返回：

```json
{
  "title": "...",
  "thumbnail": "...",
  "duration": 123,
  "uploader": "...",
  "webpage_url": "...",
  "formats": []
}
```

### 5.3 创建下载任务

```http
POST /api/download
```

请求：

```json
{
  "url": "https://...",
  "format_id": "..."
}
```

返回：

```json
{ "task_id": "..." }
```

### 5.4 查询任务

```http
GET /api/tasks/{task_id}
```

状态：

```text
pending | running | finished | failed
```

### 5.5 下载文件

```http
GET /api/files/{filename}
```

### 5.6 图片代理和本地缩略图

```http
GET /api/image-proxy?url=...
GET /api/thumbnails/{filename}
```

## 6. 前端实现

前端是单页下载工作台，首屏就是核心功能区。

主要模块：

- 顶部导航：品牌、下载工作台、支持平台、授权提醒。
- 链接输入框：胶囊输入，蓝色解析按钮。
- 授权模式：可选择本机浏览器登录态。
- 解析结果卡片：封面、标题、作者、时长、来源链接、格式选择。
- 下载任务卡片：进度条、状态、错误、另存为按钮。
- 平台与权益展示：突出高清解析、格式选择、本地保存等能力。

UI 风格：

- 白底轻导航。
- 蓝色主按钮。
- 圆角胶囊输入框。
- 柔和阴影卡片。
- 紧凑但有付费感的功能文案。

## 7. 已实现网站能力

### 7.1 抖音

已实现：

- 支持 `https://www.douyin.com/video/...`
- 支持精选页 `modal_id` 链接归一化。
- 使用浏览器嗅探真实 `douyinvod` 视频流。
- 提取页面标题、作者。
- 封面优先读取页面信息，失败时本地截图兜底。
- 过滤客户端下载、静态动画等干扰 MP4。

关键修复：

- 防止把 `douyin_pc_client.mp4` 当作目标视频。
- 防止封面加载失败时显示空白。
- 通过本地截图生成封面，避免远程签名图失效。

### 7.2 YouTube

已实现：

- 支持公开 YouTube 链接。
- 浏览器嗅探 `googlevideo.com/videoplayback`。
- 识别 progressive MP4。
- 识别视频流和音频流，并可用 ffmpeg 合并。
- YouTube 封面使用 `i.ytimg.com` 兜底。

### 7.3 PornHub

已实现：

- 支持原始页面链接：

```text
https://cn.pornhub.com/view_video.php?viewkey=...
```

- 读取页面播放器变量 `flashvars_*`。
- 读取 `mediaDefinitions`。
- 在浏览器页面上下文中请求 `get_media`，携带页面 credentials。
- 获取真实 signed MP4：

```text
https://ev.phncdn.com/.../720P_4000K_....mp4?validfrom=...&validto=...&hash=...
```

- 默认优先展示 MP4，HLS 作为备用。
- 过滤无效资源：
  - `pix-*` 缩略图。
  - 广告 MP4。
  - CSS/JS。
  - `[]` 空数组响应。
  - 图片文件伪装成 MP4。
  - 小于 1KB 的无效文件。

关键修复：

- 修复 `470 No Token`。
- 修复把 JPEG 保存成 MP4。
- 修复 `get_media` 裸请求返回 `[]`。
- 修复 HLS 卡 95% 的问题。
- 通过浏览器上下文请求 `get_media` 获取真实 MP4。

### 7.4 B站

已实现：

- 支持公开视频链接。
- 补充浏览器请求头。
- 支持 cookies / 本机授权模式兜底。

已处理问题：

- `HTTP Error 412: Precondition Failed`

### 7.5 yt-dlp 通用站点

其他 `yt-dlp` 支持的网站走通用解析：

```text
yt-dlp extract_info -> 格式整理 -> 下载任务
```

实际可用性取决于平台是否公开、是否需要登录态、当前 `yt-dlp` 版本是否支持。

## 8. 关键 Bug 修复记录

### 8.1 B站 412

现象：

```text
Unable to download JSON metadata: HTTP Error 412
```

处理：

- 增加浏览器 User-Agent。
- 增加 Origin / Referer。
- 支持 cookies 和本机授权模式。

### 8.2 B站视频无声音

原因：

- 选中了 video-only 格式。

处理：

- 解析格式时检测视频无音频。
- 自动拼接 `+bestaudio/best`。
- 后端使用 ffmpeg 合并。

### 8.3 封面无法显示

原因：

- 远程封面防盗链或签名过期。

处理：

- 增加图片代理。
- 根据平台设置 Referer。
- 抖音增加本地截图封面兜底。

### 8.4 YouTube 机器人校验

现象：

```text
Sign in to confirm you’re not a bot
```

处理：

- 增加本机授权模式。
- 支持读取本机浏览器登录态。

### 8.5 抖音解析慢和信息不准

原因：

- 页面动态加载。
- 静态动画 MP4 干扰。
- 公开 API 不稳定。

处理：

- 浏览器嗅探优先。
- 等待真实 `douyinvod`。
- 过滤静态资源。
- 从页面 `h1`、正文和 DOM 中提取元信息。

### 8.6 PornHub 470 No Token

原因：

- 抓到了缺少签名参数的裸 CDN MP4。

处理：

- 不再使用 `original_*.mp4` 裸链。
- 通过播放器变量和浏览器上下文获取 signed MP4。

### 8.7 PornHub 下载后打不开

原因：

- 缩略图 JPEG 被误保存成 MP4。
- `get_media` 裸请求返回 `[]`。

处理：

- 过滤 `pix-*` 缩略图。
- 校验 Content-Type。
- 校验文件头。
- 校验小文件。
- 在页面上下文中请求 `get_media`。

### 8.8 HLS 卡 95%

原因：

- ffmpeg 下载 HLS 时 stderr 管道可能堵塞。
- HLS 源不稳定时任务长时间不结束。

处理：

- ffmpeg 使用 `-hide_banner -loglevel error -nostdin`。
- stderr 改为 `DEVNULL`。
- 增加 reconnect 参数。
- 增加 30 秒无文件增长失败保护。
- 增加 180 秒超时保护。

## 9. 本地部署指南

### 9.1 克隆项目

```powershell
git clone https://github.com/hl-1/UniversalVideo.git
cd UniversalVideo
```

### 9.2 配置 Python

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### 9.3 配置 Playwright

当前代码优先使用本机 Chrome / Edge 路径。如果迁移到其他机器，需要确认浏览器路径：

```text
CHROME_EXE
EDGE_EXE
```

如需安装 Playwright 浏览器：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 9.4 配置 ffmpeg

安装 ffmpeg，并修改后端常量：

```python
FFMPEG_DIR = Path(r"C:\softWare\environment\ffmpeg")
```

### 9.5 启动后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### 9.6 启动前端

```powershell
cd frontend
npm.cmd install
npm.cmd run dev -- --host 127.0.0.1
```

访问：

```text
http://127.0.0.1:5174
```

## 10. 迁移指南

迁移到新电脑时，需要复制：

```text
backend/
frontend/
docs/
scripts/
README.md
CHANGELOG.md
AGENTS.md
.gitignore
```

不建议复制：

```text
.venv/
frontend/node_modules/
frontend/dist/
downloads/
config/
```

迁移后重新执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

cd frontend
npm.cmd install
```

需要根据新机器修改：

- Python 路径。
- Node.js 环境。
- ffmpeg 路径。
- Chrome / Edge 路径。

## 11. GitHub 发布记录

已发布：

```text
v1.0.0
```

发布内容：

- VideoDream 首个稳定版本。
- 多平台解析和下载。
- 抖音、YouTube、PornHub、B站专项适配。
- 本地保存和任务进度。
- 文档沉淀。

## 12. 后续优化建议

- MP4 Range 多线程下载，提高 signed MP4 下载速度。
- 把浏览器路径、ffmpeg 路径改为配置文件。
- 增加任务取消按钮。
- 增加下载历史列表。
- 增加更细粒度的任务日志。
- 增加 Playwright 浏览器自动探测。
- 增加桌面端封装。
