# Examples

**English** | [简体中文](zh-CN/examples.md)

This document is not an API reference. It is a set of practical workflow
examples. If you already understand what the system does but need to remember
how to use it, start here.

## 1. Account Configuration Example

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

Field notes:

- `alias`
  Local account name. It determines the archive directory
  `archive/accounts/{alias}/`.
- `sso`
  Required authentication field.
- `cf_clearance`
  Cloudflare-related cookie, required in some environments.
- `cf_cookies`
  Additional cookie string for environments that need it.
- `user_agent`
  Browser user agent sent in request headers.
- `browser`
  `curl-cffi` impersonation target.
- `proxy`
  Optional proxy.

## 2. First-Time Setup

### Step 1: Verify Account Access

```bash
uv run grok-imagine-archive auth check --account demo
```

Expected output:

```text
auth ok: account=demo folders=2
```

If this fails, do not start a full sync yet. Check cookies, user agent, and proxy
configuration first.

### Step 2: Run A Small Trial Sync

```bash
uv run grok-imagine-archive sync --account demo --limit 20
```

Expected output:

```text
sync done: account=demo pages=1 folders=2 posts=18 assets=27 downloaded=27 errors=0
```

Notes:

- `posts` and `assets` are the counts observed during traversal, not
  necessarily the final unique counts.
- If `downloaded=0`, the items may already exist locally.

### Step 3: Verify The Archive

```bash
uv run grok-imagine-archive verify --account demo
```

Expected output:

```text
verify: account=demo posts=18 images=10 videos=4 thumbnails=4 downloaded=18 failed=0 missing=0 hash_mismatches=0
```

## 3. Full Sync Example

```bash
uv run grok-imagine-archive sync --account demo --full --download-concurrency 8
```

Typical output:

```text
sync done: account=demo pages=12 folders=3 posts=420 assets=618 downloaded=74 errors=0
```

How to read this:

- `pages=12`
  The tool traversed many API pages, not just the first rendered screen.
- `folders=3`
  The folder list returned three folders.
- `posts=420`
  This is the number of posts encountered during traversal, including repeated
  appearances across root and folder lists.
- `assets=618`
  This is the number of assets recognized during this run.
- `downloaded=74`
  This run downloaded newly discovered files. A later run may report
  `downloaded=0` if everything is already present locally.

After the sync, run:

```bash
uv run grok-imagine-archive status --account demo
uv run grok-imagine-archive verify --account demo
```

## 4. Resume After Interruption

Scenario: the network drops during sync, or some media URLs fail temporarily.

Check status first:

```bash
uv run grok-imagine-archive status --account demo
```

If there are missing or failed assets, download them:

```bash
uv run grok-imagine-archive download --account demo --concurrency 8
uv run grok-imagine-archive verify --account demo
```

Typical output:

```text
download done: account=demo total=12 downloaded=10 already_present=1 failed=1
```

Meaning:

- `downloaded`
  Files newly downloaded during this run.
- `already_present`
  SQLite considered the asset pending, but the file was already present and
  accepted by the current flow.
- `failed`
  Items that still failed after this retry.

## 5. JSON Status Output

```bash
uv run grok-imagine-archive status --account demo --json
```

Useful for:

- automated health checks
- exporting statistics
- integration with local tools

Fields to watch:

- `posts`
- `images`
- `videos`
- `thumbnails`
- `downloaded`
- `failed`
- `missing`
- `latest_run`

## 6. Web UI Startup And Access

### Foreground

```bash
GROK_IMAGINE_ARCHIVE_WEB_TOKEN='replace-with-long-random-token' \
  uv run grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860
```

Console output:

```text
web ui: http://127.0.0.1:7860
```

First browser visit:

```text
http://127.0.0.1:7860/?token=your-access-token
```

### Background

```bash
umask 077
python - <<'PY' > archive/accounts/demo/web-token.txt
import secrets
print(secrets.token_urlsafe(32))
PY
GROK_IMAGINE_ARCHIVE_WEB_TOKEN="$(cat archive/accounts/demo/web-token.txt)" \
  setsid -f sh -c 'cd /path/to/grok-imagine-archive && exec .venv/bin/grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860 > archive/accounts/demo/logs/web.log 2>&1 < /dev/null'
```

Browser access:

```text
http://127.0.0.1:7860/?token=value-from-web-token.txt
```

## 7. What The Web UI Shows

The list page supports:

- folder filtering
- media type filtering
- prompt search
- ascending or descending time order
- automatic pagination while scrolling

The detail page supports:

- primary media preview
- `prompt` / `originalPrompt`
- model and resolution
- folders
- edges
- asset details
- raw JSON

## 8. API Examples

### Query Status

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  http://127.0.0.1:7860/api/status
```

### Query Posts

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  "http://127.0.0.1:7860/api/posts?media=MEDIA_POST_TYPE_VIDEO&limit=5"
```

Example response shape:

```json
{
  "items": [
    {
      "id": "mock-video-001",
      "createTime": "2026-04-29T19:49:05.903892Z",
      "mediaType": "MEDIA_POST_TYPE_VIDEO",
      "modelName": "imagine_x_1",
      "previewPath": "thumbs/mock-video-001-thumb.png",
      "previewKind": "image"
    }
  ],
  "total": 42,
  "limit": 5,
  "offset": 0
}
```

### Query A Detail Record

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  http://127.0.0.1:7860/api/posts/<post_id>
```

Important fields:

- `assets`
- `folders`
- `edges`
- `raw`

## 9. Docker Examples

Build the image:

```bash
docker build -t grok-imagine-archive:local .
```

Mount local archive and config, then check status:

```bash
docker run --rm \
  -v "$PWD/archive:/data/archive" \
  -v "$PWD/config:/app/config:ro" \
  -e GROK_IMAGINE_ARCHIVE_ROOT=/data/archive \
  grok-imagine-archive:local \
  grok-imagine-archive status --account demo
```

Use cases:

- verify that the image works
- reuse the same archive on another machine
- isolate the CLI runtime from the host

## 10. Troubleshooting Examples

### Example 1: `auth check` Fails

Check:

- whether `sso` expired
- whether `cf_clearance` is still valid
- whether `user_agent` differs too much from the cookie acquisition environment
- whether a proxy is required

### Example 2: `sync` Is Slow

First confirm that this is not a false alarm. A full sync may:

- enumerate many root-list cursor pages
- enumerate all folder pages
- request folder relationships for each post
- download or check many media files

This kind of job is not expected to finish in seconds.

### Example 3: `verify` Reports `missing > 0`

Recommended handling:

1. Check `metadata/failures/missing-assets.tsv`.
2. Run `download`.
3. Run `verify` again.

### Example 4: Web UI Opens But Media Does Not Load

Check:

- whether the first visit used `?token=...`
- whether the token matches the current Web process
- whether `/media/` requests return 401
- whether the browser has received the cookie

## 11. Recommended Workflow

For long-term archive use:

1. Run `auth check` when onboarding a new account.
2. Run a small `sync --limit 20`.
3. Run the real `sync --full`.
4. Always run `verify`.
5. Use the Web UI to sample-check a few folders and relationship chains.
6. Repeat `sync --full` + `verify` periodically.

The point is to verify "collection succeeded" and "archive integrity holds" as
separate checks.
