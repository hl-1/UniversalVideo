# 视频总结字幕获取规格

## 目标

视频总结只使用平台提供的结构化字幕轨，不使用弹幕、语音转写或画面 OCR。

## 获取顺序

1. B 站链接先调用 `x/web-interface/view` 获取分 P 的 `cid`。
2. 调用 `x/player/v2` 获取创作者上传或平台生成的字幕列表。
3. 按现有语言优先级选择字幕，下载 B 站 JSON 字幕并映射为带时间戳文本。
4. 平台接口无字幕或请求失败时，使用 yt-dlp 提取内嵌字幕或自动字幕。
5. 两种方式均无字幕时明确返回失败，不把 `danmaku` 当作字幕。

## 兼容与错误

- 保持现有 `SubtitleSegment`、总结任务和前端响应契约不变。
- 支持 URL 的 `p` 参数，选择对应分 P 的 `cid`。
- B 站 API 网络错误、非零业务码和异常响应均回退 yt-dlp。
- 匿名接口返回 `need_login_subtitle` 时，按 Firefox、Chrome、Edge 顺序尝试读取本机浏览器登录态；只使用 `.bilibili.com` Cookie，且必须存在 `SESSDATA`，Cookie 不写盘、不记录。
- 字幕地址以 `//` 开头时规范化为 `https://`。
- `ai-*` 语言代码按对应基础语言参与优先级排序；`type=1`、`ai-*` 或非零 `ai_type` 的字幕统一标记为自动字幕。
- 两种方式均未命中时返回“平台字幕 API 和 yt-dlp 均未找到可用字幕”。

## 测试

- 覆盖 B 站 API 字幕解析、API 优先短路、yt-dlp 兜底、弹幕过滤和双路未命中错误。
- 原有 B 站 JSON、YouTube `json3`、VTT/SRT 和抖音页面文案路径不得回归。
