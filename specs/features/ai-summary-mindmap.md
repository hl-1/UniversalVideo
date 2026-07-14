# AI 总结思维导图规格

## 目标

基于已完成的视频总结字幕生成结构化思维导图。前端使用经典曲线脑图画布，支持节点折叠、缩放、平移、居中和时间戳跳转，不实现问答、转写、历史或向量检索。

## 数据契约

- `MindMapNode` 包含 `id`、`title`、`summary`、`timestamp`、`segment_ids`、`children`。
- `MindMapResult` 包含 `title`、`nodes`、`generated_at`。
- 节点最大深度为 4，每个节点最多 10 个子节点。
- 标题最长 80 字，说明最长 300 字。
- 模型只返回标题、说明、字幕索引和子节点；后端生成稳定 ID，并从有效字幕引用回算时间戳。
- 删除空标题、无有效字幕引用和完全无效的节点。清洗后无节点时任务失败。

## 接口

- `POST /api/summaries/{task_id}/mind-map` 创建任务；查询参数 `regenerate=true` 可强制重生成。
- `GET /api/summaries/{task_id}/mind-map` 查询任务。
- 状态为 `pending`、`running`、`finished`、`failed`。
- 总结不存在、未完成或失败时拒绝创建。
- 同一总结默认复用现有 pending、running 或 finished 任务。

## AI 与安全

- 复用 `config/ai.json` 的 OpenAI 兼容配置。
- 使用 JSON 输出并通过 Pydantic 与后端清洗器双重校验。
- Prompt 要求只能依据字幕，不得补充字幕中不存在的事实。
- 不重新提取字幕，不使用弹幕，不返回 API Key。

## 前端

- 总结结果提供“总结 / 字幕 / 思维导图”标签页。
- 使用 Mind Elixir 只读模式呈现单侧经典脑图：中心主题、彩色曲线分支、文字贴线、子分支继承一级分支颜色。
- 初始视图将中心主题靠左放置，右侧展开各级分支。
- 支持节点展开/收起、画布缩放、拖拽平移和一键居中；画布底部提供水平滑动条浏览右侧分支。
- 不允许用户编辑、拖动节点或删除节点。
- 节点显示标题、说明和时间戳；当前平台存在在线预览时可点击时间戳跳转播放器，否则禁用时间入口。
- API 数据必须经过 runtime filter，再转换为 Mind Elixir 数据；所有进入自定义节点 HTML 的文本必须转义。
- 本阶段不实现 PNG/SVG/PDF 导出。

## 测试

- 后端覆盖合法树、非法 JSON、空结果、深度和子节点裁剪、非法字幕引用、时间戳回算、任务复用与强制重生成。
- 前端覆盖 runtime filter、递归深度、Mind Elixir adapter、HTML 转义、失败重试和时间戳跳转 helper。
