# VideoDream 用户与管理员功能设计

> 2026-10-23 数据库管理更新：表结构统一由 Flyway 管理，完整定义见 `backend/migrations/sql/V1__baseline.sql`。启动时只检查版本；本文早期自动建表、自动应用结构迁移的说明由 [Flyway 规格](Flyway数据库版本管理设计.md) 替代。历史业务和文件迁移保留为显式命令，已有业务表及 Redis 契约不变。

版本：v1.3

日期：2026-10-26

状态：功能实现与维护规格；云端用户目录须在发布时离线迁移

## 1. 设计目标

为个人开发项目增加账号登录、个人数据隔离和管理员后台。参考成熟产品的安全实践，固定使用普通用户、超级管理员两种角色。

## 2. 角色权限矩阵

| 功能 | 普通用户 | 超级管理员 |
| --- | --- | --- |
| 搜索、播放、下载音乐 | 可以 | 可以 |
| 视频下载、AI 总结、思维导图、去水印 | 可以 | 可以 |
| YouTube / Pornhub 的全部功能、历史与关联资源 | 不可以 | 可以 |
| 海外平台代理连通性检测 | 不可以 | 可以 |
| 修改个人资料和密码 | 本人 | 本人 |
| 收藏、历史、歌单、任务、文件及处理结果 | 管理自己的 | 管理全部 |
| 查看、下载、删除其他用户业务数据 | 不可以 | 可以 |
| 查看用户列表、创建用户、停用及恢复账号 | 不可以 | 可以 |
| 重置用户密码、调整用户角色 | 不可以 | 可以 |
| 配置注册方式、业务功能开关和外部服务 | 不可以 | 可以 |
| 查看系统日志、任务统计及存储占用 | 不可以 | 可以 |
| 查看密码、认证密钥和完整访问凭证 | 不可以 | 不可以 |

角色字段固定为 `user`、`super_admin`，不设置自定义角色。禁止删除、停用或降级最后一个有效超级管理员。

2026-10-20 权限补充：YouTube / Pornhub 按来源平台统一限制，覆盖音频、预览、字幕、总结、导图、去水印、封面、旧文件与缓存；普通用户不能从其他入口调用。后台任务在检查点重新读取账号角色，降级后停止受限任务。实现、无 DDL 数据兼容、Redis 与回滚明细见[海外下载规格](features/overseas-video-downloads.md)。

## 3. 账号与个人中心

- **账号功能**：账号密码及图片验证码登录、退出、密码修改和管理员重置密码；公开注册默认开启，超级管理员可关闭，新注册账号始终为普通用户。2026-10-21 已按用户指令开启本机注册，登录和注册均必须校验图片验证码，数据及交付规则见第 17 节。
- **初始化与状态**：通过本地初始化流程创建首个超级管理员；账号状态分为正常、停用，停用后不能登录或继续使用旧会话。
- **个人中心**：头像、昵称、简介、密码修改和本人登录记录。退出时清理个人数据缓存及播放状态；重置密码后要求修改临时密码。

## 4. 认证与存储架构

| 项目 | 最终设计 |
| --- | --- |
| Access Token | JWT，有效期 15 分钟，前端保存在内存，通过 `Authorization: Bearer` 发送 |
| Refresh Token | 高熵随机凭证，有效期 7 天，放入 `HttpOnly` Cookie，服务端仅保存摘要 |
| 媒体 Cookie | `videodream_media`，签名 JWT，受众 `videodream-media`，有效期不超过 Redis 会话剩余时间；只允许指定媒体读取路由 |
| Redis | 保存登录会话、刷新凭证摘要、用户会话索引及登录限流计数，使用 TTL 自动过期 |
| MySQL | 保存用户、密码摘要、角色、账号状态、业务数据、系统配置和关键操作日志 |

JWT 包含用户 ID、会话 ID、签发及过期时间，不包含密码或密钥。账号状态和角色以 MySQL 当前数据为准，JWT 不作为最终权限依据。

## 5. 登录与会话流程

1. **登录**：校验图片验证码、账号、密码、状态及限速规则，创建 Redis 会话，签发 JWT 和 Refresh Token。注册成功直接创建同样的登录会话，无需重复填写验证码。
2. **业务请求**：验证 JWT 签名与有效期，检查 Redis 会话，再读取 MySQL 当前账号状态和角色，执行数据权限检查。
3. **刷新**：校验账号、会话及刷新凭证，原子轮换 Refresh Token 并签发新 JWT；旧刷新凭证失效，轮换不无限延长会话期限。
4. **撤销**：退出撤销当前会话；停用或重置密码撤销全部会话；角色降级在后续请求即时生效。

生产 Cookie 启用 `Secure`、`HttpOnly`、`SameSite`，刷新和退出接口保护 CSRF。Redis 不可用时返回服务暂不可用；会话数据丢失后要求重新登录，不能仅凭 JWT 恢复授权。

## 6. 业务数据与权限控制

收藏、历史、歌单、任务、文件、总结和处理结果关联 `user_id`。普通用户的列表、详情、统计及修改操作必须限定当前用户；超级管理员允许管理全部数据。公共歌曲元数据可以继续复用。

文件下载、封面、字幕、歌词、预览、播放及 Range 请求统一鉴权。批量操作逐项检查，后台任务保留发起用户和输出归属；账号停用后取消排队任务，在可中断点停止运行任务。

## 7. 基础安全与日志

- **凭证安全**：使用 Argon2id 保存密码摘要，登录失败限速；JWT 固定允许算法并校验签发方和受众，密钥通过本地环境配置管理。
- **接口安全**：公网使用 HTTPS；验证输入、上传内容和文件路径，防范 XSS、SSRF、路径穿越及命令注入；所有权限由后端执行。
- **关键日志**：记录登录异常、密码重置、停用、角色调整、删除及配置修改，包含操作者、对象、结果和时间，不记录密码或访问凭证。

## 8. 管理后台

提供用户管理、业务数据管理、系统配置、日志和运行统计页面。支持筛选、分页及必要的批量操作；删除、停用和角色变更提供确认提示，外部服务密钥脱敏展示。

AI 配置分为文字总结与思维导图、截图识别、语音转写三组，每组独立设置服务地址、模型和密钥。密钥只返回已配置标记与掩码，未勾选替换时保留，明确提交空串仅清除对应组。优先级为进程变量、`.env.universal-video.local`、旧 `.env`、后台配置、默认值；有环境配置的组在后台只读并提示修改文件后重启后端，接口拒绝覆盖该组，保存其他系统设置不把环境覆盖值写回数据库。新任务使用新配置，已固定配置的任务保持原值。

MySQL 仍使用以下完整表结构，不新增表、列、索引或关联：

```sql
CREATE TABLE IF NOT EXISTS system_settings (
  id INT PRIMARY KEY,
  config MEDIUMTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`id=1` 的 JSON 保留 `ai_base_url`、`ai_model`、`ai_api_key`，新增 `ai_vision_base_url`、`ai_vision_api_key`、`ai_asr_base_url`、`ai_asr_api_key`，保留已有 `ai_vision_model`、`ai_asr_model`。无外键，除主键外无索引。旧数据没有独立连接字段时兼容原共享连接，下次保存前分别复制旧连接再应用修改，保证首次切换文字服务不会改变其他组。环境文件初始化时补齐三组当前有效连接，真实凭证只留在本地忽略文件。回滚代码可继续读取原有文字字段，新增 JSON 键可保留；回滚前备份配置，如需恢复共享行为则恢复原 env 并删除新增独立连接键。

Redis 无新增或修改的 key、值结构、TTL、读写或失效规则；AI 配置直接从环境文件及 MySQL 读取，不使用 Redis 缓存，因此无新增缓存一致性要求。

## 9. 存量迁移与交付

迁移前备份数据库和文件；现有固定个人资料及无归属业务数据默认归初始化超级管理员，补齐用户关联和文件索引，废止旧匿名资源入口及旧播放凭证。无法确认关联的数据单独记录处理。

沿用 Vue 3、FastAPI、MySQL，启用项目预留的 Redis 配置。开发服务保持前端 `5174`、后端 `8001`，统一访问地址为 `http://127.0.0.1:5174/`。

## 10. 验收标准

使用两个普通用户和一个超级管理员验证：用户数据互不可见，普通用户无法调用管理接口，管理员可以管理全部业务数据；JWT 过期可刷新，旧刷新凭证不可重复使用，退出、停用及降级有效，Redis 故障不能绕过认证。现有音乐、下载、总结和去水印功能保持可用，越权访问或撤销失效必须阻断发布。

## 11. 云端媒体鉴权与播放修复（2026-10-21）

云端已下载文件出现 `/api/files/…m4a` 返回 401；列表和歌词正常不代表媒体请求带有有效凭证。普通业务 fetch 添加 Bearer，而原生音视频、图片和下载链接曾依赖 Service Worker 向页面索取凭证。媒体读取改为浏览器自动携带 HttpOnly Cookie，认证初始化不再等待 Worker 支持、注册或控制页面。旧 Worker 升级期间保留页面凭证消息处理，新 Worker 激活后接管旧页面并直接透传网络请求。

`videodream_media` 使用当前签名密钥和 HS256，包含 `sub`、`sid`、`iat`、`exp`、`iss=videodream`、`aud=videodream-media`，与普通 Access JWT 的 `videodream-web` 受众分离。Cookie 为 host-only，Path `/api/`，HttpOnly、SameSite Strict，生产启用 Secure；最长 604800 秒，签发期限不超过对应 Redis 会话剩余 TTL。登录和成功刷新会话时签发；现有用户通过刷新获得；退出和修改密码清理 Cookie。刷新摘要轮换不撤销有效媒体 Cookie。

媒体 Cookie 只允许以下路由的 GET/HEAD；实际支持的方法由路由定义约束：

- `/api/files/{filename}`、`/api/summary-files/{filename}`、`/api/thumbnails/{filename}`。
- `/api/library/images/{name}`、`/api/image-proxy`。
- `/api/music/playback/{token}/content`、`/api/previews/{token}/content`。
- `/api/watermarks/videos/{filename}`，不包括其 `/info` 业务接口。
- `/api/music/batches/{batch_id}/archive`。

普通业务继续要求 Bearer。带 Bearer 的请求优先校验该凭证，失败不回退 Cookie；未携带 Authorization 且命中媒体读取白名单时才接受媒体 Cookie。媒体 JWT 不能作为业务 Bearer。每次读取仍检查 Redis 会话、当前 MySQL 账号状态和 auth_version、角色、临时密码要求、功能开关与资源归属；缺失会话返回 401，越权资源返回 404，Redis 故障返回 503。会话撤销后拒绝后续请求，已发送的响应内容无法追回。

失败认证日志记录服务端生成的 request_id、方法、接口区域、认证来源、状态和原因，不记录 JWT、Cookie、刷新凭证或签名播放地址；响应返回 `X-Request-ID`。

## 12. 用户文件目录与统一读写

所有下载、导入和生成的用户文件保存到 `downloads/users/{user_id}/`。本地基于项目目录，容器内为 `/app/downloads/users/{user_id}/`，云端宿主机为 `/opt/videodream/downloads/users/{user_id}/`。文件仍占用服务器磁盘，另存为设备文件不会自动删除服务器原件。目录按需建立，以稳定数据库 ID 命名，用户名或昵称修改不影响路径。

| 子目录 | 内容 |
| --- | --- |
| files | 音频、视频、导入音频、去水印结果及关联 LRC |
| thumbnails | 自动提取或缓存的封面 |
| images | 用户上传的头像、封面和背景图 |
| lyrics | 旧歌词 JSON 缓存，歌词长期数据继续保存 MySQL |
| summaries | 总结 Markdown、JSON 导出 |
| batches | 响应结束后删除的批量 ZIP |
| tmp | 下载、转码及转写的中间文件 |

`backend/core/storage.py` 统一生成和解析路径。新文件以任务所属用户为准；已有资源先检查权限，再从 resources.user_id 定位。管理员读取、上传封面、重试和删除必须保留原用户归属。后台任务固定 user_id，库扫描只扫描当前业务用户的文件目录；管理员跨用户管理通过资源目录完成。容器继续由同一服务账号运行，用户隔离由后端授权实现，不创建额外操作系统账号。

文件名保留任务 ID/随机标识等全局唯一前缀，保持现有 resources 复合主键和 music_downloads 文件名唯一约束。现有 `/api/files/{filename}` 等 URL 不加入物理目录，数据库不保存绝对路径。拒绝路径穿越、非法文件名和符号链接；取消任务、失败清理、打包和删除限定到目标用户及资源。停用账号保留其文件；删除账号沿用明确的管理员删除流程。

## 13. 文件响应与有限恢复

M4A 显式返回 `audio/mp4`，MP3 返回 `audio/mpeg`，MP4 返回 `video/mp4`。播放使用 inline，明确保存时文件地址增加 `download=1` 返回 attachment；总结和 ZIP 默认附件。文件、音视频代理支持 GET/HEAD，保留 Range、If-Range、Content-Length、Content-Range、Accept-Ranges 和正确的 206/416；私有响应使用 `private, no-store`。新生成 M4A 增加 faststart，存量文件不批量转码。

播放器失败后以带媒体 Cookie 的小范围读取诊断状态，并取消诊断响应体。401 刷新会话后重载一次，在线内容 410 重新获取源一次，尽量恢复原进度；总自动恢复最多一次。403 提示权限或密码要求，404 提示文件不存在或无权访问，502 提示上游不可用，503 提示服务故障。200/206 与媒体解码错误结合判断格式异常；浏览器要求手势时保留点击继续入口，不循环自动播放。移动端必须用真实设备补充验收，不能仅以桌面缩窄窗口替代。

## 14. 存量迁移、兼容与回滚

`scripts/migrate-user-storage.py` 提供 plan/apply/status/rollback。plan 输出清单，不移动文件；apply/rollback 要求应用及所有写入进程已停止，并显式传入 `--offline-confirmed`。脚本使用 MySQL 命名锁防止多个迁移并发。迁移清单必须在 downloads 外，默认运维位置 `.run/storage-migrations/`。

按 resources 归属交叉核对 music_downloads；关联 LRC、歌词缓存和音频提取封面可从原文件名推导归属。归属冲突、目标内容不同或符号链接阻止执行；未索引文件保留原处并报告，不自动分给管理员；已缺失文件记入 missing。清单包含用户、类型、文件名、来源、目标、大小、SHA-256、授权依据和状态。

apply 在任何移动前校验全部清单并自动备份当前数据库 SQL 和整个 downloads ZIP，校验 ZIP CRC。通过同文件系统的“不覆盖目标的硬链接创建 + 校验 + 删除原目录项”搬迁，逐项持久化进度；中断可重复执行并识别已搬迁或双目录项状态，禁止覆盖不同内容。迁移执行中的 active 清单只允许已确认用户、类型、文件名和哈希匹配的旧路径回读；完成后删除 active，正常读取不再搜索公共根目录。

全部验证后写 account_migrations 的 `user-storage-v1` 记录。回滚先备份当前数据，再扫描所有现有用户目录，包括升级后新增的文件，反向恢复旧布局；文件名冲突阻止全部搬迁。成功后删除迁移标记并回滚匹配代码。仅回滚镜像无法恢复旧文件布局，不允许用升级前备份直接覆盖并丢失新增文件。

## 15. 本次 MySQL / Redis 数据契约

无新增或修改的 MySQL 表结构、列、索引、外键及业务关系。目录由 users.id、resources.kind、resources.resource_key 推导，music_downloads.filename 与历史 URL 保持不变；现有 resource_owner、音乐用户索引、文件名唯一索引与歌曲外键继续使用。唯一新增迁移标记写入现有 account_migrations，其完整结构为：

```sql
CREATE TABLE account_migrations (
  id VARCHAR(40) PRIMARY KEY,
  completed_at DOUBLE NOT NULL,
  backup_path TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

主键为 id，无其他索引或外键。新增行 id=`user-storage-v1`、completed_at=验证完成时间、backup_path=本次备份目录；未完成不写完成标记。无需执行 DDL，回滚仅在反向迁移成功后删除该版本记录，原业务记录与关系保持。

无新增或修改 Redis key、类型、值、TTL；目录不使用 Redis 缓存。媒体认证复用以下既有结构：

| Key（示例） | 类型与值 | TTL、读写与失效 |
| --- | --- | --- |
| `videodream:session:{sid}`（`videodream:session:abc123`） | Hash：uid 用户 ID、digest 刷新凭证 SHA-256、version 账号认证版本，均为字符串 | 最长 604800 秒；登录创建，业务/媒体认证读取，刷新原子轮换 digest 但不延长会话；退出删除，停用及密码重置批量撤销 |
| `videodream:user-sessions:{uid}`（`videodream:user-sessions:28`） | Set：该用户会话 ID | 沿用登录时设置/更新 604800 秒规则；登录加入，登出移除，批量撤销读取并删除 |

媒体 JWT 的期限不超过 Redis 会话剩余 TTL，不单独缓存授权；账号状态、角色、版本和资源归属以 MySQL 当前数据为准。会话不存在或版本不一致拒绝读取，Redis 故障返回 503，丢失会话后重新登录。解析、预览、Worker 心跳和限流 key 全部沿用原规则，无 key 转换或额外缓存一致性迁移。

## 16. 交付验证

验证两个普通用户和管理员的目录隔离、同名内容、跨用户访问、管理员上传与删除归属、媒体 Cookie 的读取白名单和独立受众、HEAD/Range/附件响应、刷新期限与撤销、Redis 故障、迁移冲突/中断/重复执行和回滚保留新增文件。运行后端测试、前端测试和生产构建。修改后按原端口重启本地前端 5174、后端 8001；云端保持内部 8001 与现有 HTTPS 入口。云端发布前离线备份迁移，发布后补真实手机和桌面验证。

### 16.1 本地执行记录（2026-10-21）

- **迁移完成**：停止本项目 8001、8002 两个后端实例后，执行离线 plan/apply。335 个文件、856306581 字节全部迁入 `downloads/users/1/`，逐文件 SHA-256 校验通过；缺失、冲突和未归属文件均为 0，原文件 URL 保持不变。现有本地文件均归用户 1，其他用户的隔离通过独立测试数据验证。
- **清单与备份**：清单为 `.run/storage-migrations/local-20261021-final.json`，状态 `complete`；备份目录为 `.run/storage-migrations/local-20261021-final.backup-1792549671215897000/`，包含 `database.sql`、`downloads.zip`，ZIP 完整性校验通过。MySQL 已写入 `user-storage-v1` 完成标记，迁移中回读清单已移除。备份属于敏感本地运维资料，不纳入版本管理。
- **服务与响应**：按原端口恢复前端 5174、后端 8001、原有额外后端 8002；三个入口的健康检查通过。经前端代理及直连验证媒体 Cookie 读取、HEAD、32 字节 Range 206、越界 416、附件下载；匿名访问、无效 Bearer、仅媒体 Cookie 访问业务接口和已撤销会话均返回 401。
- **浏览器播放**：独立 Chrome 测试上下文禁用 Service Worker、Web Locks、BroadcastChannel，页面刷新会话成功并获得 HttpOnly 媒体 Cookie；迁移后的存量 M4A 可实际播放、跳转至第 5 秒，媒体请求不带 Bearer，返回 206，无页面脚本错误。临时验证会话已清理，不修改现有用户密码。
- **自动验证**：本次后端相关范围 309 项通过，后续定向复验 45 项通过；最新前端 51 项测试和生产构建通过。后端验证使用独立、仅开放 Unix socket 的临时 MySQL，验证后已关闭。本记录不将工作区内同时进行的场景歌单 Agent 改动计为完整全量验收通过。
- **该轮交付边界**：本地实现与迁移已完成；当时未执行 git commit、git push 或云端发布。后续仓库交付见第 16.2 节。云端须独立执行第 14 节离线迁移和部署验收，真实手机与生产 HTTPS 环境仍待验证。

### 16.2 仓库合并与发布前验证（2026-10-21）

按用户指令将本地代码、测试与相关规格纳入仓库交付，同时合并远端 main 已发布的视频预览、平台权限、移动端界面与场景歌单 Agent 更新。合并保留来源权限和请求路由隔离，媒体 Cookie 不扩大既有平台权限。新传输引擎的成品、任务中间文件和可续传分段分别接入用户 `files/`、`tmp/`、`tmp/transfers/`；旧共享续传分段只按原期限清理。预览保留音视频准备、无声提示与缓存复用，认证恢复及源回退共用一次自动恢复额度。新版代理预览的 HEAD 保留 Range／If-Range 并及时释放上游连接。

合并后后端全量 527 项、前端 91 项测试及生产构建通过；前端 5174、后端 8001／8002 已按原端口重启，存量 M4A 的 Cookie 读取、HEAD、Range 206、附件下载、匿名拒绝与新版 Worker 响应通过实际服务验证。测试使用隔离数据库。环境文件、密钥、下载数据、运行目录、数据库／文件备份、依赖、构建产物与编辑器配置不提交。

本轮不增加 MySQL 表／字段／索引／关系或 Redis key／类型／TTL，继续采用第 15 节数据契约及第 14 节迁移回滚。提交或推送仓库不会更新云端运行镜像；服务器首次部署新版时必须停服备份并执行目录迁移，再构建、重建应用及验证真实手机。

## 17. 公开注册与图片验证码（2026-10-21）

### 17.1 功能与接口

本机现有 `system_settings.id=1` 的 `config.registration_enabled` 已设为 JSON 布尔值 `true`，并在现有审计表记录该次配置操作。此为当时的当前实例配置。2026-10-23 用户进一步要求注册默认开启，具体默认值与新环境初始化规则见下文；不自动修改已有云端配置。

登录与注册仍使用用户名和密码，增加 4 位图片验证码，排除 I、L、O、0、1 等易混字符，不区分大小写。验证码有效期 120 秒，可点击图片换图；提交校验一次即消耗，不论答案、用途或随后密码是否正确。登录失败后自动获取新图，切换登录／注册时按新用途取图，网络失败显示重试按钮；加载期间禁止提交。前端用请求序号丢弃已切换页面或用途的旧响应。旧图换下后最多保留至原 120 秒期限，不延长有效期。

`GET /api/auth/captcha?purpose=login|register` 为公开入口，返回 `captcha_id`、PNG data URL `image` 和 `expires_in=120`，响应 `Cache-Control: no-store`。不返回验证码明文或摘要。每 IP 每分钟最多获取 30 次，超额返回 429 与 `Retry-After: 60`。注册关闭时不发放注册验证码。使用项目已有 Pillow，无新增依赖，验证码图片不落盘。

`POST /api/auth/login`、`POST /api/auth/register` 在原字段基础上强制要求 `captcha_id`（32 位 URL 安全随机标识）、`captcha_code`（4 位字母数字），缺少或格式错误返回 422；错误、过期、用途不符、重放返回 400。服务器先按原规则校验请求来源和限流，再原子消耗验证码，再校验密码或创建用户。Redis 不可用返回 503。原有密码错误限流继续执行，验证码不可绕过后端校验。

注册继续只创建普通用户；201 响应直接返回与登录一致的访问令牌、期限、公开用户信息，并设置刷新及媒体 HttpOnly Cookie。前端直接接受该响应，不再额外调用密码登录。创建会话前锁定账号行，复核账号状态、密码摘要和认证版本，防止停用或重置密码与创建会话之间的竞态。管理员创建账号及本地初始化不使用公开注册验证码。

网页获取验证码使用与现有认证状态请求相同的同源 GET；安卓登录、注册继续通过原生认证桥透传 JSON，直接接收注册返回的会话，无需新增原生接口或允许列表项。

兼容工作区现有安卓适配：安卓连接尚未更新的后端时，若取图接口返回 401／404，允许使用该旧服务已有的密码登录；网页版仍要求验证码。旧服务注册仅返回 `ok` 时，安卓沿用原自动密码登录；新服务注册直接接受会话，不重复校验已消耗的验证码。此兼容不会绕过新后端的强制验证码校验，新后端始终要求完整验证码字段。

### 17.2 MySQL 完整数据契约

无新增或修改表、字段、索引、外键及用户关联。验证码不持久化到 MySQL，注册继续使用现有 `users`、`music_profile` 与审计规则。修改注册开关只更新已有配置 JSON，完整表结构为：

```sql
CREATE TABLE IF NOT EXISTS system_settings (
  id INT PRIMARY KEY,
  config MEDIUMTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

只有主键 `id`，无其他索引或外键，配置固定行为 `id=1`。更新时仅设置 `config.registration_enabled=true`，保留其余 AI 与业务配置。配置操作、注册及登录继续使用已有审计表，完整结构为：

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  actor_id BIGINT,
  action VARCHAR(80) NOT NULL,
  target VARCHAR(255) NOT NULL,
  result VARCHAR(40) NOT NULL,
  ip VARCHAR(64) NOT NULL,
  created_at DOUBLE NOT NULL,
  INDEX audit_actor (actor_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`actor_id` 逻辑关联用户，允许 NULL，无物理外键；本次本机运维启用操作使用 NULL 操作者、`settings_update`、目标 `registration_enabled=true`、来源 `local`，不伪造管理员会话。注册、登录记录正常归属实际用户，不记录密码、验证码、验证码摘要或访问凭证。

### 17.3 Redis 完整数据契约

| Key 模板与示例 | 类型、完整值结构 | TTL、读写、失效与一致性 |
| --- | --- | --- |
| `videodream:captcha:{id}`，如 `videodream:captcha:K8pQ2Z7vN4mR6sT9wX3yA5bC1dE0fG2h` | String，JSON `{"digest":"64位十六进制 HMAC-SHA256 摘要","purpose":"login或register"}` | 120 秒；发图时 SET 并同时设置 EX；摘要绑定随机 ID、用途和大写答案，以服务端认证密钥做 HMAC；不存明文。校验以 Lua 原子 GET＋DEL 消耗，再核对用途和摘要，正确／错误／用途不符均不可重用；过期自动清理，换图不延长旧图 TTL |
| `videodream:rate:{sha256}`，如 `videodream:rate:9f…` | 复用既有 String 整数计数；新增计数输入为 `captcha-ip:{客户端IP}` 的 SHA-256 | 60 秒，最多 30 次；Lua 原子 INCR，首写设置 EXPIRE，不滑动延长。登录与注册发图共用此 IP 配额；旧登录 900 秒、注册 3600 秒计数保持原规则 |

不使用进程内缓存判断验证码有效性，多个后端实例共享同一个 Redis 前缀并使用同一认证密钥；原子消耗保证同一验证码并发提交最多成功一次。Redis 不可用拒绝发图及登录／注册。原有会话 Hash、用户会话 Set 及其 TTL、撤销和刷新规则继续沿用第 15 节。

### 17.4 兼容、迁移、回滚与验收

无需 DDL 或数据回填，已有用户和已登录会话兼容；刷新、退出、媒体 Cookie 无需验证码。旧登录／注册客户端未提交验证码会返回 422，发布时需同步更新前端及安卓内嵌页面资源。当前工作区保留现有安卓适配，生产 HTTPS 与真实安卓设备验证需在对应环境进行。

回滚时同步恢复前后端，新增验证码及限流 key 等待原 TTL 自动清理，无需扫描删除会话；注册响应接口与前端必须一并恢复。关闭注册可通过后台或仅将已有 JSON 字段改回 `false`，不删除本轮已注册用户。正式发布应先保存当前配置，保留现有数据迁移和备份。

验收覆盖 PNG 内容可解码与不缓存、摘要不含明文、缺字段拦截、大小写兼容、错误／过期／用途不符／重复和并发重放、密码错误后验证码失效、取图限流、CSRF、Redis 故障、关闭注册、注册成功自动登录与刷新。测试使用隔离数据库及 Redis 前缀；生产构建通过后按原端口重启前端 5174、后端 8001／8002，并验证实际入口。

### 17.5 本地执行结果

- 隔离环境下账号与歌单 Agent 相关后端测试 105 项通过，覆盖本节验证码及自动登录规则；首次运行受业务账号无建库权限影响，随后使用独立临时 MySQL、仅本地 Unix socket 完成验证，现有业务库未用于集成测试。
- 前端现有 130 项测试及生产构建通过；直接执行认证模块另验证新安卓后端注册直接接受会话、旧安卓后端注册后登录、网页版拒绝旧取图接口、安卓遇到 503 不降级等 4 个兼容场景。浏览器确认登录、注册和点击换图；320 像素宽页面无横向溢出，验证码图片可正常解码显示。验证截图保存在忽略目录 `.run/captcha-verification/`。
- 前端 5174、后端 8001／8002 已按原端口重启，三个入口均验证健康检查、公开注册为 true、验证码 PNG 与 no-store、缺验证码返回 422、错误验证码返回 400 且 Redis 条目被消耗。
- 本机功能已启用；生产环境及真实安卓设备仍需在对应部署中验证。


## 设备会话归属（2026-10-22）

新登录接收 X-VideoDream-Device（UUIDv4）和 X-VideoDream-Client（web/android），写入既有 Redis session Hash 的 device_id/client_kind。设备是归属标签，不能代替用户认证。refresh 原子轮换 secret 并在旧会话尚未绑定时一次性登记设备，保留剩余 TTL，不能延长 7 天上限；已绑定设备与请求头冲突拒绝。媒体读取从有效 session 恢复设备，无需 URL 携带令牌。退出、禁用和密码重置仍按既有规则撤销会话。

MySQL resources 新增 device_id CHAR(36) NULL 和 resource_device(user_id,kind,device_id,created_at) 索引；旧数据保留 NULL。设备任务读取、分页/总数、删除及完成回调均按认证会话归属校验，超级管理员的个人设备页也不能混入其他设备。music_server_assets 新表、完整 SQL/FK 与 Redis key 模板、值、TTL、迁移备份和兼容回滚详见[统一下载规格](features/unified-download-delivery.md)，此处无额外独立表或 key。回滚保留字段及资产数据，不删除用户文件，不将历史记录重分配给当前设备。


### 注册默认开启（2026-10-23）

- `DEFAULT_SETTINGS.registration_enabled` 和管理员设置 DTO 默认值改为 `true`，已有配置显式保存的开关仍优先；本机已保存为 `true`，无需数据库更新。
- 已执行的 Flyway V1 及其部署快照不改写。新部署在 `scripts/initialize-accounts.py` 首次设置管理员密码时，在同一事务将 `system_settings.config.registration_enabled` 设置为 JSON 布尔值 `true`；通过锁定管理员行及 `!uninitialized` 标记识别首次初始化。再次设置管理员密码不改注册开关。
- 无 MySQL 表、字段、索引或外键变更；仅首次初始化更新现有 JSON 字段，完整结构沿用第 17 节。无 Redis Key、类型、值结构或 TTL 变更，验证码、注册限流、普通用户角色与鉴权规则沿用现状。
- 回滚可通过管理员设置关闭公开注册；回退代码不删除已注册用户，也不覆盖已有显式开关。


## 18. 用户雪花主键与账号展示（2026-10-26）

### 18.1 身份与生成规则

直接使用雪花算法生成 users.id 主键，旧账号同步换号，现有用户名、密码摘要、角色、好友/消息、音乐/文件归属保持其对应关系。个人资料展示登录账号、用户 ID 和复制账号按钮；好友页顶部展示可复制的登录账号，昵称仅用于展示，好友搜索仍按登录账号或至少两个字符的前缀。

雪花 ID 为正的有符号 63 位整数，布局是 41 位毫秒时间差、10 位节点、12 位同毫秒序列，纪元为 2020-01-01 00:00:00 UTC。数据库使用 BIGINT；HTTP、WebSocket 和前端以十进制字符串表示，禁止 Number/parseInt 转换。计数、播放位置等普通数值不改变类型。服务端 Python 与 MySQL 使用整数计算，不能使用浮点数拼装 ID。

VIDEODREAM_USER_ID_WORKER 默认 0，运行节点可配置 0–1022，节点 1023 保留给旧账号迁移。分配 ID 在创建用户的同一事务中锁定 user_id_generator_state 的节点行，锁后读取数据库 CURRENT_TIMESTAMP(3)，再更新毫秒与序列。同一数据库上同节点多实例共享这行锁；进程重启保留状态。时钟回退、4096 个序列用尽或 41 位时间越界时拒绝分配，不复用 ID，不回退到自增或随机数。跨数据库独立发号不保证唯一，不能共享一个账号命名空间。

### 18.2 MySQL 完整结构与索引

Flyway V6 新建 user_id_generator_state、user_id_migration_map。users.id 取消 AUTO_INCREMENT；music_profile.id、music_theme.id 由 INT 扩为 BIGINT；8 个个人音乐表的 user_id 去掉默认 1。账号创建显式写入雪花 ID，管理员初始化按账号/角色查找，不依赖 id=1。完整最终表结构如下（既有索引及外键保留）：

```sql
CREATE TABLE `users` (
  `id` bigint NOT NULL,
  `username` varchar(40) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` varchar(16) NOT NULL DEFAULT 'user',
  `status` varchar(16) NOT NULL DEFAULT 'active',
  `must_change_password` tinyint(1) NOT NULL DEFAULT '0',
  `auth_version` int NOT NULL DEFAULT '0',
  `created_at` double NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_profile` (
  `id` bigint NOT NULL,
  `nickname` varchar(40) NOT NULL,
  `bio` varchar(200) NOT NULL DEFAULT '',
  `avatar` mediumtext,
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `music_profile_owner` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_theme` (
  `id` bigint NOT NULL,
  `config` text NOT NULL,
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `music_theme_owner` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_tracks` (
  `id` char(64) NOT NULL,
  `provider` varchar(12) NOT NULL,
  `identifier` varchar(255) NOT NULL,
  `title` varchar(255) NOT NULL,
  `artist` varchar(255) NOT NULL,
  `webpage_url` text NOT NULL,
  `thumbnail` text,
  `duration` double DEFAULT NULL,
  `access` varchar(100) NOT NULL,
  `custom_thumbnail` text,
  `cover_updated_at` double NOT NULL DEFAULT '0',
  `cover_checked_at` double NOT NULL DEFAULT '0',
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `music_tracks_owner` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_playlists` (
  `id` char(24) NOT NULL,
  `name` varchar(80) NOT NULL,
  `description` varchar(300) NOT NULL,
  `created_at` double NOT NULL,
  `thumbnail` text,
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `music_playlists_owner` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_favorites` (
  `track_id` char(64) NOT NULL,
  `created_at` double NOT NULL,
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`track_id`),
  KEY `music_favorites_owner` (`user_id`),
  CONSTRAINT `music_favorites_ibfk_1` FOREIGN KEY (`track_id`) REFERENCES `music_tracks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_history` (
  `track_id` char(64) NOT NULL,
  `played_at` double NOT NULL,
  `play_count` int NOT NULL DEFAULT '1',
  `preview` tinyint(1) NOT NULL DEFAULT '0',
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`track_id`),
  KEY `music_history_owner` (`user_id`),
  CONSTRAINT `music_history_ibfk_1` FOREIGN KEY (`track_id`) REFERENCES `music_tracks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_downloads` (
  `id` char(24) NOT NULL,
  `track_id` char(64) NOT NULL,
  `filename` varchar(255) NOT NULL,
  `created_at` double NOT NULL,
  `imported` tinyint(1) NOT NULL DEFAULT '0',
  `file_hash` char(64) DEFAULT NULL,
  `file_size` bigint DEFAULT NULL,
  `file_mtime` bigint DEFAULT NULL,
  `file_available` tinyint(1) NOT NULL DEFAULT '1',
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `filename` (`filename`),
  KEY `track_id` (`track_id`),
  KEY `file_hash_index` (`file_hash`),
  KEY `music_downloads_owner` (`user_id`),
  CONSTRAINT `music_downloads_ibfk_1` FOREIGN KEY (`track_id`) REFERENCES `music_tracks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `music_lyrics` (
  `track_id` char(64) NOT NULL,
  `text` mediumtext NOT NULL,
  `source` varchar(80) NOT NULL,
  `format` varchar(8) NOT NULL,
  `signature` text NOT NULL,
  `checked_at` double NOT NULL,
  `manual` tinyint(1) NOT NULL DEFAULT '0',
  `updated_at` double NOT NULL,
  `user_id` bigint NOT NULL,
  PRIMARY KEY (`track_id`),
  KEY `music_lyrics_owner` (`user_id`),
  CONSTRAINT `music_lyrics_ibfk_1` FOREIGN KEY (`track_id`) REFERENCES `music_tracks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE user_id_generator_state (
  worker_id SMALLINT UNSIGNED NOT NULL PRIMARY KEY,
  last_timestamp_ms BIGINT UNSIGNED NOT NULL,
  sequence SMALLINT UNSIGNED NOT NULL,
  CONSTRAINT user_id_worker_range CHECK (worker_id <= 1023),
  CONSTRAINT user_id_sequence_range CHECK (sequence <= 4095)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE user_id_migration_map (
  old_id BIGINT NOT NULL PRIMARY KEY,
  new_id BIGINT NOT NULL UNIQUE,
  created_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

```

生成器表以 worker_id 为主键，无外键，限制节点和序列范围；状态与用户 INSERT 同事务提交。迁移对照表以 old_id 为主键，new_id 有唯一索引，无用户外键，保留换号对照供恢复与浏览器设备索引迁移，不作为账号访问别名。旧 ID 不能用于访问新账号。

### 18.3 全部关系和数据迁移

离线阶段按 information_schema 收集全部 user_id 列和声明指向 users.id 的外键，同时处理审计 actor_id、资料/主题镜像主键。当前范围如下；以后新增的 user_id 或用户外键会被同一迁移器识别：

| 表 | 用户身份列 |
|---|---|
| `audit_logs` | `actor_id` |
| `music_discovery_batches` | `user_id` |
| `music_discovery_candidates` | `user_id` |
| `music_discovery_daily_usage` | `user_id` |
| `music_discovery_events` | `user_id` |
| `music_discovery_feedback` | `user_id` |
| `music_discovery_feeds` | `user_id` |
| `music_discovery_identities` | `user_id` |
| `music_discovery_profile_runs` | `user_id` |
| `music_discovery_profile_versions` | `user_id` |
| `music_discovery_profiles` | `user_id` |
| `music_downloads` | `user_id` |
| `music_favorites` | `user_id` |
| `music_history` | `user_id` |
| `music_lyrics` | `user_id` |
| `music_playlists` | `user_id` |
| `music_profile` | `id`, `user_id` |
| `music_recommendation_runs` | `user_id` |
| `music_server_assets` | `user_id` |
| `music_theme` | `id`, `user_id` |
| `music_track_features` | `user_id` |
| `music_tracks` | `user_id` |
| `resources` | `user_id` |
| `social_assets` | `owner_id` |
| `social_conversation_members` | `user_id` |
| `social_conversations` | `owner_id` |
| `social_duet_invites` | `claimed_by`, `invitee_id`, `inviter_id` |
| `social_karaoke_songs` | `creator_id` |
| `social_messages` | `sender_id` |
| `social_notifications` | `user_id` |
| `social_performance_parts` | `user_id` |
| `social_performances` | `owner_id` |
| `social_playlist_items` | `contributor_id` |
| `social_playlist_members` | `user_id` |
| `social_playlists` | `owner_id` |
| `social_relations` | `target_user_id`, `user_id` |
| `social_reports` | `handler_id`, `reporter_id` |
| `social_room_invites` | `invitee_id`, `inviter_id` |
| `social_room_members` | `user_id` |
| `social_room_queue` | `partner_id`, `requester_id` |
| `social_rooms` | `owner_id` |
| `social_rule_library` | `user_id` |
| `social_rules` | `author_id` |
| `social_shares` | `owner_id` |
| `social_user_settings` | `user_id` |
| `users` | `id` |
| `video_mind_maps` | `user_id` |
| `video_parse_records` | `user_id` |
| `video_recent_links` | `user_id` |
| `video_summaries` | `user_id` |
| `video_view_states` | `user_id` |

事务内更新用户主键及上列关联、审计账号目标、JSON 中的身份字段和 recipients/singers 等用户数组、私聊 direct_key、按用户派生的 video_view/video_search 键；播放位置、歌曲 ID、消息序号等业务数字保持原义。旧账号的 ID 对照先持久化，数据更新及最终完成标记在一个事务中提交，校验旧 ID 残留与每条用户外键后才开放服务。迁移期间外键检查只在独立迁移连接上临时关闭，完成或异常时恢复；应用连接不能跳过外键检查。

用户目录 downloads/users/{old_id}、downloads/_tmp/{old_id}、.run/browser-profiles/{old_id} 按清单重命名为新 ID。清单先持久化，检查根目录、符号链接、目标冲突和文件清单指纹，禁止覆盖或合并。异常回滚事务并逆向恢复已移动目录；进程中断后使用同一对照与清单幂等重试。Flyway 只负责 DDL，scripts/database.py migrate 还负责此数据/目录阶段；缺少 users-snowflake-v1 完成标记时 schema gate 拒绝启动。

登录用户信息增加 legacy_id 字符串，仅用于本人浏览器的旧设备音乐索引与下载记录复制；旧 IndexedDB 条目、文件句柄和 localStorage 记录保留供回滚，不提升原文件权限，不复制其他账号的设备音乐。新用户 legacy_id 为 null。

### 18.4 Redis 数据契约与一致性

雪花生成不使用 Redis，不新增 Redis key、类型、值结构或 TTL。现有 key 中 uid 替换为雪花十进制字符串，例如 videodream:user-sessions:903456789012345679（Set，会话索引，TTL 7 天，成员为会话 ID）、videodream:session:{sid}（Hash，uid 字段为相同十进制文本，既有会话 TTL 7 天）、videodream:social:user-presence:903456789012345679（ZSet，连接 ID→心跳时间，TTL 90 秒）和 videodream:download:{kind}:903456789012345679:{identity}（现有下载缓存类型、值结构与 TTL 保留）。其余会话、下载、视频及社交 key 的完整结构与 TTL 分别沿用本规格与社交规格原章节。

离线切换清理本项目 videodream:session:*、user-sessions:*、social:*、video:*、download:* 命名空间，撤销旧会话、票据和身份缓存；不清理其他应用前缀、登录限流和验证码。用户重新登录后按新 ID 重建缓存；旧 Access/Refresh/媒体凭证不能继续访问。Redis 故障使迁移停止，应用保持停服，不能带着旧身份缓存上线。MySQL 是账号与关系的权威数据，Outbox 继续负责重建及时通知。

### 18.5 发布、兼容和回滚

1. 停止后端与所有后台写入，保存匹配 V5 的代码/镜像、环境和文件备份。scripts/database.py migrate 会在 V6 DDL 前备份完整 MySQL（含 Flyway 历史），再执行结构和离线数据阶段。
2. 已接管库直接 migrate → validate → check；未接管旧库先按 V1 preflight/baseline。V1–V5 不改校验和。对照表与 .run/user-id-migrations 目录清单必须备份保留。
3. 使用匹配 V6 的前后端，验证全部账号数与角色、管理员初始化状态、资料/主题、好友/群聊、歌曲和文件归属、旧文件可读、完整 ID 展示和复制，旧会话应失效。
4. 回滚时停服，恢复包含 Flyway 历史的匹配 V5 完整数据库备份；按清单验证 target 指纹和 source 不存在后逆向重命名目录，再恢复 V5 应用和原配置。浏览器保留的旧设备记录继续使用。禁止只删新表、只改回自增或自动 repair。
5. 如换号后已有新数据，先导出新账号、业务与媒体，选择保留 V6 的兼容应用或编写前向补偿迁移；不能把雪花 ID 截断回 INT。所有恢复完成后重新登录并核对归属，才能开放服务。

### 18.6 本地实施验证（2026-10-26）

本地库已完成 V5 → V6，保留原有两个账号及角色；原 admin 主键 1 已迁移为雪花 ID，另一原账号同步迁移。迁移检查未发现受迁移字段中的旧 ID 残留，Flyway validate 与应用 schema check 均通过。迁移前已在 `.run/database-backups/` 保存完整数据库 SQL 和对应用户目录的 ZIP 备份，目录清单保存在 `.run/user-id-migrations/`。后端已按原端口 8001 重启，经统一地址 `http://127.0.0.1:5174/api/health` 验证返回 200。旧会话已失效，需重新登录。

雪花专项 6 项、社交与 Flyway 回归 25 项、账号相关回归 6 项通过；前端 179 项测试与类型检查、生产构建通过。专项覆盖旧用户关联及文件迁移、并发唯一性、时钟回退、JSON 长整数精度和设备索引兼容。使用可清理的临时账号实际登录，个人资料显示的 18 位 ID 与数据库精确一致；390×844 手机视口无横向溢出，账号复制内容正确。临时账号验证后清理。

以上为相关范围的验证；此前全量后端未通过的历史结果仍见社交规格第 18 节，不能据此视为全量通过。真实 LiveKit 双设备音频仍待配置后验收。

2026-10-26 提交前全量检查：前端重新运行 179 项测试、类型检查和生产构建均通过；后端全量 718 项运行完成，53 项失败、113 项错误，未通过。已确认错误包含旧测试仍写死管理员 ID 1、旧 Redis 的多字段 HSET 兼容、Windows 历史目录迁移和 WinError 10048 临时端口耗尽；另外存在资源、权限和任务时序断言失败，尚未逐项修复。专项通过不能替代该全量结果。V5/V6 部署 SQL 与 Flyway 副本一致，Python 编译与提交空白检查通过。
