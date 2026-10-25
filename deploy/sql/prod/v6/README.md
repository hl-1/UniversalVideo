# 用户雪花主键 V6

应用必须停止写入。使用 `python scripts/database.py migrate`，随后运行 `validate` 与 `check`。Compose 使用现有 migrate 服务执行同一 Python 入口。不要将 `001_ddl_user_snowflake.sql` 与 Flyway 重复执行；它只包含结构，不能替代离线数据和文件阶段。

迁移入口在 V6 DDL 前保存包含 Flyway 历史的完整 MySQL 备份。请另存旧代码/镜像、环境配置、downloads 与 .run/browser-profiles；目录迁移只重命名并验证文件清单，不删除原文件内容。新旧 ID 对照保存在 user_id_migration_map，目录恢复清单保存在 .run/user-id-migrations。迁移已应用的 V1–V5 校验和保持不变。

V6 新增 user_id_generator_state 和 user_id_migration_map，删除 users.id 的 AUTO_INCREMENT，将 music_profile.id / music_theme.id 扩为 BIGINT，删除 8 个个人音乐表 user_id 的默认 1。完整表结构及全部数据契约见 spec/VideoDream-用户与管理员功能设计.md 第 18 节。

离线阶段为旧账号生成保留节点 1023 的雪花 ID，事务内更新所有 user_id、声明的用户外键、审计操作者、资料/主题主键、JSON 中的用户身份、私聊配对键和视图键。更新完成验证每个用户外键及旧 ID 残留，再重命名 downloads/users、downloads/_tmp、.run/browser-profiles 中对应的用户目录。目标冲突、目录变化或数据库异常立即停止；捕获到的异常会回滚数据库和已重命名目录，进程中断则按持久对照及清单恢复后重试。禁止在未完成时启动应用，schema gate 会拒绝缺少 users-snowflake-v1 标记的库。

用户 ID 由数据库时钟、41 位毫秒差（2020-01-01 UTC 起）、10 位节点、12 位序列组成。VIDEODREAM_USER_ID_WORKER 默认 0，可选 0–1022；同一数据库的同节点多进程共用事务行锁与持久序列，不依赖进程内存、Redis 或系统启动时间。时钟回退、单毫秒序列耗尽和时间范围越界时拒绝分配。新建账号必须显式写入 ID。

切换会清理本项目 session、user-sessions、social、video、download 的 Redis 命名空间，使旧登录、媒体和实时票据失效；用户重新登录。不会清空其他前缀，不修改登录限流、验证码 TTL。浏览器通过登录响应 legacy_id 为当前用户复制本地音乐索引和下载记录到新 ID，保留旧记录供回滚，文件读取权限仍由浏览器决定。

## 回滚

停服后优先恢复匹配的 V5 数据库完整备份（含 Flyway 历史），使用目录清单将每个 target 重命名回 source，再恢复 V5 应用和原配置。先验证 source 不存在、target 指纹匹配且路径仍在原用户目录内；不覆盖、合并或删除未知目录。已恢复的目录无需重复移动。

如切换后已有新业务数据，先导出并保留这些数据及文件，再选择保留 V6 结构的兼容应用或编写前向补偿迁移；不能将新雪花账号压回 INT/自增序列。禁止只删除 V6 表、修改历史校验和或自动 repair。恢复后重新登录并验证账号数量、角色、好友/聊天、音乐/文件归属和原文件读取。
