# 使用示例

[English](../examples.md) | **简体中文**

这份文档不是 API 参考，而是“真实工作流示例”。如果你已经知道系统是什么，但忘了具体怎么用，先看这里最快。

## 1. 账号配置示例

```toml
[[accounts]]
alias = "demo"
enabled = true
sso = "..."
cf_clearance = "..."
cf_cookies = ""
user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
browser = "chrome136"
proxy = ""
```

字段说明：

- `alias`
  本地账号名，决定归档目录 `archive/accounts/{alias}/`
- `sso`
  必填认证字段
- `cf_clearance`
  Cloudflare 相关 cookie，某些环境需要
- `cf_cookies`
  额外 cookie 串，按需使用
- `user_agent`
  请求头中的浏览器 UA
- `browser`
  `curl-cffi` 的 impersonate 目标
- `proxy`
  可选代理

## 2. 第一次接入

### 步骤 1：验证账号是否可用

```bash
uv run grok-imagine-archive auth check --account demo
```

预期输出示例：

```text
auth ok: account=demo folders=2
```

如果这里失败，先不要继续跑全量同步。优先检查 cookie、UA 和代理。

### 步骤 2：先小规模试跑

```bash
uv run grok-imagine-archive sync --account demo --limit 20
```

预期输出示例：

```text
sync done: account=demo pages=1 folders=2 posts=18 assets=27 downloaded=27 errors=0
```

说明：

- `posts` 和 `assets` 是这次遍历中看到的数量，不一定等于最终唯一数量
- 如果 `downloaded=0`，可能说明这批内容此前已经存在于本地

### 步骤 3：做一次校验

```bash
uv run grok-imagine-archive verify --account demo
```

预期输出示例：

```text
verify: account=demo posts=18 images=10 videos=4 thumbnails=4 downloaded=18 failed=0 missing=0 hash_mismatches=0
```

## 3. 全量同步示例

```bash
uv run grok-imagine-archive sync --account demo --full --download-concurrency 8
```

典型输出：

```text
sync done: account=demo pages=12 folders=3 posts=420 assets=618 downloaded=74 errors=0
```

如何理解这个结果：

- `pages=12`
  说明确实遍历了多页 API 数据，而不是只拿首屏
- `folders=3`
  表示 folder 列表枚举到 3 个 folder
- `posts=420`
  是遍历过程中见到的 post 总数，包含主列表和 folder 列表中的重复出现
- `assets=618`
  是这次枚举中识别到的资产数
- `downloaded=74`
  代表这次运行下载了新增文件。后续如果都已存在，本字段可能为 `0`

同步完成后马上跑：

```bash
uv run grok-imagine-archive status --account demo
uv run grok-imagine-archive verify --account demo
```

## 4. 断点恢复示例

场景：同步时网络闪断，或者某些媒体返回临时错误。

先看状态：

```bash
uv run grok-imagine-archive status --account demo
```

如果有缺失或失败，再补下载：

```bash
uv run grok-imagine-archive download --account demo --concurrency 8
uv run grok-imagine-archive verify --account demo
```

典型输出：

```text
download done: account=demo total=12 downloaded=10 already_present=1 failed=1
```

这里的含义是：

- `downloaded`
  本次真正新下载成功的数量
- `already_present`
  SQLite 认为待处理，但实际文件已经存在并通过了当前流程
- `failed`
  本次补下载后仍失败的数量

## 5. 查看 JSON 形式状态

```bash
uv run grok-imagine-archive status --account demo --json
```

适合脚本消费的场景包括：

- 自动巡检
- 导出统计
- 接入其他内部工具

你可以关注这些字段：

- `posts`
- `images`
- `videos`
- `thumbnails`
- `downloaded`
- `failed`
- `missing`
- `latest_run`

## 6. Web UI 启动与访问

### 前台启动

```bash
GROK_IMAGINE_ARCHIVE_WEB_TOKEN='replace-with-long-random-token' \
  uv run grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860
```

控制台会输出：

```text
web ui: http://127.0.0.1:7860
```

浏览器首次访问：

```text
http://127.0.0.1:7860/?token=你的令牌
```

### 后台启动

```bash
umask 077
python - <<'PY' > archive/accounts/demo/web-token.txt
import secrets
print(secrets.token_urlsafe(32))
PY
GROK_IMAGINE_ARCHIVE_WEB_TOKEN="$(cat archive/accounts/demo/web-token.txt)" \
  setsid -f sh -c 'cd /path/to/grok-imagine-archive && exec .venv/bin/grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860 > archive/accounts/demo/logs/web.log 2>&1 < /dev/null'
```

浏览器访问方式：

```text
http://127.0.0.1:7860/?token=web-token.txt 里的值
```

## 7. Web UI 能看什么

列表页支持：

- folder 过滤
- media type 过滤
- prompt 搜索
- 时间正序/倒序
- 分页加载更多

详情页支持：

- 主媒体预览
- prompt / originalPrompt
- model / 分辨率
- folders
- edges
- assets 明细
- raw JSON

## 8. API 调用示例

### 查询状态

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  http://127.0.0.1:7860/api/status
```

### 查询列表

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  "http://127.0.0.1:7860/api/posts?media=MEDIA_POST_TYPE_VIDEO&limit=5"
```

返回结构示意：

```json
{
  "items": [
    {
      "id": "mock-video-001",
      "createTime": "2026-04-29T19:49:05.903892Z",
      "mediaType": "MEDIA_POST_TYPE_VIDEO",
      "modelName": "imagine_x_1",
      "previewPath": "thumbs/example.jpg",
      "previewKind": "image"
    }
  ],
  "total": 42,
  "limit": 5,
  "offset": 0
}
```

### 查询详情

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  http://127.0.0.1:7860/api/posts/<post_id>
```

重点字段：

- `assets`
- `folders`
- `edges`
- `raw`

## 9. Docker 示例

构建镜像：

```bash
docker build -t grok-imagine-archive:local .
```

挂载本地归档和配置后查看状态：

```bash
docker run --rm \
  -v "$PWD/archive:/data/archive" \
  -v "$PWD/config:/app/config:ro" \
  -e GROK_IMAGINE_ARCHIVE_ROOT=/data/archive \
  grok-imagine-archive:local \
  grok-imagine-archive status --account demo
```

适用场景：

- 验证镜像可用性
- 在不同机器上复用同一份归档
- 将 CLI 运行环境和主机隔离

## 10. 常见排查示例

### 例 1：`auth check` 失败

优先检查：

- `sso` 是否过期
- `cf_clearance` 是否仍有效
- `user_agent` 是否与 cookie 获取环境相差太大
- 是否需要代理

### 例 2：`sync` 很慢

先确认这不是误判。全量同步会：

- 枚举主列表多页 cursor
- 枚举所有 folder 分页
- 对每个 post 拉 folder 关系
- 下载或检查大量媒体

这类任务本来就不是秒级操作。

### 例 3：`verify` 出现 `missing > 0`

处理方式：

1. 看 `metadata/failures/missing-assets.tsv`
2. 跑 `download`
3. 再跑 `verify`

### 例 4：浏览器打开 Web UI 但看不到媒体

优先检查：

- 首次是否用了 `?token=...`
- token 是否与当前 Web 进程一致
- `/media/` 请求是否返回 401
- 浏览器里是否已经写入 cookie

## 11. 推荐工作流

如果你想把这个项目当作长期归档工具来用，建议固定流程：

1. 新账号首次接入时先 `auth check`
2. 先小规模 `sync --limit 20`
3. 再跑正式 `sync --full`
4. 必跑 `verify`
5. 用 Web UI 抽查几个 folder 和派生链
6. 后续定期重复 `sync --full` + `verify`

这套流程的目的不是“多打一遍命令”，而是把“采集成功”和“归档完整”分开验证。
