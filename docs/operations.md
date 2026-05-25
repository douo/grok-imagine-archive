# 运维手册

这份文档面向“已经决定把项目跑起来，并长期维护”的场景。它重点回答四个问题：

1. 线上或本机如何稳定运行
2. 如何判断一次备份是否真的完成
3. 出现异常时先看哪里
4. Web UI 如何安全暴露

## 1. 运行前提

部署前先确认：

- `config/accounts.toml` 位于持久化存储
- `archive/` 位于持久化存储
- 账号 cookie 已验证可用
- 运行机器有足够磁盘空间

建议把配置和归档目录放在不同于代码仓库生命周期的路径上，避免代码更新影响数据。

## 2. 标准运行流程

### 2.1 本机前台运行

```bash
uv run grok-downloader auth check --account andy
uv run grok-downloader sync --account andy --full --download-concurrency 8
uv run grok-downloader verify --account andy
GROK_DOWNLOADER_WEB_TOKEN='replace-with-long-random-token' \
  uv run grok-downloader web --account andy --host 127.0.0.1 --port 7860
```

这套流程适合第一次接入或人工值守。

### 2.2 后台运行 Web UI

```bash
umask 077
python - <<'PY' > archive/accounts/andy/web-token.txt
import secrets
print(secrets.token_urlsafe(32))
PY
GROK_DOWNLOADER_WEB_TOKEN="$(cat archive/accounts/andy/web-token.txt)" \
  setsid -f sh -c 'cd /path/to/grok-downloader && exec .venv/bin/grok-downloader web --account andy --host 127.0.0.1 --port 7860 > archive/accounts/andy/logs/web.log 2>&1 < /dev/null'
```

说明：

- `umask 077` 保证 token 文件权限收紧
- token 建议放在账号归档目录下，便于与该账号同生命周期管理
- 日志写入 `archive/accounts/andy/logs/web.log`

## 3. 健康检查

### 3.1 备份是否完成

最小检查集：

```bash
uv run grok-downloader status --account andy
uv run grok-downloader verify --account andy
```

健康标准：

- `failed=0`
- `missing=0`
- `hash_mismatches=0`
- 最近一次 `sync_run.status` 为 `ok`

### 3.2 Web 是否正常

```bash
curl http://127.0.0.1:7860/healthz
curl -H "x-access-token: $GROK_DOWNLOADER_WEB_TOKEN" http://127.0.0.1:7860/api/status
```

判断原则：

- `/healthz` 返回 200 说明进程在线
- `/api/status` 返回 200 说明鉴权与只读数据库访问正常

### 3.3 日志与文件检查

建议优先看：

- `archive/accounts/{alias}/logs/web.log`
- `archive/accounts/{alias}/metadata/failures/`
- `archive/accounts/{alias}/metadata/pages/`

这三处分别对应：

- Web 进程日志
- 明确失败项目
- 原始 API 响应，适合排接口问题

## 4. Web 访问控制

### 4.1 默认策略

当 Web UI 绑定在以下 loopback 地址时，可不强制 token：

- `127.0.0.1`
- `localhost`
- `::1`

当绑定到非 loopback 地址，例如 `0.0.0.0`，必须显式提供：

- `GROK_DOWNLOADER_WEB_TOKEN`

除非你非常确定环境隔离无风险，否则不要使用 `--allow-unauthenticated`。

### 4.2 浏览器访问方式

首访可用：

```text
http://127.0.0.1:7860/?token=你的访问令牌
```

系统会写入 HTTP-only cookie，后续浏览器再请求媒体文件时不需要手工带 token。

### 4.3 API 访问方式

```bash
curl -H "x-access-token: $GROK_DOWNLOADER_WEB_TOKEN" \
  http://127.0.0.1:7860/api/status
```

或者：

```bash
curl "http://127.0.0.1:7860/?token=$GROK_DOWNLOADER_WEB_TOKEN"
```

## 5. Docker 运行

### 5.1 Compose 方式

```bash
cp docker-compose.example.yml docker-compose.yml
docker compose up --build
```

### 5.2 单次命令方式

```bash
docker build -t grok-downloader:local .
docker run --rm \
  -v "$PWD/archive:/data/archive" \
  -v "$PWD/config:/app/config:ro" \
  -e GROK_DOWNLOADER_ARCHIVE=/data/archive \
  grok-downloader:local \
  grok-downloader status --account andy
```

运维原则：

- `archive/` 与 `config/` 必须通过挂载注入
- 凭据和归档不要封装进镜像
- 如果对外发布，前面必须有认证代理和 TLS

## 6. 日常巡检建议

如果这是一个长期运行的归档环境，建议定期执行：

```bash
uv run grok-downloader sync --account andy --full --download-concurrency 8
uv run grok-downloader verify --account andy
uv run grok-downloader status --account andy --json
```

建议关注这些指标的变化：

- `posts`
- `downloaded`
- `failed`
- `missing`
- `latest_run.status`

如果 `posts` 增长而 `downloaded` 不增长，通常说明新增的是重复索引或仍待下载的媒体，需要进一步检查明细。

## 7. 故障处理

### 7.1 认证失败

现象：

- `auth check` 失败
- `sync` 一开始就报 HTTP 错误

排查顺序：

1. `sso` 是否过期
2. `cf_clearance` 是否失效
3. `user_agent` 是否与获取 cookie 时差异过大
4. 是否需要代理

### 7.2 同步失败

现象：

- `sync` 退出码非 0
- `sync_runs.status = failed`

排查顺序：

1. 看控制台错误
2. 看 `metadata/failures/`
3. 看最近写入的 `metadata/pages/*.json`
4. 重新跑 `sync --full`

由于系统是幂等写入，大多数情况下直接重跑比手工修补更可靠。

### 7.3 下载失败

现象：

- `status` 中 `failed > 0` 或 `missing > 0`

处理：

```bash
uv run grok-downloader download --account andy --concurrency 8
uv run grok-downloader verify --account andy
```

如果仍失败，检查：

- 远端 URL 是否还可访问
- 本地磁盘是否已满
- 归档目录权限是否正确

### 7.4 校验失败

现象：

- `verify` 输出 `hash_mismatches > 0`

说明：

- 文件可能被手工改动
- 下载过程中发生损坏
- SQLite 记录与实际文件不一致

先看缺失清单：

```text
archive/accounts/{alias}/metadata/failures/missing-assets.tsv
```

如果只是局部文件问题，优先重新下载，不要直接改数据库。

### 7.5 Web 有页面但媒体加载失败

排查顺序：

1. 浏览器首访是否使用了 `?token=...`
2. token 是否与当前进程一致
3. `/api/status` 是否可成功鉴权
4. `/media/...` 请求是否返回 401 或 404

## 8. 数据安全

运维时必须把这些路径视为敏感数据：

- `config/accounts.toml`
- `archive/`
- `samples/`

原则：

- 不提交到版本库
- 不贴到公开聊天记录
- 不打包进 Docker 镜像
- 不通过公网无保护暴露 Web UI

## 9. 推荐备份节奏

如果账号内容经常变化，建议：

- 日常：定期 `sync --full`
- 每次同步后：`verify`
- 偶尔：启动 Web UI 抽查几个 folder 和最近内容

如果账号主要是历史归档，建议：

- 大版本补采后跑一次全量
- 之后按月或按需同步

## 10. 交接建议

如果以后换人维护，至少交接以下信息：

- `config/accounts.toml` 的存放位置
- `archive/` 的存放位置和容量
- 当前是否有后台 Web 进程
- Web token 文件位置
- 最近一次 `status` / `verify` 的结果

否则接手者最容易陷入“代码在，但不知道哪份数据才是生产归档”的状态。
