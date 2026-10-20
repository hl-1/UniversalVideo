# VideoDream 云服务器部署

本文同时记录本次部署设计与操作步骤。配置统一放在 `deploy/`，适用于从 GitHub 拉取代码、云端全新初始化的环境。真实 env、私钥和证书单独上传，不写进镜像。

## 1. 已确定的环境与部署范围

| 项目 | 配置 |
| --- | --- |
| 云服务器 | Ubuntu、x86_64、4 核、约 8 GB 内存，部署前磁盘剩余约 32 GB |
| 公网 IP | `124.223.179.147` |
| 访问域名 | `https://xianyue.huanliyu.cn` |
| 仓库 SSH 地址 | `git@github.com:hl-1/UniversalVideo.git` |
| 服务器代码目录 | `/opt/videodream` |
| HTTPS 入口 | 现有 `aippt-nginx-1`，已有公网 80/443 |
| MySQL | 现有 `code-generator-mysql-1`，MySQL 8.4 |
| Redis | 现有 `code-generator-redis-1`，Redis 7 |
| 入口网络 | 已存在的 `huanliyu-ingress` |
| 数据库网络 | 已存在的 `code-generator_default` |

新增一个应用容器，Vue 构建页面由 FastAPI 同源提供，容器内部端口固定为 `8001`，不发布宿主机端口。应用同时接入以上两个网络，以 `videodream-app` 供 Nginx 解析。

镜像包含 Python 3.12、Node.js 22、FFmpeg/FFprobe、Playwright 对应版本的 Chromium。应用以 UID/GID `10001:10001`、单进程运行，初始限制 2 核、3 GB 内存；构建阶段不受这些运行限制。Redis 逻辑数据库共享内存及故障范围，Docker 共享网络也不是独立安全边界。

基础镜像支持 FFmpeg 快速填补；本地 LaMa AI 去水印按第 11 节额外安装。无桌面容器不能完成人工浏览器验证码，抖音遇到验证时需要另外规划有鉴权的远程浏览器操作。服务器访问目标网站与 AI 提供商的能力需要实测。

## 2. 文件说明

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 多阶段构建前端与后端 |
| `Dockerfile.dockerignore` | Dockerfile 专用构建上下文排除规则 |
| `compose.yml` | 应用、持久化、已有网络、资源限制与健康检查 |
| `compose.inpainting.yml` | 可选 CPU AI 去水印依赖构建覆盖 |
| `.env.example` | 云端运行配置示例，可提交 |
| `.env.production` | 真实运行配置，保存在本机并单独上传，Git 忽略 |
| `mysql-init.sql` | 空库及专用数据库账号初始化模板 |
| `nginx/xianyue.huanliyu.cn.conf` | HTTPS、反向代理、媒体 Range 请求配置 |
| `nginx/compose.override.yml` | 为现有 aippt Nginx 增加站点配置挂载 |
| `.gitignore` | 忽略实际 env、证书、私钥、本地 SQL 和备份 |

Docker 构建上下文为仓库根目录，必须保留 `deploy/Dockerfile.dockerignore`；它只允许应用构建输入进入上下文，排除本机环境、密钥、下载和运行数据。不要把项目根目录整体挂载到 `/app`，否则会覆盖镜像内的前端构建结果。

下文除“本机执行”外，命令均在服务器运行；所有应用 Compose 命令从 `/opt/videodream` 执行。

## 3. 从 GitHub 获取代码

用户先把代码及 `deploy/` 上传到自己的 GitHub 仓库。服务器已有 SSH 配置，使用 SSH 地址拉取，无需重新生成密钥。

服务器执行：

```bash
sudo install -d -o ubuntu -g ubuntu /opt/videodream
git clone git@github.com:hl-1/UniversalVideo.git /opt/videodream
cd /opt/videodream
docker compose version
docker network inspect huanliyu-ingress code-generator_default --format '{{.Name}}'
getent ahostsv4 xianyue.huanliyu.cn
```

目录应为空，且解析结果应包含 `124.223.179.147`。若已克隆，不要重新 clone 或删除目录；先核对仓库，再使用 `git pull --ff-only`。遇到错误停在当前步骤处理。

Docker 命令权限不足时，在对应命令前使用 `sudo`。准备媒体与运行目录：

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /opt/videodream/downloads /opt/videodream/.run
```

## 4. 初始化独立数据库与 Redis 分配

### MySQL

本机 `deploy/.env.production` 已准备好时，直接使用其中的 `MYSQL_PASSWORD` 创建云端账号，不要再次生成不同的密码。打开该文件查看，密码不写入公共模板或聊天记录。

仅在尚未准备真实配置、需要从示例首次初始化时，在自己的电脑上生成专用数据库密码并保存：

```bash
openssl rand -hex 24
```

服务器登录现有 MySQL（提示时输入现有 MySQL root 密码）：

```bash
docker exec -it -e MYSQL_HISTFILE=/dev/null \
  code-generator-mysql-1 mysql -u root -p
```

参考 `deploy/mysql-init.sql`，把下面密码替换成真实 env 中的 `MYSQL_PASSWORD`，再粘贴到 MySQL 终端：

```sql
CREATE DATABASE videodream
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'videodream_app'@'%' IDENTIFIED BY '替换为真实env中的MYSQL_PASSWORD';
GRANT ALL PRIVILEGES ON videodream.* TO 'videodream_app'@'%';
SHOW GRANTS FOR 'videodream_app'@'%';
EXIT;
```

SQL 与 env 中的密码必须一致。不要把替换后的真实密码保存到受版本控制的 SQL 模板。账号只授权 `videodream.*`；首次启动需要在该库内建表、修改结构和读写。对象已存在时先检查，不删除重建。

### Redis

当前观测只有 `db0` 有数据，暂定给 VideoDream 使用 `db1`。上线前确认现有项目配置未预留 `db1`，并检查：

```bash
docker exec code-generator-redis-1 redis-cli INFO keyspace
docker exec code-generator-redis-1 redis-cli -n 1 DBSIZE
```

第二条预期为 `0`，空库只表示当前没有 key，不能替代对现有项目配置的核对。如果已被使用，先选择并确认其他逻辑数据库，再修改自己的 env。若提示 `NOAUTH`，使用实例原有的认证方式；不要修改共享实例密码。

本次不改变 Redis 的全局持久化和淘汰策略。不要执行 `FLUSHALL`，不要清理其他项目的 key。

## 5. 单独上传真实 env

真实 `deploy/.env.production` 只保存在本机，GitHub 克隆不会携带它，需要单独上传。该文件已保存独立的云端数据库密码和 JWT 密钥；AI 分别显式配置三组服务地址、模型和密钥，各组可以单独修改。

| 用途 | 配置变量 | 服务地址 | 模型 |
| --- | --- | --- | --- |
| 文字总结与思维导图 | `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` | `https://api.deepseek.com/v1` | `deepseek-flash` |
| 截图识别 | `LLM_VISION_BASE_URL`、`LLM_VISION_MODEL`、`LLM_VISION_API_KEY` | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-VL-8B-Instruct` |
| 语音转写 | `LLM_ASR_BASE_URL`、`LLM_ASR_MODEL`、`LLM_ASR_API_KEY` | `https://api.siliconflow.cn/v1` | `FunAudioLLM/SenseVoiceSmall` |

文字组使用用户提供的 DeepSeek 密钥，其模型已通过 `/models` 接口确认可用。截图和转写组分别保存原有硅基流动密钥，配置不引用文字组变量，修改文字服务不会覆盖它们；两组也可以分别更换密钥或服务。模型实际调用还取决于服务额度和权限。

**已有真实配置时直接上传，不要用示例覆盖，也不要重新生成固定密钥。**

只有从示例首次准备配置时，才在本机执行以下命令；第一条复制命令会保留已有文件：

```bash
cd /Users/hl/Documents/Project/UniversalVideo
test -f deploy/.env.production || cp deploy/.env.example deploy/.env.production
openssl rand -hex 48
```

编辑 `deploy/.env.production`：

- `MYSQL_PASSWORD` 与新数据库账号密码一致。
- `VIDEODREAM_JWT_SECRET` 必须是已固定保存的随机密钥；从示例首次配置时才填入生成的 96 个十六进制字符。初始化脚本发现该值为空时会尝试写文件，而容器内挂载是只读，因此必须事先填好。
- 保留 `VIDEODREAM_COOKIE_SECURE=true`、`VIDEODREAM_ORIGINS=https://xianyue.huanliyu.cn`。
- MySQL、Redis 地址使用示例中的容器名称，不能填 `127.0.0.1`。
- `REDIS_DATABASE` 使用已核对的库编号。当前 Redis 无需认证时密码留空；以后启用认证时填写实例对应密码。
- AI 每组都有独立的地址、模型、密钥，修改时只修改对应组；三个配置字段均应显式保留，不使用 `${LLM_API_KEY}` 等跨组引用。空密钥意味着该组不可用，不会借用文字服务密钥。也可删除整组 env 配置，改用登录后的管理员配置页管理该组。

上传（本机执行）：

```bash
scp deploy/.env.production \
  ubuntu@124.223.179.147:/opt/videodream/deploy/.env.production
```

服务器执行：

```bash
sudo chown ubuntu:10001 /opt/videodream/deploy/.env.production
chmod 640 /opt/videodream/deploy/.env.production
```

Compose 把该文件只读挂载到 `/app/.env.universal-video.local`，由项目按 dotenv 规则读取，避免 Compose 对密钥中的 `$` 进行展开。不要同时把本机旧 env 或旧 `config/ai.json` 上传进镜像。

## 6. 构建、检查连接与初始化管理员

```bash
cd /opt/videodream
docker compose -f deploy/compose.yml config -q
docker compose --progress plain -f deploy/compose.yml build app
```

首次构建会下载镜像、Python/npm 依赖和 Chromium；建议低峰执行。GitHub SSH 已配置不代表镜像仓库、npm、PyPI 和浏览器下载都可访问。若失败，先按实际错误修复网络或依赖问题。

前端 npm 安装统一使用 `https://registry.npmmirror.com`，保留锁文件版本及完整性校验，设置 60 秒请求超时和 5 次连接重试；构建时可通过 `--build-arg NPM_REGISTRY=https://其他镜像` 选择软件源。

镜像内的 Debian 软件包源使用阿里云 `https://mirrors.aliyun.com/debian` 和 `https://mirrors.aliyun.com/debian-security`，沿用 Bookworm 的发行版本与 Debian 原有签名校验。目标服务器下载 2 MB 索引样本时实测约 11.3 MB/s，默认源在 20 秒内连接超时；后续速度以实际构建日志为准。Playwright 安装系统依赖也使用这组软件源。

Python 依赖（含可选 AI 去水印依赖）使用清华源 `https://pypi.tuna.tsinghua.edu.cn/simple`，设置 60 秒网络超时和 5 次连接重试。需要另选软件源时，可在构建命令中传入 `--build-arg PIP_INDEX_URL=https://其他镜像/simple`。该参数在 Debian 安装步骤之后声明，切换 Python 源可以复用前面的系统依赖安装缓存。

Python 包、Playwright 系统依赖和 Chromium 下载分别构建为独立步骤；浏览器下载失败后重试，可以复用已经成功的依赖安装步骤。Playwright 安装系统依赖时，APT 使用 60 秒网络超时和 3 次重试。

Chromium、无头浏览器和 Playwright FFmpeg 使用国内 `https://npmmirror.com/mirrors/playwright` 下载源，由已锁定的 Playwright 版本选择对应浏览器版本，下载空闲超时设为 120 秒。构建参数 `PLAYWRIGHT_DOWNLOAD_HOST` 可指定其他兼容镜像，`PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT` 可调整超时（毫秒）。例如传入 `--build-arg PLAYWRIGHT_DOWNLOAD_HOST=` 可恢复 Playwright 官方下载源。浏览器参数在系统依赖安装后声明，更换浏览器源时可以复用前面的安装缓存。

如果旧版本构建仍在运行，先在该终端按 `Ctrl+C` 停止本次构建，然后拉取更新并重试：

```bash
cd /opt/videodream
git pull --ff-only
docker compose --progress plain -f deploy/compose.yml build app
```

构建时保留缓存，无需添加 `--no-cache` 或清理 Docker 缓存。

npm 和 Python 下载目录使用 BuildKit 缓存挂载，依赖步骤失败后重试也可复用已缓存的包，缓存保留在构建器中。缓存挂载不会写入最终应用镜像；单个下载中断的文件仍可能需要重新下载。构建需使用支持缓存挂载的 Docker BuildKit，本文的 `docker compose build` 使用该构建方式。

检查固定 JWT 配置与 Redis 连接（不显示密钥）：

```bash
docker compose -f deploy/compose.yml run --rm --no-deps app \
  python -c "from backend.modules.accounts import service as a; key=a.signing_key(); assert not key.startswith('REPLACE_'); assert a.redis_client().ping(); print('JWT / Redis OK')"
```

检查浏览器：

```bash
docker compose -f deploy/compose.yml run --rm --no-deps app \
  python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); print('Chromium OK'); b.close(); p.stop()"
```

初始化管理员，这一步会连接 MySQL，执行项目现有的建表与初始化：

```bash
docker compose -f deploy/compose.yml run --rm --no-deps app \
  python scripts/initialize-accounts.py --username admin
```

交互输入两次 8～128 个字符的管理员密码，无默认密码，公开注册默认关闭。脚本输出的 `127.0.0.1:5174` 是开发地址，云端实际使用 HTTPS 域名。

启动应用：

```bash
docker compose -f deploy/compose.yml up -d app
docker compose -f deploy/compose.yml ps
docker compose -f deploy/compose.yml logs --tail=100 app
```

健康状态变为 `healthy` 后，再配置入口。缺少 env 或持久化目录时，Compose 会报错，不会自动把文件路径创建为目录。

## 7. 单独上传并安装证书

本机已有压缩包，执行：

```bash
scp /Users/hl/Documents/Project/ssl/xianyue.huanliyu.cn_nginx.zip \
  ubuntu@124.223.179.147:~/
```

服务器执行：

```bash
chmod 600 ~/xianyue.huanliyu.cn_nginx.zip
mkdir -p /opt/videodream/deploy/certs
chmod 700 /opt/videodream/deploy/certs
python3 -m zipfile -e ~/xianyue.huanliyu.cn_nginx.zip \
  /opt/videodream/deploy/certs
chmod -R go-rwx /opt/videodream/deploy/certs

sudo install -m 0644 \
  /opt/videodream/deploy/certs/xianyue.huanliyu.cn_nginx/xianyue.huanliyu.cn_bundle.crt \
  /opt/ai-ppt-generator/certs/xianyue.huanliyu.cn_bundle.crt

sudo install -m 0600 \
  /opt/videodream/deploy/certs/xianyue.huanliyu.cn_nginx/xianyue.huanliyu.cn.key \
  /opt/ai-ppt-generator/certs/xianyue.huanliyu.cn.key
```

复用 Nginx 已有的 `/opt/ai-ppt-generator/certs:/etc/nginx/certs:ro` 挂载。已检查的证书覆盖 `xianyue.huanliyu.cn`，到期时间为北京时间 **2027-01-18 13:59:59**。续签时替换服务器上的这两个文件，再执行 `docker exec aippt-nginx-1 nginx -t` 和 `docker exec aippt-nginx-1 nginx -s reload`。

## 8. 接入现有 Nginx

`deploy/nginx/compose.override.yml` 只给现有 `nginx` 服务增加配置文件挂载，不替换原有证书、端口、页面或网络。首次重建 Nginx 会让已有两个网站短暂断连。

先验证 Compose 合并与 Nginx 语法、证书：

```bash
docker compose -p aippt \
  -f /opt/ai-ppt-generator/docker-compose.prod.yml \
  -f /opt/videodream/deploy/nginx/compose.override.yml \
  config -q

docker compose -p aippt \
  -f /opt/ai-ppt-generator/docker-compose.prod.yml \
  -f /opt/videodream/deploy/nginx/compose.override.yml \
  run --rm --no-deps --entrypoint nginx nginx -t
```

只有两个检查成功后再执行：

```bash
docker compose -p aippt \
  -f /opt/ai-ppt-generator/docker-compose.prod.yml \
  -f /opt/videodream/deploy/nginx/compose.override.yml \
  up -d --no-deps --no-build --force-recreate nginx

docker exec aippt-nginx-1 nginx -t
docker logs --tail=100 aippt-nginx-1
```

这份 override 必须继续用于以后的 aippt Compose 重建；只使用原文件重建会移除本次挂载。若希望以后沿用原有单文件运维命令，可以先备份原 Compose，把下面一条挂载永久添加到原 `nginx.volumes`，之后不再传 override：

```yaml
- /opt/videodream/deploy/nginx/xianyue.huanliyu.cn.conf:/etc/nginx/conf.d/xianyue.huanliyu.cn.conf:ro
```

不要同时重复添加。修改现有 aippt 文件前应按该项目自己的修改约定确认。配置使用 Docker DNS 动态解析 `videodream-app:8001`，应用容器重建后不需要跟随修改 IP。

## 9. 上线验证

```bash
curl --silent --show-error --output /dev/null --dump-header - http://xianyue.huanliyu.cn
curl --fail --silent --show-error --output /dev/null --write-out 'HTTPS %{http_code}\n' https://xianyue.huanliyu.cn
curl --fail --silent --show-error https://xianyue.huanliyu.cn/api/health
```

预期 HTTP 跳转到 HTTPS、HTTPS 证书验证成功、健康接口返回 `{"status":"ok"}`。不使用 `curl -k` 跳过证书检查。

浏览器打开 `https://xianyue.huanliyu.cn`，验证：

1. 管理员登录、刷新后恢复登录、退出。
2. 创建普通用户并登录；普通用户不能查看其他用户的数据。
3. 音乐搜索、播放、拖动进度和下载。
4. 视频解析、下载、预览，以及从网页保存到自己的电脑。
5. 已配置的 AI 总结、导图、截图识别或转写。
6. 原有 `huanliyu.cn`、`code.huanliyu.cn` 仍能正常访问。

健康接口只证明应用存活，不检查数据库和 Redis。上述登录、初始化和业务验证用于确认完整链路。媒体认证依赖 Service Worker，公网必须使用 HTTPS。

测试数据持久化前先等在途任务完成，然后重建应用：

```bash
cd /opt/videodream
docker compose -f deploy/compose.yml up -d --no-build --force-recreate app
```

重新登录，确认账号、历史和下载文件仍在。下载文件位于服务器 `/opt/videodream/downloads`，用户另存为后才进入自己的电脑。重启可能中断运行中的任务。

## 10. 更新、备份和回滚

### 更新

先等待任务完成，备份 `videodream` 数据库、`downloads/`、`.run/` 和 `deploy/.env.production`，再执行：

```bash
cd /opt/videodream
docker image tag videodream:local videodream:previous
git pull --ff-only
docker compose -f deploy/compose.yml build app
docker compose -f deploy/compose.yml up -d --no-build --force-recreate app
docker compose -f deploy/compose.yml logs --tail=100 app
```

实际 env、媒体和运行目录已被 Git 忽略，拉取代码不覆盖这些数据。重新上传 env 后也执行 `up -d --no-build --force-recreate app`，让单文件挂载重新绑定。只修改 Nginx 站点配置后先 `nginx -t`，通过再 `nginx -s reload`。

### 备份

数据库可在现有 MySQL 容器内交互导出，不把密码放进命令行：

```bash
mkdir -p /opt/videodream/deploy/backups
chmod 700 /opt/videodream/deploy/backups
docker compose -f deploy/compose.yml stop app

docker exec -it code-generator-mysql-1 sh -c \
  'umask 077; mysqldump -u root -p --single-transaction --no-tablespaces --set-gtid-purged=OFF videodream > /tmp/videodream-backup.sql'
```

上条成功后再复制与清理，确认文件非空：

```bash
docker cp code-generator-mysql-1:/tmp/videodream-backup.sql \
  /opt/videodream/deploy/backups/videodream.sql
test -s /opt/videodream/deploy/backups/videodream.sql
docker exec code-generator-mysql-1 rm /tmp/videodream-backup.sql

sudo tar -C /opt/videodream -czf \
  /opt/videodream/deploy/backups/videodream-files.tar.gz \
  downloads .run deploy/.env.production
sudo chmod 600 /opt/videodream/deploy/backups/*

docker compose -f deploy/compose.yml up -d --no-build app
```

上述文件名用于单次备份，再次执行前将已有备份移到带日期的独立位置，避免覆盖。磁盘仅剩约 32 GB，不适合长期在本机累积完整媒体备份；确认备份后转存其他磁盘或对象存储。项目初始化生成的局部备份不能替代业务备份。Redis 会话丢失后重新登录即可，恢复业务以 MySQL 和媒体文件为准。

### 回滚

仅应用镜像回滚：

```bash
cd /opt/videodream
docker image tag videodream:previous videodream:local
docker compose -f deploy/compose.yml up -d --no-build --force-recreate app
```

若更新包含数据库迁移，必须使用更新前的数据库和文件备份恢复匹配状态，不只回滚镜像。恢复时先停止应用、核对备份，在现有 MySQL 内仅恢复 `videodream` 库；恢复 `.run`、`downloads` 和 env 后，确保持久化目录仍归 UID/GID 10001。恢复流程不要操作其他项目数据库。

首次接入 Nginx 失败且使用的仍是独立 override 时，使用原 aippt Compose 重建 Nginx 即可撤掉新增站点挂载：

```bash
docker compose -p aippt \
  -f /opt/ai-ppt-generator/docker-compose.prod.yml \
  up -d --no-deps --no-build --force-recreate nginx
```

若已把挂载永久写入原文件，先恢复事前备份或撤销该条挂载，再执行以上命令。原网站恢复后检查访问。停止 VideoDream 使用 `docker compose -f deploy/compose.yml stop app`，保留独立数据库和持久化文件排查。

## 11. 可选 CPU AI 去水印

启用时，每次构建和重建都使用两份应用 Compose 文件：

```bash
cd /opt/videodream
docker compose -f deploy/compose.yml -f deploy/compose.inpainting.yml build app
docker compose -f deploy/compose.yml -f deploy/compose.inpainting.yml \
  run --rm --no-deps app python scripts/setup-watermark-model.py --mirror
docker compose -f deploy/compose.yml -f deploy/compose.inpainting.yml \
  up -d --no-build --force-recreate app
```

模型固定版本并校验 SHA-256，保存至持久化的 `.run/models/`，不会打包进镜像。当前服务器按 CPU 运行，不承诺实时处理速度；负载较高时减少同时任务。后续更新也保留 `-f deploy/compose.inpainting.yml`，否则基础构建会移除可选依赖。

## 12. MySQL / Redis 数据契约与兼容性

本次只增加部署文件，不调整任何业务表的字段、索引或关系。云端为空库，启动时通过现有 [`backend/migrations/__init__.py`](../backend/migrations/__init__.py) 协调器创建项目已有的 22 张表并执行已有迁移：

| 模块 | 现有表 | 完整表结构与索引来源 |
| --- | --- | --- |
| 音乐库 | `music_profile`、`music_tracks`、`music_favorites`、`music_history`、`music_playlists`、`music_playlist_tracks`、`music_downloads`、`music_track_aliases`、`music_lyrics`、`music_theme` | [`library.py`](../backend/modules/music/library.py) 的 `SCHEMA`、`migrate_schema()`；账号归属由下行迁移补齐 |
| 账号与资源 | `users`、`system_settings`、`audit_logs`、`resources`、`account_migrations` | [`service.py`](../backend/modules/accounts/service.py) 的 `AUTH_SCHEMA`、`migrate()` |
| 视频 | `video_recent_links`、`video_parse_records`、`video_summaries`、`video_mind_maps`、`video_view_states` | [`store.py`](../backend/modules/video/store.py)；[现有视频规格](../spec/VideoDream-视频模块持久化功能设计.md) |
| 场景歌单 | `music_recommendation_runs`、`music_track_features` | [`agent/store.py`](../backend/modules/music/agent/store.py)；该迁移也补齐歌单曲目位置列与索引 |

音乐曲目与歌单、下载、歌词存在已有外键关联；Agent 表关联用户、来源任务及已保存歌单。视频关联由已有业务事务维护。本次不复制另一套建表 SQL，避免与自动迁移定义不一致。MySQL 8.4 支持项目使用的 JSON；空库无历史业务数据迁移，初始化日志和备份写入独立 `.run/`。

Redis 沿用已有 key、类型、值、TTL、读写与失效规则，唯一部署差异是连接专用逻辑数据库。以下为已有契约，非本次新增：

| Key 模板（示例） | 类型与值 | TTL / 读写与失效 |
| --- | --- | --- |
| `videodream:session:{sid}`（`videodream:session:abc...`） | Hash：`uid`、刷新凭证 `digest`、账号 `version` | 最长 604800 秒；登录写入，认证和刷新读取，刷新原子轮换摘要但不延长会话；退出、密码重置、停用撤销 |
| `videodream:user-sessions:{uid}`（`videodream:user-sessions:1`） | Set：该用户的会话 ID | 登录时设置/更新 604800 秒；登出移除会话，批量撤销删除对应会话及索引 |
| `videodream:rate:{sha256}`（`videodream:rate:9f...`） | String 整数计数 | 原子自增，首写设置现有限流窗口；登录 900 秒，注册 3600 秒，过期自动失效 |
| `videodream:video:parse:{uid}:{url_hash}`（`videodream:video:parse:1:ab...`） | String JSON：解析结果与临时媒体地址 | 最长 600 秒且不超过源地址有效期；重新解析/清除会话失效，过期重新获取 |
| `videodream:video:preview:{uid}:{token}`（`videodream:video:preview:1:xy...`） | String JSON：用户、媒体地址、referer、到期时间、解析记录 ID | 最长 600 秒；读取检查账号、到期及关联记录，过期重建 |
| `videodream:video:worker:{worker_id}`（`videodream:video:worker:host:1:abc...`） | String JSON：`{"alive":true}` | 45 秒，每 10 秒续期，正常关闭删除 |

MySQL 是长期数据来源，Redis 不可用时认证拒绝放行；缓存丢失后重新解析，登录会话丢失后重新登录。总结、字幕、导图及任务结果继续按现有事务写入 MySQL，不增加新的缓存一致性规则。完整已有契约见账号代码及视频规格。本次无应用表/key 定义变更，因此无需新增表迁移或 key 转换脚本；部署回滚保留独立库与文件，后续版本升级按第 10 节匹配备份。
