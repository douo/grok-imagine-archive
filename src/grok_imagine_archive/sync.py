from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .archive import Archive
from .client import GrokClient, GrokClientError
from .config import AccountConfig
from .download import _download_pending_assets_locked
from .extract import extract_assets, iter_posts
from .lock import ArchiveLock


@dataclass
class SyncSummary:
    posts_seen: int = 0
    assets_seen: int = 0
    downloaded: int = 0
    errors: int = 0
    pages: int = 0
    folders: int = 0
    folder_checks: set[str] = field(default_factory=set)


def sync_account(
    account: AccountConfig,
    *,
    full: bool = False,
    limit_posts: int | None = None,
    page_limit: int = 40,
    download: bool = True,
    download_concurrency: int = 6,
) -> SyncSummary:
    if not full and limit_posts is None:
        limit_posts = 20
    summary = SyncSummary()
    mode = "full" if full else "limited"
    archive_ref = Archive(account.alias)
    with ArchiveLock(archive_ref.root / ".write.lock"), Archive(account.alias) as archive, GrokClient(account) as client:
        run_id = archive.begin_run(mode=mode, limit_posts=limit_posts)
        try:
            folders_response = client.folder_list()
            archive.save_page("folders", 0, folders_response)
            folders = archive.upsert_folders(folders_response)
            summary.folders = len(folders)

            consume_listing(
                archive,
                client,
                summary,
                scope="liked",
                folder_id=None,
                full=full,
                limit_posts=limit_posts,
                page_limit=page_limit,
            )
            if full:
                for folder in folders:
                    folder_id = str(
                        folder.get("id")
                        or folder.get("folderId")
                        or folder.get("uuid")
                        or folder.get("folder_id")
                        or ""
                    )
                    if not folder_id:
                        continue
                    consume_listing(
                        archive,
                        client,
                        summary,
                        scope=f"folder-{folder_id}",
                        folder_id=folder_id,
                        full=True,
                        limit_posts=None,
                        page_limit=max(page_limit, 120),
                    )

            if download:
                download_summary = _download_pending_assets_locked(
                    account,
                    concurrency=download_concurrency,
                    retry_failed=True,
                    progress=False,
                )
                summary.downloaded += download_summary.downloaded
                summary.errors += download_summary.failed
            archive.finish_run(
                run_id,
                posts_seen=summary.posts_seen,
                assets_seen=summary.assets_seen,
                errors=summary.errors,
                status="ok" if summary.errors == 0 else "partial",
            )
        except Exception:
            archive.finish_run(
                run_id,
                posts_seen=summary.posts_seen,
                assets_seen=summary.assets_seen,
                errors=summary.errors + 1,
                status="failed",
            )
            raise
    return summary


def consume_listing(
    archive: Archive,
    client: GrokClient,
    summary: SyncSummary,
    *,
    scope: str,
    folder_id: str | None,
    full: bool,
    limit_posts: int | None,
    page_limit: int,
) -> None:
    cursor = ""
    page_index = 0
    remaining = limit_posts
    seen_cursors: set[str] = set()
    while True:
        response = client.post_list(cursor=cursor, limit=page_limit, folder_id=folder_id)
        archive.save_page(scope, page_index, response)
        page_index += 1
        summary.pages += 1
        posts = response.get("posts") if isinstance(response, dict) else None
        if not isinstance(posts, list):
            raise GrokClientError(f"{scope} post/list response is missing posts array")
        for post in posts:
            if remaining is not None and remaining <= 0:
                break
            if not isinstance(post, dict):
                continue
            consume_post(archive, client, summary, post)
            if remaining is not None:
                remaining -= 1
        archive.db.commit()
        if remaining is not None and remaining <= 0:
            break
        next_cursor = str(response.get("nextCursor") or "") if isinstance(response, dict) else ""
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise GrokClientError(f"{scope} post/list cursor repeated: {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if not full and limit_posts is None:
            break


def consume_post(
    archive: Archive,
    client: GrokClient,
    summary: SyncSummary,
    post: dict[str, Any],
) -> None:
    post_ids: set[str] = set()
    for nested, parent_id in iter_posts(post):
        archive.upsert_post(nested, parent_post_id=parent_id)
        nested_id = str(nested.get("id") or "")
        if nested_id:
            post_ids.add(nested_id)
            summary.posts_seen += 1
    assets = extract_assets(post)
    summary.assets_seen += len(assets)
    archive.replace_assets_for_posts(post_ids, assets)
    for post_id in sorted(post_ids):
        if post_id in summary.folder_checks:
            continue
        summary.folder_checks.add(post_id)
        try:
            folders = client.post_folders(post_id)
            archive.save_post_folders(post_id, folders)
        except GrokClientError as exc:
            # Folder membership is useful metadata, but a failure should not block media backup.
            summary.errors += 1
            archive.failures_dir.joinpath(f"post-folders-{post_id}.txt").write_text(
                f"{exc}\n",
                encoding="utf-8",
            )
