# Product Design

**English** | [简体中文](zh-CN/product-design.md)

## 1. Positioning

`grok-imagine-archive` is not a "save the webpage" script. It is a personal archive
tool for Grok Imagine assets.

It focuses on two problems:

1. The Grok Saved/Liked page is lazy-loaded, so the browser view is not a
   reliable representation of the complete dataset.
2. Media files alone are not enough. Search, review, audit, and reuse depend on
   metadata, folder membership, derivation relationships, and verification
   state.

The product goal is to turn remotely visible assets into a local archive system
that can be verified, browsed, and incrementally synced.

## 2. Target Users

### 2.1 Primary Users

- Personal creators who want to preserve saved or generated Grok Imagine work.
- Researchers or operators who need offline access to prompts, models,
  resolution, derivation chains, and source assets.
- Technical maintainers who may return to the project after a long break and
  need to understand it quickly.

### 2.2 Outcomes Users Care About

- Whether all currently visible account content has been archived.
- Which assets are images, videos, thumbnails, or uploaded sources.
- Which folders a work belongs to, what its prompt was, and whether it was
  derived from another post.
- Whether interrupted downloads can resume without starting over.
- Whether the local archive is easy to browse without reading raw JSON by hand.

## 3. Product Boundaries

### 3.1 Covered

- Grok Imagine Saved/Liked root lists.
- Folder lists and folder-scoped post lists.
- Posts and nested `images`, `videos`, `childPosts`, and `inputMediaItems`.
- Media URLs, thumbnail URLs, and source media URLs.
- Folder relationships, raw JSON, derivation edges, download state, hashes, and
  file sizes.
- A local read-only browser UI.

### 3.2 Explicitly Not Covered

- Cloud-side deletion, unliking, moving folders, or any other write operation.
- Browser UI automation or scroll scraping.
- Multi-user online service hosting.
- Automatic uploads of the archive to third-party object storage.

These boundaries are intentionally conservative. The core value is reliable
archiving, not replacing the official Grok frontend.

## 4. Core Scenarios

### Scenario A: First Full Backup

A user has fresh account cookies and wants to confirm that hundreds or thousands
of assets can be archived.

Expected flow:

1. Configure the account.
2. Run `auth check`.
3. Run `sync --full`.
4. Run `verify`.
5. Open the Web UI and sample-check the archive.

### Scenario B: Recovery After Interruption

The network drops during sync, or some media downloads fail.

Expected flow:

1. Run `sync --full` again.
2. Or run `download` directly.
3. Run `verify` again.

Key requirements:

- Existing successful data must not be damaged.
- Files that already succeeded should not be downloaded again.
- Failed items must remain traceable.

### Scenario C: Offline Browsing And Review

After the archive is complete, the main needs become:

- browse by folder
- search prompts
- view images and videos
- inspect raw JSON and relationships for a post

This is the primary value of the Web UI. It serves archive consumption, not
sync orchestration.

## 5. Product Structure

The product has three parts.

### 5.1 CLI

The CLI is for users who need explicit control over sync actions. It is stable,
scriptable, and suitable for operations.

Command set:

- `auth check`
- `sync`
- `download`
- `status`
- `verify`
- `web`

Design principles:

- Each command has a clear single responsibility.
- Output should be easy for shell scripts and operators to consume.
- Defaults are conservative. For example, `sync` without `--full` is suitable
  for a smaller trial run.

### 5.2 Local Archive

The archive is the core asset layer. Without it, the CLI and Web UI are only
wrappers.

Requirements:

- account isolation
- separate file storage and index storage
- resumable operation
- auditable raw data

### 5.3 Web UI

The Web UI is the archive consumption layer and does not write data.

Design principles:

- read-only behavior
- stable lazy loading without layout shift, using media dimensions and skeleton
  placeholders
- automatic pagination through `IntersectionObserver`, without a manual Load
  More button
- fast scanning of images and videos
- filtering by folder, media type, prompt, and time
- detail views with media, prompts, raw JSON, folders, and relationships

## 6. Key Product Decisions

### Decision 1: Use API Cursor Enumeration Instead Of Browser Scrolling

Reasons:

- The Saved page is lazy-loaded, so the current DOM does not prove the account's
  total asset count.
- The frontend may change at any time, while `nextCursor` is a clearer
  enumeration contract.
- API responses can be stored as raw JSON for later audit and replay.

### Decision 2: Keep Media Files And SQLite Together

Downloading only files creates two problems:

- The archive cannot tell whether a file is primary media, a thumbnail, or a
  source upload.
- The archive cannot reliably report failed, missing, or verified files.

SQLite is the archive control plane; the filesystem is the archive data plane.

### Decision 3: Keep The Web UI Read-Only

Reasons:

- Browsing and writes stay decoupled, reducing operational risk.
- The UI can safely browse old data while sync is running.
- Deployment and permissions remain simpler.

### Decision 4: Store Raw JSON Explicitly

Reasons:

- Future analysis may need fields that the current extractor does not use.
- Extractor upgrades can replay existing archived payloads.
- The archive remains reinterpretable as the API evolves.

## 7. Information Architecture

### 7.1 Core Archive Objects

- `post`
  A Grok Imagine record and the central object for browsing and relationship
  analysis.
- `asset`
  A media object that belongs to a post. It can be primary media, a thumbnail,
  a source upload, or another extracted URL.
- `folder`
  A local snapshot of a remote folder.
- `post_edge`
  A derivation or nesting relationship between posts.
- `sync_run`
  An audit record for one sync task.

### 7.2 Main Web UI Views

- List view
  Bulk browsing and scanning.
- Detail view
  Deep inspection of one post.

Filter dimensions:

- account
- folder
- media type
- prompt keyword
- time order

## 8. Typical Use Cases

### Use Case 1: Confirm A Video Was Fully Archived

1. Open the Web UI.
2. Search by prompt or time.
3. Open the detail view.
4. Confirm that `assets` contains `video/media` and thumbnails.
5. Run `verify` for final integrity confirmation.

### Use Case 2: Analyze How An Image Was Derived

1. Check `originalPostId` and `parentPostId` in the detail view.
2. Review `edges`.
3. Combine raw JSON with `inputMediaItems` to reconstruct source context.

### Use Case 3: Sync New Assets Without Re-downloading Everything

1. Run `sync --full`.
2. The system re-enumerates remote lists and updates the index.
3. Only new or missing assets become `pending`.
4. The downloader handles only pending items.

## 9. Usability Design

### 9.1 Why The Web UI Stays Simple

The primary problem is archive reliability, not frontend complexity. The current
Web UI is intentionally:

- single-page
- low-dependency
- read-only
- quick to start locally

This makes it easier to maintain and practical for emergency browsing.

### 9.2 Why Raw API Pages Are Preserved

When the project is revisited months later, `metadata/pages/` and
`metadata/posts/` show exactly what was received from the API at sync time. This
is safer than guessing from memory or from today's frontend behavior.

## 10. Non-Functional Requirements

### 10.1 Reliability

- Sync uses a write lock.
- Downloads support retry.
- Verification recomputes hashes.
- Sync run results are traceable.

### 10.2 Safety

- Tokens and cookies are not printed.
- Web access supports a token.
- Media paths are constrained to the archive root.
- Sensitive data is not committed.

### 10.3 Maintainability

- Module boundaries are clear.
- Raw responses are auditable.
- README is the entry point; topic docs cover product design, architecture,
  operations, and examples.

## 11. Future Extension Ideas

Recommended priority:

1. Improve detail navigation, such as jumping directly through related posts.
2. Add exports such as folder-level CSV or JSON reports.
3. Add finer verification reports by asset role.
4. Add incremental change views, such as "posts/assets added in this sync."

Lower priority:

- cloud write operations
- heavy frontend framework rewrites
- browsing directly from the file tree without SQLite

These directions add complexity without improving the core archive value enough
to justify doing them first.
