# Grok Imagine Archive

[English](../../README.md) | **简体中文**

`grok-imagine-archive` 是一个面向 Grok Imagine Saved/Liked 资产的本地归档工具。它通过官方 Web 接口背后的 JSON API 做只读同步，把账号当前可枚举到的图片、视频、缩略图、提示词、原始 JSON、文件夹关系和派生关系完整保存到本地，并提供一个可离线浏览归档内容的 Web UI。

这个项目不是一次性的脚本，而是一个长期可维护的归档系统：

- 远端枚举使用 cursor 分页，不依赖浏览器首屏 DOM，也不会被 Saved 页面滚动懒加载误导。
- 本地存储使用 SQLite + 文件归档，支持状态追踪、失败重试、哈希校验和只读浏览。
- 每个账号完全隔离，适合长期增量同步，而不是仅导出一次后丢弃。

## 截图

下面的截图由 mock 数据生成，不来自真实 Grok 账号或私人归档。

![Mock Web archive grid](../assets/web-grid.png)

![Mock post detail dialog](../assets/web-detail.png)

## 文档导航

如果你后续很久不接触这个项目，只想快速恢复理解，按下面顺序看：

1. [产品与使用总览](#产品与使用总览)
2. [产品设计](product-design.md)
3. [技术架构](architecture.md)
4. [使用示例](examples.md)
5. [运维手册](operations.md)
6. [English README](../../README.md)

## 产品与使用总览

### 解决什么问题

Grok Imagine 的 Saved 页面是滚动懒加载的，账号资源多时，靠浏览器滚动截图或手工另存基本不可控，也无法稳定保留：

- 原图、缩略图、视频等不同形态的媒体文件
- prompt、originalPrompt、模型名、分辨率等元数据
- 某个 post 与 folder 的归属关系
- 派生链路，比如原始 post、子 post、输入素材之间的关系
- 某次归档是否完整、哪些文件失败、是否能安全重试

`grok-imagine-archive` 的目标就是把这些内容变成一个可以反复同步、可以核验、可以浏览的本地资产库。

### 核心能力

- `auth check`
  检查本地账号配置是否还能访问 Grok API。
- `sync`
  枚举远端列表、写入 posts/assets/folders/edges，并按需下载媒体。
- `download`
  仅处理 SQLite 中仍处于 `pending` 或 `failed` 的资产，不重新跑远端枚举。
- `status`
  输出账号当前归档规模和最近一次同步状态。
- `verify`
  对已下载文件重新计算哈希和大小，确认归档是否一致。
- `web`
  在本地启动只读 Web UI，浏览归档内容。

### 全量同步语义

`sync --full` 的行为不是“抓一页页面”，而是：

1. 调 `/rest/media/folder/list`
2. 调 `/rest/media/post/list` 枚举 liked/saved 主列表，直到 `nextCursor == ""`
3. 对每个 folder 再次跑 `/rest/media/post/list` 的 folder 维度分页
4. 对每个 post 递归提取：
   `images`、`videos`、`childPosts`、`inputMediaItems`、`thumbnailImageUrl` 以及嵌套对象中的源地址
5. 对已知 post 调 `/rest/media/post/folders` 保存 folder 关系
6. 下载全部待处理媒体并写入哈希、大小、重试状态

这意味着它解决的是 API 层面的“完整枚举”问题，而不是前端渲染层面的“滚动到最底”问题。

## 快速开始

### 1. 准备配置

复制示例配置：

```bash
cp config/accounts.example.toml config/accounts.toml
```

从能正常打开 Grok Saved/Liked 图片的同一个浏览器里复制这些值：

1. 用 Chrome 或 Edge 打开 `https://grok.com/` 并登录。
2. 进入一次 Grok Imagine 的 Saved 或 Liked 页面，然后不要关闭这个标签页。
3. 在页面上右键，选择“检查”。也可以按 `F12`；macOS 可以按 `Command+Option+I`。
4. 在开发者工具里打开 **Application**。如果没看到，点顶部的 `>>` 菜单找一下。
   Firefox 里对应的是 **Storage**。
5. 在左侧打开 **Cookies** > `https://grok.com`。
6. 找到名为 `sso` 的 cookie。只复制它的 **Value** 这一栏，填到 `sso`。
   不要复制 `sso=`。
7. 找到名为 `cf_clearance` 的 cookie。只复制它的 **Value** 这一栏，填到
   `cf_clearance`。不要复制 `cf_clearance=`。如果没有这一项，先保留
   `cf_clearance = ""` 并运行 `auth check`；如果提示 Cloudflare 拦截，再刷新
   Grok 页面重新找一次。
8. 打开 **Console** 标签，输入 `navigator.userAgent` 并回车。把引号里的内容复制到
   `user_agent`。
9. `browser` 填 User-Agent 里的 Chrome 大版本。例如 User-Agent 里有
   `Chrome/136...`，就填 `browser = "chrome136"`。不确定的话，第一次检查先保留示例值。
10. 除非登录浏览器时也用了同一个代理，否则保持 `proxy = ""`。

不要复制整段 `Cookie:` 请求头，也不要把 cookie 值贴到 GitHub issue、Reddit、截图或日志里。

最小配置示例：

```toml
[[accounts]]
alias = "demo"
enabled = true
sso = "..."
cf_clearance = "..."
user_agent = "Mozilla/5.0 ..."
browser = "chrome136"
proxy = ""
```

说明：

- `config/accounts.toml`、`samples/`、`archive/` 都被 `.gitignore` 忽略
- 不要把 cookie、token、HAR 样本提交到仓库或贴到日志
- 可以通过环境变量覆盖路径：

```bash
export GROK_IMAGINE_ARCHIVE_CONFIG=/secure/accounts.toml
export GROK_IMAGINE_ARCHIVE_ROOT=/data/grok-archive
```

### 2. 先跑连通性检查

```bash
uv run grok-imagine-archive auth check --account demo
```

### 3. 小批量验证

```bash
uv run grok-imagine-archive sync --account demo --limit 20
uv run grok-imagine-archive verify --account demo
```

### 4. 正式全量同步

```bash
uv run grok-imagine-archive sync --account demo --full --download-concurrency 8
uv run grok-imagine-archive verify --account demo
```

### 5. 浏览归档

```bash
GROK_IMAGINE_ARCHIVE_WEB_TOKEN='replace-with-long-random-token' \
  uv run grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860
```

浏览器首次访问：

```text
http://127.0.0.1:7860/?token=你的访问令牌
```

## 本地归档结构

```text
archive/accounts/{alias}/
  index.sqlite
  media/images/
  media/videos/
  thumbs/
  metadata/posts/
  metadata/pages/
  metadata/failures/
  logs/
```

目录说明：

- `index.sqlite`
  结构化索引，记录 posts、assets、folders、post_edges、sync_runs 等信息
- `media/images/`、`media/videos/`
  已下载的主媒体文件
- `thumbs/`
  缩略图和预览图
- `metadata/posts/`
  单个 post 的原始 JSON 快照
- `metadata/pages/`
  每一页 API 响应的原始 JSON，用于审计和回放
- `metadata/failures/`
  失败清单和辅助排障文件
- `logs/`
  本地后台 Web 进程等运行日志

## 代码结构

主要实现集中在 `src/grok_imagine_archive/`：

- `cli.py`
  命令入口和参数解析
- `client.py`
  Grok API 客户端，请求头和浏览器指纹兼容逻辑
- `sync.py`
  全量/增量同步编排
- `extract.py`
  post 递归遍历和资产 URL 提取
- `archive.py`
  SQLite 模式、文件落盘、下载状态管理
- `download.py`
  并发下载待处理资产
- `verify.py`
  哈希复算和完整性检查
- `web.py`
  只读 Web API 和单页浏览界面

更详细的模块职责、数据流和表设计见 [architecture.md](architecture.md)。

## 健康标准

一次归档完成后，至少应满足：

- `status` 中 `failed=0`
- `status` 中 `missing=0`
- `verify` 中 `hash_mismatches=0`
- `sync_runs.latest.status` 为 `ok` 或可解释的 `partial`

常用检查：

```bash
uv run grok-imagine-archive status --account demo
uv run grok-imagine-archive verify --account demo
```

如果出现短暂下载失败：

```bash
uv run grok-imagine-archive download --account demo --concurrency 8
uv run grok-imagine-archive verify --account demo
```

## 安全边界

- 该工具对 Grok 是只读的，不删除也不修改云端资源
- 同一账号的 `sync`/`download` 会加写锁，避免并发写坏归档
- Web UI 打开 SQLite 时使用只读模式
- 媒体文件访问被限制在账号归档目录内部，避免目录穿越
- 归档中可能包含私密媒体、prompt 和历史输入素材，应视为敏感数据

## 进一步阅读

- [product-design.md](product-design.md)
  看产品目标、用户场景、交互设计和取舍
- [architecture.md](architecture.md)
  看技术架构、数据模型、关键流程和扩展点
- [examples.md](examples.md)
  看实际命令样例、返回结果和常见工作流
- [operations.md](operations.md)
  看运维、部署、巡检和故障处理
