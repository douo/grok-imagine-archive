# Operations

**English** | [简体中文](zh-CN/operations.md)

This guide is for users who have decided to run and maintain the project over
time. It answers four operational questions:

1. How to run it reliably on a local machine or server.
2. How to know whether a backup really completed.
3. Where to look first when something fails.
4. How to expose the Web UI safely.

## 1. Runtime Prerequisites

Before deployment, confirm:

- `config/accounts.toml` is stored on persistent storage.
- `archive/` is stored on persistent storage.
- account cookies have been verified.
- the machine has enough disk space.

Prefer keeping config and archive data on paths whose lifecycle is independent
from the source checkout. Code updates should not endanger data.

## 2. Standard Run Flows

### 2.1 Local Foreground Run

```bash
uv run grok-imagine-archive auth check --account demo
uv run grok-imagine-archive sync --account demo --full --download-concurrency 8
uv run grok-imagine-archive verify --account demo
GROK_IMAGINE_ARCHIVE_WEB_TOKEN='replace-with-long-random-token' \
  uv run grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860
```

This is suitable for first-time onboarding or an attended run.

### 2.2 Background Web UI

```bash
umask 077
python - <<'PY' > archive/accounts/demo/web-token.txt
import secrets
print(secrets.token_urlsafe(32))
PY
GROK_IMAGINE_ARCHIVE_WEB_TOKEN="$(cat archive/accounts/demo/web-token.txt)" \
  setsid -f sh -c 'cd /path/to/grok-imagine-archive && exec .venv/bin/grok-imagine-archive web --account demo --host 127.0.0.1 --port 7860 > archive/accounts/demo/logs/web.log 2>&1 < /dev/null'
```

Notes:

- `umask 077` restricts token file permissions.
- Keeping the token under the account archive directory ties it to the same
  operational lifecycle as that account.
- Logs go to `archive/accounts/demo/logs/web.log`.

## 3. Health Checks

### 3.1 Confirm Backup Completion

Minimum check set:

```bash
uv run grok-imagine-archive status --account demo
uv run grok-imagine-archive verify --account demo
```

Healthy state:

- `failed=0`
- `missing=0`
- `hash_mismatches=0`
- latest `sync_run.status` is `ok`

### 3.2 Confirm Web UI Health

```bash
curl http://127.0.0.1:7860/healthz
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" http://127.0.0.1:7860/api/status
```

Interpretation:

- `/healthz` returning 200 means the process is online.
- `/api/status` returning 200 means authentication and read-only database access
  work.

### 3.3 Logs And Files To Inspect

Start with:

- `archive/accounts/{alias}/logs/web.log`
- `archive/accounts/{alias}/metadata/failures/`
- `archive/accounts/{alias}/metadata/pages/`

These correspond to:

- Web process logs
- explicit failure artifacts
- raw API responses for API troubleshooting

## 4. Web Access Control

### 4.1 Default Strategy

When the Web UI is bound to a loopback address, a token is optional:

- `127.0.0.1`
- `localhost`
- `::1`

When binding to a non-loopback address such as `0.0.0.0`, you must provide:

- `GROK_IMAGINE_ARCHIVE_WEB_TOKEN`

Avoid `--allow-unauthenticated` unless you fully understand and accept the
environment risk.

### 4.2 Browser Access

First visit:

```text
http://127.0.0.1:7860/?token=your-access-token
```

The server writes an HTTP-only cookie. Later browser requests, including media
requests, do not need the token in the URL.

### 4.3 API Access

```bash
curl -H "x-access-token: $GROK_IMAGINE_ARCHIVE_WEB_TOKEN" \
  http://127.0.0.1:7860/api/status
```

Or:

```bash
curl "http://127.0.0.1:7860/?token=$GROK_IMAGINE_ARCHIVE_WEB_TOKEN"
```

## 5. Docker

### 5.1 Compose

```bash
cp docker-compose.example.yml docker-compose.yml
docker compose up --build
```

### 5.2 One-Off Command

```bash
docker build -t grok-imagine-archive:local .
docker run --rm \
  -v "$PWD/archive:/data/archive" \
  -v "$PWD/config:/app/config:ro" \
  -e GROK_IMAGINE_ARCHIVE_ROOT=/data/archive \
  grok-imagine-archive:local \
  grok-imagine-archive status --account demo
```

Operational principles:

- Mount `archive/` and `config/` from outside the image.
- Do not bake credentials or archive data into the image.
- If exposing the service outside the host, put an authenticated TLS proxy in
  front of it.

## 6. Routine Checks

For a long-running archive environment, run periodically:

```bash
uv run grok-imagine-archive sync --account demo --full --download-concurrency 8
uv run grok-imagine-archive verify --account demo
uv run grok-imagine-archive status --account demo --json
```

Watch:

- `posts`
- `downloaded`
- `failed`
- `missing`
- `latest_run.status`

If `posts` grows but `downloaded` does not, the new observations may be duplicate
index entries or media that is still pending. Check details before assuming a
problem.

## 7. Troubleshooting

### 7.1 Authentication Failure

Symptoms:

- `auth check` fails
- `sync` fails immediately with an HTTP error

Check in order:

1. whether `sso` expired
2. whether `cf_clearance` expired
3. whether `user_agent` differs too much from the browser used to obtain cookies
4. whether a proxy is required

If the error contains `Cloudflare challenge detected`, refresh the browser
session and copy a new `cf_clearance` into the local account config. Keep
`user_agent`, `browser`, and any proxy setting aligned with the same browser
session and egress IP that produced the clearance cookie.

### 7.2 Sync Failure

Symptoms:

- `sync` exits non-zero
- `sync_runs.status = failed`

Check in order:

1. console error
2. `metadata/failures/`
3. the most recent `metadata/pages/*.json`
4. rerun `sync --full`

Because writes are idempotent, rerunning is usually safer than manual database
repair.

### 7.3 Download Failure

Symptoms:

- `status` reports `failed > 0` or `missing > 0`

Handling:

```bash
uv run grok-imagine-archive download --account demo --concurrency 8
uv run grok-imagine-archive verify --account demo
```

If failures remain, check:

- whether the remote URL is still reachable
- whether local disk is full
- whether archive directory permissions are correct

### 7.4 Verification Failure

Symptoms:

- `verify` reports `hash_mismatches > 0`

Possible causes:

- a file was manually modified
- a download was corrupted
- SQLite metadata and local files diverged

Start with:

```text
archive/accounts/{alias}/metadata/failures/missing-assets.tsv
```

For localized file issues, prefer redownloading over editing the database by
hand.

### 7.5 Web Page Loads But Media Does Not

Check in order:

1. whether the first browser visit used `?token=...`
2. whether the token matches the current process
3. whether `/api/status` authenticates successfully
4. whether `/media/...` returns 401 or 404

## 8. Data Safety

Treat these paths as sensitive operational data:

- `config/accounts.toml`
- `archive/`
- `samples/`

Rules:

- do not commit them
- do not paste them into public conversations
- do not package them into Docker images
- do not expose the Web UI to the public internet without protection

## 9. Recommended Backup Cadence

If account content changes often:

- routine: run `sync --full` periodically
- after each sync: run `verify`
- occasionally: open the Web UI and sample-check folders and recent content

If the account is mostly a historical archive:

- run a full sync after major backfill work
- then sync monthly or on demand

## 10. Handoff Notes

For future maintainers, hand off at least:

- where `config/accounts.toml` is stored
- where `archive/` is stored and how large it is
- whether any background Web UI process is running
- where the Web token file is stored
- latest `status` / `verify` result

Without this, maintainers may have the code but not know which data location is
the real production archive.
