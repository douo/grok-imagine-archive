from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from .archive import AssetRecord


URL_KEYS = {
    "mediaUrl",
    "thumbnailImageUrl",
    "url",
    "imageUrl",
    "videoUrl",
    "previewImageUrl",
    "sourceUrl",
}
POST_LIST_KEYS = ("images", "videos", "childPosts", "inputMediaItems")


def iter_posts(
    post: dict[str, Any], *, parent_post_id: str | None = None
) -> Iterator[tuple[dict[str, Any], str | None]]:
    post_id = str(post.get("id") or "")
    yield post, parent_post_id
    for key in POST_LIST_KEYS:
        value = post.get(key)
        if not isinstance(value, list):
            continue
        for child in value:
            if isinstance(child, dict) and child.get("id"):
                yield from iter_posts(child, parent_post_id=post_id or parent_post_id)


def extract_assets(post: dict[str, Any]) -> list[AssetRecord]:
    assets: dict[str, AssetRecord] = {}
    for current, _parent_id in iter_posts(post):
        post_id = str(current.get("id") or post.get("id") or "").strip()
        if not post_id:
            continue
        for source_path, key, url in iter_url_fields(current, root=True):
            role = role_for_key(key)
            mime_type = mime_for_item(current, key)
            kind = kind_for_item(current, key, mime_type, url)
            asset_key = asset_key_for(post_id, role, url)
            assets[asset_key] = AssetRecord(
                asset_key=asset_key,
                post_id=post_id,
                kind=kind,
                role=role,
                url=url,
                mime_type=mime_type,
                source_path=source_path,
            )
    return list(assets.values())


def iter_url_fields(
    value: Any, path: str = "$", *, root: bool = False
) -> Iterator[tuple[str, str, str]]:
    if isinstance(value, dict):
        if not root and looks_like_post(value):
            return
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in URL_KEYS and isinstance(item, str) and item.startswith(("http://", "https://")):
                yield child_path, key, item
            elif key == "audioUrls" and isinstance(item, list):
                for index, audio_url in enumerate(item):
                    if isinstance(audio_url, str) and audio_url.startswith(("http://", "https://")):
                        yield f"{child_path}[{index}]", "audioUrl", audio_url
            elif isinstance(item, (dict, list)):
                yield from iter_url_fields(item, child_path, root=False)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                yield from iter_url_fields(item, f"{path}[{index}]", root=False)


def looks_like_post(value: dict[str, Any]) -> bool:
    return bool(
        value.get("id")
        and (
            "mediaUrl" in value
            or "mediaType" in value
            or "thumbnailImageUrl" in value
            or "originalPostId" in value
            or any(isinstance(value.get(key), list) for key in POST_LIST_KEYS)
        )
    )


def role_for_key(key: str) -> str:
    if key == "thumbnailImageUrl" or "preview" in key.lower() or "thumbnail" in key.lower():
        return "thumbnail"
    if key in {"sourceUrl", "imageUrl"}:
        return "source"
    if key == "audioUrl":
        return "audio"
    return "media"


def mime_for_item(item: dict[str, Any], key: str) -> str:
    if key == "thumbnailImageUrl":
        return "image/jpeg"
    mime_type = item.get("mimeType")
    return str(mime_type or "")


def kind_for_item(item: dict[str, Any], key: str, mime_type: str, url: str) -> str:
    if key == "thumbnailImageUrl" or "preview" in key.lower() or "thumbnail" in key.lower():
        return "image"
    lowered = " ".join(
        [
            str(item.get("mediaType") or ""),
            mime_type,
            url,
            key,
        ]
    ).lower()
    if "audio" in lowered:
        return "audio"
    if "video" in lowered or url.lower().endswith((".mp4", ".webm", ".mov")):
        return "video"
    if "image" in lowered or url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    return "other"


def asset_key_for(post_id: str, role: str, url: str) -> str:
    digest = hashlib.sha256(f"{post_id}\0{role}\0{url}".encode("utf-8")).hexdigest()
    return digest
