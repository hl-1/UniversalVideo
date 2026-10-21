# VideoDream 视频下载助手

当前版本：`v1.0.0`

下载及生成文件按账号保存于 `downloads/users/<用户ID>/`，包含 files、thumbnails、images、lyrics、summaries、batches、tmp；云端对应 `/opt/videodream/downloads/users/<用户ID>/`。文件仍保存在服务器，另存为手机/电脑后服务器原件保留。播放、图片与下载通过专用 HttpOnly 媒体 Cookie 鉴权，不依赖 Service Worker 就绪；业务 API 继续使用 Bearer。已有公共目录须停服备份并执行 `scripts/migrate-user-storage.py`，不能直接只更新镜像。详见[完整规格](spec/VideoDream-用户与管理员功能设计.md#11-云端媒体鉴权与播放修复2026-10-21)和[迁移部署说明](deploy/README.md#13-用户文件目录迁移与媒体鉴权更新)。

VideoDream 是一个本地运行的视频与音乐工作台，前端使用 Vue 3 + Vite，后端使用 FastAPI。它封装 `yt-dlp`、浏览器页面解析和任务下载能力，支持先解析视频信息，再选择格式和保存位置。

## 已支持的网站

### 抖音

- 解析浏览器按项目账号保存独立会话，配置目录为 `.run/browser-profiles/<账号ID>/`，无需使用个人 Chrome 配置。
- 遇到安全验证时，在下载工作台点击「打开验证窗口」，在本机桌面的浏览器中完成验证或登录，再点击「验证完成，重新解析」。图片、样式及验证码会正常加载。
- 关闭窗口保留会话，服务重启后可继续复用；清除会话会删除该账号的浏览器数据。验证窗口 15 分钟后自动关闭，普通解析窗口空闲 10 分钟后关闭。账号停用或删除时清除会话。
- 可见窗口需要后端运行在带桌面的本机环境；远程访问页面时，窗口会出现在后端所在电脑。

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
- 视频最近链接、解析历史、总结、字幕和思维导图按账号持久化；历史支持筛选、版本切换和删除，刷新后恢复导图视图与播放进度。
- MySQL 使用 5 张独立视频业务表保存历史、总结、字幕、导图版本及视图，启动时自动备份并迁移旧数据；Redis 保存最长 10 分钟的临时解析和预览地址。详细表结构和 key 见 [视频模块持久化功能设计](spec/VideoDream-视频模块持久化功能设计.md)。
- 下载结果会校验，避免图片、空响应或无效小文件被当作视频保存。
- 已下载视频可框选最多 8 个固定水印区域，后台去水印后预览并另存为，原文件保留。

## 音乐与音轨下载

### 音乐中心与个人音乐库

首页提供音乐中心：侧栏可进入喜欢、最近播放、本地和下载、试听列表、自建歌单及个人中心。头像和昵称可修改；歌单支持创建、编辑、删除、添加/移除歌曲和整单播放。搜索结果可在线听歌、收藏、加入歌单，保留单曲和批量下载。

侧栏「批量找歌」支持上传、拖入或粘贴歌单截图，以及多行文本名单。截图一次最多 8 张，单张不超过 10 MB、总计不超过 32 MB；长截图保留完整内容并分段识别。文本每行一首，可选择「歌名 - 歌手」或「歌手 - 歌名」顺序。一次最多 100 首，导入后可编辑、添加和删除歌曲，再逐首搜索 QQ、网易云和酷狗。匹配结果支持切换候选、试听、勾选和跳过已下载歌曲，确认后进入现有批量下载队列。截图识别与歌曲搜索可取消，任务和匹配结果按账号保存；截图仅临时用于识别，不保存到音乐封面目录。

AI 按输入类型选择模型：文字总结和思维导图使用 `LLM_*`，截图识别使用 `LLM_VISION_*`，无字幕视频转写使用 `LLM_ASR_*`；三组各自配置服务地址、模型和密钥。例如文字模型可使用 DeepSeek，截图识别和语音转写分别使用 SiliconFlow 的 `Qwen/Qwen3-VL-8B-Instruct`、`FunAudioLLM/SenseVoiceSmall`。截图和待转写音频会发送到对应的 AI 服务，并按服务方规则计费；未配置视觉模型时仅截图识别不可用，文本名单仍可导入和搜索。

取消任务会停止后续识别和搜索，并丢弃当前请求的结果；已发送至外部服务的请求可能仍会完成并产生费用。

全局底部播放器支持上一首/下一首、顺序/列表循环/单曲循环/随机播放、进度、音量和队列；切换页面保留播放，开始播放视频时暂停音乐。下载成功自动进入个人音乐库，优先播放本地音频。可导入 MP3、M4A、FLAC、WAV、OGG、AAC、OPUS 文件。展开歌曲可查看歌词、匹配歌词或导入 LRC，点击已同步的歌词可跳转进度。

歌词通过 LRCLIB 按歌曲及歌手匹配，缓存以本地音频文件名保存；也支持导入 LRC 或读取同名歌词文件。当前为逐行同步，尚未实现逐字高亮。缺少歌手信息或无法匹配时可手动导入歌词。

个人资料、收藏、播放历史、歌单及音频文件索引存储在 MySQL，并按账号隔离。音频保留在 `downloads/`，在线歌曲在播放时重新解析。登录后可使用个人中心和任务与文件页面；超级管理员可进入用户管理、业务数据、系统配置、操作日志和运行统计。

### 账号初始化

首次启动会备份音乐数据库及 `downloads/` 文件，补齐用户归属，将存量数据分配给 `admin` 超级管理员。备份及迁移记录保存在本地 `.run/library-backups/`。公开注册默认关闭，普通用户由管理员创建，或在管理员开启注册后自行注册。

在项目根目录运行本地初始化脚本，并按提示设置管理员密码：

密码长度为 8 到 128 个字符，不强制包含大小写字母、数字或特殊符号。输入时终端不显示字符；长度不合规或两次输入不一致时，脚本会提示重新输入。

```powershell
.\.venv\Scripts\python.exe scripts\initialize-accounts.py --username admin
```

已有环境使用 `.run/startup-venv` 时，替换为其 `Scripts/python.exe`；启动脚本会显示实际所选环境。密码不通过命令行参数传入，不提供默认密码。脚本还会在忽略的 `.env.universal-video.local` 中生成 JWT 密钥。Access Token 在浏览器内存保存 15 分钟；Refresh Token 使用 HttpOnly / SameSite Cookie 和 Redis 会话，最长 7 天，轮换不延长会话期限。修改或重置密码、停用账号会撤销全部会话。

开发访问地址固定为 `http://127.0.0.1:5174/`。生产环境配置 HTTPS、`VIDEODREAM_COOKIE_SECURE=true`，并将 `VIDEODREAM_ORIGINS` 设置为实际前端地址。媒体资源通过同源 Service Worker 添加认证头，文件、图片、歌词及 Range 请求均需认证；旧匿名地址不能直接访问。Redis 不可用时返回 503，不允许仅凭 JWT 访问。

MySQL、Redis、JWT 和 AI API 配置统一放在项目根目录 `.env.universal-video.local`（已忽略，不提交），参考 `.env.example`：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=videodream
MYSQL_USER=videodream_local
MYSQL_PASSWORD=replace-with-your-password
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_TTL=3600
REDIS_DATABASE=0
```

首次连接需要预先创建 MySQL 数据库，并授予用户该库的建表、修改表结构、查询和写入权限；后端自动创建表并执行归属迁移。配置优先级为进程环境变量、`.env.universal-video.local`、旧 `.env`；也支持旧的 `config/database.json` 作为 MySQL 备用。Redis 必须可用，负责会话、刷新摘要、用户会话索引及认证限流。

本机已有 MySQL 容器时，也可用 `scripts/setup-music-mysql.py --container <容器名>` 建立独立数据库和账号，凭证保存到本地忽略的 `config/database.json`；本地环境配置文件中存在 MySQL 配置时以环境配置文件为准。

下载工作台在输入框上方提供「视频下载」和「音乐下载」。音乐模式默认选择视频音轨，可粘贴抖音完整分享文字或视频链接，解析后导出 M4A、MP3，并试听、另存为。抖音 DASH 音视频分离时优先下载独立音频流；AAC 导出 M4A 时保留原始编码。视频包含的歌曲片段、人声和音效会全部保留，不会自动补全成原曲。

音乐来源可切换 QQ 音乐、网易云音乐、酷狗音乐或全部平台，支持按歌名/歌手搜索与单曲链接解析。原平台无法提供完整音频时，三个平台都会尝试公开第三方解析服务，并在下载结果中显示服务来源。第三方服务可能失效，支持某个平台不代表该平台全部会员或付费歌曲都可下载。

QQ 音乐和网易云音乐的现有第三方解析服务未取得有效音源时，会自动尝试 51ape（at38.cn）备用解析，支持在线播放、单曲和批量下载。正常访客 Cookie 由后端自动处理，无需手动配置；下载结果显示「51ape音乐 · at38.cn」。备用音源仍会校验音频完整性和时长，不保证每首歌曲都可用。

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

运行时需要 ffmpeg 和 ffprobe，以及 `delogo`、`libx264` 支持。程序自动识别 Windows 的 `.exe` 和 macOS/Linux 的可执行文件；优先从 PATH 查找，兼容 Windows 原目录 `C:\softWare\environment\ffmpeg` 和 macOS 的 Homebrew 目录。输出保留音频，视频重新编码为 H.264 MP4，生成独立文件。

## AI 视频总结配置

AI 总结使用 OpenAI 兼容接口。在项目根目录的统一配置文件 `.env.universal-video.local` 中填写：

```dotenv
# 文字总结与思维导图
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
LLM_API_KEY=replace-with-your-text-api-key

# 截图识别
LLM_VISION_BASE_URL=https://api.openai.com/v1
LLM_VISION_MODEL=
LLM_VISION_API_KEY=replace-with-your-vision-api-key

# 语音转写
LLM_ASR_BASE_URL=https://api.siliconflow.cn/v1
LLM_ASR_MODEL=
LLM_ASR_API_KEY=replace-with-your-speech-api-key
```

登录任务和本地脚本都优先使用进程中的同名环境变量，其次是 `.env.universal-video.local`、旧 `.env`；登录任务再使用后台保存配置和默认值。修改本地文件后重启后端，新任务使用新配置，已固定配置的在途任务继续使用原配置。三组地址和密钥应分别填写；一组的独立地址已填写但密钥缺失时，不借用文字服务密钥。仅旧配置完全没有独立连接字段时，兼容原有共享连接；后台下次保存会将旧连接分别固定，再修改文字组不会改变其他组。

后台分别显示三组地址、模型及脱敏密钥。有环境配置的组标注来源并禁用后台修改，避免保存后被环境值覆盖；如需使用后台管理，请移除该组环境配置及进程变量。环境密钥不会因保存其他后台设置而写入数据库。此文件使用 UTF-8 dotenv 格式，配置值不展开 `${...}`；普通地址、模型和密钥可不加引号，含空格或注释符号的值需保证解析后完整。

视频总结优先使用平台人工字幕或自动字幕。没有可用字幕时，自动下载音轨、校验完整性、按约 60 秒分段，通过 `/audio/transcriptions` 转写后生成总结。SiliconFlow 可填写 `LLM_ASR_MODEL=FunAudioLLM/SenseVoiceSmall`；其他服务需填写其支持的语音识别模型。仅支持 4 小时以内的非直播音轨。结果标记为「语音转写（分段时间戳）」，时间戳代表分段起点，章节定位为近似值。临时音频在成功、失败或取消后清理，服务重启可能中断正在处理的任务。

兼容原有配置：未登录脚本未提供对应环境连接时，还可复制 `config/ai.example.json` 为 `config/ai.json`，填写：

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "replace-with-your-api-key",
  "model": "gpt-4.1-mini"
}
```

B 站总结优先调用平台字幕 API，直接获取创作者上传或平台生成的字幕；平台接口没有字幕时，再使用 yt-dlp 提取内嵌字幕或自动字幕。弹幕不会作为字幕参与总结。

`.env.universal-video.local` 和 `config/ai.json` 已被 `.gitignore` 忽略，请不要提交真实密钥。

## 开发运行

环境初始化、配置、管理员设置、日常启动与验证见 [开发指南](docs/development.md)。在项目根目录用两个 PowerShell 7 终端分别启动：

```powershell
pwsh -File scripts/start-backend.ps1
pwsh -File scripts/start-frontend.ps1
```

首次安装先执行 `pwsh -File scripts/setup.ps1`。日常启动不重复安装依赖；后端打印实际使用的 Python 环境。访问固定地址 `http://127.0.0.1:5174/`，后端为 `8001`。端口已占用时检查现有进程，不自动换端口或启动第二份服务。

统一验证入口：

```powershell
pwsh -File scripts/test.ps1
```

## 项目结构与文档

```text
backend/core/            路径与配置
backend/infrastructure/  数据库、网络、浏览器、音频与 AI 适配
backend/modules/         按业务组织的后端代码
backend/migrations/      已有数据迁移的协调入口
backend/tests/           按业务归类的 Python 测试
frontend/src/app/        应用入口与布局
frontend/src/features/   按业务组织的组件与状态
frontend/src/shared/     公共请求、工具与样式
frontend/tests/          前端测试
spec/                    当前功能规格
docs/                    架构、开发指南和历史资料
scripts/                 初始化、启动与验证
```

详见 [工程结构](docs/architecture.md)、[功能规格](spec/README.md) 和 [文档导航](docs/README.md)。用户媒体位于 `downloads/`，运行资源和备份位于 `.run/`。

## 发布记录

详见 [CHANGELOG.md](CHANGELOG.md)。
