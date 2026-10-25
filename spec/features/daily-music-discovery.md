# 今日推荐：画像、候选池与模型增强

更新日期：2026-10-25。本文合并原候选池解耦方案与用户音乐画像 Agent 方案，删除重复说明和历史测试流水。

**状态：候选池换歌与画像 Agent 已实现；画像 Agent 默认开启，模型相关验证使用模拟响应。本地迁移与验收结果见文末实施记录。**

## 1. 当前能力与目标差异

| 项目 | 原有行为 | 本次实现 |
| --- | --- | --- |
| 登录 | 后台准备当天推荐，复用已有批次 | 读取当前列表，按需后台维护画像和候选池 |
| 换一批 | 每日最多三批，与检索、分析绑定 | 从候选池算法重排，无每日三次限制 |
| 画像 | 收藏、种子和有效行为的规则统计 | 规则画像即时更新，Agent 维护有证据的推断 |
| 模型预算 | 每批最多 60,000 token | 候选增强与画像评估各自按用户每日限额记账 |
| 数据变化 | 不自动替换当天已有结果 | 下一批使用最新画像，当前播放不被打断 |
| 移动端 | 今日推荐列表无封面 | 保留歌名、歌手及播放、收藏、反馈操作 |

范围：持续发现符合用户口味的陌生歌曲。不训练协同过滤或深度排序模型，不扫描上传本地音频，不恢复已撤回的 Android 原生采集。下载或批量导入不直接当作喜欢；当前 Android 原生播放不计入有效收听画像。

## 2. 统一流程与页面

```text
收藏 / 有效播放 / 明确反馈
        ↓
记录事件、递增证据版本 → 规则画像更新
        ↓ 达到阈值
画像 Agent → 提案校验 → 发布画像版本
                              ↓
登录 / 换一批 → 读取已生效画像 → 候选池过滤、重排 → 推荐列表
                                      ↓ 候选不足
                              后台检索、分析、播放校验
                                      ↓
                              可选模型增强 → 写入候选池
```

- 登录不等待模型；有结果先展示，无偏好显示引导。
- 换歌接口不调用模型或外部检索，每批固定 6 首；不足时保留当前列表并等待后台补充，不降低标准凑数。
- 已发布列表的歌曲信息不因一小时播放校验到期而隐藏，播放时仍重新获取音源并检查完整时长。历史短列表优先恢复最近 30 天内仍满足屏蔽和明确条件的完整六首列表；有新鲜候选时，后台会自动重新分配完整六首，不修改正在播放的队列。
- 换歌间隔 3 秒并防重复提交，无每日次数上限。没有完整新列表时保留当前列表，主动换歌后简短提示“新推荐准备中，稍后再试”，可选择“重听推荐”。完整列表不常驻显示后台补充、额度、恢复时间或音源校验信息，管理员的听歌页面也遵循此规则。
- 点击“减少推荐”后，保留其余合规歌曲及顺序，立即从已校验候选池补足六首；不受换歌冷却限制，不同步调用外部检索或模型。缺少合规候选时保留剩余歌曲并自动调度后台补充，候选到货后只补缺口，页面自动更新，无需用户操作。短列表仅提示“新推荐准备中，准备好后会自动补齐”。
- 移动端首页的六首推荐分成两页横向滑动，每页纵向显示三首紧凑歌曲行；左右滑动一次切换整组三首，也可点击两个圆点切换，显示“1 / 2”页码，不自动轮播。播放、收藏、减少推荐保留，播放全部仍使用完整列表；反馈补位后尽量保留当前页，筛选后的页数按每页三首重新计算，无结果有明确空态。桌面及推荐详情继续列表展示，曝光只计算实际进入可视区域的卡片。本项仅改前端呈现，无 MySQL／Redis 数据变更或迁移，回退界面代码即可。
- 收藏和明确偏好立即更新规则依据；普通行为合并计算。画像变化提示“偏好已更新，下批生效”，不替换正在播放的队列。
- 首页指定区域展示前 6 首，继续聆听下移；详情每批同样展示六首。桌面沿用三列布局，手机单列且不显示封面与辅助推荐说明，兼容深浅主题。
- 详情新增“我的音乐画像”：展示长期、近期、场景偏好，证据摘要、可信状态、更新时间与变更历史。支持确认、纠正、暂停学习、重新评估、清除推断及恢复历史版本；手机用单列和抽屉。

## 3. 行为证据与规则画像

| 行为 | 初始处理 |
| --- | --- |
| 当前收藏 / 用户种子 | +5，明确偏好 |
| 主动单曲加入歌单 | +3，区别于批量导入 |
| 主动重播 | +2，同录音每日限权 |
| 有效完播 | +0.5 |
| 15 秒内主动跳过 | -0.2，弱负向 |
| 取消收藏 | 撤销收藏依据，不直接视为讨厌 |
| 自动循环、播放失败、暂停、网络中断 | 不产生主动重播或不喜欢信号 |
| 曝光 | 只用于曝光控制及评估 |
| 下载、批量导入、自动保存推荐 | 不逐首计强正向 |

同录音每天隐式正向上限 2.5、负向绝对值上限 0.4。权重是待校准参数，不是用户喜欢概率。

Web 按播放会话 UUID、递增序号去重，约每 15 秒及暂停、切歌、结束时上报实际时长与覆盖并集；缓冲、拖动、重复片段不能增加虚假覆盖。完播条件：覆盖至少 80%，实际播放至少 `min(60 秒, 时长×80%)`，无无效播放错误；未知时长不计完播。离线仅使用有上限的内存缓冲，丢失行为保持未知。

明确反馈优先于评分：不喜欢持续至撤销，听腻了默认 90 天，今天不想听至上海日期结束。可靠作品身份可屏蔽作品，否则仅屏蔽录音并说明范围；反馈与收藏独立，均可撤销。“多推荐类似的”默认只作用于下一批，用户选择长期记住后才保存。

长期窗口 180 天、半衰期 60 天；近期窗口 7 天、半衰期 3 天。旧收藏不重复成为近期新兴趣，重复分析不刷新证据时间。事件事务同时递增规则 `revision` 和相关证据版本；收藏事件保存操作当时的结果，旧事件缺失信息不倒推补造。

## 4. 画像 Agent

### 4.1 维度与权限

| 维度 | 合法依据 |
| --- | --- |
| 语言、歌手 | 用户设置、可信歌词和行为 |
| 人声、情绪、主题 | 已有歌曲分析及行为 |
| 节奏、能量 | 已有音频分析，不能从歌词推断 |
| 场景 | 用户明确选择或确认的专注、通勤、运动、睡前等场景 |
| 风格、乐器 | 用户表达或已有可信标签，缺失时保持未知 |

每项保存强度、可信分、证据、作用范围和时间。范围分长期、近期、场景；本批需求 > 用户设置及屏蔽 > 场景画像 > 长短期推断。探索程度由用户设置，Agent 只能建议。

工具：`read_profile`、`summarize_evidence`、`inspect_track_evidence`、`inspect_conflicts`、`propose_profile_changes`、`finish_without_changes`。查询只允许访问任务冻结的当前用户证据快照，不在画像任务内额外抓取歌词或音频。

Agent 只提出变更，后端校验维度白名单、数值、证据和版本后发布。可信分由服务端根据歌曲数、天数、质量与矛盾计算；低于 0.6 进入待观察。长期推断需多首、多天依据并限制单次变化，用户确认部分不可由模型覆盖。

### 4.2 触发与融合

- 累计至少 10 条有效新证据且涉及至少 3 首不同录音后安排评估；明确设置立即生效，再合并评估。
- 冷启动在用户活跃时依据收藏、种子和明确设置评估；修订号只表示变化，不能替代有效证据阈值。
- 应用内工作线程读取 MySQL 持久化队列，事务提交后唤醒，定时扫描补偿漏唤醒。
- 推荐读取确定的规则版本和 Agent 版本；同用户换歌与画像更新由用户行锁串行化，提交前复核规则版本，冲突返回重试。Agent 推断采用任务快照，发布前仍重查明确设置与屏蔽。
- 匹配分：长期 55%、近期 20%、种子相似度 15%、证据可信度 10%；缺失项归一。仅使用有字段证据的特征，节奏、能量还需完整音频覆盖。分类特征按证据匹配，节奏、能量按距离核匹配。歌手按个人拆分合作署名，合作记录的偏好权重平分至参与者，再以相对最高权重的平方根计算歌手匹配，减轻收藏数量差距对其他已喜欢艺人的压制。
- 维度内权重：歌手及音乐特征各为 1，语言、人声类型各为 0.25；只有语言／人声类型时相似度上限 0.30，只有歌手时上限 0.65，只有歌手与基础属性时上限 0.75。仅语言相同且不共享歌手的种子不产生种子相似度；有音乐特征时按真实重合维度比较，不能仅因同为中文就给出满分。明确的本批语言、歌手、纯音乐或能量条件可支持标为符合明确条件的探索，不把推断语言当作用户明确要求。以上是证据不足时的评分约束，分数仍是内部排序分，尚未经真实用户偏好概率校准。
- Agent 与规则重叠维度的推断融合权重上限 20%，新增语义维度须达到可信门槛；证据去重，归纳不增加行为计数。明确确认的偏好按用户设置处理。
- Agent 失败继续使用上一生效版本，无 Agent 画像时使用规则画像。

## 5. 候选池与选曲

| 参数 | 默认值 |
| --- | ---: |
| 每用户候选池上限 | 300 首 |
| 可用未展示候选低水位 | 30 首 |
| 补充目标 / 单次新增上限 | 100 / 100 首 |
| 候选保留 / 重复抑制窗口 | 7 / 7 天 |
| 同用户补充间隔 / 并发 | 30 分钟 / 1 个 |

后台工作线程每 30 秒扫描一页最近 7 天活跃用户，每页最多 20 人并循环推进；登录与换歌也会立即检查候选池。低于低水位或现有候选受歌手多样性等限制无法组成六首时，自动创建持久化补充任务。补充间隔或当日预算暂不允许时自动等待，后续扫描继续检查，无需用户再次操作。账号停用及管理员关闭推荐/网络会阻止补充。

候选保存来源、特征和校验证据，换歌按最新画像重算分数。来源检索、规则分析先行，模型可补充歌词语义和检索方向，不能编造歌曲或绕过硬条件。

后台补充以可组成完整六首列表的储备衡量增益，而不只统计候选总数：按既有去重和每歌手最多两首规则逐批计算，最后一批可计入待补齐的部分。优先检索现有可用候选较少的歌手；对过期候选复检和新歌分析／播放校验前，跳过不能增加可组批储备的同歌手过量候选及同曲。检索使用计划内全部路线，仍受单任务搜索上限约束；近期低收益路线同时读取后台补充任务记录。只有匹配及完整播放校验通过的歌曲才能增加储备，不放宽六首发布、匹配、屏蔽或去重要求；每日额度与30分钟补充间隔保持不变。仅调整既有候选、任务和画像数据的计算及读取，无 MySQL 表／列／索引／关系或 Redis key／TTL 变更，无迁移；回退选曲和执行代码即可，已有列表、事件和账本保留。

歌手检索覆盖全部可读偏好和收藏／种子中的个人歌手，不再被前四首收藏或前五个歌手永久截断。单任务仍最多五条检索，按可用候选缺口、最近五个任务的同检索词使用次数及低收益历史排序，在多个任务间轮换覆盖；有已知曲风／情绪／主题／乐器偏好时可安排相应探索检索。先完成受限检索并为各路线预留候选名额，再按个人歌手库存和尝试次数轮流分析／校验；过期复检加入同一待处理队列，避免先耗尽网络额度。用户明确排除的歌手及合作署名同样受限。

待分析的真实检索结果保存到既有任务 `context.recall_pending`，恢复任务先处理已保存结果，保留原检索次数、网络消耗和执行时长预算，不重复发起已完成检索。歌词预取遇到临时繁忙、限流或无结果时继续使用已有可信特征；未知特征保持未知，仍须通过匹配门槛及完整音频校验。认证、任务取消、管理员禁用和预算错误继续中止任务。此队列属于既有 JSON 任务快照，受原 512 KiB 上限约束，无表结构或 Redis 契约变更；旧任务没有此字段时按原入口启动，无结构迁移。

发布新列表的条件：身份可靠、完整播放校验有效、符合明确要求及匹配阈值。候选校验过期后不能进入新批次，等待后台复检；已经发布的歌曲元数据继续展示，过期状态单独标记，不宣称音源仍通过当前校验。临时播放链接不长期保存，实际播放仍重新解析并检查时长。

后台状态明确区分执行中、补充间隔等待、当日网络额度耗尽、管理员关闭和可调度。额度耗尽时接口返回下一个上海日期零点，间隔等待返回实际到期时间，供运维诊断使用；听歌页面不展示这些内部状态，不把等待显示为正在检索。等待不会清空当前列表，额度上限不因重试或恢复列表提高。

同批录音最多一首、可靠作品最多一首、每位个人歌手最多两首；合作曲同时占用每位参与歌手的名额，相邻回避和近期曝光惩罚也按个人歌手累计。推荐配额对署名两端的装饰符号做保守归组，如“周杰伦♚”不能绕过“周杰伦”的限额或被当作陌生歌手；此归组不证明官方艺人身份，不用于合并录音键或改写历史身份。探索比例按用户设置为 10%/20%/30%，优先填充对应数量的探索位置；候选全部参与者均不属于正向熟悉歌手时才能标为探索，且仍必须有匹配证据，合作署名新增嘉宾不能把熟悉歌手变成探索。默认排除已知歌曲、当前列表和重复抑制期内已分配歌曲；“重听推荐”只放宽推荐曝光重复，不解除屏蔽或播放校验。

历史完整列表若超出个人歌手限额，读取及补充完成时尝试保留仍合规的歌曲并从有效候选补齐六首，发布新的不可变列表；候选不足时保留原完整列表等待后台补充，不能把已有六首裁成短批次。历史列表、原位置和原评分证据保留，新列表的探索标记按当前熟悉歌手重新判定。

录音身份比较保留完整歌名及版本说明，对歌手署名做 Unicode／空白规范化并按集合比较，已知时长相差不超过一秒时识别为同一录音，适用于同平台不同 ID 及跨平台来源。例如“刀马旦 · 周杰伦 / 李玟”与“刀马旦 · 李玟 / 周杰伦”只能出现一次；不同歌手、Live 标记或时长不符不合并，时长未知不推断。历史独立录音键在读取时建立等价关系，去重、屏蔽、已知歌曲及近期曝光排除共同使用；原录音键、反馈及事件引用保持有效，不批量重写历史数据。

反馈补位、历史重复修复和后台补齐均产生新的完整列表，复制仍合规的歌曲快照、推荐依据及原校验时间，只对新增歌曲要求校验仍有效。历史列表及其位置保持不变，正在播放的队列不自动替换；旧列表的保存记录及播放／曝光事件可继续引用。所有补位按用户锁串行提交，重复反馈或并发反馈不产生重复歌曲；候选不足六首时不发布新的短批次。

本次无 MySQL 新增或修改的表、列、索引、外键关系，无 Redis 新增或修改的 key、类型、值结构、TTL 或一致性协议：复用第 8 节既有 identities、feedback、feeds、feed_items、candidates 表，仅按现有格式写入正常反馈和新列表；不需要结构迁移或数据清洗。回滚应用代码即可，历史身份、列表、播放事件及保存的歌单均保留。

## 6. 模型预算与结算

| 限制 | 候选增强 | 画像评估 |
| --- | ---: | ---: |
| 每用户每日 token | 60,000 | 24,000 |
| 每用户每日模型请求 | 16 次 | 最多 3 个付费任务、每任务 4 次 |
| 单任务 token | 每日剩余与管理员上限较小值 | 12,000 |
| 单任务时长 | 180 秒 | 120 秒 |
| 同用户任务间隔 | 30 分钟 | 30 分钟 |
| 网络请求 | 每任务 160、每日默认及最大 5,000 | 不额外抓取歌曲资料 |
| 单任务搜索 | 5 次 | 无 |
| 并发 | 同用户最多 1 个补充任务 | 同用户最多 1 个有效任务，全局默认 1 |

两类预算独立：默认每日合计最多 84,000 token，均受管理员及全局模型并发限制；这不是换歌次数配额。画像付费任务次数包含已发出请求后的失败。管理员可收紧预算；图片、音频和歌词分析仍遵守原 Agent 的来源权限及请求限额。

每日默认及最大 5,000 次、单任务 160 次网络请求是后台自动补充的保护值，用于约束失败重试、检索、歌词获取和音源校验的累积请求，避免无限访问；不是平台官方限额。2026-10-24 完成 120 次 HTTP 发送、28 次完整音频验证（26 次通过），结合两个历史后台任务，按全天维护 30 首候选及每天换 48 批的成本模型估算 P95 为 4,060 次，加 20% 余量约 4,872 次，经用户确认将原 480 次调整为 5,000 次。此值是当前样本支持的初始配置，未经过真实全天运行验证，也不保证外部来源故障时仍能补齐歌曲。单任务上限和 30 分钟补充间隔沿用原配置，不属于本轮优化结论。管理员可在 1～5,000 内收紧，已显式保存的较低值继续生效；缺失配置使用新默认值。不限制播放次数或候选池算法换歌次数，请求上限与歌曲数、模型 token 预算分别计算。达到上限时等待下一个上海日期，保留已发布推荐，不把列表可见性绑定到网络余额。

本次仅调整既有 `system_settings.config.music_daily_network_requests` 的默认值、请求准入上限及管理表单范围。MySQL 无新增或修改表、列、索引、关系；既有 `music_discovery_daily_usage.network_request_limit` 在下次汇总时按当前设置刷新，实际 `network_requests` 和请求明细保持累计，不清零、不返还消耗，无数据迁移。Redis 无 key、类型、值结构、TTL、读写、失效或一致性变更。功能回滚可将额度调低或回退匹配代码并按原端口重启；已用量超过回退额度时停止新增请求并等待次日，不删账本或历史推荐。

- 任务与每日预算在同一事务原子预留，逐请求记录实际、执行中预留、未知占用，按请求 ID 幂等结算。
- 输入估算沿用 `max(cl100k估算×1.25+512, UTF-8字节数/2)` 加输出与安全余量；不是供应商精确 tokenizer。
- 实际超单次预留但未超任务及每日总额时允许继续，累计超限停止付费。推理 token 为输出细分，不与 total 重复相加。
- 候选请求归属请求准入时的上海日期；画像任务在首次预留时固定归属日期，跨午夜不重置。
- 任一链路出现未知用量，暂停该用户两类新增付费调用直至核对；保留已有画像、算法选曲和合规网络补充。两个账本均需检查，不能跨链路或跨日绕过。
- 取消、失败和重新登录不返还已发生用量；已发送请求不自动重发。模型调用及外部检索均在事务外执行。
- 画像预算使用画像任务账本聚合，候选预算使用每日汇总表及原推荐任务请求明细；对账不能混入场景歌单消费。

模型价值在于复杂需求理解、歌词主题/情绪分析和检索扩展，不是每次换歌的必要步骤。是否提高收藏率、有效播放率并降低跳过率，需要真实对照验证。

## 7. 接口

统一前缀 `/api/music/discovery`。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /prepare` | 登录幂等准备，按需安排后台维护 |
| `GET /feed` | 当前列表、画像版本、补充状态；按需修复重复或缺口 |
| `POST /feed/next` | 候选池换歌，要求 Idempotency-Key |
| `GET /feeds/{feed_id}` | 读取指定列表 |
| `POST /feeds/{feed_id}/save` | 保存个人歌单 |
| `PUT /feedback` | 持久化反馈、按缺口补位，响应的 `feed` 为最新列表 |
| `GET /profile` | 兼容原响应，增加画像、版本、学习状态 |
| `PATCH /profile` | 按实际提供字段合并明确设置及学习开关 |
| `POST /profile/evaluations` | 幂等请求画像评估 |
| `GET /profile/evaluations/{run_id}` | 本用户任务状态 |
| `POST /profile/corrections` | 确认或纠正维度 |
| `GET /profile/versions` | 分页历史 |
| `POST /profile/restore` | 从历史推断创建新版本 |
| `POST /profile/reset` | 清除推断，保留明确设置，开始新学习区间 |

纠正、恢复、重置需 `expected_agent_revision`，冲突返回 409。列表响应包含 `feed_id`、`tracks`、`target_count`（6）、`actual_count`、`profile_revision`、`latest_profile_revision`、`agent_revision`、`refreshing`、`can_change`、`retry_after_seconds`、`reason`；区分冷启动、补充中、不足六首、耗尽及服务错误。另返回 `validation_stale` 和 `refill: {state,retry_at}`，其中 state 为 running、budget_exhausted、cooldown、disabled 或 idle，retry_at 为自动恢复的 Unix 秒或 null；歌曲 discovery 同样携带 validation_stale。旧客户端可忽略新增响应字段。

成功分配的换歌请求按用户与幂等键复用同一列表，参数不同返回 409。没有可分配候选时保持当前列表，不创建空列表；相同请求稍后重试可以获得新补充的候选。画像评估同时按输入版本和请求键复用任务。

现有 capabilities/today/batches/events/feedback 接口兼容保留。旧客户端创建批次也必须经过新共享预算，不保留付费绕过入口。事件增加可选 `context`（默认 general，必须用户明确选择）；新列表关联写入 payload 的 feed_id/position，旧 batch_id 留空，并校验归属。

## 8. MySQL 结构

| 范围 | 变更 |
| --- | --- |
| 既有六张 discovery 表 | 保留；仅 profiles 按下文目标结构扩展 |
| 候选池方案 | 新增 candidates、feeds、feed_items、daily_usage 四表 |
| 画像 Agent | 新增 profile_runs、profile_versions 两表 |
| 其他现有表 | 仅扩展已有 JSON，不改列、索引或外键 |

时间字段沿用 Unix 秒 DOUBLE。下列 SQL 是完整结构说明，不直接重建已有表；画像升级使用 ALTER TABLE。实际迁移为 `backend/migrations/sql/V4__discovery_pool_and_profile.sql`，保留 V1～V3 的已执行内容。

### 8.1 现有六表基线

```sql
CREATE TABLE music_discovery_profiles (
    user_id BIGINT NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    built_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    profile_version VARCHAR(40) NOT NULL,
    settings JSON NOT NULL,
    long_term JSON NOT NULL,
    short_term JSON NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_identities (
    user_id BIGINT NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    canonical JSON NOT NULL,
    evidence JSON NOT NULL,
    identity_version VARCHAR(40) NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, track_key),
    KEY discovery_identity_recording (user_id, recording_key),
    KEY discovery_identity_work (user_id, work_key),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_batches (
    batch_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    run_id CHAR(32) NOT NULL,
    recommendation_date DATE NOT NULL,
    batch_no TINYINT UNSIGNED NOT NULL,
    target_count SMALLINT UNSIGNED NOT NULL,
    request_key CHAR(64) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    profile_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,
    generation_seed CHAR(64) NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (batch_id),
    UNIQUE KEY discovery_batch_run (run_id),
    UNIQUE KEY discovery_batch_request (user_id, request_key),
    UNIQUE KEY discovery_batch_number
        (user_id, recommendation_date, batch_no),
    KEY discovery_batch_created (created_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id)
        REFERENCES music_recommendation_runs(run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_items (
    batch_id CHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    selection JSON NOT NULL,
    validated_at DOUBLE NOT NULL,
    published_at DOUBLE NOT NULL,
    first_exposed_at DOUBLE NULL,
    last_exposed_at DOUBLE NULL,
    exposure_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, position),
    UNIQUE KEY discovery_item_track (batch_id, track_key),
    UNIQUE KEY discovery_item_recording (batch_id, recording_key),
    KEY discovery_item_recording_lookup (recording_key),
    KEY discovery_item_work_lookup (work_key),
    KEY discovery_item_published (published_at),
    FOREIGN KEY (batch_id)
        REFERENCES music_discovery_batches(batch_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_events (
    user_id BIGINT NOT NULL,
    event_id CHAR(36) NOT NULL,
    track_key CHAR(64) NOT NULL,
    batch_id CHAR(32) NULL,
    position SMALLINT UNSIGNED NULL,
    playback_id CHAR(36) NULL,
    seq INT UNSIGNED NULL,
    event_type VARCHAR(24) NOT NULL,
    origin VARCHAR(16) NOT NULL,
    listened_ms INT UNSIGNED NOT NULL DEFAULT 0,
    covered_ms INT UNSIGNED NOT NULL DEFAULT 0,
    duration_ms INT UNSIGNED NULL,
    terminal_reason VARCHAR(24) NOT NULL DEFAULT '',
    payload JSON NOT NULL,
    occurred_at DOUBLE NOT NULL,
    received_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, event_id),
    UNIQUE KEY discovery_event_sequence
        (user_id, playback_id, seq),
    KEY discovery_event_owner_time (user_id, received_at),
    KEY discovery_event_track_time
        (user_id, track_key, received_at),
    KEY discovery_event_item (batch_id, position),
    KEY discovery_event_gc (received_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, track_key)
        REFERENCES music_discovery_identities(user_id, track_key)
        ON DELETE CASCADE,
    FOREIGN KEY (batch_id, position)
        REFERENCES music_discovery_items(batch_id, position)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_feedback (
    user_id BIGINT NOT NULL,
    scope VARCHAR(16) NOT NULL,
    target_key CHAR(64) NOT NULL,
    reason VARCHAR(16) NOT NULL,
    note VARCHAR(300) NOT NULL DEFAULT '',
    expires_at DOUBLE NULL,
    revoked_at DOUBLE NULL,
    source_event_id CHAR(36) NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, scope, target_key, reason),
    KEY discovery_feedback_expiry (user_id, expires_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

JSON 契约：settings 保存明确设置；long_term/short_term 保存特征、支持数、证据和窗口；canonical/evidence 保存歌曲身份及依据；track_snapshot 为播放元数据；selection 为评分、路线、原因和证据；events.payload 为主动操作、场景及操作结果。

反馈 scope 为 recording/work，reason 为 dislike/fatigue/today；revoked_at 为空且 expires_at 为空或未到期才生效。作品反馈为服务端验证的多态引用；source_event_id 为审计关联，不强制保留原事件。

### 8.2 新增候选池与列表表

```sql
CREATE TABLE music_discovery_candidates (
    user_id BIGINT NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    analysis JSON NOT NULL,
    validation JSON NOT NULL,
    source_run_id CHAR(32) NULL,
    policy_version VARCHAR(40) NOT NULL,
    validated_at DOUBLE NULL,
    validation_expires_at DOUBLE NULL,
    expires_at DOUBLE NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, track_key),
    KEY discovery_candidate_recording (user_id, recording_key),
    KEY discovery_candidate_work (user_id, work_key),
    KEY discovery_candidate_expiry (user_id, expires_at),
    KEY discovery_candidate_validation (user_id, validation_expires_at),
    FOREIGN KEY (user_id, track_key)
        REFERENCES music_discovery_identities(user_id, track_key)
        ON DELETE CASCADE,
    FOREIGN KEY (source_run_id)
        REFERENCES music_recommendation_runs(run_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_feeds (
    feed_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    recommendation_date DATE NOT NULL,
    request_key CHAR(64) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    profile_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,
    generation_seed CHAR(64) NOT NULL,
    target_count SMALLINT UNSIGNED NOT NULL,
    actual_count SMALLINT UNSIGNED NOT NULL,
    saved_playlist_id VARCHAR(64) NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (feed_id),
    UNIQUE KEY discovery_feed_request (user_id, request_key),
    KEY discovery_feed_latest (user_id, created_at),
    KEY discovery_feed_date (user_id, recommendation_date),
    KEY discovery_feed_gc (created_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_feed_items (
    feed_id CHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    selection JSON NOT NULL,
    validated_at DOUBLE NOT NULL,
    first_exposed_at DOUBLE NULL,
    last_exposed_at DOUBLE NULL,
    exposure_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (feed_id, position),
    UNIQUE KEY discovery_feed_item_track (feed_id, track_key),
    UNIQUE KEY discovery_feed_item_recording (feed_id, recording_key),
    KEY discovery_feed_item_recording_lookup (recording_key),
    FOREIGN KEY (feed_id)
        REFERENCES music_discovery_feeds(feed_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_daily_usage (
    user_id BIGINT NOT NULL,
    usage_date DATE NOT NULL,
    token_limit INT UNSIGNED NOT NULL,
    model_call_limit INT UNSIGNED NOT NULL,
    network_request_limit INT UNSIGNED NOT NULL,
    actual_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    reserved_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    unconfirmed_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    model_calls INT UNSIGNED NOT NULL DEFAULT 0,
    network_requests INT UNSIGNED NOT NULL DEFAULT 0,
    next_refill_at DOUBLE NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, usage_date),
    KEY discovery_usage_date (usage_date),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

候选通过用户与 track_key 关联身份，来源任务删除仅置空。列表不外键引用候选，清理候选不影响历史。saved_playlist_id 为逻辑引用，服务端验证歌单用户归属。analysis 保存特征/置信度/证据/版本，validation 保存校验结论及有效期；selection 保存评分与画像版本。

### 8.3 画像主表目标结构

```sql
CREATE TABLE music_discovery_profiles (
    user_id BIGINT NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    built_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    profile_version VARCHAR(40) NOT NULL,
    settings JSON NOT NULL,
    long_term JSON NOT NULL,
    short_term JSON NOT NULL,

    evidence_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    control_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    agent_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    agent_source_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    agent_state JSON NULL,
    agent_updated_at DOUBLE NULL,
    learning_since DOUBLE NULL,
    next_review_at DOUBLE NULL,

    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,

    PRIMARY KEY (user_id),
    KEY discovery_profile_review (next_review_at, user_id),

    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

新增字段：evidence_revision 表示证据变化，control_revision 表示设置/纠正/重置等控制变化，agent_revision 为发布版本，agent_source_revision 为已评估证据版本，agent_state 为画像，agent_updated_at 为发布时间，learning_since 为重置后的学习起点，next_review_at 为下次调度时间。原规则画像字段保持兼容。

### 8.4 画像任务表

```sql
CREATE TABLE music_discovery_profile_runs (
    run_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    trigger_type VARCHAR(24) NOT NULL,

    source_revision BIGINT UNSIGNED NOT NULL,
    base_control_revision BIGINT UNSIGNED NOT NULL,
    base_agent_revision BIGINT UNSIGNED NOT NULL,
    input_hash CHAR(64) NOT NULL,

    policy_version VARCHAR(40) NOT NULL,
    skill_version CHAR(64) NOT NULL,

    input_snapshot JSON NOT NULL,
    proposal JSON NULL,
    context JSON NOT NULL,
    steps JSON NOT NULL,
    applied_version BIGINT UNSIGNED NULL,

    accounting_date DATE NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    worker_id VARCHAR(64) NULL,
    lease_until DOUBLE NULL,

    error_code VARCHAR(64) NULL,
    error_message VARCHAR(300) NULL,

    created_at DOUBLE NOT NULL,
    started_at DOUBLE NULL,
    finished_at DOUBLE NULL,
    updated_at DOUBLE NOT NULL,

    active_user_id BIGINT GENERATED ALWAYS AS (
        CASE
            WHEN status IN (
                'pending', 'running', 'usage_pending'
            )
            THEN user_id
            ELSE NULL
        END
    ) VIRTUAL,

    PRIMARY KEY (run_id),
    UNIQUE KEY discovery_profile_input (user_id, input_hash),
    UNIQUE KEY discovery_profile_active (active_user_id),
    KEY discovery_profile_queue (
        status, lease_until, created_at
    ),
    KEY discovery_profile_owner (
        user_id, created_at
    ),
    KEY discovery_profile_accounting (
        user_id, accounting_date
    ),

    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

状态：pending/running/succeeded/no_change/failed/stale/cancelled/interrupted/usage_pending。唯一 active_user_id 保证同用户最多一个待执行、执行中或待对账任务。input_hash 包含用户、证据/控制/基础画像版本、策略及输入，重复输入复用任务。context 保存预算与请求明细，单任务快照最多 512 KiB。

`profile_runs.active_user_id` 使用 VIRTUAL 生成列及唯一索引，保证同用户最多一条 pending/running/usage_pending 任务。当前共享 MySQL 不允许 STORED 生成列依赖带 ON DELETE CASCADE 的外键列；VIRTUAL 已在真实隔离库验证，保留账号删除级联与活动任务唯一性。

### 8.5 画像版本表

```sql
CREATE TABLE music_discovery_profile_versions (
    user_id BIGINT NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    base_version BIGINT UNSIGNED NOT NULL,
    source VARCHAR(16) NOT NULL,
    run_id CHAR(32) NULL,

    evidence_revision BIGINT UNSIGNED NOT NULL,
    control_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,

    snapshot JSON NOT NULL,
    changes JSON NOT NULL,
    evidence_summary JSON NOT NULL,
    summary VARCHAR(500) NOT NULL,

    created_at DOUBLE NOT NULL,

    PRIMARY KEY (user_id, version),
    KEY discovery_profile_version_time (
        user_id, created_at
    ),
    KEY discovery_profile_version_run (run_id),

    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

source 为 bootstrap/agent/user/reset/restore。run_id 为检查用户归属的逻辑关联，版本与用量独立保留；恢复历史只提取自动推断，结合当前明确设置和屏蔽生成递增的新版本。

### 8.6 画像 JSON 与现有表复用

```json
{
  "schema_version": 1,
  "policy_version": "profile-v1",
  "summary": "长期偏好中文歌曲，近期在专注场景中偏好舒缓纯音乐",
  "items": [
    {
      "dimension": "vocal_role",
      "value": "instrumental",
      "scope": "context",
      "context": "focus",
      "preference": 0.75,
      "confidence": 0.78,
      "source": "inferred",
      "support": {
        "recordings": 6,
        "days": 4
      },
      "evidence_refs": [
        "event:示例事件",
        "features:示例歌曲"
      ],
      "observed_at": "2026-10-24T10:00:00+08:00",
      "half_life_days": 60,
      "expires_at": null
    }
  ],
  "hypotheses": []
}
```

契约要求：

- `preference` 范围为 `-1..1`，表示减少推荐至偏好。
- `confidence` 范围为 `0..1`。
- `scope` 为 `long_term`、`recent`、`context`。
- `source` 为 `user`、`rule`、`inferred`。
- 同一维度、值、范围和场景唯一。
- 明确确认的偏好不自动衰减。
- 生效条目最多 120 项，待观察条目最多 30 项，画像 JSON 最大 128 KiB。
- 任务保留必要的证据摘要和快照，便于原始事件到期后解释历史变更。

现有表无其他 DDL：events.payload 扩展场景与操作事实；track_features 的有效特征变化递增画像证据版本；feedback 保留过滤依据；recommendation_runs JSON 保存画像快照、任务类型和请求账本；system_settings.config 增加开关、预算、间隔、并发；profiles.settings 增加 profile_learning_enabled。旧客户端未提交字段保持原值。

## 9. 一致性、恢复与清理

- 固定锁顺序：全局配额 → 用户 → 画像/每日预算 → 任务/列表；外部调用不持锁。任务与补充冷却时间同事务创建，模型预留及结算同步更新明细与汇总。
- 用户行锁和唯一幂等键控制多端换歌，发布列表和明细原子提交；实际曝光用于评估，分配记录用于防止立即重复。
- 画像任务在短事务冻结版本和证据，分页统计标注覆盖范围；不能处理部分数据却宣称全量完成。
- 发布画像检查账号、学习开关、worker、租约、基础控制版本与画像版本，校验证据；写版本、更新主表和已处理修订、结束任务同事务提交。
- 普通播放在模型执行期间发生，留给下一轮；明确设置、纠正或重置令旧提案 stale。暂停学习、账号禁用及取消阻止后续调用和画像发布。
- 重启扫描未派发任务，允许已保存提案继续确定性校验，不重发已发送模型请求；旧 worker 不得覆盖新租约结果。
- 原始事件及旧批次保留 180 天；新列表保留 30 天；候选保留 7 天且受数量上限约束；画像版本保留最近 180 天与最近 50 个的并集；已结算任务至少 180 天。
- 未知用量任务和相关汇总保留至对账，不因历史清理返还预算。清理分批执行、检查归属、跳过活跃任务和长期有效屏蔽。

## 10. Redis

本方案无新增或修改 key、类型、值结构、TTL、读写或失效规则。画像、队列、候选、预算和幂等均以 MySQL 为权威来源，不引入双写缓存或 Redis 推荐锁。

| 既有会话项目 | 规则 |
| --- | --- |
| 模板 / 示例 | videodream:session:{sid} / videodream:session:example-session-id |
| 类型与值 | Hash，uid/digest/version 及设备会话字段 |
| TTL | 沿用 604800 秒及账号续期规则 |
| 读写 | 账号模块登录/续期写入，受保护接口读取 |
| 失效 | 注销、过期、账号停用、认证版本变化 |

后台画像维护依据持久化用户身份、账号状态和学习开关校验；前台仍遵守会话权限。

## 11. 迁移、兼容与回滚

1. 已核对本地历史 V1～V3，使用 V4；不修改已执行迁移。
2. 等待活跃任务结算、停止相关写入，用共享环境工具备份项目库与配置；在隔离测试库验证迁移。
3. 为 profiles 增加字段与索引，新增六表；保留旧画像、收藏、反馈、批次与任务，初始化新版本字段。
4. 迁移入口在 V4 成功后幂等汇总旧每日任务各日期的实际/预留/未知占用，并检查跨日未确认请求；不混入场景歌单。合格旧歌曲经有效期及反馈检查后导入候选池，不调用模型或外部网络。
5. 更新应用结构门禁，执行 migrate/validate/结构检查，禁止新旧付费执行器并行。
6. 画像 Agent 默认开启，管理员可关闭，用户可暂停个人学习；新增字段为空时回退规则画像，旧客户端保留兼容响应和共享预算约束。

功能回滚：关闭 Agent 与融合，恢复旧结果展示，保留新数据和账本；回滚构建必须支持迁移后的结构门禁。原旧版本构建不能直接连接新增迁移后的数据库。

结构回滚：先备份新增数据，使用新 Flyway 补偿版本执行反向 DDL并部署匹配构建，或恢复匹配备份；须单独确认。不得直接删表、清账本、退还已发生配额。回退不支持每日预算的后端前，必须关闭付费生成并完成结算。

## 12. 实施范围与验收

实施涉及 discovery 的画像、候选、接口、调度；Agent 模型与预算适配、共享执行器；歌曲特征失效、音乐库行为、应用线程生命周期；账号/管理配置；首页、详情、播放事件与管理页；Flyway 迁移和测试。新增画像 Skill 位于 backend/skills/music-profile/SKILL.md。同步本文、移动音乐体验、Flyway 规格及规格索引，Android 原生代码不在范围内。

| 验收项 | 预期 |
| --- | --- |
| 连续换歌超过三次 | 不出现每日次数用完；有候选时零模型、零外部检索 |
| 收藏或有效播放 | 证据正确进入画像，下一批使用新规则版本并可验证排序变化 |
| 重复/乱序事件、循环播放 | 不重复计数、不无限放大偏好，异常播放不计负向 |
| 多端并发与幂等 | 同请求一个列表、同用户一个补充任务及一个画像有效任务 |
| 屏蔽、作品去重、完整音频 | 发布和读取持续遵守，候选不足不降低门槛 |
| 错误证据、用户纠正、重置 | 拒绝伪造引用及旧提案，历史恢复尊重当前明确设置 |
| 模型失败或预算耗尽 | 继续已有画像和算法换歌 |
| 取消、重启、跨日、未知用量 | 无重复付费、漏计或绕过；租约失效不能写回 |
| 移动端与主题 | 手机无封面、无横向溢出，操作完整 |
| 迁移和回滚 | 数据保留、门禁匹配、旧客户端兼容 |

测试默认模拟模型，使用共享 MySQL/Redis 的隔离资源并清理。本地仅运行一个后端8001，前端5174代理8001；8002按用户要求关闭，不再作为启动或验证目标。真实联调记录请求数、token、画像变化和推荐反馈；质量指标采用有效播放率、收藏率、跳过率、重复曝光率、每百次换歌 token 和候选不足率，不把测试通过当作准确率证明。

### 实施记录（2026-10-24）

- 新增 `pool`/`feeds`/`usage`，将算法换歌、后台补充与每日付费预算分开；原批次接口继续兼容，仍经过共享配额检查。
- 新增画像冻结、提案校验、版本控制、持久化任务扫描和租约恢复。自动评估触发需要 10 条新证据及 3 首录音；长期推断需要 3 首录音、2 天，单次强度变化最多 0.25。低可信结果只进入待观察。
- 首页六首、详情六首，提供画像确认/纠正/暂停/重评/历史恢复与重置。手机隐藏歌曲封面及辅助推荐说明，适配深浅主题。
- V4 增加六表及 profiles 的八个字段及一个索引；完整表结构、索引和外键见第 8 节与迁移 SQL。Redis 无新增或修改，原因及既有会话契约见第 10 节。
- 任务快照最多 450 KiB，为总任务 512 KiB 上限预留空间；最多纳入 1,000 条证据，覆盖不全显式标注。已有歌词/音频证据缺失时不补造维度，不在画像任务内抓取新资料。
- 验证覆盖连续六次无模型换歌（每次六首）、多端幂等、冷却、耗尽保留、屏蔽、校验过期、证据伪造、版本冲突、独立预算、跨日未知占用、模拟工具调用、已保存提案恢复及租约失效。迁移另验重复执行和生成列备份恢复。
- 后端全量 669 项通过；持续后台补充调整后，相关 109 项回归通过。前端 179 项测试及类型检查/生产构建通过。Chrome 使用隔离接口验证 1440px 桌面、390px 手机浅色/深色主题：首页六首、详情六首、换歌、保存、证据、确认偏好、模拟重新评估、场景表单和历史抽屉均通过，无脚本错误或横向溢出；未执行真实付费模型质量评估或实体手机验收。
- 本地共享 MySQL 已完成 V4 migrate/validate/门禁检查，原有 30 张业务表的 6,684 行旧数据逐行摘要核对不变。备份位于共享目录 `backups/videodream-discovery-v4-20261024-154434/`，包含项目 SQL、相关私有配置、SHA-256 清单与迁移核对报告。旧验证已过期，未强行导入候选；旧每日预算回填 4 行。
- 服务恢复在前端 5174、主后端 8001 和此前已有的第二后端 8002，健康及代理检查均为 200；本功能本身只需主后端。共享 MySQL/Redis 复用原实例，ES/MinIO 与其他项目专用组件未操作；测试资源按归属清理，包括本轮三个失败迁移遗留的空测试库。
- 按用户最新要求，画像 Agent 默认开启；管理员仍可关闭，用户仍可暂停个人学习。此开关仅调整已有 `system_settings.config` 的默认解析，不增加 MySQL 字段、索引、关系或 Redis key，也不覆盖已显式保存的关闭设置；无需新迁移，回退默认值或在管理页关闭即可。
- 按用户最新要求，新候选列表每批固定六首；少于六首不发布新批次，历史部分列表仍可继续播放并提示后台自动补充。工作线程持续检查低水位与补充到期时间，测试验证不再访问页面也会在补充间隔结束后自动排队，以及候选总量足够但歌手多样性不足时仍会补充。复用 profiles.updated_at 和既有 daily_usage.next_refill_at，无新表/列/索引/关系或 Redis 数据；历史批次和播放事件保持兼容，无数据迁移，回退选曲策略保留已发布列表。
- 真实运行确认扫描线程在补充间隔到期后自动创建并执行后台任务，2026-10-24 18:10 完成补充 16 首候选；随后读取的 19 首合格候选可按多样性规则选出完整六首。电脑休眠曾造成共享 MySQL/Redis 连接超时，唤醒后应用自行恢复扫描；未重启共享组件。候选不足与耗尽提示均明确由后台自动补充，仍遵守补充间隔、每日预算及播放校验，不承诺立即凑齐六首。运行快照保存在 `.run/daily-discovery-background-live.json`；本项仅调整提示并记录验证，无 MySQL/Redis 结构或数据契约变更。
- 随后用户复测发现空列表：一小时播放校验到期隐藏了已发布歌曲，同时每日网络额度耗尽，页面却未显示实际等待原因。现改为保留已发布歌曲元数据，恢复可用的历史完整列表，后台在新鲜候选足够时自动修复短列表；预算/间隔等待返回准确恢复时间。发布新批次、屏蔽与播放完整性检查仍保留。仅改变读取、自动分配及响应字段，不修改 MySQL 表、索引、关系、既有 JSON 持久化格式或 Redis key/类型/TTL/一致性；无需迁移或数据回滚，功能回退保留原列表和账本。
- 空列表修正后相关后端 114 项、前端 181 项及生产构建通过；测试覆盖过期候选拒绝新分配、已发布歌曲保留、历史六首恢复、屏蔽不被恢复绕过、后台补齐短列表、跨日预算自动恢复和幂等回放。原端口重启后，8001/8002 及 5174 代理的真实接口均返回六首已发布歌曲和 2026-10-25 00:00（上海）的补充恢复时间，临时验证会话已清理。浏览器使用该真实接口响应验证桌面及手机深浅主题；同时修复手机首页摘要样式隐藏第 4～6 首的问题，首页及详情均检查六首实际可见，手机额外确认第六首播放按钮可操作、滚动后六首均不被底部导航遮挡。前端已在原 5174 端口重启并核对新样式及代理健康，具体记录位于 `.run/daily-discovery-empty-page-ui/`。
- 2026-10-25 修正历史同录音重复及反馈补位，替代前述常驻后台恢复时间提示。相关后端 53 项、前端 182 项及生产构建通过，覆盖歌手反序、同平台多 ID、版本保护、历史反馈／收藏／曝光等价排除、单首候选补位、重复及并发反馈、保留旧事件位置、候选到货后后台补齐。原端口重启后实测等待既定间隔自动恢复补充，真实列表由三首合规旧歌加三首新候选恢复为六首，8001、8002、5174 返回同一批且无重复；未放宽有效期、屏蔽或预算，临时会话已删除。证据见 `.run/discovery-dedupe-tests.log`、`.run/discovery-dedupe-live.json`、`.run/discovery-dedupe-ui/results.json`。
- 同日完成移动端首页轮播。Chrome 触摸事件验证 390px 深浅主题及 320px 窄屏的左右滑动、圆点切换、第六首操作入口、第二／第六首反馈后保留位置、筛选单首／空态及恢复，确认隐藏卡片未提前曝光、无页面横向溢出；1440px 桌面保持网格、详情保持六首列表。生产构建通过，前端已在原 5174 重启；截图与记录位于 `.run/discovery-carousel-ui/`。未使用 Android 模拟器，未作实体设备验收。共享中间件保持原实例，隔离测试库、测试账号及 Redis 测试命名空间已清理，未新增专用组件。
- 随后按用户纠正，将首页轮播改为每页纵向三首、六首共两页，滑动一次切换整组三首。生产构建通过；Chrome 使用隔离接口及触摸事件验证 390px 深浅主题、320px 窄屏的每页实际可见三首、一次滑动切至后三首、两个圆点及页码、第二页反馈补位后保留当前页、筛选后的页数及空态、隐藏页不提前曝光，1440px 桌面保持六首网格；无页面横向溢出或脚本错误。前端已按原 5174 重启，截图与验收结果位于 `.run/discovery-carousel-three-ui/`，构建日志位于 `.run/discovery-carousel-three-build.log`。本轮仅调整前端布局及规格，无 MySQL／Redis 结构、契约或数据迁移，无新增中间件、专用组件或测试库；浏览器验证使用模拟接口，不写入业务数据，浏览器已关闭。
- 同日修正“换一批”因候选歌手分布不足而保留原列表的问题。现场15首可用候选中11首属于同一歌手，去重及每歌手两首上限后只能选出五首；后台补充先处理欠缺歌手，跳过不能增加可组批储备的复检／分析／校验，并使用计划内全部路线和后台任务的低收益历史。相关后端55项测试通过，包含过量同歌手、跨批去重、第四条检索路线、补齐后切换完整新六首、反馈补位、冷却和预算回归；日志位于 `.run/discovery-refill-diversity-tests.log`。部署后Chrome实际点击5174首页“换一批”，真实8001接口返回不同的六首、DOM同步、代理一致，390px保留三首一页、两页，无脚本错误或横向溢出；认证仅使用已清理的临时会话，曝光上报拦截，推荐接口未模拟，保留正常发布的新列表。截图及结果位于 `.run/discovery-refill-diversity-live/`。用户要求停用8002，已关闭且核对无监听；本地仅保留8001后端、5174前端，共享中间件及其他项目组件未调整。隔离测试库、账号、Redis测试命名空间和临时会话已清理，无MySQL／Redis结构变更或迁移。
- 同日进一步修复个人歌手计数、探索误判、多歌手检索覆盖及稀疏证据高分。相关65项后端测试通过，覆盖合作署名／装饰署名、保留项配额、个人曝光、语言不等于种子满分、其他已喜欢歌手仍可入选、五条路线间轮流校验、旧六首集中列表修复、歌词503降级、认证错误继续中止，以及恢复候选不重复搜索／不重置预算。真实补充检索了温暖、IU、薛之谦、EXO、旺仔小乔；第一次因歌词503暂停，修复后从该任务期间已保存的身份记录恢复69条候选元数据，沿用同一任务的剩余额度继续匹配及完整音频校验。累计125次网络、5次搜索、约120秒，新增17首有效候选，原冷却时间保留。真实六首列表已自动修复为周杰伦相关署名2首、张韶涵2首、IU和EXO各1首，没有熟悉歌手被标为探索；8001与5174代理一致，Chrome实际页面显示新六首及每页三首的两页结构。此项验证没有模拟推荐接口或播放校验，不代表长期收藏率／播放率已改善。原端口重启，8002保持关闭；隔离库、Redis测试资源和临时会话清理完毕。日志及证据见 `.run/discovery-artist-quality-tests.log`、`.run/discovery-artist-quality-live.json`、`.run/discovery-artist-quality-resume.json`；无MySQL结构或Redis契约迁移。
- 经用户确认，每日后台网络额度默认值及可配置上限由 480 调整为 5,000；后端默认解析、管理员请求校验、实际准入和管理页输入范围同步更新，显式较低配置保留。相关后端 45 项测试及前端生产构建通过，覆盖管理员允许 5,000/拒绝 5,001、耗尽保留推荐及跨日恢复。8001、8002、5174 按原端口重启，三个实际设置接口均返回 5,000、推荐接口均保留六首并报告后台执行中；账本核对快照为 530/5,000，原有 480 次消耗继续累计，新任务仍受单任务 160 次约束。共享中间件未调整，隔离测试资源及临时验证会话已清理。无 MySQL/Redis 结构或契约迁移；实测、估算边界和兼容回滚见第 6 节，运行验证记录位于 `.run/discovery-budget-5000-live.json`。
