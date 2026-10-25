# 工程结构

本项目采用模块化单体：一个 FastAPI 后端、一个 Vue 前端。业务代码按功能归类，公共基础能力由业务模块共同使用。

```text
backend/
  main.py                  Uvicorn 入口，仅导出 app
  application.py           应用组装、路由、中间件、启动和关闭
  core/                    项目路径和环境配置
  infrastructure/          MySQL、网络、浏览器、音频、语音与 AI 适配
  migrations/              Flyway 操作、结构核对、只读版本检查
    sql/                   Flyway 版本迁移 SQL
  modules/
    accounts/              身份、会话和权限
    admin/                 管理后台
    workspace/             用户任务与文件
    video/                 解析、下载、预览、字幕、总结、导图、历史
    music/                 音源、音乐库、导入、歌词与场景歌单 Agent
    watermark/             去水印与本地修复模型
  skills/                  Agent 使用的版本化资源
  tests/                   accounts、video、music 等业务测试
frontend/
  src/
    app/                   入口、认证根组件与应用布局
    features/              accounts、admin、workspace、video、music、watermark
    shared/                公共 API 请求、工具和基础样式
  tests/                   Node 测试
spec/                      当前功能规格及工程调整方案
docs/                      架构、开发指南与 archive 历史资料
scripts/                   初始化、启动、验证和运维工具
deploy/sql/prod/v1/        部署 SQL 和逐脚本回滚 SQL
config/                    示例配置及兼容的本地配置
downloads/                 用户媒体及导出文件
.run/                      本地运行资源、缓存、模型、备份及诊断输出
```

## 后端职责

- `main.py` 是稳定启动入口，运行命令仍为 `uvicorn backend.main:app`。
- `application.py` 组装路由、认证、静态文件和生命周期；业务模块不导入应用入口。
- `core/paths.py` 以自身位置定位项目根目录，避免移动代码后误写到模块目录。
- `core/configuration.py` 读取 `.env` 和 `.env.universal-video.local`。进程环境变量保持原优先级。
- `infrastructure/database.py` 管理 MySQL 配置、连接和事务。音乐库不再拥有公共连接的实现。
- `migrations.initialize_schema()` 只查询 `flyway_schema_history`，不再建表；lifespan 在启动任务前检查结构版本。`migrations/runner.py` 提供显式迁移、结构核对和存量库登记。
- `backend/migrations/sql/` 是版本 SQL 来源；`deploy/sql/prod/v1/` 是对应拆分部署 SQL 和逐脚本回滚 SQL。两者通过集成测试核对结构，回滚目录不在 Flyway 扫描范围内。
- `modules/video/` 内，`api.py` 和 `files.py` 处理 HTTP；`parsing.py` 编排平台解析；`browser.py`、`douyin.py`、`sniffing.py` 等实现解析细节；`subtitles.py`、`summary.py` 负责字幕和 AI 生成；`tasks.py`、`downloads.py` 负责任务状态和下载。
- `modules/music/agent/` 保留原有 Agent 分层；`backend/skills/` 是应用资源，不能当作本地临时文件删除。
- `modules/music/discovery/` 中，`feeds` 只负责候选过滤与每批六首的算法换歌，`pool` 维护候选及补充准入，`usage` 汇总每日发现预算；`profile_store` 冻结证据、校验提案和发布版本，`profile_agent` 只读取冻结证据，`profile_runtime` 扫描 MySQL 队列、续租并恢复任务，同时循环检查活跃用户的低候选池，补充间隔结束后自动续补。画像与候选增强各自记账，共享原 Agent 模型适配及全局并发。

业务模块内部可调用同一业务的服务和数据访问函数。跨业务协作通过明确导入的服务函数完成；新增公共能力放入 `core/` 或 `infrastructure/`，避免加入 `main.py`。现有模块中的 SQL 与服务代码可在后续功能调整时继续细分，无需预先创建空层级。

## 前端职责

- `app/App.vue` 只负责音乐、视频工具、管理页面的切换和全局播放器。
- `features/video/VideoWorkspace.vue` 负责工具区视图；`useVideoWorkspace.ts` 编排历史、解析和预览；`useVideoSummary.ts`、`useVideoDownload.ts`、`useBrowserSession.ts` 分别管理任务和会话。
- 工作区继续使用 `v-show` 保持挂载，切换页面保留播放器和视频状态。
- `features/music/` 按组件、播放器、外观设置归类；其他业务组件放在各自功能目录。
- 今日推荐首页与详情通过 `useDailyFeed` 读取/更换列表，不替换全局播放队列；`MyMusicProfile` 提供偏好控制和历史，`discoveryEvents` 记录有序播放会话与用户明确选择的场景。手机隐藏推荐封面与辅助说明，沿用主题变量。
- 播放校验到期只阻止过期候选进入新批次，已发布歌曲元数据继续展示，播放时重新获取音源。`identity` 规范化歌手署名顺序并结合完整歌名、时长匹配同一录音，对历史键建立读取时等价关系，使去重、屏蔽和近期排除保持一致。反馈和后台修复保留当前合规歌曲，只从候选池补缺口，提交新的完整列表以保留旧位置和事件引用；不足时后台自动补齐。`refill` 响应保留执行、额度、间隔与恢复时间供诊断，完整听歌列表不显示常驻运维提示。复用现有表与 Redis 契约，无结构迁移。
- 公共 `api()`、JSON 请求及错误解析位于 `shared/api/client.ts`；认证刷新及媒体 Service Worker 行为保持原有方式。
- 新增业务样式优先放在对应功能目录或组件的 `scoped` 样式中。基础样式位于 `shared/styles/base.css`；视频工作区样式随工作区组件维护。

## 文档与本地文件

`spec/` 是当前规格的统一入口，原 `specs/features/` 已迁入 `spec/features/`。`docs/archive/` 的首版需求、方案和总结仅保留为历史资料；当前运行方式以 [开发指南](development.md) 为准。

`.run/`、虚拟环境、下载文件和本机 IDE 配置属于本地工作状态。整理源码不搬移已有下载、浏览器配置、模型或备份；`downloads/.gitkeep` 继续保留。
