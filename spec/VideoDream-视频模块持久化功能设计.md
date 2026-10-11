# VideoDream 视频模块持久化功能设计

版本：v0.5｜日期：2026-10-11｜状态：独立业务表实现，待业务验收

## 1. 设计目标

按账号保存搜索记录、解析记录、AI 总结、字幕和思维导图，支持刷新、重新登录、服务重启及跨设备访问后恢复查看。MySQL 保存长期业务数据，Redis 保存临时解析与播放信息，文件系统保存生成文件。

“搜索记录”指最近提交的视频链接。本次范围为视频历史和预览恢复，不增加关键词搜索、导图编辑或图片导出功能。

## 2. 用户功能

- **搜索记录：**有效链接提交时保存，解析失败也保留；按链接去重并更新提交时间，输入框展示最近 5 条，支持删除和清空。
- **解析历史：**每次主动解析创建独立记录；按时间倒序分页，每页 20 条，支持标题／链接搜索、平台和状态筛选。
- **历史列表：**展示封面、标题、平台、时长、解析时间、状态及总结／导图标记；支持打开、重新解析、复制链接和删除。
- **历史详情：**展示保存的解析信息，提供“总结、字幕与转写、思维导图”标签页，并支持下载已有成果文件。
- **导图生成：**默认复用已有成功结果或进行中的任务；主动重新生成时创建新版本，成功后切换默认版本，失败时保留上一版。
- **预览恢复：**保存标签页、导图缩放和平移、折叠节点、播放进度；恢复播放器后保持暂停。

## 3. 存储分配

新增 5 张视频业务表，分别保存最近链接、解析记录、总结、思维导图版本和视图状态。现有 `resources` 只保留视频数据的归属、资源 ID、状态及关联 ID 等轻量目录信息；正文、字幕和节点树只保存在对应业务表。通用下载任务和文件索引继续使用已有结构。

| 数据 | 保存内容 | 存储与期限 |
|---|---|---|
| `video_recent_links` | 用户、视频页面链接、最近提交时间 | MySQL，保留最近 5 条 |
| `video_parse_records` | 链接、平台、标题、封面引用、时长、作者、格式描述、状态、错误、时间 | MySQL，保留至主动删除 |
| `video_summaries` | 总结、字幕／转写、来源、语言、任务状态、解析记录关联 | MySQL，保留至主动删除 |
| `video_mind_maps` | 节点树、说明、时间戳、字幕引用、版本、总结关联 | MySQL，保留至主动删除 |
| `video_view_states` | 标签页、导图版本及视图状态、播放进度、最近打开时间 | MySQL，按用户和记录保存 |
| 近期解析缓存 | 可复用解析结果、临时媒体地址 | Redis，最长 10 分钟 |
| 预览会话 | 用户 ID、媒体地址、请求参数、有效期 | Redis，最长 10 分钟 |
| 任务进度 | 任务身份、阶段、进度快照、最终状态 | MySQL；Redis 高频缓存按需增加 |

能够识别源站过期时间时，媒体地址缓存期限不超过地址有效期；MySQL 不将临时签名地址作为长期播放依据。视频、封面和 Markdown／JSON 文件保存在现有目录，数据库保存引用、归属及大小。

### 3.1 MySQL 独立业务表

全部使用 InnoDB、utf8mb4。结构由 `backend/video_store.py` 统一建表和迁移；业务 JSON 使用 MySQL 原生 `JSON` 类型。

```sql
CREATE TABLE video_recent_links (
  user_id BIGINT NOT NULL,
  url_hash CHAR(64) NOT NULL,
  link_id VARCHAR(64) NOT NULL,
  url TEXT NOT NULL,
  submitted_at DOUBLE NOT NULL,
  PRIMARY KEY (user_id, url_hash),
  INDEX video_recent_time (user_id, submitted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_parse_records (
  record_id VARCHAR(64) PRIMARY KEY,
  user_id BIGINT NOT NULL,
  url TEXT NOT NULL,
  webpage_url TEXT,
  title TEXT,
  platform VARCHAR(100) NOT NULL,
  status VARCHAR(24) NOT NULL,
  worker_id VARCHAR(255),
  snapshot JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  INDEX video_parse_owner_time (user_id, deleted, created_at, record_id),
  INDEX video_parse_platform (user_id, deleted, platform, created_at),
  INDEX video_parse_status (user_id, deleted, status, created_at),
  INDEX video_parse_worker (deleted, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_summaries (
  task_id VARCHAR(64) PRIMARY KEY,
  user_id BIGINT NOT NULL,
  parse_record_id VARCHAR(64),
  webpage_url TEXT,
  status VARCHAR(24) NOT NULL,
  worker_id VARCHAR(255),
  content JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  INDEX video_summary_record (user_id, parse_record_id, deleted, created_at),
  INDEX video_summary_worker (deleted, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_mind_maps (
  task_id VARCHAR(64) PRIMARY KEY,
  user_id BIGINT NOT NULL,
  parse_record_id VARCHAR(64),
  summary_task_id VARCHAR(64),
  version INT UNSIGNED NOT NULL,
  status VARCHAR(24) NOT NULL,
  worker_id VARCHAR(255),
  content JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE KEY video_map_version (summary_task_id, version),
  INDEX video_map_record (user_id, parse_record_id, deleted, created_at),
  INDEX video_map_summary (user_id, summary_task_id, deleted, status, created_at),
  INDEX video_map_worker (deleted, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_view_states (
  user_id BIGINT NOT NULL,
  record_id VARCHAR(64) NOT NULL,
  view_key VARCHAR(255) NOT NULL,
  selected_tab VARCHAR(24) NOT NULL,
  playback_time DOUBLE NOT NULL,
  view_state JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (user_id, record_id),
  UNIQUE KEY video_view_key (view_key),
  INDEX video_view_recent (user_id, deleted, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

字段用途：

| 表 | 业务键和约束 | JSON 内容与写入方式 |
|---|---|---|
| `video_recent_links` | `(user_id, sha256(url))` 唯一；每条链接一行 | 无 JSON；提交时锁定用户行，去重、更新时间并保留最近 5 条 |
| `video_parse_records` | `record_id` 主键 | `snapshot` 保存兼容现有接口的完整解析快照、错误和格式描述；查询常用字段另设列 |
| `video_summaries` | `task_id` 主键；`parse_record_id` 可空 | `content` 保存任务响应、总结正文、字幕／转写及文件引用 |
| `video_mind_maps` | `task_id` 主键；同一总结内 `version` 唯一 | `content` 保存节点树、任务状态、来源关联与版本；重新生成新增行 |
| `video_view_states` | `(user_id, record_id)` 主键；`view_key={user_id}:{record_id}` | `view_state` 保存标签页、总结／导图选择、播放格式、进度及缩放／平移／折叠节点 |

`created_at`、`updated_at` 和 `submitted_at` 沿用 Unix 时间戳；解析、成果和视图使用 `deleted` 软删除。最近链接只保留 5 条，删除时直接移除对应行。

平台／状态／时间及成果关联使用独立列和组合索引。标题与链接的包含搜索仍使用 `LIKE '%关键词%'`，不能由普通 B-tree 索引直接覆盖；历史列表通过 `EXISTS` 查询成果标记，不逐条加载总结正文或导图树。

关系由业务事务维护，不新增外键，以保留无法关联的旧成果并支持“删除解析记录但保留成果”。创建、保存视图和删除时检查归属，按“解析记录、总结、导图”的顺序加锁。新导图版本使用包含已删除版本的 `MAX(version)+1`，防止复用已删除版本号；唯一约束提供最终保护。

`resources` 继续供权限检查、个人任务与文件、管理员数据列表使用。视频条目的 `payload` 只保存 `storage=video-tables-v1`、状态和关联 ID 等目录元数据；读取接口从业务表还原原有响应形状。业务表和目录在同一事务中更新，删除同时写入两处删除状态。下载任务沿用 `resources`，保留 `parse_record_id`。

### 3.2 Redis Key 设计

视频功能新增三类 key，全部使用 Redis `String` 类型，value 为 JSON，通过 `SET ... EX` 设置有效期：

| 用途 | Key 格式 | Value 内容 | TTL |
|---|---|---|---|
| 解析缓存 | `videodream:video:parse:{user_id}:{sha256(url)}` | 完整解析结果，包含格式和临时媒体地址，不包含本次解析记录 ID | 最长 600 秒 |
| 预览会话 | `videodream:video:preview:{user_id}:{token}` | `user_id`、`media_url`、`referer`、`expires_at`、`parse_record_id` | 最长 600 秒 |
| 进程心跳 | `videodream:video:worker:{worker_id}` | `{"alive": true}` | 45 秒，每 10 秒续期 |

Key 各部分的含义：

- `videodream:` 是当前代码固定的应用前缀，`video` 是视频功能命名空间。
- `user_id` 隔离解析缓存和预览会话，不跨账号复用临时播放源。
- `sha256(url)` 是传入页面 URL 的 SHA256 十六进制摘要；解析入口先规范化链接，短链与最终页面可能分别缓存。
- `token` 由 `secrets.token_urlsafe(24)` 随机生成；读取预览时验证账号归属、有效期及关联记录是否已删除。
- `worker_id` 为 `{hostname}:{pid}:{random}`；进程心跳不带用户 ID，同一进程处理的任务引用同一个心跳。

解析与预览缓存的有效期按下式计算：

```text
TTL = min(600 秒, 所有可识别媒体地址中最早的过期时间 - 当前时间 - 15 秒)
```

当前识别 URL 查询参数 `expire`、`expires`、`expiration`、`deadline`、`x-expires` 中的 Unix 时间戳，并兼容秒和毫秒。音视频分离格式分别检查两条流，使用较早到期时间；已过期的地址不写入解析缓存，也不创建预览会话。没有可识别过期时间时按 600 秒缓存，源站仍可能提前失效，需要重新获取播放源。

主动重新解析清除对应解析缓存；删除记录清除原页面及最终页面缓存，关联预览在读取时失效；清除平台会话时通过 `SCAN` 清理当前用户的解析和预览缓存。心跳查询失败时推迟任务失联判断，不把 Redis 故障当作进程退出。

总结正文、字幕、导图和视图状态目前直接读取 MySQL，没有增加 Redis 业务缓存；任务进度也仍保存在 MySQL。账号认证的 Redis key 沿用既有实现，不属于本次新增范围。

### 3.3 缓存使用边界

MySQL 是长期业务数据的权威来源，清空 Redis 后仍能读取历史、字幕和导图。解析格式和播放地址具有时效性，适合使用 Redis；进程心跳依赖自动过期判断失联，也使用 Redis。

总结、字幕和导图目前直接读取 MySQL；任务进度按最多每秒一次落库，并在终态强制保存。只有观察到实际读取压力后，才增加按用户和资源 ID 隔离的成果或进度缓存，并在事务提交后失效对应缓存。

Redis 环境目前通过连接配置及 `REDIS_DATABASE` 隔离；多个环境共享同一 Redis 数据库时，应将应用前缀改成包含环境名的可配置前缀，避免 key 冲突。任务进度缓存同样按实际负载另行启用。

## 4. 数据关联

新建总结、下载任务时携带并保存 `parse_record_id`；导图通过 `summary_task_id` 关联总结，并保存版本信息。历史详情按关联查询成果，列表只返回摘要，正文和导图按需加载。

- 同一链接可以有多条解析记录；重新解析产生新记录，原记录及成果保留。
- 同一总结可以保留多个导图版本，仅成功生成后更新默认版本引用。
- 视图状态按账号、解析记录保存；切换导图版本时过滤无效的折叠节点 ID。
- 无法关联到解析记录的旧成果继续在“任务与文件”中访问。

## 5. 主要流程

1. **提交解析：**先创建 MySQL 解析记录，再检查 Redis；命中可用缓存则复用，否则调用解析器。结果或失败原因写入数据库后返回。
2. **打开历史：**根据记录 ID 加载解析信息、成果引用和已保存视图；不自动调用 AI，也不自动重新解析视频。
3. **恢复页面：**详情地址携带记录 ID 和标签页；重新进入视频模块时恢复上次打开记录，记录已删除时回到输入页。
4. **播放或下载：**优先使用关联本地文件，否则从 Redis 获取有效播放源；未命中或地址失效时重新解析。源视频不可用时仍允许查看保存的总结和导图。
5. **生成导图：**在数据库事务中按“解析记录、总结记录”的顺序加锁，检查删除状态、已有结果和进行中的任务，再创建任务；唯一任务由 MySQL 事务保证。
6. **保存视图：**导图操作后防抖保存，播放进度定期及暂停时保存，离开时尽力补存；恢复以最近成功落库的状态为准。

## 6. 接口设计

| 接口 | 用途 |
|---|---|
| `GET /api/videos/recent-links` | 查询最近提交链接 |
| `POST /api/videos/recent-links/migrate` | 导入当前账号浏览器的旧最近链接，服务端记录优先 |
| `DELETE /api/videos/recent-links`、`DELETE /api/videos/recent-links/{id}` | 清空全部或删除单条 |
| `POST /api/parse` | 创建解析记录并解析，响应增加记录 ID |
| `GET /api/videos/last-record` | 查询当前用户上次保存视图的有效记录 |
| `GET /api/videos/records` | 查询、筛选和分页解析历史 |
| `GET /api/videos/records/{id}` | 查询记录详情及成果引用 |
| `POST /api/videos/records/{id}/formats?force=false` | 重新获取可用格式，不增加解析历史；`force=true` 绕过缓存 |
| `DELETE /api/videos/records/{id}` | 删除记录，可指定同时删除关联成果 |
| `PUT /api/videos/records/{id}/view` | 保存标签页、导图视图和播放进度 |
| `POST /api/summaries/{id}/mind-map?regenerate=false` | 复用导图；`regenerate=true` 创建新版本，进行中的任务仍复用 |
| `GET /api/summaries/{id}/mind-map?map_id={map_id}` | 读取指定版本；不指定时优先返回最近成功版本 |

沿用现有总结、导图、下载接口，补充解析记录关联及数据库任务复用；个人“任务与文件”和管理员业务数据页面同步支持新增资源类型。列表支持 `page`、`q`、`platform`、`status`；删除通过 `cascade=true` 选择级联范围。

## 7. 一致性、异常与权限

- **保存顺序：**先提交 MySQL，再更新或删除缓存；落库失败不得显示“已保存”。读取业务缓存前仍需完成用户归属检查。
- **缓存失效：**业务缓存过期或丢失后重新获取；主动重新解析、完成验证码、清除平台会话时绕过或清除相关缓存。
- **服务重启：**完成结果继续可查；任务保存进程 `worker_id`，Redis 心跳每 10 秒更新、租约 45 秒。确认失联的未完成任务标记失败，由用户主动重试；更新时校验读取时的时间及状态，避免覆盖已完成任务。Redis 查询失败时推迟判断；没有进程信息的旧任务仅在超过 24 小时后处理。
- **服务故障：**MySQL 不可用时拒绝需要可靠保存的新操作；认证 Redis 不可用时沿用现有规则返回 503，会话丢失后要求重新登录。
- **删除行为：**清空搜索记录不影响解析历史；删除解析记录默认保留成果，可勾选级联删除。涉及运行任务时先取消，后续回写不得恢复已删除记录；关联预览随记录删除失效。
- **权限控制：**普通用户仅管理本人数据；管理员沿用现有管理权限。Redis 键按用户隔离，预览 token 绑定用户；退出仅清理本地缓存和会话。

## 8. 迁移与验收

启动时运行 `video-tables-v1` 迁移。先创建业务表，取得按数据库名称区分的 MySQL advisory lock，再检查 `account_migrations` 标记。将旧 `video_search`、`video_parse`、`summary`、`mind_map`、`video_view` 原始行及可读总结文件备份到 `.run/video-table-backups/`，随后在事务中迁移。

保留原资源 ID、账号归属、创建／更新时间、删除状态、关联 ID、字幕、节点树及文件引用。旧导图缺少版本号或版本号冲突时，按创建时间和 ID 排序，分配确定性的有效版本号。验证迁移后内容，才提交轻量目录和迁移标记；失败时回滚数据写入并保留备份，下次启动可重试。重复启动检测到标记后不重复迁移。

迁移可读取的本地最近链接；同一账号、同一页面仅有一条有效解析记录时，已有总结及其导图可自动关联。修改关联前，将原始资源快照及已有 Markdown／JSON 文件备份到 `.run/video-history-backups/`；存在多条候选解析记录时保留为独立成果。未保存过的解析历史无法补回。

验收覆盖：解析成功与失败留档、链接去重和并发写入、刷新与跨设备恢复、重启后成果读取、导图复用与版本切换、已删除版本不复用、过期播放源重新获取、删除范围、账号隔离、迁移内容与文件完整性、迁移回滚与幂等性，以及现有视频和音乐功能回归。

## 9. 实现约定

- 最近链接按条保存到 `video_recent_links`，事务锁定用户行后更新，避免并发提交丢失数据。
- 临时格式在数据库中保存为 `saved:{index}` 等稳定占位标识；实际媒体地址只放 Redis，音视频分离时取两条流中较早的过期时间，并预留 15 秒。
- 导图视图保存 `scale`、相对画布宽高的根节点 `x`／`y` 坐标及 `collapsed` 节点 ID；跨屏幕恢复使用相对位置，不保存 DOM 或截图。
- 标签页和格式变更防抖 650 毫秒保存；播放进度最多每 5 秒保存一次，暂停及离开页面时补存。新导图生成期间保留上一版内容和视图，失败时仍可查看；成功后使用新版本的初始视图。
- 浏览器请求按当前记录和版本选择校验，迟到响应不得覆盖新记录。恢复页面只读取持久化成果和任务状态，生成及重新解析均由用户触发。
- 专用表的查询列用于历史筛选和成果关联，JSON 用于保留完整业务内容；新增的存储适配层保持前端和已有接口兼容。

## 10. 本地运行与链接校验修复

开发入口固定为前端 `http://127.0.0.1:5174/`，Vite 默认代理到后端 `http://127.0.0.1:8001`。后端变更后重启 8001 上的项目进程，避免前端已更新而后端仍缺少新增接口。

本机 TUN 代理可能将 B 站等平台的 DNS 结果映射到 `198.18.0.0/15` 的 Fake-IP。旧公网校验因此误报“仅支持可访问的公网 HTTP/HTTPS 地址”。`backend/network.py` 针对已支持的视频平台／CDN 域名允许这一代理地址段，域名必须精确匹配或为有效子域名；不放行任意域名、字面量 Fake-IP、回环、私网或链路本地地址。URL 凭据、协议、端口及每次重定向仍按原规则校验。

该修复解决本机代理环境的链接校验误拦截，平台登录、版权、源视频删除及源站风控仍由解析器返回对应结果。
