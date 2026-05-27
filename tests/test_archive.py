import sqlite3

import pytest

from grok_imagine_archive.archive import Archive
from grok_imagine_archive.lock import ArchiveLock, ArchiveLockError


def test_archive_status_counts_assets_by_role(tmp_path) -> None:
    with Archive("test", root=tmp_path) as archive:
        archive.db.execute(
            """
            INSERT INTO posts (id, raw_json)
            VALUES ('post-1', '{}')
            """
        )
        archive.db.executemany(
            """
            INSERT INTO assets (
              asset_key, post_id, kind, role, url, local_path, status, size
            )
            VALUES (?, 'post-1', ?, ?, ?, ?, 'downloaded', 1)
            """,
            [
                ("image", "image", "media", "https://assets.example/image.jpg", "media/images/a.jpg"),
                ("video", "video", "media", "https://assets.example/video.mp4", "media/videos/a.mp4"),
                ("thumb", "image", "thumbnail", "https://assets.example/thumb.jpg", "thumbs/a.jpg"),
            ],
        )
        archive.db.commit()

        status = archive.status()

    assert status["posts"] == 1
    assert status["images"] == 1
    assert status["videos"] == 1
    assert status["thumbnails"] == 1
    assert status["downloaded"] == 3
    assert status["failed"] == 0
    assert status["missing"] == 0


def test_open_readonly_does_not_allow_writes(tmp_path) -> None:
    with Archive("test", root=tmp_path) as archive:
        archive.db.execute("INSERT INTO posts (id, raw_json) VALUES ('post-1', '{}')")
        archive.db.commit()

    archive = Archive("test", root=tmp_path).open_readonly()
    try:
        assert archive.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            archive.db.execute("INSERT INTO posts (id, raw_json) VALUES ('post-2', '{}')")
    finally:
        archive.close()


def test_archive_lock_blocks_concurrent_writer(tmp_path) -> None:
    lock_path = tmp_path / "archive" / ".write.lock"
    with ArchiveLock(lock_path):
        with pytest.raises(ArchiveLockError):
            with ArchiveLock(lock_path):
                pass
