# VideoDream 视频下载助手

当前版本：`v1.0.0`

VideoDream 是一个轻量视频下载工作台：前端使用 Vue 3 + Vite，后端使用 FastAPI 封装 yt-dlp。首版支持 yt-dlp 可识别的公开视频链接，流程为解析信息、选择格式、创建下载任务、选择保存位置、保存本地文件。

## 运行环境

- Node.js 20+，用于前端开发服务和构建。
- Python 3.11+，用于 FastAPI 后端。
- ffmpeg，已安装到 `C:\softWare\environment\ffmpeg`，用于音视频合并。

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

PowerShell 如果拦截 `npm.ps1`，请使用 `npm.cmd`：

```powershell
cd C:\softWare\project\vD\frontend
npm.cmd install
npm.cmd run dev -- --host 127.0.0.1
```

访问：

```text
http://127.0.0.1:5174
```

## 文件保存

后端会先把 yt-dlp 产物临时保存到项目根目录的 `downloads/`。该目录已加入 `.gitignore`，不会作为业务数据提交。

前端点击“选择位置并下载”时，会优先调用浏览器的系统保存文件对话框，让你选择最终保存位置。部分内置浏览器不支持该能力时，会自动降级为普通浏览器下载。

## Cookies 文件

部分平台会要求登录态、年龄确认或反机器人校验。VideoDream 不会自动读取浏览器 cookies；如果你确认自己有权访问并下载，可以在前端错误提示出现后点击“填写 cookies 后重试”，粘贴 Netscape 格式 cookies 内容。

后端会把 cookies 保存到：

```text
C:\softWare\project\vD\config\bilibili-cookies.txt
C:\softWare\project\vD\config\youtube-cookies.txt
C:\softWare\project\vD\config\pornhub-cookies.txt
C:\softWare\project\vD\config\douyin-cookies.txt
```

放好后重启后端。

通过前端弹窗保存 cookies 后，一般不需要手动刷新页面，系统会自动重新解析当前链接。

## 本机授权模式

如果不想手动导出和粘贴 cookies，可以在下载面板开启“本机授权模式”，并选择 Chrome、Edge、Firefox、Brave 或 Vivaldi。

开启后，后端会调用 yt-dlp 的 `cookiesfrombrowser`，在你的电脑本机读取所选浏览器的已登录状态用于当前解析/下载任务。系统不会把 Cookie 展示在页面里，也不会写入 `config` 文件。

注意：

- 该模式只适合本地运行，不适合部署成公网多人下载服务。
- Chrome 正在运行时，Windows 上可能出现 `Could not copy Chrome cookie database`，可关闭 Chrome 后重试，或选择 Edge/Firefox。
- 浏览器里必须已经登录目标平台，否则仍可能出现登录/机器人校验提示。

## 常见平台问题

### 抖音公开解析

后端已为抖音增加专用公开视频解析模块：会先提取视频 ID，调用 `iesdouyin.com` 公开视频信息接口，并尝试从分享页数据中兜底提取播放地址；失败后再回退 yt-dlp。

如果某条抖音链接返回 `encrypt_data_miss` 或 `Fresh cookies are needed`，表示平台当前要求签名参数、登录态或触发风控。VideoDream 不会绕过这些限制，也不会自动读取你的浏览器 cookies。

### B站 412

如果 B站链接返回 `HTTP Error 412: Precondition Failed`，通常是平台接口校验请求头或登录态导致。后端已经默认补充浏览器 `User-Agent`、`Origin` 和 `Referer`。如果仍失败，请提供 `config/bilibili-cookies.txt`。

### YouTube bot/sign in

如果 YouTube 返回 `Sign in to confirm you’re not a bot`，需要你本人浏览器中的 YouTube cookies。请导出到：

```text
C:\softWare\project\vD\config\youtube-cookies.txt
```

然后重启后端再试。

### PornHub 410

如果 PornHub 链接返回 `HTTP Error 410: Gone`，通常表示页面已删除、下架、地区不可访问，或需要站点侧登录/年龄校验后才可访问。后端会自动规范短 viewkey 地址并补充常规浏览器请求头；如果浏览器中可正常打开，请提供 `config/pornhub-cookies.txt`。

## 使用边界

请仅下载你拥有权利、获得授权或平台允许保存的公开视频内容。VideoDream 不提供登录代办、年龄验证、会员内容下载或规避访问限制能力。
