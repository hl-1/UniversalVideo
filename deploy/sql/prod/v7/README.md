# V7：公共 Agent 运行时

| 项目 | 说明 |
| --- | --- |
| 来源 | `backend/migrations/sql/V7__agent_runtime.sql`；本目录副本用于发布核对，勿重复执行 |
| 新表 | agent_tasks、agent_actions、agent_model_calls、agent_checkpoints、agent_events |
| 关系 | 任务引用 users；动作/账本/checkpoint/事件引用任务；全部 RESTRICT，完整字段和索引见 SQL |
| 前置 | V6 与用户雪花 ID 离线迁移完成；停本项目应用并备份项目数据库/文件 |
| 发布 | 使用项目外部配置执行 `scripts/database.py migrate`，再 validate、check；本版本应用启用新引擎须继续迁移 V8 |
| 验证 | 核对 Flyway 成功、9 表（连同 V8）及外键；原端口 8001/5174 启动应用 |
| Redis | 无新增/修改 Key、类型、TTL、一致性规则 |

## 回退

先在任务中心停止新任务，核对已发出的模型用量，未知账本继续保留。设 `VIDEODREAM_AGENT_ENGINE=legacy`、`VIDEODREAM_AGENT_ENABLED=false`，关闭本项目 worker/浏览器后重启兼容 V8 的当前构建。V7/V8 数据和 Flyway 历史保留，已有任务不能中途切换引擎。

不提供日常 DROP 回滚。确需恢复旧结构，停本项目应用并恢复匹配的数据库（含 Flyway 历史）与文件备份，或发布经审核的补偿迁移；不删除历史、不清共享 Redis、不删卷。旧代码若只接受 V6，不能直接连接 V8。
