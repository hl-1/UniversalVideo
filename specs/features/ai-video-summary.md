# AI 视频总结功能规格

## 目标

从公开视频的人工字幕、自动字幕或允许的页面文案中提取带时间戳文本，调用 OpenAI 兼容接口生成中文结构化摘要，并提供 Markdown 与 JSON 下载。

首版不执行音频语音识别、画面 OCR 或弹幕总结，不在没有可信文本来源时编造视频内容。

## 配置

运行时从 `config/ai.json` 读取：

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "replace-with-your-api-key",
  "model": "gpt-4.1-mini"
}
```

- `api_key` 和 `model` 必填，`base_url` 可选。
- DeepSeek 等兼容服务通过替换 `base_url`、`model` 和本地 `api_key` 接入，不改变接口契约。
- `api_key` 为空时允许读取 `OPENAI_API_KEY` 环境变量，但 `config/ai.json` 必须存在。
- 配置文件必须是合法 JSON。
- `config/ai.json` 被 Git 忽略，只提交 `config/ai.example.json`。

## 接口与任务状态

### 创建任务

`POST /api/summaries`

请求体只包含公开视频 `url`，响应返回 12 位任务 ID。

### 查询任务

`GET /api/summaries/{task_id}`

任务状态为 `pending`、`running`、`finished` 或 `failed`。服务重启后内存任务丢失，未知任务返回 404。

进度节点：

- 8%：解析字幕或页面文案。
- 42%：文本提取完成，调用 AI。
- 82%：保存总结文件。
- 100%：总结完成。

## 文本来源顺序

1. B 站优先使用平台字幕 API，详细规则见 `video-summary-platform-subtitles.md`。
2. 其他平台使用 yt-dlp 读取人工字幕和自动字幕。
3. 字幕按中文简体、中文、中文繁体、英文的顺序选择。
4. 字幕格式按 VTT、SRT、JSON3、JSON、SRV3、TTML/XML 顺序选择。
5. 抖音因 `fresh cookies` 无法获取字幕时，回退到解析结果中的标题、作者和时长，来源标记为“页面文案”。
6. 没有任何可用文本时任务失败，不调用 AI。

## 字幕解析

- 支持 VTT、SRT、YouTube JSON3 和 B 站 JSON 字幕。
- 清理 HTML 标签、重复空白和连续重复片段。
- 时间戳统一格式化为 `H:MM:SS` 或 `M:SS`。
- API 响应最多返回 500 个字幕片段。
- 提交给 AI 的文本最多 28000 个字符，超出后追加截断提示。

## AI 输出契约

使用 `chat.completions` OpenAI 兼容接口，温度为 `0.2`。摘要必须包含：

- 概览
- 关键要点
- 时间线章节
- 适合保存的笔记

模型不得补充字幕或页面文案中不存在的事实。AI 返回空内容时任务失败。

## 结果文件

- Markdown 文件包含 AI 摘要以及完整的“字幕/转写文本”章节。
- JSON 文件保存结构化 `SummaryResult`，不写入下载 URL 字段。
- 文件保存到 `downloads/_summaries`，名称由任务 ID 和安全化标题组成。
- 通过 `/api/summary-files/{filename}` 下载，只允许访问安全化后的文件名。

## 前端行为

- 解析成功后允许创建总结任务。
- 前端轮询任务状态并显示进度、错误、来源、语言和字幕数量。
- 完成后展示纯文本摘要和最多 80 条字幕，并提供 Markdown/JSON 下载入口。
- 新解析开始时停止旧总结轮询并清理旧结果。

## 测试要求

- 覆盖字幕来源优先级、VTT/SRT/JSON3/B站 JSON 解析和弹幕过滤。
- 覆盖抖音 `fresh cookies` 页面文案兜底。
- 覆盖配置缺失、非法 JSON、缺少 Key/模型和 AI 空响应。
- 覆盖任务成功、失败、结果文件生成和安全文件访问。
