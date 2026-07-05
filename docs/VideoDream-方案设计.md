# VideoDream 方案设计

## 1. 技术栈

- 后端：Python 3.12 + FastAPI + yt-dlp。
- 前端：Vue 3 + TypeScript + Vite。
- 下载辅助：ffmpeg，用于音视频合并。
- 存储：本地 `downloads/` 目录。
- 任务状态：内存字典 + 后台线程池。

## 2. 项目结构

```text
C:\softWare\project\vD
├── backend
│   ├── main.py
│   └── requirements.txt
├── frontend
│   ├── src
│   │   ├── App.vue
│   │   ├── main.ts
│   │   └── styles.css
│   ├── package.json
│   └── vite.config.ts
├── downloads
├── scripts
│   ├── start-backend.ps1
│   └── start-frontend.ps1
└── README.md
```

## 3. 后端接口

### `GET /api/health`

健康检查。返回：

```json
{ "status": "ok" }
```

### `POST /api/parse`

解析视频信息。

请求：

```json
{ "url": "https://example.com/video" }
```

返回：

```json
{
  "title": "sample",
  "thumbnail": null,
  "duration": 1,
  "uploader": null,
  "webpage_url": "http://127.0.0.1:8765/sample.mp4",
  "formats": [
    {
      "format_id": "mp4",
      "label": "320x180 · MP4",
      "ext": "mp4",
      "resolution": "320x180",
      "filesize": 12092
    }
  ]
}
```

### `POST /api/download`

创建下载任务。

请求：

```json
{ "url": "https://example.com/video", "format_id": "mp4" }
```

返回：

```json
{ "task_id": "1ae340ccb911" }
```

### `GET /api/tasks/{task_id}`

查询任务状态。状态值为 `pending`、`running`、`finished`、`failed`。

### `GET /api/files/{filename}`

下载已完成文件。文件名会用 `Path(filename).name` 做基础防穿越处理。

## 4. 数据流

1. 前端输入 URL，调用 `/api/parse`。
2. 后端用 yt-dlp `extract_info(download=False)` 获取元信息。
3. 前端展示解析结果和格式选项。
4. 用户点击下载，前端调用 `/api/download`。
5. 后端创建任务，提交到 `ThreadPoolExecutor`。
6. yt-dlp 下载过程中通过 `progress_hooks` 更新内存任务状态。
7. 前端每 1.2 秒轮询 `/api/tasks/{task_id}`。
8. 任务完成后展示 `/api/files/{filename}` 下载入口。

## 5. UI 设计

- 顶部轻导航：品牌、下载工作台、支持平台、授权提醒。
- 首屏左侧：产品定位、价值文案、使用场景胶囊标签。
- 首屏右侧：白色下载面板、胶囊输入框、蓝色主按钮。
- 解析成功后展示封面卡片、元信息、格式选择、下载按钮。
- 下载任务展示进度条、状态文案、完成下载按钮。
- 下方展示支持平台标签、核心优势卡片、授权提醒。

## 6. 运行方式

后端：

```powershell
cd C:\softWare\project\vD
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd C:\softWare\project\vD\frontend
npm.cmd run dev -- --host 127.0.0.1
```

访问：

```text
http://127.0.0.1:5174
```

## 7. 后续扩展建议

- 增加下载历史数据库，例如 SQLite。
- 增加自动清理策略，避免 `downloads/` 无限增长。
- 增加任务取消、重试和并发限制配置。
- 增加平台兼容性提示和常见失败原因解释。
- 增加生产部署模式，让 FastAPI 直接托管前端构建产物。

## 8. B站兼容修复记录

- 封面图不再让前端直连平台图片，而是返回 `/api/image-proxy?url=...`，由后端补充浏览器请求头和 `Referer` 后代理读取，解决 B站封面防盗链导致的图片不显示。
- 格式列表中如果某个视频流不包含音频，后端会把下载表达式改为 `视频格式+bestaudio/best`，并在文案中标注“含最佳音频”。
- 已用 `https://www.bilibili.com/video/BV1oxT46REE4/` 验证：封面代理返回 200，前端封面正常显示，下载后的 MP4 经 ffprobe 检查包含 `video,audio` 两条流。

## 9. PornHub 410 处理记录

- 后端会把 `https://www.pornhub.com/{viewkey}` 这类短路径规范成 `https://www.pornhub.com/view_video.php?viewkey={viewkey}`。
- 后端会为 PornHub 链接补充常规浏览器请求头、`Origin`、`Referer` 和英文 `Accept-Language`。
- 如果用户确认浏览器中可访问该视频，可提供本人账号导出的 `config/pornhub-cookies.txt` 后重试。
- 对于站点真实返回的 `410 Gone`，系统只展示明确错误原因，不做登录、年龄验证、地区限制绕过或其他规避访问限制的行为。

## 10. 保存位置交互

- 后端仍需先把 yt-dlp 下载产物写入 `downloads/` 作为临时文件。
- 前端点击“选择位置并下载”时优先调用浏览器 `showSaveFilePicker`，让用户选择最终文件位置。
- 下载任务完成后，前端读取 `/api/files/{filename}` 的 Blob 并写入用户选择的文件。
- 如果当前浏览器不支持 `showSaveFilePicker`，则降级为普通浏览器下载，并保留“另存为”按钮。

## 11. 抖音专用解析模块

- 抖音链接不再只依赖 yt-dlp，后端会先走专用公开视频解析：
  1. 从 `modal_id`、`/video/{id}`、`item_ids`、`aweme_id` 等位置提取视频 ID。
  2. 对短链或分享链接先做 302 跳转解析。
  3. 优先请求 `https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={id}`。
  4. 如果公共 API 无数据，则尝试读取分享页里的 `window._ROUTER_DATA`。
  5. 从 `video.play_addr.url_list` 里选择播放地址，必要时把 `playwm` 替换为 `play`。
- 如果专用解析失败，系统会自动回退 yt-dlp。
- 后端已显式配置 Node.js 作为 yt-dlp 的 `js_runtimes`，让 yt-dlp 有机会执行站点所需的 JS 签名逻辑。
- 当前验证链接 `https://www.douyin.com/jingxuan?modal_id=7652684072266845450` 的公共 API 返回 `status_code=11110`、`status_msg=encrypt_data_miss`，yt-dlp 回退仍返回 `Fresh cookies`。这说明旧公共 API 方案对该链接已经不稳定，后续若要继续强化，需要实现或引入可信的 `a_bogus` 签名模块。

## 12. YouTube 兼容策略

- YouTube 链接继续走 yt-dlp。
- 后端已配置 `player_client=["web", "android"]` 和 Node.js runtime，提高公开视频免登录解析成功率。
- 当前环境验证 `https://www.youtube.com/watch?v=kcmAnZNXjsg` 时，YouTube 返回 `LOGIN_REQUIRED / Sign in to confirm you’re not a bot`。这是平台风控结果，不是前端流程错误。
- 系统不会在用户无感知时自动读取浏览器 cookies；如用户确认拥有访问和保存权利，可开启“本机授权模式”，后端会调用 yt-dlp 的 `cookiesfrombrowser` 从用户本机指定浏览器读取登录态并用于当前解析/下载任务。
- 如果 Chrome 正在运行，Windows 上可能出现 `Could not copy Chrome cookie database`，可关闭 Chrome 后重试，或选择 Edge/Firefox。
- 手动导入 Netscape cookies 仍保留为兜底方案。

## 13. 本机授权模式

- 前端下载面板新增“本机授权模式”开关，默认关闭。
- 用户开启后可选择 Chrome、Edge、Firefox、Brave、Vivaldi。
- `/api/parse` 和 `/api/download` 会收到：

```json
{
  "use_browser_cookies": true,
  "browser": "chrome"
}
```

- 后端把该选择转换为 yt-dlp Python API 参数：

```python
options["cookiesfrombrowser"] = ("chrome",)
```

- 开启本机授权模式时，不再优先使用 `config/*-cookies.txt` 文件，避免手动 Cookie 与浏览器 Cookie 混用。
- 该模式只适合本地桌面应用/本机服务，不适合部署成给多人访问的公共网站。

## 14. 任意网页视频嗅探

- 对于 yt-dlp 不直接支持的普通网页，后端增加轻量嗅探兜底：
  1. 如果 URL 本身是 `.mp4/.webm/.mov/.m3u8`，直接作为媒体资源返回。
  2. 如果是普通 HTML 页面，抓取页面源码。
  3. 从 `<video src>`、`<source src>` 和页面文本中识别 `.mp4/.webm/.mov/.m3u8` 资源。
  4. 解析结果仍返回统一 `ParseResponse`，格式 ID 使用 `sniff:{url}`。
- 下载策略：
  - `.mp4/.webm/.mov`：后端用 `requests` 流式下载。
  - `.m3u8`：后端用 ffmpeg 下载并合并成 MP4。
- 已验证：
  - 普通网页 `<video src="https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4">` 可解析和下载。
  - 直接传 `sniff:{media_url}` 可完成流式下载并生成本地 MP4。

## 15. 浏览器网络嗅探

- 对于抖音这类运行时动态加载视频地址的页面，后端新增 Playwright 浏览器嗅探链路：
  1. 启动本机 Chrome/Edge。
  2. 打开规范化后的页面地址。
  3. 监听页面 request/response。
  4. 筛选 `douyinvod.com`、`mime_type=video_`、`/aweme/v1/play/`、`.mp4/.m3u8` 等视频资源。
  5. 返回 `sniff:{url}` 格式项给前端。
  6. 下载时走现有嗅探下载分支。
- 当前抖音解析顺序：
  1. 浏览器网络嗅探。
  2. 抖音专用公开 API。
  3. yt-dlp。
  4. 静态 HTML 嗅探。
- 已验证链接：`https://www.douyin.com/jingxuan?modal_id=7652684072266845450`
  - 解析返回 `douyinvod.com` 视频资源。
  - 下载任务完成，生成 `911a4fc8d7bf.sniffed-edeeaac0.mp4`。
  - 优化后首次解析约 7 秒；同一链接命中 10 分钟内存缓存后约 0.01 秒。
