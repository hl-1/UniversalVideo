# 规格导航

## 当前功能规格

- [xianyue 安卓应用、后台下载与离线播放](features/android-app.md)
- [统一下载、本机保存与云端文件去重](features/unified-download-delivery.md)
- [移动端音乐中心布局与交互](features/mobile-music-experience.md)
- [歌曲列表共用筛选与时间排序](features/music-list-controls.md)
- [今日推荐候选池与用户音乐画像](features/daily-music-discovery.md)
- [视频模块持久化](VideoDream-视频模块持久化功能设计.md)
- [用户与管理员](VideoDream-用户与管理员功能设计.md)
- [场景歌单 Agent](VideoDream-场景歌单Agent功能设计.md)
- [AI 视频总结](features/ai-video-summary.md)
- [思维导图](features/ai-summary-mindmap.md)
- [平台字幕](features/video-summary-platform-subtitles.md)
- [B站公开 API 解析与下载](features/bilibili-public-parsing.md)
- [国内平台下载与速度优化](features/domestic-video-downloads.md)
- [YouTube / Pornhub 下载与统一超级管理员权限](features/overseas-video-downloads.md)
- [抖音公开解析](features/douyin-public-parsing.md)
- [通用视频在线预览](features/douyin-online-preview.md)
- [最近链接](features/recent-link-history.md)

## 规划与工程记录

- [Flyway 数据库版本管理](Flyway数据库版本管理设计.md)：SQL 目录、基线接管、部署和逐脚本回滚。

- [视频 AI 链路性能记录（2026-10-19）](performance/ai-video-generation-2026-10-19.md)：优化设计、各阶段优化前后实测、原始数据、数据契约及验证边界。
- [社交板块设计](VideoDream-社交板块功能设计.md)：规划，具体实现状态以正文为准。
- [工程结构整理](工程结构整理.md)：本次模块和目录调整范围、兼容与验收。

运行方式见 [开发指南](../docs/development.md)，代码职责见 [工程结构](../docs/architecture.md)。


### 今日推荐接入（2026-10-23）

今日推荐首次接入使用 Flyway V2，候选池与音乐画像已升级至 V4；实施与验证边界见 [今日推荐](features/daily-music-discovery.md)。

### 歌单链接导入（2026-10-23）

QQ 音乐、网易云音乐及酷狗公开歌单读取和批量匹配见 [歌单链接导入](features/music-playlist-import.md)。含链接支持边界、500 首范围、任务恢复、MySQL/Redis 明细及真实接口验证记录；无数据库结构迁移。

### 音乐搜索并发测试日志（2026-10-23）

[并发测试与 MySQL 日志](performance/music-search-benchmark-2026-10-23.md)：串行/4/10/20 首对比、网易云业务错误边界、V3 新增日志表及查询方式。正式搜索策略和页面计时仍待后续实施。

### 用户音乐画像 Agent 与候选池（2026-10-24）

[功能与实现规格](features/daily-music-discovery.md)：候选池算法换歌、每用户一条画像、证据校验、独立预算、持久化任务与版本控制，含 V4 完整 MySQL 结构、Redis 契约、迁移回滚及验收。已实现，画像 Agent 默认开启，模型调用以模拟测试验证。
