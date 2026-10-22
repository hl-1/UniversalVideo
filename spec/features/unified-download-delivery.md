# 统一下载、设备曲库与本站音源

## 范围与原因

覆盖本轮对话的完整需求：下载慢因与处理逻辑、视频和歌曲复用提速能力、单曲与批量下载、文件与导出、本机保存位置、云端存储去重、用户隔离，以及搜索结果隐藏已下载歌曲的下载按钮。

原流程先由服务器取得完整资源，再由用户设备从服务器读取。服务器下载完成的 100% 只代表第一段完成。服务器到源站的入站速度、服务器公网出站带宽、用户网络、浏览器写盘速度并不相同，因此不能用第一段速度推断保存到本机的速度。

服务器处理原有用途包括平台请求头和登录限制、音视频合并、格式转换、ZIP、云端播放及后续编辑。新设计保留这些能力，并让保存目标和处理阶段明确可见。

## 保存目标与交付路径

- 界面默认“选择位置并保存到本机”。支持文件选择 API 时，单文件点击下载立即打开系统保存对话框；批量歌曲先打开一次目录选择器，再逐首保存。选择器在任何解析、状态查询或媒体请求之前调用，取消后不启动下载。按本轮用户明确选择，此模式在网页显示保存进度，不进入浏览器下载记录。本机路径和文件句柄不上传服务器。
- 不支持文件选择 API 的浏览器使用原生下载管理器，保存路径由浏览器设置或浏览器自己的对话框决定，不能承诺网页能强制弹出路径选择。
- 歌曲下载仅面向当前设备。本站音源通过独立“上传到本站”入口创建；歌单和收藏只保存歌曲引用。视频资料库功能继续使用原有目标。
- 允许匿名读取、无需敏感凭据且探测通过 CORS 的单一媒体源，优先浏览器直连。浏览器实际连接失败时使用同一下载计划的认证转发地址；服务器探测成功不能保证用户网络也能直连。
- 需要平台请求头的完整 HTTP 媒体由服务器立即流式转发，缓冲有上限，不先下载完整媒体。
- 单曲音频可通过受控请求进入 FFmpeg 管道，输出原格式或所选 MP3/M4A，并校验时长。输入和输出都采用有界管道；FFmpeg 输入协议仅允许 `pipe`，不能从媒体内容中绕过网络校验发起任意请求。
- 同格式 MP3/M4A 可复制音轨，避免重复有损编码；“原格式”保留音轨编码。云端保留原格式时，已确认无视频的原音频文件直接保留字节。视频音轨的原格式提取仍可能需要重新封装。
- HLS/DASH、需要合并、无法通过管道读取的容器使用兼容处理。音频管道失败可使用同一已授权本机目标从零重试兼容处理一次，不将不同来源拼接到已写入内容后面。批量原生下载为避免浏览器接收校验失败的残缺歌曲，也按单首兼容任务先准备完整文件；旧 ZIP 接口使用单首暂存后校验再加入条目。以上临时文件按账户隔离，不作为云端资料库文件保留。
- 临时文件位于 `downloads/users/{uid}/tmp/delivery/{task_id}/{file|temp}/`。任务成功且本机写入确认后清理；失败或取消后停止处理并清理；未确认、浏览器接管和断线遗留文件由定时维护在任务终止约一小时后回收。活跃任务不按创建时间误删。
- 原有 `/api/download` 默认保持云端任务语义；新的界面通过 `/api/downloads/plan` 明确传递目标。旧任务和文件地址继续可用。

## 统一前端保存

公共实现位于 `frontend/src/shared/downloads.ts`，复用现有视频流式写盘能力。视频、歌曲、ZIP、用户文件、管理员下载、去水印结果、Markdown/JSON、歌词 LRC/TXT 与思维导图图片都接入统一保存或原生下载管理器。

网络媒体不通过完整 `response.blob()` 后才交给下载管理器。支持选择器时分块写入，每次批量写入最多 1 MiB，等待写入形成背压。浏览器内生成的图片、歌词属于已生成的小型导出内容，可直接交给统一 Blob 保存方法。

下载状态分为选择位置、准备、处理、传输、完成写入、已保存、交给浏览器、失败、取消。共享浮层在准备到写盘确认全过程保留名称和取消入口；批量队列同时展示每首保存信息。文件大小已知时展示进度条、已写入／总大小、百分比（确认前最高 99%）、平均速度及可计算的剩余时间；大小未知时展示滚动进度条、已写入大小和速度，不编造百分比或剩余时间。速度不足 1 MiB/s 时显示 KB/s，避免低速被四舍五入为 0.0 MB/s。后端准备进度单独标明“正在准备文件”，不得冒充本机保存进度。

本机建议文件名与服务端资源标识分离：`964ba09f4629.Here For It All.m4a` 下载为 `Here For It All.m4a`；仅移除项目生成的 12 位十六进制任务前缀或 `import-` 加该前缀，保留 `2026. 歌曲名.mp3` 等正常数字歌名及实际扩展名。文件选择器、下载计划、队列和服务端附件响应使用可读名称，GET/HEAD 及 X-Accel 的 `Content-Disposition` 保持一致。服务器路径、任务编号、资源 URL 与共享池关系不改名，不批量重命名已经保存到用户磁盘的文件。

歌曲界面默认 MP3，可明确选择 M4A；两种格式均在打开选择器前确定后缀，避免未知原格式写入错误扩展名。底层接口保留 `original`，适用于可按解析结果命名的原生下载或目录方式。

只有文件写入流 `close()` 成功后，才显示“已保存到你选择的位置”。原生下载管理器只能显示“已交给浏览器下载”。中断、长度不符、FFmpeg 失败或时长不符会中止写入，不能记为已下载。

通过下载计划交付的媒体，写盘关闭后还须核对 `/complete` 返回的实际 `status`。HTTP 200 不等于保存成功：服务端已失败、取消或未确认时，前端必须保留对应错误，不能忽略响应后更新歌曲成功记录。确认请求失败同样不创建成功历史；此前写入的提示须被实际错误替换，目录模式清理本次文件。

取消会中止前端请求和写入，并通知服务端取消计划；转发和 FFmpeg 定期检查取消、会话撤销和账户状态。每次停滞读取或写入保留原有 60 秒保护，长文件只要持续有进展就不受总时长 60 秒限制。

已有文件和普通 HTTP 转发保留 HEAD、单段 Range、If-Range、ETag 等语义。原生下载管理器可利用源站支持的断点能力；流式转码和 ZIP 不提供随机续传。应用自行重试从零开始，不在未确认源版本时追加字节。

## 批量歌曲

- 根据平台与歌曲 ID 去除同批重复，不按歌名去重，不将同名、现场、录音室版本或不同平台 ID 自动合并。
- 默认“保存到本机文件夹”，搜索多选、批量找歌和场景歌单均复用该入口；不自动请求或生成 ZIP。
- 点击下载先选择一个文件夹，再同时最多处理两首；每首独立解析、校验、写入及关闭，网页显示该首真实保存进度及速度。文件名不加批次序号，同名或已有文件加 ` (2)`、` (3)` 等后缀；同一队列在检查磁盘前预留名称，避免并发同名写入。失败只清理本次新建文件，成功文件保留。此方式直接写入授权目录，不进入浏览器下载记录。
- 用户仍可在搜索多选中显式选择“浏览器下载（逐首）”；不支持目录 API 时也使用此方式，并提示由浏览器管理位置与进度。此路径按顺序为每首准备并校验完整临时文件，再分别交给浏览器，不等待完整批次。保存位置由浏览器设置决定；网页不能强制开启浏览器“下载前询问保存位置”。界面保留每首“保存此歌曲”链接；临时文件到期需重试准备。
- 一首解析、获取、校验或写入失败，标记该首失败并继续后续歌曲，不将残缺文件记为成功。每首使用独立取消控制器，单曲停滞超时不会取消整个队列；用户主动停止和切换账户才取消活动与后续歌曲。迟到回调不能将旧账户状态写入新账户界面。
- 队列分别显示已保存、交给浏览器、失败、取消。仅真实写盘关闭成功更新本机历史并隐藏下载按钮；浏览器接管不能确认磁盘完成，不隐藏按钮。部分失败明确展示成功数量，重试只处理失败或取消曲目，不重新下载此前成功或已交给浏览器的歌曲。
- 批量找歌、场景歌单和搜索多选共用下载能力。保存到歌单只添加在线歌曲引用，可先于下载完成；自建歌单的数量表示条目数，不表示本机文件数，下载失败不撤回用户添加的在线引用。是否保存成功以逐首队列及本机写盘结果为准；上传到本站通过独立入口执行。
- 保留旧 ZIP 接口兼容已有调用方。曾发生音轨在输出部分字节后时长校验失败，导致 ZIP 中央目录未输出、压缩包无法解压；现在每首完全获取及校验到临时文件后才建立 ZIP 条目，单曲失败直接跳过，随后继续。使用 `ZIP_STORED`、ZIP64，单首暂存上限 1 GiB，用完关闭删除，不生成完整云端 ZIP；FFmpeg 管道与兼容任务继续采用各自原有校验。
- 旧 ZIP 存在失败歌曲时附加 UTF-8 `下载结果.txt`，列出成功／失败数量、失败歌名和不含源凭据的重试说明。全部失败时返回只有说明的有效 ZIP，成功数量为零。只有输出通道故障、磁盘故障、主动取消或会话撤销等批次级错误才终止请求，不能把已开始写入条目的输出故障当成跳过成功。浏览器接管 ZIP 不把曲目标记为本机已保存。

## 搜索与已下载状态

搜索结果仅当当前账号、当前设备索引中存在可读音频时隐藏下载入口。服务器文件、保存凭证、浏览器接管及其他设备下载均不能代替本设备文件。旧 `/api/library/download-status` 只描述历史服务器文件，界面不再把它用于本地判定。

三个入口独立：**本地音乐**仅列出本设备实际可读文件；**下载管理**显示当前设备活动任务、已保存凭证及待核实旧记录；**本站音源**只列出用户明确上传、状态为 ready 的服务器音频。同账号其他设备可在线访问本站音源，但不会因此出现本地下载标记。本站上传不自动下载到设备，本地导入不上传。

网页设备身份为随机 UUID，保存于 `videodream.device.id`。浏览器、配置文件、站点存储实例相互独立；退出及重新登录保留 UUID，清除站点数据生成新身份。不能使用 IP、硬件指纹或用户提供的任意查询参数代替设备身份。设备标识是归属标签，认证仍由有效会话负责。

网页 IndexedDB `videodream-device-library` v1 的 `tracks` store 按 `uid:device_id:provider:identifier`（无 identifier 时用 URL）保存：`key,uid,device_id,track,handle?,bytes,modified?,added_at`。只保存文件句柄和歌曲元数据；不上传文件句柄或路径。授权持久化可用时重新打开页面后核对文件大小、mtime 和读取权限；普通 File 输入只在当前页面有效。文件缺失、权限撤销或更改后移出可播放列表，提供重新定位/授权入口；本地播放失败不会静默改用网络。移出曲库只移除本设备索引，原文件保留。前台恢复时刷新实际可读状态，播放时再次验证。

`videodream.music.saved.{uid}.{device_id}` 保存逐首保存凭证，与真实文件索引独立。旧 uid-only 数据保留原文并转为待核实，绝不自动归属为当前设备已下载。切换账号取消任务、清空播放和文件引用，迟到回调不得写入另一账号。本地音乐数量只来自本设备索引。

在线平台播放使用 `progressive:true` 请求：先解析和探测可播放源，返回短时认证流式地址，支持单段 Range，不先下载整曲到服务器。返回 `full:false,verification:pending`，不冒充完整音源已验证。网页核对播放器实际时长，不匹配时停止并重试一次严格完整校验。无法渐进读取的来源保留严格校验，处理临时文件不会进入本站或设备曲库。外部平台可用性、源速度和浏览器解码仍影响在线起播；本地文件播放不经过服务器。

## 接口约定

| 接口 | 行为 |
|---|---|
| `POST /api/music/site/uploads` | 上传音频到本站（最多 512 MB，探测真实音轨），返回 site 歌曲；旧 `/api/library/import` 作为明确上传的兼容入口。 |
| `GET /api/music/site/tracks` | 当前账号确认上传且实际存在的服务器音源。 |
| `GET/HEAD /api/music/site/{asset_id}/content` | 验证资源归属和媒体会话，支持 Range 在线播放及设备保存。 |
| `POST /api/music/playback` | 新客户端传 progressive:true；旧客户端及严格重试保留完整校验。 |
| `POST /api/downloads/plan` | 输入 `url,format_id?,parse_record_id?,title?,media_type,audio_format,destination,compatibility?`，返回 `download_id,version,destination,mode,filename,url,fallback_url,expires_at,task_id?`；目标默认本机。 |
| `GET/HEAD /api/downloads/{id}/content` | 当前会话的既有文件或媒体转发；返回附件名称、适用的长度与 Range 元数据。 |
| `GET /api/downloads/{id}/state` | 获取准备、传输和终态；计划过期返回 410。 |
| `POST /api/downloads/{id}/complete` | 输入 `status,completion_source,transferred_bytes`；校验本机和云端完成来源，终态幂等，兼容处理文件按完成或取消清理。 |
| `GET/HEAD /api/downloads/tasks/{task_id}/file` | 获取已完成且尚未回收的当前账户临时文件。 |
| `POST /api/downloads/batches` | 兼容旧调用方：输入最多 100 首歌曲元数据及音频格式，校验来源权限，返回 ZIP 下载计划；新界面批量逐首使用 `/plan`。 |
| `GET/HEAD /api/downloads/batches/{id}/archive` | 当前会话按需生成 ZIP，每首校验后才写入，失败歌曲跳过并加入结果说明，正常关闭中央目录。 |
| `POST /api/library/download-status` | 输入最多 200 首歌曲，返回每首的有效文件、文件名和当前用户地址；不返回其他用户信息。 |

浏览器原生附件请求不会经过页面的 `authenticatedFetch`，也不会自动附加应用内存中的 Bearer 凭据。上述 `/api/downloads/{id}/content`、`/api/downloads/tasks/{task_id}/file`、`/api/downloads/batches/{id}/archive` 三类地址的 GET/HEAD 必须接受既有 `videodream_media` HttpOnly Cookie，并继续校验有效会话、资源归属、平台权限及计划期限。不得将访问令牌拼入下载 URL。创建计划、查询状态、确认完成等业务接口仍须 Bearer 认证，Cookie 不授权这些接口或写操作。

直连不符合条件时转发；源错误返回 502、缺失资源返回 404、源或临时文件过期返回 410。流已经开始后遇到错误会中断响应，本机写入必须中止。账号退出、撤销会话、停用或删除相关资源会阻止新请求；转发与音频管道会定期复查并停止。已经交给浏览器的普通文件传输由浏览器管理，不能保证网页在传输途中撤回。界面生成的导出文件没有新增服务端数据行。

## 云端保留文件去重

每个用户继续保留独立文件名、资源归属和访问检查；底层相同字节使用共享不可变文件池：

```text
downloads/_blobs/sha256/{hash前两位}/{sha256}
downloads/users/{uid}/files/{原文件名}
```

用户路径与池文件在同一文件系统时使用硬链接。不同用户看到不同路径，实际共享一份物理数据。比较 SHA-256 与长度，并校验已有池文件内容；不是符号链接，不公开池路径或全局媒体下载接口。跨文件系统无法建立硬链接时保留用户独立文件。

同账户相同下载目标复用有效文件或在途任务。歌曲标识使用平台与歌曲 ID；临时解析 token 不作歌曲身份。云端任务映射保存在既有资源记录中，通过短 MySQL 会话锁串行检查和登记，下载过程不持有该锁或数据库事务。

写入完成后才计算完整字节哈希，因此物理去重不能保证所有首次获取都省略上游流量。私有上传只允许物理字节共享，不能成为其他用户可读取的来源。

发布池文件、替换用户路径和垃圾回收使用同一哈希锁及原子文件替换。已发布字节不原地修改；需要改变媒体内容应生成新文件再发布。删用户文件只移除该用户链接；回收池文件须同时确认没有用户硬链接与有效音乐下载引用。数据库和文件系统不构成单一事务，失败保留可核对的文件并由维护流程收敛。

1000 个用户保留同一个 8 MB 文件，物理媒体可由约 8 GB 降至约 8 MB 加引用；1000 个用户各下载到本机，仍需向用户传输约 8 GB。

## MySQL 数据明细

新增 music_server_assets，resources 增加可空 device_id 和 resource_device 索引。music_tracks 结构不变，provider VARCHAR(12) 使用新增语义 site/device。完整相关结构如下，幂等迁移位于 backend/migrations/device_music.py。

```sql
CREATE TABLE resources (
  kind VARCHAR(24) NOT NULL,
  resource_key VARCHAR(255) NOT NULL,
  user_id BIGINT NOT NULL,
  payload MEDIUMTEXT NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  device_id CHAR(36) NULL,
  PRIMARY KEY (kind, resource_key),
  INDEX resource_owner (user_id, kind),
  INDEX resource_device (user_id, kind, device_id, created_at)
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

CREATE TABLE music_tracks (
  id CHAR(64) PRIMARY KEY,
  provider VARCHAR(12) NOT NULL,
  identifier VARCHAR(255) NOT NULL,
  title VARCHAR(255) NOT NULL,
  artist VARCHAR(255) NOT NULL,
  webpage_url TEXT NOT NULL,
  thumbnail TEXT,
  duration DOUBLE,
  access VARCHAR(100) NOT NULL,
  custom_thumbnail TEXT,
  cover_updated_at DOUBLE NOT NULL DEFAULT 0,
  cover_checked_at DOUBLE NOT NULL DEFAULT 0,
  user_id BIGINT NOT NULL DEFAULT 1,
  INDEX music_tracks_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_track_aliases (
  alias_id CHAR(64) PRIMARY KEY,
  track_id CHAR(64) NOT NULL,
  FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_downloads (
  id CHAR(24) PRIMARY KEY,
  track_id CHAR(64) NOT NULL,
  filename VARCHAR(255) NOT NULL UNIQUE,
  created_at DOUBLE NOT NULL,
  imported BOOLEAN NOT NULL DEFAULT FALSE,
  file_hash CHAR(64),
  file_size BIGINT,
  file_mtime BIGINT,
  file_available BOOLEAN NOT NULL DEFAULT TRUE,
  user_id BIGINT NOT NULL DEFAULT 1,
  INDEX file_hash_index (file_hash),
  INDEX music_downloads_owner (user_id),
  FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`resources.user_id` 与现有账户的关系由应用校验；本次不新增用户外键。`music_track_aliases` 通过目标歌曲归属隔离；不同账户的歌曲 ID 仍采用现有账户前缀哈希规则。

既有 `resources` 增加两类数据内容：

- `kind=delivery`，`resource_key=24位下载ID`：初始 JSON 为 `delivery_version:2`、`destination:device|library`、`delivery_mode:file|relay|direct|task|batch`、`status:ready`、`suggested_filename`、可空 `media_identity` 和 `task_id`。初始及终态均保存 device_id、client_kind、origin_sid；终态 JSON 保存状态、阶段、传输字节、完成来源、目标和文件名等。`completed/filesystem` 表示本机写盘关闭成功，`handed_off/browser` 只表示交给浏览器，`library_saved/server` 须核对云端任务已完成；另有 `failed`、`cancelled`。媒体源 URL、Cookie、本机路径和文件句柄不写入下载历史。终态先持久化再设置 Redis 的短 TTL，重复完成不会退回运行态，Redis 状态失效后仍以 MySQL 已持久化终态为准。个人中心与管理员的“下载与保存记录”可查看、删除记录。
- `kind=download_variant`，`resource_key=SHA256(uid+页面URL+稳定格式/歌曲标识+媒体类型+音频格式)`：JSON 为 `{ "task_id": "既有下载任务ID" }`。读取时仍检查原任务和文件是否存在、归属是否有效。失败、取消或被删除后可重新登记。

本机下载不为不存在的云端文件创建 `music_downloads` 行。新上传只维护 music_server_assets 及歌曲/文件资源，music_downloads 仅保留历史兼容。ready 本站资产参与共享池回收引用检查。

## Redis 数据明细

| Key 模板和示例 | 类型 | 值与 TTL |
|---|---|---|
| `videodream:download:plan:{uid}:{download_id}`；如 `videodream:download:plan:1:0123456789abcdef01234567` | String / JSON | `version:2,policy_version:2,id,uid,sid,origin_sid,device_id,client_kind,source_kind,provider,source_id,destination,mode,filename,expires_at,page,request`，按模式含服务端 `url,headers,audio,output_format,expected_duration,source_filename,task_id,tracks,audio_format`。TTL 为 600 秒与已知源到期剩余时间减 15 秒的较小值。 |
| `videodream:download:state:{uid}:{download_id}`；如 `videodream:download:state:1:0123456789abcdef01234567` | Hash | `version,status,phase,transferred_bytes,updated_at,completion_source`（完成来源为 `none/filesystem/browser/server`）；旧 ZIP 批量含 JSON `items`、`finished`、`failed`，例如 `finished="2",failed="1"`，items 为歌曲元数据、`status:finished|failed` 及失败 `error`。活动 TTL 24 小时，每次进度刷新；终态（含云端完成 `library_saved`）TTL 1 小时。 |
| `videodream:session:{sid}`；如 `videodream:session:example-session-id` | Hash | 既有 `uid,digest,version` 新增 `device_id,client_kind`。登录写入，refresh 原子轮换 digest 并为旧会话一次性绑定设备；保留原 7 天上限和剩余 TTL。请求头与已绑定设备冲突返回 401；媒体 Cookie 从会话取得归属。撤销删除整 key。 |
| `videodream:music:source:{uid}:{token}`；如 `videodream:music:source:1:example-token` | String / JSON | `version:1,uid,sid,track_key,provider,mode:relay,source_url,media_url,headers,expected_duration,verification:pending,expires_at`。TTL 为 600 秒与签名源剩余时间减 15 秒的较小值；凭据仅在服务端，过期重新解析。每次打开验证账号、会话和平台 ACL，传输期间检查会话撤销。 |

计划创建时写入，读取时同时验证当前账户、绑定设备、会话、持久化资源未删除及源权限。源请求头只存在于服务端短期计划，不出现在公共计划响应。取消、源错误和会话撤销阻止后续读取。源已过期时明确重新解析；已经建立的有效响应不因计划 TTL 到期而中途断开。

非终态通过 Lua 防止迟到进度覆盖终态；完成和取消通过短会话锁串行持久化。Redis 丢失不视为下载成功，MySQL 保存历史，本机完成提示以真实写盘结果为准。终态通知在源计划过期后仍须验证持久化资源的 uid、device_id、origin_sid；过期计划不能重新取得媒体凭据。

没有为共享媒体本体再引入 Redis 哈希索引；池文件和 MySQL 短锁协调发布与回收，避免缓存失效时错误删除用户文件。

本次逐首保存与失败隔离修正不新增 Redis key。旧 ZIP 在每首处理结束后写入同一 state Hash 的 `finished/failed/items`，状态接口按原权限读取；取消、终态保护、TTL 和失效规则沿用上文。逐首队列的 `pending/running/finished/handed_off/failed/cancelled` 和计数保存在当前浏览器内存，每首服务端计划独立更新，不在 Redis 创建共享前端队列。

## 浏览器存储

`localStorage["videodream.music.saved.{uid}.{device_id}"]`，如 `videodream.music.saved.1.123e4567-e89b-42d3-a456-426614174000`，保存以下 JSON，无固定 TTL，由当前账号当前设备保存时更新。旧 uid-only key 原样保留，读取时仅作为待核实记录，不作为本设备保存成功证据；设备文件索引另存 IndexedDB，格式见“搜索与已下载状态”。

```json
{
  "version": 2,
  "tracks": [{
    "title": "已保存歌曲",
    "artist": "歌手",
    "provider": "netease",
    "identifier": "123",
    "webpage_url": "https://music.163.com/song?id=123",
    "saved_to_device": true,
    "file_confirmation": {
      "source": "filesystem",
      "artifact": "individual_file",
      "status": "completed",
      "bytes": 123456,
      "saved_at": 1792540800000,
      "download_id": "0123456789abcdef01234567"
    }
  }],
  "unverified_keys": ["netease:456"],
  "unverified_tracks": [{
    "title": "旧保存记录",
    "provider": "netease",
    "identifier": "456",
    "webpage_url": "https://music.163.com/song?id=456",
    "saved_to_device": false,
    "save_unverified": true
  }]
}
```

成功历史仅接收单首文件、正整数实际写入字节数、真实写盘关闭后的确认。凭据的 `source/artifact/status` 必须匹配上述枚举，时间为有效毫秒时间戳；旧 ZIP、交给浏览器、零字节或只带成功标志均不可信。`download_id` 为下载计划提供的可选 ID；对已有资料库文件的直接另存仍以实际写入和关闭结果为依据。该凭据用于修正应用状态，不能让网页持续探测用户磁盘。成功保存一首只替换该首待核实记录，不批量确认其余歌曲。

首次读取数组或 v1 数据，先原样备份至 `videodream.music.saved.backup.{uid}.v1`（如 `videodream.music.saved.backup.1.v1`，String，值为原 JSON，无 TTL），已有备份不覆盖，再写入 v2。反复登录、刷新或升级不能把待核实记录提升为成功；账号切换清空内存并分别读取本人数据，迟到回调不写入其他账号。存储满时内存判定仍保持待核实，不强行覆盖未备份记录。备份和凭据均留在当前浏览器，不上传、不含绝对路径、文件句柄或平台凭据。

## 历史版本迁移、兼容和回滚

以下记录描述此前下载优化；本轮设备曲库的 DDL、历史文件分类与回滚以末尾“设备曲库升级与回滚”为准。

1. 上线代码无需 DDL；旧请求、旧任务和云端文件继续保留。新界面明确选择本机或云端。
2. 新云端文件自动进入共享池。既有用户文件不在服务请求中批量重写。
3. `scripts/deduplicate-files.py --manifest <新清单路径>` 默认只读盘点现有有效文件，生成用户、路径、大小和哈希清单。
4. 既有文件迁移须先暂停媒体写入，预留独立备份空间，再使用 `--apply --backup <空备份目录> --manifest <新清单路径>`。先完成并校验全部独立备份和清单，再替换链接；发现内容变化停止，保留证据。
5. 回滚物理存储先在停止写入时执行 `--rollback --manifest <迁移清单>`，将每个用户文件恢复为独立副本，再部署旧的可变写入实现。后续已变化的文件不覆盖，需先处理冲突；预留展开副本所需磁盘空间。无需反向 DDL，也不删除新用户数据。
6. Nginx 的内部路径配置与只读下载目录挂载准备就绪后，设置 `VIDEODREAM_X_ACCEL_PREFIX=/_protected_downloads`。所有下载仍先由应用认证，内部路径禁止公网直接读取。未开启时继续由 FastAPI 提供文件，保留开发和直连后端的兼容性。
7. 回退界面、下载服务或转发实现可恢复旧入口；旧数据结构可继续读取。新增 Redis key 按 TTL 失效；保留历史 JSON，不需要破坏性清理。
8. 逐首保存与失败隔离修正没有 MySQL 表、列、索引、关系或 DDL 变更，复用上述现有资源结构；无需迁移业务数据。旧客户端和 ZIP 地址继续可用。回滚仅涉及批量前端、共享保存方法及 ZIP 生成代码；新增可选 `failed` 计数字段可被旧版本忽略，TTL 自行回收，保留已成功保存文件和本机历史。回滚会恢复旧 ZIP 中途失败风险，部署前须保留该项修复。
9. 历史误标修正仅改变前端状态解释及浏览器存储格式，没有 MySQL 表结构、索引、关系或资源数据迁移，也不新增或修改 Redis key、类型、值、TTL、读写和一致性规则。旧服务端失败终态本已持久化，继续读取真实状态。回滚前端须保留 v2 和原始备份；兼容旧版时仅将已确认的单首凭据导出为旧格式，不将待核实 key 恢复为成功。无需反向 DDL 或 Redis 清理，不删除歌曲文件或自建歌单引用。
10. 浏览器附件 401 修正仅补齐既有媒体 Cookie 的只读地址匹配，没有新增或修改 MySQL 表、列、索引、关系、资源 payload，也没有 Redis key、类型、值、TTL、读写或一致性规则变化；复用现有会话与资源权限检查，无数据迁移。回退账号校验代码并重启后端即可回滚，但会恢复这些原生下载地址的 401 故障；不清理现有 Cookie、文件或保存历史。
11. 本轮保存位置、进度和名称优化无 MySQL 表、完整表结构、索引、关系或业务数据迁移变更；继续使用原资源键及本机 v2 确认。无 Redis key 模板、示例、类型、值结构、TTL、读写、失效或一致性规则变更，因为保存进度留在浏览器内存，服务器名称仅在响应时转换。兼容既有文件与 URL，无需重命名或反向 DDL。回滚涉及共享前端保存、默认批量方式、队列展示和附件建议名称代码及规格，重启原服务即可；保留用户文件和保存凭据，不恢复旧记录误标或 Cookie 401 故障。

## 提速预期与验收

提速来自减少完整文件等待、减少应用中转开销、避免完整 Blob 缓冲和不必要转码，以及复用已有文件。入站 20 秒、出站 40 秒的串行流程约 60 秒；能够重叠后约为 40 秒加准备开销。不是所有来源都可重叠，不能承诺固定倍数。

27.2 MB 的纯传输时间在 1/5/20 Mbps 下约为 218/44/11 秒，尚未计协议和写盘。公网出站带宽若仍是瓶颈，代码不能超过该上限；扩容须依据实际测量单独配置。

验证记录应区分准备时间、首字节、流式传输、写盘关闭与总耗时，检查单任务和并发下的带宽、内存、CPU、磁盘和失败率。至少覆盖：

- CORS 失败转发、认证过期、源过期、缺失文件、取消、长度不符、音轨时长不符、兼容重试和有界缓冲。
- ZIP 的 CRC 与中央目录、部分歌曲已输出但校验失败时不建立条目、全失败说明包、取消和输出故障；目录模式两首并发、部分成功、单曲超时、只重试失败项，以及原生逐首交付和手动链接。
- 超过 1000 条下载记录、歌曲别名、同名不同版本、当前账户隔离、按钮隐藏、全选排除和完成后即时刷新。
- 相同字节共享 inode、不同字节保持独立、池损坏、删除一名用户不影响其他用户、临时回收和内部 Nginx 路径权限。
- 前端测试、类型检查、生产构建、后端回归和 `git diff --check`；在原端口前端 5174、后端 8001 重启并验证。

云端实际带宽、CDN 出口及用户网络的提速倍数需要部署后的测量。代码交付与本地验证不等于已经部署云端或完成既有生产文件迁移。

## 本地验证记录（2026-10-21）

- 后端全量回归 566 项通过，使用独立 MySQL Unix socket 测试实例，没有更改业务数据库账号权限。随后增加终态过期幂等、云端完成校验与迁移回滚覆盖，下载专项 13 项全部通过。
- 前端 102 项测试、类型检查、生产构建通过；覆盖文件选择先于解析、CORS 失败转发、写盘关闭后才确认、音轨不完整从零兼容重试、取消和云端完成区分。
- 首轮桌面 1440px 与手机 390px 的浏览器模拟接口验收通过：已下载行、详情及搜索菜单隐藏下载，多选跳过，缺失文件恢复入口，单曲成功即时隐藏。当时 ZIP 为默认方式，现已由下面的逐首保存修正替代。
- 文件夹模式验证两首并发、已有名称避让、部分失败清理和只重试失败歌曲；原生下载验证仅提示浏览器接管，不误记本机保存。页面验证使用模拟接口与浏览器文件句柄，不写入用户真实下载目录。
- 物理去重测试验证相同字节共享 inode、单个用户删除不影响其他用户、损坏池拒绝复用；离线迁移测试验证独立备份、去重后回滚为独立文件。
- 前端 5174、后端 8001 已在原端口重启；首页与健康检查返回 200，新下载计划未登录返回 401。保留原有 8002 服务。`git diff --check` 通过，隔离测试 MySQL 已停止。
- 未在云端执行部署、开启 X-Accel 或迁移既有生产文件；Nginx 真实传输和实际公网提速仍需上线环境验证。

### 逐首保存与失败隔离修正（2026-10-21）

- 后端下载专项 18 项通过，新增真实 ZIP 解压、CRC 和中央目录验证：中间歌曲输出部分字节后失败，仅包含前后两首完整歌曲；全部失败仍有有效结果说明；取消继续生效，ZIP 输出故障仍上报失败，单首暂存无文件遗留。兼容任务每首完成即回收，再准备下一首。
- 前端 109 项测试、类型检查与生产构建通过；新增浏览器具备目录 API 时默认仍使用原生逐首下载、显式文件夹选择先于请求、两首并发、名称避让、失败文件清理、后续歌曲继续、只重试未成功项、原生下载先校验且不误记本机保存、单曲超时隔离和账户切换取消覆盖。
- Chrome 桌面 1440px 与手机视口 390px 共 7 组隔离接口场景通过：文件夹全成功、部分失败和重试、原生逐首全成功与部分失败，以及目录 API 可用时仍默认浏览器下载。真实浏览器下载事件与手动链接可取得独立文件；网页原生下载不创建本机成功历史，显式目录模式确认写入后更新历史。页面无横向溢出，暗色队列标题与操作可见。原生下载测试文件由本地 Vite 提供，平台接口和目录句柄使用模拟数据，未在 Safari 或实体手机验证。
- 本轮前端 5174、后端 8001 在原端口重启，首页、健康检查返回 200，未登录下载计划返回 401；保留独立 8002 服务。未提交、推送、部署或修改生产数据库及用户歌单数据。

### 历史失败误标修正（2026-10-21）

- 只读核对此前失败的批量 `music.zip` 及《Love Story》交付记录：均为 `failed`、完成来源 `none`；对应歌曲未登记为本人有效资料库文件。前端旧版缓存仍会仅凭歌曲 key 显示本机成功，现已通过 v2 迁移修正。
- 前端 118 项测试通过，新增旧数组/v1 迁移、原样备份、刷新幂等、逐首凭据校验、ZIP/浏览器接管/零字节排除、单首替换、账户切换及完成接口返回失败不能误报成功等覆盖。后端下载专项 19 项通过，新增失败终态在 Redis 失效后仍不可被保存确认覆盖。
- 类型检查和生产构建通过；浏览器隔离场景验证旧误标恢复搜索按钮、更多菜单和批量选择，待核实记录从本机成功列表分离，原备份在刷新后保留。旧记录不能自动证明用户磁盘现状，因此保留为待核实，不一律认定为下载失败或删除真实文件。

### 原生浏览器下载 401 修正（2026-10-21）

- 日志中的《Here For It All》任务 `dd940f110b48` 已完成准备，但原生 GET `/api/downloads/tasks/dd940f110b48/file` 返回 401，认证来源为 `none`。页面内准备与完成接口正常；新下载地址未加入媒体 Cookie 只读匹配，导致浏览器下载管理器无法取得文件。此前静态文件下载验证未覆盖这条认证链路。
- 新增真实账号登录与认证中间件集成测试：修复前逐首内容、临时文件及旧 ZIP 三个地址均复现 401，修复后无需 Authorization 即可凭有效 Cookie 读取附件；验证完整字节、长度、HEAD、206 Range、其他账号隔离、匿名拒绝、无效 Bearer 拒绝及会话撤销。旧 ZIP 仅模拟媒体输出，不模拟认证；创建计划、完成确认、状态查询及写操作不因 Cookie 放开。
- 账号与下载专项共 46 项通过，前端 118 项通过，类型检查和生产构建通过。账号集成测试使用独立 MySQL Unix socket 测试实例、隔离 Redis 前缀与临时文件目录，没有修改业务数据库；测试实例用完关闭。前端 5174、后端 8001 已在原端口重启，首页及健康检查返回 200，匿名下载请求仍返回 401；原有 8002 服务保留。未在用户 Chrome 窗口完成本轮下载交互复验，未部署云端。

### 保存位置、进度及文件名优化（2026-10-21）

- 用户明确选择“弹出保存位置，网页显示进度和网速（不进入浏览器下载记录）”，替代上面逐首修正阶段的原生下载默认行为。搜索多选、批量找歌与场景歌单默认选择本机文件夹；单首保存优先系统文件选择器，取消后没有资源请求。原生下载仍作为明确选项和兼容方式。
- 前端 125 项测试通过，覆盖默认选择目录、单首选择先于网络、取消不建任务、进度与低速单位、未知大小不伪造百分比、并发同名预留、失败继续及重试。类型检查与生产构建通过。12 位任务前缀在建议名称及附件响应中去除，正常数字歌名和只有 ID 加扩展名的旧文件保留扩展名。
- 账号与下载专项 47 项通过；真实附件响应验证文件内容、可读名称、HEAD、Range、X-Accel 内部路径保留原始资源名及既有认证隔离。使用独立测试 MySQL、Redis 前缀及临时文件目录，不变更业务数据。
- 在实际共享 Vue 组件的隔离预览页检查桌面及 390px 手机宽度：已知大小显示 50% 和 MB/s、剩余时间，未知大小显示滚动条和 KB/s。390px 时页面宽度为 390px、浮层宽 358px，无横向溢出；预览使用模拟进度，不写入用户下载目录，未自动操作系统保存对话框。
- 前端 5174、后端 8001 在原端口重启，首页和健康检查返回 200，匿名下载仍返回 401。规格、迁移与回滚说明同步，无 MySQL 或 Redis 结构变更；未部署云端。

## 设备曲库升级与回滚（2026-10-22）

此节和本页更新后的入口、数据契约优先于历史验收记录中的旧行为描述。

- 首次 DDL 前备份 music_* 与 resources，分类前输出旧记录 inventory（含既有大小、hash、owner 和文件名），写入 `.run/library-backups/`。迁移只处理未登记资产，可中断重跑；已确认 imported 的旧文件标为 site_upload，其余 music_downloads 为 legacy_download；缺失标为 missing。未索引的服务器文件保留在旧资源页待核实，不凭文件名自动认定上传。迁移不删除、移动或重新下载音频。
- 旧歌曲 ID、歌单、收藏、历史、歌词保持 canonical ID；site 身份通过 music_track_aliases 关联。合并歌曲前迁移资产 FK，避免丢失引用。历史无 device_id 的资源保持 NULL，不能自动分配给当前设备，也不进入设备下载列表。
- resources 的账号及设备归属由服务端写入，任务进度更新保留初始归属。当前设备任务列表在 SQL 过滤 device_id，分页及总数一致；读取、删除、临时文件下载以及过期完成回调都验证设备。管理员管理资源入口可保留服务器历史，但不混入个人本地曲库。
- 服务端正常曲库读取不再运行 sync_downloads 全盘扫描/hash/解码；旧文件整理保留显式维护调用。平台音源下载不会自动复用历史服务器副本。新歌曲云端创建/批量重试返回 422/410，历史任务查询及文件保留；视频的既有云端工具继续可用。
- 回滚必须保留新增表/列、asset 数据、别名、设备文件及原始备份；先切回兼容读取实现再重启服务。不要执行 DROP 或将所有 device_id 改成当前设备。Redis 新源令牌自然过期，旧 session/plan 需兼容字段；旧前端如果不能区分三类来源，不应直接恢复旧“已下载”判断。
- 本功能只完成本地工作区开发与测试，不自动发布云服务器或覆盖手机安装。网页构建与后端接口须配套发布。


## 设备曲库本地验收（2026-10-22）

- 网页“本地音乐／下载管理／本站音源”已分开，列表、更多菜单与播放器的下载入口统一以本设备可读文件判定。本地导入不发上传请求，实际播放地址为本机 Blob；缺失文件需要重新定位，保存凭证和服务器文件不充当设备文件。
- 前端 136 项测试、类型检查和生产构建通过。后端账号、设备权限、下载交付、曲库、上传、歌词、来源解析、严格播放及渐进转发相关回归共 145 个不同用例通过（各批次存在重复运行）。集成测试使用独立 MySQL Unix socket 实例与隔离 Redis 前缀，该测试 MySQL 已停止。
- Chrome 桌面 1440px 与移动视口 390px 验证真实文件句柄、IndexedDB 刷新恢复、Blob 播放、本地导入无上传、同账号第二个浏览器存储环境为空、服务器歌曲独立显示、下载管理入口，以及本地歌曲在播放器内隐藏下载按钮。账号与业务接口使用隔离模拟响应；服务端鉴权另由集成测试覆盖。本轮没有在实体手机、Safari 或真实外部平台网络上验收。
- 本工作区 MySQL 已应用幂等迁移：resources.device_id、四列 resource_device 索引及 music_server_assets 均存在；既有 113 条服务器文件登记为 legacy_download / ready，原文件保留，不自动变成本站上传或当前设备本地歌曲。迁移前备份和清单位于 .run/library-backups/。
- 前端 5174、后端 8001 在原端口重启，首页、健康检查、认证状态返回 200；匿名读取本站音源返回 401。git diff --check 通过。未提交、推送或部署云端；在线起播仍取决于外部音源和网络，实际本地播放不经过服务器。


## 网页本地导入校验修正（2026-10-22）

用户提供的 M4A 能读取约 398 秒容器时长，但 Chrome 在首个 AAC 包返回 PIPELINE_ERROR_DECODE；FFmpeg 独立读取原文件同样报 Prediction is not allowed in AAC-LC / Invalid data found when processing input。时长、后缀和文件可读均不能单独证明音频可解码。原导入逻辑还会每次生成 UUID，导致同一文件重复登记；本地 Blob 错误套用网络恢复后显示不准确的“媒体恢复失败”。

- 导入用本机临时 audio 元素读取有效时长并等待首批音频解码数据，超时 10 秒。仅元数据可读不算成功；损坏或浏览器不支持的编码给出文件名及错误原因，不新增可播放条目。校验、去重和播放均留在浏览器，不上传音频。首批解码通过不等于整曲完整性验证，后续解码失败仍需在播放器处理。
- 相同账号、设备、文件名和大小的候选，优先用 FileSystemFileHandle.isSameEntry 确认同一文件；没有句柄身份时分块比较完整字节，不以同名、同大小、同时间戳自动合并。保留已有歌曲身份及来源，补齐实际时长；重新导入可合并旧版同一文件的多个 UUID。拒绝音频时仅移除已经证明指向同一无效文件的旧索引，原文件保留。多选中一个文件失败不阻止后续有效文件导入。
- 本地解码失败显示本地文件原因，不执行网络探测、认证刷新或在线替代。浏览器明确返回解码失败/格式不支持时移除对应本设备索引，保留用户原音频文件；再次导入需要有效且当前浏览器可解码的副本。切歌、停止、退出释放 Blob URL，load 中断的 AbortError 不作为媒体故障重复恢复。
- MySQL：没有新增或修改表、字段、完整表结构、索引或关系；本修正仅执行浏览器文件读取与设备索引更新。Redis：没有新增或修改 key 模板/示例、类型、值、TTL、读写、失效或一致性规则，因为本地文件校验不调用后端。IndexedDB 继续 tracks v1，duration 使用原有 track 字段，无 schema 迁移；兼容旧条目。回滚只涉及网页曲库、音频检查和播放器代码，保留设备 ID、索引、原文件及服务器数据，不执行 DDL、清库或文件重写。
- 前端 147 项测试、类型检查与生产构建通过。Chrome 使用用户原文件复现旧错误并验证新导入拒绝；文件 SHA-256 前后一致。正常 AAC/M4A 验证实际播放时间推进、时长保存、重复导入、旧 UUID 重复记录合并和 IndexedDB 刷新恢复。账号及业务接口为隔离模拟响应，音频解码、文件句柄及 IndexedDB 为真实浏览器能力；没有音频上传或在线播放回退请求。截图和结果位于 .run/local-import-fixtures/。服务使用原前端 5174、后端 8001，本轮仅修改网页代码与规格。
