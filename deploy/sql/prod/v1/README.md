# v1 部署 SQL 与对应回滚

正向执行顺序：001 → 002 → 003；回滚顺序：997 → 998 → 999，只撤销已执行脚本。

- 001_ddl_schema.sql ↔ 999_fallback_drop_all.sql：完整初始建表及对应反向删表。
- 002_dml_seed_data.sql ↔ 998_fallback_seed_data.sql：基础账号/资料种子数据及对应撤销。
- 003_dml_seed_settings.sql ↔ 997_fallback_seed_settings.sql：默认系统配置及对应撤销。

Flyway V1 已包含三份正向 SQL 的效果，不能重复执行。手工部署后先 preflight，再 baseline V1。回滚脚本不在 Flyway 扫描目录中；已被 Flyway 管理的库使用补偿版本或恢复匹配备份，不能只修改结构而保留原成功历史。

数据回滚仅撤销仍为初始化值的记录，保留已设置的密码、已修改资料和配置。发生过业务写入后需要完整恢复时，使用操作前备份。999 不修改 Flyway 历史；执行前确认目标库、停服并备份，不用于后续版本的普通回滚。

完整契约与操作见项目 spec/Flyway数据库版本管理设计.md。
