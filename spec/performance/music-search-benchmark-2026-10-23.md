# 音乐批量搜索并发测试与日志持久化

日期：2026-10-23。测试名单为本次 146 首 QQ 歌单导入任务，只读提取，不修改任务、歌单、收藏或生产缓存。

## 测试方法

独立 Python 进程调用现有搜索和匹配函数。测试时串行基线保留每首之间 0.4 秒等待；并行组分别同时处理 4、10、20 首，各平台独立线程池最多 2 个请求。每轮开始清空测试进程自己的搜索缓存，生产缓存不受影响；名单内重复查询仍可命中缓存。正序、倒序各测一轮，并单独对 20 首子集做热缓存测试。轮次之间间隔 8 秒。

统计任务耗时、处理吞吐、平台请求的排队时间、网络时间 P50/P95、HTTP/业务错误、空结果、候选覆盖。请求日志保留哈希化候选身份以供比较。匹配率包含原平台 ID 直接确认，不能用来代表跨平台搜索质量。

耗时只覆盖搜索与匹配，不包含生产任务保存、个人下载状态查询、浏览器轮询或歌单读取。结果不能解释为整页端到端耗时，也不能外推为平台官方容量。冷缓存只指本应用测试进程，不能清除平台/CDN 缓存。

## 网易云停止条件

三平台串行基线仅完成 5 首时，网易云 5 次请求中 4 次返回 JSON 业务码 405，其中连续 3 次失败，触发停止。HTTP 未报错，测试时生产代码会将其误显示为平台可用但没有结果。当日仅记录问题；2026-10-24 已在正式搜索中加入业务码检查。

随后暂停网易云的网络请求，继续 QQ、酷狗两平台对比，不能将两平台结果冒充三平台表现。日志保留网易云 skipped 记录及暂停原因。初始中断轮次的吞吐按实际处理 5 首计算，不按计划 146 首计算。

## MySQL 表结构

Flyway `V3__music_search_benchmark_logs.sql` 新增独立表，应用结构门禁由 V2 更新为 V3。只追加日志表，不修改业务表。完整结构：

```sql
CREATE TABLE music_search_benchmark_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    benchmark_id CHAR(32) NOT NULL,
    run_label VARCHAR(64) NOT NULL,
    record_type VARCHAR(16) NOT NULL,
    sequence_no INT UNSIGNED NOT NULL,
    song_concurrency SMALLINT UNSIGNED NOT NULL,
    provider_concurrency SMALLINT UNSIGNED NULL,
    provider VARCHAR(12) NULL,
    query_hash CHAR(64) NULL,
    queue_ms DOUBLE NULL,
    network_ms DOUBLE NULL,
    elapsed_ms DOUBLE NULL,
    status VARCHAR(24) NOT NULL,
    payload JSON NOT NULL,
    recorded_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY benchmark_record (benchmark_id, run_label, record_type, sequence_no),
    KEY benchmark_run (benchmark_id, run_label),
    KEY provider_status (provider, status, recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- benchmark_id 标识一次测试活动；run_label 区分平台范围、冷热缓存、轮次及并发档位。
- record_type 为 run/request；run 的 sequence_no=0，请求从 1 开始。唯一索引保证补录幂等。
- provider 为 qq/netease/kugou；汇总行为 NULL。无用户外键，日志不随账号删除。
- query_hash 为查询摘要；本次原始脚本使用 SHA-256 前 16 个十六进制字符，原样入库，不伪称为完整 64 位摘要。
- queue_ms 为请求线程池排队，network_ms 为网络函数执行时间，elapsed_ms 为对应请求服务时间或整轮时间；原始秒值换算为毫秒。
- status 为 finished/aborted（汇总）或 ok/cache_hit/failed/skipped（明细）。业务码 405 写入 failed，即使 HTTP 成功。
- payload 保留汇总统计、哈希化候选和请求原始记录，供后续复核。不写歌曲原文、分享链接、Cookie、密码或凭据。
- recorded_at 为入库时刻；原始日志没有请求绝对时间，不使用入库时刻冒充请求发生时刻。

使用 `scripts/import-music-benchmark.py <快照.json> --benchmark-id <32位hex> --prefix <轮次前缀>` 导入。连接通过外部 dev-infra 环境加载；默认平台上限 2，可通过 --provider-concurrency 指定。只由本地命令执行，无新公开 API。后台测试每轮结束保存快照，再补录到表；不是已接入所有生产搜索的实时日志系统。

## 兼容、迁移与回滚

新表无外键，不改变现有数据关系；索引覆盖测试活动/轮次以及平台错误分析。现有 V1/V2 SQL 不改动。迁移前使用既有备份工具，对 videodream 数据库一致性快照备份；在共享 MySQL 临时独立库先测试迁移和幂等补录，完成后清理临时库。

应用回滚时可停止日志写入并保留表和数据，但必须保留 V3 迁移历史及兼容 V3 的版本门禁。不能直接运行仅接受 V2 的旧二进制，也不删除 Flyway 历史来伪造降级。确需删除日志表应先备份并单独授权，不属于本次操作。

Redis 无新增或修改：无新 Key 模板、类型、值结构、TTL、读写或失效规则。继续使用原共享 MySQL/Redis，不启动额外中间件；测试进程内缓存随进程结束释放。

## 当前测试活动

benchmark_id：`7a40c0496c054df899768e0c2897f52d`。

备份：`.run/database-backups/20261023-172743-15d419e1/database.sql`（私有、忽略提交）。

查询示例：

```sql
SELECT run_label, song_concurrency, elapsed_ms / 1000 AS seconds, status,
       JSON_EXTRACT(payload, '$.providers.qq.network_p95_s') AS qq_p95_seconds
FROM music_search_benchmark_logs
WHERE benchmark_id='7a40c0496c054df899768e0c2897f52d' AND record_type='run'
ORDER BY id;

SELECT provider, status, COUNT(*) AS requests
FROM music_search_benchmark_logs
WHERE benchmark_id='7a40c0496c054df899768e0c2897f52d' AND record_type='request'
GROUP BY provider, status;
```

## 最终结果与质量门槛

以下为同一份 146 首名单的 QQ＋酷狗结果；网易云已因业务码 405 暂停。

| 歌曲并发 | 第一轮秒数 | 第二轮秒数 | 说明 |
| --- | ---: | ---: | --- |
| 当前串行 | 267.677 | 未重复 | 保留每首间隔 0.4 秒 |
| 4 | 114.763 | 81.230（无效） | 第二轮 QQ 空结果 62/146，不计入提速比较 |
| 10 | 101.146 | 96.050 | 均无显式错误、空结果为 0 |
| 20 | 98.991 | 96.491 | 均无显式错误、空结果为 0 |

有效并发轮次每个平台最多 2 个请求。10 首均值 98.598 秒，20 首均值 97.741 秒，差异不足 1%，不能认定 20 首有稳定优势。相比串行基线，有效 10 首结果约快 2.72 倍，但混合了取消固定间隔和并发两项收益，不单独归因于线程数。

首轮 QQ 排队 P95：4 首 2.242 秒、10 首 7.265 秒、20 首 14.418 秒；网络 P95 分别为 2.286、2.166、1.932 秒。酷狗网络 P95 分别为 0.765、0.444、0.379 秒。并行组 289/292 个平台查询返回与串行完全相同的候选 ID 集；候选总数 858→857，外部搜索结果存在小幅变化，不能宣称逐条结果完全相同。146 首自动匹配包含来源 ID，不等同于跨平台全部成功。

末轮 4 首的 QQ 空列表从 0 增至 62/146，返回候选数量从 2890 降至 1660，没有显式 HTTP 或业务错误；原因未证实，可能为限制或接口异常。此轮排除。其后的 20 首预热也有 11 个 QQ 空结果，热缓存轮次虽只有 0.027–0.039 秒但缓存了这些空列表，全部排除，不据此宣称加速。

相关汇总 payload 增加 `valid_for_speed_comparison=false` 与 `quality_warning`，保留全部原始明细。未继续尝试更高的平台请求上限。建议后续正式方案采用可配置的 10 首起点＋平台级并发/频率控制，并把业务错误和突增的空结果纳入退避判定，而非仅检查 HTTP 429。

最终已入库 3333 条：12 条轮次汇总、3321 条请求记录（含跳过和缓存命中记录，并非 3321 次网络请求）。其中网易云初始 4 条失败、1 条成功；暂停后 1102 条为跳过记录。冷缓存每个平台每轮 145 次网络请求＋1 次名单内重复查询缓存命中。

迁移验证：Flyway 集成测试通过；本次独立测试库清理完成。V3 已在共享 MySQL 的 videodream 库迁移及 validate 成功，8001、8002、5174 health 均返回 200。上述为 2026-10-23 验证结果；2026-10-24 已实施 10 首并发、平台配额和页面耗时统计，详见 [歌单导入规格](../features/music-playlist-import.md)。
