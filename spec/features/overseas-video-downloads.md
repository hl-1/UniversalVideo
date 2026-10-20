# YouTube / Pornhub 下载与统一超级管理员权限

日期：2026-10-20。功能交付地址 `http://127.0.0.1:5174/`，前端 5174、后端 8001。

## 功能范围

YouTube、Pornhub 的全部功能及关联资源统一要求 `super_admin`：解析、下载、在线预览、音轨提取与播放、字幕、AI 总结、思维导图、去水印、封面、文件、任务、历史和最近链接。普通用户不显示两站入口；直接调用接口、历史 ID、旧文件链接、Range 请求或缓存命中仍需后端权限校验。国内平台遵循[国内下载规格](domestic-video-downloads.md)。

两站共用现有输入框和下载引擎，支持平台区域另设超级管理员可见的海外入口及“检查代理连接”。本次不增加视频批次队列 UI，不部署云服务；后续云主机可使用同一组代理配置。

## 平台识别与授权

- YouTube：`youtube.com`、`youtu.be`、`youtube-nocookie.com`、`googlevideo.com`、`ytimg.com`、`youtubei.googleapis.com`、`youtube.googleapis.com`。
- Pornhub：`pornhub.com`、`pornhub.org`、`pornhubpremium.com`、`phncdn.com`、`phprcdn.com`。
- 采用 hostname 精确匹配或点分子域匹配，覆盖 `cn.pornhub.com`、移动站、短链及相关 CDN；拒绝 `youtube.com.evil.example` 等误判。
- 公共权限层在请求输入、网络请求/重定向、资源读取、缓存读取和 JSON 输出执行来源权限校验。所有 HTTP API 先经过账号认证；内部无 HTTP 身份的单元测试和维护调用不等于匿名 HTTP 访问权限。
- 新文件继承 `source_url`、父任务/解析记录和限制标记。文件再次入库不能清除已有 `super_admin` 限制。旧文件通过文件名中的任务 ID、解析记录、父总结、源文件以及历史封面关联查找来源；限制不因资源软删除而解除。无法证明来源的普通用户手动导入本地文件保持原文件权限，不能依赖文件内容推断来源。
- 下载、总结、导图、预览、去水印运行中按检查点重新读取账号状态/角色，约每秒一次；停用、降级或取消后终止后续处理，不发布未完成成品。已经传到客户端的字节无法撤回。
- 历史与个人资源列表在分页前过滤，total 与可见结果一致；最近链接、音乐列表和封面过滤限制项，音乐统计排除限制歌曲。普通用户不能通过已有歌曲 ID 操作受限歌曲的歌词、封面或播放记录。

## 网络路径

| 环节 | YouTube / Pornhub | 国内平台 |
| --- | --- | --- |
| 页面、API、提取器、HTTP 跳转 | 显式海外代理，跳转逐次鉴权和公网校验 | 显式直连 |
| 临时 Chromium | 同一海外代理，HTTP 代理支持独立凭据 | `direct://` |
| 视频/音频 HTTP、HLS/DASH 清单、密钥、分片 | 继承原页面代理配置，保留签名参数及媒体请求头 | 原页面直连 |
| 字幕、视频封面、关联音频封面、播放代理 | 根据来源平台/Referer 使用同一代理 | 按国内域名/Referer 直连；未统一的国内专用字幕 API 见国内规格 |
| FFmpeg 合并视频 | 输入为本地下载文件，合并不联网 | 相同 |
| 浏览器另存为 | 从应用受鉴权文件接口保存 | 相同 |

配置只影响本应用，不更改 Windows 系统代理或 VPN。透明 TUN 仍由 VPN 软件控制，国内流量需要在客户端配置 DIRECT 规则。海外代理不可用时明确失败，不自动直连。

```dotenv
VIDEODREAM_OVERSEAS_ENABLED=true
VIDEODREAM_OVERSEAS_PROXY_URL=http://127.0.0.1:7890
VIDEODREAM_OVERSEAS_ROUTE_REVISION=1
```

优先级：进程环境变量、`.env.universal-video.local`、`.env`、默认值。支持 `http`、`https`、`socks5h`，必须显式提供端口。SOCKS 支持解析/下载的远端 DNS；Chromium 备用解析要求 HTTP/HTTPS 端口，不兼容时返回明确错误。建议本机 VPN 使用混合端口或 HTTP 端口；云主机可配置自己的出口代理地址及凭据。配置文件按现有加载规则修改后重启后端。每个任务固定路由配置，后续任务采用新配置。

路由 revision 为代理 URL（含凭据）及配置版本的 SHA-256 前 20 位，用于缓存/续传隔离；响应、数据库和错误日志不保存明文代理凭据。管理员检测接口也不返回代理地址。

原生分片线程使用下载器在任务开始时保存的代理配置，不依赖线程继承 ContextVar。VPN 客户端在相同端口切换节点时，应同步增加 `VIDEODREAM_OVERSEAS_ROUTE_REVISION` 并重启后端，以隔离此前出口的签名和缓存；仅代理 URL 的摘要无法感知客户端内部节点变化。

## 下载和格式选择

复用国内引擎：HTTP 文件超过 16 MiB 且正确返回 Range、总长度和强 ETag/可靠 Last-Modified 时最多四连接；不满足则完整单连接。每段核对长度、范围、标识；断流重试、24 小时本地续传，内容变化停止拼接。HLS/DASH 采用原生清单和最多四个分片并发，失败重试三次，缺片报错，随后本地合并。两条音视频流依次下载，单任务正文并发最多四路，执行池最多两个下载任务。

保留原媒体 URL、查询参数、Cookie 和提取器生成的请求头。源站签名过期时最多重新解析一次。失败和取消不发布不完整文件。下载信息显示路由、协议、配置的并发上限、有效字节速度、剩余时间；HTTP 回退单连接时显示原因。并发上限不代表每一时刻都有四个活动连接，也不保证四倍速度。

Pornhub 的 `hls-3398` 一类格式 ID 含动态码率，下一次提取可能改变，不能作为稳定选择。新解析结果使用精确高度、容器及优先协议选择式，例如：

```text
best[height=1080][ext=mp4][protocol^=m3u8]/best[height=1080][ext=mp4]
```

旧 HLS ID 从当前缓存或所属解析记录恢复分辨率，再选择相同高度/容器；恢复失败要求重新解析。缺少原清晰度时不自动降级为低清。YouTube 保留提取器默认客户端选择，安装与 `yt-dlp==2026.7.4` 匹配的 EJS（默认依赖包含 `yt-dlp-ejs==0.8.0`）并使用项目 Node 运行时。平台登录、反机器人、地区、PO Token、删除或 DRM 限制仍会明确报错，不承诺所有公开页面均可下载。

## 管理员接口

- `GET /api/admin/video-network`：返回 enabled、scheme、revision 或安全错误说明。
- `POST /api/admin/video-network/check`：固定请求两站 `robots.txt`，返回站点状态、连接耗时和安全错误；连接成功与实际视频可下载分别表述。每账号每分钟最多三次。请求不接收任意目标 URL，也不提供开放代理。
- 两接口均通过当前账号的 `super_admin` 检查，普通用户返回 403。

## MySQL 数据明细与兼容

**无新增或修改表、列、类型、索引、外键或关系，无 DDL 迁移。** 复用 `users.role/status/auth_version`、`resources.payload`、`video_parse_records.snapshot`、`video_summaries.content`、`video_mind_maps.content`、`video_view_states.view_state` 及现有音乐关联表。完整原结构与关系仍以账号、视频持久化规格及现有 schema 为准，本次没有另建业务表的必要。

`resources` 仍以 `(kind, resource_key)` 为主键、`user_id` 记录归属；payload 保持 JSON 文本，允许按需新增以下可选内容：

```json
{
  "filename": "abcdef123456.video.mp4",
  "size": 123456,
  "source_url": "https://www.youtube.com/watch?v=example",
  "source_platform": "youtube",
  "access_scope": "super_admin",
  "source_task_id": "abcdef123456",
  "parse_record_id": "012345abcdef",
  "route_profile": "overseas-proxy",
  "route_revision": "20位配置摘要"
}
```

`source_filename` 用于去水印/本地音频封面，`summary_task_id` 用于总结导出，`source_task_id` 用于下载成品；均通过现有资源键查父资源，不新增数据库外键。任务 snapshot 仍保留原 URL/解析记录，下载任务附带路由摘要，预览短期来源保存在 Redis。不同资源只保存实际需要的字段，不写假关联。歌曲继续使用现有 `webpage_url` 和下载文件关联。

旧 JSON 可读，不强制批量回填。新授权逻辑可追溯旧关联，读取缓存不会绕过持久化资源授权；请求内仅缓存来源判断，注册资源时清理该缓存，后续 HTTP 请求重新读取权限。

## Redis 和本地缓存明细

前缀以下以 `videodream:` 为例，实际使用账号模块的 redis_prefix。

| key 模板/示例 | 类型、值 | TTL / 读写及失效 |
| --- | --- | --- |
| `<prefix>video:parse:<uid>:<digest>`；`videodream:video:parse:1:<64位摘要>` | String / 原 ParseResponse JSON | 最长 600 秒，媒体签名更早到期时缩短；解析前鉴权，命中后再次检查结果来源；force/过期源重解析删除对应 key |
| `<prefix>video:preview:<uid>:<token>`；`videodream:video:preview:1:exampleToken` | String / 原预览 JSON，增加可选 access_scope、source_platform、route_revision | 准备阶段按签名最长 600 秒；完成的本地成品按原会话规则最长 86400 秒；删除解析记录/取消/账号清理失效，读取与 Range 访问重新检查来源和当前角色 |
| `<prefix>rate:<sha256("overseas-network:"+uid)>` | String / INCR 计数 | 60 秒；首次 INCR 设置 EXPIRE，每次管理员连接检测读取/增加；超过三次返回 429，自动到期。复用既有限流模板 |

国内 parse digest 仍为 `sha256(url)`；两站改为 `sha256(url + "\0" + route_revision)`，其中 `\0` 表示实际 NUL 分隔字节。代理地址/凭据/版本变更形成新命名空间，旧 key 自然到期。并发合并解析也纳入该 revision，不共享不同出口的临时资源。预览路由 revision 不同返回 410，要求重新准备。

本地 `downloads/.transfers/` key 加入路由 revision，保留原 24 小时清理与身份检查。已完成预览本地 `_cache` manifest 增加可选来源权限与路由摘要；复用前先检查父解析记录、来源和 revision，TTL 1800 秒、容量 512 MiB、最多 128 项，沿用现有清理机制。媒体缓存不替代权限判断。

## 实测与速度边界

2026-10-20，用户提供的 Pornhub 页面通过本机代理成功取得 240p、480p、720p、1080p 的 HTTP/HLS 格式。1080p 清单与 1 MiB 视频片段读取成功；这证明格式选择及媒体访问链路可用，不能等同完整 471.9 MiB 文件下载完成。

同一 1080p HLS 源四个片段各取 512 KiB，仅用于有界网络抽样：

| 并发 | 总抽样字节 | 墙钟耗时 | 含连接等待的吞吐 |
| --- | ---: | ---: | ---: |
| 1 路 | 2,097,152 | 24.56 秒 | 0.081 MiB/s |
| 4 路 | 2,097,152 | 13.52 秒 | 0.148 MiB/s |

该次约 1.8 倍。抽样顺序、缓存、握手和线路波动会影响结果；不是持续整文件测速，也无法据此定位限速环节或认定 VPN 整体带宽不足。

随后对相同 1080p MP4 媒体地址、相同本地代理做独立工具对照：

| 客户端 | 抽样方式 | 含连接等待的吞吐 |
| --- | --- | ---: |
| 应用 HTTP 传输 | 四个 512 KiB 范围，单连接依次读取 | 0.073 MiB/s |
| 应用 HTTP 传输 | 相同四个范围，四连接并行读取 | 0.109 MiB/s |
| curl / HTTP/1.1 | 同一地址连续读取 2 MiB | 0.122 MiB/s |
| Chromium / HTTP/2 | 同一地址，25 秒有界读取约 3.08 MiB | 0.123 MiB/s |

Windows 系统代理与应用均指向本机 7890 端口；应用提取到的 HLS、MP4 分别使用 `hv-h.phncdn.com`、`ev.phncdn.com`。同地址对照未复现用户在 Chrome 中的快速下载，无法据此判定其实际 CDN、签名、会话或出口与应用一致，也无法认定切换 HTTP/2 即可解决。用户已决定暂停进一步提速排查，保留当前实现，慢速根因尚未确认。

未来云代理需比较服务器出口到源站及服务器到用户两段带宽。目标上限约为宽带、代理可用吞吐、CDN 总限速和磁盘速度的最小值，不能承诺固定 MB/s。

## 验证和回滚

回归覆盖平台域名边界、显式代理与国内隔离、网络重定向鉴权、代理关闭/无效时拒绝直连、路由摘要隔离、两站复用视频/音频下载入口、Pornhub 动态码率 ID、普通账号的解析/字幕派生入口、历史/旧成品/去水印关联文件、缓存预览和歌曲 ID，以及运行中角色降级。

2026-10-20 提交前验证：后端 456 项测试通过，前端 74 项测试、类型检查和生产构建通过，`git diff --check` 通过。后端在原 8001 端口重启，前端 5174 页面与转发健康检查均返回 200，管理员网络接口未登录访问返回 401；普通账号返回 403 的覆盖由账号集成测试验证。

回滚无需数据库结构迁移；旧代码忽略新增 JSON 字段。若仅回滚传输引擎，必须保留公共权限检查、来源标记和管理员入口限制；不可通过回滚把两站资源重新开放给普通用户。可设置 `VIDEODREAM_OVERSEAS_ENABLED=false` 暂停海外请求，重启前等待或明确停止活动下载，清理旧解析/预览缓存。云部署另行交付。
