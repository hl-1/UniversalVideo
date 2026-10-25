# 社交 V5 部署与回滚

V5 新增 27 张 social_* 表，完整结构、索引、关系及 Redis 规则见 `spec/VideoDream-社交板块功能设计.md`。不改已有个人音乐表，不搬移私人文件。

## Flyway（优先）

1. 停止应用写入，备份 MySQL（含 flyway_schema_history）、downloads/_social、环境配置及旧镜像。
2. 已接管的 V4 库运行 `python scripts/database.py migrate`，再运行 `validate` 和 `check`；空库直接 migrate；未接管旧库先按 V1 文档 preflight/baseline，不忽略差异。
3. 确认 history 的 V5 success、27 张 social_* 表、关键索引/外键以及原数据量；启动匹配 V5 应用。
4. 按固定服务端口验证匿名拒绝、两用户好友/消息/分享、房间邀请、合唱同意与规则复用。开启 LiveKit 前按部署文档配置证书、网络、签名回调。

## 手工执行

`001_ddl_social.sql` 与 Flyway V5 内容相同，仅供受控手工部署。MySQL DDL 隐式提交，失败时先检查已创建结构再恢复备份，不宣称整个脚本自动事务回滚。不得在 Flyway 已执行 V5 后重复执行，也不得对不一致的库强行登记历史。

## 回滚

优先关闭社交功能，保留表和媒体，使用匹配 schema 的兼容应用或新的前向补偿迁移。旧 V4 应用存在版本门禁，不能直接连接 V5 库。

`999_fallback_social.sql` 是 001 的离线结构撤销参考：先拆循环外键，再逆依赖删表。它会删除社交业务数据，执行前必须停服并导出需保留的消息/作品/规则/媒体。它不修改 Flyway 历史，不可用于线上日常降级。需要完整恢复时使用包含历史表的匹配备份；禁止仅删表、删除 V5 history 或自动 repair。恢复后确认 Flyway validate/check 和原数据一致，再启动对应镜像。
