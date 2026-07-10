from __future__ import annotations

import sqlite3
from secrets import compare_digest
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from .archive import Archive, json_loads


def create_app(
    alias: str,
    *,
    access_token: str = "",
    aliases: list[str] | tuple[str, ...] | None = None,
) -> FastAPI:
    account_aliases = normalize_aliases(alias, aliases)
    app = FastAPI(title=f"Grok Imagine Archive - {alias}")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not access_token or request.url.path == "/healthz":
            return await call_next(request)
        provided = extract_access_token(request)
        if not provided or not compare_digest(provided, access_token):
            if request.url.path == "/":
                return PlainTextResponse("Unauthorized", status_code=401)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        if request.query_params.get("token") == access_token:
            response.set_cookie(
                "grok_imagine_archive_token",
                access_token,
                httponly=True,
                samesite="lax",
                secure=False,
            )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "account": alias}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.replace("__ALIAS__", alias)

    @app.get("/api/accounts")
    def accounts() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in account_aliases:
            archive = Archive(account)
            exists = archive.db_path.exists()
            status: dict[str, Any] | None = None
            if exists:
                opened = archive.open_readonly()
                try:
                    status = opened.status()
                finally:
                    opened.close()
            result.append(
                {
                    "alias": account,
                    "selected": account == alias,
                    "archiveExists": exists,
                    "status": status,
                }
            )
        return result

    @app.get("/api/status")
    def status(account: str = "") -> dict[str, Any]:
        selected = select_accounts(account, account_aliases)
        if len(selected) == 1:
            return account_status(selected[0])
        return {"accounts": [account_status(current) for current in selected]}

    @app.get("/api/folders")
    def folders(account: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for current in select_accounts(account, account_aliases):
            archive = Archive(current)
            if not archive.db_path.exists():
                continue
            opened = archive.open_readonly()
            try:
                account_rows = opened.db.execute(
                    "SELECT id, name FROM folders ORDER BY name COLLATE NOCASE"
                ).fetchall()
            finally:
                opened.close()
            rows.extend(
                {"account": current, "id": row["id"], "name": row["name"]}
                for row in account_rows
            )
        rows.sort(key=lambda row: (row["account"].lower(), (row["name"] or row["id"]).lower()))
        return rows

    @app.get("/api/posts")
    def posts(
        account: str = "",
        folder: str = "",
        media: str = "",
        q: str = "",
        sort: str = "desc",
        has_video: str = "",
        has_upload: str = "",
        has_tree: str = "",
        model: str = "",
        limit: int = Query(80, ge=1, le=300),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        selected = select_accounts(account, account_aliases)
        order = "asc" if sort == "asc" else "desc"
        items: list[dict[str, Any]] = []
        total = 0
        for current in selected:
            page = query_posts_for_account(
                current,
                folder=folder,
                media=media,
                q=q,
                sort=order,
                has_video=truthy(has_video),
                has_upload=truthy(has_upload),
                has_tree=truthy(has_tree),
                model=model,
                limit=offset + limit,
                offset=0,
            )
            items.extend(page["items"])
            total += page["total"]
        items.sort(
            key=lambda item: (
                item.get("createTime") or "",
                item.get("account") or "",
                item.get("id") or "",
            ),
            reverse=order == "desc",
        )
        return {
            "items": items[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/models")
    def models(account: str = "") -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for current in select_accounts(account, account_aliases):
            archive = Archive(current)
            if not archive.db_path.exists():
                continue
            opened = archive.open_readonly()
            try:
                rows = opened.db.execute(
                    """
                    SELECT model_name AS name, COUNT(*) AS n
                      FROM posts
                     WHERE model_name IS NOT NULL AND TRIM(model_name) != ''
                     GROUP BY model_name
                    """
                ).fetchall()
            finally:
                opened.close()
            for row in rows:
                name = str(row["name"] or "").strip()
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + int(row["n"] or 0)
        return [
            {"name": name, "count": counts[name]}
            for name in sorted(counts, key=lambda value: (-counts[value], value.lower()))
        ]

    @app.get("/api/posts/{post_id}")
    def post_detail(post_id: str, account: str = "") -> dict[str, Any]:
        for current in select_accounts(account, account_aliases):
            detail = query_post_detail(current, post_id)
            if detail is not None:
                return detail
        raise HTTPException(status_code=404)

    @app.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        media_account, relative_path = split_media_account(path, alias, account_aliases)
        requested = resolve_media_path(Archive(media_account).root, relative_path)
        if not requested.exists() or not requested.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            requested,
            media_type=guess_media_type(requested),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return app


def normalize_aliases(alias: str, aliases: list[str] | tuple[str, ...] | None) -> list[str]:
    ordered: list[str] = []
    for current in (alias, *(aliases or ())):
        if current and current not in ordered:
            ordered.append(current)
    return ordered


def select_accounts(account: str, aliases: list[str]) -> list[str]:
    if not account:
        return aliases
    if account not in aliases:
        raise HTTPException(status_code=404, detail=f"unknown account: {account}")
    return [account]


def account_status(alias: str) -> dict[str, Any]:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return {"account": alias, "archiveExists": False}
    opened = archive.open_readonly()
    try:
        status = opened.status()
    finally:
        opened.close()
    status["account"] = alias
    status["archiveExists"] = True
    return status


def truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def query_posts_for_account(
    alias: str,
    *,
    folder: str,
    media: str,
    q: str,
    sort: str,
    limit: int,
    offset: int,
    has_video: bool = False,
    has_upload: bool = False,
    has_tree: bool = False,
    model: str = "",
) -> dict[str, Any]:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return {"items": [], "total": 0}
    where: list[str] = []
    params: list[Any] = []
    if folder:
        folder_account, separator, folder_id = folder.partition(":")
        if separator and folder_account != alias:
            return {"items": [], "total": 0}
        where.append(
            "EXISTS (SELECT 1 FROM post_folders pf WHERE pf.post_id = p.id AND pf.folder_id = ?)"
        )
        params.append(folder_id if separator else folder)
    if media:
        where.append("p.media_type = ?")
        params.append(media)
    if has_video:
        where.append(
            "("
            "p.media_type = 'MEDIA_POST_TYPE_VIDEO' OR "
            "EXISTS (SELECT 1 FROM assets a WHERE a.post_id = p.id AND a.kind = 'video')"
            ")"
        )
    if has_upload:
        where.append(
            "EXISTS ("
            "SELECT 1 FROM assets a WHERE a.post_id = p.id AND "
            "(a.role = 'source' OR a.source_path LIKE '%inputMediaItems%')"
            ")"
        )
    if has_tree:
        where.append(
            "EXISTS ("
            "SELECT 1 FROM post_edges e "
            "WHERE e.parent_post_id = p.id OR e.child_post_id = p.id"
            ")"
        )
    if model:
        where.append("p.model_name LIKE ?")
        params.append(f"%{model}%")
    if q:
        needle = q.strip()
        where.append(
            "("
            "p.id = ? OR p.id LIKE ? OR p.prompt LIKE ? OR "
            "p.original_prompt LIKE ? OR IFNULL(p.model_name, '') LIKE ?"
            ")"
        )
        params.extend([needle, f"%{needle}%", f"%{needle}%", f"%{needle}%", f"%{needle}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    order = "ASC" if sort == "asc" else "DESC"
    opened = archive.open_readonly()
    try:
        rows = opened.db.execute(
            f"""
            SELECT p.*, (
                SELECT local_path FROM assets a
                 WHERE a.post_id = p.id AND a.status = 'downloaded'
                 ORDER BY CASE
                   WHEN a.kind = 'video' THEN 0
                   WHEN a.role = 'thumbnail' THEN 1
                   WHEN a.role = 'media' THEN 2
                   ELSE 2
                 END, a.kind
                 LIMIT 1
            ) AS preview_path,
            (
                SELECT kind FROM assets a
                 WHERE a.post_id = p.id AND a.status = 'downloaded'
                 ORDER BY CASE
                   WHEN a.kind = 'video' THEN 0
                   WHEN a.role = 'thumbnail' THEN 1
                   WHEN a.role = 'media' THEN 2
                   ELSE 2
                 END, a.kind
                 LIMIT 1
            ) AS preview_kind,
            EXISTS (
                SELECT 1 FROM assets a
                 WHERE a.post_id = p.id
                   AND (a.role = 'source' OR a.source_path LIKE '%inputMediaItems%')
            ) AS has_uploaded_source,
            EXISTS (
                SELECT 1 FROM assets a
                 WHERE a.post_id = p.id AND a.kind = 'video'
            ) AS has_video,
            (SELECT COUNT(*) FROM post_edges e WHERE e.parent_post_id = p.id) AS child_count,
            (SELECT COUNT(*) FROM post_edges e WHERE e.child_post_id = p.id) AS parent_count
              FROM posts p
              {where_sql}
             ORDER BY p.create_time {order}
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        total = opened.db.execute(
            f"SELECT COUNT(*) AS n FROM posts p {where_sql}",
            params,
        ).fetchone()["n"]
    finally:
        opened.close()
    return {
        "items": [post_row_to_dict(row, account=alias) for row in rows],
        "total": total,
    }


def query_post_detail(alias: str, post_id: str) -> dict[str, Any] | None:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return None
    opened = archive.open_readonly()
    try:
        row = opened.db.execute(
            """
            SELECT p.*,
                   NULL AS preview_path,
                   NULL AS preview_kind,
                   EXISTS (
                     SELECT 1 FROM assets a
                      WHERE a.post_id = p.id
                        AND (a.role = 'source' OR a.source_path LIKE '%inputMediaItems%')
                   ) AS has_uploaded_source,
                   EXISTS (
                     SELECT 1 FROM assets a
                      WHERE a.post_id = p.id AND a.kind = 'video'
                   ) AS has_video,
                   (SELECT COUNT(*) FROM post_edges e WHERE e.parent_post_id = p.id) AS child_count,
                   (SELECT COUNT(*) FROM post_edges e WHERE e.child_post_id = p.id) AS parent_count
              FROM posts p
             WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if not row:
            return None
        assets = opened.db.execute(
            "SELECT * FROM assets WHERE post_id = ? ORDER BY kind, role",
            (post_id,),
        ).fetchall()
        folders = opened.db.execute(
            """
            SELECT folder_id, folder_name
              FROM post_folders
             WHERE post_id = ?
             ORDER BY folder_name
            """,
            (post_id,),
        ).fetchall()
        edges = opened.db.execute(
            """
            SELECT e.parent_post_id, e.child_post_id, e.relation,
                   parent.prompt AS parent_prompt,
                   parent.media_type AS parent_media_type,
                   child.prompt AS child_prompt,
                   child.media_type AS child_media_type
              FROM post_edges e
              LEFT JOIN posts parent ON parent.id = e.parent_post_id
              LEFT JOIN posts child ON child.id = e.child_post_id
             WHERE e.parent_post_id = ? OR e.child_post_id = ?
             ORDER BY e.relation, e.parent_post_id, e.child_post_id
            """,
            (post_id, post_id),
        ).fetchall()
    finally:
        opened.close()
    data = post_row_to_dict(row, account=alias)
    data["raw"] = json_loads(row["raw_json"], {})
    data["assets"] = [asset_row_to_dict(asset) for asset in assets]
    data["folders"] = [dict(folder) for folder in folders]
    data["edges"] = [dict(edge) for edge in edges]
    data["relationships"] = summarize_relationships(alias, post_id, data["edges"])
    data["hasUploadedSource"] = any(
        asset["role"] == "source" or "inputMediaItems" in (asset["sourcePath"] or "")
        for asset in data["assets"]
    )
    data["hasVideo"] = any(asset["kind"] == "video" for asset in data["assets"]) or (
        data["mediaType"] == "MEDIA_POST_TYPE_VIDEO"
    )
    return data


def summarize_relationships(
    alias: str, post_id: str, edges: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    original_parents: list[dict[str, Any]] = []
    original_children: list[dict[str, Any]] = []
    nested_parents: list[dict[str, Any]] = []
    nested_children: list[dict[str, Any]] = []
    for edge in edges:
        parent_id = edge["parent_post_id"]
        child_id = edge["child_post_id"]
        relation = edge["relation"]
        if relation == "original":
            if child_id == post_id:
                original_parents.append(
                    relationship_row(
                        alias, parent_id, edge.get("parent_prompt"), edge.get("parent_media_type")
                    )
                )
            elif parent_id == post_id:
                original_children.append(
                    relationship_row(
                        alias, child_id, edge.get("child_prompt"), edge.get("child_media_type")
                    )
                )
        elif relation == "nested":
            if child_id == post_id:
                nested_parents.append(
                    relationship_row(
                        alias, parent_id, edge.get("parent_prompt"), edge.get("parent_media_type")
                    )
                )
            elif parent_id == post_id:
                nested_children.append(
                    relationship_row(
                        alias, child_id, edge.get("child_prompt"), edge.get("child_media_type")
                    )
                )
    return {
        "originalParents": original_parents,
        "originalChildren": original_children,
        "nestedParents": nested_parents,
        "nestedChildren": nested_children,
    }


def relationship_row(
    alias: str, post_id: str, prompt: str | None, media_type: str | None
) -> dict[str, Any]:
    return {
        "account": alias,
        "id": post_id,
        "prompt": prompt or "",
        "mediaType": media_type or "",
    }


def split_media_account(path: str, default_alias: str, aliases: list[str]) -> tuple[str, str]:
    first, separator, rest = path.partition("/")
    if separator and first in aliases:
        return first, rest
    return default_alias, path


def extract_access_token(request: Request) -> str:
    header_token = request.headers.get("x-access-token", "")
    if header_token:
        return header_token
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    query_token = request.query_params.get("token", "")
    if query_token:
        return query_token
    return request.cookies.get("grok_imagine_archive_token", "")


def resolve_media_path(root: Path, path: str) -> Path:
    archive_root = root.resolve()
    requested = (archive_root / path).resolve()
    try:
        requested.relative_to(archive_root)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return requested


def post_row_to_dict(row: sqlite3.Row, *, account: str = "") -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "account": account,
        "id": row["id"],
        "createTime": row["create_time"],
        "mediaType": row["media_type"],
        "mimeType": row["mime_type"],
        "prompt": row["prompt"],
        "originalPrompt": row["original_prompt"],
        "modelName": row["model_name"],
        "width": row["width"],
        "height": row["height"],
        "originalPostId": row["original_post_id"],
        "parentPostId": row["parent_post_id"],
        "isLiked": bool(row["is_liked"]),
        "rRated": bool(row["r_rated"]),
        "previewPath": row["preview_path"] if "preview_path" in keys else None,
        "previewKind": row["preview_kind"] if "preview_kind" in keys else None,
        "hasUploadedSource": bool(row["has_uploaded_source"]) if "has_uploaded_source" in keys else False,
        "hasVideo": bool(row["has_video"]) if "has_video" in keys else False,
        "childCount": row["child_count"] if "child_count" in keys else 0,
        "parentCount": row["parent_count"] if "parent_count" in keys else 0,
    }


def asset_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "assetKey": row["asset_key"],
        "postId": row["post_id"],
        "kind": row["kind"],
        "role": row["role"],
        "url": row["url"],
        "mimeType": row["mime_type"],
        "sourcePath": row["source_path"],
        "localPath": row["local_path"],
        "status": row["status"],
        "sha256": row["sha256"],
        "size": row["size"],
        "failReason": row["fail_reason"],
    }


def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grok Imagine Archive · __ALIAS__</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #050505;
      --bg-elevated: #0c0c0e;
      --surface: #121214;
      --surface-2: #1a1a1e;
      --surface-3: #222228;
      --text: #f5f5f7;
      --muted: #8e8e96;
      --faint: #5c5c66;
      --line: rgba(255,255,255,0.08);
      --line-strong: rgba(255,255,255,0.14);
      --accent: #f4f4f5;
      --accent-soft: rgba(255,255,255,0.08);
      --blue: #7aa2ff;
      --cyan: #5ec8d8;
      --green: #6fbf86;
      --amber: #d6b36a;
      --red: #e06b63;
      --radius: 18px;
      --radius-sm: 12px;
      --shadow: 0 18px 50px rgba(0,0,0,0.45);
      --ease: cubic-bezier(0.22, 1, 0.36, 1);
      --header-h: 64px;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      background:
        radial-gradient(1200px 600px at 50% -10%, rgba(90,100,140,0.16), transparent 60%),
        radial-gradient(800px 400px at 100% 0%, rgba(70,80,120,0.08), transparent 50%),
        var(--bg);
      -webkit-font-smoothing: antialiased;
    }
    button, input, select { font: inherit; color: inherit; }
    button { cursor: pointer; }
    a { color: inherit; }

    /* Header */
    .top {
      position: sticky;
      top: 0;
      z-index: 40;
      height: var(--header-h);
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(5,5,5,0.72);
      backdrop-filter: blur(22px) saturate(1.2);
      -webkit-backdrop-filter: blur(22px) saturate(1.2);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      flex: 0 0 auto;
      min-width: 0;
      text-decoration: none;
    }
    .brand-mark {
      width: 28px;
      height: 28px;
      border-radius: 9px;
      background:
        linear-gradient(145deg, #fff 0%, #c7c7d1 40%, #6d6d7a 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.5);
    }
    .brand-text {
      font-size: 15px;
      font-weight: 650;
      letter-spacing: -0.03em;
      white-space: nowrap;
    }
    .brand-sub {
      display: block;
      margin-top: -2px;
      color: var(--faint);
      font-size: 11px;
      font-weight: 500;
    }
    .search-shell {
      flex: 1 1 auto;
      position: relative;
      max-width: 560px;
    }
    .search-shell svg {
      position: absolute;
      left: 14px;
      top: 50%;
      width: 16px;
      height: 16px;
      transform: translateY(-50%);
      color: var(--faint);
      pointer-events: none;
    }
    #q {
      width: 100%;
      height: 40px;
      padding: 0 84px 0 40px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,0.04);
      outline: none;
      transition: border-color .2s var(--ease), background .2s var(--ease), box-shadow .2s var(--ease);
    }
    #q:focus {
      border-color: rgba(255,255,255,0.22);
      background: rgba(255,255,255,0.06);
      box-shadow: 0 0 0 4px rgba(255,255,255,0.04);
    }
    #q::placeholder { color: var(--faint); }
    .search-hint {
      position: absolute;
      right: 12px;
      top: 50%;
      transform: translateY(-50%);
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--faint);
      font-size: 11px;
      font-weight: 600;
      pointer-events: none;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: auto;
    }
    .icon-btn, .ghost-btn, .chip, .select-like {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
      color: var(--text);
      transition: background .15s var(--ease), border-color .15s var(--ease), color .15s var(--ease);
    }
    .icon-btn {
      width: 36px;
      display: grid;
      place-items: center;
      padding: 0;
    }
    .icon-btn:hover, .ghost-btn:hover, .chip:hover, .select-like:hover {
      background: rgba(255,255,255,0.07);
      border-color: var(--line-strong);
    }
    .ghost-btn {
      padding: 0 14px;
      font-size: 13px;
      font-weight: 550;
    }
    .select-like {
      padding: 0 12px;
      max-width: 150px;
      appearance: none;
      background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
      background-position: calc(100% - 14px) 55%, calc(100% - 10px) 55%;
      background-size: 4px 4px, 4px 4px;
      background-repeat: no-repeat;
      padding-right: 26px;
    }

    /* Filter strip */
    .filter-strip {
      position: sticky;
      top: var(--header-h);
      z-index: 30;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      padding: 12px 18px 10px;
      border-bottom: 1px solid transparent;
      background: linear-gradient(180deg, rgba(5,5,5,0.92), rgba(5,5,5,0.78));
      backdrop-filter: blur(16px);
    }
    .chip-row, .meta-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0 12px;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--muted);
      user-select: none;
    }
    .chip.active {
      color: #0a0a0a;
      background: #f2f2f4;
      border-color: #f2f2f4;
    }
    .chip .dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.7;
    }
    .meta-row { margin-left: auto; }
    .count-pill {
      color: var(--muted);
      font-size: 12.5px;
      font-weight: 550;
      letter-spacing: -0.01em;
    }
    .active-filters {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      width: 100%;
    }
    .active-filters:empty { display: none; }
    .filter-tag {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(122,162,255,0.08);
      color: #c9d7ff;
      font-size: 12px;
      font-weight: 550;
    }
    .filter-tag button {
      border: 0;
      background: transparent;
      color: inherit;
      padding: 0;
      line-height: 1;
      opacity: 0.75;
    }
    .filter-tag button:hover { opacity: 1; }

    /* Main */
    main {
      padding: 18px 18px 80px;
      max-width: 1800px;
      margin: 0 auto;
    }
    .grid {
      columns: 5 220px;
      column-gap: 12px;
    }
    .tile {
      position: relative;
      display: inline-block;
      width: 100%;
      margin: 0 0 12px;
      break-inside: avoid;
      border-radius: var(--radius);
      overflow: hidden;
      background: var(--surface);
      border: 1px solid transparent;
      cursor: pointer;
      isolation: isolate;
      transition: transform .25s var(--ease), border-color .2s var(--ease), box-shadow .25s var(--ease);
    }
    .tile:hover {
      transform: translateY(-2px);
      border-color: rgba(255,255,255,0.12);
      box-shadow: var(--shadow);
    }
    .tile.active {
      border-color: rgba(255,255,255,0.35);
      box-shadow: 0 0 0 1px rgba(255,255,255,0.18), var(--shadow);
    }
    .media-wrap {
      position: relative;
      display: block;
      width: 100%;
      background: #0a0a0c;
      overflow: hidden;
    }
    .media-wrap.loading::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(110deg, #121214 20%, #1b1b20 40%, #121214 60%);
      background-size: 200% 100%;
      animation: shimmer 1.4s linear infinite;
      z-index: 1;
    }
    @keyframes shimmer {
      to { background-position: -200% 0; }
    }
    .tile img, .tile video {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      opacity: 0;
      transition: opacity .35s var(--ease), transform .4s var(--ease);
    }
    .tile:hover img.loaded, .tile:hover video.loaded { transform: scale(1.03); }
    .tile img.loaded, .tile video.loaded { opacity: 1; }
    .badge-row {
      position: absolute;
      top: 10px;
      left: 10px;
      right: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      z-index: 2;
      pointer-events: none;
    }
    .badge {
      height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(0,0,0,0.55);
      color: #f3f3f5;
      font-size: 10.5px;
      font-weight: 700;
      letter-spacing: 0.02em;
      backdrop-filter: blur(8px);
      display: inline-flex;
      align-items: center;
    }
    .badge.video { color: #d9f7ff; border-color: rgba(94,200,216,0.35); }
    .badge.source { color: #fff0cc; border-color: rgba(214,179,106,0.35); }
    .badge.account { color: #dff5e6; border-color: rgba(111,191,134,0.35); }
    .badge.tree { color: #e4e0ff; border-color: rgba(150,140,255,0.35); }
    .tile-overlay {
      position: absolute;
      left: 0; right: 0; bottom: 0;
      padding: 28px 12px 12px;
      background: linear-gradient(180deg, transparent, rgba(0,0,0,0.82));
      opacity: 0;
      transform: translateY(6px);
      transition: opacity .2s var(--ease), transform .2s var(--ease);
      z-index: 2;
    }
    .tile:hover .tile-overlay, .tile:focus-within .tile-overlay, .tile.active .tile-overlay {
      opacity: 1;
      transform: none;
    }
    .tile-prompt {
      font-size: 12.5px;
      font-weight: 550;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .tile-sub {
      margin-top: 6px;
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: rgba(255,255,255,0.62);
      font-size: 11px;
      font-variant-numeric: tabular-nums;
    }
    .tile-sub span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .empty, .loading-more {
      display: none;
      text-align: center;
      color: var(--muted);
      padding: 48px 12px;
      font-weight: 500;
    }
    .empty.show, .loading-more.show { display: block; }
    .sentinel { height: 1px; }

    .progress-float {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 25;
      min-width: 54px;
      height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(12,12,14,0.85);
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      display: grid;
      place-items: center;
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
    }

    /* Dialog */
    dialog {
      width: min(1280px, 96vw);
      height: min(900px, 94vh);
      margin: auto;
      border: 1px solid var(--line-strong);
      border-radius: 22px;
      padding: 0;
      color: var(--text);
      background: #0a0a0c;
      box-shadow: 0 40px 100px rgba(0,0,0,0.65);
      overflow: hidden;
    }
    dialog::backdrop {
      background: rgba(0,0,0,0.72);
      backdrop-filter: blur(10px);
    }
    .detail {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(340px, 0.75fr);
      height: 100%;
    }
    .viewer {
      position: relative;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background:
        radial-gradient(circle at 50% 40%, rgba(255,255,255,0.04), transparent 55%),
        #050506;
    }
    .viewer img, .viewer video {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
    }
    .viewer video { width: 100%; background: #000; }
    .nav-btn {
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      width: 42px;
      height: 42px;
      border-radius: 50%;
      border: 1px solid var(--line-strong);
      background: rgba(10,10,12,0.7);
      color: var(--text);
      display: grid;
      place-items: center;
      backdrop-filter: blur(10px);
      z-index: 3;
    }
    .nav-btn:hover { background: rgba(255,255,255,0.1); }
    .nav-btn.prev { left: 14px; }
    .nav-btn.next { right: 14px; }
    .nav-btn:disabled { opacity: 0.25; cursor: default; }
    .side {
      overflow: auto;
      border-left: 1px solid var(--line);
      background: rgba(12,12,14,0.96);
      padding: 18px 18px 28px;
    }
    .side-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 14px;
    }
    .post-id {
      font-size: 14px;
      font-weight: 700;
      letter-spacing: -0.02em;
      overflow-wrap: anywhere;
    }
    .kv {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .side-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 14px;
    }
    .side-actions button, .side .close {
      height: 32px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.04);
      padding: 0 12px;
      font-size: 12px;
      font-weight: 600;
    }
    .side-actions button:hover, .side .close:hover {
      background: rgba(255,255,255,0.08);
    }
    .side h2 {
      margin: 18px 0 8px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--faint);
    }
    .side h2:first-of-type { margin-top: 8px; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #070709;
      max-height: 220px;
      overflow: auto;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
      color: #d7d7de;
    }
    .pill-line, .line-list { display: flex; flex-wrap: wrap; gap: 6px; }
    .line-list { display: grid; gap: 6px; }
    .line-item, .asset, .rel-item {
      color: var(--muted);
      font-size: 12.5px;
      overflow-wrap: anywhere;
    }
    .asset, .rel-item {
      padding: 10px 0;
      border-top: 1px solid var(--line);
    }
    .rel-item button {
      height: auto;
      min-height: 30px;
      margin-bottom: 4px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.04);
      padding: 6px 10px;
      text-align: left;
      font-size: 12px;
      font-weight: 600;
    }
    .rel-item button:hover { background: rgba(255,255,255,0.08); }
    .asset-actions { display: flex; gap: 6px; margin-top: 6px; }
    .asset-actions a, .asset-actions button {
      height: 28px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      padding: 0 10px;
      font-size: 11.5px;
      font-weight: 600;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }

    /* Help modal */
    .help-panel {
      position: fixed;
      right: 18px;
      bottom: 62px;
      z-index: 35;
      width: min(320px, calc(100vw - 36px));
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line-strong);
      background: rgba(12,12,14,0.94);
      box-shadow: var(--shadow);
      display: none;
    }
    .help-panel.show { display: block; }
    .help-panel h3 {
      margin: 0 0 8px;
      font-size: 13px;
      font-weight: 700;
    }
    .help-panel dl {
      margin: 0;
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .help-panel dt {
      color: var(--text);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 24px;
      transform: translateX(-50%) translateY(12px);
      z-index: 50;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      background: rgba(20,20,24,0.95);
      color: var(--text);
      font-size: 12.5px;
      font-weight: 600;
      opacity: 0;
      pointer-events: none;
      transition: opacity .2s var(--ease), transform .2s var(--ease);
    }
    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }

    @media (max-width: 960px) {
      .brand-sub { display: none; }
      .top { gap: 8px; padding: 0 12px; }
      .select-like { max-width: 120px; }
      .detail { grid-template-columns: 1fr; }
      .viewer { min-height: 42vh; }
      .side { border-left: 0; border-top: 1px solid var(--line); }
      .meta-row { margin-left: 0; width: 100%; }
    }
    @media (max-width: 640px) {
      .grid { columns: 2 140px; column-gap: 8px; }
      .top-actions .ghost-btn { display: none; }
      .brand-text { font-size: 13px; }
    }
  </style>
</head>
<body>
  <header class="top">
    <a class="brand" href="/" title="Home">
      <span class="brand-mark" aria-hidden="true"></span>
      <span>
        <span class="brand-text">Imagine Archive</span>
        <span class="brand-sub">__ALIAS__</span>
      </span>
    </a>
    <div class="search-shell">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      <input id="q" placeholder="Search prompt, model, or paste post ID" autocomplete="off" spellcheck="false">
      <span class="search-hint">/</span>
    </div>
    <div class="top-actions">
      <select id="account" class="select-like" title="Account"><option value="">All accounts</option></select>
      <select id="folder" class="select-like" title="Folder"><option value="">All folders</option></select>
      <select id="model" class="select-like" title="Model"><option value="">All models</option></select>
      <select id="sort" class="select-like" title="Sort"><option value="desc">Newest</option><option value="asc">Oldest</option></select>
      <button type="button" class="ghost-btn" id="jumpBtn" title="Jump to post ID">Jump</button>
      <button type="button" class="icon-btn" id="helpBtn" title="Keyboard shortcuts">?</button>
    </div>
  </header>

  <div class="filter-strip">
    <div class="chip-row" id="chipRow">
      <button type="button" class="chip active" data-filter="all"><span class="dot"></span>All</button>
      <button type="button" class="chip" data-filter="image"><span class="dot"></span>Images</button>
      <button type="button" class="chip" data-filter="video"><span class="dot"></span>Videos</button>
      <button type="button" class="chip" data-filter="upload"><span class="dot"></span>Uploads</button>
      <button type="button" class="chip" data-filter="tree"><span class="dot"></span>Has tree</button>
    </div>
    <div class="meta-row">
      <div class="count-pill" id="count">0 / 0</div>
      <button type="button" class="ghost-btn" id="clearFilters" hidden>Clear filters</button>
    </div>
    <div class="active-filters" id="activeFilters"></div>
  </div>

  <main>
    <div id="grid" class="grid"></div>
    <div id="empty" class="empty">No posts match the current filters.</div>
    <div id="loading-more" class="loading-more">Loading more…</div>
    <div id="sentinel" class="sentinel"></div>
  </main>

  <div id="progress" class="progress-float">0%</div>
  <div id="help" class="help-panel" role="dialog" aria-label="Shortcuts">
    <h3>Keyboard shortcuts</h3>
    <dl>
      <dt>/</dt><dd>Focus search</dd>
      <dt>Esc</dt><dd>Close detail / blur search</dd>
      <dt>← →</dt><dd>Previous / next post in list</dd>
      <dt>J</dt><dd>Jump to post ID</dd>
      <dt>C</dt><dd>Copy current post ID</dd>
      <dt>L</dt><dd>Copy shareable link</dd>
      <dt>?</dt><dd>Toggle this help</dd>
    </dl>
  </div>
  <div id="toast" class="toast"></div>
  <dialog id="dialog"><div id="detail" class="detail"></div></dialog>

  <script>
    const state = {
      offset: 0,
      limit: 80,
      total: 0,
      loading: false,
      exhausted: false,
      suppressClose: false,
      items: [],
      activeIndex: -1,
      pendingFolder: '',
      media: '',
      hasVideo: false,
      hasUpload: false,
      treeOnly: false,
    };

    const grid = document.querySelector('#grid');
    const dialog = document.querySelector('#dialog');
    const detail = document.querySelector('#detail');
    const els = {
      account: document.querySelector('#account'),
      folder: document.querySelector('#folder'),
      model: document.querySelector('#model'),
      q: document.querySelector('#q'),
      sort: document.querySelector('#sort'),
      count: document.querySelector('#count'),
      empty: document.querySelector('#empty'),
      progress: document.querySelector('#progress'),
      loading: document.querySelector('#loading-more'),
      activeFilters: document.querySelector('#activeFilters'),
      clearFilters: document.querySelector('#clearFilters'),
      help: document.querySelector('#help'),
      toast: document.querySelector('#toast'),
    };

    function toast(message) {
      els.toast.textContent = message;
      els.toast.classList.add('show');
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => els.toast.classList.remove('show'), 1600);
    }

    function apiParams(extra = {}) {
      const params = new URLSearchParams({
        account: els.account.value,
        folder: els.folder.value,
        model: els.model.value,
        media: state.media,
        q: els.q.value.trim(),
        sort: els.sort.value,
        has_video: state.hasVideo ? '1' : '',
        has_upload: state.hasUpload ? '1' : '',
        has_tree: state.treeOnly ? '1' : '',
        limit: state.limit,
        offset: state.offset,
        ...extra,
      });
      for (const [key, value] of [...params.entries()]) if (!value && value !== 0) params.delete(key);
      return params;
    }

    function mediaUrl(account, path) {
      if (!path) return '';
      return '/media/' + encodeURI(account + '/' + path);
    }

    function pageUrl(post = null) {
      const params = new URLSearchParams();
      for (const [id, value] of Object.entries({
        account: els.account.value,
        folder: els.folder.value,
        model: els.model.value,
        media: state.media,
        sort: els.sort.value,
        q: els.q.value.trim(),
        has_video: state.hasVideo ? '1' : '',
        has_upload: state.hasUpload ? '1' : '',
        tree: state.treeOnly ? '1' : '',
      })) {
        if (value) params.set(id, value);
      }
      if (post) {
        params.set('post', post.id);
        params.set('postAccount', post.account);
      }
      const query = params.toString();
      return location.pathname + (query ? '?' + query : '');
    }

    function syncControlsFromUrl() {
      const params = new URLSearchParams(location.search);
      els.account.value = params.get('account') || '';
      els.model.value = params.get('model') || '';
      els.sort.value = params.get('sort') || 'desc';
      els.q.value = params.get('q') || '';
      state.pendingFolder = params.get('folder') || '';
      state.media = params.get('media') || '';
      state.hasVideo = params.get('has_video') === '1' || state.media === 'MEDIA_POST_TYPE_VIDEO';
      state.hasUpload = params.get('has_upload') === '1';
      state.treeOnly = params.get('tree') === '1';
      if (state.media === 'MEDIA_POST_TYPE_IMAGE') state.hasVideo = false;
      syncChips();
    }

    function syncChips() {
      const mode = state.hasUpload ? 'upload'
        : state.treeOnly ? 'tree'
        : state.media === 'MEDIA_POST_TYPE_VIDEO' || state.hasVideo ? 'video'
        : state.media === 'MEDIA_POST_TYPE_IMAGE' ? 'image'
        : 'all';
      for (const chip of document.querySelectorAll('.chip')) {
        chip.classList.toggle('active', chip.dataset.filter === mode);
      }
      renderActiveFilters();
    }

    function renderActiveFilters() {
      const tags = [];
      if (els.account.value) tags.push(['account', 'Account: ' + els.account.value]);
      if (els.folder.value) {
        const label = els.folder.selectedOptions[0]?.textContent || els.folder.value;
        tags.push(['folder', label]);
      }
      if (els.model.value) tags.push(['model', 'Model: ' + els.model.value]);
      if (els.q.value.trim()) tags.push(['q', 'Search: ' + els.q.value.trim()]);
      if (state.media === 'MEDIA_POST_TYPE_IMAGE') tags.push(['media', 'Images']);
      if (state.media === 'MEDIA_POST_TYPE_VIDEO' || state.hasVideo) tags.push(['video', 'Videos']);
      if (state.hasUpload) tags.push(['upload', 'Uploads']);
      if (state.treeOnly) tags.push(['tree', 'Has tree']);
      if (els.sort.value === 'asc') tags.push(['sort', 'Oldest first']);

      els.activeFilters.replaceChildren();
      for (const [key, label] of tags) {
        const tag = document.createElement('span');
        tag.className = 'filter-tag';
        tag.innerHTML = '<span></span><button type="button" aria-label="Remove">×</button>';
        tag.querySelector('span').textContent = label;
        tag.querySelector('button').addEventListener('click', () => clearOneFilter(key));
        els.activeFilters.appendChild(tag);
      }
      els.clearFilters.hidden = tags.length === 0;
    }

    function clearOneFilter(key) {
      if (key === 'account') els.account.value = '';
      if (key === 'folder') els.folder.value = '';
      if (key === 'model') els.model.value = '';
      if (key === 'q') els.q.value = '';
      if (key === 'media') state.media = '';
      if (key === 'video') { state.media = ''; state.hasVideo = false; }
      if (key === 'upload') state.hasUpload = false;
      if (key === 'tree') state.treeOnly = false;
      if (key === 'sort') els.sort.value = 'desc';
      syncChips();
      refreshFromControls(true);
    }

    function clearAllFilters() {
      els.account.value = '';
      els.folder.value = '';
      els.model.value = '';
      els.q.value = '';
      els.sort.value = 'desc';
      state.media = '';
      state.hasVideo = false;
      state.hasUpload = false;
      state.treeOnly = false;
      syncChips();
      refreshFromControls(true);
    }

    async function loadAccounts() {
      const accounts = await fetch('/api/accounts').then(r => r.json());
      for (const account of accounts) {
        const option = document.createElement('option');
        option.value = account.alias;
        const count = account.status && account.status.posts != null ? ` (${account.status.posts})` : '';
        option.textContent = account.alias + count;
        els.account.appendChild(option);
      }
    }

    async function loadFolders() {
      const selected = state.pendingFolder || els.folder.value;
      state.pendingFolder = '';
      const folders = await fetch('/api/folders?' + apiParams({ limit: '', offset: '', media: '', q: '', model: '', has_video: '', has_upload: '' })).then(r => r.json());
      els.folder.replaceChildren(new Option('All folders', ''));
      for (const folder of folders) {
        const option = document.createElement('option');
        option.value = folder.account + ':' + folder.id;
        option.textContent = `[${folder.account}] ${folder.name || folder.id}`;
        els.folder.appendChild(option);
      }
      els.folder.value = [...els.folder.options].some(o => o.value === selected) ? selected : '';
    }

    async function loadModels() {
      const selected = els.model.value;
      const models = await fetch('/api/models?' + apiParams({ limit: '', offset: '', media: '', q: '', folder: '', model: '', has_video: '', has_upload: '' })).then(r => r.json());
      els.model.replaceChildren(new Option('All models', ''));
      for (const model of models) {
        const option = document.createElement('option');
        option.value = model.name;
        option.textContent = `${model.name} (${model.count})`;
        els.model.appendChild(option);
      }
      els.model.value = [...els.model.options].some(o => o.value === selected) ? selected : '';
    }

    function resetList() {
      state.offset = 0;
      state.total = 0;
      state.exhausted = false;
      state.items = [];
      state.activeIndex = -1;
      grid.replaceChildren();
      els.empty.classList.remove('show');
      updateCount();
    }


    async function load(reset = false) {
      if (reset) resetList();
      if (state.loading || state.exhausted) return;
      state.loading = true;
      els.loading.classList.add('show');
      try {
        const data = await fetch('/api/posts?' + apiParams()).then(r => r.json());
        const items = data.items || [];
        state.total = data.total;
        state.offset += items.length;
        state.exhausted = state.offset >= state.total || items.length === 0;
        for (const item of items) {
          state.items.push(item);
          grid.appendChild(tile(item, state.items.length - 1));
        }
        els.empty.classList.toggle('show', state.items.length === 0 && state.exhausted);
        maybeAutoOpenExact();
      } finally {
        state.loading = false;
        els.loading.classList.remove('show');
        updateCount();
        renderActiveFilters();
      }
    }

    function maybeAutoOpenExact() {
      const q = els.q.value.trim();
      if (!q || state.items.length === 0) return;
      const exact = state.items.find(item => item.id === q);
      if (exact && state.items.filter(item => item.id === q).length === 1 && state.total === 1) {
        openDetail(exact.id, exact.account, true);
      }
    }

    function updateCount() {
      els.count.textContent = `${state.items.length} / ${state.total}`;
      updateProgress();
    }

    function updateProgress() {
      const scrollMax = Math.max(1, document.documentElement.scrollHeight - innerHeight);
      const scrollFraction = Math.min(1, Math.max(0, scrollY / scrollMax));
      const loadedFraction = state.total ? Math.min(1, state.items.length / state.total) : 0;
      els.progress.textContent = Math.round(scrollFraction * loadedFraction * 100) + '%';
    }

    function badge(text, kind = '') {
      const span = document.createElement('span');
      span.className = 'badge ' + kind;
      span.textContent = text;
      return span;
    }

    function previewMedia(item) {
      const wrap = document.createElement('div');
      wrap.className = 'media-wrap';
      wrap.style.aspectRatio = (item.width > 0 && item.height > 0) ? `${item.width} / ${item.height}` : '1 / 1';
      if (!item.previewPath) {
        const missing = document.createElement('div');
        missing.style.cssText = 'display:grid;place-items:center;min-height:140px;color:var(--muted);font-size:12px;';
        missing.textContent = 'Missing media';
        wrap.appendChild(missing);
      } else if (item.previewKind === 'video') {
        const video = document.createElement('video');
        wrap.classList.add('loading');
        video.addEventListener('loadeddata', () => { wrap.classList.remove('loading'); video.classList.add('loaded'); });
        video.addEventListener('error', () => wrap.classList.remove('loading'));
        video.src = mediaUrl(item.account, item.previewPath);
        video.muted = true; video.loop = true; video.playsInline = true; video.preload = 'metadata';
        wrap.appendChild(video);
        wrap.addEventListener('mouseenter', () => video.play().catch(() => {}));
        wrap.addEventListener('mouseleave', () => { video.pause(); video.currentTime = 0; });
      } else {
        const img = document.createElement('img');
        wrap.classList.add('loading');
        img.addEventListener('load', () => { wrap.classList.remove('loading'); img.classList.add('loaded'); });
        img.addEventListener('error', () => wrap.classList.remove('loading'));
        img.src = mediaUrl(item.account, item.previewPath);
        img.loading = 'lazy'; img.decoding = 'async';
        img.alt = item.prompt || item.id;
        wrap.appendChild(img);
      }
      const badges = document.createElement('div');
      badges.className = 'badge-row';
      badges.appendChild(badge(item.account, 'account'));
      if (item.hasVideo || item.previewKind === 'video') badges.appendChild(badge('VIDEO', 'video'));
      if (item.hasUploadedSource) badges.appendChild(badge('UPLOAD', 'source'));
      if (item.childCount || item.parentCount) badges.appendChild(badge(`TREE ${item.parentCount || 0}/${item.childCount || 0}`, 'tree'));
      wrap.appendChild(badges);

      const overlay = document.createElement('div');
      overlay.className = 'tile-overlay';
      const prompt = document.createElement('div');
      prompt.className = 'tile-prompt';
      prompt.textContent = item.prompt || item.originalPrompt || item.id;
      const sub = document.createElement('div');
      sub.className = 'tile-sub';
      const id = document.createElement('span');
      id.textContent = shortId(item.id);
      id.title = item.id;
      const time = document.createElement('span');
      time.textContent = formatTime(item.createTime);
      sub.append(id, time);
      overlay.append(prompt, sub);
      wrap.appendChild(overlay);
      return wrap;
    }

    function shortId(id) {
      if (!id) return '';
      return id.length > 12 ? id.slice(0, 8) + '…' : id;
    }

    function formatTime(value) {
      if (!value) return '';
      try {
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      } catch {
        return value;
      }
    }

    function tile(item, index) {
      const div = document.createElement('article');
      div.className = 'tile';
      div.dataset.id = item.id;
      div.dataset.account = item.account;
      div.dataset.index = String(index);
      div.tabIndex = 0;
      div.appendChild(previewMedia(item));
      div.addEventListener('click', () => openDetail(item.id, item.account, true));
      div.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openDetail(item.id, item.account, true);
        }
      });
      return div;
    }

    function setActiveIndex(index) {
      state.activeIndex = index;
      for (const node of grid.querySelectorAll('.tile')) node.classList.remove('active');
      if (index < 0 || index >= state.items.length) return;
      const node = grid.querySelector(`.tile[data-index="${index}"]`);
      if (node) {
        node.classList.add('active');
        node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    }

    async function openDetail(id, account, push = false) {
      const params = new URLSearchParams({ account });
      const data = await fetch('/api/posts/' + encodeURIComponent(id) + '?' + params).then(r => r.json());
      const index = state.items.findIndex(item => item.id === id && item.account === account);
      setActiveIndex(index);
      renderDetail(data, index);
      if (!dialog.open) dialog.showModal();
      if (push) history.pushState({ post: id, account }, '', pageUrl(data));
    }

    function detailMedia(data) {
      const primary = data.assets.find(a => a.localPath && a.role === 'media')
        || data.assets.find(a => a.localPath && a.role !== 'thumbnail')
        || data.assets.find(a => a.localPath);
      const viewer = document.createElement('div');
      viewer.className = 'viewer';
      if (!primary) {
        const missing = document.createElement('div');
        missing.className = 'kv';
        missing.textContent = 'Missing media';
        viewer.appendChild(missing);
      } else if (primary.kind === 'video') {
        const video = document.createElement('video');
        video.src = mediaUrl(data.account, primary.localPath);
        video.controls = true; video.playsInline = true; video.preload = 'metadata';
        viewer.appendChild(video);
      } else {
        const img = document.createElement('img');
        img.src = mediaUrl(data.account, primary.localPath);
        img.decoding = 'async';
        img.alt = data.prompt || data.id;
        viewer.appendChild(img);
      }
      return viewer;
    }

    function renderDetail(data, index) {
      const viewer = detailMedia(data);
      const prev = document.createElement('button');
      prev.type = 'button'; prev.className = 'nav-btn prev'; prev.textContent = '‹';
      prev.disabled = index <= 0;
      prev.addEventListener('click', () => navigate(-1));
      const next = document.createElement('button');
      next.type = 'button'; next.className = 'nav-btn next'; next.textContent = '›';
      next.disabled = index < 0 || index >= state.items.length - 1;
      next.addEventListener('click', () => navigate(1));
      viewer.append(prev, next);

      const side = document.createElement('div');
      side.className = 'side';
      side.innerHTML = `
        <div class="side-head">
          <div>
            <div class="post-id"></div>
            <div class="kv summary"></div>
          </div>
          <button type="button" class="close">Close</button>
        </div>
        <div class="side-actions">
          <button type="button" class="copy-id">Copy ID</button>
          <button type="button" class="copy-link">Copy link</button>
          <button type="button" class="locate">Show in grid</button>
        </div>
        <div class="pill-line"></div>
        <h2>Prompt</h2><pre class="prompt-full"></pre>
        <h2>Folders</h2><div class="folders line-list"></div>
        <h2>Original parent</h2><div class="original-parents"></div>
        <h2>Nested parent</h2><div class="nested-parents"></div>
        <h2>Child posts</h2><div class="children"></div>
        <h2>Assets</h2><div class="assets"></div>
        <h2>Raw JSON</h2><pre class="raw"></pre>`;
      side.querySelector('.post-id').textContent = data.id;
      side.querySelector('.summary').textContent = [
        data.account,
        prettyMedia(data.mediaType),
        (data.width && data.height) ? `${data.width}×${data.height}` : '',
        data.modelName || '',
        formatTime(data.createTime),
      ].filter(Boolean).join(' · ');
      side.querySelector('.prompt-full').textContent = data.prompt || data.originalPrompt || '';
      side.querySelector('.raw').textContent = JSON.stringify(data.raw, null, 2);
      const pills = side.querySelector('.pill-line');
      pills.appendChild(badge(data.account, 'account'));
      if (data.hasVideo) pills.appendChild(badge('VIDEO', 'video'));
      if (data.hasUploadedSource) pills.appendChild(badge('UPLOAD', 'source'));
      if (data.modelName) pills.appendChild(badge(data.modelName, ''));
      renderFolders(side.querySelector('.folders'), data.folders || []);
      renderRelations(side.querySelector('.original-parents'), data.relationships?.originalParents || [], 'Source');
      renderRelations(side.querySelector('.nested-parents'), data.relationships?.nestedParents || [], 'Container');
      renderRelations(
        side.querySelector('.children'),
        [...(data.relationships?.nestedChildren || []), ...(data.relationships?.originalChildren || [])],
        'Child'
      );
      renderAssets(side.querySelector('.assets'), data.assets || [], data.account);
      side.querySelector('.close').addEventListener('click', () => closeDetail(true));
      side.querySelector('.copy-id').addEventListener('click', async () => {
        await navigator.clipboard.writeText(data.id);
        toast('Post ID copied');
      });
      side.querySelector('.copy-link').addEventListener('click', async () => {
        const url = location.origin + pageUrl(data);
        await navigator.clipboard.writeText(url);
        toast('Link copied');
      });
      side.querySelector('.locate').addEventListener('click', () => {
        closeDetail(true);
        setActiveIndex(index);
        toast('Located in grid');
      });
      detail.dataset.account = data.account;
      detail.replaceChildren(viewer, side);
    }

    function prettyMedia(type) {
      if (type === 'MEDIA_POST_TYPE_VIDEO') return 'Video';
      if (type === 'MEDIA_POST_TYPE_IMAGE') return 'Image';
      return type || '';
    }

    function renderFolders(node, folders) {
      if (!folders.length) { node.textContent = 'None'; return; }
      for (const folder of folders) {
        const row = document.createElement('div');
        row.className = 'line-item';
        row.style.display = 'flex';
        row.style.alignItems = 'center';
        row.style.gap = '8px';
        const label = document.createElement('span');
        label.textContent = folder.folder_name || folder.folder_id;
        label.title = folder.folder_id || '';
        const filterBtn = document.createElement('button');
        filterBtn.type = 'button';
        filterBtn.style.cssText = 'height:26px;border-radius:8px;border:1px solid var(--line);background:rgba(255,255,255,.04);padding:0 8px;font-size:11px;font-weight:600;';
        filterBtn.textContent = 'Filter';
        filterBtn.addEventListener('click', () => {
          const account = detail.dataset.account || els.account.value;
          const value = account ? `${account}:${folder.folder_id}` : folder.folder_id;
          if (![...els.folder.options].some(o => o.value === value)) {
            els.folder.appendChild(new Option(folder.folder_name || folder.folder_id, value));
          }
          els.folder.value = value;
          closeDetail(false);
          refreshFromControls(true);
        });
        row.append(label, filterBtn);
        node.appendChild(row);
      }
    }

    function renderRelations(node, relations, prefix) {
      if (!relations.length) {
        node.textContent = 'None';
        node.className = 'line-item';
        return;
      }
      node.className = '';
      for (const rel of relations) {
        const row = document.createElement('div');
        row.className = 'rel-item';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = `${prefix}: ${rel.id}`;
        btn.addEventListener('click', () => openDetail(rel.id, rel.account, true));
        const prompt = document.createElement('div');
        prompt.textContent = rel.prompt || prettyMedia(rel.mediaType) || '';
        row.append(btn, prompt);
        node.appendChild(row);
      }
    }

    function renderAssets(node, assets, account) {
      if (!assets.length) {
        node.textContent = 'None';
        return;
      }
      for (const asset of assets) {
        const row = document.createElement('div');
        row.className = 'asset';
        const title = document.createElement('div');
        title.textContent = `${asset.kind} / ${asset.role} / ${asset.status} / ${formatBytes(asset.size || 0)}`;
        const path = document.createElement('div');
        path.className = 'line-item';
        path.textContent = asset.localPath || asset.url || '';
        row.append(title, path);
        if (asset.localPath) {
          const actions = document.createElement('div');
          actions.className = 'asset-actions';
          const open = document.createElement('a');
          open.href = mediaUrl(account, asset.localPath);
          open.target = '_blank';
          open.rel = 'noopener';
          open.textContent = 'Open';
          const copy = document.createElement('button');
          copy.type = 'button';
          copy.textContent = 'Copy path';
          copy.addEventListener('click', async () => {
            await navigator.clipboard.writeText(asset.localPath);
            toast('Path copied');
          });
          actions.append(open, copy);
          row.appendChild(actions);
        }
        node.appendChild(row);
      }
    }

    function formatBytes(n) {
      if (!n) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0; let v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
      return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    function closeDetail(push = false) {
      state.suppressClose = true;
      if (dialog.open) dialog.close();
      state.suppressClose = false;
      if (push) history.pushState({}, '', pageUrl());
    }

    async function navigate(delta) {
      if (state.activeIndex < 0) return;
      const next = state.activeIndex + delta;
      if (next < 0) return;
      if (next >= state.items.length) {
        if (!state.exhausted) {
          await load(false);
        }
      }
      if (next < 0 || next >= state.items.length) return;
      const item = state.items[next];
      await openDetail(item.id, item.account, true);
    }

    async function refreshFromControls(push = true) {
      if (push) history.pushState({}, '', pageUrl());
      await loadFolders();
      await loadModels();
      await load(true);
    }

    async function jumpToId() {
      const raw = prompt('Paste post ID to open:', els.q.value.trim());
      if (!raw) return;
      const id = raw.trim();
      els.q.value = id;
      await refreshFromControls(true);
      // try open from loaded items or direct detail
      const hit = state.items.find(item => item.id === id);
      if (hit) {
        await openDetail(hit.id, hit.account, true);
        return;
      }
      // brute force across selected accounts
      const accounts = els.account.value
        ? [els.account.value]
        : [...els.account.options].map(o => o.value).filter(Boolean);
      for (const account of (accounts.length ? accounts : ['__ALIAS__'])) {
        const res = await fetch('/api/posts/' + encodeURIComponent(id) + '?' + new URLSearchParams({ account }));
        if (res.ok) {
          const data = await res.json();
          await openDetail(data.id, data.account, true);
          toast('Opened ' + shortId(id));
          return;
        }
      }
      toast('Post not found');
    }

    // Events
    document.querySelector('#chipRow').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      const mode = chip.dataset.filter;
      state.media = '';
      state.hasVideo = false;
      state.hasUpload = false;
      state.treeOnly = false;
      if (mode === 'image') state.media = 'MEDIA_POST_TYPE_IMAGE';
      if (mode === 'video') { state.media = 'MEDIA_POST_TYPE_VIDEO'; state.hasVideo = true; }
      if (mode === 'upload') state.hasUpload = true;
      if (mode === 'tree') state.treeOnly = true;
      syncChips();
      refreshFromControls(true);
    });
    els.clearFilters.addEventListener('click', clearAllFilters);
    document.querySelector('#helpBtn').addEventListener('click', () => els.help.classList.toggle('show'));
    document.querySelector('#jumpBtn').addEventListener('click', jumpToId);

    dialog.addEventListener('click', event => { if (event.target === dialog) closeDetail(true); });
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDetail(true); });
    dialog.addEventListener('close', () => {
      if (!state.suppressClose && new URLSearchParams(location.search).has('post')) closeDetail(true);
    });

    els.account.addEventListener('change', () => {
      els.folder.value = '';
      refreshFromControls(true);
    });
    for (const control of [els.folder, els.model, els.sort]) {
      control.addEventListener('change', () => refreshFromControls(true));
    }
    els.q.addEventListener('input', () => {
      clearTimeout(window._searchTimer);
      window._searchTimer = setTimeout(() => refreshFromControls(true), 220);
    });
    els.q.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        refreshFromControls(true).then(() => {
          const q = els.q.value.trim();
          const exact = state.items.find(item => item.id === q);
          if (exact) openDetail(exact.id, exact.account, true);
        });
      }
    });

    addEventListener('scroll', updateProgress, { passive: true });
    addEventListener('resize', updateProgress);
    addEventListener('popstate', async () => {
      syncControlsFromUrl();
      await loadFolders();
      await loadModels();
      await load(true);
      const params = new URLSearchParams(location.search);
      if (params.has('post')) {
        await openDetail(params.get('post'), params.get('postAccount') || els.account.value, false);
      } else if (dialog.open) {
        state.suppressClose = true;
        dialog.close();
        state.suppressClose = false;
      }
    });

    addEventListener('keydown', (event) => {
      const tag = (event.target && event.target.tagName) || '';
      const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target?.isContentEditable;
      if (event.key === '/' && !typing) {
        event.preventDefault();
        els.q.focus();
        els.q.select();
        return;
      }
      if (event.key === 'Escape') {
        if (els.help.classList.contains('show')) { els.help.classList.remove('show'); return; }
        if (dialog.open) { closeDetail(true); return; }
        if (document.activeElement === els.q) els.q.blur();
        return;
      }
      if (typing) return;
      if (event.key === '?' ) { els.help.classList.toggle('show'); return; }
      if (event.key === 'j' || event.key === 'J') { jumpToId(); return; }
      if (event.key === 'ArrowLeft') { if (dialog.open) navigate(-1); return; }
      if (event.key === 'ArrowRight') { if (dialog.open) navigate(1); return; }
      if ((event.key === 'c' || event.key === 'C') && dialog.open && state.activeIndex >= 0) {
        const item = state.items[state.activeIndex];
        if (item) navigator.clipboard.writeText(item.id).then(() => toast('Post ID copied'));
        return;
      }
      if ((event.key === 'l' || event.key === 'L') && dialog.open && state.activeIndex >= 0) {
        const item = state.items[state.activeIndex];
        if (item) navigator.clipboard.writeText(location.origin + pageUrl(item)).then(() => toast('Link copied'));
      }
    });

    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) load(false);
    }, { rootMargin: '900px 0px' });
    observer.observe(document.querySelector('#sentinel'));

    (async function init() {
      await loadAccounts();
      syncControlsFromUrl();
      // restore control values after options load
      const params = new URLSearchParams(location.search);
      if (params.get('account')) els.account.value = params.get('account');
      await loadFolders();
      await loadModels();
      if (params.get('model')) {
        if (![...els.model.options].some(o => o.value === params.get('model'))) {
          els.model.appendChild(new Option(params.get('model'), params.get('model')));
        }
        els.model.value = params.get('model');
      }
      await load(true);
      if (params.has('post')) {
        await openDetail(params.get('post'), params.get('postAccount') || els.account.value, false);
      }
    })();
  </script>
</body>
</html>
"""
