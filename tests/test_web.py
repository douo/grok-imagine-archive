import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from grok_downloader.archive import Archive
from grok_downloader.web import resolve_media_path
from grok_downloader.web import create_app


def test_resolve_media_path_allows_archive_relative_path(tmp_path) -> None:
    root = tmp_path / "archive"
    media = root / "media" / "images"
    media.mkdir(parents=True)
    image = media / "image.jpg"
    image.write_bytes(b"jpg")

    assert resolve_media_path(root, "media/images/image.jpg") == image.resolve()


def test_resolve_media_path_rejects_path_traversal(tmp_path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"jpg")

    with pytest.raises(HTTPException) as exc:
        resolve_media_path(root, "../outside.jpg")

    assert exc.value.status_code == 404


def test_web_access_token_protects_archive_api(tmp_path, monkeypatch) -> None:
    with Archive("web", root=tmp_path) as archive:
        archive.db.execute("INSERT INTO posts (id, raw_json) VALUES ('post-1', '{}')")
        archive.db.commit()
    monkeypatch.setenv("GROK_DOWNLOADER_ARCHIVE", str(tmp_path))

    client = TestClient(create_app("web", access_token="secret"))

    assert client.get("/api/status").status_code == 401
    assert client.get("/healthz").json() == {"ok": True, "account": "web"}
    authorized = client.get("/api/status", headers={"x-access-token": "secret"})
    assert authorized.status_code == 200
    assert authorized.json()["posts"] == 1


def test_web_query_token_sets_cookie_for_media_requests(tmp_path, monkeypatch) -> None:
    with Archive("web", root=tmp_path):
        pass
    monkeypatch.setenv("GROK_DOWNLOADER_ARCHIVE", str(tmp_path))

    client = TestClient(create_app("web", access_token="secret"))

    first = client.get("/", params={"token": "secret"})
    assert first.status_code == 200
    assert "grok_downloader_token" in first.cookies
    second = client.get("/api/folders")
    assert second.status_code == 200


def test_web_posts_are_account_aware_and_search_ids(tmp_path, monkeypatch) -> None:
    with Archive("web", root=tmp_path) as archive:
        archive.db.execute(
            """
            INSERT INTO posts (id, create_time, media_type, prompt, raw_json)
            VALUES ('web-post-1', '2026-01-02T00:00:00Z', 'MEDIA_POST_TYPE_IMAGE', 'red sun', '{}')
            """
        )
        archive.db.execute(
            """
            INSERT INTO folders (id, name, raw_json)
            VALUES ('saved', 'Saved', '{}')
            """
        )
        archive.db.execute(
            """
            INSERT INTO post_folders (post_id, folder_id, folder_name)
            VALUES ('web-post-1', 'saved', 'Saved')
            """
        )
        archive.db.commit()
    with Archive("other", root=tmp_path) as archive:
        archive.db.execute(
            """
            INSERT INTO posts (id, create_time, media_type, prompt, raw_json)
            VALUES ('other-post-1', '2026-01-03T00:00:00Z', 'MEDIA_POST_TYPE_VIDEO', 'blue moon', '{}')
            """
        )
        archive.db.commit()
    monkeypatch.setenv("GROK_DOWNLOADER_ARCHIVE", str(tmp_path))

    client = TestClient(create_app("web", aliases=["web", "other"]))

    accounts = client.get("/api/accounts").json()
    assert [account["alias"] for account in accounts] == ["web", "other"]

    all_posts = client.get("/api/posts").json()
    assert all_posts["total"] == 2
    assert [(item["account"], item["id"]) for item in all_posts["items"]] == [
        ("other", "other-post-1"),
        ("web", "web-post-1"),
    ]

    account_posts = client.get("/api/posts", params={"account": "web"}).json()
    assert account_posts["total"] == 1
    assert account_posts["items"][0]["account"] == "web"

    id_search = client.get("/api/posts", params={"q": "other-post"}).json()
    assert id_search["total"] == 1
    assert id_search["items"][0]["id"] == "other-post-1"

    folder_search = client.get("/api/posts", params={"folder": "web:saved"}).json()
    assert folder_search["total"] == 1
    assert folder_search["items"][0]["id"] == "web-post-1"


def test_web_detail_summarizes_original_and_nested_relationships(
    tmp_path, monkeypatch
) -> None:
    with Archive("web", root=tmp_path) as archive:
        archive.db.executemany(
            """
            INSERT INTO posts (id, prompt, media_type, raw_json)
            VALUES (?, ?, ?, '{}')
            """,
            [
                ("original-parent", "source", "MEDIA_POST_TYPE_IMAGE"),
                ("nested-parent", "container", "MEDIA_POST_TYPE_IMAGE"),
                ("child", "child prompt", "MEDIA_POST_TYPE_VIDEO"),
                ("derived-child", "derived", "MEDIA_POST_TYPE_IMAGE"),
            ],
        )
        archive.db.executemany(
            """
            INSERT INTO post_edges (parent_post_id, child_post_id, relation)
            VALUES (?, ?, ?)
            """,
            [
                ("original-parent", "child", "original"),
                ("nested-parent", "child", "nested"),
                ("child", "derived-child", "original"),
            ],
        )
        archive.db.execute(
            """
            INSERT INTO assets (asset_key, post_id, kind, role, url, source_path)
            VALUES ('source-asset', 'child', 'image', 'source', 'https://example.test/source.jpg', '$.inputMediaItems[0].imageUrl')
            """
        )
        archive.db.commit()
    monkeypatch.setenv("GROK_DOWNLOADER_ARCHIVE", str(tmp_path))

    client = TestClient(create_app("web"))

    detail = client.get("/api/posts/child", params={"account": "web"}).json()

    assert detail["account"] == "web"
    assert detail["hasUploadedSource"] is True
    assert detail["hasVideo"] is True
    assert detail["relationships"]["originalParents"] == [
        {
            "account": "web",
            "id": "original-parent",
            "prompt": "source",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
        }
    ]
    assert detail["relationships"]["nestedParents"] == [
        {
            "account": "web",
            "id": "nested-parent",
            "prompt": "container",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
        }
    ]
    assert detail["relationships"]["originalChildren"] == [
        {
            "account": "web",
            "id": "derived-child",
            "prompt": "derived",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
        }
    ]
