from grok_imagine_archive.extract import extract_assets, iter_posts


def test_extracts_nested_media_thumbnail_and_source_urls() -> None:
    post = {
        "id": "root",
        "mediaType": "MEDIA_POST_TYPE_IMAGE",
        "mediaUrl": "https://assets.grok.com/root/content",
        "mimeType": "image/jpeg",
        "images": [
            {
                "id": "image-1",
                "mediaType": "MEDIA_POST_TYPE_IMAGE",
                "mediaUrl": "https://assets.grok.com/image-1/content",
                "mimeType": "image/png",
            }
        ],
        "videos": [
            {
                "id": "video-1",
                "mediaType": "MEDIA_POST_TYPE_VIDEO",
                "mediaUrl": "https://assets.grok.com/video-1/generated_video.mp4",
                "thumbnailImageUrl": "https://assets.grok.com/video-1/preview_image.jpg",
                "mimeType": "video/mp4",
            }
        ],
        "inputMediaItems": [
            {
                "id": "input-1",
                "imageUrl": "https://assets.grok.com/input/source.jpg",
                "mimeType": "image/jpeg",
            }
        ],
    }

    posts = list(iter_posts(post))
    assert [item[0]["id"] for item in posts] == ["root", "image-1", "video-1", "input-1"]

    assets = extract_assets(post)
    urls = {asset.url for asset in assets}
    assert "https://assets.grok.com/root/content" in urls
    assert "https://assets.grok.com/image-1/content" in urls
    assert "https://assets.grok.com/video-1/generated_video.mp4" in urls
    assert "https://assets.grok.com/video-1/preview_image.jpg" in urls
    assert "https://assets.grok.com/input/source.jpg" in urls
    assert any(asset.kind == "video" for asset in assets)
    assert any(asset.role == "thumbnail" for asset in assets)
    assert all(asset.kind == "image" for asset in assets if asset.role == "thumbnail")


def test_nested_post_assets_are_owned_by_the_nested_post() -> None:
    post = {
        "id": "root",
        "mediaType": "MEDIA_POST_TYPE_IMAGE",
        "mediaUrl": "https://assets.grok.com/root/content",
        "mimeType": "image/jpeg",
        "childPosts": [
            {
                "id": "child",
                "mediaType": "MEDIA_POST_TYPE_VIDEO",
                "mediaUrl": "https://assets.grok.com/child/video.mp4",
                "thumbnailImageUrl": "https://assets.grok.com/child/preview.jpg",
                "mimeType": "video/mp4",
            }
        ],
    }

    assets = extract_assets(post)
    by_url = {asset.url: asset for asset in assets}
    assert by_url["https://assets.grok.com/root/content"].post_id == "root"
    assert by_url["https://assets.grok.com/child/video.mp4"].post_id == "child"
    assert by_url["https://assets.grok.com/child/preview.jpg"].post_id == "child"
    assert len(assets) == 3
