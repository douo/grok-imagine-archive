import json
from pathlib import Path
from urllib.parse import urlparse

from grok_downloader.archive import extract_folder_items
from grok_downloader.extract import extract_assets, iter_posts


HAR = Path("samples/grok-imagine-saved-current.har")


def _entries() -> list[dict]:
    if not HAR.exists():
        return []
    return json.loads(HAR.read_text())["log"]["entries"]


def test_current_har_has_expected_media_requests() -> None:
    entries = _entries()
    assert entries, "current HAR sample is required for contract validation"
    paths = [urlparse(entry["request"]["url"]).path for entry in entries]
    assert "/rest/media/post/list" in paths
    assert "/rest/media/folder/list" in paths
    assert "/rest/media/post/folders" in paths


def test_current_har_post_list_shape_when_body_is_available() -> None:
    bodies = []
    for entry in _entries():
        if urlparse(entry["request"]["url"]).path != "/rest/media/post/list":
            continue
        text = entry.get("response", {}).get("content", {}).get("text")
        if text:
            bodies.append(json.loads(text))
    assert bodies, "HAR must contain at least one retained post/list response body"
    assert any("nextCursor" in body for body in bodies)
    posts = [post for body in bodies for post in body.get("posts", []) if isinstance(post, dict)]
    assert posts
    assert any(post.get("images") for post in posts)
    assert any(post.get("videos") or post.get("childPosts") for post in posts)
    assets = [asset for post in posts[:5] for asset in extract_assets(post)]
    assert assets
    assert all(asset.url.startswith("https://") for asset in assets)
    assert any(asset.kind == "image" for asset in assets)
    assert any(asset.kind == "video" for asset in assets)
    nested = [nested for post in posts[:5] for nested, _parent in iter_posts(post)]
    assert len(nested) >= len(posts[:5])


def test_current_har_folder_shape_when_body_is_available() -> None:
    for entry in _entries():
        if urlparse(entry["request"]["url"]).path != "/rest/media/folder/list":
            continue
        text = entry.get("response", {}).get("content", {}).get("text")
        if not text:
            continue
        response = json.loads(text)
        folders = extract_folder_items(response)
        assert isinstance(folders, list)
        return
    # The HAR can omit early response text; request presence is still checked above.
