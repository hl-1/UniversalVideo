# Flyway 数据库版本管理

日期：2026-10-23。适用后端 FastAPI + PyMySQL；迁移资源归入现有 Python 后端的 `backend/migrations/`，部署和回滚 SQL 归入 `deploy/`。

## 1. 目标与目录

表结构由 Flyway 在发布阶段管理。应用启动和首次业务连接只检查版本，不运行 DDL，不自动安装工具。固定后端 8001、前端 5174，迁移工具不监听端口。

```text
backend/migrations/
  __init__.py
  runner.py
  schema_v1.json
  sql/
    V1__baseline.sql
deploy/
  sql/prod/v1/
    001_ddl_schema.sql
    002_dml_seed_data.sql
    003_dml_seed_settings.sql
    997_fallback_seed_settings.sql
    998_fallback_seed_data.sql
    999_fallback_drop_all.sql
config/flyway.conf
scripts/database.py
scripts/setup-flyway.py
```

配置中的 Flyway 扫描位置仅为 `backend/migrations/sql/`。`schema_v1.json` 与 Python 迁移入口相邻，手工部署和回滚文件独立放在 `deploy/sql/prod/v1/`。

后续版本使用递增的 `V2__描述.sql` 等文件，相应发布 SQL 和每个 SQL 对应的回滚脚本放入 `deploy/sql/prod/v2/`。同时更新 `backend/migrations/__init__.py` 中的 `SCHEMA_VERSION`，使应用在迁移完成前拒绝启动。V1 及 `schema_v1.json` 保留为首次接管的契约，不随新版本改写。

`V1__baseline.sql` 包含 001、002、003 的结构及初始化数据。空库只执行 Flyway V1，不能再执行 001～003。手工部署执行 001→002→003 后，经 preflight 验证再 baseline 到 V1；后续由 Flyway 接管。版本脚本执行后不可编辑、重命名或删除，修复使用新版本。

`schema_v1.json` 是从 V1 结构导出的只读核对契约，记录字段、默认值、索引、外键、引擎和排序规则。它不执行 SQL。完整 SQL 来源见附录；集成测试分别执行 Flyway V1 和拆分 SQL 并核对同一契约，防止两套结构漂移。

## 2. 工具、配置和身份

固定 Flyway OSS 13.11.1，MySQL Connector/J 9.5.0。安装脚本将 CLI 和驱动保存到忽略目录 `.run/tools/`，下载均校验固定 SHA-256，CLI 需要 Java 21+。Python 服务运行时无需 Java。

配置优先级沿用 `database_config()`：`config/database.json` 备用值、旧 `.env`、`.env.universal-video.local`、进程 MYSQL_*，最后是既有 VIDEODREAM_DB_* 覆盖。`--config <文件>` 替换本地 dotenv 来源，进程环境仍优先。

可选 VIDEODREAM_MIGRATION_USER / VIDEODREAM_MIGRATION_PASSWORD 为迁移账号。迁移账号仅授权目标业务库所需 CREATE、ALTER、INDEX、REFERENCES、DROP 和 SELECT/INSERT/UPDATE/DELETE 等权限；业务账号仅需 SELECT/INSERT/UPDATE/DELETE（包括查询 flyway_schema_history）。不授予应用全实例或用户管理权限。数据库、账号及授权由管理员独立准备。

连接信息经子进程环境传给 Flyway，不写进 SQL、不放进命令行、不打印密码。Flyway 禁用 clean、自动 baseline、乱序执行及 SQL 占位符替换，并启用版本文件命名与校验和校验。

## 3. 操作流程

安装与空库初始化（数据库和账号已由管理员创建）：

```bash
.venv/bin/python scripts/setup-flyway.py
.venv/bin/python scripts/database.py migrate
.venv/bin/python scripts/initialize-accounts.py --username admin
.venv/bin/python scripts/database.py check
```

Windows 使用 `.venv\Scripts\python.exe`。管理员密码交互设置；V1 只存 `!uninitialized` 占位值，不能用于登录。默认系统配置包含空 API Key，已有配置不会被 baseline 覆盖。

存量库：等待任务结束并停服，先按现有制度备份数据库、downloads、.run 和环境配置，再执行：

```bash
.venv/bin/python scripts/database.py preflight
.venv/bin/python scripts/database.py baseline
.venv/bin/python scripts/database.py migrate
.venv/bin/python scripts/database.py check
```

preflight 只读比较 23 张表的字段类型、默认值、可空性、附加属性、索引定义、外键及引擎，拒绝缺表、缺字段、缺索引和未知表。索引比较定义，不要求 MySQL 自动命名的外键索引名称完全一致。兼容当前本地 utf8mb4_unicode_ci 和云端 utf8mb4_0900_ai_ci，并检查关联字段排序规则相同，不进行排序规则转换。

baseline 仅允许不存在 Flyway 历史表且完整通过 V1 核对的库；持有按数据库区分的运维锁，再次检查结构，先创建数据库一致性快照 SQL 备份，成功后才登记 V1。备份在 `.run/database-backups/<时间-随机值>/database.sql`，文件权限 0600；已有用户、密码、配置、媒体索引和业务记录不重写。应用须保持停止，避免文件与数据库在备份期间发生不匹配。

baseline 不等于结构或业务数据迁移。结构缺失时，使用接入前的兼容版本先完成旧结构升级。`database.py legacy-data` 只适用于结构已完整的旧数据：停服后显式运行，先备份，再调用原有账号归属、视频 JSON 转换和音乐文件分类逻辑；这些函数不再包含 DDL。用户目录搬迁继续使用 migrate-user-storage.py 的清单与备份流程。全新数据库无需执行 legacy-data。

应用 lifespan 在启动后台任务前检查所有历史记录成功且当前版本与 `SCHEMA_VERSION` 一致（目前 V4）；缺少历史表、版本不符或存在失败记录时启动失败。首次业务连接也进行只读检查，缓存按连接目标隔离。`/api/health` 保留现有存活语义。

## 4. Docker 发布

迁移镜像基于当前 app 镜像单独加入 Java 和 Flyway，app 镜像本身不加入 JVM。migrate 服务仅加入数据库网络，共享既有环境配置、downloads、.run 挂载，使用 migration profile，不常驻、不发布端口。

```bash
docker compose -f deploy/compose.yml build app
docker compose -f deploy/compose.yml build migrate
# 等待任务结束，停止应用并按部署文档备份。
docker compose -f deploy/compose.yml stop app
# 首次接管已有库执行以下两行；空库省略。
docker compose -f deploy/compose.yml run --rm --no-deps migrate preflight
docker compose -f deploy/compose.yml run --rm --no-deps migrate baseline
# 每次发布执行；非零退出立即停止，不能继续启动。
docker compose -f deploy/compose.yml run --rm --no-deps migrate migrate
docker compose -f deploy/compose.yml run --rm --no-deps migrate validate
docker compose -f deploy/compose.yml run --rm --no-deps app python scripts/database.py check
docker compose -f deploy/compose.yml up -d --no-build --force-recreate app
```

首次空库完成迁移后，启动前通过 app 容器交互执行 initialize-accounts.py 设置管理员密码。启用可选去水印构建时保留现有 compose.inpainting.yml 覆盖参数。

## 5. 每个 SQL 的对应回滚

| 正向 SQL | 对应回滚 SQL | 范围 |
| --- | --- | --- |
| 001_ddl_schema.sql | 999_fallback_drop_all.sql | 按依赖逆序删除 001 创建的 23 张表 |
| 002_dml_seed_data.sql | 998_fallback_seed_data.sql | 撤销仍保持初始状态的管理员、资料记录 |
| 003_dml_seed_settings.sql | 997_fallback_seed_settings.sql | 仅删除内容仍等于初始化快照的配置 |

完整回滚顺序为 997→998→999，且只回滚已执行的正向脚本。999 是对应初始建表脚本的反向 SQL；后续版本回滚只撤销各自变更，不能重复使用 V1 删表脚本。所有回滚文件均在 Flyway 扫描目录之外，不自动执行。

初始化后发生过用户活动时，数据回滚脚本保护已初始化密码、已修改的资料和配置，不保证恢复所有数据；需要原样恢复时使用操作前备份。UPDATE/DELETE 的未来迁移必须保存原值，不能靠猜测生成反向数据。

Flyway 已执行的版本采用新的补偿版本执行审核后的反向 SQL，保留历史；或者停服恢复匹配的数据库和媒体备份（包含 flyway_schema_history）。不能只跑反向 DDL 而留下原版本成功记录。MySQL 多条 DDL 不能统一事务回滚；repair 只处理迁移历史，不恢复结构和数据，不自动运行。旧 Agent 结构迁移/回滚脚本及业务模块中仅用于登记旧结构版本的方法已删除，避免保留第二套结构管理入口。管理员初始化脚本仅设置密码，不再提供历史迁移兼容参数。

本次重复入口清理不修改 MySQL 表、字段、索引、关系和业务数据，不删除 `account_migrations` 中已有记录或备份；无需新增迁移 SQL。Redis key、TTL 和读写规则不变，因为清理仅涉及已被 Flyway 替代的代码入口。

## 6. MySQL 数据契约

现有 23 张业务表保持完整字段、索引、默认值、外键及关系，见附录和 schema_v1.json。音乐曲目关联收藏、历史、歌单曲目、下载、别名、歌词、服务器音源；Agent 任务关联用户、自身父任务和已保存歌单，特征关联用户；视频引用继续由现有业务事务维护。account_migrations 保留业务迁移和备份路径，不替代 Flyway 历史。

唯一新增管理表为同一 videodream 数据库中的 flyway_schema_history，完整等价结构如下，实际由固定 Flyway 版本自动创建：

```sql
CREATE TABLE flyway_schema_history (
  installed_rank INT NOT NULL,
  version VARCHAR(50) NULL,
  description VARCHAR(200) NOT NULL,
  type VARCHAR(20) NOT NULL,
  script VARCHAR(1000) NOT NULL,
  checksum INT NULL,
  installed_by VARCHAR(100) NOT NULL,
  installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  execution_time INT NOT NULL,
  success BOOLEAN NOT NULL,
  PRIMARY KEY (installed_rank),
  KEY flyway_schema_history_s_idx (success)
) ENGINE=InnoDB;
```

该表不关联业务外键，Flyway 负责写入版本、脚本校验和和执行结果，应用只读检查；字段 version/checksum 可空以兼容 Flyway 自身管理记录。采用目标库的默认字符集及排序规则。存量库基线登记只增加该管理表；如取消首次接管，应停服恢复登记前数据库备份与原应用版本，不随意删除已发生后续迁移的历史。

## 7. Redis 数据契约

无新增、删除或修改的 key 模板、示例、类型、值结构、TTL、读写、失效或一致性规则。原因是 Flyway 使用 MySQL 保存版本和协调迁移，不通过 Redis 加锁或缓存结构版本。原有会话、限流、视频解析、播放与任务 key 完全沿用现有业务规格；接入不清空 Redis。

## 8. 验证

独立测试库通过实际 Flyway CLI 创建结构，不再依靠业务连接建表。测试覆盖空库、重复执行、完整结构、手工部署等价性、存量库登记前备份及数据保留、缺失索引和默认值漂移、已执行脚本修改、DDL 中途失败、并发迁移、对应回滚和初始化后数据保护。账号、音乐、视频、Agent 集成测试使用相同迁移入口。

2026-10-23 本地验证结果：在独立 MySQL 8.4.8 实例上运行完整后端套件，608 项测试全部通过。视频冻结计时测试对 MySQL JSON 浮点往返保留 0.001 毫秒容差，字段、状态及其余值仍精确比较。

当前本地 MySQL 26.7 业务库已完成 preflight、数据库快照备份、baseline V1、migrate、validate 和启动检查；接管前后 23 张业务表的行数与内容 SHA-256 均一致。后端在原 8001 端口重启，健康、认证状态、验证码以及前端 5174 的代理健康请求均返回 200。Flyway 对 MySQL 26.7 提示其高于官方已验证版本；本次兼容结果仅来自上述本地实测。

Docker Compose 配置校验通过；完整 Docker 镜像构建及云端发布未执行。本地执行不等同云端部署。

## 附录：V1 业务表完整结构

以下与 001_ddl_schema.sql 一致，包含全部 23 张表、索引和关系，不包含 Flyway 自建历史表。

```sql
-- v1 full schema. Empty database only; existing databases must pass preflight before baseline.

CREATE TABLE music_profile (
        id INT PRIMARY KEY, nickname VARCHAR(40) NOT NULL, bio VARCHAR(200) NOT NULL DEFAULT '', avatar MEDIUMTEXT,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_profile_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_tracks (
        id CHAR(64) PRIMARY KEY, provider VARCHAR(12) NOT NULL, identifier VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL, artist VARCHAR(255) NOT NULL, webpage_url TEXT NOT NULL,
        thumbnail TEXT, duration DOUBLE, access VARCHAR(100) NOT NULL,
    custom_thumbnail TEXT NULL,
    cover_updated_at DOUBLE NOT NULL DEFAULT 0,
    cover_checked_at DOUBLE NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_tracks_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_favorites (
        track_id CHAR(64) PRIMARY KEY, created_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_favorites_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_history (
        track_id CHAR(64) PRIMARY KEY, played_at DOUBLE NOT NULL, play_count INT NOT NULL DEFAULT 1,
        preview BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_history_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_playlists (
        id CHAR(24) PRIMARY KEY, name VARCHAR(80) NOT NULL, description VARCHAR(300) NOT NULL,
        created_at DOUBLE NOT NULL,
    thumbnail TEXT NULL,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_playlists_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_playlist_tracks (
        playlist_id CHAR(24) NOT NULL, track_id CHAR(64) NOT NULL, added_at DOUBLE NOT NULL,
        PRIMARY KEY (playlist_id, track_id),
        FOREIGN KEY (playlist_id) REFERENCES music_playlists(id) ON DELETE CASCADE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    position INT UNSIGNED NULL DEFAULT NULL,
    KEY playlist_position (playlist_id,position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_downloads (
        id CHAR(24) PRIMARY KEY, track_id CHAR(64) NOT NULL, filename VARCHAR(255) NOT NULL UNIQUE,
        created_at DOUBLE NOT NULL, imported BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    file_hash CHAR(64) NULL,
    file_size BIGINT NULL,
    file_mtime BIGINT NULL,
    file_available BOOLEAN NOT NULL DEFAULT TRUE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_downloads_owner (user_id),
    KEY file_hash_index (file_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_track_aliases (
        alias_id CHAR(64) PRIMARY KEY, track_id CHAR(64) NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_lyrics (
        track_id CHAR(64) PRIMARY KEY, text MEDIUMTEXT NOT NULL, source VARCHAR(80) NOT NULL,
        format VARCHAR(8) NOT NULL, signature TEXT NOT NULL, checked_at DOUBLE NOT NULL,
        `manual` BOOLEAN NOT NULL DEFAULT FALSE, updated_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_lyrics_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_theme (
        id INT PRIMARY KEY, config TEXT NOT NULL,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_theme_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE users (id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(40) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL DEFAULT 'user', status VARCHAR(16) NOT NULL DEFAULT 'active',
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE, auth_version INT NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE system_settings (id INT PRIMARY KEY, config MEDIUMTEXT NOT NULL)
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE audit_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_id BIGINT,
    action VARCHAR(80) NOT NULL,target VARCHAR(255) NOT NULL,result VARCHAR(40) NOT NULL,ip VARCHAR(64) NOT NULL,
    created_at DOUBLE NOT NULL, INDEX audit_actor(actor_id,created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE resources (kind VARCHAR(24) NOT NULL, resource_key VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL, payload MEDIUMTEXT NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(kind,resource_key), INDEX resource_owner(user_id,kind),
    device_id CHAR(36) NULL,
    KEY resource_device (user_id,kind,device_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE account_migrations (id VARCHAR(40) PRIMARY KEY, completed_at DOUBLE NOT NULL,
    backup_path TEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_recent_links (
        user_id BIGINT NOT NULL, url_hash CHAR(64) NOT NULL, link_id VARCHAR(64) NOT NULL,
        url TEXT NOT NULL, submitted_at DOUBLE NOT NULL,
        PRIMARY KEY(user_id,url_hash), INDEX video_recent_time(user_id,submitted_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_parse_records (
        record_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL,
        url TEXT NOT NULL, webpage_url TEXT, title TEXT, platform VARCHAR(100) NOT NULL,
        status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), snapshot JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_parse_owner_time(user_id,deleted,created_at,record_id),
        INDEX video_parse_platform(user_id,deleted,platform,created_at),
        INDEX video_parse_status(user_id,deleted,status,created_at), INDEX video_parse_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_summaries (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        webpage_url TEXT, status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), content JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_summary_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_summary_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_mind_maps (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        summary_task_id VARCHAR(64), version INT UNSIGNED NOT NULL, status VARCHAR(24) NOT NULL,
        worker_id VARCHAR(255), content JSON NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        deleted BOOLEAN NOT NULL DEFAULT FALSE, UNIQUE KEY video_map_version(summary_task_id,version),
        INDEX video_map_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_map_summary(user_id,summary_task_id,deleted,status,created_at),
        INDEX video_map_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_view_states (
        user_id BIGINT NOT NULL, record_id VARCHAR(64) NOT NULL, view_key VARCHAR(255) NOT NULL,
        selected_tab VARCHAR(24) NOT NULL, playback_time DOUBLE NOT NULL, view_state JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(user_id,record_id), UNIQUE KEY video_view_key(view_key),
        INDEX video_view_recent(user_id,deleted,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_recommendation_runs (
    run_id CHAR(32) NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, parent_run_id CHAR(32) NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending', request JSON NOT NULL, context JSON NOT NULL,
    steps JSON NOT NULL, result JSON NOT NULL, policy_version VARCHAR(40) NOT NULL,
    skill_version VARCHAR(64) NOT NULL, idempotency_key CHAR(64) NOT NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 0, result_version INT UNSIGNED NOT NULL DEFAULT 0,
    saved_playlist_id CHAR(24) NULL, saved_at DOUBLE NULL, worker_id VARCHAR(64) NULL,
    lease_until DOUBLE NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE KEY rec_request(user_id,idempotency_key), KEY rec_owner(user_id,deleted,created_at),
    KEY rec_worker(deleted,status,lease_until),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_run_id) REFERENCES music_recommendation_runs(run_id) ON DELETE SET NULL,
    FOREIGN KEY(saved_playlist_id) REFERENCES music_playlists(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_track_features (
    user_id BIGINT NOT NULL, track_key CHAR(64) NOT NULL, track_snapshot JSON NOT NULL,
    features JSON NOT NULL, evidence JSON NOT NULL, content_signature CHAR(64) NOT NULL,
    analyzer_version VARCHAR(40) NOT NULL, checked_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL,
    PRIMARY KEY(user_id,track_key), KEY feature_expiry(expires_at),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_server_assets (
 id CHAR(24) NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, track_id CHAR(64) NOT NULL,
 filename VARCHAR(255) NOT NULL, original_filename VARCHAR(255) NOT NULL,
 asset_kind VARCHAR(24) NOT NULL, status VARCHAR(16) NOT NULL, mime_type VARCHAR(100) NOT NULL,
 file_size BIGINT NULL, sha256 CHAR(64) NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 UNIQUE KEY asset_owner_file (user_id,filename),
 KEY asset_owner_kind_status (user_id,asset_kind,status,created_at),
 KEY asset_track (track_id), KEY asset_hash (sha256),
 CONSTRAINT asset_track_fk FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```


### 每日发现接入（2026-10-23）

每日发现新增 V2__daily_discovery.sql，应用门禁更新为 V2；V1 基线核对契约仍仅用于存量 V1 接管，不用于完整 V2 校验。V2 新增六表，定义及回滚兼容要求见 [每日发现](features/daily-music-discovery.md)。

### 音乐搜索测试日志 V3（2026-10-23）

新增 V3__music_search_benchmark_logs.sql，应用结构门禁更新为 V3；V1/V2 迁移保持不变。新增表、幂等写入、索引及数据兼容见 [音乐搜索并发测试日志](performance/music-search-benchmark-2026-10-23.md)。回滚日志功能保留 V3 历史和兼容 V3 的门禁，不删除迁移历史或测试数据。

### 候选池与音乐画像 V4（2026-10-24）

`V4__discovery_pool_and_profile.sql` 只扩展 profiles 并新增六表，门禁为 V4，V1～V3 不变。完整新增/修改字段、索引、外键与关系见[今日推荐数据结构](features/daily-music-discovery.md#8-mysql-结构)。活动任务唯一约束采用有唯一索引的 VIRTUAL 生成列，以兼容当前 MySQL 的外键级联限制；备份工具 INSERT 排除生成列，恢复时由 MySQL 重算。

应用停止后先备份项目库及私有配置，执行 `database.py migrate`；成功 DDL 后入口幂等回填旧 daily_discovery 的逐日实际/预留/未知用量及仍有效候选，不混入场景歌单，不调用模型或网络。随后 validate/check 再启动新执行器，画像 Agent 默认开启。重复 migrate 可补偿回填中断；不能在新旧付费执行器同时运行时迁移。

回滚优先关闭学习与融合并保留账本、候选、版本和 V4 历史，使用兼容 V4 门禁的构建。原 V3 构建不能直接连接 V4；结构反向操作须另备份并以新补偿迁移或匹配备份恢复，不能直接删表、删历史、返还已发生用量。Redis 无变更，迁移不启停共享中间件。
