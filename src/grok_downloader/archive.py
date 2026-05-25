from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import GrokClient
from .config import archive_root


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


@dataclass(frozen=True)
class AssetRecord:
    asset_key: str
    post_id: str
    kind: str
    role: str
    url: str
    mime_type: str = ""
    source_path: str = ""


class Archive:
    def __init__(self, alias: str, root: Path | None = None) -> None:
        self.alias = alias
        self.root = (root or archive_root()) / "accounts" / alias
        self.db_path = self.root / "index.sqlite"
        self.media_images = self.root / "media" / "images"
        self.media_videos = self.root / "media" / "videos"
        self.thumbs = self.root / "thumbs"
        self.metadata_posts = self.root / "metadata" / "posts"
        self.raw_pages = self.root / "metadata" / "pages"
        self.failures_dir = self.root / "metadata" / "failures"
        self.conn: sqlite3.Connection | None = None

    def open(self, *, readonly: bool = False) -> "Archive":
        if readonly:
            if not self.db_path.exists():
                raise FileNotFoundError(f"missing archive database: {self.db_path}")
            self.conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000")
            return self

        for directory in (
            self.media_images,
            self.media_videos,
            self.thumbs,
            self.metadata_posts,
            self.raw_pages,
            self.failures_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()
        return self

    def open_readonly(self) -> "Archive":
        return self.open(readonly=True)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Archive":
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("archive is not open")
        return self.conn

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
              id TEXT PRIMARY KEY,
              create_time TEXT,
              media_type TEXT,
              media_url TEXT,
              mime_type TEXT,
              prompt TEXT,
              original_prompt TEXT,
              model_name TEXT,
              width INTEGER,
              height INTEGER,
              original_post_id TEXT,
              parent_post_id TEXT,
              is_liked INTEGER,
              r_rated INTEGER,
              raw_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS folders (
              id TEXT PRIMARY KEY,
              name TEXT,
              raw_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS post_folders (
              post_id TEXT NOT NULL,
              folder_id TEXT NOT NULL,
              folder_name TEXT,
              raw_json TEXT,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (post_id, folder_id)
            );

            CREATE TABLE IF NOT EXISTS assets (
              asset_key TEXT PRIMARY KEY,
              post_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              role TEXT NOT NULL,
              url TEXT NOT NULL,
              mime_type TEXT,
              source_path TEXT,
              local_path TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              sha256 TEXT,
              size INTEGER,
              http_status INTEGER,
              fail_reason TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (post_id, role, url)
            );

            CREATE TABLE IF NOT EXISTS post_edges (
              parent_post_id TEXT NOT NULL,
              child_post_id TEXT NOT NULL,
              relation TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (parent_post_id, child_post_id, relation)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              finished_at TEXT,
              account TEXT NOT NULL,
              mode TEXT NOT NULL,
              limit_posts INTEGER,
              posts_seen INTEGER NOT NULL DEFAULT 0,
              assets_seen INTEGER NOT NULL DEFAULT 0,
              errors INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'running'
            );

            CREATE INDEX IF NOT EXISTS idx_posts_create_time ON posts(create_time);
            CREATE INDEX IF NOT EXISTS idx_posts_prompt ON posts(prompt);
            CREATE INDEX IF NOT EXISTS idx_assets_post_id ON assets(post_id);
            CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
            CREATE INDEX IF NOT EXISTS idx_post_folders_folder_id ON post_folders(folder_id);
            """
        )
        self.db.execute("UPDATE assets SET kind = 'image' WHERE role = 'thumbnail' AND kind != 'image'")
        self.db.commit()

    def begin_run(self, *, mode: str, limit_posts: int | None) -> int:
        cur = self.db.execute(
            "INSERT INTO sync_runs (account, mode, limit_posts) VALUES (?, ?, ?)",
            (self.alias, mode, limit_posts),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        posts_seen: int,
        assets_seen: int,
        errors: int,
        status: str,
    ) -> None:
        self.db.execute(
            """
            UPDATE sync_runs
               SET finished_at = CURRENT_TIMESTAMP,
                   posts_seen = ?,
                   assets_seen = ?,
                   errors = ?,
                   status = ?
             WHERE id = ?
            """,
            (posts_seen, assets_seen, errors, status, run_id),
        )
        self.db.commit()

    def save_page(self, scope: str, index: int, payload: Any) -> None:
        safe_scope = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in scope)
        path = self.raw_pages / f"{safe_scope}-{index:05d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert_folders(self, response: Any) -> list[dict[str, Any]]:
        folders = extract_folder_items(response)
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
            name = str(folder.get("name") or folder.get("title") or folder_id)
            self.db.execute(
                """
                INSERT INTO folders (id, name, raw_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  raw_json=excluded.raw_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (folder_id, name, json_dumps(folder)),
            )
        self.db.commit()
        return folders

    def upsert_post(self, post: dict[str, Any], *, parent_post_id: str | None = None) -> None:
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            return
        resolution = post.get("resolution") if isinstance(post.get("resolution"), dict) else {}
        interaction = (
            post.get("userInteractionStatus")
            if isinstance(post.get("userInteractionStatus"), dict)
            else {}
        )
        self.metadata_posts.joinpath(f"{post_id}.json").write_text(
            json.dumps(post, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.db.execute(
            """
            INSERT INTO posts (
              id, create_time, media_type, media_url, mime_type, prompt, original_prompt,
              model_name, width, height, original_post_id, parent_post_id, is_liked,
              r_rated, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
              create_time=excluded.create_time,
              media_type=excluded.media_type,
              media_url=excluded.media_url,
              mime_type=excluded.mime_type,
              prompt=excluded.prompt,
              original_prompt=excluded.original_prompt,
              model_name=excluded.model_name,
              width=excluded.width,
              height=excluded.height,
              original_post_id=excluded.original_post_id,
              parent_post_id=COALESCE(posts.parent_post_id, excluded.parent_post_id),
              is_liked=excluded.is_liked,
              r_rated=excluded.r_rated,
              raw_json=excluded.raw_json,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                post_id,
                post.get("createTime"),
                post.get("mediaType"),
                post.get("mediaUrl"),
                post.get("mimeType"),
                post.get("prompt"),
                post.get("originalPrompt"),
                post.get("modelName"),
                resolution.get("width"),
                resolution.get("height"),
                post.get("originalPostId"),
                parent_post_id,
                1 if interaction.get("likeStatus") else 0,
                1 if post.get("rRated") else 0,
                json_dumps(post),
            ),
        )
        original_post_id = post.get("originalPostId")
        if original_post_id:
            self.add_edge(str(original_post_id), post_id, "original")
        if parent_post_id:
            self.add_edge(parent_post_id, post_id, "nested")

    def add_edge(self, parent_post_id: str, child_post_id: str, relation: str) -> None:
        if not parent_post_id or not child_post_id or parent_post_id == child_post_id:
            return
        self.db.execute(
            """
            INSERT INTO post_edges (parent_post_id, child_post_id, relation, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(parent_post_id, child_post_id, relation) DO UPDATE SET
              updated_at=CURRENT_TIMESTAMP
            """,
            (parent_post_id, child_post_id, relation),
        )

    def upsert_asset(self, asset: AssetRecord) -> bool:
        existing = self.db.execute(
            "SELECT status, local_path FROM assets WHERE asset_key = ?",
            (asset.asset_key,),
        ).fetchone()
        self.db.execute(
            """
            INSERT INTO assets (
              asset_key, post_id, kind, role, url, mime_type, source_path, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(asset_key) DO UPDATE SET
              post_id=excluded.post_id,
              kind=excluded.kind,
              role=excluded.role,
              url=excluded.url,
              mime_type=excluded.mime_type,
              source_path=excluded.source_path,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                asset.asset_key,
                asset.post_id,
                asset.kind,
                asset.role,
                asset.url,
                asset.mime_type,
                asset.source_path,
            ),
        )
        return existing is None or not existing["local_path"]

    def upsert_assets(self, assets: Iterable[AssetRecord]) -> int:
        count = 0
        for asset in assets:
            if self.upsert_asset(asset):
                count += 1
        self.db.commit()
        return count

    def replace_assets_for_posts(self, post_ids: Iterable[str], assets: Iterable[AssetRecord]) -> int:
        asset_list = list(assets)
        seen = {asset.asset_key for asset in asset_list}
        post_id_list = sorted({post_id for post_id in post_ids if post_id})
        count = self.upsert_assets(asset_list)
        if post_id_list:
            placeholders = ",".join("?" for _ in post_id_list)
            if seen:
                seen_placeholders = ",".join("?" for _ in seen)
                self.db.execute(
                    f"""
                    DELETE FROM assets
                     WHERE post_id IN ({placeholders})
                       AND asset_key NOT IN ({seen_placeholders})
                    """,
                    (*post_id_list, *sorted(seen)),
                )
            else:
                self.db.execute(
                    f"DELETE FROM assets WHERE post_id IN ({placeholders})",
                    post_id_list,
                )
        self.db.commit()
        return count

    def save_post_folders(self, post_id: str, response: Any) -> int:
        folders = extract_folder_items(response)
        if not folders and isinstance(response, list):
            folders = [item for item in response if isinstance(item, dict)]
        if (
            not folders
            and isinstance(response, dict)
            and not any(key in response for key in ("folders", "mediaFolders", "items", "data"))
        ):
            # Some responses are a compact object keyed by folder id.
            folders = []
            for key, value in response.items():
                if isinstance(value, dict):
                    folder = {"id": key, **value}
                else:
                    folder = {"id": key, "name": str(value)}
                folders.append(folder)
        count = 0
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
            name = str(folder.get("name") or folder.get("title") or folder_id)
            self.db.execute(
                """
                INSERT INTO post_folders (post_id, folder_id, folder_name, raw_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(post_id, folder_id) DO UPDATE SET
                  folder_name=excluded.folder_name,
                  raw_json=excluded.raw_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (post_id, folder_id, name, json_dumps(folder)),
            )
            count += 1
        self.db.commit()
        return count

    def pending_assets(self, *, retry_failed: bool = True) -> list[sqlite3.Row]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.db.execute(
                f"""
                SELECT * FROM assets
                 WHERE status IN ({placeholders})
                    OR local_path IS NULL
                 ORDER BY kind, post_id, role
                """,
                statuses,
            )
        )

    def download_asset(self, client: GrokClient, row: sqlite3.Row) -> bool:
        url = str(row["url"])
        local_path = self.local_path_for_asset(row)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists() and local_path.stat().st_size > 0:
            sha256, size = hash_file(local_path)
            self.mark_asset_downloaded(row["asset_key"], local_path, sha256, size)
            return False
        try:
            response = client.get(url, timeout=180, stream=True)
            if response.status_code != 200:
                self.mark_asset_failed(row["asset_key"], response.status_code, f"HTTP {response.status_code}")
                return False
            fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=str(local_path.parent))
            sha = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
                if size <= 0:
                    raise RuntimeError("empty response body")
                os.replace(tmp_name, local_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self.mark_asset_downloaded(row["asset_key"], local_path, sha.hexdigest(), size)
            return True
        except Exception as exc:
            self.mark_asset_failed(row["asset_key"], None, str(exc)[:1000])
            return False

    def local_path_for_asset(self, row: sqlite3.Row) -> Path:
        kind = str(row["kind"] or "other")
        role = str(row["role"] or "media")
        post_id = str(row["post_id"] or "unknown")
        url = str(row["url"] or "")
        mime_type = str(row["mime_type"] or "")
        suffix = suffix_for_url(url, mime_type)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        filename = f"{post_id}-{role}-{digest}{suffix}"
        filename = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)
        if role == "thumbnail":
            return self.thumbs / filename
        if kind == "video":
            return self.media_videos / filename
        if kind == "image":
            return self.media_images / filename
        return self.root / "media" / "other" / filename

    def mark_asset_downloaded(
        self, asset_key: str, local_path: Path, sha256: str, size: int
    ) -> None:
        self.db.execute(
            """
            UPDATE assets
               SET status = 'downloaded',
                   local_path = ?,
                   sha256 = ?,
                   size = ?,
                   http_status = 200,
                   fail_reason = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE asset_key = ?
            """,
            (str(local_path.relative_to(self.root)), sha256, size, asset_key),
        )
        self.db.commit()

    def mark_asset_failed(self, asset_key: str, http_status: int | None, reason: str) -> None:
        self.db.execute(
            """
            UPDATE assets
               SET status = 'failed',
                   http_status = ?,
                   fail_reason = ?,
                   retry_count = retry_count + 1,
                   updated_at = CURRENT_TIMESTAMP
             WHERE asset_key = ?
            """,
            (http_status, reason, asset_key),
        )
        self.db.commit()

    def stats(self) -> dict[str, int]:
        row = self.db.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM posts) AS posts,
              (SELECT COUNT(*) FROM assets WHERE kind='image' AND role!='thumbnail') AS images,
              (SELECT COUNT(*) FROM assets WHERE kind='video' AND role!='thumbnail') AS videos,
              (SELECT COUNT(*) FROM assets WHERE role='thumbnail') AS thumbnails,
              (SELECT COUNT(*) FROM assets WHERE status='downloaded') AS downloaded,
              (SELECT COUNT(*) FROM assets WHERE status='failed') AS failed,
              (SELECT COUNT(*) FROM assets WHERE status!='downloaded') AS missing
            """
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def status(self) -> dict[str, Any]:
        stats = self.stats()
        rows = self.db.execute(
            """
            SELECT kind, role, status, COUNT(*) AS count
              FROM assets
             GROUP BY kind, role, status
             ORDER BY kind, role, status
            """
        ).fetchall()
        latest_run = self.db.execute(
            """
            SELECT id, mode, started_at, finished_at, posts_seen, assets_seen, errors, status
              FROM sync_runs
             ORDER BY id DESC
             LIMIT 1
            """
        ).fetchone()
        return {
            **stats,
            "archive_root": str(self.root),
            "db_path": str(self.db_path),
            "assets_by_kind_role_status": [dict(row) for row in rows],
            "latest_run": dict(latest_run) if latest_run else None,
        }


def hash_file(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def suffix_for_url(url: str, mime_type: str = "") -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    suffix = Path(name).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    if "video" in mime_type:
        return ".mp4"
    if "image" in mime_type:
        return ".jpg"
    return ".bin"


def extract_folder_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        return []
    for key in ("folders", "mediaFolders", "items", "data"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_folder_items(value)
            if nested:
                return nested
    return []
