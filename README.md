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
- 已下载视频可框选最多 8 个固定水印区域，后台去水印后预览并另存为，原文件保留。

## 音乐与音轨下载

下载工作台在输入框上方提供「视频下载」和「音乐下载」。音乐模式默认选择视频音轨，可粘贴抖音完整分享文字或视频链接，解析后导出 M4A、MP3，并试听、另存为。抖音 DASH 音视频分离时优先下载独立音频流；AAC 导出 M4A 时保留原始编码。视频包含的歌曲片段、人声和音效会全部保留，不会自动补全成原曲。

音乐来源可切换 QQ 音乐、网易云音乐、酷狗音乐或全部平台，支持按歌名/歌手搜索与单曲链接解析。原平台无法提供完整音频时，三个平台都会尝试公开第三方解析服务，并在下载结果中显示服务来源。第三方服务可能失效，支持某个平台不代表该平台全部会员或付费歌曲都可下载。

搜索结果中，平台名称左侧提供播放/暂停按钮，可直接在线播放当前歌曲，不需要先下载。播放器支持拖动进度和调节音量；切换歌曲停止上一首，切换到视频页暂停播放。音源通过后端流式转发，支持 Range 请求，播放链接有效期为 30 分钟。若音源仅提供试听，播放器会明确标记试听片段；音源失效或不支持的格式会显示播放错误并允许重新播放。

音频导出会校验真实音频轨、文件完整性和源时长，拒绝把试听片段当作完整歌曲。当前已实测抖音约 64.735 秒音轨的 M4A/MP3 导出、QQ《晴天》、网易云《Hello》和酷狗《晴天》完整文件，包括平台标记为会员或付费的样例。公开解析服务不能保证覆盖各平台的所有歌曲，因此仍不能承诺任意会员或付费歌曲都可免费下载。

完成 `frontend` 构建后，后端也会提供前端页面，直接访问后端地址即可。开发模式可通过 `VIDEODREAM_API_TARGET` 指定 Vite 的后端地址，默认仍为 `http://127.0.0.1:8001`。Windows 下需要浏览器解析时，后端应以单进程方式启动；uvicorn `--reload` 可能造成 Playwright 子进程不受支持。

## 视频去水印

进入「视频去水印」，选择项目 `downloads/` 中的原视频，在画面上拖动框选水印，也可以添加区域后输入精确像素坐标。选择「AI 修复」或「快速填补」，点击「预览当前帧」检查效果，满意后再「处理整段视频」。完成后可预览结果并另存为 MP4，处理过程中可以取消。

- AI 修复使用本地 LaMa 模型，根据周围画面生成选区内容，只把修复结果合成到选区内。视频不上传到外部服务。安装 CUDA 依赖时优先使用 NVIDIA 显卡；其他 Windows 环境尝试 DirectML，无法使用时回退 CPU。逐帧处理可能出现细节失真或帧间闪烁，不能保证还原被遮挡的真实内容。
- 快速填补使用 ffmpeg `delogo`，根据选区边缘像素插值，适合固定位置、简单背景的水印，复杂背景可能产生块状模糊。
- 建议框选贴近水印，留少量余量。请从原视频重新处理，已经模糊的输出不能可靠恢复原始细节。已处理文件可通过「使用原视频」切回对应来源。
- 移动水印暂不支持自动跟踪；多个固定位置可以分别框选。视频四周至少保留 1 像素。

安装本地 AI 修复依赖和约 198 MiB 的模型：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-inpainting.txt
.\.venv\Scripts\python.exe scripts\setup-watermark-model.py --mirror
```

不需要镜像时省略 `--mirror`。脚本使用固定模型版本并校验 SHA-256，权重保存在本地忽略目录 `.run/models/`。模型来源：[Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX)，原始模型：[advimman/lama](https://github.com/advimman/lama)。首次预览需要加载模型；预览完成后显示按本机推理速度估算的整段处理时间，不含首次模型加载。处理时显示实际使用的 CPU/GPU、已处理帧数、耗时和预计剩余时间。多显卡机器可通过 `VIDEODREAM_DML_DEVICE_ID` 指定 DirectML 设备编号，默认 `0`。`VIDEODREAM_INPAINTING_DEVICE` 支持 `auto`（默认）、`cuda`、`dml`、`cpu`；显卡初始化或推理失败时自动回退 CPU。CPU 修复可能需要较长时间，AI 任务最长运行 12 小时。

NVIDIA 显卡可以安装 CUDA 版本（需要支持 CUDA 12 的驱动；以下命令需在后端停止后执行）：

```powershell
.\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime onnxruntime-directml onnxruntime-gpu
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-inpainting-cuda.txt
$env:VIDEODREAM_INPAINTING_DEVICE = "auto"
```

CUDA 与 DirectML 使用不同的 ONNX Runtime 安装包，同一个虚拟环境只保留一种。CUDA 依赖会安装本地运行库，不需要单独安装完整的 CUDA Toolkit。

运行时需要 `C:\softWare\environment\ffmpeg` 中的 `ffmpeg.exe` 和 `ffprobe.exe`，以及 `delogo`、`libx264` 支持。输出保留音频，视频重新编码为 H.264 MP4，生成独立文件。

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

## 首次安装

安装后端依赖：

```powershell
cd C:\softWare\project\UniversalVideo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

安装前端依赖：

```powershell
cd C:\softWare\project\UniversalVideo\frontend
npm ci
```

首次使用 AI 总结前，创建本地配置并填写 OpenAI 兼容接口信息：

```powershell
cd C:\softWare\project\UniversalVideo
Copy-Item config\ai.example.json config\ai.json
notepad config\ai.json
```

## 启动后端

打开一个 PowerShell 7 终端并保持窗口运行：

```powershell
cd C:\softWare\project\UniversalVideo
pwsh -File scripts\start-backend.ps1
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/health
```

接口文档：`http://127.0.0.1:8001/docs`

## 启动前端

再打开一个 PowerShell 7 终端并保持窗口运行：

```powershell
cd C:\softWare\project\UniversalVideo\frontend
npm run dev
```

访问：

```text
http://127.0.0.1:5174
```

前端开发服务器会把 `/api` 请求代理到 `http://127.0.0.1:8001`，因此手动启动后端时必须使用 `8001` 端口。

后端和前端都是前台进程。关闭对应 PowerShell 窗口会停止服务；需要停止时，在对应终端按 `Ctrl+C`。

本地固定使用前端 `5174`、后端 `8001`。更新后端代码后重启 `8001` 上的项目进程，使新增接口生效；不要另开端口连接另一份后端。前端开发模式在原地址自动更新，后端单独提供页面时需重新构建前端后访问 `http://127.0.0.1:8001/`。

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
