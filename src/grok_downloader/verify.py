from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .archive import Archive, hash_file


@dataclass
class VerifySummary:
    posts: int
    images: int
    videos: int
    thumbnails: int
    downloaded: int
    failed: int
    missing: int
    hash_mismatches: int


def verify_account(alias: str) -> VerifySummary:
    with Archive(alias) as archive:
        mismatches = 0
        rows = archive.db.execute(
            """
            SELECT asset_key, local_path, sha256, size, status
              FROM assets
             WHERE status = 'downloaded'
            """
        ).fetchall()
        for row in rows:
            rel = row["local_path"]
            if not rel:
                mismatches += 1
                continue
            path = archive.root / Path(str(rel))
            if not path.exists() or path.stat().st_size <= 0:
                mismatches += 1
                continue
            sha256, size = hash_file(path)
            if row["sha256"] and row["sha256"] != sha256:
                mismatches += 1
            if row["size"] and int(row["size"]) != size:
                mismatches += 1
        stats = archive.stats()
        missing_rows = archive.db.execute(
            """
            SELECT asset_key, post_id, kind, role, url, status, fail_reason
              FROM assets
             WHERE status != 'downloaded'
                OR local_path IS NULL
            """
        ).fetchall()
        if missing_rows:
            failure_path = archive.failures_dir / "missing-assets.tsv"
            failure_path.write_text(
                "\n".join(
                    ["asset_key\tpost_id\tkind\trole\tstatus\treason\turl"]
                    + [
                        "\t".join(
                            str(row[key] or "").replace("\t", " ")
                            for key in (
                                "asset_key",
                                "post_id",
                                "kind",
                                "role",
                                "status",
                                "fail_reason",
                                "url",
                            )
                        )
                        for row in missing_rows
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        return VerifySummary(
            posts=stats["posts"],
            images=stats["images"],
            videos=stats["videos"],
            thumbnails=stats["thumbnails"],
            downloaded=stats["downloaded"],
            failed=stats["failed"],
            missing=stats["missing"],
            hash_mismatches=mismatches,
        )
