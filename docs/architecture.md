# 技术架构

## 1. 架构总览

`grok-downloader` 可以拆成四层：

1. 接入层
   CLI 和 Web UI，负责触发操作和展示结果
2. 编排层
   `sync`、`download`、`verify` 等流程控制
3. 领域层
   post/asset/folder/edge 的提取、索引和状态管理
4. 基础设施层
   Grok API 请求、SQLite、本地文件系统、进程锁

用文字表示数据流：

```text
Grok API
  -> client.py
  -> sync.py
  -> extract.py
  -> archive.py(SQLite + files)
  -> verify.py / web.py / cli.py
  -> 用户
```

## 2. 模块划分

### 2.1 CLI 入口

文件：`src/grok_downloader/cli.py`

职责：

- 解析命令行参数
- 装配账号配置
- 调用对应业务模块
- 输出适合 shell 和运维脚本消费的摘要结果

命令与实现映射：

- `auth check` -> `cmd_auth_check`
- `sync` -> `cmd_sync`
- `download` -> `cmd_download`
- `status` -> `cmd_status`
- `verify` -> `cmd_verify`
- `web` -> `cmd_web`

### 2.2 API 客户端

文件：`src/grok_downloader/client.py`

职责：

- 统一封装对 Grok API 的请求
- 注入认证 cookie 和请求头
- 处理浏览器版本 impersonation 的兼容降级
- 提供 `folder_list`、`post_list`、`post_folders` 和媒体下载 `get`

关键点：

- `post_list()` 使用 `source = MEDIA_POST_SOURCE_LIKED`
- 列表枚举依赖 `cursor`
- 媒体下载与 API JSON 请求共用认证上下文

### 2.3 同步编排

文件：`src/grok_downloader/sync.py`

职责：

- 创建同步 run
- 枚举主列表和 folder 列表
- 对每个 post 写入本地索引
- 调用提取器生成资产清单
- 记录 folder 关系
- 在需要时触发下载

`sync_account()` 的实际流程：

1. 获取写锁
2. 打开归档和 API 客户端
3. `folder_list`
4. `consume_listing(scope="liked")`
5. 若 `--full`，对每个 folder 再次 `consume_listing`
6. 汇总待下载资产并下载
7. 写入 `sync_runs`

### 2.4 资产提取

文件：`src/grok_downloader/extract.py`

职责：

- 遍历 post 及嵌套 post
- 找出所有 URL 型资产字段
- 基于字段名、媒体类型和 URL 推断 `role` 与 `kind`
- 生成稳定 `asset_key`

关键策略：

- `iter_posts()` 递归处理 `images`、`videos`、`childPosts`、`inputMediaItems`
- `iter_url_fields()` 会继续扫描嵌套对象中的 `mediaUrl`、`thumbnailImageUrl`、`sourceUrl` 等字段
- 通过 `asset_key_for(post_id, role, url)` 保证同一资产可幂等写入

### 2.5 归档存储

文件：`src/grok_downloader/archive.py`

职责：

- 初始化目录结构
- 管理 SQLite schema
- 写入 post/folder/asset/edge/run 数据
- 下载文件并维护其状态、大小、哈希、失败原因
- 生成聚合状态供 CLI 和 Web 使用

这是项目最核心的模块，因为它决定了“归档是否可持续维护”。

### 2.6 下载器

文件：`src/grok_downloader/download.py`

职责：

- 查询 SQLite 中待处理资产
- 启动多线程并发下载
- 复用 `archive.download_asset()` 完成状态机更新

设计特征：

- 下载和枚举解耦
- 线程内各自打开独立的 `Archive` 和 `GrokClient`
- 通过队列实现简单直接的并发模型

### 2.7 校验器

文件：`src/grok_downloader/verify.py`

职责：

- 对所有 `status='downloaded'` 的资产重新计算哈希和大小
- 找出缺失、损坏或元数据不一致的文件
- 输出汇总结果
- 当存在缺失项时，生成 `metadata/failures/missing-assets.tsv`

### 2.8 Web UI

文件：`src/grok_downloader/web.py`

职责：

- 提供只读 HTTP API
- 服务媒体文件
- 渲染一个无构建步骤的浏览页面

设计约束：

- SQLite 只读打开
- token 中间件可选
- 媒体路径必须位于归档根目录之下
- 前端渲染机制：支持图片/视频宽高 `aspect-ratio` 预占位（无布局抖动），并结合 `IntersectionObserver` 实现自动滚动加载（无手动 Load More 按钮）。加载过程中提供骨架屏占位与平滑淡入动画效果。

## 3. 数据模型

### 3.1 `posts`

一条 Grok Imagine 记录的主表。

关键字段：

- `id`
- `create_time`
- `media_type`
- `media_url`
- `prompt`
- `original_prompt`
- `model_name`
- `width` / `height`
- `original_post_id`
- `parent_post_id`
- `raw_json`

### 3.2 `assets`

每个 post 的实际下载对象。

关键字段：

- `asset_key`
- `post_id`
- `kind`
- `role`
- `url`
- `local_path`
- `status`
- `sha256`
- `size`
- `http_status`
- `fail_reason`
- `retry_count`

常见组合：

- `image/media`
- `video/media`
- `image/thumbnail`
- `image/source`

### 3.3 `folders`

远端 folder 的本地快照。

### 3.4 `post_folders`

post 与 folder 的多对多关系表。

### 3.5 `post_edges`

记录 post 之间的依赖关系。

当前关系类型：

- `original`
  Grok API 字段 `originalPostId` 表示的来源/派生关系。UI 中显示为 `Original parent`，用于回答“这条 post 是从哪条原始 post 派生出来的”。
- `nested`
  本地提取器在 `images`、`videos`、`childPosts`、`inputMediaItems` 等嵌套列表里发现 post 时记录的包含关系。UI 中显示为 `Nested parent` 和 `Child posts`，用于回答“这条 post 是在哪条 post 的嵌套结构里出现的”。

### 3.6 `sync_runs`

记录每次同步任务的审计信息：
n
- 模式：`full` 或 `limited`
- 开始/结束时间
- 看到了多少 posts/assets
- 错误数
- 最终状态：`running` / `ok` / `partial` / `failed`

## 4. 目录结构设计

```text
archive/accounts/{alias}/
  index.sqlite
  media/images/
  media/videos/
  thumbs/
  metadata/posts/
  metadata/pages/
  metadata/failures/
```

为什么这样设计：

- 文件和索引分离，避免只靠目录名猜语义
- 原始页面响应和结构化结果并存，既能查问题，也能直接使用
- 每账号单独目录，降低串号和误删风险

## 5. 关键流程

### 5.1 全量同步流程

```text
sync --full
  -> 获取账号锁
  -> 拉 folder 列表
  -> 拉 liked 主列表直到 nextCursor 为空
  -> 拉每个 folder 的分页列表直到 nextCursor 为空
  -> 递归写入 posts 和 post_edges
  -> 提取 assets
  -> 保存 post_folders
  -> 下载 pending/failed 资产
  -> 结束 sync_run
```

说明：

- `posts_seen` 统计的是遍历过程中见到的 post 次数，可能大于 `posts` 表最终唯一行数
- `assets_seen` 也是枚举总量，不等于最终去重后 `assets` 行数

### 5.2 下载流程

```text
download
  -> 查询 assets 中 pending / failed
  -> 多线程并发下载
  -> 落临时文件
  -> 计算 sha256 和大小
  -> 原子移动到目标路径
  -> 更新状态 downloaded / failed
```

这个流程允许你在不重新枚举远端列表的情况下单独补齐媒体。

### 5.3 校验流程

```text
verify
  -> 扫描所有 downloaded 资产
  -> 检查 local_path 是否存在
  -> 重新计算 hash 和 size
  -> 汇总 missing / failed / hash_mismatches
  -> 生成缺失清单
```

### 5.4 浏览流程

```text
browser
  -> GET /api/posts
  -> GET /api/posts/{id}
  -> GET /media/{path}
```

Web UI 不直接触发同步，也不写数据库。

## 6. 为什么不依赖浏览器滚动

这是整个项目最重要的技术决策之一。

Saved 页面是典型的懒加载列表，UI 里的“滚动更多”只是前端消费分页数据的手段，不是后端数据边界本身。因此：

- DOM 中当前存在的卡片数量并不代表账号总资产数
- 前端实现可能改版，但 cursor 分页的契约通常更直接
- 使用 API 可以保留原始 JSON，便于审计和重新解释

项目当前选择的是“以接口分页为准，以页面滚动行为为背景知识”，而不是“模拟网页滚动直到看起来到底了”。

## 7. 幂等性与恢复能力

### 7.1 幂等写入

- `posts.id` 是主键
- `folders.id` 是主键
- `assets.asset_key` 是主键
- `post_folders` 和 `post_edges` 也使用稳定主键

这意味着多次跑 `sync --full` 不会无限膨胀，而是更新已有记录。

### 7.2 下载恢复

- 已成功下载的资产带有 `local_path`、`sha256`、`size`
- 失败项保留 `fail_reason` 和 `retry_count`
- `download` 可以在后续独立运行

### 7.3 同步恢复

如果 `sync` 中途失败：

- 已经成功写入的 post、asset、page JSON 仍会保留
- `sync_runs` 会标为 `failed`
- 重新运行可以继续补齐，而不必清库重来

## 8. 并发与一致性

### 8.1 为什么要加写锁

两个 `sync` 或 `download` 同时写同一账号的 SQLite 和文件树，风险很高：

- 竞争写入同一资产
- 状态覆盖
- 临时文件冲突

因此系统以账号为粒度加 `.write.lock`。

### 8.2 为什么 Web 只读

Web UI 只消费归档，不参与写入，带来的收益是：

- 浏览和同步隔离
- 数据库权限模型简单
- 部署时更容易控制安全边界

## 9. 安全设计

### 9.1 认证信息

认证信息只从 `config/accounts.toml` 读取，不写入归档数据库。

### 9.2 Web 访问令牌

当 Web UI 暴露到非 loopback 地址时，要求显式提供 token，避免局域网裸奔。

### 9.3 媒体文件访问约束

`/media/{path}` 会把请求路径解析到账号归档根目录下，并拒绝目录穿越。

### 9.4 敏感数据隔离

以下内容被明确视为敏感：

- `config/accounts.toml`
- `archive/`
- `samples/`

这些都不应进入版本库或公开日志。

## 10. 测试策略

现有测试覆盖的重点是“高风险基础行为”，包括：

- 配置解析
- API 客户端兼容逻辑
- 资产提取
- HAR 契约样本
- 归档状态和只读访问
- Web 路由鉴权和路径安全

建议继续保持这种策略：先保核心语义，再补边缘交互。

## 11. 技术取舍

### 11.1 为什么用 SQLite

因为需求是单机、单用户、本地归档优先。SQLite 的优势正好匹配：

- 零部署
- 查询能力足够
- 易于备份和迁移
- 适合作为归档索引，而不是高并发在线交易数据库

### 11.2 为什么 Web UI 直接嵌在 FastAPI 里

当前 Web UI 的作用是浏览归档，不是复杂前端应用。把 HTML 直接内嵌：

- 依赖更少
- 部署更简单
- 不需要额外构建链路

### 11.3 为什么下载器用线程而不是异步

下载场景以 I/O 为主，线程模型已经足够直接，而且与现有同步、SQLite 和请求库整合成本更低。

## 12. 扩展点

后续如果要继续演进，推荐优先从这些点扩展：

- 在 `extract.py` 增加新的 URL 字段识别规则
- 在 `archive.py` 增加更多聚合查询和报表接口
- 在 `web.py` 增加更强的详情导航和筛选项
- 在 `verify.py` 增加更细粒度的校验报告

不建议轻易动的点：

- `assets` 主键策略
- 归档目录层级
- Web 只读模式

这些属于系统稳定性的基础契约。
