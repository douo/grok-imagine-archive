from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
import zlib
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: create_mock_archive.py /tmp/grok-imagine-archive-mock-archive", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    if root == Path("/") or not str(root).startswith("/tmp/"):
        print("mock archive root must be under /tmp", file=sys.stderr)
        return 2
    if root.exists():
        shutil.rmtree(root)
    accounts = {
        "demo": [
            MockPost("mock-image-001", "MEDIA_POST_TYPE_IMAGE", "Saved", "saved", 900, 1200),
            MockPost("mock-video-001", "MEDIA_POST_TYPE_VIDEO", "Motion Studies", "motion", 1280, 720),
            MockPost("mock-image-002", "MEDIA_POST_TYPE_IMAGE", "References", "refs", 1200, 900),
            MockPost("mock-image-003", "MEDIA_POST_TYPE_IMAGE", "Saved", "saved", 900, 1100),
            MockPost("mock-video-002", "MEDIA_POST_TYPE_VIDEO", "Motion Studies", "motion", 900, 1200),
            MockPost("mock-image-004", "MEDIA_POST_TYPE_IMAGE", "References", "refs", 1100, 900),
        ],
        "studio": [
            MockPost("studio-image-001", "MEDIA_POST_TYPE_IMAGE", "Campaign", "campaign", 900, 900),
            MockPost("studio-video-001", "MEDIA_POST_TYPE_VIDEO", "Campaign", "campaign", 1280, 720),
            MockPost("studio-image-002", "MEDIA_POST_TYPE_IMAGE", "Review Queue", "review", 900, 1200),
        ],
    }
    for account, posts in accounts.items():
        build_account(root, account, posts)
    return 0


class MockPost:
    def __init__(
        self,
        post_id: str,
        media_type: str,
        folder_name: str,
        folder_id: str,
        width: int,
        height: int,
    ) -> None:
        self.post_id = post_id
        self.media_type = media_type
        self.folder_name = folder_name
        self.folder_id = folder_id
        self.width = width
        self.height = height


def build_account(root: Path, account: str, posts: list[MockPost]) -> None:
    account_root = root / "accounts" / account
    images = account_root / "media" / "images"
    videos = account_root / "media" / "videos"
    thumbs = account_root / "thumbs"
    metadata = account_root / "metadata" / "posts"
    pages = account_root / "metadata" / "pages"
    failures = account_root / "metadata" / "failures"
    for directory in (images, videos, thumbs, metadata, pages, failures):
        directory.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(account_root / "index.sqlite")
    db.row_factory = sqlite3.Row
    migrate(db)
    folders = sorted({(post.folder_id, post.folder_name) for post in posts})
    for folder_id, folder_name in folders:
        db.execute(
            "INSERT INTO folders (id, name, raw_json) VALUES (?, ?, ?)",
            (folder_id, folder_name, json.dumps({"id": folder_id, "name": folder_name})),
        )
    for index, post in enumerate(posts):
        create_post(db, account_root, post, index)
    if account == "demo":
        db.executemany(
            """
            INSERT INTO post_edges (parent_post_id, child_post_id, relation)
            VALUES (?, ?, ?)
            """,
            [
                ("mock-image-002", "mock-image-001", "original"),
                ("mock-image-003", "mock-image-001", "nested"),
                ("mock-image-001", "mock-video-001", "original"),
            ],
        )
    db.execute(
        """
        INSERT INTO sync_runs (
          account, mode, limit_posts, posts_seen, assets_seen, errors, status,
          finished_at
        )
        VALUES (?, 'full', NULL, ?, ?, 0, 'ok', CURRENT_TIMESTAMP)
        """,
        (account, len(posts), len(posts) * 2),
    )
    db.commit()
    db.close()


def create_post(db: sqlite3.Connection, account_root: Path, post: MockPost, index: int) -> None:
    created = f"2026-05-{20 - index:02d}T{10 + index:02d}:30:00Z"
    prompt = prompt_for(post.post_id)
    raw = {
        "id": post.post_id,
        "createTime": created,
        "mediaType": post.media_type,
        "prompt": prompt,
        "originalPrompt": f"Mock original prompt for {post.post_id}",
        "modelName": "imagine_mock_1",
        "resolution": {"width": post.width, "height": post.height},
        "userInteractionStatus": {"likeStatus": True},
        "mockData": True,
    }
    if post.post_id == "mock-image-001":
        raw["originalPostId"] = "mock-image-002"
    metadata_path = account_root / "metadata" / "posts" / f"{post.post_id}.json"
    metadata_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    db.execute(
        """
        INSERT INTO posts (
          id, create_time, media_type, media_url, mime_type, prompt,
          original_prompt, model_name, width, height, original_post_id,
          parent_post_id, is_liked, r_rated, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
        """,
        (
            post.post_id,
            created,
            post.media_type,
            f"https://example.invalid/{post.post_id}",
            "image/png" if post.media_type.endswith("IMAGE") else "video/mp4",
            prompt,
            raw["originalPrompt"],
            raw["modelName"],
            post.width,
            post.height,
            raw.get("originalPostId"),
            None,
            json.dumps(raw, sort_keys=True),
        ),
    )
    db.execute(
        """
        INSERT INTO post_folders (post_id, folder_id, folder_name, raw_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            post.post_id,
            post.folder_id,
            post.folder_name,
            json.dumps({"id": post.folder_id, "name": post.folder_name}),
        ),
    )
    if post.media_type.endswith("VIDEO"):
        media_path = account_root / "media" / "videos" / f"{post.post_id}-media.mp4"
        write_mock_mp4(media_path)
        add_asset(
            db,
            post.post_id,
            "video",
            "media",
            f"https://example.invalid/media/{post.post_id}.mp4",
            media_path,
            account_root,
        )
        thumb_path = account_root / "thumbs" / f"{post.post_id}-thumb.png"
        write_png(thumb_path, 640, 360, palette_for(post.post_id), f"{post.post_id} preview")
        add_asset(
            db,
            post.post_id,
            "image",
            "thumbnail",
            f"https://example.invalid/thumbs/{post.post_id}.png",
            thumb_path,
            account_root,
        )
    else:
        media_path = account_root / "media" / "images" / f"{post.post_id}-media.png"
        write_png(media_path, post.width, post.height, palette_for(post.post_id), post.post_id)
        add_asset(
            db,
            post.post_id,
            "image",
            "media",
            f"https://example.invalid/media/{post.post_id}.png",
            media_path,
            account_root,
        )
        source_path = account_root / "media" / "images" / f"{post.post_id}-source.png"
        write_png(source_path, 640, 640, palette_for(post.post_id + "-source"), "uploaded source")
        add_asset(
            db,
            post.post_id,
            "image",
            "source",
            f"https://example.invalid/source/{post.post_id}.png",
            source_path,
            account_root,
            source_json_path="$.inputMediaItems[0].imageUrl",
        )


def add_asset(
    db: sqlite3.Connection,
    post_id: str,
    kind: str,
    role: str,
    url: str,
    path: Path,
    account_root: Path,
    *,
    source_json_path: str = "",
) -> None:
    data = path.read_bytes()
    db.execute(
        """
        INSERT INTO assets (
          asset_key, post_id, kind, role, url, mime_type, source_path,
          local_path, status, sha256, size, http_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'downloaded', ?, ?, 200)
        """,
        (
            f"{post_id}:{role}:{hashlib.sha1(url.encode()).hexdigest()[:8]}",
            post_id,
            kind,
            role,
            url,
            "video/mp4" if kind == "video" else "image/png",
            source_json_path,
            str(path.relative_to(account_root)),
            hashlib.sha256(data).hexdigest(),
            len(data),
        ),
    )


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE posts (
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
        CREATE TABLE folders (
          id TEXT PRIMARY KEY,
          name TEXT,
          raw_json TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE post_folders (
          post_id TEXT NOT NULL,
          folder_id TEXT NOT NULL,
          folder_name TEXT,
          raw_json TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (post_id, folder_id)
        );
        CREATE TABLE assets (
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
        CREATE TABLE post_edges (
          parent_post_id TEXT NOT NULL,
          child_post_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (parent_post_id, child_post_id, relation)
        );
        CREATE TABLE sync_runs (
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
        CREATE INDEX idx_posts_create_time ON posts(create_time);
        CREATE INDEX idx_assets_post_id ON assets(post_id);
        CREATE INDEX idx_assets_status ON assets(status);
        CREATE INDEX idx_post_folders_folder_id ON post_folders(folder_id);
        """
    )


def prompt_for(post_id: str) -> str:
    prompts = {
        "mock-image-001": "A clean editorial product shot of a ceramic travel cup on a steel desk",
        "mock-video-001": "A six second turntable study of a translucent kinetic sculpture",
        "mock-image-002": "Reference board with soft window light, neutral props, and cropped material swatches",
        "mock-image-003": "A compact layout study with warm background panels and crisp foreground edges",
        "mock-video-002": "Vertical motion test for a minimal gallery installation with slow camera drift",
        "mock-image-004": "Archive-ready concept image with visible prompt metadata and source material",
        "studio-image-001": "Campaign key visual mockup with structured negative space",
        "studio-video-001": "Short social motion draft with high contrast product silhouette",
        "studio-image-002": "Review queue item showing source upload and final generated result",
    }
    return prompts[post_id]


def palette_for(key: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    digest = hashlib.sha256(key.encode()).digest()
    a = (60 + digest[0] % 120, 60 + digest[1] % 120, 60 + digest[2] % 120)
    b = (90 + digest[3] % 130, 90 + digest[4] % 130, 90 + digest[5] % 130)
    return a, b


def write_png(
    path: Path,
    width: int,
    height: int,
    palette: tuple[tuple[int, int, int], tuple[int, int, int]],
    label: str,
) -> None:
    a, b = palette
    rows = []
    label_hash = hashlib.sha256(label.encode()).digest()
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            t = (x / max(1, width - 1) + y / max(1, height - 1)) / 2
            stripe = 28 if ((x // 80 + y // 80) % 2 == 0) else -18
            r = clamp(int(a[0] * (1 - t) + b[0] * t) + stripe)
            g = clamp(int(a[1] * (1 - t) + b[1] * t) + stripe)
            blue = clamp(int(a[2] * (1 - t) + b[2] * t) + stripe)
            if 24 < x < min(width - 24, 24 + len(label_hash) * 18) and 24 < y < 84:
                bit = label_hash[(x // 18) % len(label_hash)] & (1 << ((y // 8) % 8))
                if bit:
                    r, g, blue = 240, 244, 248
            row.extend((r, g, blue))
        rows.append(bytes(row))
    payload = b"".join(rows)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(payload, level=9))
        + png_chunk(b"IEND", b"")
    )


def write_mock_mp4(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to generate mock video assets")
    color = "#{:02x}{:02x}{:02x}".format(*palette_for(path.stem)[0])
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:d=1",
            "-vf",
            "format=yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        check=True,
    )


def png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def clamp(value: int) -> int:
    return min(255, max(0, value))


if __name__ == "__main__":
    raise SystemExit(main())
