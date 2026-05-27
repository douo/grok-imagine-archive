# Architecture

**English** | [简体中文](zh-CN/architecture.md)

## 1. Overview

`grok-imagine-archive` can be viewed as four layers:

1. Access layer
   CLI and Web UI, responsible for triggering actions and displaying results.
2. Orchestration layer
   `sync`, `download`, `verify`, and related workflows.
3. Domain layer
   Extraction, indexing, and state management for posts, assets, folders, and
   edges.
4. Infrastructure layer
   Grok API requests, SQLite, local filesystem storage, and process locks.

Data flow:

```text
Grok API
  -> client.py
  -> sync.py
  -> extract.py
  -> archive.py(SQLite + files)
  -> verify.py / web.py / cli.py
  -> user
```

## 2. Modules

### 2.1 CLI Entry Point

File: `src/grok_imagine_archive/cli.py`

Responsibilities:

- parse command-line arguments
- load account configuration
- call the corresponding business module
- print summaries suitable for shells and operational scripts

Command mapping:

- `auth check` -> `cmd_auth_check`
- `sync` -> `cmd_sync`
- `download` -> `cmd_download`
- `status` -> `cmd_status`
- `verify` -> `cmd_verify`
- `web` -> `cmd_web`

### 2.2 API Client

File: `src/grok_imagine_archive/client.py`

Responsibilities:

- wrap Grok API requests
- inject authentication cookies and request headers
- handle browser impersonation fallback behavior
- provide `folder_list`, `post_list`, `post_folders`, and media download `get`

Key points:

- `post_list()` uses `source = MEDIA_POST_SOURCE_LIKED`.
- Listing enumeration is cursor-based.
- Media downloads and API JSON requests share the same authentication context.

### 2.3 Sync Orchestration

File: `src/grok_imagine_archive/sync.py`

Responsibilities:

- create a sync run
- enumerate the root list and folder-scoped lists
- write each post to the local index
- call the extractor to create asset records
- record folder relationships
- trigger downloads when requested

Actual `sync_account()` flow:

1. acquire the account write lock
2. open the archive and API client
3. call `folder_list`
4. call `consume_listing(scope="liked")`
5. if `--full` is set, call `consume_listing` for each folder
6. summarize pending assets and download them
7. update `sync_runs`

### 2.4 Asset Extraction

File: `src/grok_imagine_archive/extract.py`

Responsibilities:

- traverse posts and nested posts
- find URL-shaped asset fields
- infer `role` and `kind` from field names, media types, and URLs
- generate stable `asset_key` values

Key strategies:

- `iter_posts()` recursively processes `images`, `videos`, `childPosts`, and
  `inputMediaItems`.
- `iter_url_fields()` scans nested objects for fields such as `mediaUrl`,
  `thumbnailImageUrl`, and `sourceUrl`.
- `asset_key_for(post_id, role, url)` makes repeated writes idempotent.

### 2.5 Archive Storage

File: `src/grok_imagine_archive/archive.py`

Responsibilities:

- initialize the archive directory tree
- manage the SQLite schema
- write post/folder/asset/edge/run data
- download files and maintain their state, size, hash, and failure reason
- provide aggregate status for the CLI and Web UI

This is the project's core module because it defines whether the archive can be
maintained over time.

### 2.6 Downloader

File: `src/grok_imagine_archive/download.py`

Responsibilities:

- query pending assets from SQLite
- run concurrent media downloads
- reuse `archive.download_asset()` for state transitions

Design traits:

- downloads are decoupled from remote enumeration
- each worker thread opens its own `Archive` and `GrokClient`
- concurrency is implemented with a simple queue-based worker model

### 2.7 Verifier

File: `src/grok_imagine_archive/verify.py`

Responsibilities:

- recompute hash and size for every `status='downloaded'` asset
- detect missing, damaged, or inconsistent files
- return a summary
- generate `metadata/failures/missing-assets.tsv` when missing files exist

### 2.8 Web UI

File: `src/grok_imagine_archive/web.py`

Responsibilities:

- provide a read-only HTTP API
- serve archived media files
- render a no-build single-page browser UI

Design constraints:

- SQLite is opened read-only.
- Token middleware is optional for loopback use and required for unsafe public
  binding unless explicitly bypassed.
- Media paths must resolve inside the account archive root.
- The frontend reserves image/video space with `aspect-ratio`, uses
  `IntersectionObserver` for automatic pagination, and shows skeleton
  placeholders with smooth fade-in.

## 3. Data Model

### 3.1 `posts`

Main table for Grok Imagine records.

Important fields:

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

Downloadable objects associated with posts.

Important fields:

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

Common combinations:

- `image/media`
- `video/media`
- `image/thumbnail`
- `image/source`

### 3.3 `folders`

Local snapshots of remote folders.

### 3.4 `post_folders`

Many-to-many relationships between posts and folders.

### 3.5 `post_edges`

Relationships between posts.

Current relationship types:

- `original`
  The source or derivation relationship represented by Grok API
  `originalPostId`. The UI displays it as `Original parent`, which answers
  "which original post was this derived from?"
- `nested`
  A containment relationship found by the local extractor inside `images`,
  `videos`, `childPosts`, `inputMediaItems`, and similar nested lists. The UI
  displays it as `Nested parent` and `Child posts`, which answers "where did
  this post appear as nested data?"

### 3.6 `sync_runs`

Audit records for sync tasks:

- mode: `full` or `limited`
- start and finish time
- number of posts and assets observed
- error count
- final status: `running`, `ok`, `partial`, or `failed`

## 4. Directory Layout

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

Why this layout:

- Files and indexes are separate, so behavior is not inferred from filenames
  alone.
- Raw page responses and structured records coexist for audit and direct use.
- Each account has its own directory, reducing cross-account mistakes and
  accidental deletion risk.

## 5. Key Flows

### 5.1 Full Sync Flow

```text
sync --full
  -> acquire account lock
  -> fetch folder list
  -> fetch liked root list until nextCursor is empty
  -> fetch every folder list until nextCursor is empty
  -> recursively write posts and post_edges
  -> extract assets
  -> save post_folders
  -> download pending/failed assets
  -> finish sync_run
```

Notes:

- `posts_seen` counts every post encountered while traversing lists, so it can
  be larger than the final unique row count in `posts`.
- `assets_seen` is also an enumeration count, not the final deduplicated
  `assets` row count.

### 5.2 Download Flow

```text
download
  -> query pending / failed assets
  -> download concurrently
  -> write a temporary file
  -> compute sha256 and size
  -> atomically move the file into place
  -> update status to downloaded / failed
```

This allows media completion without re-enumerating remote lists.

### 5.3 Verification Flow

```text
verify
  -> scan all downloaded assets
  -> check local_path existence
  -> recompute hash and size
  -> summarize missing / failed / hash_mismatches
  -> generate a missing-assets manifest
```

### 5.4 Browsing Flow

```text
browser
  -> GET /api/posts
  -> GET /api/posts/{id}
  -> GET /media/{path}
```

The Web UI never triggers sync and never writes to SQLite.

## 6. Why Browser Scrolling Is Not A Contract

This is one of the project's most important technical decisions.

The Saved page is a lazy-loaded list. "Scroll for more" is a frontend method for
consuming paginated data, not a backend completeness boundary. Therefore:

- the number of cards currently in the DOM is not the account's total asset
  count
- frontend behavior can change, while cursor pagination is a clearer contract
- API enumeration can preserve raw JSON for audit and reinterpretation

The project treats API pagination as authoritative and page scrolling as only
background knowledge.

## 7. Idempotency And Recovery

### 7.1 Idempotent Writes

- `posts.id` is the primary key.
- `folders.id` is the primary key.
- `assets.asset_key` is the primary key.
- `post_folders` and `post_edges` also use stable primary keys.

This means repeated `sync --full` runs update existing records instead of
expanding the archive indefinitely.

### 7.2 Download Recovery

- Successfully downloaded assets have `local_path`, `sha256`, and `size`.
- Failed items retain `fail_reason` and `retry_count`.
- `download` can be run independently later.

### 7.3 Sync Recovery

If `sync` fails partway through:

- already written posts, assets, and page JSON are kept
- `sync_runs` is marked `failed`
- rerunning sync can fill gaps without clearing the database

## 8. Concurrency And Consistency

### 8.1 Why A Write Lock Exists

Two `sync` or `download` processes writing the same account's SQLite database and
file tree would be risky:

- they may compete to write the same asset
- state updates can overwrite each other
- temporary files can collide

The system therefore uses an account-scoped `.write.lock`.

### 8.2 Why The Web UI Is Read-Only

The Web UI only consumes archive data. Benefits:

- browsing and sync stay isolated
- database permissions are simple
- deployment boundaries are easier to reason about

## 9. Security Design

### 9.1 Credentials

Credentials are read from `config/accounts.toml` and are not written into the
archive database.

### 9.2 Web Access Token

When the Web UI binds to a non-loopback address, an explicit token is required
unless the operator opts into unauthenticated access.

### 9.3 Media Path Constraint

`/media/{path}` resolves requested paths inside the account archive root and
rejects path traversal.

### 9.4 Sensitive Data Isolation

These paths are sensitive:

- `config/accounts.toml`
- `archive/`
- `samples/`

They must not enter the public repository or public logs.

## 10. Test Strategy

Existing tests cover high-risk foundation behavior:

- config parsing
- API client compatibility behavior
- asset extraction
- HAR contract samples
- archive status and read-only access
- Web route authentication and path safety

The preferred strategy is to protect core semantics first, then add edge
interaction coverage as the surface grows.

## 11. Tradeoffs

### 11.1 Why SQLite

The project is local-first, single-machine, and usually single-user. SQLite fits
that profile:

- zero deployment
- enough query power
- easy backup and migration
- suitable as an archive index rather than a high-concurrency transaction system

### 11.2 Why The Web UI Is Embedded In FastAPI

The Web UI exists to browse an archive, not to be a complex frontend
application. Embedding the HTML keeps:

- dependencies low
- deployment simple
- build tooling unnecessary

### 11.3 Why Threads Instead Of Async For Downloads

Media download work is mostly I/O. Threads are direct, sufficient, and integrate
well with the current sync code, SQLite usage, and request library.

## 12. Extension Points

Recommended areas for future evolution:

- add URL field recognition rules in `extract.py`
- add more aggregate queries and reports in `archive.py`
- add richer detail navigation and filters in `web.py`
- add finer verification reports in `verify.py`

Avoid changing these casually:

- `assets` key strategy
- archive directory hierarchy
- read-only Web UI behavior

Those are core stability contracts.
