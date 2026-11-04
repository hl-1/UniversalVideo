# V8：知识库

| 项目 | 说明 |
| --- | --- |
| 来源 | `backend/migrations/sql/V8__knowledge_bases.sql`；本目录副本用于发布核对，勿重复执行 |
| 新表 | knowledge_bases、knowledge_members、knowledge_documents、knowledge_document_versions |
| 关系 | 归属/上传者引用 users；成员/文档引用知识库，版本引用文档；全部 RESTRICT，完整字段和索引见 SQL |
| 发布 | 应用停止并完成项目备份后，执行 migrate → validate → check；本地仅复用共享中间件 |
| 外部存储 | 项目 ES `videodream_rag_*`、MinIO `videodream` Bucket/受限账号；Embedding 维度必须与索引相同 |
| 验证 | 入库后才发布 active_version；提问返回引用；成员撤销后无法读取；8001/5174 启动 |
| Redis | 无新增/修改；问答不加缓存 |

## 回退

按 [V7 回退流程](../v7/README.md) 关闭新入口与专用 worker，切 legacy 并重启兼容构建。保留四张知识表、Agent 账本、ES 索引及 MinIO 原文，避免引用和费用证据丢失。

禁止只 DROP 表或删除 Flyway 成功历史。确需撤销结构，使用审核后的补偿迁移或恢复匹配备份；ES/MinIO 的生产数据清理另行核对归属与备份。本次新增表不改变既有音乐/视频表结构。
