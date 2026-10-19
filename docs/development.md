# 开发与验证

从项目根目录执行命令。开发地址固定为 `http://127.0.0.1:5174/`，后端固定为 `8001`，Vite 保留 `strictPort: true`。

## 环境

- Python 3.11+；可选本地 AI 修复依赖需要 Python 3.12+。
- Node.js 20.19+ 或 22.12+；前端测试使用支持 TypeScript 类型擦除的 Node，建议使用 Node 22.12+。
- ffmpeg/ffprobe、MySQL、Redis。
- Chrome/Edge 或 Playwright Chromium；浏览器解析时 Windows 后端使用单进程，避免 `--reload`。

## 首次初始化

Windows PowerShell 7：

```powershell
pwsh -File scripts/setup.ps1
Copy-Item .env.example .env.universal-video.local
# 编辑 .env.universal-video.local，填写数据库、Redis 和所需 AI 配置。
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
# 编辑配置后初始化管理员。
.venv/bin/python scripts/initialize-accounts.py --username admin
```

数据库与 Redis 需要事先可连接。管理员密码通过终端交互设置，无默认密码。配置优先级仍为进程环境变量、`.env.universal-video.local`、旧 `.env`；`config/database.json`、`config/ai.json` 继续作为已有兼容来源。

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

该脚本并行执行后端测试和前端测试/构建，最后执行 `git diff --check`。后端测试会创建独立的临时测试库，需本地 MySQL 账号具备建库、删库权限，并需要 Redis；测试不使用生产业务库作为测试库。

单独执行或在 macOS/Linux 执行：

```bash
.venv/bin/python -m unittest discover -s backend/tests -t . -p 'test_*.py'
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

Windows 将 Python 路径替换为所选环境的 `Scripts/python.exe`。Node 测试位于 `frontend/tests/`，Python 测试按业务位于 `backend/tests/`；单项测试可使用完整包名，例如 `backend.tests.video.test_summary`。
