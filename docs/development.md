# 开发与验证

从项目根目录执行命令。开发地址固定为 `http://127.0.0.1:5174/`，后端固定为 `8001`，Vite 保留 `strictPort: true`。

## 环境

- Python 3.11+；可选本地 AI 修复依赖需要 Python 3.12+。
- Node.js 20.19+ 或 22.12+；前端测试使用支持 TypeScript 类型擦除的 Node，建议使用 Node 22.12+。
- ffmpeg/ffprobe、MySQL、Redis；本地 Flyway CLI 需要 Java 21+。
- Chrome/Edge 或 Playwright Chromium；浏览器解析时 Windows 后端使用单进程，避免 `--reload`。

## 首次初始化

Windows PowerShell 7：

```powershell
pwsh -File scripts/setup.ps1
Copy-Item .env.example .env.universal-video.local
# 编辑 .env.universal-video.local，填写数据库、Redis 和所需 AI 配置。
.\.venv\Scripts\python.exe scripts/setup-flyway.py
# 以下 migrate 仅用于空库；存量库先按后文接管。
.\.venv\Scripts\python.exe scripts/database.py migrate
.\.venv\Scripts\python.exe scripts/initialize-accounts.py --username admin
```

只在本地配置文件不存在时复制示例，避免覆盖已有凭证。脚本创建 `.venv`，安装后端依赖并执行前端 `npm ci`。没有本机浏览器时另行执行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
npm --prefix frontend ci
cp .env.example .env.universal-video.local
# 编辑配置。先安装 Java 21+，再安装项目固定版本 Flyway。
.venv/bin/python scripts/setup-flyway.py
# 以下 migrate 仅用于空库；存量库先按后文接管。
.venv/bin/python scripts/database.py migrate
.venv/bin/python scripts/initialize-accounts.py --username admin
```

数据库与 Redis 需要事先可连接。管理员密码通过终端交互设置，无默认密码。配置优先级仍为进程环境变量、`.env.universal-video.local`、旧 `.env`；`config/database.json`、`config/ai.json` 继续作为已有兼容来源。

## 数据库版本管理

目录为 `backend/migrations/sql/V1__baseline.sql` 和 `deploy/sql/prod/v1/`。后者包含 001～003 正向 SQL 与 997～999 对应回滚 SQL；迁移文件执行后不可修改。完整结构、索引、关系和回滚约定见 [Flyway 规格](../spec/Flyway数据库版本管理设计.md)。

后续变更新增 `backend/migrations/sql/V2__描述.sql` 等递增版本，在 `deploy/sql/prod/v2/` 提供对应发布和回滚脚本，并更新 `backend/migrations/__init__.py` 中应用要求的 `SCHEMA_VERSION`。`schema_v1.json` 保留为首次接管 V1 的结构契约。发布顺序为停服、备份、migrate、validate、check、启动；回滚按规格使用补偿版本或恢复匹配备份。

存量库接入：先等待任务完成并停服，备份数据库、downloads 和 .run，再使用项目 Python 执行：

```bash
.venv/bin/python scripts/database.py preflight
.venv/bin/python scripts/database.py baseline
.venv/bin/python scripts/database.py migrate
.venv/bin/python scripts/database.py check
```

`preflight` 只读校验完整 V1 结构，不补字段；`baseline` 再次校验并先生成权限为 0600 的数据库 SQL 备份，然后登记 V1，不执行 V1 内的初始化数据。已有管理员密码、配置和用户记录保留。缺表或缺字段时，使用接管前的兼容代码先完成旧结构迁移，不能强行登记基线。

历史数据转换使用 `scripts/database.py legacy-data`，必须停服；该命令要求结构已完整，先备份再显式执行原有归属、视频和文件分类逻辑，不运行 DDL。用户目录搬迁仍使用 `migrate-user-storage.py`。新库无需执行 legacy-data。管理员脚本只设置密码，数据库状态统一使用 `scripts/database.py check`；旧 Agent 结构迁移/回滚脚本及其版本标记写入方法已删除。

配置优先级沿用现有规则，也可使用 `--config <dotenv路径>`。可选 `VIDEODREAM_MIGRATION_USER/PASSWORD` 提供独立迁移身份；密码仅传给子进程环境变量。启动应用只检查结构，不自动安装或执行 Flyway。后端 8001、前端 5174 保持原端口。

当前结构版本为 V4，包含每日发现候选池和音乐画像；`database.py migrate` 在 DDL 成功后幂等回填旧每日任务预算和有效候选，执行期间应用须停止。画像 Agent 默认开启，受管理员开关、用户学习开关、证据阈值及独立每日预算约束；可在管理页关闭。模型测试使用模拟响应，数据库测试复用共享 MySQL 的隔离库和 Redis 测试前缀，禁止测试脚本启动额外中间件。完整数据与回滚规则见[今日推荐规格](../spec/features/daily-music-discovery.md)。

## 日常启动

Windows 在两个终端分别执行：

```powershell
pwsh -File scripts/start-backend.ps1
pwsh -File scripts/start-frontend.ps1
```

启动脚本不安装依赖，端口已占用时直接报告，不启动第二份服务。后端启动时优先检查 `.venv`；解释器不存在或不可运行时，兼容使用 `.run/startup-venv`，并打印实际路径。两者都不可用时要求初始化环境。也可使用 `-PythonPath` 明确指定项目解释器。

macOS/Linux 在两个终端分别执行：

```bash
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
npm --prefix frontend run dev
```

访问 `http://127.0.0.1:5174/`。健康检查为 `http://127.0.0.1:8001/api/health`，接口文档为 `http://127.0.0.1:8001/docs`。前端构建后，后端继续提供构建页面及 `auth-worker.js`。

重启服务前核对占用端口的进程确属本项目。其他程序占用时先处理冲突，不自动终止或改端口。

## 验证

Windows 统一入口：

```powershell
pwsh -File scripts/test.ps1
```

该脚本并行执行后端测试和前端测试/构建，最后执行 `git diff --check`。后端测试会创建独立的临时测试库，需本地测试 MySQL 账号具备建库、删库权限、已安装固定版本 Flyway，并需要 Redis。Flyway 权限隔离测试还需在专用测试 MySQL 上创建、授权和删除临时账号；测试不使用生产业务库作为测试库。

单独执行或在 macOS/Linux 执行：

```bash
.venv/bin/python -m unittest discover -s backend/tests -t . -p 'test_*.py'
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

Windows 将 Python 路径替换为所选环境的 `Scripts/python.exe`。Node 测试位于 `frontend/tests/`，Python 测试按业务位于 `backend/tests/`；单项测试可使用完整包名，例如 `backend.tests.video.test_summary`。
