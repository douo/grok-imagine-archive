from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from .archive import Archive
from .client import GrokClient
from .config import AccountConfig
from .lock import ArchiveLock


@dataclass
class DownloadSummary:
    total: int = 0
    downloaded: int = 0
    already_present: int = 0
    failed: int = 0


def download_pending_assets(
    account: AccountConfig,
    *,
    concurrency: int = 6,
    retry_failed: bool = True,
    progress: bool = False,
) -> DownloadSummary:
    concurrency = max(1, concurrency)
    archive_ref = Archive(account.alias)
    with ArchiveLock(archive_ref.root / ".write.lock"):
        return _download_pending_assets_locked(
            account,
            concurrency=concurrency,
            retry_failed=retry_failed,
            progress=progress,
        )


def _download_pending_assets_locked(
    account: AccountConfig,
    *,
    concurrency: int,
    retry_failed: bool,
    progress: bool,
) -> DownloadSummary:
    with Archive(account.alias) as archive:
        rows = [dict(row) for row in archive.pending_assets(retry_failed=retry_failed)]
    work: queue.Queue[dict[str, Any]] = queue.Queue()
    for row in rows:
        work.put(row)
    summary = DownloadSummary(total=len(rows))
    lock = threading.Lock()

    def add_result(downloaded: bool, status: str) -> None:
        with lock:
            if status == "downloaded" and downloaded:
                summary.downloaded += 1
            elif status == "downloaded":
                summary.already_present += 1
            else:
                summary.failed += 1
            done = summary.downloaded + summary.already_present + summary.failed
            if progress and (done == summary.total or done % 100 == 0):
                print(
                    "download progress: "
                    f"{done}/{summary.total} downloaded={summary.downloaded} "
                    f"present={summary.already_present} failed={summary.failed}",
                    flush=True,
                )

    def worker() -> None:
        with Archive(account.alias) as archive, GrokClient(account, timeout=180) as client:
            while True:
                try:
                    row = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    downloaded = archive.download_asset(client, row)
                    status_row = archive.db.execute(
                        "SELECT status FROM assets WHERE asset_key = ?",
                        (row["asset_key"],),
                    ).fetchone()
                    status = str(status_row["status"] if status_row else "failed")
                    add_result(downloaded, status)
                finally:
                    work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return summary
